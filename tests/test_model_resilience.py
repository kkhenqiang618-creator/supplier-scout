import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from model_service import ModelService,ModelConfigError,ModelTransientError

def reply(content):
    class Response(io.BytesIO):
        def __enter__(self):return self
        def __exit__(self,*args):self.close()
    return Response(json.dumps({'choices':[{'message':{'content':content}}]}).encode())
def http(code,body=b'{}',headers=None):return urllib.error.HTTPError('https://model.example/v1/chat/completions',code,'error',headers or {},io.BytesIO(body))

class ModelRetryTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.service=ModelService(os.path.join(self.folder.name,'model.json'))
        self.service.save({'base_url':'https://model.example/v1','model':'fixture','api_key':'secret-key'})
    def tearDown(self):self.folder.cleanup()
    def test_transient_503_and_timeout_are_retried_then_succeed(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=[http(503,b'{"error":{"message":"busy secret-key"}}'),socket.timeout('timed out'),reply('OK')]) as opened,patch('model_service.time.sleep') as slept:
            self.assertEqual(self.service.complete([]),'OK')
        self.assertEqual(opened.call_count,3);self.assertEqual(slept.call_count,2)
        self.assertLessEqual(sum(c.args[0] for c in slept.call_args_list),40)
    def test_retry_after_is_honoured_and_capped(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=[http(429,headers={'Retry-After':'120'}),reply('OK')]),patch('model_service.time.sleep') as slept:
            self.assertEqual(self.service.complete([]),'OK')
        self.assertEqual(slept.call_args.args[0],30)
    def test_config_401_is_not_retried(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=[http(401,b'{"error":{"message":"bad secret-key"}}')]) as opened,patch('model_service.time.sleep') as slept:
            with self.assertRaisesRegex(ModelConfigError,'API Key') as raised:self.service.complete([])
        self.assertEqual(opened.call_count,1);slept.assert_not_called();self.assertNotIn('secret-key',str(raised.exception))
    def test_no_available_accounts_is_config_and_exhausted_transient_is_transient(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=[http(503,b'{"error":{"message":"No available accounts"}}')]),patch('model_service.time.sleep'):
            with self.assertRaises(ModelConfigError):self.service.complete([])
        with patch('urllib.request.OpenerDirector.open',side_effect=urllib.error.URLError('reset')) as opened,patch('model_service.time.sleep'):
            with self.assertRaises(ModelTransientError):self.service.complete([])
        self.assertEqual(opened.call_count,3)
    def test_empty_body_retried_once_then_config_error(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=lambda *a,**k:reply('')) as opened,patch('model_service.time.sleep'):
            with self.assertRaisesRegex(ModelConfigError,'空正文'):self.service.complete([])
        self.assertEqual(opened.call_count,2)
        with patch('urllib.request.OpenerDirector.open',side_effect=[reply(''),reply('OK')]),patch('model_service.time.sleep'):self.assertEqual(self.service.complete([]),'OK')
    def test_context_length_400_ends_site_without_retry(self):
        for code,body in [(400,b'{"error":{"message":"This model\'s maximum context length is 32768 tokens"}}'),(422,b'{"error":{"message":"content_policy_violation"}}')]:
            with patch('urllib.request.OpenerDirector.open',side_effect=[http(code,body)]) as opened,patch('model_service.time.sleep'):
                with self.assertRaises(ModelTransientError) as raised:self.service.complete([])
            self.assertFalse(raised.exception.retryable);self.assertEqual(opened.call_count,1)
        with patch('urllib.request.OpenerDirector.open',side_effect=[http(400,b'{"error":{"message":"invalid model"}}')]):
            with self.assertRaises(ModelConfigError):self.service.complete([])
    def test_connection_test_does_not_retry(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=socket.timeout('timed out')) as opened,patch('model_service.time.sleep') as slept:
            with self.assertRaises(ModelTransientError):self.service.test()
        self.assertEqual(opened.call_count,2);slept.assert_not_called()  # /models probe + one chat attempt.

