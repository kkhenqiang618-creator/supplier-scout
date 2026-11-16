import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import discovery
import web_app
from local_store import Store


def page(host, body):
    return body


class FakeSite:
    """An in-memory HTTP site; tests never touch a real network."""

    def __init__(self, pages=None, errors=None):
        self.pages = dict(pages or {})
        self.errors = dict(errors or {})
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        code = self.errors.get(url)
        if code:
            raise urllib.error.HTTPError(url, code, 'error', {}, None)
        if url in self.pages:
            return self.pages[url], url
        raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)


def status(system_name='Alpha API'):
    return json.dumps(
        {
            'success': True,
            'data': {
                'system_name': system_name,
                'price': 1.0,
                'quota_per_unit': 500000,
                'announcements': 'K3.1 Pro 已上架，欢迎测试',
            },
        }
    )


PRICING = json.dumps(
    {
        'success': True,
        'data': [
            {
                'model_name': '[Alpha]kimi-k3.1-pro',
                'model_ratio': 0.75,
                'completion_ratio': 4,
                'cache_ratio': 0.25,
                'model_price': 0,
                'quota_type': 0,
                'enable_groups': ['default', 'vip'],
            },
            {
                'model_name': 'kimi-k3.1-pro-highspeed',
                'model_ratio': 1.5,
                'completion_ratio': 2,
                'cache_ratio': 0.5,
                'quota_type': 0,
                'enable_groups': ['default'],
            },
            {'model_name': 'gpt-4o', 'model_ratio': 1},
        ],
        'group_ratio': {'default': 1, 'vip': 0.8},
    }
)

DIRECTORY = """<html><body>
<a href="https://alpha.example">alpha</a>
<a href="https://alpha.example/pricing">alpha again</a>
<a href="http://www.beta.example/x">beta</a>
<a href="https://gamma.example/">gamma</a>
<a href="https://epsilon.example/">epsilon</a>
<a href="/about">self</a>
</body></html>"""

HOMEPAGE = '<html><body><p>我们提供 B300 算力集群，现货可谈。</p></body></html>'


def site_pages(alpha_home=HOMEPAGE):
    return {
        'https://directory.example/': DIRECTORY,
        'https://alpha.example/robots.txt': 'User-agent: *\nAllow: /',
        'https://alpha.example/': alpha_home,
        'https://alpha.example/api/status': status('Alpha API'),
        'https://alpha.example/api/pricing': PRICING,
        'https://beta.example/robots.txt': 'User-agent: *\nAllow: /',
        'https://beta.example/': '<html><body>beta 站，没有自部署信息</body></html>',
        'https://beta.example/api/status': status('Beta API'),
        'https://beta.example/api/pricing': json.dumps(
            {'success': True, 'data': [{'model_name': 'gpt-4o', 'model_ratio': 1}]}
        ),
        'https://gamma.example/robots.txt': 'User-agent: *\nAllow: /',
        'https://gamma.example/': '<html><body>普通网站</body></html>',
        'https://gamma.example/api/status': json.dumps({'ok': True, 'version': '1.0'}),
        'https://epsilon.example/robots.txt': 'User-agent: *\nDisallow: /\n',
    }


