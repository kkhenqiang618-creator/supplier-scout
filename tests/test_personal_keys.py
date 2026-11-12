import contextlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from auth import AuthManager,hash_password
from cryptography.fernet import Fernet
from cloud_store import CloudModelService,PostgresStore

class SettingsDB:
    def for_user(self,username):return self
    def __init__(self):
        self.conn=sqlite3.connect(':memory:',check_same_thread=False);self.conn.row_factory=sqlite3.Row
        self.conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)');self.lock=threading.RLock()
    @contextlib.contextmanager
    def transaction(self,write=False):
        with self.lock:yield self.conn

class PersonalKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.verifier=hash_password('test-password')
    def manager(self):
        with patch.dict(os.environ,{'SCOUT_AUTH_USERS_JSON':json.dumps({'小吴':self.verifier,'fo':self.verifier})},clear=True):return AuthManager.from_env(required=True,secure_cookie=False)
    def test_multiple_logins_keep_session_identity_and_reject_old_account(self):
        auth=self.manager()
        self.assertTrue(auth.verify('小吴','test-password'));self.assertTrue(auth.verify('fo','test-password'))
        self.assertFalse(auth.verify('abc123','test-password'));self.assertFalse(auth.verify('fo','wrong'))
        a=auth.create_session('小吴');b=auth.create_session('fo')
        self.assertEqual(auth.authenticate(a)['username'],'小吴');self.assertEqual(auth.authenticate(b)['username'],'fo')
    def test_empty_account_map_fails_closed(self):
        with patch.dict(os.environ,{'SCOUT_AUTH_USERS_JSON':'{}'},clear=True):
            with self.assertRaises(ValueError):AuthManager.from_env(required=True)
    def services(self):
        db=SettingsDB();key=Fernet.generate_key().decode();legacy=CloudModelService(db,key)
        legacy.save({'base_url':'https://legacy.example.com','model':'legacy','api_key':'legacy-secret'})
        return db,CloudModelService(db,key,username='小吴'),CloudModelService(db,key,username='fo')
    def test_personal_configs_never_fall_back_or_overwrite_each_other(self):
        db,a,b=self.services();self.assertEqual(a.read(),{});self.assertEqual(b.read(),{})
        a.save({'base_url':'https://a.example.com','model':'a','api_key':'a-secret'})
        b.save({'base_url':'https://b.example.com','model':'b','api_key':'b-secret'})
        a.save({'base_url':'https://a.example.com','model':'a-new','api_key':''})
        self.assertEqual(a.read()['api_key'],'a-secret');self.assertEqual(b.read()['api_key'],'b-secret')
        self.assertNotIn('api_key',a.public());self.assertNotIn('a-secret',str(db.conn.execute('SELECT * FROM settings').fetchall()))
        a.save({'base_url':'https://different.example.com','model':'a','api_key':''});self.assertFalse(a.public()['key_configured'])
    def test_job_snapshot_is_encrypted_and_independent_of_later_saves(self):
        _,a,b=self.services();a.save({'base_url':'https://a.example.com','model':'a','api_key':'original-secret'})
        snapshot=a.snapshot();self.assertNotIn('original-secret',snapshot)
        a.save({'base_url':'https://a.example.com','model':'a','api_key':'replacement-secret'})
        with tempfile.TemporaryDirectory() as folder:
            path=b.materialize(folder,snapshot=snapshot)
            self.assertEqual(json.loads(Path(path).read_text())['api_key'],'original-secret')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):b.materialize(folder,snapshot='')
    def test_job_public_state_never_exposes_provider_snapshot(self):
        row={'id':'job','status':'queued','state':{'requested_by':'小吴','_model_config':'encrypted-private-config'},'cancel_requested':False,'position':0,'scope':'contacts','mode':'real'}
        result=PostgresStore.job_state(row);self.assertNotIn('_model_config',result);self.assertEqual(result['requested_by'],'小吴')
    def test_scoped_store_does_not_mutate_shared_instance(self):
        db,_,_=self.services();store=PostgresStore.__new__(PostgresStore);store.database=db;store.lock=threading.RLock();store.model_service=CloudModelService(db,Fernet.generate_key().decode())
        a=store.for_user('小吴');b=store.for_user('fo')
        self.assertEqual(a.username,'小吴');self.assertEqual(b.username,'fo');self.assertIsNot(a.model_service,b.model_service)
        self.assertFalse(hasattr(store,'username'));self.assertIs(a.lock,b.lock)
    def test_other_user_cannot_stop_active_batch(self):
        class Cursor:
            def fetchone(self):return {'state':{'requested_by':'小吴'}}
        class DB:
            @contextlib.contextmanager
            def transaction(self,write=False):yield self
            def execute(self,query,*args):
                if not query.startswith('SELECT'):raise AssertionError('Unauthorized mutation')
                return Cursor()
        store=PostgresStore.__new__(PostgresStore);store.database=DB();store.username='fo'
        with self.assertRaisesRegex(ValueError,'任务发起人'):store.stop_job()
    def test_worker_uses_enqueued_config_not_current_account_settings(self):
        import cloud_worker
        from unittest.mock import MagicMock
        db,a,_=self.services();a.save({'base_url':'https://a.example.com','model':'a','api_key':'original-secret'})
        snapshot=a.snapshot();a.save({'base_url':'https://a.example.com','model':'new','api_key':'replacement-secret'})
        store=MagicMock();store.model_service=a;store.get.return_value={'id':1,'url':'https://supplier.example.com','name':'fixture'}
        @contextlib.contextmanager
        def fenced(*args):yield MagicMock(),{'cancel_requested':False}
        store.fenced=fenced
        observed=[]
        def collect(url,log,stop,scope,path,rules):
            observed.append(json.loads(Path(path).read_text())['api_key'])
            return {'contacts':{},'sources':[],'contact_status':'incomplete'}
        job={'id':'job','position':0,'supplier_ids':[1],'mode':'real','scope':'contacts','state':{'requested_by':'小吴','_model_config':snapshot}}
        with patch.object(cloud_worker,'collect_site',side_effect=collect),patch.object(cloud_worker,'self_check',return_value=None):cloud_worker.execute_job(store,job,'lease',threading.Event())
        self.assertEqual(observed,['original-secret']);store.finish_job.assert_called_with('job','lease','completed')
    def test_actual_http_uses_session_owner_and_ignores_forged_username(self):
        from http.server import ThreadingHTTPServer
        from cloud_http import CloudHandler
        db=SettingsDB();cipher=Fernet.generate_key().decode()
        class Shared(PostgresStore):
            def state(self,full=True):return {'username':self.username,'model_service':self.model_service.public()}
        store=Shared.__new__(Shared);store.database=db;store.lock=threading.RLock();store.model_service=CloudModelService(db,cipher)
        auth=self.manager()
        with patch('cloud_http.context',return_value=(store,auth)),patch.dict(os.environ,{'SCOUT_ALLOW_HTTP':'1'}):
            server=ThreadingHTTPServer(('127.0.0.1',0),CloudHandler);threading.Thread(target=server.serve_forever,daemon=True).start()
            def request(path,data=None,cookie=None):
                c=http.client.HTTPConnection(*server.server_address,timeout=4);headers={'Content-Type':'application/json'}
                if cookie:headers['Cookie']=cookie
                c.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers);r=c.getresponse();result=(r.status,dict(r.getheaders()),json.loads(r.read()));c.close();return result
            try:
                cookies=[]
                for name in ['小吴','fo']:
                    status,headers,_=request('/api/auth/login',{'username':name,'password':'test-password'});self.assertEqual(status,200);cookies.append(headers['Set-Cookie'].split(';')[0])
                status,_,_=request('/api/model-service',{'username':'fo','base_url':'https://a.example.com','model':'a','api_key':'a-secret'},cookies[0]);self.assertEqual(status,200)
                a=request('/api/state',cookie=cookies[0])[2];b=request('/api/state',cookie=cookies[1])[2]
                self.assertEqual(a['username'],'小吴');self.assertTrue(a['model_service']['key_configured']);self.assertFalse(b['model_service']['key_configured'])
                self.assertNotIn('a-secret',json.dumps([a,b]));self.assertEqual(request('/api/state')[0],401)
            finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
