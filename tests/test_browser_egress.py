"""Egress routing: no provider credentials or external traffic."""
import io
import json
import os
import socket
import unittest
from unittest.mock import patch
from egress_proxy import GuardProxy, Denied, resolve_public
import browser_egress as net

class EgressTests(unittest.TestCase):
    def test_configuration_explicit_and_invalid_is_rejected(self):
        with patch.dict(os.environ, {'HTTPS_PROXY':'http://other:99'},clear=True):
            self.assertIsNone(net.configured_upstream())
        for value in ['socks5://localhost:7897','http://user:secret@localhost:7897','http://localhost:7897/a','http://localhost:7897/?x=1']:
            with self.subTest(value=value),patch.dict(os.environ,{'SCOUT_BROWSER_PROXY_URL':value}),self.assertRaisesRegex(ValueError,'采集代理'):
                net.configured_upstream()
    def test_remote_resolution_checks_addresses_and_cache(self):
        p=net.Upstream('http://127.0.0.1:7897')
        with patch.object(p,'_query',return_value=['104.26.14.134']) as query:
            self.assertEqual(resolve_public('site.test',443,resolver=p.resolve),'104.26.14.134')
            p.resolve('site.test',443);self.assertEqual(query.call_count,1)
        for ips in [['10.0.0.1'],['104.26.14.134','127.0.0.1'],[]]:
            p=net.Upstream('http://127.0.0.1:7897')
            with patch.object(p,'_query',return_value=ips),self.assertRaises((Denied,OSError)):
                resolve_public('site.test',443,resolver=p.resolve)
    def test_literals_do_not_query_dns_and_private_is_denied(self):
        p=net.Upstream('http://127.0.0.1:7897')
        with patch.object(p,'_query',side_effect=AssertionError('literal must not query')):
            self.assertEqual(resolve_public('8.8.8.8',443,resolver=p.resolve),'8.8.8.8')
            with self.assertRaises(Denied):resolve_public('169.254.169.254',443,resolver=p.resolve)
    def test_doh_uses_configured_proxy_and_refuses_redirects(self):
        p=net.Upstream('http://127.0.0.1:7897')
        class Reply(io.BytesIO):
            status=200
        payload=json.dumps({'Status':0,'Answer':[{'type':1,'data':'104.26.14.134'}]}).encode()
        with patch('browser_egress.urllib.request.build_opener') as build:
            build.return_value.open.return_value=Reply(payload)
            self.assertEqual(p._query('site.test',1),['104.26.14.134'])
            args=build.call_args.args
            proxy=next(x for x in args if hasattr(x,'proxies'))
            self.assertEqual(proxy.proxies['https'],'http://127.0.0.1:7897')
            self.assertTrue(any(isinstance(x,net.NoRedirect) for x in args))
    def test_connect_only_sends_pinned_ip_and_closes_on_failure(self):
        p=net.Upstream('http://127.0.0.1:7897')
        for head,success in [(b'HTTP/1.1 200 OK\r\n\r\n',True),(b'HTTP/1.1 502 Bad Gateway\r\n\r\n',False)]:
            from unittest.mock import MagicMock
            sock=MagicMock();sock.recv.side_effect=[bytes([b]) for b in head]
            with patch('browser_egress.socket.create_connection',return_value=sock):
                if success:self.assertIs(p.connect('104.26.14.134',443),sock)
                else:
                    with self.assertRaises(OSError):p.connect('104.26.14.134',443)
                    sock.close.assert_called_once()
            self.assertIn(b'CONNECT 104.26.14.134:443 HTTP/1.1',sock.sendall.call_args.args[0])
            self.assertNotIn(b'site.test',sock.sendall.call_args.args[0])
    def test_guard_selects_upstream_and_does_not_use_direct_dial(self):
        from unittest.mock import MagicMock
        upstream=MagicMock();p=GuardProxy(upstream=upstream)
        try:
            self.assertIs(p.resolver,upstream.resolve)
            with patch('egress_proxy.socket.create_connection',side_effect=AssertionError('must not dial direct')):
                self.assertIs(p._connect('8.8.8.8',443),upstream.connect.return_value)
        finally:p.close()


