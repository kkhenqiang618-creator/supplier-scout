import json
import unittest
from unittest import mock

import browser_agent

NEWAPI_STATUS = {
    'system_name': '某中转站',
    'version': 'v0.8.7',
    'start_time': 1700000000,
    'announcements': [
        {
            'title': '商务合作',
            'content': '<p>商务邮箱：<a href="mailto:sales@example.com">sales@example.com</a></p>',
        },
        {'title': '玩家群', 'content': '<p>QQ群：<a href="https://qm.qq.com/q/abc123">123456</a></p>'},
    ],
    'faq': [{'question': '怎么联系客服？', 'answer': '<div>Telegram：https://t.me/example_support</div>'}],
    'footer_html': '<footer>© 2025 示例科技 · 微信：example_ops</footer>',
}

# What new-api / one-api really answers: the useful keys live inside "data".
WRAPPED_STATUS = {'success': True, 'message': '', 'data': NEWAPI_STATUS}
WRAPPED_NOTICE = {'success': True, 'message': '', 'data': '<p>客服 QQ：80012345</p>'}
WRAPPED_ABOUT = {'success': True, 'message': '', 'data': '<p>关于我们：hi@relay.example.com</p>'}


class DetectionTests(unittest.TestCase):
    def test_newapi_status_is_recognised_after_unwrapping(self):
        self.assertTrue(browser_agent.is_newapi(browser_agent.unwrap_api(WRAPPED_STATUS)))
        self.assertTrue(browser_agent.is_newapi({'announcements': []}))

    def test_outer_envelope_alone_is_not_enough(self):
        # The real site was skipped exactly because detection looked at the envelope only.
        self.assertFalse(browser_agent.is_newapi(WRAPPED_STATUS))
        self.assertFalse(browser_agent.is_newapi({'success': True, 'message': '', 'data': {'ok': 1}}))

    def test_other_json_is_not(self):
        self.assertFalse(browser_agent.is_newapi({'status': 'ok'}))
        self.assertFalse(browser_agent.is_newapi(['a']))
        self.assertFalse(browser_agent.is_newapi('text'))


class UnwrapTests(unittest.TestCase):
    def test_data_object_is_returned(self):
        self.assertIs(browser_agent.unwrap_api(WRAPPED_STATUS), NEWAPI_STATUS)

    def test_data_text_is_returned(self):
        self.assertEqual(browser_agent.unwrap_api(WRAPPED_NOTICE), '<p>客服 QQ：80012345</p>')

    def test_data_json_text_is_parsed(self):
        payload = {'success': True, 'data': json.dumps(NEWAPI_STATUS)}
        self.assertEqual(browser_agent.unwrap_api(payload), NEWAPI_STATUS)

    def test_plain_payload_is_untouched(self):
        self.assertEqual(browser_agent.unwrap_api(NEWAPI_STATUS), NEWAPI_STATUS)
        self.assertEqual(browser_agent.unwrap_api('text'), 'text')


class ApiTextTests(unittest.TestCase):
    def test_only_public_content_keys_survive(self):
        text = browser_agent.api_text(
            {**browser_agent.unwrap_api(WRAPPED_STATUS), 'data': {'secret': 'sk-123'}}
        )
        self.assertIn('某中转站', text)
        self.assertIn('sales@example.com', text)
        self.assertIn('t.me/example_support', text)
        self.assertNotIn('sk-123', text)

    def test_wrapped_notice_and_about_keep_their_text(self):
        notice = browser_agent.api_text(browser_agent.unwrap_api(WRAPPED_NOTICE))
        about = browser_agent.api_text(browser_agent.unwrap_api(WRAPPED_ABOUT))
        self.assertIn('客服 QQ：80012345', notice)
        self.assertIn('hi@relay.example.com', about)

    def test_html_is_reduced_to_lines(self):
        text = browser_agent.api_text({'notice': '<p>邮箱 a@b.cn</p><script>x()</script>'})
        self.assertIn('邮箱 a@b.cn', text)
        self.assertNotIn('<p>', text)
        self.assertNotIn('x()', text)


class ContactExtractionTests(unittest.TestCase):
    def setUp(self):
        self.observation = browser_agent.api_observation(
            'https://relay.example.com/api/status', browser_agent.unwrap_api(WRAPPED_STATUS)
        )
        self.assertIsNotNone(self.observation)

    def test_contacts_carry_the_exact_source_line(self):
        records = browser_agent.api_contact_records([self.observation])
        values = {(r['type'], r['value']) for r in records}
        self.assertIn(('email', 'sales@example.com'), values)
        self.assertIn(('qq_group', 'https://qm.qq.com/q/abc123'), values)
        self.assertIn(('telegram', 'https://t.me/example_support'), values)
        self.assertIn(('wechat', 'example_ops'), values)
        for record in records:
            self.assertEqual(record['source_url'], 'https://relay.example.com/api/status')
            self.assertIn(record['evidence'], self.observation['text'].split('\n'))
            self.assertTrue(record['risk_flags'])

    def test_same_handle_on_several_lines_is_one_record(self):
        observation = {
            'url': 'https://relay.example.com/api/status',
            'text': '邮箱：a@example.com\n合作：a@example.com\n客服：a@example.com',
            'method': '公开接口',
        }
        records = browser_agent.api_contact_records([observation])
        emails = [r for r in records if r['type'] == 'email']
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]['evidence'], '邮箱：a@example.com')

    def test_duplicates_across_endpoints_are_merged(self):
        other = {
            'url': 'https://relay.example.com/api/notice',
            'text': '邮箱：A@Example.com',
            'method': '公开接口',
        }
        records = browser_agent.api_contact_records([self.observation, other])
        emails = [r['value'] for r in records if r['type'] == 'email']
        self.assertEqual(len(emails), len(set(e.lower() for e in emails)))

    def test_records_pass_the_normal_verification(self):
        records = browser_agent.api_contact_records([self.observation])
        accepted, rejected = browser_agent.validate_contacts(records, [self.observation])
        self.assertEqual(len(accepted), len(records))
        self.assertEqual(rejected, [])


