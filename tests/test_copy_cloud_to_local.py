"""P28: the cloud-to-local copy only takes one account's records and never touches secrets."""

import contextlib
import copy
import datetime
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from copy_cloud_to_local import copy_cloud_to_local, DEFAULT_LOCAL  # noqa: E402
from engine import DEFAULT_RULES  # noqa: E402
from local_store import Store  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return [dict(row) for row in self.rows]

    def __iter__(self):
        return iter([dict(row) for row in self.rows])


class FakeDatabase:
    """Stands in for Postgres: row-level security is emulated with an owner filter."""

    def __init__(self, owner, tables):
        self.owner = owner
        self.tables = tables
        self.statements = []

    def execute(self, statement, parameters=()):
        self.statements.append(statement)
        if not statement.lstrip().upper().startswith('SELECT'):
            raise AssertionError('云端只能读：' + statement)
        table = statement.split(' FROM ')[1].split()[0]
        rows = [row for row in self.tables.get(table, []) if row.get('owner') == self.owner]
        rows.sort(key=lambda row: row['id'])
        return FakeCursor(rows)

    @contextlib.contextmanager
    def transaction(self, write=False):
        if write:
            raise AssertionError('复制过程不能写云端')
        yield self


class FakeCloudStore:
    def __init__(self, owner, tables):
        self.username = owner
        self.database = FakeDatabase(owner, tables)
        self.followups_by_owner = {
            '小姜': {
                '11': {'status': 'replied', 'note': '已回电', 'updated_at': '2026-11-16T10:00:00+00:00'},
            },
            '别人': {'12': {'status': 'called', 'note': '别人的记录'}},
        }
        self.rules_by_owner = {'小姜': copy.deepcopy(DEFAULT_RULES), '别人': copy.deepcopy(DEFAULT_RULES)}

    def followups(self):
        return copy.deepcopy(self.followups_by_owner.get(self.username, {}))

    def rules(self):
        return copy.deepcopy(self.rules_by_owner[self.username])


def cloud_tables():
    supplier = {
        'name': '示例站点',
        'mode': 'real',
        'status': 'qualified',
        'reason': '人工复核',
        'contacts': {'email': ['sales@a.example']},
        'offers': [{'model': 'kimi-k3.1-pro', 'ratio': 0.5}],
        'sources': [{'url': 'https://a.example', 'text': '原文'}],
        'warnings': [],
        'notes': '客户要发票',
        'checked_at': datetime.datetime(2026, 11, 16, 9, 30, tzinfo=datetime.timezone.utc),
        'created_at': '2026-11-15T10:00:00+00:00',
        'contact_status': 'found',
        'contact_checked_at': '2026-11-16T09:31:00+00:00',
        'contact_result': {'email': ['sales@a.example'], 'source': '接口'},
        'archived_at': None,
    }
    return {
        'suppliers': [
            dict(id=11, url='https://a.example', owner='小姜', **supplier),
            dict(
                id=12,
                url='https://b.example',
                owner='小姜',
                **dict(supplier, name='第二个站', contacts={'qq': ['123']}, offers=[], sources=[]),
            ),
            dict(
                id=21,
                url='https://secret.example',
                owner='别人',
                **dict(supplier, name='别人的站', notes='别人的备注'),
            ),
        ],
        'events': [
            {'id': 1, 'supplier_id': 11, 'mode': 'real', 'message': '采集完成', 'created_at': '2026-11-16T09:00:00+00:00', 'owner': '小姜'},
            {'id': 2, 'supplier_id': 21, 'mode': 'real', 'message': '别人的事件', 'created_at': '2026-11-16T09:00:00+00:00', 'owner': '别人'},
        ],
        'history': [
            {'id': 1, 'supplier_id': 11, 'mode': 'real', 'offers': [{'model': 'kimi-k3.1-pro'}], 'created_at': '2026-11-16T09:00:00+00:00', 'owner': '小姜'},
            {'id': 2, 'supplier_id': 21, 'mode': 'real', 'offers': [], 'created_at': '2026-11-16T09:00:00+00:00', 'owner': '别人'},
        ],
        'drafts': [
            {'id': 1, 'supplier_id': 11, 'subject': '报价', 'body': '正文', 'created_at': '2026-11-16T09:05:00+00:00', 'owner': '小姜'},
            {'id': 2, 'supplier_id': 21, 'subject': '别人的草稿', 'body': '正文', 'created_at': '2026-11-16T09:05:00+00:00', 'owner': '别人'},
        ],
        'settings': [
            {'key': 'model-service:user:小姜', 'value': '{"api_key":"sk-secret"}', 'owner': '小姜'},
            {'key': 'model-service:user:别人', 'value': '{"api_key":"sk-other"}', 'owner': '别人'},
        ],
    }


