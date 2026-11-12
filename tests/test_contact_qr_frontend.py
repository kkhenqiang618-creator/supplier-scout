"""Contact QR frontend: vendored library route, auth boundary, and the Node QR helper test."""
import hashlib, http.client, json, os, shutil, subprocess, tempfile, threading, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
import auth, server
ROOT = Path(__file__).resolve().parents[1]
QR_SHA256 = '18ae399f81182bc9de916e9c77b195df20cc58d6f2d55a62b085a299f1bf1780'

class ContactQrFrontendTests(unittest.TestCase):
    def serve(self, handler, **attrs):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        app = ThreadingHTTPServer(('127.0.0.1', 0), handler); app.daemon_threads = True; app.store = server.Store(os.path.join(tmp.name, 'test.db'), seed=False)
        for key, value in attrs.items(): setattr(app, key, value)
        threading.Thread(target=app.serve_forever, daemon=True).start(); self.addCleanup(app.server_close); self.addCleanup(app.shutdown)
        def request(path, method='GET', data=None, headers=None):
            connection = http.client.HTTPConnection(*app.server_address, timeout=5)
            connection.request(method, path, None if data is None else json.dumps(data), {'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse(); result = response.status, dict(response.getheaders()), response.read(); connection.close(); return result
        return request

    def test_vendored_library_is_unchanged_and_loaded_before_app(self):
        self.assertEqual(hashlib.sha256((ROOT / 'static/vendor/qrcode.js').read_bytes()).hexdigest(), QR_SHA256)
        html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
        self.assertLess(html.index('<script src="/vendor/qrcode.js" defer></script>'), html.index('<script src="/app.js" defer></script>'))
        self.assertNotRegex(html, r'<script(?![^>]*\bsrc=)'); self.assertNotIn('//cdn', html)

    def test_local_route_serves_only_the_fixed_vendor_path(self):
        request = self.serve(server.Handler)
        status, headers, body = request('/vendor/qrcode.js')
        self.assertEqual(status, 200); self.assertEqual(headers['Content-Type'], 'application/javascript; charset=utf-8'); self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(body, (ROOT / 'static/vendor/qrcode.js').read_bytes())
        for path in ['/vendor/', '/vendor/../server.py', '/vendor/%2e%2e/server.py', '/vendor/qrcode.js/../../server.py', '/static/vendor/qrcode.js', '/vendor/QRCODE.js', '/vendor/other.js']:
            with self.subTest(path=path): self.assertEqual(request(path)[0], 404)

    def test_vendor_path_requires_login_like_app_js(self):
        class Protected(auth.AuthHandlerMixin, server.Handler): pass
        request = self.serve(Protected, auth=auth.AuthManager('owner', auth.hash_password('test-only-password'), session_store=auth.MemorySessionStore()))
        for path in ['/app.js', '/vendor/qrcode.js']:
            with self.subTest(path=path): self.assertEqual(request(path)[0], 303)
        cookie = request('/api/auth/login', 'POST', {'username': 'owner', 'password': 'test-only-password'})[1]['Set-Cookie'].split(';', 1)[0]
        for path in ['/app.js', '/vendor/qrcode.js']:
            with self.subTest(path=path, authenticated=True): self.assertEqual(request(path, headers={'Cookie': cookie})[0], 200)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_qr_helper_renders_refuses_and_falls_back(self):
        output = subprocess.run(['node', str(ROOT / 'tests/test_contact_qr.cjs')], capture_output=True, text=True, timeout=60)
        self.assertEqual(output.returncode, 0, output.stderr + output.stdout)

if __name__ == '__main__': unittest.main()
