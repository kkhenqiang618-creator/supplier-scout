import unittest
from datetime import datetime, timezone
from collection import encode, now
from cloud_store import Cursor, SQL, adapt_placeholders


class FakeCursor:
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self):
        self.seen = None

    def execute(self, statement, parameters=()):
        self.seen = (statement, parameters)
        return FakeCursor()


class PlaceholderTests(unittest.TestCase):
    def test_rewrites_placeholders_outside_literals(self):
        self.assertEqual(
            adapt_placeholders('SELECT 1 FROM t WHERE id=? AND status=?'),
            'SELECT 1 FROM t WHERE id=%s AND status=%s',
        )

    def test_leaves_question_marks_and_percent_inside_literals(self):
        self.assertEqual(
            adapt_placeholders("SELECT 1 FROM t WHERE note='100%? done' AND id=?"),
            "SELECT 1 FROM t WHERE note='100%%? done' AND id=%s",
        )

    def test_keeps_doubled_quotes_inside_literals(self):
        self.assertEqual(
            adapt_placeholders("SELECT 1 FROM t WHERE note='it''s 50%?' AND id=?"),
            "SELECT 1 FROM t WHERE note='it''s 50%%?' AND id=%s",
        )

    def test_does_not_reinterpret_postgres_identifiers(self):
        self.assertEqual(
            adapt_placeholders('SELECT "owner" FROM t WHERE id=?'), 'SELECT "owner" FROM t WHERE id=%s'
        )


class StatementShapeTests(unittest.TestCase):
    def test_insert_or_ignore_gets_conflict_clause(self):
        connection = FakeConnection()
        SQL(connection).execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', ('k', 'v'))
        self.assertEqual(
            connection.seen[0], 'INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT DO NOTHING'
        )

    def test_supplier_insert_gets_returning_id(self):
        connection = FakeConnection()
        SQL(connection).execute('INSERT INTO suppliers(name) VALUES(?)', ('n',))
        self.assertEqual(connection.seen[0], 'INSERT INTO suppliers(name) VALUES(%s) RETURNING id')

    def test_other_inserts_stay_unchanged(self):
        connection = FakeConnection()
        SQL(connection).execute("INSERT INTO events(kind,payload) VALUES(?,?)", ('x', '{}'))
        self.assertEqual(connection.seen[0], 'INSERT INTO events(kind,payload) VALUES(%s,%s)')


class ParameterBindingTests(unittest.TestCase):
    def test_json_documents_are_sent_as_plain_text(self):
        # psycopg sends str untyped, so TEXT and jsonb columns both accept it.
        connection = FakeConnection()
        document = encode({'email': 'a@b'})
        SQL(connection).execute('UPDATE suppliers SET contacts=? WHERE id=?', (document, 1))
        self.assertEqual(connection.seen[1], (document, 1))
        self.assertIs(type(connection.seen[1][0]), str)

    def test_timestamps_keep_the_iso_text_shape(self):
        connection = FakeConnection()
        stamp = now()
        SQL(connection).execute('UPDATE suppliers SET checked_at=? WHERE id=?', (stamp, 1))
        bound = connection.seen[1][0]
        self.assertIs(type(bound), str)
        self.assertRegex(bound, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$')

    def test_now_does_not_change_the_stored_text_format(self):
        self.assertRegex(now(), r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$')


class RowPassThroughTests(unittest.TestCase):
    def test_jsonb_and_timestamp_values_are_not_rewritten(self):
        # cloud_jobs.state and supplier_ids are already jsonb in production.
        stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        class Stub:
            rowcount = 0

            def fetchone(self):
                return {'state': {'stage': '云端采集'}, 'supplier_ids': [1, 2], 'updated_at': stamp, 'id': 7}

            def fetchall(self):
                return []

        row = Cursor(Stub()).fetchone()
        self.assertIsInstance(row['state'], dict)
        self.assertEqual(row['state'].get('stage'), '云端采集')
        self.assertEqual(row['supplier_ids'], [1, 2])
        self.assertEqual(row['updated_at'], stamp)
        self.assertEqual(row['id'], 7)


class JobRowTests(unittest.TestCase):
    def test_jsonb_job_rows_support_state_get_and_id_slicing(self):
        import time

        from cloud_store import Cursor, PostgresStore

        class Stub:
            rowcount = 0

            def fetchone(self):
                return {
                    'id': 7,
                    'status': 'running',
                    'cancel_requested': False,
                    'position': 2,
                    'scope': 'all',
                    'mode': 'real',
                    'state': {'queued_at': time.time(), '_hidden': 1},
                    'supplier_ids': [11, 12, 13, 14],
                }

            def fetchall(self):
                return []

        row = Cursor(Stub()).fetchone()
        job = PostgresStore.job_state(row)
        self.assertEqual(job['done'], 2)
        self.assertNotIn('_hidden', job)
        self.assertEqual(row['supplier_ids'][row['position'] :], [13, 14])


class DecodeRowTests(unittest.TestCase):
    def test_decode_row_reads_jsonb_values_and_timestamptz(self):
        from local_store import Store

        row = {
            'id': 1,
            'url': 'https://s.example.com',
            'contacts': {'email': ['a@example.com']},
            'offers': [{'model': 'M', 'price': '1'}],
            'sources': [],
            'warnings': [],
            'contact_result': {'contact_records': [], 'contact_summary': '', 'scope': 'contacts'},
            'checked_at': datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            'created_at': '2026-01-01T00:00:00+00:00',
            'contact_checked_at': None,
        }
        item = Store.decode_row(row)
        self.assertEqual(item['contacts'], {'email': ['a@example.com']})
        self.assertEqual(item['offers'][0]['model'], 'M')
        self.assertEqual(item['checked_at'], '2026-01-02T03:04:05+00:00')
        self.assertEqual(item['created_at'], '2026-01-01T00:00:00+00:00')
        self.assertIsNone(item['contact_checked_at'])


if __name__ == '__main__':
    unittest.main()
