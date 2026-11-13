import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import server

class RerunIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=server.Store(Path(self.temp.name)/'test.db',seed=False)
        self.store.import_rows([{'url':'https://fixture.example'}])
        with self.store.connect() as db:self.sid=db.execute('SELECT id FROM suppliers').fetchone()[0]
        self.result={'contacts':{'email':['sales@fixture.example']},'sources':[{'url':'https://fixture.example','excerpt':'original evidence'}], 'offers':[{'model':'deepseek-v3','ratio':.35,'currency':'USD','unit':'1M tokens','group':'a','resource_form':'自部署','notes':'预付费'}]}
        self.store.save_result(self.sid,copy.deepcopy(self.result))
    def test_sources_are_deduplicated_and_changed_text_retained(self):
        prior=self.store.get(self.sid)
        self.store.save_result(self.sid,dict(self.result,sources=prior['sources']+self.result['sources']))
        self.assertEqual(len(self.store.get(self.sid)['sources']),1)
        self.store.save_result(self.sid,dict(self.result,sources=[{'url':'https://fixture.example','excerpt':'new evidence'}]))
        self.assertEqual(len(self.store.get(self.sid)['sources']),2)
    def test_manual_decisions_survive_successful_rerun(self):
        for decision in ['qualified','skipped']:
            with self.subTest(decision=decision):
                self.store.review(self.sid,decision,'人工确认')
                self.store.save_result(self.sid,dict(self.result,offers=[]))
                row=self.store.get(self.sid)
                self.assertEqual((row['status'],row['reason'],row['notes']),(decision,'人工复核：人工确认','人工确认'))
    def test_manual_decisions_survive_failed_rerun(self):
        for decision in ['qualified','skipped']:
            with self.subTest(decision=decision):
                self.store.review(self.sid,decision,'人工确认');row=self.store.get(self.sid)
                self.store.job_lock.acquire();self.store.job.update(running=True,status='running',done=0)
                with patch('server.collect_site',side_effect=ValueError('fixture timeout')):self.store.run_job([row],'real','all')
                row=self.store.get(self.sid)
                self.assertEqual((row['status'],row['reason'],row['notes']),(decision,'人工复核：人工确认','人工确认'))
                self.assertEqual(row['contact_status'],'needs_review')
                self.assertEqual(row['offers'][0]['ratio'],.35)
    def test_business_quote_changes_warn_without_replacing_manual_decision(self):
        for field,value in [('currency','CNY'),('unit','1K tokens'),('group','b'),('resource_form','官转'),('notes','充值优惠'),('cache_price',.1),('ratio',.2)]:
            with self.subTest(field=field):
                self.store.save_result(self.sid,copy.deepcopy(self.result));self.store.review(self.sid,'qualified','已核实')
                changed=copy.deepcopy(self.result);changed['offers'][0][field]=value
                self.store.save_result(self.sid,changed)
                row=self.store.get(self.sid)
                self.assertEqual(row['status'],'qualified')
                self.assertIn('报价已变化，建议重新复核',row['warnings'])
    def test_quote_order_and_evidence_changes_do_not_warn(self):
        result=copy.deepcopy(self.result);result['offers'].append(dict(result['offers'][0],group='b'))
        self.store.save_result(self.sid,result);self.store.review(self.sid,'qualified','已核实')
        result['offers'].reverse();result['offers'][0]['evidence']='refreshed proof'
        self.store.save_result(self.sid,result)
        self.assertNotIn('报价已变化，建议重新复核',self.store.get(self.sid)['warnings'])

    def test_manual_review_acknowledges_price_change(self):
        self.store.review(self.sid,'qualified','old quote')
        result=copy.deepcopy(self.result);result['offers'][0]['ratio']=.2
        self.store.save_result(self.sid,result)
        self.assertIn('报价已变化，建议重新复核',self.store.get(self.sid)['warnings'])
        self.store.review(self.sid,'skipped','new quote confirmed')
        self.store.save_result(self.sid,result)
        self.assertNotIn('报价已变化，建议重新复核',self.store.get(self.sid)['warnings'])
