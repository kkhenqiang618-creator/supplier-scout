"""Browser failures must reach the existing batch recovery paths, without live services."""
import json
import tempfile
import unittest
from unittest.mock import patch

import browser_agent
import cloud_worker
import test_chrome_fallback as fixtures
import test_cloud_jobs as job_fixtures
from model_service import ModelTransientError


class AgentFailureRecoveryTests(fixtures.Harness):
    def test_chrome_failure_keeps_contacts_but_leaves_site_retryable(self):
        found = json.dumps({'contacts': [fixtures.EMAIL], 'action': {'type': 'wait'}})
        result, _, _ = self.site([fixtures.WEAK], [fixtures.GOOD], [found, ModelTransientError('503')])
        self.assertIsInstance(result, browser_agent.ModelTransientSiteError)
        self.assertEqual(result.partial['contacts'], {'email': ['sales@example.com']})
        self.assertEqual(result.partial['contact_status'], 'incomplete')
        self.assertTrue(result.partial['model_transient'])
        self.assertNotIn('_fallback', result.partial)

    def test_cloud_batch_pauses_after_three_chrome_model_failures(self):
        outcomes = []
        for _ in range(4):
            result, _, _ = self.site([fixtures.WEAK], [fixtures.GOOD], [ModelTransientError('503')])
            # collect_site reconstructs this exception from the isolated worker's JSON message.
            if isinstance(result, browser_agent.ModelTransientSiteError):
                result = cloud_worker.ModelTransientSiteError(str(result), result.partial)
            outcomes.append(result)
        harness = job_fixtures.CloudJobResilienceTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        store = harness.worker_store(4)
        harness.run_job(store, 4, outcomes)
        self.assertEqual(store.writes, [1, 2, 3])
        self.assertEqual(store.finish_job.call_args.args[2], 'failed')
        self.assertIn('连续 3 家不可用', store.finish_job.call_args.args[3])

    def test_node_process_creation_errors_stop_the_site_as_runtime_failures(self):
        for error in (FileNotFoundError(2, 'node missing', 'node'), PermissionError(13, 'permission denied', 'node')):
            with self.subTest(error=type(error).__name__):
                def factory(folder):
                    browser = object.__new__(browser_agent.Browser)
                    browser.stderr = None
                    browser._spawn('fixture-module', ['cdp', 'http://127.0.0.1:1'])
                with tempfile.TemporaryDirectory() as folder:
                    service = fixtures.ModelService(folder + '/model.json')
                    service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'})
                    config = service.read(); config['tested'] = True; service._write(config)
                    with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', fixtures.URL + 'robots.txt')), patch('browser_agent.subprocess.Popen', side_effect=error):
                        with self.assertRaises(browser_agent.BrowserRuntimeError):
                            browser_agent.run_site(fixtures.URL, 'contacts', service, [], moli_factory=factory, chrome_ready=lambda: False)

    def test_moli_process_creation_error_is_a_runtime_failure(self):
        with tempfile.TemporaryDirectory() as folder, patch('browser_agent.runtime_paths', return_value=('missing-moli', 'fixture-module')), patch('browser_agent.subprocess.Popen', side_effect=FileNotFoundError(2, 'moli missing', 'moli')):
            with self.assertRaises(browser_agent.BrowserRuntimeError):
                browser_agent.Browser(folder)

    def test_model_failure_after_chrome_launch_fallback_still_propagates(self):
        result, _, _ = self.site([fixtures.WEAK], [fixtures.GOOD], [ModelTransientError('503')], chrome_fail=browser_agent.BrowserRuntimeError('Chrome unavailable'))
        self.assertIsInstance(result, browser_agent.ModelTransientSiteError)
        self.assertTrue(result.partial['model_transient'])
        self.assertNotIn('_fallback', result.partial)


if __name__ == '__main__':
    unittest.main()
