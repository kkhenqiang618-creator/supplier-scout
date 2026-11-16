"""P26: the browser worker reports itself, so a stuck queue can be told apart from a dead daemon."""

import contextlib
import importlib.util
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    importlib.util.find_spec('psycopg') and importlib.util.find_spec('cryptography'),
    'cloud adapter dependencies are optional locally',
)
class WorkerStatusStorageTests(unittest.TestCase):
    def fake_store(self, rows=None):
        from cloud_store import PostgresStore

        statements = []
        answers = list(rows or [])

        class DB:
            def execute(self, statement, parameters=()):
                statements.append((' '.join(statement.split()), parameters))
                if not answers:
                    raise AssertionError('unexpected query: ' + statement)
                answer = answers.pop(0)
                return MagicMock(fetchone=lambda: answer)

        @contextlib.contextmanager
        def transaction(*args, **kwargs):
            yield DB()

        database = MagicMock()
        database.transaction = transaction
        store = PostgresStore.__new__(PostgresStore)
        store.database = database
        return store, statements

    def test_record_writes_one_row_that_the_daemon_overwrites(self):
        store, statements = self.fake_store([None])
        with patch.object(store.__class__, 'connect', lambda self: self.database.transaction()):
            store.record_worker_status(
                {
                    'started_at': 1700000000.0,
                    'state': 'running',
                    'job_id': 'job-1',
                    'job_owner': '小吴',
                    'site': '正在采集 3/40',
                    'error': '',
                    'version': 'dpl_abc',
                }
            )
        statement, parameters = statements[0]
        self.assertIn('INSERT INTO cloud_worker_status', statement)
        self.assertIn('ON CONFLICT(id) DO UPDATE SET', statement)
        self.assertIn('heartbeat_at=now()', statement)
        self.assertEqual(
            parameters,
            ('browser-worker', 1700000000.0, 'running', 'job-1', '小吴', '正在采集 3/40', '', 'dpl_abc'),
        )

    def test_status_reads_ages_from_the_server_clock(self):
        store, _ = self.fake_store(
            [
                {'present': True},
                {
                    'state': 'idle',
                    'job_id': '',
                    'job_owner': '',
                    'site': '',
                    'error_text': '',
                    'version': 'local',
                    'uptime': 30,
                    'age': 30,
                },
            ]
        )
        with patch.object(store.__class__, 'connect', lambda self: self.database.transaction()):
            status = store.worker_status()
        self.assertTrue(status['available'])
        self.assertTrue(status['seen'])
        self.assertTrue(status['online'])
        self.assertEqual(status['last_seen_seconds'], 30)
        self.assertEqual(status['uptime_seconds'], 30)
        self.assertEqual(status['state'], 'idle')

    def test_a_stale_heartbeat_is_reported_offline(self):
        store, _ = self.fake_store(
            [
                {'present': True},
                {
                    'state': 'running',
                    'job_id': 'job',
                    'job_owner': '小吴',
                    'site': 'https://x.example.com',
                    'error_text': '',
                    'version': 'local',
                    'uptime': 500,
                    'age': 400,
                },
            ]
        )
        with patch.object(store.__class__, 'connect', lambda self: self.database.transaction()):
            status = store.worker_status()
        self.assertFalse(status['online'])
        self.assertEqual(status['last_seen_seconds'], 400)

    def test_without_the_migration_the_page_says_unknown_instead_of_failing(self):
        store, _ = self.fake_store([{'present': False}])
        with patch.object(store.__class__, 'connect', lambda self: self.database.transaction()):
            self.assertEqual(store.worker_status(), {'available': False})

    def test_a_migrated_database_without_a_worker_row_reports_never_seen(self):
        store, _ = self.fake_store([{'present': True}, None])
        with patch.object(store.__class__, 'connect', lambda self: self.database.transaction()):
            self.assertEqual(store.worker_status(), {'available': True, 'seen': False})

    def test_members_do_not_see_whose_site_the_shared_browser_is_on(self):
        from cloud_store import worker_view

        status = {
            'available': True,
            'seen': True,
            'online': True,
            'state': 'running',
            'job_id': 'job',
            'job_owner': '小吴',
            'site': 'https://secret.example.com',
        }
        member = worker_view(status, False)
        for key in ('job_id', 'job_owner', 'site'):
            self.assertNotIn(key, member)
        self.assertTrue(member['online'])
        self.assertEqual(worker_view(status, True), status)
        self.assertIsNone(worker_view(None, True))


