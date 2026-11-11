import tempfile
import unittest
from pathlib import Path
from model_service import ModelService

class ModelServiceTests(unittest.TestCase):
    def test_save_url_model_key_without_exposing_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(Path(folder)/'provider.json')
            result=service.save({'base_url':'https://example.com/v1/','model':'my-model','api_key':'a-test-secret'})
            self.assertNotIn('api_key',result);self.assertTrue(result['key_configured']);self.assertFalse(result['tested'])
            self.assertEqual(service.read()['base_url'],'https://example.com/v1')
            service.save({'base_url':'https://example.com/v1','model':'other-model','api_key':''})
            self.assertEqual(service.read()['api_key'],'a-test-secret')
            self.assertEqual(service.path.stat().st_mode & 0o777,0o600)

if __name__=='__main__':unittest.main()

class ProviderBoundaryTests(unittest.TestCase):
    def test_root_url_uses_v1_and_test_resolves_exact_advertised_model(self):
        import json,threading
        from http.server import HTTPServer,BaseHTTPRequestHandler
        seen=[]
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.assert_path=self.path
                self.send_response(200);self.end_headers()
                self.wfile.write(json.dumps({'data':[{'id':'deepseek-v4.1-flash'}]}).encode())
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append((self.path,body['model']))
                ok=self.path=='/v1/chat/completions' and body['model']=='deepseek-v4.1-flash'
                self.send_response(200 if ok else 503);self.end_headers()
                self.wfile.write(json.dumps({'choices':[{'message':{'content':'OK'}}]} if ok else {'error':{'message':'No available accounts'}}).encode())
        server=HTTPServer(('127.0.0.1',0),Provider);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                service=ModelService(Path(folder)/'provider.json')
                service.save({'base_url':'http://127.0.0.1:'+str(server.server_port),'model':'DeepSeek-V4.1-Flash','api_key':'test-key'})
                self.assertEqual(service.test()['reply'],'OK')
                self.assertEqual(service.public()['model'],'deepseek-v4.1-flash')
                self.assertTrue(service.public()['tested'])
                self.assertEqual(seen,[('/v1/chat/completions','deepseek-v4.1-flash')])
        finally:server.shutdown();server.server_close();thread.join()
    def test_provider_error_is_actionable_and_secret_is_redacted(self):
        import io,urllib.error
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(Path(folder)/'provider.json');service.save({'base_url':'https://example.com/v1','model':'wrong','api_key':'test-secret'})
            error=urllib.error.HTTPError('https://example.com/v1/chat/completions',503,'Service Unavailable',{},io.BytesIO(b'{"error":{"message":"No available accounts test-secret"}}'))
            with patch('urllib.request.OpenerDirector.open',side_effect=error):
                with self.assertRaisesRegex(ValueError,'无可用账号') as raised:service.complete([])
            self.assertIn('503',str(raised.exception));self.assertNotIn('test-secret',str(raised.exception))
    def test_changing_provider_does_not_forward_previous_key(self):
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(Path(folder)/'provider.json')
            service.save({'base_url':'https://example.com/v1','model':'one','api_key':'old-key'})
            service.save({'base_url':'https://example.org/v1','model':'two','api_key':''})
            self.assertFalse(service.public()['key_configured'])
    def test_test_result_cannot_mark_a_different_configuration_successful(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(Path(folder)/'provider.json')
            service.save({'base_url':'https://example.com/v1','model':'one','api_key':'test-key'})
            def network(messages,config=None):
                service.save({'base_url':'https://example.com/v1','model':'two','api_key':''})
                return 'OK'
            with patch.object(service,'complete',side_effect=network):
                with self.assertRaisesRegex(ValueError,'配置已变更'):service.test()
            self.assertEqual(service.public()['model'],'two');self.assertFalse(service.public()['tested'])
    def test_actual_http_contract_empty_body_and_redirect_boundaries(self):
        import json,threading
        from http.server import HTTPServer,BaseHTTPRequestHandler
        seen=[]
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):seen.append(self.headers.get('Authorization'));self.send_response(200);self.end_headers()
        destination=HTTPServer(('127.0.0.1',0),Receiver)
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if body['model']=='redirect':
                    self.send_response(302);self.send_header('Location','http://127.0.0.1:'+str(destination.server_port)+'/');self.end_headers();return
                ok=self.path=='/v1/chat/completions' and self.headers.get('Authorization')=='Bearer test-key'
                self.send_response(200 if ok else 400);self.end_headers()
                self.wfile.write(json.dumps({'choices':[{'message':{'content':'OK' if body['model']=='works' else None}}]}).encode())
        server=HTTPServer(('127.0.0.1',0),Provider)
        threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in [server,destination]]
        for thread in threads:thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                service=ModelService(Path(folder)/'provider.json');base='http://127.0.0.1:'+str(server.server_port)+'/v1'
                service.save({'base_url':base,'model':'works','api_key':'test-key'})
                self.assertEqual(service.test()['reply'],'OK');self.assertTrue(service.public()['tested'])
                service.save({'base_url':base,'model':'empty','api_key':''})
                with self.assertRaisesRegex(ValueError,'空正文'):service.test()
                self.assertFalse(service.public()['tested'])
                service.save({'base_url':base,'model':'redirect','api_key':''})
                with self.assertRaisesRegex(ValueError,'重定向'):service.test()
                self.assertEqual(seen,[])
        finally:
            for item in [server,destination]:item.shutdown();item.server_close()
            for thread in threads:thread.join()
