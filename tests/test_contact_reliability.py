import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import browser_agent
from model_service import ModelService
from server import Store

class ReliabilityTests(unittest.TestCase):
    def run_agent(self,snapshot,decision,commands=None,chrome_snapshot=None):
        commands=[] if commands is None else commands
        class FakeBrowser:
            label='Fixture'
            def __init__(self,folder):pass
            def command(self,action):
                commands.append(action)
                item=snapshot.pop(0) if isinstance(snapshot,list) and len(snapshot)>1 else snapshot[0] if isinstance(snapshot,list) else snapshot
                if isinstance(item,Exception):raise item
                return {'snapshot':item}
            def close(self):pass
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(Path(folder)/'model.json');service.save({'base_url':'https://model.example/v1','model':'fixture'});config=service.read();config['tested']=True;service._write(config)
            with patch('browser_agent.validate_url'),patch('browser_agent.fetch_robots',return_value='User-agent: *\nAllow: /'),patch.object(service,'complete',side_effect=lambda messages:json.dumps(decision.pop(0) if isinstance(decision,list) and len(decision)>1 else decision[0] if isinstance(decision,list) else decision)) as complete:
                if chrome_snapshot is None:result=browser_agent.run_agent('https://fixture.example/','contacts',service,[],max_steps=8,browser_factory=FakeBrowser)
                else:
                    class Chrome(FakeBrowser):
                        label='Chrome'
                        def command(self,action):commands.append(dict(action,browser='Chrome'));return {'snapshot':chrome_snapshot}
                    result=browser_agent.run_site('https://fixture.example/','contacts',service,[],moli_factory=FakeBrowser,chrome_factory=Chrome,chrome_ready=lambda:True)
                return result,commands,complete.call_count
    def test_unchanged_same_control_not_clicked_repeatedly(self):
        s={'url':'https://fixture.example/','text':'公开正文'*100,'controls':[{'ref':'c0','label':'公告','aria':'通知'}],'images':[]}
        result,commands,_=self.run_agent(s,{'action':{'type':'click','ref':'c0'}})
        self.assertLessEqual(sum(c['type']=='click' for c in commands),2)
        self.assertTrue(any('重复' in w for w in result['warnings']))
    def test_challenge_waits_do_not_spend_model_steps(self):
        s={'url':'https://fixture.example/','text':'Cloudflare 安全验证 等待源站响应','controls':[],'images':[],'challenge':True}
        result,commands,calls=self.run_agent(s,{'action':{'type':'wait'}})
        self.assertEqual(calls,0);self.assertLessEqual(sum(c['type']=='wait' for c in commands),2)
        self.assertIn('challenge',[d['code'] for d in result.get('diagnostics',[])])
    def test_pending_load_waits_inside_observation_not_model_decisions(self):
        s={'url':'https://fixture.example/','text':'Loading...','controls':[],'images':[],'loading_pending':True}
        result,commands,calls=self.run_agent(s,{'action':{'type':'wait'}})
        self.assertEqual(calls,0);self.assertLessEqual(sum(c['type']=='wait' for c in commands),2)
        self.assertIn('loading_timeout',[d['code'] for d in result.get('diagnostics',[])])
    def test_diagnostics_survive_save_without_overwriting_contacts_or_quotes(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'data.db',seed=False)
            store.import_rows([{'name':'1','url':'https://fixture.example/'}])
            row=store.state()['suppliers'][0]
            store.save_contacts(row['id'],{'contacts':{},'contact_status':'blocked','warnings':['域名无法解析'],'steps':[],'sources':[],'scope':'contacts'})
            got=store.get(row['id']);self.assertEqual(got['contact_result']['diagnostics'][0]['code'],'dns');self.assertEqual(got['offers'],row['offers'])
    def test_scroll_progress_can_reach_below_second_screen(self):
        views=[{'url':'https://fixture.example/','text':'长页正文'*100,'controls':[],'images':[],'scroll_y':700*i,'scroll_height':12000} for i in range(9)]
        result,commands,calls=self.run_agent(views,{'action':{'type':'scroll','direction':'down'}})
        self.assertEqual(sum(c['type']=='scroll' for c in commands),8)

    def test_distinct_same_label_controls_are_all_visited(self):
        s={'url':'https://fixture.example/','text':'正文'*100,'controls':[{'ref':'c'+str(i),'label':'联系','context':'渠道'+str(i)} for i in range(3)],'images':[]}
        answers=[{'action':{'type':'click','ref':'c'+str(i)}} for i in range(3)]+[{'action':{'type':'finish'}}]
        result,commands,calls=self.run_agent(s,answers)
        self.assertEqual([c['ref'] for c in commands if c['type']=='click'],['c0','c1','c2'])

    def test_stale_control_allows_next_alternative_instead_of_ending_site(self):
        s={'url':'https://fixture.example/','text':'正文'*100,'controls':[{'ref':'c0','label':'联系'}],'images':[]}
        result,commands,calls=self.run_agent(s,[{'action':{'type':'click','ref':'c99'}},{'action':{'type':'click','ref':'c0'}},{'action':{'type':'finish'}}])
        self.assertTrue(any(c.get('ref')=='c0' for c in commands));self.assertFalse(any('NoneType' in w for w in result['warnings']))

    def test_auxiliary_spinner_does_not_hide_other_contact_navigation(self):
        s={'url':'https://fixture.example/','text':'公开正文'*100,'controls':[{'ref':'c0','label':'关于','href':'/about'}],'images':[],'loading_pending':True}
        result,commands,calls=self.run_agent(s,{'action':{'type':'click','ref':'c0'}})
        self.assertGreaterEqual(calls,1);self.assertTrue(any(c['type']=='click' for c in commands))
        self.assertLessEqual(sum(c['type']=='wait' for c in commands),2)

    def test_transient_initial_navigation_retried_once_without_model_step(self):
        s={'url':'https://fixture.example/','text':'公开正文'*100,'controls':[],'images':[]}
        result,commands,calls=self.run_agent([ValueError('page.goto: net::ERR_TIMED_OUT'),s],{'action':{'type':'finish'}})
        self.assertEqual(sum(c['type']=='open' for c in commands),2);self.assertEqual(calls,1)
        self.assertEqual(result['contact_status'],'incomplete')

    def test_error_reasons_have_specific_labels_and_failure_urls(self):
        from collection_diagnostics import collection_diagnostics
        for warning,code in [('域名无法解析','dns'),('ERR_CERT_AUTHORITY_INVALID','tls'),('ERR_TIMED_OUT','timeout'),('robots.txt 不允许读取该页面','robots'),('该操作超出只读采集范围','interaction'),('Execution context was destroyed','navigation'),('达到本次单站动作预算','budget')]:
            with self.subTest(code=code):
                issues=collection_diagnostics({'warnings':[warning],'steps':[{'url':'https://fixture.example/docs','browser':'Chrome'}]})
                self.assertIn(code,[d['code'] for d in issues]);self.assertEqual(issues[0]['url'],'https://fixture.example/docs')
        issues=collection_diagnostics({},pages=[{'url':'https://fixture.example/','http_status':404}])
        self.assertEqual(issues[0]['code'],'http');self.assertEqual(issues[0]['http_status'],404)

    def test_http_diagnostic_keeps_failed_page_after_later_success(self):
        from collection_diagnostics import collection_diagnostics
        issues=collection_diagnostics({'warnings':['网站返回 HTTP 404：https://fixture.example/missing'],'steps':[{'url':'https://fixture.example/working'}]},pages=[{'url':'https://fixture.example/missing','http_status':404},{'url':'https://fixture.example/working','http_status':200}])
        self.assertEqual(len(issues),1);self.assertEqual(issues[0]['url'],'https://fixture.example/missing');self.assertEqual(issues[0]['http_status'],404);self.assertFalse(issues[0]['retryable'])

    def test_loading_diagnostic_uses_current_page_instead_of_previous_step(self):
        from collection_diagnostics import collection_diagnostics
        issues=collection_diagnostics({'warnings':['页面加载等待已达上限，可稍后继续检查'],'steps':[{'url':'https://fixture.example/old'}]},pages=[{'url':'https://fixture.example/new','loading_wait_exhausted':True}])
        self.assertEqual(len(issues),1);self.assertEqual(issues[0]['url'],'https://fixture.example/new')

    def test_loading_exhaustion_still_uses_chrome_fallback(self):
        moli={'url':'https://fixture.example/','text':'Loading...','controls':[],'images':[],'loading_pending':True,'render_weak':True}
        chrome={'url':'https://fixture.example/','text':'商务邮箱 sales@fixture.example','controls':[],'images':[]}
        answer={'contacts':[{'type':'email','value':'sales@fixture.example','source_url':'https://fixture.example/','evidence':'商务邮箱 sales@fixture.example'}],'action':{'type':'finish'}}
        result,commands,calls=self.run_agent(moli,answer,chrome_snapshot=chrome)
        self.assertEqual(result['contact_status'],'found');self.assertEqual(result['contacts'],{'email':['sales@fixture.example']})
        self.assertTrue(any(c.get('browser')=='Chrome' for c in commands));self.assertEqual(calls,1)
        self.assertLessEqual(sum(c['type']=='wait' and c.get('browser')!='Chrome' for c in commands),2)

    def test_author_and_closed_site_contacts_still_count_as_found(self):
        for kind in ['other','email']:
            self.assertEqual(browser_agent.choose_contact_status([{'type':kind,'value':'author or refund'}],False,1),'found')

if __name__=='__main__':unittest.main()
