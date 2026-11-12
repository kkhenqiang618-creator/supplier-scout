"""GuardProxy / resolve_public / is_public_ip: the Chrome fallback's SSRF filter. Loopback sockets only, no internet."""
import os
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from egress_proxy import Denied, GuardProxy, resolve_public

def fixed(*ips):
    calls = []
    def resolver(host, port, **kw):
        calls.append(host); return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, 1, 6, '', (ip, port)) for ip in ips]
    resolver.calls = calls
    return resolver

PRIVATE = ['127.0.0.1', '10.1.2.3', '172.16.0.1', '172.31.255.1', '192.168.1.1', '169.254.169.254', '100.64.0.1', '0.0.0.0', '::1', 'fe80::1', 'fc00::1', 'fd12::1',
           '::ffff:10.0.0.1', '::ffff:127.0.0.1', '2002:a00:1::', '64:ff9b::a00:1', '64:ff9b:1::a00:1', 'fe80::1%en0',
           '224.0.0.1', '239.255.255.250', 'ff05::2', 'ff0e::1', 'fec0::1']

class IsPublicIpTests(unittest.TestCase):
    def test_private_reserved_and_tunnelled_forms_refused(self):
        for ip in PRIVATE:
            with self.subTest(ip=ip): self.assertFalse(engine.is_public_ip(ip))
    def test_public_forms_allowed(self):
        for ip in ['93.184.216.34', '2606:4700::1111', '::ffff:8.8.8.8', '2002:808:808::1']:
            with self.subTest(ip=ip): self.assertTrue(engine.is_public_ip(ip))
    def test_validate_url_uses_it(self):
        for ip in ['2002:a00:1::', '64:ff9b::a00:1']:
            with self.subTest(ip=ip), patch('engine.socket.getaddrinfo', fixed(ip)), self.assertRaisesRegex(ValueError, '内网'): engine.validate_url('https://example.com/')

class ResolvePublicTests(unittest.TestCase):
    def test_private_refused(self):
        for ip in PRIVATE:
            with self.subTest(ip=ip), self.assertRaises(Denied): resolve_public('example.com', 443, resolver=fixed(ip))
    def test_mixed_public_and_private_refused(self):
        with self.assertRaises(Denied): resolve_public('example.com', 443, resolver=fixed('93.184.216.34', '10.0.0.1'))
    def test_public_pinned_to_first_and_resolved_once(self):
        resolver = fixed('93.184.216.34', '93.184.216.35')
        self.assertEqual(resolve_public('example.com', 443, resolver=resolver), '93.184.216.34'); self.assertEqual(resolver.calls, ['example.com'])
    def test_ports(self):
        for port in [80, 443]: self.assertEqual(resolve_public('example.com', port, resolver=fixed('93.184.216.34')), '93.184.216.34')
        for port in [22, 8080]:
            with self.subTest(port=port), self.assertRaisesRegex(Denied, '80/443'): resolve_public('example.com', port, resolver=fixed('93.184.216.34'))
    def test_loopback_switch_is_keyword_only_and_narrow(self):
        self.assertEqual(resolve_public('localhost', 5555, resolver=fixed('127.0.0.1'), allow_loopback=True), '127.0.0.1')
        for ip in ['10.0.0.1', '169.254.169.254']:
            with self.subTest(ip=ip), self.assertRaises(Denied): resolve_public('x.test', 443, resolver=fixed(ip), allow_loopback=True)
        with self.assertRaises(Denied): resolve_public('x.test', 443, resolver=fixed('127.0.0.1', '10.0.0.1'), allow_loopback=True)
        with self.assertRaises(Denied): resolve_public('localhost', 5555, resolver=fixed('127.0.0.1'), allow_loopback='yes')
    def test_environment_cannot_enable_loopback(self):
        env = {k: '1' for k in ['SCOUT_ALLOW_LOOPBACK', 'ALLOW_LOOPBACK_FOR_TESTS', 'SCOUT_CHROME_FALLBACK', 'SCOUT_PROXY_ALLOW_PRIVATE']}
        with patch.dict(os.environ, env):
            proxy = GuardProxy(resolver=fixed('127.0.0.1'))
            try: self.assertFalse(proxy.allow_loopback)
            finally: proxy.close()
        proxy = GuardProxy(allow_loopback_for_tests='true')
        try: self.assertFalse(proxy.allow_loopback)
        finally: proxy.close()

def exchange(port, payload, read=65536):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
        sock.sendall(payload); data = b''
        while True:
            chunk = sock.recv(read)
            if not chunk: break
            data += chunk
        return data

