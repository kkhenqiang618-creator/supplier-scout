"""run_site fallback policy and merge_results with mock browser factories: no real browser, no network, no model."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_agent
from browser_agent import AgentServiceError, BrowserDeadError, ModelTransientSiteError, merge_results, needs_chrome, run_site
from model_service import ModelService, ModelTransientError

URL = 'https://example.com/'
GOOD = {'url': URL, 'text': '正文' * 100 + ' 商务邮箱 sales@example.com', 'controls': [], 'images': []}
PLAIN = {'url': URL, 'text': '正文' * 100, 'controls': [], 'images': []}
WEAK = {'url': URL, 'text': 'Loading', 'controls': [], 'images': [], 'render_weak': True}
EMAIL = {'type': 'email', 'value': 'sales@example.com', 'source_url': URL, 'evidence': '商务邮箱 sales@example.com'}

def factory(label, snapshots, events, fail=None):
    class Fake:
        def __init__(self, folder):
            events.append(('start', label))
            if fail: raise fail
        def command(self, action):
            events.append(('cmd', label, action['type']))
            item = snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
            if isinstance(item, Exception): raise item
            return {'snapshot': dict(item)}
        def close(self): events.append(('close', label))
    Fake.label = label
    return Fake

class Clock:
    def __init__(self, step=0): self.now = 1000.0; self.step = step
    def __call__(self): self.now += self.step; return self.now

class Harness(unittest.TestCase):
    def site(self, moli, chrome, answers, chrome_fail=None, clock=None, env=None, chrome_ready=True):
        events = []; answers = list(answers); calls = []
        def complete(messages):
            calls.append(json.loads(messages[1]['content'])['page']['url'])
            item = answers.pop(0)
            if isinstance(item, Exception): raise item
            return item
        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
            with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', URL + 'robots.txt')), patch.object(service, 'complete', side_effect=complete), patch.dict(os.environ, env or {}):
                try: result = run_site(URL, 'contacts', service, [], moli_factory=factory('Moli', list(moli), events), chrome_factory=factory('Chrome', list(chrome), events, chrome_fail), clock=clock or Clock(), chrome_ready=lambda: chrome_ready)
                except Exception as error: result = error
        return result, events, calls

FINISH = json.dumps({'action': {'type': 'finish'}})
FOUND = json.dumps({'contacts': [EMAIL], 'action': {'type': 'finish'}})

class FallbackPolicyTests(Harness):
    def test_strong_render_with_contacts_never_starts_chrome(self):
        result, events, _ = self.site([GOOD], [GOOD], [FOUND])
        self.assertNotIn(('start', 'Chrome'), events); self.assertEqual(result['contact_status'], 'found')
        self.assertNotIn('_fallback', result); self.assertEqual(result['contact_records'][0]['browser'], 'Moli')

    def test_render_weak_runs_chrome_exactly_once_after_moli_closed(self):
        result, events, _ = self.site([PLAIN, WEAK], [GOOD], [json.dumps({'action': {'type': 'wait'}}), FINISH, FOUND], env={'SCOUT_CHROME_FALLBACK': '1'})
        # PLAIN then WEAK: first page strong, so no early escalation; site_weak is false here, but 0 contacts + weak page triggers Chrome.
        self.assertEqual(events.count(('start', 'Chrome')), 1)
        self.assertLess(events.index(('close', 'Moli')), events.index(('start', 'Chrome')))
        self.assertEqual(result['contact_status'], 'found'); self.assertEqual(result['contact_records'][0]['browser'], 'Chrome')
        self.assertEqual([s['method'] for s in result['sources']][-1], 'Chrome 浏览器 Agent'); self.assertIn('Moli 浏览器 Agent', [s['method'] for s in result['sources']])
        self.assertEqual({s['browser'] for s in result['steps']}, {'Moli', 'Chrome'}); self.assertNotIn('render_weak', result); self.assertNotIn('_fallback', result)

    def test_early_escalation_spends_no_model_call_on_moli(self):
        result, events, calls = self.site([WEAK], [GOOD], [FOUND])
        self.assertEqual([e for e in events if e[0] == 'cmd' and e[1] == 'Moli'], [('cmd', 'Moli', 'open'), ('cmd', 'Moli', 'wait')])
        self.assertEqual(len(calls), 1); self.assertEqual(events.count(('start', 'Chrome')), 1)
        self.assertEqual(result['contact_status'], 'found'); self.assertNotIn('render_weak', result)
        self.assertEqual(result['steps'][0]['browser'], 'Moli'); self.assertEqual(result['steps'][0]['action'], 'wait')

    def test_no_contacts_without_weak_page_does_not_retry(self):
        result, events, _ = self.site([PLAIN], [GOOD], [FINISH])
        self.assertNotIn(('start', 'Chrome'), events); self.assertEqual(result['contact_status'], 'incomplete')

    def test_dead_moli_without_contacts_retries(self):
        result, events, _ = self.site([PLAIN, BrowserDeadError('浏览器无响应')], [GOOD], [json.dumps({'action': {'type': 'wait'}}), FOUND])
        self.assertEqual(events.count(('start', 'Chrome')), 1); self.assertEqual(result['contact_status'], 'found')

    def test_robots_refusal_does_not_retry(self):
        events = []
        with patch('engine.fetch', return_value=('User-agent: *\nDisallow: /', URL + 'robots.txt')):
            with tempfile.TemporaryDirectory() as folder:
                service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
                with patch('browser_agent.validate_url'): result = run_site(URL, 'contacts', service, [], moli_factory=factory('Moli', [GOOD], events), chrome_factory=factory('Chrome', [GOOD], events))
        self.assertEqual(events, []); self.assertEqual(result['contact_status'], 'blocked')

    def test_moli_launch_failure_does_not_retry(self):
        events = []
        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
            with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', URL + 'robots.txt')):
                result = run_site(URL, 'contacts', service, [], moli_factory=factory('Moli', [GOOD], events, ValueError('Moli 浏览器启动失败')), chrome_factory=factory('Chrome', [GOOD], events))
        self.assertNotIn(('start', 'Chrome'), events); self.assertEqual(result['contact_status'], 'blocked')

    def test_switch_off_disables_fallback_and_escalation(self):
        result, events, calls = self.site([WEAK], [GOOD], [FINISH], env={'SCOUT_CHROME_FALLBACK': '0'})
        self.assertNotIn(('start', 'Chrome'), events); self.assertEqual(len(calls), 1); self.assertTrue(result['render_weak']); self.assertNotIn('_fallback', result)

    def test_chrome_contacts_kept_when_model_fails_mid_chrome_run(self):
        # Chrome finds the email on its first step, then every model attempt fails: the email must survive.
        found_and_continue = json.dumps({'action': {'type': 'wait'}, 'contacts': [EMAIL]})
        with patch('model_service.time.sleep'):
            result, events, _ = self.site([WEAK], [GOOD], [found_and_continue] + [ModelTransientError('503')] * 6)
        self.assertIsInstance(result, ModelTransientSiteError)
        result=result.partial
        self.assertEqual(events.count(('start', 'Chrome')), 1)
        self.assertEqual(result['contact_status'], 'incomplete'); self.assertEqual([r['value'] for r in result['contact_records']], ['sales@example.com'])
        self.assertEqual(result['contact_records'][0]['browser'], 'Chrome')
        self.assertTrue(any(w.startswith('Chrome 备用浏览器未完成') for w in result['warnings'])); self.assertNotIn('_fallback', result)

    def test_chrome_launch_failure_keeps_moli_and_batch(self):
        from browser_agent import BrowserRuntimeError
        result, events, _ = self.site([PLAIN, WEAK], [GOOD], [json.dumps({'action': {'type': 'wait'}}), FINISH], chrome_fail=BrowserRuntimeError('Chrome 浏览器启动失败（后台运行环境异常），批次已停止'))
        self.assertNotIsInstance(result, Exception); self.assertEqual(result['contact_status'], 'incomplete')
        self.assertTrue(any('Chrome 备用浏览器无法启动' in w for w in result['warnings']))

    def test_moli_worker_launch_failure_stops_batch(self):
        from browser_agent import BrowserRuntimeError
        events = []
        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
            with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', URL + 'robots.txt')):
                with self.assertRaises(BrowserRuntimeError):
                    run_site(URL, 'contacts', service, [], moli_factory=factory('Moli', [GOOD], events, BrowserRuntimeError('Moli 浏览器启动失败（后台运行环境异常），批次已停止')), chrome_factory=factory('Chrome', [GOOD], events))
        self.assertNotIn(('start', 'Chrome'), events)

    def test_model_transient_and_service_error_on_moli_never_retry(self):
        result, events, _ = self.site([WEAK, PLAIN], [GOOD], [ModelTransientError('503')] * 3, env={'SCOUT_CHROME_FALLBACK': '0'})
        self.assertIsInstance(result, ModelTransientSiteError); self.assertNotIn(('start', 'Chrome'), events); self.assertNotIn('_fallback', result.partial)
        result, events, _ = self.site([PLAIN], [GOOD], [ValueError('401 鉴权失败')])
        self.assertIsInstance(result, AgentServiceError); self.assertNotIn(('start', 'Chrome'), events); self.assertNotIn('_fallback', result.partial)

class FallbackFailureTests(Harness):
    def test_chrome_launch_failure_keeps_moli_result(self):
        # Early hand-over skipped Moli's model steps, so a failed Chrome launch gives Moli its normal run back (closed before the rerun starts).
        result, events, calls = self.site([WEAK], [GOOD], [FINISH], chrome_fail=ValueError('Chrome 浏览器启动失败'))
        self.assertEqual(result['contact_status'], 'blocked'); self.assertTrue(result['render_weak']); self.assertEqual(len(calls), 1)
        self.assertTrue(any(w.startswith('Chrome 备用浏览器未完成') and '启动失败' in w for w in result['warnings'])); self.assertEqual(events.count(('close', 'Moli')), 2)
        self.assertEqual([e[0] for e in events if e[-1] in ('Moli', 'Chrome') and e[0] != 'cmd'], ['start', 'close', 'start', 'start', 'close'])

    def test_chrome_launch_failure_after_escalation_recovers_with_moli(self):
        late = {'url': URL, 'text': '正文' * 100 + ' 商务邮箱 sales@example.com', 'controls': [], 'images': []}
        result, events, _ = self.site([WEAK, WEAK, late], [GOOD], [json.dumps({'action': {'type': 'wait'}}), FOUND], chrome_fail=ValueError('Chrome 浏览器启动失败'))
        self.assertEqual(result['contact_status'], 'found'); self.assertEqual(result['contact_records'][0]['browser'], 'Moli'); self.assertNotIn('_fallback', result)

    def test_short_page_with_visible_contact_is_not_escalated(self):
        tiny = {'url': URL, 'text': '商务微信：biz_wx_777', 'controls': [], 'images': [], 'render_weak': True}
        wx = json.dumps({'contacts': [{'type': 'wechat', 'value': 'biz_wx_777', 'source_url': URL, 'evidence': '商务微信：biz_wx_777'}], 'action': {'type': 'finish'}})
        result, events, calls = self.site([tiny], [GOOD], [wx], chrome_fail=ValueError('Chrome 浏览器启动失败'))
        self.assertEqual(len(calls), 1); self.assertEqual(result['contact_status'], 'found'); self.assertEqual(result['contacts'], {'wechat': ['biz_wx_777']})

    def test_chrome_model_transient_propagates_with_moli_evidence(self):
        result, _, _ = self.site([PLAIN, WEAK], [GOOD], [json.dumps({'contacts': [], 'action': {'type': 'wait'}}), FINISH, ModelTransientError('503')])
        self.assertIsInstance(result, ModelTransientSiteError)
        self.assertEqual(result.partial['contact_status'], 'incomplete')
        self.assertTrue(result.partial['model_transient'])
        self.assertIn('Moli 浏览器 Agent', [source['method'] for source in result.partial['sources']])
        self.assertTrue(any(w.startswith('Chrome 备用浏览器未完成') for w in result.partial['warnings']))

    def test_chrome_service_error_raises_with_merged_partial(self):
        moli_page = {'url': URL, 'text': '正文' * 100, 'controls': [], 'images': []}
        result, _, _ = self.site([moli_page, WEAK], [GOOD], [json.dumps({'action': {'type': 'wait'}}), FINISH, ValueError('401 鉴权失败')])
        self.assertIsInstance(result, AgentServiceError); partial = result.partial
        self.assertNotIn('_fallback', partial); self.assertIn('Chrome 浏览器 Agent', [s['method'] for s in partial['sources']]); self.assertIn('Moli 浏览器 Agent', [s['method'] for s in partial['sources']])

    def test_both_weak_stays_render_weak_and_blocked(self):
        result, events, _ = self.site([WEAK], [WEAK], [FINISH])
        self.assertEqual(events.count(('start', 'Chrome')), 1); self.assertTrue(result['render_weak']); self.assertEqual(result['contact_status'], 'blocked')
        self.assertEqual(result['contact_summary'], browser_agent.BOTH_WEAK_SUMMARY)

    def test_missing_chrome_keeps_full_moli_run(self):
        result, events, calls = self.site([WEAK], [GOOD], [FINISH], chrome_ready=False)
        self.assertNotIn(('start', 'Chrome'), events); self.assertEqual(len(calls), 1); self.assertTrue(result['render_weak'])
        self.assertIn(browser_agent.NO_CHROME_WARNING, result['warnings']); self.assertNotIn(('cmd', 'Moli', 'wait'), events)

    def test_chrome_success_rewrites_moli_retry_warning(self):
        result, _, _ = self.site([WEAK], [GOOD], [FOUND])
        self.assertIn(browser_agent.CHROME_RETRIED, result['warnings']); self.assertFalse(any(w.startswith(browser_agent.RENDER_WEAK_WARNING) for w in result['warnings']))

    def test_weak_subpage_after_good_homepage_retries_with_chrome(self):
        # Intended: 0 contacts and any weak page means Moli may have missed the contact entry, so Chrome reruns the site once.
        result, events, _ = self.site([PLAIN, WEAK], [GOOD], [json.dumps({'action': {'type': 'open', 'url': URL}}), FINISH, FOUND])
        self.assertEqual(events.count(('start', 'Chrome')), 1); self.assertEqual(result['contact_status'], 'found')

WAIT = json.dumps({'action': {'type': 'wait'}})

class SoftDeadlineTests(Harness):
    def timed(self, scope, moli, chrome, seconds, answers=None, first=None):
        """run_site on a fake clock: each model call advances it by `seconds` (the first call by `first` if given)."""
        now = [0.0]; answers = list(answers or [WAIT] * 40); events = []; calls = []
        def complete(messages):
            now[0] += first if first is not None and not calls else seconds
            calls.append(json.loads(messages[1]['content'])); return answers.pop(0)
        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
            with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', URL + 'robots.txt')), patch.object(service, 'complete', side_effect=complete):
                result = run_site(URL, scope, service, [], moli_factory=factory('Moli', list(moli), events), chrome_factory=factory('Chrome', list(chrome), events), clock=lambda: now[0], chrome_ready=lambda: True)
        return result, events, calls

    def test_healthy_site_keeps_all_steps(self):
        # Strong pages never tighten Moli to its weak budget. Full: 12*40=480 < 810-210. Contacts with nothing found extends 8 -> 12:
        # 5s/14s steps fit all 12 (11*14=154 <= 310-110); at 20s steps 9-11 start at t=160..200 and the 12th (t=220) is refused.
        for scope, steps, seconds, cut in [('all', 12, 15, False), ('all', 12, 25, False), ('all', 12, 40, False), ('contacts', 12, 5, False), ('contacts', 12, 14, False), ('contacts', 11, 20, True)]:
            with self.subTest(scope=scope, seconds=seconds):
                result, events, calls = self.timed(scope, [PLAIN], [GOOD], seconds)
                self.assertEqual(len(calls), steps); self.assertEqual(browser_agent.SOFT_DEADLINE_WARNING in result['warnings'], cut); self.assertNotIn(('start', 'Chrome'), events)

    def test_step_cap_per_scope_and_steps_remaining(self):
        # Contacts with nothing found: 8 base steps counted down honestly, then the 4-step extension counted down on its own.
        for scope, remaining in [('contacts', list(range(8, 0, -1)) + [4, 3, 2, 1]), ('all', list(range(12, 0, -1)))]:
            with self.subTest(scope=scope):
                result, events, calls = self.timed(scope, [PLAIN], [GOOD], 1)
                self.assertEqual([c['steps_remaining'] for c in calls], remaining)
                self.assertIn('达到本次单站动作预算，检查尚未完成，可单独继续', result['warnings'])
        self.assertEqual((browser_agent.CONTACTS_MAX_STEPS, browser_agent.MAX_STEPS), (8, 12))

    def test_chrome_retry_also_capped_at_eight_for_contacts(self):
        # Moli PLAIN -> WEAK with nothing found triggers Chrome; each browser run gets its own 8-step cap (+4 when readable and nothing found).
        result, events, calls = self.timed('contacts', [PLAIN, WEAK], [GOOD], 1)
        self.assertEqual(events.count(('start', 'Chrome')), 1); self.assertEqual(len(calls), 24)
        self.assertEqual([s['browser'] for s in result['steps']].count('Chrome'), 12)
        # Moli had one readable page, so it extended too (12 steps) before Chrome finds the contact on its first step and stops.
        result, events, calls = self.timed('contacts', [PLAIN, WEAK], [GOOD], 1, [WAIT] * 12 + [FOUND])
        self.assertEqual([s['browser'] for s in result['steps']].count('Chrome'), 1); self.assertEqual(len(calls), 13)

    def test_weak_page_tightens_moli_to_its_budget(self):
        # Full: steps at t=0,40,80,120 (360-160<210 stops at the 5th); Chrome then gets the rest of SITE_BUDGET.
        result, events, _ = self.timed('all', [PLAIN, WEAK], [GOOD], 40)
        self.assertEqual(sum(1 for e in events if e[:2] == ('cmd', 'Moli')), 5); self.assertEqual(events.count(('start', 'Chrome')), 1)
        # Contacts: steps at t=0,20,40 (150-60<110 stops the 4th); Chrome starts at t=60 and may start steps until 300-110=190 (t=60..180: 7 steps).
        result, events, calls = self.timed('contacts', [PLAIN, WEAK], [GOOD], 20)
        self.assertEqual(sum(1 for e in events if e[:2] == ('cmd', 'Moli')), 4); self.assertEqual(events.count(('start', 'Chrome')), 1)
        self.assertEqual([s['browser'] for s in result['steps']].count('Chrome'), 7); self.assertIn(browser_agent.SOFT_DEADLINE_WARNING, result['warnings'])

    def test_contacts_soft_deadline_300_is_respected(self):
        # 30s per step on strong pages: steps start at t=0..180 (300-180=120 >= 110), the 8th at t=210 would leave 90 < 110.
        result, events, calls = self.timed('contacts', [PLAIN], [GOOD], 30)
        self.assertEqual(len(calls), 7); self.assertIn(browser_agent.SOFT_DEADLINE_WARNING, result['warnings']); self.assertNotIn(('start', 'Chrome'), events)
        self.assertLessEqual(30 * (len(calls) - 1), browser_agent.CONTACTS_SITE_BUDGET - browser_agent.CONTACTS_STEP_RESERVE)

    def test_moli_stops_before_its_soft_deadline_and_chrome_skipped_when_site_budget_gone(self):
        clock = Clock(step=60)  # each clock read advances 60s: Moli hits its weak budget minus the reserve quickly
        result, events, calls = self.site([PLAIN, WEAK], [GOOD], [WAIT] * 12, clock=clock)
        self.assertLess(len(calls), 8); self.assertIn(browser_agent.SOFT_DEADLINE_WARNING, result['warnings'])
        self.assertNotIn('_fallback', result)

    def test_no_step_starts_with_less_than_reserve(self):
        # deadline=360: full reserve 210 -> steps at t=0,100 only; contacts reserve 110 -> steps at t=0,100,200 (360-300=60 < 110).
        for scope, expected in [('all', [100, 200]), ('contacts', [100, 200, 300])]:
            with self.subTest(scope=scope):
                now = [0]; events = []; spent = []
                def complete(messages): now[0] += 100; spent.append(now[0]); return WAIT
                with tempfile.TemporaryDirectory() as folder:
                    service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
                    with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', URL + 'robots.txt')), patch.object(service, 'complete', side_effect=complete):
                        result = browser_agent.run_agent(URL, scope, service, [], browser_factory=factory('Moli', [PLAIN], events), deadline=360, clock=lambda: now[0])
                self.assertEqual(spent, expected); self.assertIn(browser_agent.SOFT_DEADLINE_WARNING, result['warnings'])

    def test_chrome_not_started_without_budget(self):
        result, events, _ = self.timed('contacts', [PLAIN, WEAK], [GOOD], 500, [FINISH])
        self.assertNotIn(('start', 'Chrome'), events)

    def test_contacts_chrome_skipped_below_reserve_but_full_mode_still_retries(self):
        # One 200s step, page turns weak, nothing found. Contacts: 300-200=100 < 110 -> no Chrome. Full: 810-200=610 >= 210 -> Chrome.
        result, events, _ = self.timed('contacts', [PLAIN, WEAK], [GOOD], 200, [WAIT, FINISH])
        self.assertNotIn(('start', 'Chrome'), events); self.assertIn('本站剩余时间不足，未启动 Chrome 备用浏览器', result['warnings'])
        result, events, _ = self.timed('all', [PLAIN, WEAK], [GOOD], 1, [WAIT, FINISH, FINISH], first=200)
        self.assertEqual(events.count(('start', 'Chrome')), 1)
        # Chrome needs reserve + launch/open headroom (110+50=160): 300-140=160 starts it, 300-150=150 (above the bare reserve) does not.
        result, events, _ = self.timed('contacts', [PLAIN, WEAK], [GOOD], 1, [WAIT, FINISH], first=140)
        self.assertEqual(events.count(('start', 'Chrome')), 1)
        result, events, _ = self.timed('contacts', [PLAIN, WEAK], [GOOD], 1, [WAIT, FINISH], first=150)
        self.assertNotIn(('start', 'Chrome'), events); self.assertIn('本站剩余时间不足，未启动 Chrome 备用浏览器', result['warnings'])

    def test_run_cut_before_first_step_on_weak_page_stays_render_weak(self):
        # A browser run that reaches its deadline at step 1 on a weak homepage must not count as a good Chrome render in merge_results.
        events = []
        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
            with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', URL + 'robots.txt')), patch.object(service, 'complete', side_effect=AssertionError('no model call')):
                chrome = browser_agent.run_agent(URL, 'contacts', service, [], browser_factory=factory('Chrome', [WEAK], events), deadline=100, clock=lambda: 0)
        self.assertTrue(chrome['render_weak']); self.assertIn(browser_agent.SOFT_DEADLINE_WARNING, chrome['warnings'])
        merged = merge_results({'contact_records': [], 'sources': [{'url': URL}], 'render_weak': True, 'warnings': [browser_agent.RENDER_WEAK_WARNING]}, browser_agent.strip_internal(chrome))
        self.assertTrue(merged['render_weak']); self.assertEqual(merged['contact_summary'], browser_agent.BOTH_WEAK_SUMMARY)

class MergeTests(unittest.TestCase):
    def test_union_dedup_and_additional_sources(self):
        moli = {'contact_records': [dict(EMAIL)], 'offers': [{'model_name': 'm', 'source': URL, 'price_text': '1'}], 'sources': [{'url': URL, 'method': 'Moli 浏览器 Agent'}], 'steps': [{'step': 1}], 'warnings': ['a'], 'scope': 'all', 'render_weak': True, 'contact_status': 'found'}
        chrome = {'contact_records': [dict(EMAIL, value='SALES@example.com', source_url=URL + 'contact', evidence='邮箱 SALES@example.com'), {'type': 'wechat', 'value': 'biz1', 'source_url': URL, 'evidence': '微信 biz1'}],
                  'offers': [{'model_name': 'm', 'source': URL, 'price_text': '1'}, {'model_name': 'n', 'source': URL, 'price_text': '2'}], 'sources': [{'url': URL, 'method': 'Chrome 浏览器 Agent'}], 'steps': [{'step': 1, 'browser': 'Chrome'}], 'warnings': ['a', 'b'], 'scope': 'all', 'contact_status': 'found'}
        merged = merge_results(moli, chrome)
        self.assertEqual([(r['type'], r['value'], r['browser']) for r in merged['contact_records']], [('email', 'sales@example.com', 'Moli'), ('wechat', 'biz1', 'Chrome')])
        self.assertEqual(merged['contact_records'][0]['additional_sources'], [{'source_url': URL + 'contact', 'evidence': '邮箱 SALES@example.com', 'browser': 'Chrome'}])
        self.assertEqual(merged['contacts'], {'email': ['sales@example.com'], 'wechat': ['biz1']}); self.assertEqual(len(merged['offers']), 2)
        self.assertEqual([s['method'] for s in merged['sources']], ['Moli 浏览器 Agent', 'Chrome 浏览器 Agent']); self.assertEqual([s['browser'] for s in merged['steps']], ['Moli', 'Chrome'])
        self.assertEqual(merged['warnings'], ['a', 'b']); self.assertEqual(merged['contact_status'], 'found'); self.assertNotIn('render_weak', merged)

    def test_status_rules(self):
        weak = {'contact_records': [], 'sources': [{'url': URL}], 'render_weak': True, 'contact_status': 'blocked'}
        ok = {'contact_records': [], 'sources': [{'url': URL}], 'contact_status': 'incomplete', 'contact_summary': 'x'}
        self.assertEqual(merge_results(weak, ok)['contact_status'], 'incomplete'); self.assertNotIn('render_weak', merge_results(weak, ok))
        both = merge_results(weak, dict(weak)); self.assertEqual(both['contact_status'], 'blocked'); self.assertTrue(both['render_weak'])
        qr = merge_results(weak, dict(ok, contact_records=[{'type': 'qrcode', 'value': 'q', 'source_url': URL, 'evidence': 'q'}])); self.assertEqual(qr['contact_status'], 'needs_review')

    def test_needs_chrome(self):
        self.assertTrue(needs_chrome({'render_weak': True, 'contact_records': [EMAIL]}))
        self.assertTrue(needs_chrome({'contact_records': [], '_fallback': {'weak': True}}))
        self.assertTrue(needs_chrome({'contact_records': [], '_fallback': {'dead': True}}))
        self.assertFalse(needs_chrome({'contact_records': [EMAIL], '_fallback': {'weak': True, 'dead': True}}))
        self.assertFalse(needs_chrome({'contact_records': [], '_fallback': {}}))

class WiringTests(unittest.TestCase):
    def test_collection_worker_and_server_timeouts(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('result=run_site(', (root / 'collection_worker.py').read_text(encoding='utf-8'))
        import server
        self.assertEqual(server.SITE_HARD_TIMEOUT, {'all': 840, 'contacts': 480})
        # Worst step = 3 x 45s model requests + 40s retry sleeps + one 30s browser command = 205s; the last step may start at soft - reserve,
        # so even that worst step ends before the scope's hard timeout. Contacts also covers a hanging new-origin robots.txt (~55s) and browser close (~15s).
        worst = 3 * 45 + 40 + 30
        self.assertLess(browser_agent.CONTACTS_SITE_BUDGET - browser_agent.CONTACTS_STEP_RESERVE + worst + 55 + 15, server.SITE_HARD_TIMEOUT['contacts'])
        for scope in ['all', 'contacts']:
            budget = browser_agent.site_budget(scope)
            self.assertGreaterEqual(budget['reserve'], 2 * (14 + 30))  # >= two P90 model steps plus full 30s browser commands before the soft deadline
            self.assertLess(budget['site'] - budget['reserve'] + worst, server.SITE_HARD_TIMEOUT[scope])
            self.assertLess(budget['moli'], budget['site'] - budget['reserve'])
        self.assertEqual((browser_agent.SITE_BUDGET, browser_agent.MOLI_BUDGET, browser_agent.STEP_RESERVE), (810, 360, 210))

    def test_collect_site_uses_scope_aware_hard_timeout(self):
        import io, threading, types
        import server
        class Process:
            pid = 0
            def __init__(self): self.stdout = io.StringIO('')
            def poll(self): return 0
        for scope, elapsed, message in [('contacts', 481, '单站 Agent 超过 8 分钟'), ('all', 841, '单站 Agent 超过 14 分钟'), ('contacts', 479, '采集进程意外结束'), ('all', 839, '采集进程意外结束')]:
            with self.subTest(scope=scope, elapsed=elapsed):
                reads = iter([0.0]); fake = types.SimpleNamespace(monotonic=lambda: next(reads, elapsed))
                with patch('server.subprocess.Popen', return_value=Process()), patch('server.time', fake):
                    with self.assertRaisesRegex(ValueError, message): server.collect_site(URL, lambda m: None, threading.Event(), scope)

    def test_chrome_browser_always_uses_guard_proxy(self):
        events = []
        class Proxy:
            def __init__(self, **kw): events.append(kw); self.port = 4321
            def close(self): events.append('proxy-closed')
        spawned = []
        with tempfile.TemporaryDirectory() as folder, patch('egress_proxy.GuardProxy', Proxy), patch('browser_agent.runtime_paths', return_value=('moli', 'pw')), patch.object(browser_agent.Browser, '_spawn', lambda self, module, argv: spawned.append(argv) or (_ for _ in ()).throw(ValueError('Chrome 浏览器启动失败'))), patch.dict(os.environ, {'SCOUT_CHROME_FALLBACK': '0'}):
            os.environ.pop('SCOUT_CHROME_BIN', None)
            with self.assertRaisesRegex(ValueError, '启动失败'): browser_agent.ChromeBrowser(folder)
        self.assertEqual(events, [{'allow_loopback_for_tests': False}, 'proxy-closed']); self.assertEqual(spawned, [['launch', '4321']])

class CrashAndQrTests(unittest.TestCase):
    def worker_reply(self, reply):
        import queue
        browser = browser_agent.Browser.__new__(browser_agent.Browser)
        class Worker:
            class stdin:
                @staticmethod
                def write(line): pass
                @staticmethod
                def flush(): pass
            @staticmethod
            def poll(): return None
        browser.worker = Worker; browser.replies = queue.Queue(); browser.replies.put(json.dumps(reply) + '\n')
        return browser.command({'type': 'wait'})

    def test_dead_reply_and_closed_target_map_to_browser_dead(self):
        with self.assertRaises(BrowserDeadError): self.worker_reply({'ok': False, 'dead': True, 'error': '浏览器进程已退出'})
        with self.assertRaises(BrowserDeadError): self.worker_reply({'ok': False, 'error': 'page.evaluate: Target page, context or browser has been closed'})
        with self.assertRaises(ValueError) as caught: self.worker_reply({'ok': False, 'error': '控件编号无效'})
        self.assertNotIsInstance(caught.exception, BrowserDeadError)

    def test_worker_reports_disconnect(self):
        source = (Path(__file__).resolve().parents[1] / 'agent_browser.cjs').read_text(encoding='utf-8')
        self.assertIn("browser.on('disconnected'", source); self.assertIn('dead:true', source)
        self.assertNotIn('--disable-features=', source)  # would replace Playwright's own disabled-feature list

    def test_decode_qr_skips_attempts_past_deadline(self):
        snapshot = {'url': URL, 'images': [{'ref': 'i0', 'kind': 'img', 'src': 'https://example.com/qr.png'}]}
        class B:
            calls = []
            def command(self, action): self.calls.append(action['type']); return {'image': {}}
        browser = B()
        with tempfile.TemporaryDirectory() as folder, patch('browser_agent.download_image', side_effect=AssertionError('no download')):
            with self.assertRaisesRegex(ValueError, '剩余时间不足'): browser_agent.decode_qr(snapshot, 'i0', browser, folder, deadline=100, clock=lambda: 90)
        self.assertEqual(browser.calls, [])

if __name__ == '__main__': unittest.main()
