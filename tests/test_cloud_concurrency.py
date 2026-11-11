import contextlib
import importlib.util
import threading
import unittest
from unittest.mock import patch

@unittest.skipUnless(importlib.util.find_spec('psycopg') and importlib.util.find_spec('cryptography'),'cloud adapter dependencies are optional locally')
class CloudConcurrencyTests(unittest.TestCase):
    def test_fenced_worker_and_http_mutation_use_one_lock_order(self):
        from cloud_store import PostgresStore
        pg=threading.RLock();worker_ready=threading.Event();http_waiting=threading.Event();errors=[]
        class Cursor:
            def fetchone(self):return {'id':'fixture'}
        class DB:
            @contextlib.contextmanager
            def transaction(self,write=False):
                if threading.current_thread().name=='http-writer':http_waiting.set()
                if not pg.acquire(timeout=1):raise RuntimeError('reversed lock order')
                try:yield self
                finally:pg.release()
            def execute(self,*args):return Cursor()
        store=PostgresStore.__new__(PostgresStore);store.database=DB();store.lock=threading.RLock()
        def worker():
            try:
                with store.fenced('job','owner'):
                    worker_ready.set();self.assertTrue(http_waiting.wait(2));store.event('worker result')
            except Exception as error:errors.append(str(error))
        def http():
            try:store.event('HTTP mutation')
            except Exception as error:errors.append(str(error))
        first=threading.Thread(target=worker,name='worker');first.start();self.assertTrue(worker_ready.wait(2));second=threading.Thread(target=http,name='http-writer');second.start();first.join(4);second.join(4)
        self.assertFalse(first.is_alive() or second.is_alive());self.assertEqual(errors,[])
    def test_provider_test_final_revision_check_is_in_database_transaction(self):
        from cloud_store import CloudModelService
        held=[];reads=[];written=[]
        class DB:
            @contextlib.contextmanager
            def transaction(self,write=False):
                held.append(write)
                try:yield None
                finally:held.pop()
        service=CloudModelService.__new__(CloudModelService);service.database=DB();service.lock=threading.RLock()
        def read():
            reads.append(True)
            if len(reads)>1:self.assertEqual(held,[True])
            return {'base_url':'https://example.com/v1','model':'fixture','revision':'one'}
        service.read=read;service.models=lambda config:['fixture'];service.complete=lambda *args,**kwargs:'OK'
        def write(config):self.assertEqual(held,[True]);written.append(config)
        service._write=write
        self.assertEqual(service.test()['reply'],'OK');self.assertTrue(written[0]['tested'])
    def test_each_claim_has_distinct_fencing_owner(self):
        import cloud_worker
        owners=[]
        class FakeStore:
            def claim_job(self,owner):owners.append(owner);return {'id':'job'}
        def execute(store,job,owner,shutdown):
            if len(owners)==2:shutdown.set()
        with patch.object(cloud_worker,'PostgresStore',return_value=FakeStore()),patch.object(cloud_worker,'execute_job',side_effect=execute),patch.object(cloud_worker.signal,'signal'):
            cloud_worker.main()
        self.assertEqual(len(set(owners)),2)
    def test_stale_quote_update_keeps_fresh_contact_facts(self):
        from cloud_store import PostgresStore
        from server import Store
        class DB:
            @contextlib.contextmanager
            def transaction(self,write=False):yield None
        store=PostgresStore.__new__(PostgresStore);store.database=DB();store.lock=threading.RLock()
        store.get=lambda sid:{'contacts':{'email':['fresh@example.com']},'sources':[{'url':'https://example.com/new','excerpt':'fresh contact'}]}
        with patch.object(Store,'save_result',return_value=None) as save:
            store.save_result(1,{'contacts':{'email':['old@example.com']},'sources':[{'url':'https://example.com/old','excerpt':'old quote'}],'offers':[]})
        result=save.call_args.args[-1]
        self.assertEqual(set(result['contacts']['email']),{'fresh@example.com','old@example.com'});self.assertEqual(len(result['sources']),2)

if __name__=='__main__':unittest.main()
