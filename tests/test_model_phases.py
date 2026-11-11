"""Phase-aware model failures against local sockets only: upload stalls vs. slow replies."""
import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch
import model_service
from model_service import ModelService,ModelTransientError

KEY='sk-phase-secret-key'

class PhaseTests(unittest.TestCase):
    def service(self,port):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        service=ModelService(Path(folder.name)/'provider.json');service.save({'base_url':'http://127.0.0.1:'+str(port)+'/v1','model':'m','api_key':KEY});return service
    def counting(self):
        original=model_service.ModelService._complete_once;calls=[]
        def wrapped(self,*args,**kwargs):calls.append(1);return original(self,*args,**kwargs)
        return patch.object(model_service.ModelService,'_complete_once',wrapped),calls
    def test_upload_stall_is_request_phase_and_retried_once(self):
        listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096);listener.bind(('127.0.0.1',0));listener.listen(8)
        held=[];alive=threading.Event();alive.set()
        def accept():
            listener.settimeout(.2)
            while alive.is_set():
                try:held.append(listener.accept()[0])  # Accept but never read: the client blocks while sending the body.
                except OSError:pass
        thread=threading.Thread(target=accept,daemon=True);thread.start()
        try:
            service=self.service(listener.getsockname()[1]);wrapper,calls=self.counting()
            with wrapper,patch.object(model_service,'REQUEST_TIMEOUT',.5),patch('model_service.time.sleep'):
                with self.assertRaises(ModelTransientError) as raised:service.complete([{'role':'user','content':'填'*4_000_000}],attempts=3)
            self.assertEqual(raised.exception.phase,'request');self.assertIn('连接或上传请求超时',str(raised.exception));self.assertEqual(len(calls),2);self.assertNotIn(KEY,str(raised.exception))
        finally:
            alive.clear();thread.join();listener.close()
            for item in held:item.close()
    def serve(self,handler):
        server=HTTPServer(('127.0.0.1',0),handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()));return server.server_port
    def test_reply_stall_is_response_phase_with_full_retries(self):
        release=threading.Event();self.addCleanup(release.set)
        class Slow(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):self.rfile.read(int(self.headers['Content-Length']));release.wait(2)
        service=self.service(self.serve(Slow));wrapper,calls=self.counting()
        with wrapper,patch.object(model_service,'REQUEST_TIMEOUT',.3),patch('model_service.time.sleep'):
            with self.assertRaises(ModelTransientError) as raised:service.complete([{'role':'user','content':'hi'}],attempts=3)
        self.assertEqual(raised.exception.phase,'response');self.assertIn('等待模型响应超时',str(raised.exception));self.assertEqual(len(calls),3);self.assertNotIn(KEY,str(raised.exception))
    def test_bare_ssl_error_after_handshake_is_response_phase(self):
        import ssl
        self.assertEqual(model_service.failure_phase(ssl.SSLEOFError(8,'EOF occurred in violation of protocol'))[0],'response')
        self.assertEqual(model_service.failure_phase(ssl.SSLError(1,'handshake failure'))[0],'tls')
        import urllib.error
        self.assertEqual(model_service.failure_phase(urllib.error.URLError(ssl.SSLEOFError(8,'EOF')))[0],'tls')
    def test_immediate_reply_still_returns_content(self):
        class Fast(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'choices':[{'message':{'content':'OK'}}]}).encode())
        self.assertEqual(self.service(self.serve(Fast)).complete([{'role':'user','content':'hi'}]),'OK')
    def test_refused_is_connect_phase_and_keeps_text(self):
        probe=socket.socket();probe.bind(('127.0.0.1',0));port=probe.getsockname()[1];probe.close()
        service=self.service(port);wrapper,calls=self.counting()
        with wrapper,patch('model_service.time.sleep'):
            with self.assertRaises(ModelTransientError) as raised:service.complete([],attempts=3)
        self.assertEqual(raised.exception.phase,'connect');self.assertEqual(len(calls),2);self.assertIn('模型服务调用失败：',str(raised.exception));self.assertNotIn('超时',str(raised.exception))

if __name__=='__main__':unittest.main()
