import tempfile
import unittest
import threading
import model_service
from pathlib import Path
from unittest.mock import patch
from model_service import ModelService, ModelTransientError


class ConnectionTestStatusTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.service = ModelService(Path(self.folder.name) / 'provider.json')
        self.service.save({'base_url': 'https://provider.example/v1', 'model': 'm', 'api_key': 'test-secret'})
        self.discovery = patch.object(self.service, 'models', return_value=['m'])
        self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def test_failed_retest_invalidates_past_success_and_records_latest_result(self):
        with patch.object(self.service, '_complete_once', return_value='OK'):
            self.service.test()
        with patch.object(self.service, '_complete_once', side_effect=ModelTransientError('连接失败', phase='request')):
            with self.assertRaises(ModelTransientError):
                self.service.test()
        public = self.service.public()
        self.assertFalse(public['tested'])
        self.assertEqual(public.get('last_test_status'), 'failed')
        self.assertTrue(public.get('last_test_at'))
        self.assertNotIn('test-secret', str(public))

    def test_transient_connection_failure_gets_one_recovery_attempt(self):
        with patch.object(self.service, '_complete_once', side_effect=[ModelTransientError('连接失败', phase='request'), 'OK']):
            result = self.service.test()
        self.assertEqual(result['reply'], 'OK')
        self.assertTrue(self.service.public()['tested'])
        self.assertEqual(self.service.public().get('last_test_status'), 'passed')

    def test_slow_response_does_not_gain_another_45_second_attempt(self):
        error = ModelTransientError('等待回复超时', phase='response')
        with patch.object(self.service, '_complete_once', side_effect=[error, 'OK']):
            with self.assertRaises(ModelTransientError):
                self.service.test()

    def test_old_failure_cannot_invalidate_a_new_configuration(self):
        def changed(*args, **kwargs):
            self.service.save({'base_url': 'https://provider.example/v1', 'model': 'new-model', 'api_key': ''})
            current = self.service.read()
            current['tested'] = True
            self.service._write(current)
            raise ModelTransientError('等待回复超时', phase='response')
        with patch.object(self.service, '_complete_once', side_effect=changed):
            with self.assertRaises(ModelTransientError):
                self.service.test()
        self.assertEqual(self.service.public()['model'], 'new-model')
        self.assertTrue(self.service.public()['tested'])

    def test_overall_deadline_records_failure_and_ignores_late_success(self):
        release = threading.Event()
        finished = threading.Event()
        def slow(*args, **kwargs):
            release.wait(1)
            finished.set()
            return 'OK'
        try:
            with patch.object(model_service, 'TEST_NETWORK_TIMEOUT', .03, create=True), patch.object(self.service, '_complete_once', side_effect=slow):
                with self.assertRaisesRegex(ModelTransientError, '测试总时限'):
                    self.service.test()
                self.assertFalse(self.service.public()['tested'])
                release.set()
                self.assertTrue(finished.wait(1))
            self.assertEqual(self.service.public()['last_test_status'], 'failed')
            self.assertFalse(self.service.public()['tested'])
        finally:
            release.set()
