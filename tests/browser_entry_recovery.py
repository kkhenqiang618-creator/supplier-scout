"""End-to-end real browsers + fixture model, localhost migration notices only."""
import json,sys,tempfile,threading,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import browser_agent
from browser_agent import Browser,ChromeBrowser
from model_service import ModelService

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/notice':body='<h1>星河平台域名升级通知</h1><p>旧域名不再使用，请访问新域名。</p><a href="/new">立即访问新域名</a>'
        elif self.path=='/meta':body='<h1>星河平台域名升级通知</h1><p>请访问新域名</p><meta http-equiv="refresh" content="30;url=/new">'
        else:body='<h1>星河平台</h1><p>联系商务邮箱 sales@fixture.example</p><p>'+('公开官网介绍。'*40)+'</p>'
        raw=body.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

class MigrationBrowserTests(unittest.TestCase):
    factory=ChromeBrowser
    @classmethod
    def setUpClass(cls):cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=cls.server.serve_forever,daemon=True).start();cls.base='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close()
    def run_migration(self,path):
        base=self.base;factory=self.factory
        class Fixture(factory):
            def __init__(self,folder):super().__init__(folder,allow_loopback_for_tests=True)
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(Path(folder)/'model.json');service.save({'base_url':'https://model.example/v1','model':'fixture'});c=service.read();c['tested']=True;service._write(c)
            answer={'contacts':[{'type':'email','value':'sales@fixture.example','source_url':base+'/new','evidence':'联系商务邮箱 sales@fixture.example'}],'action':{'type':'finish'}}
            # Production rejects localhost; relax only this fixture origin in these tests.
            def checked(url):
                if not url.startswith(base+'/'):raise ValueError('fixture origin only')
            with patch('entry_recovery.safe_candidate',side_effect=lambda value,source:value.startswith(base+'/')),patch('browser_agent.validate_url',side_effect=checked),patch('browser_agent.fetch_robots',return_value='User-agent: *\nAllow: /'),patch.object(service,'complete',return_value=json.dumps(answer)) as model:
                result=browser_agent.run_agent(base+path,'contacts',service,[],browser_factory=Fixture,max_steps=2)
            self.assertEqual(result['contact_status'],'found');self.assertEqual(result['entry_recovery']['to'],base+'/new');self.assertEqual(result['entry_recovery']['from'],base+path);self.assertTrue(result['entry_recovery']['verified']);self.assertEqual(model.call_count,1);self.assertIn(base+path,[s['url'] for s in result['sources']])
    def test_notice_recovers_and_finds_contact_in_one_model_observation(self):self.run_migration('/notice')
    def test_meta_refresh_recovers_without_waiting_30_seconds(self):self.run_migration('/meta')

if __name__=='__main__':
    if '--moli' in sys.argv:sys.argv.remove('--moli');MigrationBrowserTests.factory=Browser
    unittest.main(verbosity=2)
