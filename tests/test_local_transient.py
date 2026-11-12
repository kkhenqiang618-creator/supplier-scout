import os
import tempfile
import unittest
from unittest.mock import patch
import server
from server import Store, ModelTransientSiteError

class LocalTransientTests(unittest.TestCase):
    def make(self,folder,count):
        store=Store(os.path.join(folder,'test.db'),seed=False)
        store.import_rows([{'url':'https://site%d.example.com'%i} for i in range(count)])
        store.model_service.save({'base_url':'https://example.com/v1','model':'fixture','api_key':'fixture-key'})
        return store
    def run_with(self,store,side_effect):
        with patch.object(server,'collect_site',side_effect=side_effect),patch('browser_agent.runtime_paths',return_value=('moli','playwright')),patch.object(type(store.model_service),'public',return_value={'configured':True,'tested':True,'base_url':'x','model':'fixture'}):
            store.start_job('real',scope='contacts');store.job_thread.join(timeout=5)
    def test_transient_site_keeps_partial_and_batch_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            store=self.make(folder,2);calls=[]
            def fake(url,*args,**kwargs):
                calls.append(url)
                if len(calls)==1:raise ModelTransientSiteError('模型服务暂时不可用（已重试）',{'contacts':{'email':['a@site0.example.com']},'sources':[],'warnings':[]})
                return {'contacts':{},'contact_records':[],'contact_status':'incomplete','contact_summary':'ok','sources':[],'warnings':[],'scope':'contacts'}
            self.run_with(store,fake)
            self.assertEqual(len(calls),2);self.assertEqual(store.job['status'],'completed')
            first=[s for s in store.state()['suppliers'] if s['url'].startswith('https://site0')][0]
            self.assertEqual(first['contact_status'],'incomplete');self.assertIn('a@site0.example.com',first['contacts'].get('email',[]))
            self.assertEqual(first['status'],'pending')
    def test_three_consecutive_transient_sites_pause_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            store=self.make(folder,5);calls=[]
            def fake(url,*args,**kwargs):
                calls.append(url);raise ModelTransientSiteError('模型服务暂时不可用（已重试）',None)
            self.run_with(store,fake)
            self.assertEqual(len(calls),3);self.assertEqual(store.job['status'],'failed')
            self.assertTrue(any('连续 3 家不可用' in e['message'] for e in store.state()['events']))

if __name__=='__main__':unittest.main()
