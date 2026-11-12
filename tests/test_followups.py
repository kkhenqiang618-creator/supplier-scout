"""Workspace persistence and HTTP contracts, using only disposable fixtures."""
import contextlib
import json
import threading
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import patch
import test_api
from test_personal_keys import SettingsDB
from cloud_store import PostgresStore, CloudModelService
from cryptography.fernet import Fernet
from server import Store, encode

class WorkspaceHttpTests(test_api.ApiTests):
    def fixture(self):
        self.call('/api/import', {'rows':[{'url':'https://example.com'}]})
        sid=self.app.store.state()['suppliers'][0]['id']
        self.app.store.save_result(sid, {'contacts':{'email':['first@example.com','second@example.com']},'offers':[{'model':'DeepSeek-V3','ratio':.35,'notes':str(i)} for i in range(6)]})
        return sid
    def test_followup_save_reload_update_and_validation(self):
        sid=self.fixture()
        response=self.call('/api/followup',{'id':sid,'status':'waiting','note':'已手动联系'})
        self.assertEqual(response[0],200)
        saved=json.loads(response[2]);self.assertEqual(saved['status'],'waiting')
        restored=Store(self.app.store.path,seed=False).state()
        self.assertEqual(restored['followups'][str(sid)],saved)
        self.assertEqual(restored['suppliers'][0]['notes'],'')
        self.assertEqual(self.call('/api/followup',{'id':sid,'status':'done','note':'完成'})[0],200)
        for payload in [{'id':sid,'status':'bad','note':'x'},{'id':sid,'status':'waiting','note':''},{'id':sid,'status':'waiting','note':[]},{'id':999,'status':'waiting','note':'x'}]:
            self.assertEqual(self.call('/api/followup',payload)[0],400)
        record=self.app.store.state()['followups'][str(sid)]
        self.assertEqual(record['status'],'done')
        self.assertEqual([item['status'] for item in record['history']],['waiting','done'])
        self.assertEqual(record['history'][0]['note'],'已手动联系')
    def test_pricing_full_offers_history_and_no_model_service(self):
        sid=self.fixture()
        response=self.call('/api/pricing?mode=real');self.assertEqual(response[0],200)
        data=json.loads(response[2]);self.assertEqual(len(data['suppliers'][0]['offers']),6)
        self.assertEqual(len(data['history'][0]['offers']),6)
        self.assertEqual(set(data),{'suppliers','history','rules'})
        self.assertEqual(set(data['suppliers'][0]),{'id','name','url','mode','offers','checked_at'})
        self.assertEqual(json.loads(self.call('/api/pricing?mode=demo')[2])['suppliers'],[])
        self.assertEqual(self.call('/api/pricing?mode=all')[0],400)
    def test_recipient_persists_and_controls_eml(self):
        sid=self.fixture()
        self.assertEqual(self.call('/api/drafts',{'id':sid,'recipient':'second@example.com'})[0],200)
        self.assertEqual(Store(self.app.store.path,seed=False).state()['drafts'][0]['recipient'],'second@example.com')
        msg=BytesParser(policy=policy.default).parsebytes(self.call('/api/draft?id='+str(sid))[2])
        self.assertEqual(str(msg['To']),'second@example.com')
        self.assertEqual(self.call('/api/drafts',{'id':sid,'subject':'updated'})[0],200)
        self.assertEqual(self.app.store.state()['drafts'][0]['recipient'],'second@example.com')
        for recipient in ['outsider@example.com','second@example.com\r\nBcc:bad@example.com',None]:
            self.assertEqual(self.call('/api/drafts',{'id':sid,'recipient':recipient})[0],400)
        self.app.store.save_result(sid,{'contacts':{'email':['first@example.com']},'offers':[]})
        self.assertEqual(self.call('/api/draft?id='+str(sid))[0],400)