class AgentResilienceTests(unittest.TestCase):
    def run_site(self,answers,snapshot=None,capture=None,commands=None,scope='contacts'):
        from browser_agent import run_agent
        snapshot=snapshot or {'url':'https://example.com/','text':'商务邮箱 sales@example.com','controls':[],'images':[]}
        class Browser:
            def __init__(self,folder):pass
            def command(self,action):
                if commands is not None:commands.append(action)
                return {'snapshot':snapshot}
            def close(self):pass
        answers=list(answers)
        def answer(messages):
            if capture is not None:capture.append(messages[-1]['content'])
            item=answers.pop(0)
            if isinstance(item,Exception):raise item
            return item
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(os.path.join(folder,'model.json'));service.save({'base_url':'https://model.example/v1','model':'fixture'});data=service.read();data['tested']=True;service._write(data)
            with patch('browser_agent.validate_url'),patch('engine.fetch',return_value=('User-agent: *\nAllow: /','https://example.com/robots.txt')),patch.object(service,'complete',side_effect=answer):
                return run_agent('https://example.com',scope,service,[{'model':'m'}] if scope=='all' else [],browser_factory=Browser,max_steps=4)
    def test_transient_exhausted_ends_site_with_partial(self):
        from browser_agent import ModelTransientSiteError,AgentServiceError
        record={'type':'email','value':'sales@example.com','source_url':'https://example.com/','evidence':'商务邮箱 sales@example.com'}
        with self.assertRaises(ModelTransientSiteError) as raised:self.run_site([json.dumps({'contacts':[record],'action':{'type':'wait'}}),ModelTransientError('模型服务调用失败：HTTP 503')])
        self.assertNotIsInstance(raised.exception,AgentServiceError)
        self.assertEqual(raised.exception.partial['contacts'],{'email':['sales@example.com']});self.assertTrue(raised.exception.partial['model_transient'])
        self.assertIn('模型服务暂时不可用（已重试）',raised.exception.partial['contact_summary'])
    def test_transient_partial_with_records_is_incomplete(self):
        from browser_agent import ModelTransientSiteError
        record={'type':'email','value':'sales@example.com','source_url':'https://example.com/','evidence':'商务邮箱 sales@example.com'}
        with self.assertRaises(ModelTransientSiteError) as raised:self.run_site([json.dumps({'contacts':[record],'action':{'type':'wait'}}),ModelTransientError('模型服务调用失败：HTTP 503')])
        self.assertEqual(raised.exception.partial['contact_status'],'incomplete')
    def test_falsy_non_list_offers_do_not_crash(self):
        result=self.run_site([json.dumps({'contacts':False,'offers':False,'action':{'type':'finish'}})],scope='all')
        self.assertEqual(result['contact_status'],'incomplete');self.assertNotIn('访问受阻',result['contact_summary'])
    def test_truncated_href_open_resolves_by_prefix(self):
        long='https://example.com/contact?'+'t'*400
        snapshot={'url':'https://example.com/','text':'联系我们','controls':[{'ref':'c1','label':'联系','href':long}],'images':[]}
        prompts=[];opened=[]
        result=self.run_site([json.dumps({'action':{'type':'open','url':long[:300]+'…'}}),json.dumps({'action':{'type':'finish'}})],snapshot,prompts,opened)
        self.assertIn('…',prompts[0]);self.assertEqual(opened[-1],{'type':'open','url':long});self.assertNotEqual(result['contact_status'],'blocked')
    def test_known_contacts_evidence_clipped_in_prompt(self):
        from browser_agent import prompt_context
        context={'page':{'url':'https://example.com/'+'u'*400,'controls':[],'images':[]},'history':[],'known_contacts':[{'type':'email','value':'a@example.com','source_url':'https://example.com/','evidence':'证'*5000,'additional_sources':[{'evidence':'x'*5000}]*12}],'known_offers':[{'model':'m','evidence':'y'*5000}]}
        prompt=json.dumps(prompt_context(context),ensure_ascii=False)
        self.assertLess(len(prompt),2000);self.assertNotIn('additional_sources',prompt);self.assertIn('u'*400,prompt)
        self.assertEqual(len(context['known_contacts'][0]['evidence']),5000)
    def test_config_error_stays_fatal(self):
        from browser_agent import AgentServiceError
        with self.assertRaises(AgentServiceError):self.run_site([ModelConfigError('模型服务调用失败：HTTP 401')])
    def test_non_dict_decision_is_skipped(self):
        result=self.run_site(['[1,2]','"text"',json.dumps({'action':'finish'}),json.dumps({'action':{'type':'finish'}})])
        self.assertEqual(result['warnings'].count('模型本轮未返回有效JSON'),1);self.assertEqual(result['steps'][-1]['action'],'finish')
    def test_prompt_budget_with_huge_data_uri(self):
        uri='data:image/png;base64,'+'A'*100000
        snapshot={'url':'https://example.com/','text':'商务邮箱 sales@example.com '+'正文'*20000,'tail':'尾部'*5000,'footer':'页尾','controls':[{'ref':'c'+str(i),'label':'链接'*50,'href':uri if i==0 else '/p/'+'x'*1000,'context':'上下文'*80} for i in range(400)],'images':[{'ref':'i'+str(i),'src':uri,'alt':'图','context':'x'*250} for i in range(160)]}
        record={'type':'email','value':'sales@example.com','source_url':'https://example.com/','evidence':'商务邮箱 sales@example.com'}
        prompts=[];result=self.run_site([json.dumps({'contacts':[record],'action':{'type':'finish'}})],snapshot,prompts)
        self.assertLessEqual(len(prompts[0]),40000);self.assertNotIn('AAAA',prompts[0]);self.assertIn('"task"',prompts[0])
        self.assertEqual(result['contacts'],{'email':['sales@example.com']})
        self.assertGreater(result['sources'][0]['text_length'],40000)

class CollectSiteContractTests(unittest.TestCase):
    def test_worker_transient_marker_becomes_model_transient_site_error(self):
        import threading
        import server
        line=json.dumps({'kind':'error','message':'模型服务暂时不可用','fatal':False,'model_transient':True,'partial':{'contacts':{'email':['a@example.com']}}},ensure_ascii=False)
        class Process:
            pid=0;stdout=io.StringIO(line+'\n')
            def poll(self):return 0
        with patch('server.subprocess.Popen',return_value=Process()):
            with self.assertRaises(server.ModelTransientSiteError) as raised:server.collect_site('https://example.com',lambda m:None,threading.Event(),'contacts')
        self.assertNotIsInstance(raised.exception,server.AgentServiceError);self.assertEqual(raised.exception.partial['contacts'],{'email':['a@example.com']})

if __name__=='__main__':unittest.main()