class IntegrationTests(unittest.TestCase):
    def test_moli_creates_guard_and_closes_it_when_upstream_is_configured(self):
        import browser_agent
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        guard=MagicMock();guard.port=12345
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'SCOUT_BROWSER_PROXY_URL':'http://127.0.0.1:7897'}),patch('browser_agent.runtime_paths',return_value=('moli','module')),patch('egress_proxy.GuardProxy',return_value=guard),patch('browser_agent.subprocess.Popen') as popen,patch('browser_agent.urllib.request.urlopen'),patch.object(browser_agent.Browser,'_spawn'):
            b=browser_agent.Browser(folder)
            args=popen.call_args.args[0]
            self.assertIn('--http-proxy',args)
            self.assertEqual(args[args.index('--http-proxy')+1],'http://127.0.0.1:12345')
            b.close();guard.close.assert_called_once()
    def test_auxiliary_download_enters_guard_even_with_no_proxy(self):
        from unittest.mock import MagicMock
        with patch.dict(os.environ,{'SCOUT_BROWSER_PROXY_URL':'http://127.0.0.1:7897','NO_PROXY':'*'}),patch('egress_proxy.GuardProxy') as cls:
            cls.return_value.port=12345
            with net.collection_opener() as opener:
                req=__import__('urllib.request',fromlist=['Request']).Request('https://example.com/image.png')
                handler=next(h for h in opener.handlers if isinstance(h,net.urllib.request.ProxyHandler))
                handler.proxy_open(req,'http://127.0.0.1:12345','https')
                self.assertEqual(req.host,'127.0.0.1:12345');self.assertEqual(req._tunnel_host,'example.com')
            cls.return_value.close.assert_called_once()

class SocketChainTests(unittest.TestCase):
    def test_http_chain_preserves_host_and_pins_connect_target(self):
        import threading
        from test_egress_proxy import exchange
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen();seen=[]
        def serve():
            conn,_=server.accept()
            with conn:
                conn.settimeout(3);head=b''
                while not head.endswith(b'\r\n\r\n'):head+=conn.recv(1)
                seen.append(head);conn.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n');head=b''
                while not head.endswith(b'\r\n\r\n'):head+=conn.recv(1)
                seen.append(head);conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        upstream=net.Upstream('http://127.0.0.1:'+str(server.getsockname()[1]))
        with patch.object(upstream,'_query',return_value=['93.184.216.34']):
            guard=GuardProxy(upstream=upstream)
            try:
                reply=exchange(guard.port,b'GET http://site.test/a HTTP/1.1\r\nHost: site.test\r\nProxy-Authorization: secret\r\n\r\n')
                self.assertTrue(reply.endswith(b'ok'));self.assertIn(b'CONNECT 93.184.216.34:80 ',seen[0]);self.assertIn(b'GET /a HTTP/1.1',seen[1]);self.assertIn(b'Host: site.test',seen[1]);self.assertNotIn(b'secret',b''.join(seen))
            finally:guard.close();server.close();thread.join(3)
    def test_private_dns_never_reaches_upstream_connect(self):
        from test_egress_proxy import exchange
        upstream=net.Upstream('http://127.0.0.1:7897')
        with patch.object(upstream,'_query',return_value=['127.0.0.1']),patch.object(upstream,'connect') as connect:
            guard=GuardProxy(upstream=upstream)
            try:
                response=exchange(guard.port,b'CONNECT site.test:443 HTTP/1.1\r\n\r\n')
                self.assertTrue(response.startswith(b'HTTP/1.1 403'));connect.assert_not_called()
            finally:guard.close()
    def test_proxy_outage_never_retries_direct(self):
        from test_egress_proxy import exchange
        upstream=net.Upstream('http://127.0.0.1:7897')
        with patch.object(upstream,'_query',return_value=['93.184.216.34']),patch.object(upstream,'connect',side_effect=OSError('offline')) as connect:
            guard=GuardProxy(upstream=upstream)
            try:
                response=exchange(guard.port,b'CONNECT site.test:443 HTTP/1.1\r\n\r\n')
                self.assertTrue(response.startswith(b'HTTP/1.1 502'));connect.assert_called_once_with('93.184.216.34',443)
            finally:guard.close()

if __name__=='__main__':unittest.main()
