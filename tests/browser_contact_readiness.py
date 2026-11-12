"""Real browser contact regressions; localhost fixtures, no model or supplier data."""
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser_agent import ChromeBrowser, Browser, observed_text

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        body='''<!doctype html><html><body><p>公开页面介绍与使用帮助。LONG</p>
<button aria-label="通知" onclick="document.querySelector('#notice').innerHTML='公告加载中';setTimeout(()=>document.querySelector('#notice').innerHTML='商务邮箱 sales@fixture.example',1800)">公告</button><section id="notice"></section>
<form onsubmit="document.body.dataset.submitted='yes';return false"><button>提交</button></form>
<a href="/login">登录</a><a href="/delete">删除</a><a href="/404">失效页面</a>
<script>if(location.pathname==='/slow'){document.querySelector('#notice').textContent='Loading...';setTimeout(()=>document.querySelector('#notice').textContent='商务邮箱 slow@fixture.example',2200)}
if(location.pathname==='/scroll'){document.body.style.minHeight='10000px';window.addEventListener('scroll',()=>{if(window.scrollY>2100)document.querySelector('#notice').textContent='深层联系 deep@fixture.example'})}
if(location.pathname==='/spinner')document.querySelector('#notice').innerHTML='<span role="progressbar">Loading...</span>';</script></body></html>'''.replace('LONG','这是供应商的公开产品介绍。'*35)
        if self.path=='/404':body='<h1>404 Not Found</h1>'
        raw=body.encode();self.send_response(404 if self.path=='/404' else 200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

class ReadinessTests(unittest.TestCase):
    factory=ChromeBrowser
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=cls.server.serve_forever,daemon=True).start();cls.base='http://127.0.0.1:'+str(cls.server.server_port)
        cls.folder=tempfile.TemporaryDirectory();cls.browser=cls.factory(cls.folder.name,allow_loopback_for_tests=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.folder.cleanup();cls.server.shutdown();cls.server.server_close()
    def open(self,path='/'):return self.browser.command({'type':'open','url':self.base+path})['snapshot']
    def test_notice_without_form_opens_and_waits_for_delayed_contact(self):
        s=self.open();ref=next(c['ref'] for c in s['controls'] if c.get('aria')=='通知');s=self.browser.command({'type':'click','ref':ref})['snapshot'];self.assertIn('sales@fixture.example',observed_text(s))
    def test_long_initial_page_waits_for_loaded_contact(self):
        self.assertIn('slow@fixture.example',observed_text(self.open('/slow')))
    def test_true_submit_and_destructive_link_remain_blocked(self):
        s=self.open()
        for label in ['提交','删除']:
            ref=next(c['ref'] for c in s['controls'] if c.get('label')==label)
            with self.assertRaisesRegex(ValueError,'只读'):self.browser.command({'type':'click','ref':ref})
    def test_public_login_link_is_navigation_without_submitting(self):
        s=self.open();ref=next(c['ref'] for c in s['controls'] if c.get('label')=='登录');s=self.browser.command({'type':'click','ref':ref})['snapshot'];self.assertTrue(s['url'].endswith('/login'))
    def test_scrolling_reports_progress_and_reaches_lazy_contact(self):
        s=self.open('/scroll')
        for i in range(4):s=self.browser.command({'type':'scroll','direction':'down'})['snapshot']
        self.assertGreater(s.get('scroll_y',0),2100);self.assertIn('deep@fixture.example',observed_text(s))
    def test_http_404_is_not_just_weak_render(self):self.assertEqual(self.open('/404').get('http_status'),404)
    def test_infinite_spinner_returns_pending_within_command_budget(self):
        started=time.monotonic();s=self.open('/spinner');self.assertTrue(s.get('loading_pending'));self.assertLess(time.monotonic()-started,25)

if __name__=='__main__':
    if '--moli' in sys.argv:sys.argv.remove('--moli');ReadinessTests.factory=Browser
    unittest.main(verbosity=2)
