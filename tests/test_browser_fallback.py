import unittest
from unittest.mock import patch
from engine import crawl

class BrowserFallbackTests(unittest.TestCase):
    def test_js_shell_uses_rendered_page_without_inventing_prices(self):
        def fetch(url):
            if url.endswith('/robots.txt'): return 'User-agent: *\nAllow: /',url
            return '<html><body><div id="root"></div><script src="/app.js"></script></body></html>',url
        with patch('engine.validate_url',side_effect=lambda url:url),patch('engine.fetch',side_effect=fetch),patch('engine.render_page',return_value=('<p>DeepSeek-V3 3.5折</p><p>邮箱 sales@example.com</p>','https://example.com')):
            result=crawl('https://example.com')
        self.assertEqual(result['contacts']['email'],['sales@example.com'])
        self.assertEqual(result['offers'][0]['ratio'],.35)
        self.assertEqual(result['sources'][0]['method'],'浏览器加载')
    def test_browser_failure_preserves_static_evidence_and_explains_failure(self):
        def fetch(url): return ('User-agent: *\nAllow: /' if url.endswith('/robots.txt') else '<div>登录</div><script src="/app.js"></script>'),url
        with patch('engine.validate_url',side_effect=lambda url:url),patch('engine.fetch',side_effect=fetch),patch('engine.render_page',side_effect=ValueError('浏览器加载失败：超时')):
            result=crawl('https://example.com')
        self.assertEqual(result['sources'][0]['excerpt'],'登录')
        self.assertIn('超时',result['warnings'][0])
        self.assertEqual(result['offers'],[])

if __name__=='__main__': unittest.main()
