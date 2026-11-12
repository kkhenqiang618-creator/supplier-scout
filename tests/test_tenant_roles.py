import http.client,json,os,threading,unittest
from unittest.mock import patch,MagicMock
from http.server import ThreadingHTTPServer
from auth import AuthManager,hash_password
from cloud_store import Database,PostgresStore,CloudModelService
from cloud_http import CloudHandler
from cryptography.fernet import Fernet
from test_personal_keys import SettingsDB

class TenantRoleTests(unittest.TestCase):
 def test_database_sets_explicit_local_tenant_and_system_context(self):
  connection=MagicMock()
  with patch('cloud_store.psycopg.connect',return_value=connection):
   db=Database('postgresql://fixture').for_user('fo')
   with db.transaction():pass
  calls=[call for call in connection.execute.call_args_list if 'set_config' in call.args[0]]
  self.assertEqual(len(calls),1);self.assertEqual(calls[0].args[1],('fo','off'))
 def test_admin_role_requires_server_configuration(self):
  store=PostgresStore.__new__(PostgresStore);store.database=SettingsDB();store.lock=threading.RLock();store.model_service=CloudModelService(store.database,Fernet.generate_key().decode())
  with patch.dict(os.environ,{'SCOUT_ADMIN_USERNAME':'小姜'}):
   self.assertTrue(store.for_user('小姜').is_admin);self.assertFalse(store.for_user('小吴').is_admin)
  with patch.dict(os.environ,{},clear=True):self.assertFalse(store.for_user('小姜').is_admin)
 def test_http_reports_require_admin_and_omit_credentials(self):
  verifier=hash_password('fixture-password')
  with patch.dict(os.environ,{'SCOUT_AUTH_USERS_JSON':json.dumps({'小姜':verifier,'小吴':verifier}),'SCOUT_ADMIN_USERNAME':'小姜','SCOUT_ALLOW_HTTP':'1'}):
   auth=AuthManager.from_env(required=True,secure_cookie=False)
   class Shared(PostgresStore):
    def state(self,full=True):return {'username':self.username,'is_admin':self.is_admin,'suppliers':[{'id':1 if self.username=='小姜' else 2,'name':self.username}], 'model_service':{'base_url':'private-provider'},'integrations':{'llm':True}}
    @property
    def job(self):return {'running':False}
    def team_overview(self,names):return {'members':[{'username':name} for name in names]}
    def get(self,sid):
     if sid!=(1 if self.username=='小姜' else 2):raise ValueError('供应商不存在')
     return {'id':sid,'name':self.username}
   store=Shared.__new__(Shared);store.database=SettingsDB();store.lock=threading.RLock();store.model_service=CloudModelService(store.database,Fernet.generate_key().decode())
   with patch('cloud_http.context',return_value=(store,auth)):
    server=ThreadingHTTPServer(('127.0.0.1',0),CloudHandler);threading.Thread(target=server.serve_forever,daemon=True).start()
    def req(path,cookie=None,data=None):
     c=http.client.HTTPConnection(*server.server_address,timeout=5);headers={'Content-Type':'application/json'}
     if cookie:headers['Cookie']=cookie
     c.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers);r=c.getresponse();result=(r.status,dict(r.getheaders()),json.loads(r.read()));c.close();return result
    try:
     a=req('/api/auth/login',data={'username':'小姜','password':'fixture-password'})[1]['Set-Cookie'].split(';')[0]
     b=req('/api/auth/login',data={'username':'小吴','password':'fixture-password'})[1]['Set-Cookie'].split(';')[0]
     for path in ['/api/admin/overview','/api/admin/member?username=fo','/api/admin/supplier?username=fo&id=1']:
      self.assertEqual(req(path,b)[0],403);self.assertEqual(req(path)[0],401)
     self.assertEqual(req('/api/admin/overview',a)[0],200)
     report=req('/api/admin/member?username=%E5%B0%8F%E5%90%B4',a)
     self.assertEqual(report[0],200);self.assertEqual(report[2]['suppliers'][0]['id'],2);self.assertNotIn('model_service',report[2]);self.assertNotIn('private-provider',json.dumps(report[2]))
     self.assertEqual(req('/api/admin/member?username=unknown',a)[0],404)
     self.assertEqual(req('/api/supplier?id=1&username=%E5%B0%8F%E5%A7%9C',b)[0],404)
     self.assertEqual(req('/api/supplier-entry',data={'id':1,'url':'https://example.com','expected_url':'https://old.example','evidence':'fixture'})[0],401)
     self.assertEqual(req('/api/supplier-entry',b,data={'id':1,'url':'https://example.com','expected_url':'https://old.example','evidence':'fixture','username':'小姜'})[0],400)
    finally:server.shutdown();server.server_close()
