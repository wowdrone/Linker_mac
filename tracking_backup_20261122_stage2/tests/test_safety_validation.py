import unittest
from safety_validation import evaluate_item


class SafetyValidationTest(unittest.TestCase):
    def setUp(self):
        self.item = {'parameter': 'LIMIT', 'severity': 'error', 'criteria': {'min': 10, 'max': 20}}

    def test_within_range(self):
        self.assertEqual(evaluate_item(self.item, {'LIMIT': 15})['status'], 'ok')

    def test_outside_range(self):
        self.assertEqual(evaluate_item(self.item, {'LIMIT': 21})['status'], 'error')

    def test_missing_complete_and_incomplete(self):
        self.assertEqual(evaluate_item(self.item, {})['status'], 'na')
        self.assertEqual(evaluate_item(self.item, {}, complete=False)['status'], 'pending')

    def test_type_mismatch(self):
        self.assertEqual(evaluate_item(self.item, {'LIMIT': '15'})['status'], 'error')

    def test_unconfigured_criterion(self):
        self.item['criteria'] = None
        self.assertEqual(evaluate_item(self.item, {'LIMIT': 15})['status'], 'unconfigured')