class DiscoveryUnitTests(unittest.TestCase):
    def test_normalize_source_accepts_domain_and_url(self):
        with mock.patch.object(discovery, 'validate_url', lambda url: url):
            self.assertEqual(discovery.normalize_source('example.com'), 'https://example.com/')
            self.assertEqual(
                discovery.normalize_source('https://a.example/list?page=2'),
                'https://a.example/list?page=2',
            )
        with self.assertRaises(ValueError):
            discovery.normalize_source('   ')

    def test_hosts_from_page_dedupes_and_drops_source_host(self):
        hosts = discovery.hosts_from_page(DIRECTORY, 'https://directory.example/')
        self.assertEqual(hosts, ['alpha.example', 'beta.example', 'gamma.example', 'epsilon.example'])
        self.assertEqual(len(discovery.hosts_from_page(DIRECTORY, 'https://directory.example/')), 4)

    def test_hosts_are_capped_at_two_hundred(self):
        html = ''.join('<a href="https://h%d.example/">x</a>' % index for index in range(250))
        self.assertEqual(len(discovery.hosts_from_page(html, 'https://directory.example/')), 200)

    def test_selfhost_hits_returns_label_and_sentence(self):
        label, sentence = discovery.selfhost_hits('我们提供 B300 算力集群，现货可谈。')
        self.assertEqual(label, '疑似自部署，需询问')
        self.assertIn('B300', sentence)
        self.assertEqual(discovery.selfhost_hits('普通介绍页'), ('', ''))

    def test_model_alias_matching(self):
        self.assertTrue(discovery.matches_k31('kimi-k3.1-pro'))
        self.assertTrue(discovery.matches_k31('[某站]kimi-k3.1-pro'))
        self.assertTrue(discovery.matches_k31('Kimi-K3.1-Pro-Highspeed'))
        self.assertFalse(discovery.matches_k31('kimi-k3.1-prox'))
        self.assertFalse(discovery.matches_k31('kimi-k2.5-pro'))

    def test_unit_prices_use_model_ratio_and_completion(self):
        prices = discovery.unit_prices({'model_ratio': 0.75, 'completion_ratio': 4})
        self.assertEqual(prices['input_price'], 1.5)
        self.assertEqual(prices['output_price'], 6.0)
        self.assertEqual(prices['currency'], 'USD')
        self.assertIn('未含充值比例和分组倍率', prices['basis'])
        self.assertIsNone(discovery.unit_prices({'completion_ratio': 4}))

    def test_probe_alpha_records_everything_verbatim(self):
        site = FakeSite(site_pages())
        with mock.patch.object(discovery, 'fetch_text', site.fetch), mock.patch.object(
            discovery, 'validate_url', lambda url: url
        ):
            result = discovery.probe_host('alpha.example')
        self.assertTrue(result['ok'], result['error'])
        self.assertTrue(result['is_newapi'])
        self.assertEqual(result['system_name'], 'Alpha API')
        self.assertEqual(result['price'], 1.0)
        self.assertEqual(result['quota_per_unit'], 500000)
        self.assertIn('K3.1 Pro', result['announcement'])
        self.assertEqual(result['selfhost_label'], '疑似自部署，需询问')
        self.assertIn('B300', result['selfhost_sentence'])
        self.assertEqual(result['group_ratio'], {'default': 1, 'vip': 0.8})
        names = [entry['model_name'] for entry in result['k31']]
        self.assertEqual(names, ['[Alpha]kimi-k3.1-pro', 'kimi-k3.1-pro-highspeed'])
        first, second = result['k31']
        self.assertEqual(first['model_ratio'], 0.75)
        self.assertEqual(first['completion_ratio'], 4)
        self.assertEqual(first['cache_ratio'], 0.25)
        self.assertEqual(first['quota_type'], 0)
        self.assertEqual(first['enable_groups'], ['default', 'vip'])
        self.assertEqual(first['unit_price']['input_price'], 1.5)
        self.assertEqual(first['unit_price']['output_price'], 6.0)
        self.assertEqual(second['unit_price']['input_price'], 3.0)
        self.assertEqual(second['unit_price']['output_price'], 6.0)
        self.assertEqual(result['status_raw']['data']['system_name'], 'Alpha API')

    def test_probe_without_matching_model(self):
        site = FakeSite(site_pages())
        with mock.patch.object(discovery, 'fetch_text', site.fetch), mock.patch.object(
            discovery, 'validate_url', lambda url: url
        ):
            result = discovery.probe_host('beta.example')
        self.assertTrue(result['ok'], result['error'])
        self.assertTrue(result['is_newapi'])
        self.assertEqual(result['k31'], [])

    def test_probe_plain_site_is_not_newapi(self):
        site = FakeSite(site_pages())
        with mock.patch.object(discovery, 'fetch_text', site.fetch), mock.patch.object(
            discovery, 'validate_url', lambda url: url
        ):
            result = discovery.probe_host('gamma.example')
        self.assertTrue(result['ok'], result['error'])
        self.assertFalse(result['is_newapi'])
        self.assertEqual(result['k31'], [])

    def test_probe_respects_robots_and_skips_private_requests(self):
        site = FakeSite(site_pages())
        with mock.patch.object(discovery, 'fetch_text', site.fetch), mock.patch.object(
            discovery, 'validate_url', lambda url: url
        ):
            result = discovery.probe_host('epsilon.example')
        self.assertFalse(result['ok'])
        self.assertIn('robots.txt', result['error'])
        self.assertEqual(
            [url for url in site.calls if '/api/' in url], [], '被 robots 拒绝的主机不应请求接口'
        )

    def test_robots_server_error_is_reported_not_ignored(self):
        pages = site_pages()
        site = FakeSite(pages, {'https://epsilon.example/robots.txt': 503})
        with mock.patch.object(discovery, 'fetch_text', site.fetch), mock.patch.object(
            discovery, 'validate_url', lambda url: url
        ):
            result = discovery.probe_host('epsilon.example')
        self.assertIn('robots.txt 无法确认允许访问', result['error'])


class DiscoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'supplier.db')
        self.store = Store(self.path, seed=False)
        self.site = FakeSite(site_pages())

    def tearDown(self):
        self.tmp.cleanup()

    def discover(self, source='directory.example'):
        with mock.patch.object(discovery, 'fetch_text', self.site.fetch), mock.patch.object(
            discovery, 'validate_url', lambda url: url
        ):
            return self.store.discovery_discover(source)

    def test_discover_stores_candidates_with_k31_first(self):
        result = self.discover()
        self.assertEqual(result['found_hosts'], 4)
        self.assertEqual(result['new_hosts'], 4)
        hosts = [row['host'] for row in result['candidates']]
        self.assertEqual(hosts[0], 'alpha.example')
        self.assertEqual(
            sorted(hosts), ['alpha.example', 'beta.example', 'epsilon.example', 'gamma.example']
        )
        self.assertEqual(self.store.discovery_list()[0]['k31'][0]['model_name'], '[Alpha]kimi-k3.1-pro')

    def test_known_supplier_hosts_are_not_added(self):
        self.store.import_rows([{'url': 'https://alpha.example/'}])
        result = self.discover()
        self.assertEqual(result['known_hosts'], 1)
        self.assertEqual(result['new_hosts'], 3)
        self.assertNotIn('alpha.example', [row['host'] for row in result['candidates']])

    def test_rediscovery_updates_probe_but_keeps_status(self):
        self.discover()
        beta = next(row for row in self.store.discovery_list() if row['host'] == 'beta.example')
        self.store.discovery_set_status([beta['id']], 'ignored')
        self.site.pages['https://beta.example/api/pricing'] = PRICING
        self.discover()
        beta = next(row for row in self.store.discovery_list() if row['host'] == 'beta.example')
        self.assertEqual(beta['status'], 'ignored')
        self.assertEqual(len(beta['k31']), 2)

    def test_ignore_keeps_the_record(self):
        self.discover()
        total = len(self.store.discovery_list())
        gamma = next(row for row in self.store.discovery_list() if row['host'] == 'gamma.example')
        self.store.discovery_set_status([gamma['id']], 'ignored')
        rows = self.store.discovery_list()
        self.assertEqual(len(rows), total)
        self.assertEqual(next(row for row in rows if row['id'] == gamma['id'])['status'], 'ignored')

    def test_enqueue_adds_suppliers_and_marks_status(self):
        self.discover()
        beta = next(row for row in self.store.discovery_list() if row['host'] == 'beta.example')
        result = self.store.discovery_enqueue([beta['id']])
        self.assertEqual(result['added'], 1)
        self.assertIn(
            'https://beta.example',
            [row['url'].rstrip('/') for row in self.store.state()['suppliers']],
        )
        self.assertEqual(
            next(row for row in self.store.discovery_list() if row['id'] == beta['id'])['status'],
            'enqueued',
        )
        again = self.store.discovery_enqueue([beta['id']])
        self.assertEqual(again['added'], 0)
        self.assertEqual(again['duplicates'], 1)

    def test_recheck_keeps_status(self):
        self.discover()
        alpha = next(row for row in self.store.discovery_list() if row['host'] == 'alpha.example')
        self.store.discovery_set_status([alpha['id']], 'ignored')
        with mock.patch.object(discovery, 'fetch_text', self.site.fetch), mock.patch.object(
            discovery, 'validate_url', lambda url: url
        ):
            result = self.store.discovery_recheck([alpha['id']])
        self.assertEqual(result['checked'], 1)
        self.assertEqual(
            next(row for row in result['candidates'] if row['id'] == alpha['id'])['status'],
            'ignored',
        )

    def test_handler_is_local_only_by_default(self):
        self.assertTrue(web_app.Handler.LOCAL_ONLY)

    def test_existing_workspace_gains_the_table(self):
        Store(self.path, seed=False)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
            columns = {row[1] for row in db.execute('PRAGMA table_info(discovery_candidates)')}
        self.assertTrue({'host', 'source_url', 'k31', 'status'} <= columns)


if __name__ == '__main__':
    unittest.main()
