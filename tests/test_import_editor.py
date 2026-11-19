"""Run: python -m unittest discover -s tests. Only synthetic data."""
import unittest
from decimal import Decimal
from app.import_editing import validate_changes


class ValidationTests(unittest.TestCase):
    def test_clearing(self):
        self.assertEqual(validate_changes({'email': ' ', 'revenue_rub': None}), {'email': None, 'revenue_rub': None})

    def test_normalization(self):
        self.assertEqual(validate_changes({'email': ' Person@Example.test ', 'revenue_rub': '120 000 000,25', 'revenue_year': '2025'}),
                         {'email': 'person@example.test', 'revenue_rub': Decimal('120000000.25'), 'revenue_year': 2025})

    def test_invalid(self):
        for changes in [{'email':'broken'}, {'phone':'abc1234567'}, {'inn':'0000000000'},
                        {'inn':'1234567890'}, {'revenue_year':'2025.5'}, {'revenue_rub':'NaN'},
                        {'revenue_rub':'Infinity'}, {'revenue_rub':'-1'}, {'revenue_rub':'0.001'},
                        {'revenue_rub':True}, {'company_id':123}, {'notes':{}}, {'notes':'a'*4001}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_changes(changes)

    def test_synthetic_checksum(self):
        # Artificial checksum-valid identifier, not a real investor record.
        prefix = '123456789'
        check = sum(int(x)*w for x,w in zip(prefix, (2,4,10,3,5,9,4,6,8))) % 11 % 10
        value = prefix + str(check)
        self.assertEqual(validate_changes({'inn': value}), {'inn': value})


if __name__ == '__main__':
    unittest.main()
