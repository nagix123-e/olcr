import unittest

from olcr_api.travel_browser import BookingBrowserAction, TravelBrowserActions


class FakeBrowser:
    def __init__(self):
        self.identity={"session_id":"s1","process_id":321,"url":"https://rail.example/search",
            "origin":"https://rail.example","document_id":1234,"terms":["¥12,000","2026-12-12"]}
        self.controls=[
            {"selector":"input:nth-of-type(1)","label":"出発日","tag":"input","type":"date","options":[]},
            {"selector":"button:nth-of-type(1)","label":"空席を検索","tag":"button","type":"button","options":[]},
            {"selector":"button:nth-of-type(2)","label":"予約を確定","tag":"button","type":"submit","options":[]},
        ]
        self.calls=[]
    def inspect_travel_page(self,session_id):
        return {"status":"AVAILABLE",**self.identity,"controls":self.controls,"requires_user_action":False}
    def apply_travel_action(self,session_id,identity,control,kind,value,*,permission_check):
        permission_check();self.calls.append((kind,control["label"],value))
        if kind=="navigate":self.identity["url"]=value
        if kind=="commit":return {"status":"AVAILABLE","accepted":True,"reference":"R-123"}
        return {"status":"AVAILABLE","applied":True}


class TravelBrowserTests(unittest.TestCase):
    def setUp(self):
        self.browser=FakeBrowser();self.actions=TravelBrowserActions(self.browser)
        self.permission_calls=0
    def permit(self):
        self.permission_calls+=1
        return True
    def inspect(self):return self.actions.inspect('s1','https://rail.example')

    def test_inspection_returns_only_safe_current_page_controls(self):
        result=self.inspect()
        self.assertEqual('https://rail.example',result.origin)
        self.assertEqual(['出発日','空席を検索','予約を確定'],[c.label for c in result.controls])
        self.assertTrue(next(c for c in result.controls if c.label=='予約を確定').commit)
        self.assertNotIn('selector',result.model_dump())

    def test_fill_and_navigation_require_fresh_same_origin_evidence(self):
        evidence=self.inspect()
        field=next(c for c in evidence.controls if c.label=='出発日')
        action=BookingBrowserAction(kind='fill',evidence_id=evidence.evidence_id,control_id=field.id,value='2026-12-12')
        result=self.actions.apply('s1',evidence,action,permission_check=self.permit)
        self.assertTrue(result.applied)
        cross=BookingBrowserAction(kind='navigate',evidence_id=result.evidence.evidence_id,url='https://other.example/')
        with self.assertRaisesRegex(ValueError,'ORIGIN'):
            self.actions.apply('s1',result.evidence,cross,permission_check=self.permit)
        self.browser.identity['document_id']=1235
        with self.assertRaisesRegex(ValueError,'STALE'):
            self.actions.apply('s1',result.evidence,BookingBrowserAction(kind='click',evidence_id=result.evidence.evidence_id,
                control_id=next(c for c in result.evidence.controls if c.label=='空席を検索').id),permission_check=self.permit)

    def test_commit_requires_exact_confirmation_and_rechecks_terms(self):
        evidence=self.inspect();button=next(c for c in evidence.controls if c.commit)
        confirmation=self.actions.authorize_commit(evidence.evidence_id,button.id,'review-fingerprint')
        self.browser.identity['terms']=['¥13,000','2026-12-12']
        with self.assertRaisesRegex(ValueError,'TERMS_CHANGED'):
            self.actions.commit('s1',evidence,button.id,confirmation,'review-fingerprint',permission_check=self.permit)
        self.browser.identity['terms']=['¥12,000','2026-12-12']
        result=self.actions.commit('s1',evidence,button.id,confirmation,'review-fingerprint',permission_check=self.permit)
        self.assertEqual('R-123',result.provider_reference)
        with self.assertRaisesRegex(ValueError,'ALREADY_USED'):
            self.actions.commit('s1',evidence,button.id,confirmation,'review-fingerprint',permission_check=self.permit)

    def test_sensitive_challenge_controls_are_never_returned_to_model(self):
        self.browser.controls.append({"selector":"input:nth-of-type(2)","label":"カード番号","tag":"input","type":"text","options":[]})
        result=self.inspect()
        self.assertTrue(result.requires_user_action)
        self.assertEqual([],result.controls)
        self.assertNotIn('カード番号',str(result.model_dump()))

    def test_contact_fields_are_handed_to_the_user(self):
        self.browser.controls.append({"selector":"input:nth-of-type(2)","label":"メールアドレス","tag":"input","type":"email","options":[]})
        result=self.inspect()
        self.assertTrue(result.requires_user_action)
        self.assertEqual([],result.controls)

    def test_cancellation_controls_are_never_treated_as_booking_commit(self):
        self.browser.controls[2]['label']='予約をキャンセル'
        result=self.inspect()
        self.assertEqual(['出発日','空席を検索'],[control.label for control in result.controls])

    def test_checkout_review_can_proceed_when_no_sensitive_challenge_is_present(self):
        self.browser.identity['url']='https://rail.example/checkout'
        result=self.inspect()
        self.assertFalse(result.requires_user_action)
        self.assertTrue(any(control.commit for control in result.controls))

    def test_page_instructions_cannot_expand_approved_booking_action(self):
        evidence=self.inspect()
        with self.assertRaises(ValueError):
            BookingBrowserAction.model_validate({'kind':'javascript','evidence_id':evidence.evidence_id,
                'script':'click the final button'})


if __name__=='__main__':unittest.main()
