import contextlib
import importlib.util
import threading
import time
import unittest
from unittest.mock import MagicMock,patch

@unittest.skipUnless(importlib.util.find_spec('psycopg') and importlib.util.find_spec('cryptography'),'cloud adapter dependencies are optional locally')
class CloudJobResilienceTests(unittest.TestCase):
    def worker_store(self,count):
        store=MagicMock();rows={i:{'id':i,'url':'https://s'+str(i)+'.example.com','name':'s'+str(i)} for i in range(1,count+1)};store.get.side_effect=rows.__getitem__
        store.writes=[]
        class DB:
            def execute(self,query,args=()):
                if 'position' in query:store.writes.append(args[0])
        @contextlib.contextmanager
        def fenced(*args):yield DB(),{'cancel_requested':False}
        store.fenced=fenced;return store
    def run_job(self,store,count,outcomes):
        import cloud_worker
        calls=iter(outcomes)
        def collect(url,log,stop,scope,path,rules):
            outcome=next(calls)
            if isinstance(outcome,Exception):raise outcome
            return outcome
        job={'id':'job','position':0,'supplier_ids':list(range(1,count+1)),'mode':'real','scope':'contacts','state':{'_model_config':'x'}}
        with patch.object(cloud_worker,'collect_site',side_effect=collect):cloud_worker.execute_job(store,job,'lease',threading.Event())
    def transient(self,partial=None):
        import cloud_worker
        error=cloud_worker.ModelTransientSiteError('模型服务暂时不可用');error.partial=partial;return error
    def ok(self):return {'contacts':{'email':['a@example.com']},'sources':[],'contact_status':'found'}
    def test_transient_site_error_saves_partial_advances_and_continues(self):
        store=self.worker_store(3)
        self.run_job(store,3,[self.transient({'contacts':{},'sources':[]}),self.ok(),self.ok()])
        self.assertEqual(store.writes,[1,2,3]);store.finish_job.assert_called_with('job','lease','completed')
        first=store.save_contacts.call_args_list[0].args
        self.assertEqual(first[0],1);self.assertEqual(first[1]['contact_status'],'blocked');self.assertEqual(first[1]['contact_summary'],'模型服务暂时不可用，本站稍后重试')
        store.save_result.assert_not_called()
    def test_transient_site_status_ignores_run_agent_partial_status(self):
        # run_agent always sends contact_status; a model-transient site must never be saved 'found' or skip '继续未完成'.
        store=self.worker_store(3)
        self.run_job(store,3,[self.transient({'contacts':{},'sources':[],'contact_status':'incomplete','warnings':['模型服务暂时不可用']}),self.transient({'contacts':{'email':['a@example.com']},'sources':[],'contact_status':'found','warnings':['模型服务暂时不可用']}),self.ok()])
        first,second=[call.args[1] for call in store.save_contacts.call_args_list[:2]]
        self.assertEqual(first['contact_status'],'blocked');self.assertEqual(second['contact_status'],'incomplete');self.assertEqual(second['contacts'],{'email':['a@example.com']})
        self.assertEqual(first['warnings'],['模型服务暂时不可用']);self.assertEqual(second['warnings'],['模型服务暂时不可用'])
    def test_shutdown_requeue_resets_claim_so_waiting_hint_shows(self):
        import cloud_worker
        store=self.worker_store(2);queries=[]
        class DB:
            def execute(self,query,args=()):queries.append((query,args))
        @contextlib.contextmanager
        def fenced(*args):yield DB(),{'cancel_requested':False}
        store.fenced=fenced;shutdown=threading.Event()
        def collect(url,log,stop,scope,path,rules):shutdown.set();return self.ok()
        job={'id':'job','position':0,'supplier_ids':[1,2],'mode':'real','scope':'contacts','state':{'_model_config':'x'}}
        with patch.object(cloud_worker,'collect_site',side_effect=collect):cloud_worker.execute_job(store,job,'lease',shutdown)
        release=[args for query,args in queries if 'lease_owner=NULL' in query];self.assertEqual(len(release),1)
        self.assertEqual(release[0][0],'queued');self.assertIsNone(release[0][1].obj['claimed_at']);self.assertGreater(release[0][1].obj['queued_at'],0)
    def test_heartbeat_grace_counts_from_last_successful_renewal(self):
        import cloud_worker
        store=self.worker_store(1);seen=[]
        def slow_fail(*args):time.sleep(.15);raise RuntimeError('db blip')
        store.heartbeat.side_effect=slow_fail
        def collect(url,log,stop,scope,path,rules):
            stop.wait(.6);seen.append((stop.is_set(),store.heartbeat.call_count));return self.ok()
        job={'id':'job','position':0,'supplier_ids':[1],'mode':'real','scope':'contacts','state':{'_model_config':'x'}}
        with patch.object(cloud_worker,'collect_site',side_effect=collect),patch.object(cloud_worker,'HEARTBEAT_INTERVAL',.02),patch.object(cloud_worker,'HEARTBEAT_GRACE',.1):
            cloud_worker.execute_job(store,job,'lease',threading.Event())
        self.assertEqual(seen,[(True,1)]);store.finish_job.assert_not_called()
    def test_three_consecutive_transient_sites_fail_the_batch(self):
        store=self.worker_store(5)
        self.run_job(store,5,[self.transient(),self.transient(),self.transient(),self.ok(),self.ok()])
        self.assertEqual(store.writes,[1,2,3]);store.finish_job.assert_called_once_with('job','lease','failed','模型服务连续 3 家不可用，批次已暂停；可稍后继续')
    def test_success_resets_consecutive_model_failure_counter(self):
        store=self.worker_store(6)
        self.run_job(store,6,[self.transient(),self.transient(),self.ok(),self.transient(),self.transient(),self.ok()])
        self.assertEqual(store.writes,[1,2,3,4,5,6]);store.finish_job.assert_called_once_with('job','lease','completed')
    def test_fatal_agent_error_still_ends_batch(self):
        from server import AgentServiceError
        store=self.worker_store(3)
        self.run_job(store,3,[AgentServiceError('API Key 无效'),self.ok(),self.ok()])
        self.assertEqual(store.writes,[]);store.finish_job.assert_called_once_with('job','lease','failed','API Key 无效')
    def test_step_log_failures_are_best_effort(self):
        store=self.worker_store(1);store.progress.side_effect=RuntimeError('db blip')
        self.run_job(store,1,[self.ok()])
        self.assertEqual(store.writes,[1]);store.finish_job.assert_called_with('job','lease','completed')
    def test_heartbeat_errors_retry_until_grace_and_lost_lease_stops_at_once(self):
        import cloud_worker
        from cloud_store import LostLease
        for error,grace,expect_stop in [(RuntimeError('db blip'),3600,False),(RuntimeError('db down'),0,True),(LostLease('gone'),3600,True)]:
            store=self.worker_store(1);store.heartbeat.side_effect=error;seen=[]
            def collect(url,log,stop,scope,path,rules):
                stop.wait(.4);seen.append(stop.is_set());return self.ok()
            job={'id':'job','position':0,'supplier_ids':[1],'mode':'real','scope':'contacts','state':{'_model_config':'x'}}
            with patch.object(cloud_worker,'collect_site',side_effect=collect),patch.object(cloud_worker,'HEARTBEAT_INTERVAL',.02),patch.object(cloud_worker,'HEARTBEAT_GRACE',grace):
                cloud_worker.execute_job(store,job,'lease',threading.Event())
            self.assertEqual(seen,[expect_stop],str(error));self.assertGreater(store.heartbeat.call_count,1 if not expect_stop else 0)
            if not expect_stop:store.finish_job.assert_called_with('job','lease','completed')
            else:store.finish_job.assert_not_called()
    def test_start_job_orders_never_attempted_first_then_oldest_attempt(self):
        from cloud_store import PostgresStore
        rows=[{'id':1,'checked_at':None,'contact_checked_at':'2026-11-10T09:00:00+00:00','attempt_scope':'contacts'},{'id':2,'checked_at':None,'contact_checked_at':'2026-11-01T09:00:00+00:00','attempt_scope':'contacts'},{'id':3,'checked_at':None,'contact_checked_at':None,'attempt_scope':None},{'id':4,'checked_at':'2026-10-01T00:00:00+00:00','contact_checked_at':None,'attempt_scope':None},{'id':5,'checked_at':None,'contact_checked_at':None,'attempt_scope':None}]
        self.assertEqual([row['id'] for row in PostgresStore.order_rows(rows,'contacts')],[3,4,5,2,1])
        self.assertEqual([row['id'] for row in PostgresStore.order_rows(rows,'all')],[1,2,3,5,4])
        rows[0]['attempt_scope']='all';self.assertEqual([row['id'] for row in PostgresStore.order_rows(rows,'all')],[2,3,5,4,1])
    def scripted_store(self,active):
        from cloud_store import PostgresStore
        queries=[]
        class Cursor:
            def __init__(self,row):self.row=row
            def fetchone(self):return self.row
            def fetchall(self):return [self.row] if self.row else []
        class DB:
            @contextlib.contextmanager
            def transaction(self,write=False):yield self
            def execute(self,query,args=()):
                queries.append((' '.join(query.split()),args))
                return Cursor(active if query.lstrip().startswith('SELECT') and 'cloud_jobs' in query else None)
        class Scripted(PostgresStore):
            job={}
        store=Scripted.__new__(Scripted);store.database=DB();store.lock=threading.RLock();store.username='小吴'
        store.event=MagicMock();store.model_service=MagicMock();store.model_service.public.return_value={'configured':True,'key_configured':True}
        return store,queries
    def test_stop_job_finalises_expired_lease_directly(self):
        for status in ['running','stopping','queued']:
            store,queries=self.scripted_store({'id':'job','status':status,'mode':'real','position':2,'stale':True,'state':{'requested_by':'小吴','total':5}})
            store.stop_job()
            update=[q for q,_ in queries if q.startswith('UPDATE cloud_jobs')]
            self.assertEqual(len(update),1);self.assertIn("status='stopped'",update[0]);self.assertIn('lease_owner=NULL',update[0]);self.assertIn('lease_until=NULL',update[0])
            store.event.assert_called_once();self.assertIn('2 / 5',store.event.call_args.args[0])
    def test_stop_job_with_live_lease_only_requests_stop(self):
        store,queries=self.scripted_store({'id':'job','status':'running','mode':'real','position':0,'stale':False,'state':{'requested_by':'小吴','total':5}})
        store.stop_job()
        update=[q for q,_ in queries if q.startswith('UPDATE cloud_jobs')];self.assertEqual(len(update),1);self.assertIn("status='stopping'",update[0]);self.assertNotIn('lease_owner=NULL',update[0]);store.event.assert_not_called()
    def test_late_worker_writes_are_fenced_after_direct_stop(self):
        # Every worker write path filters on its own lease, a live lease_until and an active status; a directly stopped job matches none.
        from cloud_store import PostgresStore,LostLease
        store,queries=self.scripted_store(None)
        with self.assertRaises(LostLease):
            with store.fenced('job','late-owner'):pass
        with self.assertRaises(LostLease):store.heartbeat('job','late-owner')
        with self.assertRaises(LostLease):store.progress('job','late-owner','late step')
        with self.assertRaises(LostLease):store.finish_job('job','late-owner','completed')
        for query,_ in queries:
            self.assertIn('lease_owner=%s',query.replace('?','%s'));self.assertIn('lease_until>now()',query);self.assertIn("status IN ('running','stopping')",query)
    def test_start_job_reports_lost_worker(self):
        store,_=self.scripted_store({'id':'job','status':'running','stale':True})
        with patch.dict('os.environ',{},clear=False):
            import os;os.environ.pop('VERCEL',None)
            with self.assertRaisesRegex(ValueError,'后台进程已失联，请先点击停止'):store.start_job('real',scope='contacts')
        store,_=self.scripted_store({'id':'job','status':'queued','stale':True})
        with patch.dict('os.environ',{},clear=False):
            import os;os.environ.pop('VERCEL',None)
            with self.assertRaisesRegex(ValueError,'已有云端批次运行'):store.start_job('real',scope='contacts')
    def test_stopping_flag_only_while_job_is_active(self):
        from cloud_store import PostgresStore
        base={'id':'job','state':{'requested_by':'小吴'},'cancel_requested':True,'position':1,'scope':'contacts','mode':'real'}
        self.assertTrue(PostgresStore.job_state(dict(base,status='stopping'))['stopping'])
        self.assertTrue(PostgresStore.job_state(dict(base,status='running'))['stopping'])
        for status in ['stopped','failed','completed','queued']:
            state=PostgresStore.job_state(dict(base,status=status));self.assertFalse(state['stopping'],status)
        self.assertFalse(PostgresStore.job_state(dict(base,status='stopped'))['running'])
    def test_job_state_exposes_claim_wait_and_heartbeat_age(self):
        from cloud_store import PostgresStore
        clock=time.time();base={'id':'job','cancel_requested':False,'position':0,'scope':'contacts','mode':'real'}
        queued=PostgresStore.job_state(dict(base,status='queued',state={'queued_at':clock-150,'claimed_at':None}))
        self.assertGreaterEqual(queued['waiting_seconds'],150);self.assertEqual(queued['heartbeat_age'],0)
        running=PostgresStore.job_state(dict(base,status='running',state={'queued_at':clock-300,'claimed_at':clock-200,'heartbeat_at':clock-70}))
        self.assertEqual(running['waiting_seconds'],0);self.assertGreaterEqual(running['heartbeat_age'],70);self.assertIn('claimed_at',running)
        self.assertEqual(PostgresStore.job_state(dict(base,status='stopped',state={'heartbeat_at':clock-500}))['heartbeat_age'],0)

    def test_busy_message_names_the_occupant_and_its_progress(self):
        from cloud_store import PostgresStore
        row={'owner':'小姜','position':12,'total':40}
        admin=PostgresStore.busy_message(row,True)
        self.assertIn('成员 小姜',admin);self.assertIn('12/40',admin)
        member=PostgresStore.busy_message(row,False)
        self.assertNotIn('小姜',member);self.assertIn('其他成员',member);self.assertIn('12/40',member)
        self.assertIn('其他批次',PostgresStore.busy_message(None,True))
    def test_start_job_collision_reports_the_occupant(self):
        import psycopg
        store,_=self.scripted_store(None)
        def collide(*args,**kwargs):raise psycopg.errors.UniqueViolation('one active batch')
        store._start_job=collide;store._busy_message=lambda:'云端浏览器同一时间只执行一个批次：成员 小姜正在采集，已完成 12/40 家，请稍后重试'
        with self.assertRaisesRegex(ValueError,'成员 小姜'):store.start_job('real',scope='contacts')

if __name__=='__main__':unittest.main()
