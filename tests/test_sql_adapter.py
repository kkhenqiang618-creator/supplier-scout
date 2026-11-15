import unittest
from cloud_store import SQL, adapt_placeholders


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


if __name__=='__main__':unittest.main()
