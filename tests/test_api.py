import base64
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from server import create_server

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.app=create_server(0,os.path.join(self.tmp.name,'test.db'),seed=False)
        self.thread=threading.Thread(target=self.app.serve_forever,daemon=True); self.thread.start(); self.base='http://127.0.0.1:'+str(self.app.server_port)
    def tearDown(self): self.app.shutdown(); self.app.server_close(); self.thread.join(); self.tmp.cleanup()
    def call(self,path,data=None,origin=None):
        headers={'Content-Type':'application/json'}
        if origin: headers['Origin']=origin
        req=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=10) as res: return res.status,res.headers,res.read()
        except urllib.error.HTTPError as e: return e.code,e.headers,e.read()
    def test_import_review_reply_draft_export_pipeline(self):
        self.assertEqual(self.call('/api/import',{'rows':[{'name':'测试供应商','url':'https://example.com'}]})[0],200)
        sid=json.loads(self.call('/api/state')[2])['suppliers'][0]['id']
        reply={'id':sid,'text':'DeepSeek-V3 3.5折\n销售邮箱 sales@example.com'}
        result=self.call('/api/reply',reply); self.assertEqual(result[0],200); self.assertEqual(json.loads(result[2])['offers'],1)
        row=json.loads(self.call('/api/state')[2])['suppliers'][0]; self.assertEqual(row['status'],'qualified')
        self.assertEqual(self.call('/api/review',{'id':sid,'status':'review','note':'需进一步核实版本'})[0],200)
        self.assertEqual(self.call('/api/drafts',{'id':sid})[0],200)
        draft=self.call('/api/draft?id='+str(sid)); self.assertEqual(draft[0],200); self.assertIn(b'X-Unsent: 1',draft[2])
        csv=self.call('/api/export?mode=real'); self.assertEqual(csv[0],200); self.assertIn('测试供应商',csv[2].decode('utf-8-sig')); self.assertIn('0.35',csv[2].decode('utf-8-sig'))
    def test_excel_upload_and_single_address(self):
        from test_importers import workbook_fixture
        payload={'filename':'名单.xlsx','file_base64':base64.b64encode(workbook_fixture()).decode()}
        preview=self.call('/api/import-preview',payload)
        self.assertEqual(preview[0],200)
        self.assertEqual(json.loads(preview[2])['valid_count'],2)
        result=self.call('/api/import',payload)
        self.assertEqual(result[0],200)
        self.assertEqual(json.loads(result[2])['added'],2)
        rows=self.app.store.state()['suppliers']
        self.assertEqual(rows[0]['name'],'007')
        self.assertEqual(rows[1]['url'],'https://example.org/path?a=1&b=2')
        single=self.call('/api/import',{'text':'api.holysheep.ai'})
        self.assertEqual(single[0],200)
        self.assertEqual(json.loads(single[2])['added'],1)
        partial=self.call('/api/import',{'text':'example.net\n这不是网址'})
        self.assertEqual(partial[0],200)
        self.assertEqual(json.loads(partial[2])['added'],1)
        self.assertEqual(len(json.loads(partial[2])['rejected']),1)

    def test_stop_endpoint_is_idempotent_when_idle(self):
        for _ in range(2):
            status,headers,body=self.call('/api/stop',{})
            self.assertEqual(status,200)
            self.assertFalse(json.loads(body)['running'])

    def test_cross_site_and_malformed_requests_rejected(self):
        self.assertEqual(self.call('/api/run',{'mode':'demo'},origin='https://malicious.example')[0],403)
        self.assertEqual(self.call('/api/import',{'rows':[]})[0],400)
        self.assertEqual(self.call('/api/review',{'id':999,'status':'qualified','note':'核对'})[0],400)
    def test_private_network_target_marked_error_without_hanging_batch(self):
        self.app.store.model_service.save({'base_url':'https://example.com/v1','model':'fixture-model','api_key':'fixture-key'})
        self.call('/api/import',{'rows':[{'url':'http://127.0.0.1'}]})
        self.assertEqual(self.call('/api/run',{'mode':'real'})[0],200)
        deadline=time.monotonic()+3
        while self.app.store.job['running'] and time.monotonic()<deadline: time.sleep(.01)
        row=self.app.store.state()['suppliers'][0]
        self.assertEqual(row['status'],'error'); self.assertIn('禁止访问',row['reason']); self.assertFalse(self.app.store.job['running'])
    def test_live_crawl_contract_with_local_html_fixture(self):
        # Network transport is replaced at its boundary; real parser, worker,
        # rules, persistence and HTTP handlers still run against fixture HTML.
        from unittest.mock import patch
        from engine import extract_page
        self.app.store.model_service.save({'base_url':'https://example.com/v1','model':'fixture-model','api_key':'fixture-key'})
        html='<p>DeepSeek-V3 长期3.5折</p><a href="mailto:sales@example.com">联系销售</a>'
        page=extract_page(html,'https://example.com/pricing')
        fixture={'contacts':page['contacts'],'offers':page['offers'],'sources':[{'url':page['source'],'excerpt':page['text']}],'warnings':[]}
        self.call('/api/import',{'rows':[{'url':'https://example.com'}]})
        with patch('server.collect_site',return_value=fixture):
            self.assertEqual(self.call('/api/run',{'mode':'real'})[0],200)
            deadline=time.monotonic()+3
            while self.app.store.job['running'] and time.monotonic()<deadline: time.sleep(.01)
        row=self.app.store.state()['suppliers'][0]; self.assertEqual(row['status'],'qualified'); self.assertEqual(row['offers'][0]['ratio'],.35); self.assertEqual(row['contacts']['email'],['sales@example.com'])

if __name__=='__main__': unittest.main()
