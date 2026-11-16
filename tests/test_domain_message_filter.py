import unittest

from t6.domain.message_filter import FILTER_RULES, FilterRule, verdict


class FilterRuleTests(unittest.TestCase):
    def test_job_order_rule_is_in_the_table(self) -> None:
        rule = FILTER_RULES['job_order']
        self.assertIsInstance(rule, FilterRule)
        self.assertEqual(rule.name, 'job_order')
        self.assertEqual(set(rule.criteria), {'match', 'maybe', 'no'})
        self.assertEqual((rule.pass_at, rule.drop_at), (0.5, 0.9))
        self.assertNotIn('{', rule.question)

    def test_verdict_from_thresholds(self) -> None:
        self.assertEqual(verdict({'match': 0.5, 'maybe': 0.3, 'no': 0.2}, 0.5, 0.9), 'pass')
        self.assertEqual(verdict({'match': 0.05, 'maybe': 0.05, 'no': 0.9}, 0.5, 0.9), 'drop')
        self.assertEqual(verdict({'match': 0.2, 'maybe': 0.3, 'no': 0.5}, 0.5, 0.9), 'unsure')
        self.assertEqual(verdict({'match': 0.2, 'maybe': 0.3, 'no': 0.5}, 0.5, 0.5), 'drop')
        self.assertEqual(verdict({'match': 0.4, 'maybe': 0.3, 'no': 0.3}, 0.3, 0.9), 'pass')
