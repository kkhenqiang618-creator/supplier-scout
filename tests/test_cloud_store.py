"""Opt-in integration tests. Run against a fresh cloud test project before import."""
import json
import os
import threading
import time
import unittest
import uuid

@unittest.skipUnless(os.environ.get('SCOUT_TEST_DATABASE_URL'),'cloud DB integration is opt-in')
class CloudStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cloud_store import Database,PostgresStore
        cls.database=Database(os.environ['SCOUT_TEST_DATABASE_URL']);cls.store=PostgresStore(cls.database);cls.tag=uuid.uuid4().hex
        if any(not row['url'].startswith('https://cloud-test-') for row in cls.store.state()['suppliers']):raise unittest.SkipTest('Requires an empty temporary cloud project before data migration')
        cls.store.import_rows([{'url':'https://cloud-test-'+cls.tag+'.example.com'}]);cls.row=next(row for row in cls.store.state()['suppliers'] if cls.tag in row['url'])
        cls.jobs=[]
    @classmethod
    def tearDownClass(cls):
        with cls.database.transaction(write=True) as db:
            for job in cls.jobs:db.execute('DELETE FROM cloud_jobs WHERE id=?',(job,))
            for table in ['drafts','history','events']:db.execute('DELETE FROM '+table+' WHERE supplier_id=?',(cls.row['id'],))
            db.execute('DELETE FROM suppliers WHERE id=?',(cls.row['id'],))
    def test_01_cross_instance_contacts_preserve_quotes_and_manual_review(self):
        from cloud_store import PostgresStore
        store=self.store;sid=self.row['id'];store.save_result(sid,{'offers':[{'model':'deepseek-v4.1','ratio':None,'evidence':'source quote'}],'contacts':{},'sources':[]});store.review(sid,'qualified','人工确认后继续')
        before=store.get(sid);errors=[]
        def write(value):
            try:PostgresStore(self.database).save_contacts(sid,{'contacts':{'email':[value]},'contact_status':'found','contact_records':[{'type':'email','value':value,'source_url':self.row['url'],'evidence':value}]})
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=write,args=(value,)) for value in ['a@example.com','b@example.com']]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[]);after=store.get(sid)
        self.assertEqual(set(after['contacts']['email']),{'a@example.com','b@example.com'})
        for field in ['offers','status','reason','notes','checked_at']:self.assertEqual(before[field],after[field])
    def test_02_encrypted_provider_config_and_shared_sessions(self):
        from cloud_store import PostgresSessionStore
        key='cloud-fixture-secret-'+self.tag;self.store.model_service.save({'base_url':'https://example.com/v1','model':'fixture','api_key':key})
        with self.database.transaction() as db:value=db.execute("SELECT value FROM settings WHERE key='model-service'").fetchone()['value']
        self.assertNotIn(key,value);self.assertEqual(self.store.model_service.read()['api_key'],key)
        store=PostgresSessionStore(self.database);token='test-'+self.tag;store.create('fixture',token,time.time()+60)
        self.assertEqual(PostgresSessionStore(self.database).get(token)['username'],'fixture');store.delete(token);self.assertIsNone(store.get(token))
    def test_03_single_batch_lease_fencing_and_atomic_checkpoint(self):
        from cloud_store import PostgresStore,LostLease
        outputs=[]
        def start():
            try:outputs.append(PostgresStore(self.database).start_job('real',[self.row['id']],'contacts'))
            except ValueError:outputs.append(None)
        threads=[threading.Thread(target=start) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        jobs=[result for result in outputs if result];self.assertEqual(len(jobs),1);job_id=jobs[0]['id'];self.jobs.append(job_id)
        first=self.store.claim_job('first');self.assertEqual(first['id'],job_id);self.assertIsNone(self.store.claim_job('second'))
        with self.database.transaction(write=True) as db:db.execute("UPDATE cloud_jobs SET lease_until=now()-interval '1 second' WHERE id=?",(job_id,))
        second=self.store.claim_job('second');self.assertEqual(second['id'],job_id)
        with self.assertRaises(LostLease):
            with self.store.fenced(job_id,'first'):pass
        prior=self.store.get(self.row['id'])['contacts']
        with self.assertRaisesRegex(RuntimeError,'rollback'):
            with self.store.fenced(job_id,'second') as (db,current):
                self.store.save_contacts(self.row['id'],{'contacts':{'phone':['123456789']},'contact_status':'found'})
                db.execute('UPDATE cloud_jobs SET position=1 WHERE id=?',(job_id,));raise RuntimeError('rollback')
        self.assertEqual(self.store.get(self.row['id'])['contacts'],prior);self.assertEqual(self.store.job['done'],0)
        self.store.stop_job();self.assertTrue(self.store.heartbeat(job_id,'second'));self.store.finish_job(job_id,'second','stopped');self.assertEqual(self.store.job['status'],'stopped')
    def test_04_slim_state_retains_counts_without_sources(self):
        state=self.store.state(full=False);row=next(row for row in state['suppliers'] if row['id']==self.row['id'])
        self.assertTrue(row['_summary']);self.assertEqual(row['sources'],[]);self.assertEqual(row['offer_count'],1);self.assertTrue(row['needs_price_review'])

    def test_05_full_rerun_preserves_manual_decision_and_unique_sources(self):
        store=self.store;sid=self.row['id']
        source={'url':self.row['url'],'excerpt':'unique quote evidence'}
        result={'offers':[{'model':'deepseek-v4.1','ratio':.3,'currency':'USD','unit':'1M tokens'}],'contacts':{},'sources':[source]}
        store.save_result(sid,result);store.review(sid,'skipped','人工核实')
        store.save_result(sid,dict(result,sources=[source,source]))
        row=store.get(sid)
        self.assertEqual((row['status'],row['reason'],row['notes']),('skipped','人工复核：人工核实','人工核实'))
        self.assertEqual(sum(item==source for item in row['sources']),1)
        store.save_result(sid,dict(result,offers=[dict(result['offers'][0],unit='1K tokens')]))
        self.assertIn('报价已变化，建议重新复核',store.get(sid)['warnings'])

if __name__=='__main__':unittest.main()
