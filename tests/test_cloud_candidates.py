import importlib.util
import unittest

HAS_CLOUD_DEPS = bool(importlib.util.find_spec('psycopg') and importlib.util.find_spec('cryptography'))


def rows():
    return [
        {'id': 1, 'mode': 'real', 'status': 'pending', 'contact_status': '', 'archived_at': None},
        {
            'id': 2,
            'mode': 'real',
            'status': 'pending',
            'contact_status': '',
            'archived_at': '2026-11-16T00:00:00+00:00',
        },
        {'id': 3, 'mode': 'demo', 'status': 'done', 'contact_status': 'found', 'archived_at': None},
    ]


@unittest.skipUnless(HAS_CLOUD_DEPS, 'cloud adapter dependencies are optional locally')
class CloudCandidateTests(unittest.TestCase):
    def pick(self, **kwargs):
        from cloud_store import pick_candidates

        return [row['id'] for row in pick_candidates(rows(), **kwargs)]

    def test_archived_sites_are_skipped_for_a_full_run(self):
        self.assertEqual(self.pick(mode='real', scope='all', selection='all'), [1])

    def test_archived_sites_are_skipped_even_when_selected_by_id(self):
        self.assertEqual(self.pick(mode='real', ids=[1, 2], scope='all'), [1])

    def test_demo_mode_still_collects_its_active_rows(self):
        self.assertEqual(self.pick(mode='demo', scope='all'), [3])
