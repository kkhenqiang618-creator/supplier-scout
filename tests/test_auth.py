"""HTTP boundary regressions: never delegate protected requests before authentication."""
import hashlib
import http.client
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert importlib.util.find_spec('auth') is not None, 'auth guard is not implemented'
        import auth
        cls.auth_module = auth
        cls.password_hash = auth.hash_password('test-only-password')

    def setUp(self):
        module = self.auth_module
        self.store = module.MemorySessionStore()
        self.manager = module.AuthManager('owner', self.password_hash, session_store=self.store)
        class BusinessHandler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'PRIVATE SUPPLIERS')
            do_POST = do_GET
        class ProtectedHandler(module.AuthHandlerMixin, BusinessHandler): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ProtectedHandler)
        self.server.auth = self.manager
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, method='GET', data=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        payload = None if data is None else json.dumps(data)
        request_headers = {'Content-Type': 'application/json'} if data is not None else {}
        request_headers.update(headers or {})
        connection.request(method, path, payload, request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self, **overrides):
        return self.request('/api/auth/login', 'POST', dict(username='owner', password='test-only-password', **overrides))

    def cookie(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_private_pages_exports_and_apis_are_blocked(self):
        for path, expected in [('/', 303), ('/app.js', 303), ('/export.csv', 303), ('/api/state', 401), ('/api/export', 401)]:
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, expected)
                self.assertNotIn(b'PRIVATE', body)
                self.assertIn('no-store', headers['Cache-Control'])
        self.assertEqual(self.request('/api/import', 'POST', {})[0], 401)

    def test_only_explicit_login_assets_are_public(self):
        for path in ['/login', '/login.html', '/login.js', '/login.css']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 200)
        self.assertEqual(self.request('/favicon.ico')[0], 204)
        self.assertEqual(self.request('/login/../app.js')[0], 303)

    def test_invalid_credentials_create_no_session(self):
        status, headers, body = self.request('/api/auth/login', 'POST', {'username':'owner','password':'wrong'})
        self.assertEqual(status, 401)
        self.assertNotIn('Set-Cookie', headers)
        self.assertNotIn(b'test-only-password', body)

    def test_login_session_is_private_and_logout_revokes(self):
        status, headers, body = self.login()
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        token = cookie.split('=', 1)[1]
        self.assertGreaterEqual(len(token), 43)
        for attribute in ['HttpOnly', 'SameSite=Lax', 'Secure', 'Path=/']:
            self.assertIn(attribute, headers['Set-Cookie'])
        stored = self.store.get(hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(stored['username'], 'owner')
        self.assertNotIn(token, repr(stored))
        self.assertNotIn('test-only-password', repr(stored))
        self.assertNotIn(token.encode(), body)
        self.assertEqual(self.request('/api/state', headers={'Cookie':cookie})[2], b'PRIVATE SUPPLIERS')
        session = self.request('/api/auth/session', headers={'Cookie':cookie})
        self.assertEqual(json.loads(session[2])['username'], 'owner')
        self.assertEqual(self.request('/api/auth/logout','POST',{}, {'Cookie':cookie})[0], 200)
        self.assertEqual(self.request('/api/state', headers={'Cookie':cookie})[0], 401)
        self.assertIsNone(self.store.get(hashlib.sha256(token.encode()).hexdigest()))

    def test_expired_sessions_cannot_read_data(self):
        cookie = self.cookie()
        self.manager.clock = lambda: time.time() + 100000
        self.assertEqual(self.request('/api/state', headers={'Cookie':cookie})[0], 401)

    def test_cross_origin_mutation_and_login_are_rejected(self):
        for path in ['/api/auth/login', '/api/auth/logout', '/api/import']:
            self.assertEqual(self.request(path,'POST',{}, {'Origin':'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/auth/login','POST',{}, {'Sec-Fetch-Site':'cross-site'})[0], 403)
        origin = 'https://%s:%s' % self.server.server_address
        self.assertEqual(self.request('/api/auth/login','POST', {'username':'owner','password':'test-only-password'}, {'Origin':origin})[0], 200)

    def test_login_body_is_bounded_and_json_only(self):
        self.assertEqual(self.request('/api/auth/login','POST', {'password':'x'*9000})[0], 413)
        self.assertEqual(self.request('/api/auth/login','POST', {}, {'Content-Type':'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/auth/login','POST', [1,2])[0], 400)
        self.assertEqual(self.request('/api/auth/login','POST', {'username':[], 'password':{}})[0], 400)

    def test_failed_login_attempts_are_throttled(self):
        for _ in range(5):
            self.assertEqual(self.request('/api/auth/login','POST',{'username':'owner','password':'wrong'})[0], 401)
        self.assertEqual(self.login()[0], 429)

    def test_login_throttle_is_per_account_so_a_shared_address_does_not_lock_out_colleagues(self):
        for _ in range(5):
            self.assertEqual(self.request('/api/auth/login','POST',{'username':'owner','password':'wrong'})[0],401)
        self.assertEqual(self.request('/api/auth/login','POST',{'username':'owner','password':'wrong'})[0],429)
        self.assertEqual(self.request('/api/auth/login','POST',{'username':'teammate','password':'wrong'})[0],401)

    def test_login_throttle_uses_the_forwarded_client_address_behind_vercel(self):
        with patch.dict(os.environ,{'VERCEL':'1'}):
            for _ in range(5):
                self.assertEqual(self.request('/api/auth/login','POST',{'username':'owner','password':'wrong'},{'X-Real-Ip':'203.0.113.7'})[0],401)
            blocked=self.request('/api/auth/login','POST',{'username':'owner','password':'wrong'},{'X-Real-Ip':'203.0.113.7'})
            other=self.request('/api/auth/login','POST',{'username':'owner','password':'wrong'},{'X-Real-Ip':'203.0.113.8'})
        self.assertEqual(blocked[0],429);self.assertEqual(other[0],401)

    def test_forwarded_headers_are_ignored_without_the_platform_flag(self):
        module=self.auth_module
        class Fake:
            client_address=('127.0.0.1',1234)
            headers={'x-real-ip':'203.0.113.7','x-forwarded-for':'198.51.100.9, 203.0.113.7'}
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(module.client_ip(Fake()),'127.0.0.1')
        with patch.dict(os.environ,{'VERCEL':'1'}):
            self.assertEqual(module.client_ip(Fake()),'203.0.113.7')
            Fake.headers={'x-forwarded-for':'198.51.100.9, 203.0.113.7'}
            self.assertEqual(module.client_ip(Fake()),'203.0.113.7')
    def test_next_target_cannot_redirect_to_another_origin(self):
        for unsafe in ['//evil.example', 'https://evil.example', '/\\evil.example', '/%2f%2fevil.example', '/\r\nX: evil']:
            self.assertEqual(json.loads(self.login(next=unsafe)[2])['next'], '/')
        self.assertEqual(json.loads(self.login(next='/suppliers?mode=real')[2])['next'], '/suppliers?mode=real')

    def test_local_auth_disabled_keeps_business_routes_working(self):
        self.server.auth = None
        self.assertEqual(self.request('/api/state')[2], b'PRIVATE SUPPLIERS')

    def test_malformed_origin_is_rejected_without_connection_failure(self):
        self.assertEqual(self.request('/api/auth/login', 'POST', {}, {'Origin':'https://['})[0], 403)

    def test_secure_origin_can_be_pinned_independently_of_proxy_host(self):
        self.manager.allowed_origin = 'https://scout.example'
        credentials = {'username':'owner', 'password':'test-only-password'}
        self.assertEqual(self.request('/api/auth/login','POST',credentials, {'Origin':'https://scout.example'})[0], 200)
        self.assertEqual(self.request('/api/auth/login','POST',credentials, {'Origin':'https://evil.example','Host':'evil.example'})[0], 403)

    def test_local_cookie_can_disable_secure_and_relogin_rotates_session(self):
        self.manager.secure_cookie = False
        old = self.cookie()
        response = self.request('/api/auth/login','POST', {'username':'owner','password':'test-only-password'}, {'Cookie':old})
        self.assertNotIn('Secure', response[1]['Set-Cookie'])
        self.assertEqual(self.request('/api/state', headers={'Cookie':old})[0], 401)
        current = response[1]['Set-Cookie'].split(';',1)[0]
        self.assertEqual(self.request('/api/state',headers={'Cookie':current})[0],200)

    def test_guard_wraps_actual_business_handler_without_disclosing_data(self):
        from server import Handler, Store
        class ProtectedActual(self.auth_module.AuthHandlerMixin, Handler): pass
        with tempfile.TemporaryDirectory() as directory:
            self.server.RequestHandlerClass = ProtectedActual
            self.server.store = Store(os.path.join(directory, 'test.db'), seed=False)
            self.server.store.import_rows([{'name':'Private fixture supplier','url':'https://example.com'}])
            for path in ['/api/state','/api/export','/api/export-contacts']:
                self.assertEqual(self.request(path)[0],401)
            cookie = self.cookie()
            headers = {'Cookie':cookie}
            response = self.request('/api/state',headers=headers)
            self.assertEqual(json.loads(response[2])['suppliers'][0]['name'],'Private fixture supplier')
            self.assertNotIn(b'test-only-password', response[2])
            self.assertNotIn(cookie.split('=',1)[1].encode(), response[2])
            self.assertIn(b'Private fixture supplier',self.request('/api/export',headers=headers)[2])

    def test_env_requires_credentials_and_password_hash_is_salted(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.auth_module.AuthManager.from_env())
            with self.assertRaises(ValueError): self.auth_module.AuthManager.from_env(required=True)
        with patch.dict(os.environ, {'SCOUT_AUTH_USERNAME':'owner'}, clear=True):
            with self.assertRaises(ValueError): self.auth_module.AuthManager.from_env()
        with patch.dict(os.environ, {'SCOUT_AUTH_USERNAME':'owner', 'SCOUT_AUTH_PASSWORD_HASH':self.password_hash}, clear=True):
            self.assertIsInstance(self.auth_module.AuthManager.from_env(), self.auth_module.AuthManager)
        self.assertNotEqual(self.auth_module.hash_password('test-only-password'), self.password_hash)
        with self.assertRaises(ValueError): self.auth_module.AuthManager('owner', 'plaintext')


if __name__ == '__main__': unittest.main()
