import os
import tempfile
import unittest
from server import Store, make_draft


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'test.db')
        self.store = Store(self.path, seed=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_import_is_atomic_and_duplicates_are_skipped(self):
        result = self.store.import_rows(
            [
                {'name': '供应商甲', 'url': 'https://example.com'},
                {'name': '重复', 'url': 'https://example.com/'},
            ]
        )
        self.assertEqual(result, {'added': 1, 'duplicates': 1, 'archived': 0})
        with self.assertRaises(ValueError):
            self.store.import_rows([{'url': 'https://valid.com'}, {'url': 'file:///etc/passwd'}])
        self.assertEqual(len(self.store.state()['suppliers']), 1)

    def test_contact_merge_ignores_case_keeps_first_spelling_and_order(self):
        from server import merge_contacts

        merged = merge_contacts(
            {'email': ['Sales@Example.com'], 'wechat': ['wx-1']},
            {'email': ['sales@example.com', 'SUPPORT@example.com'], 'phone': ['123']},
        )
        self.assertEqual(merged['email'], ['Sales@Example.com', 'SUPPORT@example.com'])
        self.assertEqual(merged['wechat'], ['wx-1'])
        self.assertEqual(merged['phone'], ['123'])

    def test_310_domains_keep_numbers_without_supplier_names(self):
        rows = [{'source_row': str(i + 2), 'domain': str(i) + '.example.com'} for i in range(310)]
        self.assertEqual(self.store.import_rows(rows)['added'], 310)
        saved = self.store.state()['suppliers']
        self.assertEqual(saved[0]['name'], '2')
        self.assertEqual(saved[-1]['name'], '311')
        self.assertEqual(saved[0]['url'], 'https://0.example.com')

    def test_single_domain_gets_an_automatic_number(self):
        self.store.import_rows([{'url': 'example.com'}, {'url': 'example.org'}])
        self.assertEqual([r['name'] for r in self.store.state()['suppliers']], ['1', '2'])

    def test_results_and_manual_review_survive_restart(self):
        self.store.import_rows([{'name': '甲', 'url': 'https://example.com'}])
        sid = self.store.state()['suppliers'][0]['id']
        self.store.save_result(
            sid,
            {
                'contacts': {'email': ['sales@example.com']},
                'offers': [{'model': 'deepseek-v3', 'ratio': 0.35}],
                'sources': [],
                'warnings': [],
            },
        )
        self.assertEqual(self.store.state()['suppliers'][0]['status'], 'qualified')
        with self.assertRaises(ValueError):
            self.store.review(sid, 'qualified', '')
        self.store.review(sid, 'skipped', '报价条件不适用')
        state = Store(self.path, seed=False).state()
        self.assertEqual(state['suppliers'][0]['status'], 'skipped')
        self.assertEqual(len(state['history']), 1)
        self.assertTrue(any('报价条件不适用' in e['message'] for e in state['events']))

    def test_missing_contacts_prevents_automatic_qualification(self):
        self.store.import_rows([{'url': 'https://example.com'}])
        sid = self.store.state()['suppliers'][0]['id']
        self.store.save_result(
            sid,
            {
                'contacts': {},
                'offers': [{'model': 'deepseek-v3', 'ratio': 0.35}],
                'sources': [],
                'warnings': [],
            },
        )
        self.assertEqual(self.store.state()['suppliers'][0]['status'], 'review')

    def test_draft_is_rfc_message_and_never_marks_sent(self):
        draft = make_draft(
            {
                'id': 1,
                'name': '甲',
                'contacts': {'email': ['sales@example.com']},
                'offers': [{'model': 'deepseek-v3'}],
            }
        )
        self.assertIn(b'To: sales@example.com', draft)
        self.assertIn(b'X-Unsent: 1', draft)

    def test_demo_and_real_data_are_explicit(self):
        store = Store(os.path.join(self.temp.name, 'seed.db'))
        self.assertTrue(all(row['mode'] == 'demo' for row in store.state()['suppliers']))
        store.import_rows([{'url': 'https://example.com'}])
        self.assertEqual(sum(row['mode'] == 'real' for row in store.state()['suppliers']), 1)

    def test_real_run_refuses_an_unreadable_saved_key(self):
        import json
        from pathlib import Path

        Path(self.temp.name, 'model-service.json').write_text(
            json.dumps(
                {
                    'base_url': 'https://example.com/v1',
                    'model': 'm',
                    'api_key': 'dpapi:broken',
                    'tested': True,
                }
            ),
            encoding='utf-8',
        )
        with self.assertRaises(ValueError) as caught:
            self.store.start_job('real')
        self.assertIn('无法解密', str(caught.exception))


class ConnectionLifetimeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows refuses to replace or delete a file with an open handle')
    def test_store_releases_the_database_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'scout.db')
            store = Store(path, seed=False)
            store.import_rows([{'name': '甲', 'url': 'https://example.com'}])
            store.state()
            os.replace(path, os.path.join(folder, 'moved.db'))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.temp.name, 'test.db'), seed=False)
        self.store.import_rows([{'name': '待归档', 'url': 'https://archive.example.com'}])
        self.sid = self.store.state()['suppliers'][0]['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_keeps_every_record_and_restore_brings_the_supplier_back(self):
        from server import now

        self.store.save_followup(self.sid, 'waiting', '等回复')
        with self.store.connect() as db:
            db.execute(
                'INSERT INTO drafts(supplier_id,subject,body,created_at) VALUES(?,?,?,?)',
                (self.sid, '主题', '正文', now()),
            )
            db.execute(
                'INSERT INTO history(supplier_id,mode,offers,created_at) VALUES(?,?,?,?)',
                (self.sid, 'real', '[]', now()),
            )
        self.assertEqual(self.store.archive(self.sid), {'ok': True, 'id': self.sid, 'archived': True})
        self.assertTrue(self.store.get(self.sid)['archived_at'])
        with self.store.connect() as db:
            for table in ['drafts', 'history']:
                self.assertEqual(
                    db.execute(
                        'SELECT COUNT(*) FROM ' + table + ' WHERE supplier_id=?', (self.sid,)
                    ).fetchone()[0],
                    1,
                )
            self.assertEqual(
                db.execute(
                    'SELECT COUNT(*) FROM settings WHERE key=?',
                    (self.store.followup_prefix() + str(self.sid),),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute('SELECT COUNT(*) FROM events WHERE supplier_id=?', (self.sid,)).fetchone()[0],
                1,
            )
        self.assertEqual(self.store.restore(self.sid), {'ok': True, 'id': self.sid, 'archived': False})
        self.assertFalse(self.store.get(self.sid)['archived_at'])
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM events WHERE message LIKE '已恢复记录%'").fetchone()[0],
                1,
            )

    def test_archiving_is_refused_while_a_batch_is_running(self):
        self.store.job.update(running=True, status='running')
        try:
            with self.assertRaises(ValueError) as caught:
                self.store.archive(self.sid)
        finally:
            self.store.job.update(running=False, status='idle')
        self.assertIn('采集进行中', str(caught.exception))
        self.assertFalse(self.store.get(self.sid)['archived_at'])

    def test_archived_rows_are_left_out_of_the_next_batch(self):
        self.store.seed()
        demo = self.store.state()['suppliers']
        target = demo[0]
        self.store.archive(target['id'])
        picked = []
        self.store.run_job = lambda rows, mode, scope: picked.extend(rows)
        self.store.start_job('demo')
        self.store.job_thread.join(10)
        self.assertNotIn(target['id'], [row['id'] for row in picked])
        self.assertEqual(len(picked), len(demo) - 1)

    def test_archive_of_a_missing_record_reports_the_usual_message(self):
        with self.assertRaises(ValueError) as caught:
            self.store.archive(999)
        self.assertIn('供应商不存在', str(caught.exception))

    def test_reimporting_an_archived_url_is_reported_separately(self):
        self.store.archive(self.sid)
        result = self.store.import_rows([{'name': '再导入', 'url': 'https://archive.example.com'}])
        self.assertEqual(result, {'added': 0, 'duplicates': 1, 'archived': 1})
        self.assertIn('1 条网址已归档', self.store.state()['events'][0]['message'])

    def test_reimporting_an_active_url_stays_a_plain_duplicate(self):
        result = self.store.import_rows([{'name': '再导入', 'url': 'https://archive.example.com'}])
        self.assertEqual(result, {'added': 0, 'duplicates': 1, 'archived': 0})


if __name__ == '__main__':
    unittest.main()
