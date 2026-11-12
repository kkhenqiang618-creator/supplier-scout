import os
import tempfile
import unittest
from server import Store, select_rows


def row(id_, mode='real', status='pending', contact_status='not_started'):
    return {'id': id_, 'mode': mode, 'status': status, 'contact_status': contact_status}


class SelectionTests(unittest.TestCase):
    def test_blocked_retries_only_unfinished_contacts_and_errors(self):
        rows = [row(1, status='review', contact_status='blocked'),
                row(2, status='qualified', contact_status='found'),
                row(3, status='review', contact_status='incomplete'),
                row(4, status='error', contact_status='not_started'),
                row(5, status='pending', contact_status='not_started'),
                row(6, mode='demo', contact_status='blocked')]
        self.assertEqual([r['id'] for r in select_rows(rows, 'real', selection='blocked')], [1, 3, 4])

    def test_remaining_all_only_takes_never_checked_rows(self):
        rows = [row(1), row(2, status='review', contact_status='found'), row(3, status='skipped')]
        self.assertEqual([r['id'] for r in select_rows(rows, 'real', scope='all', selection='remaining')], [1])

    def test_remaining_contacts_skips_rows_with_found_contacts(self):
        rows = [row(1, contact_status='not_started'), row(2, contact_status='found'), row(3, contact_status='legacy')]
        self.assertEqual([r['id'] for r in select_rows(rows, 'real', scope='contacts')], [1, 3])

    def test_explicit_ids_override_the_selection(self):
        rows = [row(1), row(2, status='qualified', contact_status='found')]
        self.assertEqual([r['id'] for r in select_rows(rows, 'real', ids=[2], selection='remaining')], [2])

    def test_mode_is_always_respected(self):
        rows = [row(1, mode='demo'), row(2, mode='real')]
        self.assertEqual(select_rows(rows, 'demo', selection='blocked'), [])
        self.assertEqual([r['id'] for r in select_rows(rows, 'demo', selection='all')], [1])


class BlockedBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.temp.name, 'test.db'), seed=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_demo_blocked_batch_runs_only_the_blocked_row(self):
        target = next(r for r in self.store.state()['suppliers'] if not r['contacts'])
        self.store.save_contacts(target['id'], {'contacts': {}, 'contact_records': [], 'contact_status': 'blocked',
                                                'contact_summary': '访问受阻'})
        job = self.store.start_job('demo', selection='blocked')
        self.assertEqual(job['total'], 1)
        self.store.job_thread.join(timeout=10)
        self.assertFalse(self.store.job['running'])

    def test_blocked_selection_with_no_candidates_says_so(self):
        with self.assertRaisesRegex(ValueError, '没有采集受阻的记录'):
            self.store.start_job('demo', selection='blocked')
