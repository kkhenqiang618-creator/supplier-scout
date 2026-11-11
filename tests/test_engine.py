import unittest
from engine import evaluate, extract_page, validate_url, validate_rules, DEFAULT_RULES


class EngineTests(unittest.TestCase):
    def test_discount_boundaries_and_uncertainty(self):
        for ratio, expected in [(0.3,'qualified'),(0.4,'qualified'),(0.45,'review'),(0.7,'skipped'),(0.1,'review')]:
            with self.subTest(ratio=ratio):
                result = evaluate({'model':'deepseek-v3','ratio':ratio,'basis':'published_discount'}, DEFAULT_RULES)
                self.assertEqual(result['status'], expected)

    def test_unit_or_currency_mismatch_requires_review(self):
        offer = {'model':'deepseek-v3','input_price':0.7,'currency':'USD','unit':'1M tokens'}
        self.assertEqual(evaluate(offer,DEFAULT_RULES)['status'],'review')
        offer.update(currency='CNY', unit='1K tokens')
        self.assertEqual(evaluate(offer,DEFAULT_RULES)['status'],'review')

    def test_missing_or_invalid_price_is_never_qualified(self):
        for offer in [{'model':'deepseek-v3'}, {'model':'deepseek-v3','ratio':-1}, {'model':'unknown','ratio':0.35}]:
            self.assertEqual(evaluate(offer,DEFAULT_RULES)['status'],'review')

    def test_html_contacts_and_model_discount_have_evidence(self):
        page = extract_page('<style>.x{}</style><p>DeepSeek-V3 全模型 3.5折</p><a href="mailto:sales@example.com">销售</a><p>微信: cloud_sales QQ: 12345678</p><a href="https://t.me/cloudsales">Telegram</a><script>fake@example.net</script>', 'https://example.com/pricing')
        self.assertIn('sales@example.com',page['contacts']['email'])
        self.assertNotIn('fake@example.net',page['contacts']['email'])
        self.assertIn('cloud_sales',page['contacts']['wechat'])
        self.assertEqual(page['offers'][0]['ratio'],0.35)
        self.assertIn('3.5折',page['offers'][0]['evidence'])
        self.assertEqual(page['offers'][0]['source'],'https://example.com/pricing')

    def test_ambiguous_multiple_models_are_not_assigned_same_discount(self):
        page=extract_page('<p>DeepSeek-V3 DeepSeek-R1 起步3折，具体价格请咨询</p>','https://example.com')
        self.assertEqual(page['offers'],[])

    def test_starting_discount_is_not_an_unconditional_quote(self):
        for line in ['DeepSeek-V3 3.5折起','DeepSeek-V3 3.5 折起，详情咨询']:
            with self.subTest(line=line):
                self.assertEqual(extract_page('<p>'+line+'</p>','https://example.com')['offers'],[])

    def test_public_urls_only(self):
        for url in ['file:///etc/passwd','http://127.0.0.1','http://[::1]','http://169.254.169.254','http://localhost','http://user:pass@example.com']:
            with self.subTest(url=url), self.assertRaises(ValueError): validate_url(url)

    def test_invalid_rules_rejected(self):
        with self.assertRaises(ValueError): validate_rules([{'model':'x','min_ratio':0.5,'max_ratio':0.3}])

if __name__ == '__main__': unittest.main()
