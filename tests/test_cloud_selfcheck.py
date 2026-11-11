"""Daemon egress self-check: probes run in the worker process before any supplier is visited."""
import contextlib
import importlib.util
import json
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

KEY='sk-selfcheck-secret'
PROXY='http://user:proxy-password@10.0.0.1:3128'

@unittest.skipUnless(importlib.util.find_spec('psycopg') and importlib.util.find_spec('cryptography'),'cloud adapter dependencies are optional locally')
class CloudSelfCheckTests(unittest.TestCase):
    def store(self):
        store=MagicMock();store.get.side_effect=lambda sid:{'id':sid,'url':'https://s'+str(sid)+'.example.com','name':'s'+str(sid)}
        def materialize(folder,snapshot=None):
            path=Path(folder)/'model-service.json';path.write_text(json.dumps({'base_url':'https://api.naoshengyun.com/v1','model':'m','api_key':KEY}));return path
        store.model_service.materialize.side_effect=materialize
        @contextlib.contextmanager
        def fenced(*args):yield MagicMock(),{'cancel_requested':False}
        store.fenced=fenced;return store
    def run_job(self,store,probes,mode='real'):
        import cloud_worker
        outcomes=iter(probes);seen=[];sites=[]
        def complete(service,messages,config=None,attempts=3):
            seen.append((len(json.dumps({'model':'m','messages':messages}).encode()),attempts));outcome=next(outcomes)
            if isinstance(outcome,Exception):raise outcome
            return outcome
        def collect(url,log,stop,scope,path,rules):sites.append(url);return {'contacts':{},'sources':[],'contact_status':'incomplete'}
        job={'id':'job','position':0,'supplier_ids':[1,2],'mode':mode,'scope':'contacts','state':{'_model_config':'x'}}
        with patch.object(cloud_worker.ModelService,'complete',complete),patch.object(cloud_worker,'collect_site',side_effect=collect),patch.object(cloud_worker.socket,'getaddrinfo',return_value=[(2,1,6,'',('203.0.113.7',443))]),patch.dict(os.environ,{'HTTPS_PROXY':PROXY}),patch.object(cloud_worker.time,'sleep'):
            cloud_worker.execute_job(store,job,'lease',threading.Event())
        events=[c.args[0] for c in store.event.call_args_list]+[c.args[2] for c in store.progress.call_args_list]
        return seen,sites,events
    def request_timeout(self):
        from model_service import ModelTransientError
        return ModelTransientError('模型服务调用失败：连接或上传请求超时（云端网络出口问题，不是模型响应慢）：timed out',phase='request')
    def test_small_probe_failure_fails_job_before_any_site(self):
        store=self.store();seen,sites,events=self.run_job(store,[self.request_timeout()])
        self.assertEqual(sites,[]);self.assertEqual(len(seen),1);self.assertEqual(seen[0][1],1)
        store.finish_job.assert_called_once();args=store.finish_job.call_args.args
        self.assertEqual(args[2],'failed');self.assertTrue(args[3].startswith('云端后台无法连接模型服务：连接或上传请求超时'));self.assertIn('批次未开始，已保存自检结果',args[3])
        self.assertTrue(any(e.startswith('云端自检：小请求 失败（连接或上传请求超时') for e in events));store.save_contacts.assert_not_called();store.save_result.assert_not_called()
    def test_large_probe_failure_reports_mtu(self):
        store=self.store();seen,sites,events=self.run_job(store,['OK',self.request_timeout()])
        self.assertEqual(sites,[]);self.assertGreaterEqual(seen[1][0],55_000);self.assertLessEqual(seen[1][0],70_000)
        args=store.finish_job.call_args.args;self.assertEqual(args[2],'failed');self.assertIn('云端后台无法上传较大的请求（疑似网络 MTU 问题）',args[3])
        self.assertTrue(any(e.startswith('云端自检：小请求 成功') for e in events));self.assertTrue(any(e.startswith('云端自检：60KB 请求 失败') for e in events))
    def test_config_error_fails_with_its_message(self):
        from model_service import ModelConfigError
        store=self.store();_,sites,_=self.run_job(store,[ModelConfigError('模型服务调用失败：HTTP 401：bad。请核对 API Key')])
        self.assertEqual(sites,[]);self.assertIn('请核对 API Key',store.finish_job.call_args.args[3])
    def test_both_probes_ok_then_sites_proceed(self):
        store=self.store();seen,sites,events=self.run_job(store,['OK','OK'])
        self.assertEqual([a for _,a in seen],[1,1]);self.assertEqual(len(sites),2);store.finish_job.assert_called_with('job','lease','completed')
        self.assertTrue(any('api.naoshengyun.com 解析为 IPv4 203.0.113.7' in e for e in events))
    def test_demo_job_skips_probes(self):
        store=self.store();seen,_,events=self.run_job(store,[],mode='demo')
        self.assertEqual(seen,[]);self.assertFalse(any('云端自检' in e for e in events));store.finish_job.assert_called_with('job','lease','completed')
    def test_proxy_value_and_key_never_logged(self):
        store=self.store();_,_,events=self.run_job(store,[self.request_timeout()])
        joined=' '.join(events)+' '+' '.join(map(str,store.finish_job.call_args.args))
        self.assertIn('已设置 HTTPS_PROXY',joined);self.assertNotIn('proxy-password',joined);self.assertNotIn('10.0.0.1',joined);self.assertNotIn(KEY,joined)
    def test_large_probe_http_or_reply_failure_is_not_mtu_and_batch_continues(self):
        from model_service import ModelTransientError,ModelConfigError
        for failure in [ModelTransientError('模型服务调用失败：HTTP 400：maximum context length',retryable=False),ModelTransientError('模型服务调用失败：HTTP 503：busy'),ModelTransientError('模型服务调用失败：等待模型响应超时：timed out',phase='response'),ModelConfigError('模型服务调用失败：HTTP 400：bad padding')]:
            store=self.store();_,sites,events=self.run_job(store,['OK',failure])
            self.assertEqual(len(sites),2);store.finish_job.assert_called_with('job','lease','completed');self.assertFalse(any('MTU' in e for e in events))
            self.assertTrue(any('上传成功，模型返回异常，已忽略' in e for e in events))
    def test_small_probe_http_transient_continues_without_connect_wording(self):
        from model_service import ModelTransientError
        for failure in [ModelTransientError('模型服务调用失败：HTTP 429：slow down',retry_after=1),ModelTransientError('模型服务调用失败：等待模型响应超时：timed out',phase='response')]:
            store=self.store();seen,sites,events=self.run_job(store,[failure])
            self.assertEqual(len(seen),1);self.assertEqual(len(sites),2);store.finish_job.assert_called_with('job','lease','completed');self.assertFalse(any('无法连接' in e for e in events))
    def test_unresolvable_host_still_logs_egress_line(self):
        import cloud_worker
        store=self.store()
        with patch.object(cloud_worker.socket,'getaddrinfo',side_effect=UnicodeError('label too long')):
            path=store.model_service.materialize(self.tmp(),None);line=cloud_worker.egress_report(path)
        self.assertIn('解析失败（UnicodeError）',line)
    def tmp(self):
        import tempfile;folder=tempfile.mkdtemp();self.addCleanup(__import__('shutil').rmtree,folder);return folder
    def test_stop_between_probes_cancels(self):
        import cloud_worker
        from server import CollectionCancelled
        store=self.store();stop=threading.Event();calls=[]
        def complete(service,messages,config=None,attempts=3):calls.append(1);stop.set();return 'OK'
        with patch.object(cloud_worker.ModelService,'complete',complete),patch.object(cloud_worker.socket,'getaddrinfo',return_value=[]):
            with self.assertRaises(CollectionCancelled):cloud_worker.self_check(store,{'state':{}},'lease',lambda m:None,stop)
        self.assertEqual(len(calls),1)
    def test_cancel_before_probes_skips_them(self):
        import cloud_worker
        store=self.store()
        @contextlib.contextmanager
        def fenced(*args):yield MagicMock(),{'cancel_requested':True}
        store.fenced=fenced;seen,sites,_=self.run_job(store,[])
        self.assertEqual(seen,[]);self.assertEqual(sites,[]);store.finish_job.assert_called_with('job','lease','stopped')

if __name__=='__main__':unittest.main()
