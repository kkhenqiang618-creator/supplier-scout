import unittest
from datetime import datetime, timezone
from psycopg.types.json import Jsonb
from collection import encode, now
from cloud_store import SQL, adapt_placeholders, adapt_value


class FakeCursor:
    rowcount=0
    def fetchone(self):return None
    def fetchall(self):return []


class FakeConnection:
    def __init__(self):self.seen=None
    def execute(self,statement,parameters=()):self.seen=(statement,parameters);return FakeCursor()


class PlaceholderTests(unittest.TestCase):
    def test_rewrites_placeholders_outside_literals(self):
        self.assertEqual(adapt_placeholders('SELECT 1 FROM t WHERE id=? AND status=?'),'SELECT 1 FROM t WHERE id=%s AND status=%s')

    def test_leaves_question_marks_and_percent_inside_literals(self):
        self.assertEqual(adapt_placeholders("SELECT 1 FROM t WHERE note='100%? done' AND id=?"),
                         "SELECT 1 FROM t WHERE note='100%%? done' AND id=%s")

    def test_keeps_doubled_quotes_inside_literals(self):
        self.assertEqual(adapt_placeholders("SELECT 1 FROM t WHERE note='it''s 50%?' AND id=?"),
                         "SELECT 1 FROM t WHERE note='it''s 50%%?' AND id=%s")

    def test_does_not_reinterpret_postgres_identifiers(self):
        self.assertEqual(adapt_placeholders('SELECT "owner" FROM t WHERE id=?'),'SELECT "owner" FROM t WHERE id=%s')


class StatementShapeTests(unittest.TestCase):
    def test_insert_or_ignore_gets_conflict_clause(self):
        connection=FakeConnection()
        SQL(connection).execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',('k','v'))
        self.assertEqual(connection.seen[0],'INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT DO NOTHING')

    def test_supplier_insert_gets_returning_id(self):
        connection=FakeConnection()
        SQL(connection).execute('INSERT INTO suppliers(name) VALUES(?)',('n',))
        self.assertEqual(connection.seen[0],'INSERT INTO suppliers(name) VALUES(%s) RETURNING id')

    def test_other_inserts_stay_unchanged(self):
        connection=FakeConnection()
        SQL(connection).execute("INSERT INTO events(kind,payload) VALUES(?,?)",('x','{}'))
        self.assertEqual(connection.seen[0],'INSERT INTO events(kind,payload) VALUES(%s,%s)')


class ParameterBindingTests(unittest.TestCase):
    def test_json_documents_bind_as_jsonb_for_jsonb_tables(self):
        connection=FakeConnection()
        SQL(connection).execute('UPDATE suppliers SET contacts=? WHERE id=?',(encode({'email':'a@b'}),1))
        bound=connection.seen[1]
        self.assertIsInstance(bound[0],Jsonb)
        self.assertEqual(bound[0].obj,{'email':'a@b'})
        self.assertEqual(bound[1],1)

    def test_json_kept_as_text_for_text_columns(self):
        connection=FakeConnection()
        SQL(connection).execute('UPDATE settings SET value=? WHERE key=?',(encode({'a':1}),'rules'))
        bound=connection.seen[1]
        self.assertIsInstance(bound[0],str)
        self.assertNotIsInstance(bound[0],Jsonb)

    def test_iso_timestamps_bind_as_datetime(self):
        connection=FakeConnection()
        stamp=now()
        SQL(connection).execute('UPDATE suppliers SET checked_at=? WHERE id=?',(stamp,1))
        bound=connection.seen[1][0]
        self.assertIsInstance(bound,datetime)
        self.assertEqual(bound,datetime.fromisoformat(stamp))

    def test_postgres_values_read_back_as_text(self):
        self.assertEqual(adapt_value({'a':[1,2]}),'{"a": [1, 2]}')
        self.assertEqual(adapt_value(datetime(2026,1,2,3,4,5,tzinfo=timezone.utc)),'2026-01-02T03:04:05+00:00')
        self.assertEqual(adapt_value(7),7)


if __name__=='__main__':unittest.main()
