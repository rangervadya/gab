import unittest
from app.mailing_audience import decorate_recipient, in_scope


class AudienceTests(unittest.TestCase):
    def evidence(self, **changes):
        return {'revenue_evidence':dict(inn='1234567890',entity_type='company',
            identity_confirmed=True,status_code='001',year=2025,amount_rub=100000000,
            source_url='https://example.test/financials',**changes)}

    def test_threshold_inclusive_and_all_retains_unknown(self):
        self.assertTrue(in_scope(self.evidence(),'revenue_100m'))
        self.assertFalse(in_scope({},'revenue_100m'))
        self.assertTrue(in_scope({},'all'))

    def test_reject_unverified_inactive_individual_and_invalid_evidence(self):
        changes=[{'identity_confirmed':False},{'entity_type':'entrepreneur'},
                 {'status_code':'closed'},{'amount_rub':'NaN'},{'amount_rub':'Infinity'},
                 {'amount_rub':99999999},{'year':True},{'source_url':'javascript:alert(1)'},
                 {'inn':'123456789012'}]
        for values in changes:
            with self.subTest(values=values):
                row=self.evidence();row['revenue_evidence'].update(values)
                self.assertFalse(decorate_recipient(row)['revenue_segment_eligible'])
        for value in ['not an object',[],True]:
            self.assertFalse(in_scope({'revenue_evidence':value},'revenue_100m'))


if __name__=='__main__':unittest.main()