class FakeRow:
    def fetchone(self):
        return None


class FakeCloudDatabase:
    def __init__(self):
        self.writes = []
        self.username = None

    def for_user(self, username):
        scoped = FakeCloudDatabase()
        scoped.username = username
        scoped.writes = self.writes
        return scoped

    @contextlib.contextmanager
    def transaction(self, write=False):
        if write:
            self.writes.append('write')
        yield self

    def execute(self, statement, parameters=()):
        return FakeRow()


class CopyCloudToLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'supplier.db'
        self.store = FakeCloudStore('小姜', cloud_tables())

    def test_copies_only_requested_account_and_reads_only(self):
        counts, backup = copy_cloud_to_local('小姜', self.path, store=self.store)
        self.assertIsNone(backup)
        self.assertEqual(counts['suppliers'], 2)
        self.assertEqual(counts['events'], 1)
        self.assertEqual(counts['history'], 1)
        self.assertEqual(counts['drafts'], 1)
        self.assertEqual(counts['followups'], 1)
        self.assertEqual(counts['rules'], 1)

        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM suppliers WHERE mode='real' ORDER BY id").fetchall()
            self.assertEqual([row['url'] for row in rows], ['https://a.example', 'https://b.example'])
            self.assertEqual(json.loads(rows[0]['contacts']), {'email': ['sales@a.example']})
            self.assertEqual(rows[0]['checked_at'], '2026-11-16T09:30:00+00:00')
            self.assertEqual(json.loads(rows[1]['offers']), [])
            self.assertEqual(rows[0]['contact_status'], 'found')
            keys = {row[0] for row in db.execute('SELECT key FROM settings')}
            self.assertNotIn('model-service:user:小姜', keys)
            self.assertNotIn('model-service:user:别人', keys)
        self.assertFalse(any('settings' in statement for statement in self.store.database.statements))

    def test_copied_workspace_reads_contacts_followups_and_rules(self):
        copy_cloud_to_local('小姜', self.path, store=self.store)
        store = Store(self.path, seed=False)
        records = store.followups()
        self.assertEqual(len(records), 1)
        self.assertEqual(next(iter(records.values()))['note'], '已回电')
        self.assertEqual(store.rules(), copy.deepcopy(DEFAULT_RULES))

    def test_refuses_when_local_workspace_has_real_suppliers(self):
        store = Store(self.path)
        store.import_rows([{'url': 'https://local.example'}])
        with self.assertRaises(SystemExit):
            copy_cloud_to_local('小姜', self.path, store=self.store)
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM suppliers WHERE url='https://local.example'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM suppliers WHERE url='https://a.example'").fetchone()[0], 0)

    def test_backs_up_existing_local_file_before_writing(self):
        Store(self.path, seed=False)
        copy_cloud_to_local('小姜', self.path, store=self.store)
        backups = sorted(Path(self.temp.name).glob('supplier.db.bak-*'))
        self.assertEqual(len(backups), 1)

    def test_other_account_sees_nothing(self):
        counts, _ = copy_cloud_to_local('别人', self.path, store=FakeCloudStore('别人', cloud_tables()))
        self.assertEqual(counts['suppliers'], 1)
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            urls = [row[0] for row in db.execute("SELECT url FROM suppliers WHERE mode='real'")]
        self.assertEqual(urls, ['https://secret.example'])

    def test_secret_never_reaches_local_database(self):
        copy_cloud_to_local('小姜', self.path, store=self.store)
        self.assertNotIn(b'sk-secret', self.path.read_bytes())


class ReadOnlyCloudStoreTests(unittest.TestCase):
    """The copy must not write to the cloud, not even the legacy rules bootstrap row."""

    def test_bootstrap_false_opens_no_write_transaction(self):
        import os

        os.environ.setdefault('SCOUT_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')
        from cloud_store import PostgresStore

        database = FakeCloudDatabase()
        PostgresStore(database=database, bootstrap=False)
        self.assertEqual(database.writes, [])

    def test_default_bootstrap_keeps_legacy_behaviour(self):
        import os

        os.environ.setdefault('SCOUT_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')
        from cloud_store import PostgresStore

        database = FakeCloudDatabase()
        PostgresStore(database=database)
        self.assertEqual(len(database.writes), 1)


class DefaultPathTests(unittest.TestCase):
    def test_default_local_path_is_the_workspace_database(self):
        self.assertEqual(DEFAULT_LOCAL, ROOT / 'data' / 'supplier.db')


if __name__ == '__main__':
    unittest.main()
