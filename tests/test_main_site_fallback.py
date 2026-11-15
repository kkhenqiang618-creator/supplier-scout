import unittest
from unittest import mock

import browser_agent


def result(contacts=(), sources=None, status='incomplete'):
    records = [
        {
            'type': kind,
            'value': value,
            'purpose': '待确认',
            'source_url': sources[0]['url'] if sources else 'https://api.example.com/',
            'evidence': value,
            'risk_flags': [],
        }
        for kind, value in contacts
    ]
    grouped = {}
    for item in records:
        grouped.setdefault(item['type'], []).append(item['value'])
    return {
        'contacts': grouped,
        'contact_records': records,
        'contact_status': 'found' if records else status,
        'contact_summary': 'x',
        'sources': list(
            sources
            or [{'url': 'https://api.example.com/', 'method': 'Moli', 'excerpt': '', 'text_length': 0}]
        ),
        'steps': [{'step': 1, 'url': 'https://api.example.com/', 'action': 'finish'}],
        'warnings': [],
        'offers': [],
        'scope': 'contacts',
        'diagnostics': [],
    }


class MainDomainTests(unittest.TestCase):
    def test_main_domain_keeps_common_second_levels(self):
        self.assertEqual(browser_agent.main_domain('api.holysheep.ai'), 'holysheep.ai')
        self.assertEqual(browser_agent.main_domain('a.b.x.com.cn'), 'x.com.cn')
        self.assertEqual(browser_agent.main_domain('x.com'), 'x.com')
        self.assertEqual(browser_agent.main_domain('WWW.X.COM'), 'x.com')
        self.assertEqual(browser_agent.main_domain(''), '')

    def test_main_site_url_never_crosses_domains(self):
        self.assertEqual(browser_agent.main_site_url('https://api.x.com/'), 'https://www.x.com/')
        self.assertEqual(browser_agent.main_site_url('https://www.x.com/'), 'https://x.com/')
        self.assertIsNone(browser_agent.main_site_url('https://x.com/'))
        self.assertEqual(browser_agent.main_site_url('https://a.b.x.com.cn/path'), 'https://www.x.com.cn/')
        self.assertIsNone(browser_agent.main_site_url('mailto:support@x.com'))
        self.assertIsNone(browser_agent.main_site_url(''))

    def test_main_site_url_keeps_other_domains_apart(self):
        alt = browser_agent.main_site_url('https://api.notexample.com/')
        self.assertEqual(browser_agent.main_domain('notexample.com'), 'notexample.com')
        self.assertTrue(alt.endswith('www.notexample.com/'))


class RunSiteFallbackTests(unittest.TestCase):
    def run_site(self, primary, extra, url='https://api.example.com/', now=0.0, scope='contacts'):
        calls = []

        def fake_run_agent(alt, scope, service, rules, log, **kwargs):
            calls.append((alt, scope, kwargs))
            if isinstance(extra, Exception):
                raise extra
            return extra

        class FakeClock:
            """First call: run_site starts; later calls: the first pass already took `now` seconds."""

            def __init__(self):
                self.queue = [0.0, now]

            def __call__(self):
                return self.queue.pop(0) if self.queue else now

        with (
            mock.patch.object(browser_agent, '_run_site', return_value=primary),
            mock.patch.object(browser_agent, 'run_agent', side_effect=fake_run_agent),
        ):
            merged = browser_agent.run_site(url, scope, None, [], log=lambda message: None, clock=FakeClock())
        return merged, calls

    def test_second_run_deadline_stays_inside_the_hard_timeout(self):
        _, calls = self.run_site(result(), result(), now=140.0)
        self.assertEqual(len(calls), 1)
        # contacts hard timeout 480 - worst tail 275 + reserve 110 - 5 slack = 310 absolute.
        self.assertEqual(calls[0][2]['deadline'], 310.0)

    def test_second_run_still_happens_after_a_long_first_pass(self):
        merged, calls = self.run_site(result(), result(), now=100.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]['deadline'], 280.0)
        self.assertFalse(any('剩余时间不足' in warning for warning in merged['warnings']))

    def test_second_run_is_skipped_when_the_first_pass_used_the_budget(self):
        merged, calls = self.run_site(result(), result(), now=400.0)
        self.assertEqual(calls, [])
        self.assertTrue(any('剩余时间不足' in warning for warning in merged['warnings']))

    def test_second_run_without_contacts_keeps_the_original_outcome(self):
        primary = result(status='blocked')
        primary['contact_summary'] = '首页要求登录后才能查看'
        extra = result(
            sources=[
                {'url': 'https://www.example.com/', 'method': 'Moli', 'excerpt': '登录', 'text_length': 2}
            ]
        )
        merged, calls = self.run_site(primary, extra)
        self.assertEqual(len(calls), 1)
        self.assertEqual(merged['contact_status'], 'blocked')
        self.assertEqual(merged['contact_summary'], '首页要求登录后才能查看')
        self.assertEqual(merged['contact_records'], [])
        self.assertTrue(any('未发现联系方式' in warning for warning in merged['warnings']))

    def test_model_outage_skips_the_second_run(self):
        primary = result()
        primary['model_transient'] = True
        merged, calls = self.run_site(primary, ValueError('must not be called'))
        self.assertEqual(calls, [])
        self.assertEqual(merged, primary)

    def test_no_contacts_triggers_one_same_domain_run(self):
        merged, calls = self.run_site(
            result(),
            result(
                [('email', 'support@example.com')],
                sources=[
                    {
                        'url': 'https://www.example.com/',
                        'method': 'Moli',
                        'excerpt': 'support@example.com',
                        'text_length': 10,
                    }
                ],
            ),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'https://www.example.com/')
        self.assertEqual(calls[0][1], 'contacts')
        self.assertIn('support@example.com', merged['contacts']['email'])
        self.assertEqual(merged['contact_status'], 'found')
        self.assertEqual(merged['main_site'], 'https://www.example.com/')
        self.assertTrue(any('同主域官网' in warning for warning in merged['warnings']))
        self.assertTrue(all(s.get('site_role') == 'main_site' for s in merged['sources'][1:]))

    def test_existing_contacts_skip_the_second_run(self):
        primary = result([('email', 'a@example.com')])
        merged, calls = self.run_site(primary, ValueError('must not be called'))
        self.assertEqual(calls, [])
        self.assertEqual(merged, primary)

    def test_failed_second_run_keeps_the_original_result(self):
        primary = result()
        merged, calls = self.run_site(primary, ValueError('页面无法访问'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(merged['contact_records'], [])
        self.assertTrue(any('同主域官网检查未完成' in warning for warning in merged['warnings']))

    def test_empty_observation_keeps_the_original_result(self):
        primary = result()
        empty = result()
        empty['sources'] = []
        merged, _ = self.run_site(primary, empty)
        self.assertEqual(merged['contact_records'], [])
        self.assertTrue(any('同主域官网无法读取' in warning for warning in merged['warnings']))

    def test_same_domain_run_merges_duplicates_as_additional_sources(self):
        primary = result()
        primary['contact_records'] = [
            {
                'type': 'email',
                'value': 'support@example.com',
                'purpose': 'x',
                'source_url': 'https://api.example.com/',
                'evidence': 'support@example.com',
                'risk_flags': [],
            }
        ]
        primary['contacts'] = {'email': ['support@example.com']}
        primary['contact_status'] = 'found'
        merged, calls = self.run_site(primary, ValueError('must not be called'))
        self.assertEqual(calls, [])
        self.assertEqual(len(merged['contact_records']), 1)
