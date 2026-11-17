import json
import os
import tempfile
import time
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
        'https://delta.example/robots.txt': 'User-agent: *\nAllow: /',
        'https://delta.example/': '<html><body>delta 站，只做中转</body></html>',
        'https://delta.example/api/status': status('Delta API'),
        'https://delta.example/api/pricing': PRICING,
    }


class DiscoveryUnitTests(unittest.TestCase):
    def test_aliases_come_from_the_saved_rule(self):
        rules = [{'model': 'kimi-k3.1', 'aliases': ['Kimi K3.1', 'K31X']}]
        self.assertEqual(discovery.rules_aliases(rules), ('Kimi K3.1', 'K31X'))
        self.assertTrue(discovery.matches_k31('[Beta]K31X', ('Kimi K3.1', 'K31X')))
        self.assertFalse(discovery.matches_k31('kimi-k3.1-pro', ('K31X',)))

    def test_aliases_fall_back_to_the_built_in_list(self):
        self.assertEqual(discovery.rules_aliases([]), discovery.K31_ALIASES)
        self.assertIn('kimi-k3.1-pro', discovery.rules_aliases([{'model': 'glm-5.3'}]))

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
        with (
            mock.patch.object(discovery, 'fetch_text', site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
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
        # P31: the status payload is stored once; footer/about text is read from it.
        self.assertNotIn('footer_text', result)
        self.assertNotIn('about_text', result)

    def test_probe_without_matching_model(self):
        site = FakeSite(site_pages())
        with (
            mock.patch.object(discovery, 'fetch_text', site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
        ):
            result = discovery.probe_host('beta.example')
        self.assertTrue(result['ok'], result['error'])
        self.assertTrue(result['is_newapi'])
        self.assertEqual(result['k31'], [])

    def test_probe_plain_site_is_not_newapi(self):
        site = FakeSite(site_pages())
        with (
            mock.patch.object(discovery, 'fetch_text', site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
        ):
            result = discovery.probe_host('gamma.example')
        self.assertTrue(result['ok'], result['error'])
        self.assertFalse(result['is_newapi'])
        self.assertEqual(result['k31'], [])

    def test_probe_respects_robots_and_skips_private_requests(self):
        site = FakeSite(site_pages())
        with (
            mock.patch.object(discovery, 'fetch_text', site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
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
        with (
            mock.patch.object(discovery, 'fetch_text', site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
        ):
            result = discovery.probe_host('epsilon.example')
        self.assertIn('robots.txt 无法确认允许访问', result['error'])

    def test_robots_retry_message_names_the_status_code(self):
        for code in (401, 403, 429, 500, 503):
            with (
                self.subTest(code=code),
                mock.patch.object(
                    discovery,
                    'fetch_text',
                    side_effect=urllib.error.HTTPError(
                        'https://alpha.example/robots.txt', code, 'x', {}, None
                    ),
                ),
            ):
                with self.assertRaisesRegex(ValueError, 'HTTP %d' % code) as caught:
                    discovery.robots_for('https://alpha.example')
                self.assertIn('稍后重试', str(caught.exception))

    def test_missing_robots_stays_permissive(self):
        with mock.patch.object(
            discovery,
            'fetch_text',
            side_effect=urllib.error.HTTPError(
                'https://alpha.example/robots.txt', 404, 'Not Found', {}, None
            ),
        ):
            parser = discovery.robots_for('https://alpha.example')
        self.assertTrue(parser.can_fetch('SupplierScout', 'https://alpha.example/'))

    def test_robots_shows_the_real_reason_instead_of_one_blanket_message(self):
        cases = [
            (ValueError('禁止访问内网、回环或保留地址'), '禁止访问内网'),
            (urllib.error.URLError(OSError('禁止访问内网、回环或保留地址')), '禁止访问内网'),
            (urllib.error.URLError('Tunnel connection failed: 502 Bad Gateway'), '本机代理连接失败'),
            (urllib.error.URLError('Tunnel connection failed: 403 Forbidden'), '采集代理拒绝访问该地址'),
            (TimeoutError('timed out'), 'timed out'),
            (ValueError('采集代理 DNS 查询失败'), '采集代理 DNS 查询失败'),
        ]
        for error, expected in cases:
            with (
                self.subTest(error=expected),
                mock.patch.object(discovery, 'fetch_text', side_effect=error),
            ):
                with self.assertRaises(ValueError) as caught:
                    discovery.robots_for('https://alpha.example')
                message = str(caught.exception)
                self.assertIn('robots.txt 无法确认允许访问：', message)
                self.assertIn(expected, message)
                self.assertNotIn('稍后重试', message)


class DiscoverySearchTests(unittest.TestCase):
    def test_hosts_from_text_keeps_absolute_urls_only(self):
        text = '见 https://a.example/x 和 http://b.example/ ，not.example 不算 https://a.example/again'
        self.assertEqual(discovery.hosts_from_text(text), ['a.example', 'b.example'])

    def test_excluded_sites_cover_subdomains(self):
        exclude = ['zhihu.com', 'github.com']
        self.assertTrue(discovery.is_excluded('www.zhihu.com', exclude))
        self.assertTrue(discovery.is_excluded('github.com', exclude))
        self.assertFalse(discovery.is_excluded('notzhihu.com', exclude))
        self.assertFalse(discovery.is_excluded('a.example', exclude))

    def test_search_request_checks_the_keyword_length(self):
        with self.assertRaises(ValueError):
            discovery.search_request('', 'key')
        with self.assertRaises(ValueError):
            discovery.search_request('长' * 129, 'key')

    def test_search_round_collects_hosts_and_stops_at_twenty(self):
        calls = []

        def fake(query, key, count=50):
            calls.append(query)
            return {'results': [{'url': 'https://h%d.example/' % len(calls), 'content': ''}]}

        with mock.patch.object(discovery, 'search_request', fake):
            result = discovery.search_round(['k%d' % index for index in range(25)], 'key')
        self.assertEqual(result['calls'], 20)
        self.assertEqual(len(calls), 20)
        self.assertEqual(len(result['hosts']), 20)
        self.assertEqual(result['hosts'][0]['found_by'], '搜索：k0')

    def test_search_round_reports_an_error_and_continues(self):
        def fake(query, key, count=50):
            if query == 'bad':
                raise ValueError('DeepSeek 搜索失败（HTTP 429）：限流')
            return {'results': [{'url': 'https://good.example/', 'content': ''}]}

        with mock.patch.object(discovery, 'search_request', fake):
            result = discovery.search_round(['bad', 'good'], 'key')
        self.assertEqual(result['calls'], 2)
        self.assertEqual([host['host'] for host in result['hosts']], ['good.example'])
        self.assertIn('429', result['errors'][0])

    def test_snowball_reads_saved_texts_without_fetching(self):
        texts = [
            {
                'label': '滚雪球：https://a.example/',
                'url': 'https://a.example/',
                'text': '链接 https://alpha.example/ 与 https://beta.example/x',
            },
            {'label': '滚雪球：b', 'url': '', 'text': '重复 https://alpha.example/ 和 https://zhihu.com/'},
        ]
        with mock.patch.object(discovery, 'fetch_text', side_effect=AssertionError('不应再抓页面')):
            result = discovery.snowball_hosts(texts, exclude=['zhihu.com'])
        self.assertEqual(result['sources'], 2)
        self.assertEqual([host['host'] for host in result['hosts']], ['alpha.example', 'beta.example'])
        self.assertTrue(result['hosts'][0]['found_by'].startswith('滚雪球：'))


class DiscoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'supplier.db')
        self.store = Store(self.path, seed=False)
        self.site = FakeSite(site_pages())

    def tearDown(self):
        self.tmp.cleanup()

    def run_round(self, source='', queries=None):
        with (
            mock.patch.object(discovery, 'fetch_text', self.site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
        ):
            return self.store.discovery_round(source, queries)

    def test_paste_stores_candidates_with_k31_first(self):
        result = self.run_round('directory.example')
        self.assertEqual(result['found_hosts'], 4)
        self.assertEqual(result['new_hosts'], 4)
        hosts = [row['host'] for row in result['candidates']]
        self.assertEqual(hosts[0], 'alpha.example')
        self.assertEqual(sorted(hosts), ['alpha.example', 'beta.example', 'epsilon.example', 'gamma.example'])
        self.assertEqual(self.store.discovery_list()[0]['k31'][0]['model_name'], '[Alpha]kimi-k3.1-pro')
        row = next(item for item in result['candidates'] if item['host'] == 'gamma.example')
        self.assertEqual(row['found_by'], '粘贴：directory.example')

    def test_every_supplier_host_is_probed(self):
        self.store.import_rows([{'url': 'https://alpha.example/'}, {'url': 'https://beta.example/'}])
        result = self.run_round()
        self.assertEqual(result['probed'], 2)
        rows = {row['host']: row for row in result['candidates']}
        self.assertIn('alpha.example', rows)
        self.assertEqual(rows['alpha.example']['found_by'], '已有名单')
        self.assertEqual(len(rows['alpha.example']['k31']), 2)
        self.assertEqual(rows['alpha.example']['source_url'], 'https://alpha.example')

    def test_supplier_without_findings_stays_out_of_the_list(self):
        self.store.import_rows([{'url': 'https://plain.example/'}])
        result = self.run_round()
        self.assertEqual(result['probed'], 1)
        self.assertNotIn('plain.example', [row['host'] for row in result['candidates']])

    def test_supplier_host_is_not_added_twice(self):
        self.store.import_rows([{'url': 'https://alpha.example/'}])
        result = self.run_round('directory.example')
        hosts = [row['host'] for row in result['candidates']]
        self.assertEqual(hosts.count('alpha.example'), 1)
        self.assertEqual(result['new_hosts'], 3)
        self.assertEqual(self.store.discovery_list()[0]['found_by'], '已有名单')

    def test_rediscovery_updates_probe_but_keeps_status(self):
        self.run_round('directory.example')
        beta = next(row for row in self.store.discovery_list() if row['host'] == 'beta.example')
        self.store.discovery_set_status([beta['id']], 'ignored')
        self.site.pages['https://beta.example/api/pricing'] = PRICING
        self.run_round('directory.example')
        beta = next(row for row in self.store.discovery_list() if row['host'] == 'beta.example')
        self.assertEqual(beta['status'], 'ignored')
        self.assertEqual(len(beta['k31']), 2)

    def test_ignore_keeps_the_record(self):
        self.run_round('directory.example')
        total = len(self.store.discovery_list())
        gamma = next(row for row in self.store.discovery_list() if row['host'] == 'gamma.example')
        self.store.discovery_set_status([gamma['id']], 'ignored')
        rows = self.store.discovery_list()
        self.assertEqual(len(rows), total)
        self.assertEqual(next(row for row in rows if row['id'] == gamma['id'])['status'], 'ignored')

    def test_enqueue_adds_suppliers_and_marks_status(self):
        self.run_round('directory.example')
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
        self.run_round('directory.example')
        alpha = next(row for row in self.store.discovery_list() if row['host'] == 'alpha.example')
        self.store.discovery_set_status([alpha['id']], 'ignored')
        with (
            mock.patch.object(discovery, 'fetch_text', self.site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
        ):
            result = self.store.discovery_recheck([alpha['id']])
        self.assertEqual(result['checked'], 1)
        self.assertEqual(
            next(row for row in result['candidates'] if row['id'] == alpha['id'])['status'],
            'ignored',
        )

    def test_search_results_become_candidates_with_their_keyword(self):
        self.store.search_service.save({'api_key': 'test-key', 'queries': ['kimi-k3.1-pro API 中转 lang:zh']})
        payload = {
            'results': [
                {
                    'url': 'https://delta.example/',
                    'title': 'Delta API',
                    'content': '充值页 https://delta.example/pay 也有说明',
                },
                {'url': 'https://zhihu.com/question/1', 'content': '知乎讨论'},
            ]
        }
        with (
            mock.patch.object(discovery, 'fetch_text', self.site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
            mock.patch.object(
                discovery, 'search_request', mock.Mock(side_effect=lambda query, key, count=50: payload)
            ) as fake,
        ):
            result = self.store.discovery_round()
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(result['search_calls'], 1)
        hosts = [row['host'] for row in result['candidates']]
        self.assertIn('delta.example', hosts)
        self.assertNotIn('zhihu.com', hosts)
        row = next(item for item in result['candidates'] if item['host'] == 'delta.example')
        self.assertEqual(row['found_by'], '搜索：kimi-k3.1-pro API 中转 lang:zh')

    def test_search_result_pages_are_not_fetched_again_for_links(self):
        self.store.search_service.save({'api_key': 'test-key', 'queries': ['kimi-k3.1-pro']})
        payload = {'results': [{'url': 'https://delta.example/', 'content': ''}]}
        with (
            mock.patch.object(discovery, 'fetch_text', self.site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
            mock.patch.object(
                discovery, 'search_request', mock.Mock(side_effect=lambda query, key, count=50: payload)
            ),
            # Crawling a page again would go through discover(); snowball must not call it.
            mock.patch.object(discovery, 'discover', side_effect=AssertionError('不应重抓页面')),
        ):
            self.store.discovery_round()
        self.assertIn('delta.example', [row['host'] for row in self.store.discovery_list()])

    def test_snowball_reads_saved_sources_without_fetching_pages(self):
        self.store.import_rows([{'url': 'https://alpha.example/'}])
        sources = [
            {'url': 'https://alpha.example/', 'excerpt': '友链 https://delta.example/ 与 https://zhihu.com/x'}
        ]
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT id FROM suppliers WHERE url LIKE 'https://alpha.example%'").fetchone()
            db.execute('UPDATE suppliers SET sources=? WHERE id=?', (json.dumps(sources), row['id']))
        with mock.patch.object(discovery, 'discover', side_effect=AssertionError('不应再抓页面')):
            summary = self.store.discovery_round()
        rows = {row['host']: row for row in summary['candidates']}
        self.assertIn('delta.example', rows)
        self.assertTrue(rows['delta.example']['found_by'].startswith('滚雪球：'))
        self.assertNotIn('zhihu.com', rows)
        self.assertGreaterEqual(summary['snowball_sources'], 1)

    def test_run_without_search_key_skips_search(self):
        result = self.run_round()
        self.assertEqual(result['search_calls'], 0)
        self.assertIn('未接入 DeepSeek 搜索 Key', result['skipped'][0])
        self.assertEqual(result['candidates'], [])

    def test_round_uses_the_aliases_of_the_saved_rule(self):
        self.store._save_discovery([{'host': 'alpha.example'}])
        seen = []

        def fake_probe(hosts, aliases=None, **kwargs):
            seen.append(list(aliases))
            return []

        empty = {'calls': 0, 'hosts': [], 'errors': []}
        with (
            mock.patch.object(
                self.store, 'rules', return_value=[{'model': 'kimi-k3.1', 'aliases': ['Kimi K3.1', 'K31X']}]
            ),
            mock.patch.object(discovery, 'probe_hosts', fake_probe),
            mock.patch.object(discovery, 'search_round', lambda *a, **k: empty),
            mock.patch.object(
                discovery, 'snowball_hosts', lambda *a, **k: {'sources': 0, 'hosts': [], 'errors': []}
            ),
        ):
            self.store.discovery_round()
        self.assertEqual(seen[0], ['Kimi K3.1', 'K31X'])

    def test_start_runs_in_the_background_and_reports_progress(self):
        self.store._save_discovery([{'host': 'alpha.example'}, {'host': 'beta.example'}])

        def fake_probe(hosts, aliases=None, **kwargs):
            progress = kwargs.get('progress')
            for index, host in enumerate(hosts, 1):
                time.sleep(0.03)
                if progress:
                    progress(index, len(hosts), '探测：' + host)
            return [{'host': host, 'ok': True} for host in hosts]

        empty = {'calls': 0, 'hosts': [], 'errors': []}
        with (
            mock.patch.object(discovery, 'probe_hosts', fake_probe),
            mock.patch.object(discovery, 'search_round', lambda *a, **k: empty),
            mock.patch.object(
                discovery, 'snowball_hosts', lambda *a, **k: {'sources': 0, 'hosts': [], 'errors': []}
            ),
        ):
            started = self.store.discovery_start()
            self.assertTrue(started['started'])
            self.assertEqual(self.store.discovery_progress()['status'], 'running')
            with self.assertRaises(ValueError):
                self.store.discovery_start()
            deadline = time.time() + 5
            while self.store.discovery_progress()['status'] == 'running' and time.time() < deadline:
                time.sleep(0.02)
        progress = self.store.discovery_progress()
        self.assertEqual(progress['status'], 'done')
        self.assertEqual(progress['stage'], '完成')
        self.assertTrue(progress['summary'])
        self.assertGreaterEqual(progress['total'], 1)

    def test_background_failure_is_reported_instead_of_sticking(self):
        def boom(*args, **kwargs):
            raise RuntimeError('探测接口挂了')

        with mock.patch.object(discovery, 'probe_hosts', boom):
            self.store.discovery_start()
            deadline = time.time() + 5
            while self.store.discovery_progress()['status'] == 'running' and time.time() < deadline:
                time.sleep(0.02)
        progress = self.store.discovery_progress()
        self.assertEqual(progress['status'], 'failed')
        self.assertIn('探测接口挂了', progress['error'])

    def test_paste_runs_in_the_background_too(self):
        with (
            mock.patch.object(discovery, 'fetch_text', self.site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
        ):
            started = self.store.discovery_start('directory.example')
            self.assertTrue(started['started'])
            deadline = time.time() + 5
            while self.store.discovery_progress()['status'] == 'running' and time.time() < deadline:
                time.sleep(0.02)
        progress = self.store.discovery_progress()
        self.assertEqual(progress['status'], 'done', progress)
        rows = self.store.discovery_list()
        self.assertIn('alpha.example', {row['host'] for row in rows})
        self.assertTrue(all(row['found_by'].startswith('粘贴：') for row in rows))

    def test_dead_paste_keeps_earlier_results_and_reports_the_error(self):
        self.store.import_rows([{'url': 'https://alpha.example/'}])
        with (
            mock.patch.object(discovery, 'fetch_text', self.site.fetch),
            mock.patch.object(discovery, 'validate_url', lambda url: url),
            mock.patch.object(discovery, 'discover', side_effect=ValueError('robots.txt 不允许访问该来源页')),
        ):
            self.store.discovery_start('directory.example')
            deadline = time.time() + 5
            while self.store.discovery_progress()['status'] == 'running' and time.time() < deadline:
                time.sleep(0.02)
        progress = self.store.discovery_progress()
        self.assertEqual(progress['status'], 'done', progress)
        errors = progress['summary']['errors']
        self.assertTrue(any('robots.txt' in item for item in errors), errors)
        rows = {row['host']: row for row in self.store.discovery_list()}
        self.assertIn('alpha.example', rows)
        self.assertEqual(rows['alpha.example']['found_by'], '已有名单')

    def test_snowball_mines_the_saved_status_payload(self):
        pages = site_pages()
        pages['https://alpha.example/api/status'] = json.dumps(
            {
                'success': True,
                'data': {
                    'system_name': 'Alpha API',
                    'footer_html': '友链 <a href="https://theta.example/">theta</a>',
                    'announcements': '欢迎',
                },
            }
        )
        for name in ('robots.txt', '', 'api/status'):
            pages['https://theta.example/' + name] = pages['https://delta.example/' + name]
        self.site = FakeSite(pages)
        self.store.import_rows([{'url': 'https://alpha.example/'}])
        summary = self.run_round()
        rows = {row['host']: row for row in summary['candidates']}
        self.assertIn('theta.example', rows)
        self.assertEqual(rows['theta.example']['found_by'], '滚雪球：alpha.example/api/status')

    def test_state_has_no_discovery_progress_and_keeps_search(self):
        state = self.store.state(full=False)
        self.assertNotIn('discovery', state)
        self.assertIn('search_service', state)

    def test_handler_is_local_only_by_default(self):
        self.assertTrue(web_app.Handler.LOCAL_ONLY)

    def test_existing_workspace_gains_the_table(self):
        Store(self.path, seed=False)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            columns = {row[1] for row in db.execute('PRAGMA table_info(discovery_candidates)')}
        self.assertTrue({'host', 'source_url', 'k31', 'status', 'found_by'} <= columns)

    def test_old_discovery_table_gains_found_by_keeping_rows(self):
        path = os.path.join(self.tmp.name, 'old.db')
        import sqlite3

        db = sqlite3.connect(path)
        db.executescript(
            '''CREATE TABLE discovery_candidates(id INTEGER PRIMARY KEY,host TEXT NOT NULL UNIQUE,
            source_url TEXT NOT NULL DEFAULT '',discovered_at TEXT NOT NULL,
            is_newapi INTEGER NOT NULL DEFAULT 0,k31 TEXT NOT NULL DEFAULT '[]',
            selfhost_label TEXT NOT NULL DEFAULT '',selfhost_sentence TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',probe TEXT NOT NULL DEFAULT '{}',updated_at TEXT);
            INSERT INTO discovery_candidates(host,discovered_at,status) VALUES('old.example','2026-01-01','ignored');
            PRAGMA user_version=3;'''
        )
        db.commit()
        db.close()
        store = Store(path, seed=False)
        rows = store.discovery_list()
        self.assertEqual([row['host'] for row in rows], ['old.example'])
        self.assertEqual(rows[0]['status'], 'ignored')
        self.assertEqual(rows[0]['found_by'], '')
        with store.connect() as check:
            self.assertEqual(check.execute('PRAGMA user_version').fetchone()[0], 4)


if __name__ == '__main__':
    unittest.main()