class ScopedWorkspaceTests(unittest.TestCase):
    def stores(self):
        db=SettingsDB()
        class TenantDB:
            # Fixture stand-in for existing Postgres RLS, retaining global settings PKs.
            def __init__(self,username):self.username=username
            @contextlib.contextmanager
            def transaction(self,write=False):
                username=self.username
                class ScopedSQL:
                    def execute(self,query,params=()):
                        if 'jsonb_path_query_array' in query:query='SELECT * FROM suppliers ORDER BY id'
                        if query.startswith('SELECT') and any('FROM '+table in query for table in ('suppliers','history','drafts','events')):
                            before,separator,after=query.partition(' ORDER BY')
                            predicate='owner=?' if 'FROM suppliers' in query else 'supplier_id IN (SELECT id FROM suppliers WHERE owner=?)'
                            query=before+(' AND ' if ' WHERE ' in before else ' WHERE ')+predicate+separator+after
                            params=tuple(params)+(username,)
                        return db.conn.execute(query,params)
                with db.transaction(write) :yield ScopedSQL()
        db.for_user=lambda username:TenantDB(username)
        db.conn.executescript('''CREATE TABLE suppliers(id INTEGER PRIMARY KEY,owner TEXT,name TEXT,url TEXT,mode TEXT,status TEXT,reason TEXT,contacts TEXT,offers TEXT,sources TEXT,warnings TEXT,notes TEXT,checked_at TEXT,created_at TEXT,contact_status TEXT,contact_checked_at TEXT,contact_result TEXT);
        CREATE TABLE cloud_jobs(id TEXT,created_at TEXT);
        CREATE TABLE history(id INTEGER PRIMARY KEY,supplier_id INTEGER,mode TEXT,offers TEXT,created_at TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY,supplier_id INTEGER,mode TEXT,message TEXT,created_at TEXT);
        CREATE TABLE drafts(id INTEGER PRIMARY KEY,supplier_id INTEGER UNIQUE,subject TEXT,body TEXT,created_at TEXT);''')
        for sid,owner in [(1,'alice'),(2,'bob')]:
            db.conn.execute('INSERT INTO suppliers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(sid,owner,owner,'https://example.com','real','review','',encode({'email':[owner+'@example.com']}),'[]','[]','[]','','','','found','','{}'))
        class Scoped(PostgresStore):
            def get(self,sid):
                row=Store.get(self,sid)
                if row['owner']!=self.username:raise ValueError('供应商不存在')
                return row
        store=Scoped.__new__(Scoped);store.database=db;store.lock=threading.RLock();store.model_service=CloudModelService(db,Fernet.generate_key().decode())
        return db,store.for_user('alice'),store.for_user('bob')
    def test_keys_include_member_and_writes_check_supplier_before_settings(self):
        db,a,b=self.stores()
        a.save_followup(1,'waiting','alice note');b.save_followup(2,'replied','bob note')
        a.save_draft(1,recipient='alice@example.com');b.save_draft(2,recipient='bob@example.com')
        keys=[r['key'] for r in db.conn.execute('SELECT key FROM settings')]
        self.assertTrue(any('alice' in key for key in keys));self.assertTrue(any('bob' in key for key in keys))
        before=list(db.conn.execute('SELECT key,value FROM settings'))
        for action in [lambda:b.save_followup(1,'done','forged'),lambda:b.save_draft(1,recipient='alice@example.com'),lambda:b.draft_recipient(1)]:
            with self.assertRaisesRegex(ValueError,'供应商不存在'):action()
        self.assertEqual(before,list(db.conn.execute('SELECT key,value FROM settings')))
        self.assertEqual(a.followups()['1']['note'],'alice note');self.assertNotIn('1',b.followups())
    def test_postgres_methods_use_write_transaction(self):
        db,a,_=self.stores()
        original=db.transaction;calls=[]
        @contextlib.contextmanager
        def observed(write=False):
            calls.append(write)
            with original(write) as conn:yield conn
        db.transaction=observed
        a.save_followup(1,'waiting','hello');self.assertTrue(calls[0]);calls.clear()
        a.save_draft(1,recipient='alice@example.com');self.assertTrue(calls[0])

    def test_pricing_uses_tenant_context_and_returns_all_quotes(self):
        db,a,b=self.stores()
        for sid in [1,2]:
            offers=encode([{'model':'DeepSeek-V3','ratio':sid/10,'notes':str(i)} for i in range(7)])
            db.conn.execute('UPDATE suppliers SET offers=? WHERE id=?',(offers,sid))
            db.conn.execute('INSERT INTO history(supplier_id,mode,offers) VALUES(?,?,?)',(sid,'real',offers))
        for store,sid in [(a,1),(b,2)]:
            data=store.pricing('real')
            self.assertEqual([row['id'] for row in data['suppliers']],[sid])
            self.assertEqual([row['supplier_id'] for row in data['history']],[sid])
            self.assertEqual(len(data['suppliers'][0]['offers']),7)
            self.assertEqual(len(data['history'][0]['offers']),7)

    def test_http_session_blocks_forged_supplier_and_recipient(self):
        import http.client
        import os
        from auth import AuthManager,hash_password
        from cloud_http import CloudHandler
        from http.server import ThreadingHTTPServer
        db,a,b=self.stores()
        class Root:
            def for_user(self,username):return a if username=='alice' else b
        verifier=hash_password('fixture-password')
        with patch.dict(os.environ,{'SCOUT_ALLOW_HTTP':'1','SCOUT_AUTH_USERS_JSON':json.dumps({'alice':verifier,'bob':verifier})}):
            auth=AuthManager.from_env(required=True,secure_cookie=False)
            with patch('cloud_http.context',return_value=(Root(),auth)):
                server=ThreadingHTTPServer(('127.0.0.1',0),CloudHandler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                def call(path,data=None,cookie=None):
                    conn=http.client.HTTPConnection(*server.server_address,timeout=5)
                    headers={'Content-Type':'application/json'}
                    if cookie:headers['Cookie']=cookie
                    conn.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers)
                    response=conn.getresponse();result=(response.status,dict(response.getheaders()),response.read());conn.close();return result
                try:
                    cookie=call('/api/auth/login',{'username':'bob','password':'fixture-password'})[1]['Set-Cookie'].split(';')[0]
                    for path,data in [('/api/followup',{'id':1,'username':'alice','status':'done','note':'forged'}),('/api/drafts',{'id':1,'recipient':'alice@example.com'}),('/api/drafts',{'id':2,'recipient':'alice@example.com'})]:
                        self.assertEqual(call(path,data,cookie)[0],400)
                    self.assertEqual(call('/api/draft?id=1',cookie=cookie)[0],400)
                    response=call('/api/pricing?mode=real&username=alice',cookie=cookie)
                    self.assertEqual(response[0],200);self.assertEqual([row['id'] for row in json.loads(response[2])['suppliers']],[2])
                    self.assertEqual(call('/api/followup',{'id':2,'status':'waiting','note':'manual'},cookie)[0],200)
                    self.assertEqual(a.followups(),{});self.assertIn('2',b.followups())
                finally:server.shutdown();server.server_close();thread.join()

    def test_full_and_summary_state_include_member_followups_and_recipients(self):
        db,a,b=self.stores()
        a.save_followup(1,'waiting','manual contact')
        a.save_draft(1,recipient='alice@example.com')
        for full in [True,False]:
            state=a.state(full=full)
            self.assertEqual(state['followups']['1']['note'],'manual contact')
            self.assertEqual(state['drafts'][0]['recipient'],'alice@example.com')
            other=b.state(full=full)
            self.assertEqual(other['followups'],{})
            self.assertEqual(other['drafts'],[])
        for index in range(105):a.save_followup(1,'done','note '+str(index))
        history=a.followups()['1']['history']
        self.assertEqual(len(history),100)
        self.assertEqual(history[0]['note'],'note 5')
        self.assertEqual(history[-1]['note'],'note 104')
