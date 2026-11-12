"""Contacts-only step extension (8 -> up to 12 when nothing was found on a readable run), with fake browsers, model and clock."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_agent
from browser_agent import run_agent, run_site
from model_service import ModelService

URL = 'https://example.com/'
PRIVATE = URL + 'private'
PLAIN = {'url': URL, 'text': '正文' * 100, 'controls': [{'ref': 'c1', 'text': '内部', 'href': PRIVATE}], 'images': []}
CONTACT = {'url': URL, 'text': '正文' * 100 + ' 商务邮箱 sales@example.com', 'controls': [], 'images': []}
WEAK = {'url': URL, 'text': 'Loading', 'controls': [], 'images': [], 'render_weak': True}
TIMEOUT = {'url': URL, 'text': '正文' * 100, 'controls': [], 'images': [], 'goto_timeout': True}
EMAIL = {'type': 'email', 'value': 'sales@example.com', 'source_url': URL, 'evidence': '商务邮箱 sales@example.com'}
WAIT = json.dumps({'action': {'type': 'wait'}})
FOUND_WAIT = json.dumps({'contacts': [EMAIL], 'action': {'type': 'wait'}})
FOUND = json.dumps({'contacts': [EMAIL], 'action': {'type': 'finish'}})
OPEN_PRIVATE = json.dumps({'action': {'type': 'open', 'url': PRIVATE}})
NOTICE = '前 8 步未找到联系方式，继续查找最多 4 步（共 12 步）'

def factory(label, snapshots, events):
    class Fake:
        def __init__(self, folder): events.append(('start', label))
        def command(self, action):
            events.append(('cmd', label, action['type']))
            return {'snapshot': dict(snapshots.pop(0) if len(snapshots) > 1 else snapshots[0])}
        def close(self): events.append(('close', label))
    Fake.label = label
    return Fake

class Harness(unittest.TestCase):
    def run_with(self, snapshots, answers, scope='contacts', durations=None, site=False, chrome=None, robots='User-agent: *\nAllow: /', **kwargs):
        """Each model call advances the fake clock by the next duration (default 1s). Returns result, events, contexts, logs."""
        now = [0.0]; answers = list(answers); durations = list(durations or []); events = []; contexts = []; logs = []
        def complete(messages):
            now[0] += durations.pop(0) if durations else 1
            contexts.append(json.loads(messages[1]['content'])); return answers.pop(0) if answers else WAIT
        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
            with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=(robots, URL + 'robots.txt')), patch.object(service, 'complete', side_effect=complete):
                if site:result = run_site(URL, scope, service, [], logs.append, moli_factory=factory('Moli', list(snapshots), events), chrome_factory=factory('Chrome', list(chrome or [CONTACT]), events), clock=lambda: now[0], chrome_ready=lambda: True)
                else:result = run_agent(URL, scope, service, [], logs.append, browser_factory=factory('Moli', list(snapshots), events), clock=lambda: now[0], **kwargs)
        return result, events, contexts, logs

class ExtensionTests(Harness):
    def test_a_found_within_eight_stops_without_extension(self):
        result, _, contexts, logs = self.run_with([CONTACT], [WAIT] * 5 + [FOUND_WAIT, WAIT, WAIT], deadline=300, extended_deadline=310)
        self.assertEqual(len(contexts), 8); self.assertEqual(result['contact_status'], 'found')
        self.assertFalse(any(NOTICE in m for m in logs + result['warnings'])); self.assertFalse(any(s.get('extended') for s in result['steps']))

    def test_b_readable_nothing_found_extends_to_twelve_with_honest_countdown(self):
        result, events, contexts, logs = self.run_with([PLAIN], [], deadline=300, extended_deadline=310)
        self.assertEqual([c['steps_remaining'] for c in contexts], [8, 7, 6, 5, 4, 3, 2, 1, 4, 3, 2, 1]); self.assertEqual([c['step'] for c in contexts], list(range(1, 13)))
        self.assertEqual(sum(NOTICE in m for m in logs), 1); self.assertIn('Moli ' + NOTICE, result['warnings'])
        self.assertEqual([bool(s.get('extended')) for s in result['steps']], [False] * 8 + [True] * 4)
        self.assertEqual(events.count(('start', 'Moli')), 1)  # same browser, no restart
        self.assertEqual(result['contact_status'], 'incomplete'); self.assertIn('达到本次单站动作预算，检查尚未完成，可单独继续', result['warnings'])

    def test_c_unreadable_or_blocked_runs_do_not_extend(self):
        for name, snapshots, answers, robots in [('render_weak', [WEAK], [], None), ('goto_timeout', [TIMEOUT], [], None),
                                                   ('robots', [PLAIN], [OPEN_PRIVATE] + [WAIT] * 7, 'User-agent: *\nDisallow: /private')]:
            with self.subTest(name=name):
                result, _, contexts, logs = self.run_with(snapshots, answers, robots=robots or 'User-agent: *\nAllow: /', deadline=300, extended_deadline=310)
                self.assertEqual(len(contexts), 8); self.assertFalse(any(NOTICE in m for m in logs))
        # A dead browser ends the run where it died, so it never reaches the extension.
        events = []
        class Dead:
            label = 'Moli'
            def __init__(self, folder): self.n = 0
            def command(self, action):
                self.n += 1
                if self.n > 3: raise browser_agent.BrowserDeadError('浏览器无响应')
                return {'snapshot': dict(PLAIN)}
            def close(self): pass
        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
            with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', URL + 'robots.txt')), patch.object(service, 'complete', return_value=WAIT):
                result = run_agent(URL, 'contacts', service, [], browser_factory=Dead, clock=lambda: 0)
        self.assertTrue(result['_fallback']['dead']); self.assertEqual(len(result['steps']), 3)

    def test_d_contact_found_on_step_ten_is_kept(self):
        result, _, contexts, _ = self.run_with([PLAIN] * 9 + [CONTACT], [WAIT] * 9 + [FOUND], deadline=300, extended_deadline=310)
        self.assertEqual(len(contexts), 10); self.assertEqual(result['contact_status'], 'found'); self.assertEqual(result['contacts'], {'email': ['sales@example.com']})
        self.assertTrue(result['steps'][-1]['extended']); self.assertNotIn('达到本次单站动作预算，检查尚未完成，可单独继续', result['warnings'])

    def test_e_extension_refused_when_time_is_short(self):
        # Step 8 starts at t=190 (300-190=110, allowed) and ends at t=205: 310-205=105 < 110, so no extra step starts.
        result, _, contexts, logs = self.run_with([PLAIN], [], durations=[184, 1, 1, 1, 1, 1, 1, 15], deadline=300, extended_deadline=310)
        self.assertEqual(len(contexts), 8); self.assertFalse(any(NOTICE in m for m in logs)); self.assertEqual(contexts[-1]['steps_remaining'], 1)
        # Enough left (t=200 after step 8: 310-200=110) -> extends, then the per-step reserve check stops it once time runs short.
        result, _, contexts, logs = self.run_with([PLAIN], [], durations=[184, 1, 1, 1, 1, 1, 1, 10, 5], deadline=300, extended_deadline=310)
        self.assertEqual(len(contexts), 9); self.assertTrue(any(NOTICE in m for m in logs)); self.assertIn(browser_agent.SOFT_DEADLINE_WARNING, result['warnings'])
        # run_site wires the extended deadline: 20s steps -> extra steps start at t=160,180,200; t=220 is refused.
        result, _, contexts, _ = self.run_with([PLAIN], [], durations=[20] * 12, site=True)
        self.assertEqual(len(contexts), 11); self.assertIn(browser_agent.SOFT_DEADLINE_WARNING, result['warnings'])

    def test_f_full_scope_unchanged(self):
        result, _, contexts, logs = self.run_with([PLAIN], [], scope='all', deadline=810, extended_deadline=810)
        self.assertEqual([c['steps_remaining'] for c in contexts], list(range(12, 0, -1))); self.assertFalse(any('继续查找' in m for m in logs + result['warnings']))
        self.assertEqual(browser_agent.site_budget('all')['extra_steps'], 0)
        # An explicit max_steps (tests, callers) never extends either.
        result, _, contexts, _ = self.run_with([PLAIN], [], max_steps=4)
        self.assertEqual(len(contexts), 4)

    def test_g_chrome_retry_extends_too(self):
        result, events, contexts, logs = self.run_with([PLAIN, WEAK], [], site=True, chrome=[PLAIN])
        self.assertEqual(events.count(('start', 'Chrome')), 1); self.assertEqual(len(contexts), 24)
        self.assertEqual([c['steps_remaining'] for c in contexts[12:]], [8, 7, 6, 5, 4, 3, 2, 1, 4, 3, 2, 1])
        self.assertIn('Chrome ' + NOTICE, logs); self.assertIn('Moli ' + NOTICE, logs)
        self.assertEqual(sum(1 for s in result['steps'] if s['browser'] == 'Chrome' and s.get('extended')), 4)

    def test_i_may_extend_only_on_last_base_step_of_extendable_contacts_run(self):
        flags = lambda contexts: [i for i, c in enumerate(contexts) if c.get('may_extend')]
        _, _, contexts, _ = self.run_with([PLAIN], [], deadline=300, extended_deadline=310)
        self.assertEqual(flags(contexts), [7])  # step 8 only; extended steps 9-12 must finish normally
        _, _, contexts, _ = self.run_with([PLAIN], [], scope='all', deadline=810, extended_deadline=810)
        self.assertEqual(flags(contexts), [])  # full scope never extends
        _, _, contexts, _ = self.run_with([PLAIN], [OPEN_PRIVATE] + [WAIT] * 7, robots='User-agent: *\nDisallow: /private', deadline=300, extended_deadline=310)
        self.assertEqual(flags(contexts), [])  # robots refusal
        _, _, contexts, _ = self.run_with([CONTACT], [FOUND_WAIT] + [WAIT] * 7, deadline=300, extended_deadline=310)
        self.assertEqual(flags(contexts), [])  # contacts already found
        _, _, contexts, _ = self.run_with([PLAIN], [], durations=[190, 1, 1, 1, 1, 1, 1, 1], deadline=300, extended_deadline=310)
        self.assertEqual(flags(contexts), [])  # too little time left before step 8
        self.assertIn('仅当上下文may_extend为true', browser_agent.SYSTEM)

    def test_h_worst_extended_run_ends_before_hard_timeout(self):
        import server
        worst_tail = (3 * 45 + 40 + 30) + 55 + 15  # worst step + hanging robots.txt + browser close
        self.assertEqual(worst_tail, browser_agent.WORST_TAIL)
        budget = browser_agent.site_budget('contacts')
        self.assertEqual((budget['max_steps'], budget['extra_steps'], budget['site'], budget['extended'], budget['reserve']), (8, 4, 300, 310, 110))
        self.assertLessEqual(budget['extended'] - budget['reserve'] + worst_tail, server.SITE_HARD_TIMEOUT['contacts'] - 5)
        self.assertGreaterEqual(budget['extended'], budget['site'])  # the Chrome start gate (site_end - reserve - startup) stays the stricter one
        # Simulated worst extended run: base steps slow enough that each extra step starts right at the edge, then one worst tail.
        result, _, contexts, _ = self.run_with([PLAIN], [], durations=[24] * 8 + [1, 1, 1, 1], deadline=300, extended_deadline=310)
        self.assertEqual(len(contexts), 12); self.assertLess(24 * 8 + 3 + worst_tail, server.SITE_HARD_TIMEOUT['contacts'])

if __name__ == '__main__': unittest.main()
