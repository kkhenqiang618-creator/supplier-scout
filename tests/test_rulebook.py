import unittest
from engine import evaluate,validate_rules
from rulebook import A_RULES

class RulebookTests(unittest.TestCase):
    def test_a_rules_can_be_saved_without_inventing_official_prices(self):
        self.assertEqual(len(validate_rules(A_RULES)),10)
        self.assertEqual(next(r for r in A_RULES if r['model']=='glm-5.3')['max_ratio'],.4)
        self.assertEqual(next(r for r in A_RULES if r['model']=='deepseek-v4.1')['max_ratio'],.45)
    def test_empty_glm53_threshold_requires_review(self):
        rules=[dict(r,max_ratio=None) if r['model']=='glm-5.3' else r for r in A_RULES]
        result=evaluate({'model':'glm-5.3','ratio':.3},rules)
        self.assertEqual(result['status'],'review')
    def test_glm51_and_glm52_follow_resource_not_version(self):
        for model in ['glm-5.1','glm-5.2']:
            self.assertEqual(evaluate({'model':model,'ratio':.5,'resource_form':'自部署'},A_RULES)['status'],'skipped')
            self.assertEqual(evaluate({'model':model,'ratio':.5,'resource_form':'官转'},A_RULES)['status'],'qualified')
            self.assertEqual(evaluate({'model':model,'ratio':.5},A_RULES)['status'],'review')

    def test_threshold_is_upper_bound_including_low_prices(self):
        self.assertEqual(evaluate({'model':'kimi-k3','ratio':.2,'resource_form':'自部署'},A_RULES)['status'],'qualified')
        self.assertEqual(evaluate({'model':'kimi-k3','ratio':.46,'resource_form':'自部署'},A_RULES)['status'],'skipped')
    def test_quoted_price_without_reference_is_preserved_for_review(self):
        result=evaluate({'model':'deepseek-v4','input_price':.610755,'currency':'USD','unit':'1M tokens'},A_RULES)
        self.assertEqual(result['status'],'review')
        self.assertEqual(result['input_price'],.610755)

if __name__=='__main__':unittest.main()
