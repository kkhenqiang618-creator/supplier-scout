"""The self-healing migration must touch only what is missing and never break a start."""

import contextlib
import importlib.util
import os
import re
import unittest
from unittest.mock import patch

import cloud_schema


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeDatabase:
    """Enough of `cloud_store.Database` to exercise the catalog checks and the DDL."""

    def __init__(self, tables=(), columns=(), fail=None, broken=False):
        self.tables = set(tables)
        self.columns = set(columns)
        self.fail = fail
        self.fail_error = None
        self.broken = broken
        self.statements = []
        self.opened = 0

    @contextlib.contextmanager
    def transaction(self, write=False):
        self.opened += 1
        if self.broken:
            raise RuntimeError('connection refused')
        yield FakeSQL(self)

    def present(self, statement):
        match = re.search(r"table_schema='scout_private'(?: AND table_name='(\w+)')?", statement)
        if 'information_schema.columns' in statement:
            name, column = (
                re.search(r"table_name='(\w+)'", statement),
                re.search(r"column_name='(\w+)'", statement),
            )
            return (name.group(1), column.group(1)) in self.columns
        if match and match.group(1):
            return match.group(1) in self.tables
        return False

    def run_ddl(self, statement):
        self.statements.append(statement)
        if self.fail and self.fail in statement:
            raise psycopg_error(getattr(self, 'fail_error', None))
        created = re.match(r'CREATE TABLE IF NOT EXISTS scout_private\.(\w+)\(', statement)
        if created:
            self.tables.add(created.group(1))
        added = re.match(r'ALTER TABLE scout_private\.(\w+) ADD COLUMN IF NOT EXISTS (\w+)', statement)
        if added:
            self.columns.add((added.group(1), added.group(2)))
        return Cursor()


class FakeConnection:
    def __init__(self, database):
        self.database = database

    def execute(self, statement):
        if statement.split()[0] in ('SAVEPOINT', 'RELEASE', 'ROLLBACK'):
            return Cursor()
        return self.database.run_ddl(statement)


class FakeSQL:
    def __init__(self, database):
        self.database = database
        self.connection = FakeConnection(database)

    def execute(self, statement, parameters=()):
        if 'pg_advisory_xact_lock' in statement:
            return Cursor()
        if 'information_schema' in statement:
            return Cursor((1,) if self.database.present(statement) else None)
        return self.database.run_ddl(statement)


def psycopg_error(kind=None):
    import psycopg

    if kind == 'privilege':
        return psycopg.errors.InsufficientPrivilege('permission denied for schema scout_private')
    return psycopg.errors.UndefinedTable('boom')


@unittest.skipUnless(importlib.util.find_spec('psycopg'), 'cloud adapter dependencies are optional locally')
class CloudSchemaTests(unittest.TestCase):
    def setUp(self):
        cloud_schema.reset_state()
        self.addCleanup(cloud_schema.reset_state)

    def complete(self, **kwargs):
        return FakeDatabase(
            tables={'schema_migrations', 'cloud_worker_status'},
            columns={('suppliers', 'archived_at')},
            **kwargs,
        )

    def test_healthy_schema_runs_no_ddl(self):
        database = self.complete()
        self.assertEqual(cloud_schema.ensure(database), [])
        self.assertEqual(database.statements, [])

    def test_missing_column_is_added(self):
        database = FakeDatabase(tables={'schema_migrations', 'cloud_worker_status'})
        self.assertEqual(cloud_schema.ensure(database), ['0003_archived_at'])
        self.assertIn(
            'ALTER TABLE scout_private.suppliers ADD COLUMN IF NOT EXISTS archived_at TEXT',
            database.statements,
        )
        self.assertIn(('suppliers', 'archived_at'), database.columns)

    def test_missing_tables_are_created(self):
        database = FakeDatabase()
        applied = cloud_schema.ensure(database)
        self.assertEqual(applied, ['0001_schema_migrations', '0003_archived_at', '0003_worker_status'])
        self.assertIn('cloud_worker_status', database.tables)
        self.assertIn('schema_migrations', database.tables)

    def test_one_failing_migration_does_not_block_the_rest(self):
        database = FakeDatabase(fail='cloud_worker_status(')
        applied = cloud_schema.ensure(database)
        self.assertEqual(applied, ['0001_schema_migrations', '0003_archived_at'])
        self.assertIn(('suppliers', 'archived_at'), database.columns)

    def test_runs_once_per_process(self):
        database = FakeDatabase()
        cloud_schema.ensure(database)
        opened = database.opened
        self.assertEqual(cloud_schema.ensure(database), [])
        self.assertEqual(database.opened, opened)

    def test_missing_privilege_is_reported_not_raised(self):
        database = FakeDatabase(fail='CREATE TABLE')
        database.fail_error = 'privilege'
        self.assertEqual(cloud_schema.ensure(database), ['0003_archived_at'])
        self.assertIn(('suppliers', 'archived_at'), database.columns)

    def test_unreachable_database_is_not_fatal(self):
        database = FakeDatabase(broken=True)
        self.assertEqual(cloud_schema.ensure(database), [])

    def test_can_be_disabled(self):
        database = FakeDatabase()
        with patch.dict(os.environ, {'SCOUT_AUTO_MIGRATE': '0'}):
            self.assertEqual(cloud_schema.ensure(database), [])
        self.assertEqual(database.opened, 0)

    def test_every_version_is_idempotent_ddl(self):
        for migration in cloud_schema.MIGRATIONS:
            for statement in migration.statements:
                self.assertTrue(
                    'IF NOT EXISTS' in statement or 'DO $block$' in statement,
                    '%s is not idempotent: %s' % (migration.version, statement),
                )


if __name__ == '__main__':
    unittest.main()
