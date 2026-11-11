import os
import tempfile
import time
import unittest
from server import Store

class StopTests(unittest.TestCase):
    def test_stop_at_batch_completion_still_reaches_terminal_state(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            store=Store(os.path.join(folder,'test.db'));event=store.event
            def stop_during_final_log(message,*args,**kwargs):
                if message.startswith('批次完成'):store.stop_job()
                return event(message,*args,**kwargs)
            with patch.object(store,'event',side_effect=stop_during_final_log):
                store.start_job('demo',ids=[store.state()['suppliers'][0]['id']]);store.job_thread.join(timeout=3)
            self.assertFalse(store.job['running']);self.assertFalse(store.job['stopping']);self.assertEqual(store.job['status'],'stopped')
    def test_stop_preserves_data_and_prevents_next_supplier(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(os.path.join(folder,'test.db'))
            before=store.state()['suppliers']
            store.start_job('demo')
            store.stop_job()
            deadline=time.monotonic()+2
            while store.job['running'] and time.monotonic()<deadline: time.sleep(.02)
            self.assertFalse(store.job['running'])
            self.assertEqual(store.job['status'],'stopped')
            self.assertEqual(store.job['done'],0)
            self.assertEqual(store.state()['suppliers'],before)
            self.assertTrue(any('已停止' in e['message'] for e in store.state()['events']))
            store.stop_job()  # repeated stop is harmless
    def test_default_run_only_collects_remaining_unchecked_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(os.path.join(folder,'test.db'),seed=False)
            store.import_rows([{'url':'https://example.com'},{'url':'https://example.org'}])
            sid=store.state()['suppliers'][0]['id']
            store.save_result(sid,{'contacts':{},'offers':[],'sources':[]})
            store.model_service.save({'base_url':'https://example.com/v1','model':'fixture','api_key':'fixture-key'})
            store.start_job('real')
            self.assertEqual(store.job['total'],1)
            store.stop_job()
            deadline=time.monotonic()+3
            while store.job['running'] and time.monotonic()<deadline: time.sleep(.02)
            self.assertFalse(store.job['running'])

if __name__=='__main__': unittest.main()
