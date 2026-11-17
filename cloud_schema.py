"""Additive Postgres migrations, applied by the process that needs them.

The private schema lives outside the deploy: `cloud_schema.sql` and the files in
`migrations/postgres/` are run by hand against Supabase.  A deploy that reads a new
column can therefore reach production before the column exists, and every page that
reads the row fails with `UndefinedColumn` - a plain-text 500 instead of JSON, which
the browser then reports as "is not valid JSON".

This module closes that gap.  Before the first request (or the daemon's first job)
each migration is checked against the catalog; the DDL is issued only when the object
is missing, so a healthy start costs one read-only round trip and never takes a table
lock.  A migration that fails is reported and then ignored: a schema problem must not
turn into an outage, and the old code path keeps working.

Only additive, non-rewriting migrations belong here.  `0002_json_and_time_columns`
converts column types and rewrites table contents, so it stays a manual,
maintenance-window step.

Creating the objects needs the app role to own the schema (or to hold CREATE on it),
which the runtime role `scout_app` does not: a missing object is then reported with
the exact GRANT that unblocks it, and the request carries on instead of failing.
"""

import os
import threading

import psycopg

SCHEMA = 'scout_private'
LOCK = 8_314_770_102  # arbitrary constant, namespaced by the advisory-lock catalog


class Migration:
    """One migration file: a catalog check plus the idempotent DDL it guards."""

    __slots__ = ('version', 'check', 'statements')

    def __init__(self, version, check, statements):
        self.version = version
        self.check = check
        self.statements = statements


def _column(table, column):
    return (
        "SELECT 1 FROM information_schema.columns WHERE table_schema='%s'"
        " AND table_name='%s' AND column_name='%s'" % (SCHEMA, table, column)
    )


def _table(name):
    return "SELECT 1 FROM information_schema.tables WHERE table_schema='%s' AND table_name='%s'" % (
        SCHEMA,
        name,
    )


def _grant(table, privileges):
    return (
        'DO $block$ BEGIN IF NOT EXISTS (SELECT 1 FROM information_schema.role_table_grants'
        " WHERE table_schema='%s' AND table_name='%s' AND grantee='scout_app') THEN"
        ' GRANT %s ON %s.%s TO scout_app; END IF; END $block$' % (SCHEMA, table, privileges, SCHEMA, table)
    )


def _record(version):
    return (
        "DO $block$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.tables"
        " WHERE table_schema='%s' AND table_name='schema_migrations') THEN"
        " INSERT INTO %s.schema_migrations(version) VALUES ('%s') ON CONFLICT DO NOTHING;"
        ' END IF; END $block$' % (SCHEMA, SCHEMA, version)
    )


MIGRATIONS = (
    Migration(
        '0001_schema_migrations',
        _table('schema_migrations'),
        (
            'CREATE TABLE IF NOT EXISTS %s.schema_migrations('
            'version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())' % SCHEMA,
            _grant('schema_migrations', 'SELECT'),
            _record('0001_schema_migrations'),
        ),
    ),
    Migration(
        '0003_archived_at',
        _column('suppliers', 'archived_at'),
        (
            'ALTER TABLE %s.suppliers ADD COLUMN IF NOT EXISTS archived_at TEXT' % SCHEMA,
            _record('0003_archived_at'),
        ),
    ),
    Migration(
        '0003_worker_status',
        _table('cloud_worker_status'),
        (
            'CREATE TABLE IF NOT EXISTS %s.cloud_worker_status('
            'id TEXT PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL,'
            " heartbeat_at TIMESTAMPTZ NOT NULL, state TEXT NOT NULL DEFAULT 'unknown',"
            " job_id TEXT NOT NULL DEFAULT '', job_owner TEXT NOT NULL DEFAULT '',"
            " site TEXT NOT NULL DEFAULT '', error_text TEXT NOT NULL DEFAULT '',"
            " version TEXT NOT NULL DEFAULT '')" % SCHEMA,
            _grant('cloud_worker_status', 'SELECT,INSERT,UPDATE,DELETE'),
            _record('0003_worker_status'),
        ),
    ),
)

_LOCK = threading.Lock()
_ATTEMPTED = False
_APPLIED = set()


def applied():
    """Versions this process has applied, for tests and diagnostics."""
    return sorted(_APPLIED)


def reset_state():
    """Forget the once-per-process guard. Tests only."""
    global _ATTEMPTED
    with _LOCK:
        _ATTEMPTED = False
        _APPLIED.clear()


def ensure(database, force=False):
    """Apply the migrations the catalog is missing. Never raises.

    `database` is a `cloud_store.Database`. Runs at most once per process, so a warm
    Vercel instance pays no cost after the first request.
    """
    global _ATTEMPTED
    if os.environ.get('SCOUT_AUTO_MIGRATE') == '0':
        return []
    with _LOCK:
        if _ATTEMPTED and not force:
            return []
        _ATTEMPTED = True
        try:
            pending = [
                migration
                for migration in MIGRATIONS
                if migration.version not in _APPLIED and not _satisfied(database, migration)
            ]
            if not pending:
                return []
            return _apply(database, pending)
        except Exception as error:  # schema trouble must never break the app
            print('[schema] migration check skipped: %r' % (error,), flush=True)
            return []


def _satisfied(database, migration):
    with database.transaction() as db:
        return bool(db.execute(migration.check).fetchone())


def _apply(database, pending):
    finished = []
    with database.transaction(write=True) as db:
        # Two instances can start at the same moment; only one of them migrates.
        db.execute('SELECT pg_advisory_xact_lock(?)', (LOCK,))
        for migration in pending:
            if db.execute(migration.check).fetchone():
                _APPLIED.add(migration.version)
                continue
            connection = db.connection
            # A savepoint keeps one failing migration from poisoning the others.
            connection.execute('SAVEPOINT scout_migration')
            try:
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute('RELEASE SAVEPOINT scout_migration')
            except psycopg.Error as error:
                connection.execute('ROLLBACK TO SAVEPOINT scout_migration')
                if isinstance(error, psycopg.errors.InsufficientPrivilege):
                    print(
                        '[schema] %s not applied: the app role may not change the schema.'
                        ' Run migrations/postgres/%s.sql as the database owner, or grant it:'
                        ' GRANT CREATE ON SCHEMA %s TO scout_app'
                        % (migration.version, migration.version, SCHEMA),
                        flush=True,
                    )
                    continue
                print('[schema] %s failed: %s' % (migration.version, error), flush=True)
                continue
            _APPLIED.add(migration.version)
            finished.append(migration.version)
            print('[schema] applied %s' % migration.version, flush=True)
    return finished
