"""Writes made without a logged-in user must name their owner explicitly (P9, PR #8 review)."""
import contextlib
import os
import re
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from cloud_store import CloudModelService, LEGACY_OWNER, PostgresStore

OWNED_TABLES = {'suppliers', 'settings', 'events', 'history', 'drafts', 'cloud_jobs'}
INSERT_PATTERN = re.compile(r'INSERT(?:\s+OR\s+IGNORE)?\s+INTO\s+(\w+)\s*\(([^)]*)\)', re.I)


class Cursor:
    rowcount = 0
    def fetchone(self): return None
    def fetchall(self): return []


class ProbeDatabase:
    """Models the migrated Postgres schema: NOT NULL is checked before ON CONFLICT resolves,
    so an ownerless insert fails even when the row it would collide with already exists."""

    def __init__(self, username=None, statements=None):
        self.username = username
        self.statements = statements if statements is not None else []

    def for_user(self, username):
        return ProbeDatabase(username, self.statements)

    @contextlib.contextmanager
    def transaction(self, write=False):
        yield self

    def execute(self, statement, parameters=()):
        sql = ' '.join(statement.split())
        match = INSERT_PATTERN.match(sql)
        if match and self.username is None and match.group(1) in OWNED_TABLES:
            columns = [column.strip() for column in match.group(2).split(',')]
            if 'owner' not in columns:
                raise AssertionError('系统身份写入 %s 必须显式带 owner：%s' % (match.group(1), sql))
        self.statements.append((sql, tuple(parameters)))
        return Cursor()


class SystemIdentityOwnerTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'SCOUT_ENCRYPTION_KEY': Fernet.generate_key().decode()})
        env.start()
        self.addCleanup(env.stop)

    def inserts(self, database, table):
        pattern = re.compile(r'INSERT(?:\s+OR\s+IGNORE)?\s+INTO\s+' + table + r'\b', re.I)
        return [row for row in database.statements if pattern.match(row[0])]

    def test_startup_rules_insert_states_legacy_owner(self):
        database = ProbeDatabase()
        PostgresStore(database)
        inserts = self.inserts(database, 'settings')
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][0], 'rules')
        self.assertEqual(inserts[0][1][2], LEGACY_OWNER)

    def test_system_model_config_insert_states_legacy_owner(self):
        database = ProbeDatabase()
        service = CloudModelService(database, Fernet.generate_key().decode())
        service.save({'base_url': 'https://example.com/v1', 'model': 'fixture', 'api_key': 'secret'})
        inserts = self.inserts(database, 'settings')
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][2], LEGACY_OWNER)

    def test_user_scoped_insert_uses_the_logged_in_owner(self):
        database = ProbeDatabase()
        service = CloudModelService(database.for_user('小吴'), Fernet.generate_key().decode())
        service.save({'base_url': 'https://example.com/v1', 'model': 'fixture', 'api_key': 'secret'})
        inserts = self.inserts(database, 'settings')
        self.assertEqual(inserts[0][1][2], '小吴')


if __name__ == '__main__':
    unittest.main()
