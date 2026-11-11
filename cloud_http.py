"""Authenticated Vercel HTTP adapter. Browser work is queued, never threaded here."""
import os
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from auth import AuthManager, AuthHandlerMixin
from cloud_store import Database,PostgresStore,PostgresSessionStore,PostgresLoginLimiter
from server import Handler

_CONTEXT=None
_LOCK=threading.Lock()

def context():
    global _CONTEXT
    with _LOCK:
        if _CONTEXT is None:
            database=Database();store=PostgresStore(database)
            auth=AuthManager.from_env(required=True,session_store=PostgresSessionStore(database),limiter=PostgresLoginLimiter(database),secure_cookie=os.environ.get('SCOUT_ALLOW_HTTP')!='1')
            _CONTEXT=(store,auth)
        return _CONTEXT

class CloudBusinessHandler(Handler):
    @property
    def store(self):
        username=self.auth_session['username']
        if getattr(getattr(self,'_personal_store',None),'username',None)!=username:self._personal_store=self.server.store.for_user(username)
        return self._personal_store
    def setup(self):
        self.server.store,self.server.auth=context()
        super().setup()
    def check_host(self):
        host=(self.headers.get('Host','')).split(':')[0].lower()
        if os.environ.get('SCOUT_ALLOW_HTTP')=='1' and host in ['localhost','127.0.0.1']:return True
        allowed={urlsplit('https://'+os.environ.get(name,'')).hostname for name in ['VERCEL_URL','VERCEL_BRANCH_URL','VERCEL_PROJECT_PRODUCTION_URL']}
        allowed.add(urlsplit(os.environ.get('SCOUT_AUTH_ORIGIN','')).hostname)
        return host in allowed
    def do_GET(self):
        if not self.check_host():return self.send({'error':'访问地址不匹配'},403)
        path=urlsplit(self.path).path
        if path=='/api/state':return self.send(self.store.state(full=False))
        if path=='/api/supplier':
            try:return self.send(self.store.get(int(parse_qs(urlsplit(self.path).query).get('id',['0'])[0])))
            except (ValueError,KeyError):return self.send({'error':'供应商不存在'},404)
        return super().do_GET()
    def do_POST(self):
        if int(self.headers.get('Content-Length','0'))>4*1024*1024:return self.send({'error':'云端单次请求过大，请将原始文件控制在3MB以内'},413)
        from cloud_scaling import REQUEST_TOKEN
        request_token=REQUEST_TOKEN.set(self.headers.get('x-vercel-oidc-token',''))
        try:return super().do_POST()
        finally:REQUEST_TOKEN.reset(request_token)

class CloudHandler(AuthHandlerMixin,CloudBusinessHandler):pass

def create_cloud_server(port=0):
    server=ThreadingHTTPServer(('127.0.0.1',port),CloudHandler);server.daemon_threads=True
    server.store,server.auth=context();return server
