"""Phase 2: robots.txt unreadable no longer blocks a site, and a broken browser runtime stops the batch."""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_agent
from browser_agent import BrowserRuntimeError, run_site
from model_service import ModelService

URL = 'https://example.com/'
GOOD = {'url': URL, 'text': '正文' * 100 + ' 商务邮箱 sales@example.com', 'controls': [], 'images': []}
PLAIN = {'url': URL, 'text': '正文' * 100, 'controls': [], 'images': []}
FOUND = json.dumps({'action': {'type': 'finish'}, 'contacts': [{'type': 'email', 'value': 'sales@example.com', 'purpose': '商务', 'source_url': URL, 'evidence': '商务邮箱 sales@example.com'}]})
WAIT = json.dumps({'action': {'type': 'wait'}})


def factory(label, snapshots, events, fail=None):
    class Fake:
        def __init__(self, folder):
            events.append(('start', label))
            if fail:
                raise fail

        def command(self, action):
            events.append(('cmd', label, action['type']))
            item = snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
            if isinstance(item, Exception):
                raise item
            return {'snapshot': dict(item)}

        def close(self):
            events.append(('close', label))
    Fake.label = label
    return Fake


class Phase2Tests(unittest.TestCase):
    def service(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        service = ModelService(os.path.join(folder.name, 'm.json'))
        service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'})
        data = service.read()
        data['tested'] = True
        service._write(data)
        return service

    def test_robots_404_is_treated_as_no_rules(self):
        def fetch(url):
            raise urllib.error.HTTPError(url, 404, 'nf', {}, None)
        service = self.service()
        with patch('browser_agent.validate_url'), patch('engine.fetch', side_effect=fetch), patch.object(type(service), 'complete', side_effect=[WAIT, FOUND]):
            result = run_site(URL, 'contacts', service, [], moli_factory=factory('Moli', [GOOD], []))
        self.assertEqual(result['contact_status'], 'found')
        self.assertFalse(any('robots.txt 读取失败' in w for w in result['warnings']))  # No robots file: normal crawl, nothing to report.

    def test_robots_timeout_retries_once_then_crawls_with_warning(self):
        calls = []

        def fetch(url):
            calls.append(url)
            raise TimeoutError('timed out')
        service = self.service()
        with patch('browser_agent.validate_url'), patch('browser_agent.shutil.which', return_value=None), patch('engine.fetch', side_effect=fetch), patch.object(type(service), 'complete', side_effect=[FOUND]):
            result = run_site(URL, 'contacts', service, [], moli_factory=factory('Moli', [GOOD], []))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['contact_status'], 'found')
        self.assertTrue(any('robots.txt 读取失败' in w for w in result['warnings']))

    def test_robots_503_is_retried_then_reported(self):
        calls = []

        def fetch(url):
            calls.append(url)
            raise urllib.error.HTTPError(url, 503, 'busy', {}, None)
        service = self.service()
        with patch('browser_agent.validate_url'), patch('browser_agent.shutil.which', return_value=None), patch('engine.fetch', side_effect=fetch), patch.object(type(service), 'complete', side_effect=[FOUND]):
            result = run_site(URL, 'contacts', service, [], moli_factory=factory('Moli', [GOOD], []))
        self.assertEqual(len(calls), 2)
        self.assertTrue(any('robots.txt 读取失败（503）' in w for w in result['warnings']))

    def test_robots_disallow_still_blocks_without_chrome(self):
        events = []
        service = self.service()
        with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nDisallow: /', URL + 'robots.txt')):
            result = run_site(URL, 'contacts', service, [], moli_factory=factory('Moli', [GOOD], events))
        self.assertEqual(result['contact_status'], 'blocked')
        self.assertNotIn(('start', 'Chrome'), events)
        self.assertFalse(any('robots.txt 读取失败' in w for w in result['warnings']))

    def test_missing_browser_runtime_is_not_a_site_result(self):
        events = []
        service = self.service()
        with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', URL + 'robots.txt')), patch.object(browser_agent, 'runtime_paths', side_effect=BrowserRuntimeError('浏览器运行环境未就绪（后台缺少 Moli 或 Playwright），批次已停止')):
            with self.assertRaises(BrowserRuntimeError):
                run_site(URL, 'contacts', service, [], moli_factory=browser_agent.Browser)
        self.assertEqual([e for e in events if e[0] == 'start'], [])


if __name__ == '__main__':
    unittest.main()