class ProbeTests(unittest.TestCase):
    def probe(self, answers, allow=None):
        calls = []
        log = mock.Mock()

        def fetch(url):
            calls.append(url)
            answer = answers.get(url)
            if isinstance(answer, Exception):
                raise answer
            if answer is None:
                raise ValueError('404')
            return json.dumps(answer), url

        with mock.patch.object(browser_agent, 'fetch_public_api', fetch):
            observations = browser_agent.probe_public_api(
                'https://relay.example.com/console', allow or (lambda url: None), log
            )
        return observations, calls, log

    def test_reads_wrapped_status_notice_and_about_once_each(self):
        answers = {
            'https://relay.example.com/api/status': WRAPPED_STATUS,
            'https://relay.example.com/api/notice': WRAPPED_NOTICE,
            'https://relay.example.com/api/about': WRAPPED_ABOUT,
        }
        observations, calls, log = self.probe(answers)
        self.assertEqual(
            calls,
            [
                'https://relay.example.com/api/status',
                'https://relay.example.com/api/notice',
                'https://relay.example.com/api/about',
            ],
        )
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(o['method'] == '公开接口' for o in observations))
        self.assertIn('某中转站', observations[0]['text'])
        self.assertIn('客服 QQ：80012345', observations[1]['text'])
        self.assertIn('hi@relay.example.com', observations[2]['text'])
        log.assert_called_with('识别为 new-api/one-api 类系统，读取公开接口公告')

    def test_plain_json_site_is_left_alone(self):
        observations, calls, _ = self.probe(
            {'https://relay.example.com/api/status': {'success': True, 'data': {'ok': True}}}
        )
        self.assertEqual(observations, [])
        self.assertEqual(len(calls), 1)

    def test_network_and_robots_failures_are_silent(self):
        observations, _, _ = self.probe({}, allow=lambda url: None)
        self.assertEqual(observations, [])

        def refuse(url):
            raise ValueError('robots.txt 不允许读取该页面')

        observations, calls, _ = self.probe(
            {'https://relay.example.com/api/status': WRAPPED_STATUS}, allow=refuse
        )
        self.assertEqual(observations, [])
        self.assertEqual(calls, [])

    def test_missing_extras_keep_status_observation(self):
        observations, _, _ = self.probe({'https://relay.example.com/api/status': WRAPPED_STATUS})
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['url'], 'https://relay.example.com/api/status')


class RunAgentIntegrationTests(unittest.TestCase):
    def test_public_api_contacts_land_in_the_normal_result(self):
        import tempfile
        from pathlib import Path

        from model_service import ModelService

        snapshot = {'url': 'https://relay.example.com/console', 'text': '请登录后查看', 'controls': []}

        class FakeBrowser:
            label = 'Fixture'

            def __init__(self, folder):
                pass

            def command(self, action):
                return {'snapshot': dict(snapshot)}

            def close(self):
                pass

        answers = {
            'https://relay.example.com/api/status': json.dumps(WRAPPED_STATUS),
            'https://relay.example.com/api/notice': ValueError('404'),
            'https://relay.example.com/api/about': ValueError('404'),
        }

        def fetch(url):
            answer = answers[url]
            if isinstance(answer, Exception):
                raise answer
            return answer, url

        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(Path(folder) / 'model.json')
            service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'})
            config = service.read()
            config['tested'] = True
            service._write(config)
            with (
                mock.patch('browser_agent.validate_url'),
                mock.patch('browser_agent.fetch_robots', return_value='User-agent: *\nAllow: /'),
                mock.patch('browser_agent.fetch_public_api', fetch),
                mock.patch.object(
                    service,
                    'complete',
                    return_value=json.dumps(
                        {'reason': '只读完成', 'action': {'type': 'finish'}, 'contacts': []}
                    ),
                ),
            ):
                result = browser_agent.run_agent(
                    'https://relay.example.com/console',
                    'contacts',
                    service,
                    [],
                    max_steps=2,
                    browser_factory=FakeBrowser,
                )

        self.assertIn('sales@example.com', result['contacts'].get('email', []))
        self.assertEqual(result['contact_status'], 'found')
        methods = {source['method'] for source in result['sources']}
        self.assertIn('公开接口', methods)
        api_sources = [s for s in result['sources'] if s['method'] == '公开接口']
        self.assertEqual(api_sources[0]['url'], 'https://relay.example.com/api/status')
        self.assertIn('sales@example.com', api_sources[0]['excerpt'])


if __name__ == '__main__':
    unittest.main()
