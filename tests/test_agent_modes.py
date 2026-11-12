import json
import os
import tempfile
import unittest
from pathlib import Path
from server import Store
from browser_agent import validate_contacts, choose_contact_status

class AgentModeTests(unittest.TestCase):
    def test_decoded_contact_qr_keeps_exact_payload_and_source(self):
        from browser_agent import qr_contact_records
        link='https://work.weixin.qq.com/ct/verified-example'
        snapshot={'url':'https://example.com/','images':[{'ref':'i2','src':'https://cdn.example.com/contact.png','alt':'微信二维码','context':'扫码联系我们'}],'decoded_qr':[link]}
        records=qr_contact_records(snapshot,'i2',[link])
        self.assertEqual(records[0]['type'],'wechat_link');self.assertEqual(records[0]['value'],link)
        self.assertEqual(records[0]['qr_source_url'],'https://cdn.example.com/contact.png')
        self.assertEqual(validate_contacts(records,[snapshot])[0],records)
        snapshot['images'][0].update(alt='登录二维码',context='扫码登录')
        self.assertEqual(qr_contact_records(snapshot,'i2',[link]),[])
    def test_scanned_contact_keeps_qr_image_and_marks_scan_only_links(self):
        from browser_agent import qr_contact_records
        link='https://work.weixin.qq.com/ct/wc-example'
        snapshot={'url':'https://example.com/','images':[{'ref':'i2','src':'https://cdn.example.com/contact.png','alt':'企业微信二维码','context':'扫码添加客服'}]}
        records=qr_contact_records(snapshot,'i2',[link],'data:image/png;base64,AAAA')
        self.assertEqual(records[0]['delivery'],'scan')
        self.assertEqual(records[0]['qr_image'],'data:image/png;base64,AAAA')
        telegram=qr_contact_records({'url':'https://example.com/','images':[{'ref':'i2','alt':'contact us','context':'telegram'}]},'i2',['https://t.me/example'])[0]
        self.assertEqual(telegram['delivery'],'link')
        self.assertNotIn('qr_image',telegram)
    def test_image_data_url_keeps_small_qr_and_skips_oversized_or_unknown_files(self):
        from browser_agent import image_data_url
        with tempfile.TemporaryDirectory() as folder:
            png=Path(folder)/'a.png';png.write_bytes(b'\x89PNG\r\n\x1a\n'+b'0'*100)
            self.assertTrue(image_data_url(png).startswith('data:image/png;base64,'))
            oversized=Path(folder)/'b.png';oversized.write_bytes(b'\x89PNG\r\n\x1a\n'+b'0'*(130*1024))
            self.assertEqual(image_data_url(oversized),'')
            self.assertEqual(image_data_url(oversized,limit=64*1024),'')
            self.assertTrue(image_data_url(Path(folder)/'a.png',limit=64*1024))
            html=Path(folder)/'c.png';html.write_text('<html>not an image</html>')
            self.assertEqual(image_data_url(html),'')
    def test_robots_tls_fallback_checks_redirects_and_keeps_cert_verification(self):
        import subprocess,urllib.error
        from pathlib import Path
        from unittest.mock import patch
        from browser_agent import fetch_robots
        calls=[];checked=[]
        def curl(args,**kwargs):
            calls.append(args);headers=Path(args[args.index('--dump-header')+1]);body=Path(args[args.index('--output')+1])
            if len(calls)==1:headers.write_text('HTTP/2 302\nLocation: https://other.example/robots.txt\n');return subprocess.CompletedProcess(args,0,'302','')
            body.write_text('User-agent: *\nDisallow: /private');return subprocess.CompletedProcess(args,0,'200','')
        with patch('engine.fetch',side_effect=urllib.error.URLError('TLS EOF')),patch('browser_agent.validate_url',side_effect=lambda url:checked.append(url)),patch('browser_agent.subprocess.run',side_effect=curl),patch('browser_agent.shutil.which',return_value='/usr/bin/curl'):
            body,url=fetch_robots('https://example.com/robots.txt')
        self.assertIn('Disallow: /private',body);self.assertEqual(url,'https://other.example/robots.txt')
        self.assertEqual(checked,['https://example.com/robots.txt','https://other.example/robots.txt'])
        for args in calls:self.assertFalse(any(arg in args for arg in ['--insecure','-k','-L','--location']))
    def test_contact_evidence_survives_three_rechecks(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(os.path.join(folder,'test.db'),seed=False);store.import_rows([{'url':'https://example.com'}]);sid=store.state()['suppliers'][0]['id']
            for value in ['a@example.com','b@example.com','c@example.com']:
                record={'type':'email','value':value,'source_url':'https://example.com','evidence':value,'purpose':'商务'}
                store.save_contacts(sid,{'contacts':{'email':[value]},'contact_records':[record],'contact_status':'found'})
            result=store.get(sid)['contact_result']
            self.assertEqual({r['value'] for r in result['contact_records']+result['previous_contact_records']},{'a@example.com','b@example.com','c@example.com'})
    def test_contacts_only_autonomous_loop_and_api_failure_preserve_verified_fact(self):
        from browser_agent import run_agent,AgentServiceError
        from model_service import ModelService
        from unittest.mock import patch
        snapshot={'url':'https://example.com/','text':'商务邮箱 sales@example.com','controls':[],'images':[]}
        class Browser:
            def __init__(self,folder):pass
            def command(self,action):return {'snapshot':snapshot}
            def close(self):pass
        record={'type':'email','value':'sales@example.com','source_url':snapshot['url'],'evidence':snapshot['text']}
        contexts=[]
        def answer(messages):
            contexts.append(json.loads(messages[-1]['content']))
            if len(contexts)==2:raise ValueError('HTTP 503 provider down')
            return json.dumps({'contacts':[record],'action':{'type':'wait'},'offers':[{'model':'forbidden'}]})
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(os.path.join(folder,'model.json'));service.save({'base_url':'https://model.example/v1','model':'fixture'});data=service.read();data['tested']=True;service._write(data)
            with patch('browser_agent.validate_url'),patch('engine.fetch',return_value=('User-agent: *\nAllow: /','https://example.com/robots.txt')),patch.object(service,'complete',side_effect=answer):
                with self.assertRaises(AgentServiceError) as raised:run_agent('https://example.com','contacts',service,[{'model':'should-not-send'}],browser_factory=Browser)
            self.assertEqual(contexts[0]['rules'],[])
            self.assertEqual(raised.exception.partial['contacts'],{'email':['sales@example.com']})
            self.assertEqual(raised.exception.partial['offers'],[])
    def test_contacts_only_preserves_quotes_review_and_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(os.path.join(folder,'test.db'),seed=False)
            store.import_rows([{'url':'https://example.com'}]);sid=store.state()['suppliers'][0]['id']
            store.save_result(sid,{'contacts':{},'offers':[{'model':'deepseek-v3','ratio':.35}],'sources':[{'url':'https://example.com/pricing','excerpt':'价格证据'}]})
            store.review(sid,'skipped','人工价格结论')
            before=store.get(sid);history=store.state()['history']
            store.save_contacts(sid,{'contacts':{'email':['sales@example.com']},'contact_records':[{'type':'email','value':'sales@example.com','source_url':'https://example.com/contact','evidence':'商务 sales@example.com'}],'contact_status':'found','sources':[{'url':'https://example.com/contact','excerpt':'商务 sales@example.com'}],'steps':[{'reason':'发现联系页'}],'warnings':[]})
            after=store.get(sid)
            for field in ['offers','status','reason','notes','checked_at']:self.assertEqual(after[field],before[field])
            self.assertEqual(store.state()['history'],history)
            self.assertEqual(after['contacts']['email'],['sales@example.com'])
            self.assertEqual(after['contact_status'],'found')
            self.assertEqual(len(after['sources']),2)
    def test_missing_api_does_not_start_real_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(os.path.join(folder,'test.db'),seed=False);store.import_rows([{'url':'https://example.com'}])
            with self.assertRaisesRegex(ValueError,'API'):store.start_job('real',scope='contacts')
            self.assertFalse(store.job['running'])
    def test_contact_evidence_and_source_must_be_observed(self):
        observations=[{'url':'https://example.com','text':'商务邮箱 sales@example.com','controls':[],'images':[]}]
        records=[{'type':'email','value':'sales@example.com','source_url':'https://example.com','evidence':'商务邮箱 sales@example.com'}, {'type':'email','value':'madeup@example.com','source_url':'https://example.com','evidence':'商务邮箱 sales@example.com'}, {'type':'email','value':'sales@example.com','source_url':'https://other.com','evidence':'商务邮箱 sales@example.com'}]
        valid,rejected=validate_contacts(records,observations)
        self.assertEqual(len(valid),1);self.assertEqual(len(rejected),2)
    def test_missing_contact_is_not_a_claim_of_absence(self):
        self.assertEqual(choose_contact_status([],False,1),'incomplete')
        self.assertEqual(choose_contact_status([],True,4),'blocked')
        self.assertEqual(choose_contact_status([{'type':'email'}],False,1),'found')

if __name__=='__main__':unittest.main()
