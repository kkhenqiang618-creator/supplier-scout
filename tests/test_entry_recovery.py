import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import browser_agent
from model_service import ModelService
from server import Store

OLD='https://old.example/'
NEW='https://new.example/'
NOTICE={'url':OLD,'text':'星河平台 域名升级通知：旧域名不再使用，请访问新域名。','controls':[{'ref':'c0','label':'立即访问新域名','href':NEW,'context':'新域名（推荐使用）'}],'images':[],'http_status':200}
DEST={'url':NEW,'text':'星河平台官网，售后QQ群 https://qm.qq.com/q/fixture，欢迎联系。'*4,'controls':[],'images':[],'http_status':200}

class MigrationParsingTests(unittest.TestCase):
    def candidates(self,s):
        from entry_recovery import migration_candidates
        return migration_candidates(s)
    def test_unique_explicit_migration_has_observed_source_and_evidence(self):
        got=self.candidates(NOTICE);self.assertEqual(len(got),1);self.assertEqual(got[0]['url'],NEW);self.assertEqual(got[0]['source_url'],OLD);self.assertIn('域名升级',got[0]['evidence'])
    def test_generic_new_domain_text_does_not_authorize_advertisement(self):
        s=dict(NOTICE,text='支持绑定新域名。赞助商广告：立即访问',controls=[{'label':'立即访问','href':'https://advertiser.example/','context':'赞助商广告'}])
        self.assertEqual(self.candidates(s),[])
    def test_http_200_soft_error_is_not_a_verified_website(self):
        from entry_recovery import verified_destination
        for title in ['', '星河平台']:
            with self.subTest(title=title):self.assertFalse(verified_destination(dict(DEST,title=title,text='抱歉，网站服务暂时不可用，请稍后重试。'*5,http_status=200),NEW))

    def test_generic_documentation_link_is_not_a_migration(self):
        self.assertEqual(self.candidates(dict(NOTICE,text='模型服务使用文档',controls=[{'label':'文档','href':NEW}])),[])
    def test_ambiguous_new_domains_are_not_silently_chosen(self):
        s=dict(NOTICE,controls=NOTICE['controls']+[{'ref':'c1','label':'新域名备用','href':'https://second.example/'}]);self.assertEqual(len(self.candidates(s)),2)
    def test_downgrade_private_credentials_and_non_web_targets_are_rejected(self):
        for target in ['http://new.example/','https://127.0.0.1/','https://10.0.0.1/','https://name:pass@new.example/','javascript:alert(1)','https://localhost/','https://[::1]/']:
            with self.subTest(target=target):self.assertEqual(self.candidates(dict(NOTICE,controls=[dict(NOTICE['controls'][0],href=target)])),[])
    def test_meta_refresh_is_evidence_only_on_explicit_migration_notice(self):
        self.assertEqual(self.candidates(dict(NOTICE,controls=[],redirect_hints=[{'url':NEW,'kind':'meta_refresh'}]))[0]['url'],NEW)
        self.assertEqual(self.candidates(dict(NOTICE,text='广告推荐',controls=[],redirect_hints=[{'url':NEW,'kind':'meta_refresh'}])),[])

class MigrationAgentTests(unittest.TestCase):
    def run_fixture(self,views,answers=None):
        calls=[];commands=[]
        class Fixture:
            label='Fixture'
            def __init__(self,folder):pass
            def command(self,action):
                commands.append(action);value=views.get(action.get('url'),DEST)
                if isinstance(value,Exception):raise value
                return {'snapshot':dict(value)}
            def close(self):pass
        with tempfile.TemporaryDirectory() as folder:
            service=ModelService(Path(folder)/'model.json');service.save({'base_url':'https://model.example/v1','model':'fixture'});d=service.read();d['tested']=True;service._write(d)
            def complete(messages):calls.append(json.loads(messages[1]['content']));return json.dumps(answers.pop(0) if answers else {'action':{'type':'finish'}})
            with patch('browser_agent.validate_url'),patch('browser_agent.fetch_robots',return_value='User-agent: *\nAllow: /'),patch.object(service,'complete',side_effect=complete):
                result=browser_agent.run_agent(OLD,'contacts',service,[],browser_factory=Fixture,max_steps=4)
        return result,calls,commands
    def test_notice_followed_before_first_model_step_and_history_preserved(self):
        result,calls,commands=self.run_fixture({OLD:NOTICE,NEW:DEST})
        self.assertEqual([c['url'] for c in commands if c['type']=='open'],[OLD,NEW]);self.assertEqual(calls[0]['page']['url'],NEW)
        recovery=result.get('entry_recovery',{});self.assertEqual(recovery.get('to'),NEW);self.assertEqual(recovery.get('from'),OLD);self.assertTrue(recovery.get('verified'));self.assertIn(OLD,[s['url'] for s in result['sources']])
    def test_error_destination_not_marked_verified(self):
        result,_,_=self.run_fixture({OLD:NOTICE,NEW:dict(DEST,http_status=404,text='404 Not Found',controls=[])})
        self.assertFalse(result.get('entry_recovery',{}).get('verified',False))
    def test_migration_is_verified_after_program_side_loading_wait(self):
        pending=dict(DEST,text='Loading...',loading_pending=True,controls=[])
        result,_,commands=self.run_fixture({OLD:NOTICE,NEW:pending})
        self.assertTrue(result.get('entry_recovery',{}).get('verified'));self.assertEqual(result['entry_recovery']['to'],NEW)
        self.assertTrue(any(c['type']=='wait' for c in commands))

    def test_third_party_contact_page_cannot_rewrite_supplier_entry(self):
        third='https://thirdparty.example/'
        home=dict(DEST,url=OLD,controls=[{'ref':'c0','label':'项目作者联系方式','href':third}])
        third_notice=dict(NOTICE,url=third)
        result,_,_=self.run_fixture({OLD:home,third:third_notice,NEW:DEST},answers=[{'action':{'type':'open','url':third}},{'action':{'type':'finish'}}])
        self.assertFalse(result.get('entry_recovery',{}).get('verified',False))

    def test_cycle_is_bounded_without_unlimited_navigation(self):
        back=dict(NOTICE,url=NEW,controls=[dict(NOTICE['controls'][0],href=OLD)])
        result,calls,commands=self.run_fixture({OLD:NOTICE,NEW:back});self.assertLessEqual(len(commands),3);self.assertFalse(result.get('entry_recovery',{}).get('verified',False))

class EntryStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup);self.store=Store(Path(self.folder.name)/'test.db',seed=False);self.store.import_rows([{'name':'7','url':OLD}]);self.sid=self.store.state()['suppliers'][0]['id'];self.original=self.store.get(self.sid)['url']
    def change(self,**kwargs):
        with patch('entry_recovery.validate_url'):
            return self.store.update_entry(self.sid,NEW,'官网升级公告',OLD,OLD,**kwargs)
    def test_manual_change_keeps_business_data_and_records_old_url(self):
        self.store.save_contacts(self.sid,{'contacts':{'email':['legacy@example.com']},'contact_records':[],'contact_status':'found'});before=self.store.get(self.sid);self.change();after=self.store.get(self.sid)
        self.assertEqual(after['url'],NEW)
        for field in ['contacts','offers','status','reason','checked_at','contact_checked_at','notes']:self.assertEqual(before[field],after[field])
        self.assertEqual(after['contact_result']['entry_history'][0]['from'],self.original)
    def test_history_survives_a_subsequent_failed_contact_attempt(self):
        self.change();self.store.save_contacts(self.sid,{'contacts':{},'contact_status':'blocked','warnings':['访问超时']});self.assertEqual(self.store.get(self.sid)['contact_result']['entry_history'][0]['to'],NEW)
    def test_stale_expected_address_rejected_without_mutating(self):
        with patch('entry_recovery.validate_url'),self.assertRaises(ValueError):self.store.update_entry(self.sid,NEW,'用户核验','','https://different.example/')
        self.assertEqual(self.store.get(self.sid)['url'],self.original)
    def test_duplicate_target_rejected_without_merging_suppliers(self):
        self.store.import_rows([{'name':'8','url':NEW}])
        with self.assertRaises(ValueError):self.change()
        self.assertEqual(len(self.store.state()['suppliers']),2);self.assertEqual(self.store.get(self.sid)['url'],self.original)
    def test_active_job_prevents_manual_change(self):
        self.store.job['running']=True
        with self.assertRaises(ValueError):self.change()
        self.assertEqual(self.store.get(self.sid)['url'],self.original)
    def test_failed_result_save_does_not_commit_only_the_new_url(self):
        recovery={'from':OLD,'to':NEW,'source_url':OLD,'evidence':'官网域名升级通知明确指向新域名','verified':True}
        with patch('entry_recovery.validate_url'),self.assertRaises(TypeError):
            self.store.save_contacts(self.sid,{'contacts':{},'contact_status':'found','warnings':[set(['not-json'])],'entry_recovery':recovery})
        self.assertEqual(self.store.get(self.sid)['url'],self.original);self.assertEqual(self.store.get(self.sid)['contact_result'].get('entry_history',[]),[])

    def test_verified_recovery_saved_atomically_and_history_kept(self):
        recovery={'from':OLD,'to':NEW,'source_url':OLD,'evidence':'官网域名升级通知明确指向新域名','verified':True}
        with patch('entry_recovery.validate_url'):
            self.store.save_contacts(self.sid,{'contacts':{'qq_group':['https://qm.qq.com/q/fixture']},'contact_records':[],'contact_status':'found','entry_recovery':recovery})
        after=self.store.get(self.sid);self.assertEqual(after['url'],NEW);self.assertEqual(after['contact_result']['entry_history'][0]['from'],self.original)

if __name__=='__main__':unittest.main()
