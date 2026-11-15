import os
import sqlite3
import tempfile
import unittest
from local_store import Store, LOCAL_SCHEMA_VERSION


class LocalSchemaVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=os.path.join(self.temp.name,'workspace.db')

    def tearDown(self):
        self.temp.cleanup()

    def version(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('PRAGMA user_version').fetchone()[0]

    def test_new_workspace_records_the_current_version(self):
        Store(self.path,seed=False)
        self.assertEqual(self.version(),LOCAL_SCHEMA_VERSION)

    def test_legacy_workspace_is_upgraded_without_losing_rows(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE suppliers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,contacts TEXT,offers TEXT,sources TEXT,warnings TEXT,notes TEXT,checked_at TEXT,created_at TEXT,UNIQUE(url,mode))")
            db.execute("INSERT INTO suppliers(id,name,url,mode,status,reason,contacts,offers,sources,warnings,notes,created_at) VALUES(1,'旧行','https://a.example','real','done','',?,?,?,?,?,'2026-11-15T00:00:00+00:00')",('{"email":"a@b"}','[]','[]','[]',''))
        self.assertEqual(self.version(),0)
        store=Store(self.path,seed=False)
        self.assertEqual(self.version(),LOCAL_SCHEMA_VERSION)
        row=store.get(1)
        self.assertEqual(row['name'],'旧行')
        self.assertEqual(row['contacts'],{'email':'a@b'})
        self.assertEqual(row['contact_status'],'legacy')

    def test_workspace_from_a_newer_release_is_refused(self):
        Store(self.path,seed=False)
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA user_version=%d' % (LOCAL_SCHEMA_VERSION+1))
        with self.assertRaises(RuntimeError):
            Store(self.path,seed=False)


if __name__=='__main__':unittest.main()
