"""Evidence and budget invariants across multi-check acceptance contracts."""
import copy
import unittest

from olcr_api.coding_verification import (retained_semantic_review_ids, scope_verification_candidate,
    verification_budget_stop_reason, verification_source_excerpts)


class VerificationInvariantTests(unittest.TestCase):
    def test_large_source_excerpts_include_both_ends_and_disclose_omissions(self):
        source='START_SENTINEL'+('x'*20000)+'END_SENTINEL'
        excerpt=verification_source_excerpts({'large.js':source})['large.js']
        self.assertLessEqual(len(excerpt),16000)
        self.assertIn('START_SENTINEL',excerpt)
        self.assertIn('END_SENTINEL',excerpt)
        self.assertIn('omitted code is not evidence of absence',excerpt)
        self.assertEqual({'small.js':'complete'},verification_source_excerpts({'small.js':'complete'}))

    def test_review_is_invalidated_when_any_check_for_the_same_criterion_changes(self):
        first={'ids':['DONE:1'],'kind':'BROWSER','code':'assert.ok(first);'}
        second={'ids':['DONE:1'],'kind':'BROWSER','code':'assert.ok(second);'}
        other={'ids':['DONE:2'],'kind':'NODE','code':'assert.ok(other);'}
        previous={'checks':[first,second,other],
            '_olcr_semantically_reviewed_ids':['DONE:1','DONE:2']}
        replacement={**second,'code':'assert.ok(replacement);'}
        for checks in ([first,replacement,other],[first,other],[first,second,other,replacement]):
            with self.subTest(checks=checks):
                self.assertEqual(['DONE:2'],retained_semantic_review_ids(previous,checks))
        self.assertEqual(['DONE:1','DONE:2'],retained_semantic_review_ids(
            previous,copy.deepcopy([other,second,first])))

    def test_authoring_reserves_capacity_for_an_independent_review(self):
        for budget in ({'model_calls':14,'author_calls':7,'review_calls':6},
                       {'model_calls':13,'author_calls':6,'review_calls':7}):
            with self.subTest(budget=budget):
                self.assertTrue(verification_budget_stop_reason(budget,'author',8,15,7))
        budget={'model_calls':13,'author_calls':7,'review_calls':6}
        self.assertEqual('',verification_budget_stop_reason(budget,'author',8,15,7))
        budget.update(model_calls=14,author_calls=8)
        self.assertEqual('',verification_budget_stop_reason(budget,'review',8,15,7))

    def test_extra_model_checks_are_discarded_without_marking_missing_ids_covered(self):
        candidate={'entrypoint':'index.html','checks':[
            {'ids':['VERIFY:1','VERIFY:2'],'kind':'BROWSER','code':'await page.locator("#go").click();'},
            {'ids':['VERIFY:3'],'kind':'NODE','code':'assert.ok(true);'}]}
        scoped,discarded=scope_verification_candidate(candidate,{'VERIFY:1':'click result'})
        self.assertEqual(['VERIFY:2','VERIFY:3'],discarded)
        self.assertEqual([{'ids':['VERIFY:1'],'kind':'BROWSER','code':'await page.locator("#go").click();'}],
            scoped['checks'])
        self.assertEqual(['VERIFY:1'],[identifier for check in scoped['checks'] for identifier in check['ids']])


if __name__=='__main__':
    unittest.main()