@unittest.skipUnless(
    importlib.util.find_spec('psycopg') and importlib.util.find_spec('cryptography'),
    'cloud adapter dependencies are optional locally',
)
class WorkerStatusLoopTests(unittest.TestCase):
    def test_the_loop_publishes_until_shutdown(self):
        import cloud_worker

        store = MagicMock()
        shutdown = threading.Event()
        with patch.object(cloud_worker, 'STATUS_INTERVAL', 0.01):
            thread = threading.Thread(target=cloud_worker.status_loop, args=(store, shutdown), daemon=True)
            thread.start()
            deadline = time.monotonic() + 2
            while store.record_worker_status.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            shutdown.set()
            thread.join(timeout=2)
        self.assertGreaterEqual(store.record_worker_status.call_count, 2)
        written = store.record_worker_status.call_args.args[0]
        self.assertIn('state', written)

    def test_a_missing_table_warns_once_and_keeps_the_worker_alive(self):
        import cloud_worker

        store = MagicMock()
        store.record_worker_status.side_effect = RuntimeError('relation does not exist')
        shutdown = threading.Event()
        with (
            patch.object(cloud_worker, 'STATUS_INTERVAL', 0.01),
            patch('builtins.print') as printer,
        ):
            thread = threading.Thread(target=cloud_worker.status_loop, args=(store, shutdown), daemon=True)
            thread.start()
            deadline = time.monotonic() + 2
            while store.record_worker_status.call_count < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            shutdown.set()
            thread.join(timeout=2)
        self.assertEqual(printer.call_count, 1)
        self.assertIn('0003_worker_status.sql', printer.call_args.args[0])

    def test_the_worker_stores_only_the_error_class_because_messages_can_carry_secrets(self):
        import cloud_worker

        label = cloud_worker.failure_label(RuntimeError('DATABASE_URL=postgres://user:secret@host/db'))
        self.assertEqual(label, 'RuntimeError')
        self.assertNotIn('secret', label)

    def test_activity_notes_are_trimmed(self):
        import cloud_worker

        cloud_worker.note_activity('x' * 500)
        self.assertEqual(len(cloud_worker.STATUS['site']), 200)
        cloud_worker.STATUS['site'] = ''


@unittest.skipUnless(
    importlib.util.find_spec('psycopg') and importlib.util.find_spec('cryptography'),
    'cloud adapter dependencies are optional locally',
)
class RunOnceStatusTests(unittest.TestCase):
    def test_a_claimed_job_is_visible_as_running_while_it_runs(self):
        import cloud_worker

        store = MagicMock()
        store.claim_job.return_value = {'id': 'job', 'owner': '小吴'}
        seen = []
        status = {'site': '', 'error': ''}

        def execute(*args):
            seen.append(dict(status))

        with patch.object(cloud_worker, 'execute_job', side_effect=execute):
            self.assertTrue(cloud_worker.run_once(store, threading.Event(), status))
        self.assertEqual(seen[0]['state'], 'running')
        self.assertEqual(seen[0]['job_id'], 'job')
        self.assertEqual(seen[0]['job_owner'], '小吴')
        self.assertEqual(status['state'], 'idle')
        self.assertEqual(status['job_id'], '')

    def test_an_empty_queue_reports_idle(self):
        import cloud_worker

        store = MagicMock()
        store.claim_job.return_value = None
        status = {'state': 'running', 'job_id': 'old', 'job_owner': '小吴', 'site': 'x'}
        self.assertFalse(cloud_worker.run_once(store, threading.Event(), status))
        self.assertEqual(status, {'state': 'idle', 'job_id': '', 'job_owner': '', 'site': ''})

    def test_an_orphan_job_is_still_closed_and_the_worker_returns_to_idle(self):
        import cloud_worker

        store = MagicMock()
        store.claim_job.return_value = {'id': 'orphan', 'owner': None}
        status = {'state': 'running', 'job_id': 'orphan', 'job_owner': 'x', 'site': ''}
        with patch.object(cloud_worker, 'execute_job') as execute:
            self.assertTrue(cloud_worker.run_once(store, threading.Event(), status))
        execute.assert_not_called()
        store.fail_orphan_job.assert_called_once()


class WorkerStatusFrontendTests(unittest.TestCase):
    def test_the_page_shows_the_worker_state_and_explains_a_stuck_queue(self):
        script = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('state.worker', script)
        self.assertIn('后台进程从未上报状态', script)
        self.assertIn('后台进程最后一次心跳', script)
        self.assertIn('最近一次失败', script)
        self.assertIn('0003 迁移', script)
        self.assertIn('${workerChip()}', script)


if __name__ == '__main__':
    unittest.main()
