import os
import tempfile
import unittest
from server import Store, make_draft

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=os.path.join(self.temp.name,'test.db'); self.store=Store(self.path,seed=False)
    def tearDown(self): self.temp.cleanup()
    def test_import_is_atomic_and_duplicates_are_skipped(self):
        result=self.store.import_rows([{'name':'供应商甲','url':'https://example.com'},{'name':'重复','url':'https://example.com/'}])
        self.assertEqual(result,{'added':1,'duplicates':1})
        with self.assertRaises(ValueError): self.store.import_rows([{'url':'https://valid.com'},{'url':'file:///etc/passwd'}])
        self.assertEqual(len(self.store.state()['suppliers']),1)
    def test_310_domains_keep_numbers_without_supplier_names(self):
        rows=[{'source_row':str(i+2),'domain':str(i)+'.example.com'} for i in range(310)]
        self.assertEqual(self.store.import_rows(rows)['added'],310)
        saved=self.store.state()['suppliers']
        self.assertEqual(saved[0]['name'],'2')
        self.assertEqual(saved[-1]['name'],'311')
        self.assertEqual(saved[0]['url'],'https://0.example.com')

    def test_single_domain_gets_an_automatic_number(self):
        self.store.import_rows([{'url':'example.com'},{'url':'example.org'}])
        self.assertEqual([r['name'] for r in self.store.state()['suppliers']],['1','2'])

    def test_results_and_manual_review_survive_restart(self):
        self.store.import_rows([{'name':'甲','url':'https://example.com'}]); sid=self.store.state()['suppliers'][0]['id']
        self.store.save_result(sid,{'contacts':{'email':['sales@example.com']},'offers':[{'model':'deepseek-v3','ratio':0.35}],'sources':[],'warnings':[]})
        self.assertEqual(self.store.state()['suppliers'][0]['status'],'qualified')
        with self.assertRaises(ValueError): self.store.review(sid,'qualified','')
        self.store.review(sid,'skipped','报价条件不适用')
        state=Store(self.path,seed=False).state()
        self.assertEqual(state['suppliers'][0]['status'],'skipped')
        self.assertEqual(len(state['history']),1)
        self.assertTrue(any('报价条件不适用' in e['message'] for e in state['events']))
    def test_missing_contacts_prevents_automatic_qualification(self):
        self.store.import_rows([{'url':'https://example.com'}]); sid=self.store.state()['suppliers'][0]['id']
        self.store.save_result(sid,{'contacts':{},'offers':[{'model':'deepseek-v3','ratio':0.35}],'sources':[],'warnings':[]})
        self.assertEqual(self.store.state()['suppliers'][0]['status'],'review')
    def test_draft_is_rfc_message_and_never_marks_sent(self):
        draft=make_draft({'id':1,'name':'甲','contacts':{'email':['sales@example.com']},'offers':[{'model':'deepseek-v3'}]})
        self.assertIn(b'To: sales@example.com',draft)
        self.assertIn(b'X-Unsent: 1',draft)
    def test_demo_and_real_data_are_explicit(self):
        store=Store(os.path.join(self.temp.name,'seed.db'))
        self.assertTrue(all(row['mode']=='demo' for row in store.state()['suppliers']))
        store.import_rows([{'url':'https://example.com'}])
        self.assertEqual(sum(row['mode']=='real' for row in store.state()['suppliers']),1)

class ConnectionLifetimeTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows refuses to replace or delete a file with an open handle')
    def test_store_releases_the_database_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path=os.path.join(folder,'scout.db');store=Store(path,seed=False)
            store.import_rows([{'name':'甲','url':'https://example.com'}]);store.state()
            os.replace(path,os.path.join(folder,'moved.db'))

if __name__=='__main__': unittest.main()