class ProxySocketTests(unittest.TestCase):
    def setUp(self):
        self.proxy = GuardProxy(allow_loopback_for_tests=True, resolver=lambda h, p, **k: fixed('127.0.0.1')(h, p) if h in ['127.0.0.1', 'localhost'] else fixed('10.0.0.5' if h.startswith('internal') else '169.254.169.254' if h.startswith('169.254') else '10.0.0.1')(h, p))
    def tearDown(self): self.proxy.close()
    def test_connect_to_metadata_is_403(self):
        reply = exchange(self.proxy.port, b'CONNECT 169.254.169.254:443 HTTP/1.1\r\nHost: 169.254.169.254:443\r\n\r\n')
        self.assertTrue(reply.startswith(b'HTTP/1.1 403')); self.assertTrue(any('169.254.169.254' in t for t, _ in self.proxy.denied))
    def test_get_private_is_403(self):
        self.assertTrue(exchange(self.proxy.port, b'GET http://10.0.0.1/ HTTP/1.1\r\nHost: 10.0.0.1\r\n\r\n').startswith(b'HTTP/1.1 403'))
        self.assertTrue(exchange(self.proxy.port, b'GET http://internal.corp/ HTTP/1.1\r\nHost: internal.corp\r\n\r\n').startswith(b'HTTP/1.1 403'))
    def test_non_http_schemes_refused(self):
        self.assertTrue(exchange(self.proxy.port, b'GET ftp://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n').startswith(b'HTTP/1.1 403'))
        self.assertTrue(exchange(self.proxy.port, b'GET /relative HTTP/1.1\r\nHost: example.com\r\n\r\n').startswith(b'HTTP/1.1 403'))
    def test_oversized_header_refused(self):
        reply = exchange(self.proxy.port, b'GET http://127.0.0.1/ HTTP/1.1\r\nX-Pad: ' + b'a' * 20000 + b'\r\n\r\n')
        self.assertTrue(reply.startswith(b'HTTP/1.1 431'))
    def test_chunked_upload_refused(self):
        reply = exchange(self.proxy.port, b'POST http://127.0.0.1:1/ HTTP/1.1\r\nHost: 127.0.0.1:1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n')
        self.assertTrue(reply.startswith(b'HTTP/1.1 411'))
    def test_connect_tunnel_to_loopback_echo(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1); port = server.getsockname()[1]
        def echo():
            conn, _ = server.accept()
            with conn:
                data = conn.recv(1024); conn.sendall(b'echo:' + data)
        thread = threading.Thread(target=echo, daemon=True); thread.start()
        try:
            with socket.create_connection(('127.0.0.1', self.proxy.port), timeout=5) as sock:
                sock.sendall(b'CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n' % port)
                head = b''
                while b'\r\n\r\n' not in head: head += sock.recv(1024)
                self.assertTrue(head.startswith(b'HTTP/1.1 200'))
                sock.sendall(b'ping'); self.assertEqual(sock.recv(1024), b'echo:ping')
        finally: server.close()
    def test_plain_get_forwarded_with_host_and_without_proxy_headers(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1); port = server.getsockname()[1]; seen = {}
        def serve():
            conn, _ = server.accept()
            with conn:
                data = b''
                while b'\r\n\r\n' not in data: data += conn.recv(4096)
                seen['request'] = data; conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        threading.Thread(target=serve, daemon=True).start()
        try:
            reply = exchange(self.proxy.port, b'GET http://localhost:%d/a?b=1 HTTP/1.1\r\nHost: localhost:%d\r\nProxy-Authorization: x\r\nProxy-Connection: keep-alive\r\n\r\n' % (port, port))
            self.assertTrue(reply.endswith(b'ok'))
            request = seen['request'].decode()
            self.assertTrue(request.startswith('GET /a?b=1 HTTP/1.1')); self.assertIn('Host: localhost:%d' % port, request); self.assertIn('Connection: close', request)
            self.assertNotIn('Proxy-', request)
        finally: server.close()
    def test_close_ends_open_tunnels(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1); port = server.getsockname()[1]
        def hold():
            conn, _ = server.accept()
            with conn: conn.recv(1)
        threading.Thread(target=hold, daemon=True).start()
        try:
            sock = socket.create_connection(('127.0.0.1', self.proxy.port), timeout=5)
            sock.sendall(b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % port); sock.recv(1024)
            self.proxy.close(); self.assertEqual(sock.recv(10), b''); sock.close()
            self.proxy = GuardProxy()
        finally: server.close()

if __name__ == '__main__': unittest.main()
