import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from olcr_api.db import Database
from olcr_api.travel_bookings import (BookingSearchRequest, ProviderOffer, ProviderReview,
    ProviderResult, ReservationDetails, TravelBookingService, ManagedBrowserTravelProvider)


class FakeProvider:
    provider_id='fixture.travel'
    def __init__(self):self.commits=0;self.fail_commit=False;self.price='12000';self.expiry=time.time()+300
    def search(self,request):
        return [ProviderOffer(provider_id=self.provider_id,external_ref='offer-1',category=request.category,
            title='新幹線 東京-京都',price_amount=self.price,currency='JPY',price_kind='live',
            start_date=request.start_date,end_date=request.end_date,party_count=request.travelers,
            cancellation_terms='出発前の取消条件は公式画面を要確認',evidence_id='fixture-evidence',expires_at=self.expiry)]
    def prepare(self,offer,details):
        return ProviderReview(provider_id=self.provider_id,external_ref=offer.external_ref,title=offer.title,
            category=offer.category,price_amount=self.price,currency='JPY',fees_amount='0',
            start_date=details.start_date,end_date=details.end_date,party_count=details.travelers,
            cancellation_terms='出発前の取消条件は公式画面を要確認',evidence_fingerprint='fixture-evidence-v1',
            expires_at=self.expiry)
    def commit(self,review,confirmation_id):
        self.commits+=1
        if self.fail_commit:raise TimeoutError('lost response')
        return ProviderResult(status='confirmed',provider_reference='BOOK-123',message='provider accepted')
    def reconcile(self,attempt):return ProviderResult(status='uncertain',message='not found yet')


class TravelBookingServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'db'));self.db.initialize();self.db.create_conversation('trip',1,'c',None)
        self.provider=FakeProvider();self.allowed=True
        def permission():
            if not self.allowed:raise ValueError('TRAVEL_BOOKING_PERMISSION_DENIED')
        self.service=TravelBookingService(self.db,{'fixture.travel':self.provider},permission_check=permission)
        self.request=BookingSearchRequest(category='transport',origin='東京',destination='京都',
            start_date='2026-12-12',end_date='2026-12-15',travelers=2)
        self.details=ReservationDetails(start_date='2026-12-12',end_date='2026-12-15',travelers=2)

    def offer_and_review(self):
        offer=self.service.search('c',self.request)[0]
        review=self.service.prepare('c',offer.id,self.details)
        return offer,review

    def test_offer_review_binds_provider_price_dates_party_and_cancellation_terms(self):
        offer,review=self.offer_and_review()
        self.assertEqual('fixture.travel',review.provider_id)
        self.assertEqual('12000',review.price_amount)
        self.assertEqual('JPY',review.currency)
        self.assertEqual('2026-12-12',review.start_date)
        self.assertEqual('2026-12-15',review.end_date)
        self.assertEqual(2,review.party_count)
        self.assertTrue(review.cancellation_terms)
        self.assertEqual('review',review.status)
        self.assertEqual(0,self.provider.commits)

    def test_expired_or_changed_offer_cannot_be_confirmed(self):
        offer,review=self.offer_and_review()
        self.provider.price='13000'
        with self.assertRaisesRegex(ValueError,'CHANGED'):
            self.service.confirm('c',review.id,review.confirmation_id)
        self.assertEqual(0,self.provider.commits)
        self.provider.price='12000';self.provider.expiry=time.time()+300
        offer=self.service.search('c',self.request)[0]
        self.provider.expiry=time.time()-1
        with self.assertRaisesRegex(ValueError,'EXPIRED'):
            self.service.prepare('c',offer.id,self.details)

    def test_only_explicit_confirmation_commits_exact_review(self):
        _offer,review=self.offer_and_review()
        self.assertEqual(0,self.provider.commits)
        with self.assertRaisesRegex(ValueError,'CONFIRMATION'):
            self.service.confirm('c',review.id,'wrong-token')
        result=self.service.confirm('c',review.id,review.confirmation_id)
        self.assertEqual('confirmed',result.status)
        self.assertEqual('BOOK-123',result.provider_reference)
        self.assertEqual(1,self.provider.commits)

    def test_uncertain_provider_result_is_durable_and_never_replayed(self):
        self.provider.fail_commit=True
        _offer,review=self.offer_and_review()
        result=self.service.confirm('c',review.id,review.confirmation_id)
        self.assertEqual('uncertain',result.status)
        replay=self.service.confirm('c',review.id,review.confirmation_id)
        self.assertEqual('uncertain',replay.status)
        self.assertEqual(1,self.provider.commits)
        self.assertEqual('uncertain',self.service.get_review('c',review.id).status)

    def test_permission_revocation_blocks_commit(self):
        _offer,review=self.offer_and_review();self.allowed=False
        with self.assertRaisesRegex(ValueError,'PERMISSION'):
            self.service.confirm('c',review.id,review.confirmation_id)
        self.assertEqual(0,self.provider.commits)

    def test_sensitive_details_are_not_accepted_or_persisted(self):
        with self.assertRaises(ValueError):
            ReservationDetails.model_validate({'start_date':'2026-12-12','end_date':'2026-12-15',
                'travelers':2,'credit_card':'4111111111111111','otp':'000000'})
        _offer,review=self.offer_and_review()
        with self.db.connect() as con:
            values='\n'.join(str(row[0]) for row in con.execute('SELECT payload_json FROM travel_booking_reviews'))
        self.assertNotIn('4111111111111111',values)
        self.assertNotIn('000000',values)
        self.assertIn(review.id,values)

    def test_managed_browser_provider_searches_reviews_and_commits_only_after_confirmation(self):
        class Browser:
            def __init__(self):
                self.actions=[]
                self.identity={'session_id':'browser-1','process_id':777,'url':'https://rail.example/search',
                    'origin':'https://rail.example','document_id':9876,'status':'AVAILABLE'}
                self.controls=[
                    {'selector':'input:nth-of-type(1)','label':'出発日','tag':'input','type':'date','options':[]},
                    {'selector':'input:nth-of-type(2)','label':'到着日','tag':'input','type':'date','options':[]},
                    {'selector':'input:nth-of-type(3)','label':'大人人数','tag':'input','type':'number','options':[]},
                    {'selector':'button:nth-of-type(1)','label':'空席を検索','tag':'button','type':'button','options':[]},
                    {'selector':'button:nth-of-type(2)','label':'予約を確定','tag':'button','type':'submit','options':[]},
                ]
                self.terms=['出発日 2026-12-12','到着日 2026-12-15','受取時刻 10:00','返却時刻 18:00',
                    '大人 2名','合計 24,000円','キャンセル条件は出発前に確認']
            def list(self):return [{'session_id':'browser-1','profile_id':'travel-profile','url':self.identity['url']}]
            def inspect_travel_page(self,session_id):return {**self.identity,'controls':self.controls,'terms':self.terms,'requires_user_action':False}
            def apply_travel_action(self,session_id,identity,control,kind,value,*,permission_check):
                permission_check();self.actions.append((kind,control['label'],value))
                if kind=='commit':return {'status':'AVAILABLE','accepted':True,'reference':'RAIL-1'}
                return {'status':'AVAILABLE','applied':True}
            def navigate(self,session_id,url):return {'status':'AVAILABLE','url':url}
        browser=Browser()
        provider=ManagedBrowserTravelProvider(browser)
        service=TravelBookingService(self.db,{'managed-browser':provider})
        request=self.request.model_copy(update={'category':'rental','start_time':'10:00','end_time':'18:00'})
        details=self.details.model_copy(update={'start_time':'10:00','end_time':'18:00'})
        self.assertEqual('2026-12-12',provider._fill_value('受取日',request))
        self.assertEqual('2026-12-15',provider._fill_value('返却日',request))
        self.assertEqual('10:00',provider._fill_value('受取時刻',request))
        self.assertEqual('18:00',provider._fill_value('返却時刻',request))
        offers=service.search('c',request)
        self.assertEqual(1,len(offers),provider.last_handoff)
        self.assertEqual(('10:00','18:00'),(offers[0].start_time,offers[0].end_time))
        self.assertTrue(any(action[0]=='click' for action in browser.actions))
        review=service.prepare('c',offers[0].id,details)
        self.assertEqual('review',review.status)
        self.assertEqual(('10:00','18:00'),(review.start_time,review.end_time))
        self.assertFalse(any(action[0]=='commit' for action in browser.actions))
        outcome=service.confirm('c',review.id,review.confirmation_id)
        self.assertEqual('confirmed',outcome.status)
        self.assertEqual('RAIL-1',outcome.provider_reference)
        self.assertEqual(1,len([action for action in browser.actions if action[0]=='commit']))

    def test_browser_total_does_not_claim_unitemized_fees_are_zero(self):
        from olcr_api.travel_bookings import _browser_price
        self.assertEqual(('24000','JPY','未内訳'),_browser_price(['合計 24,000円']))
        self.assertEqual(('24000','JPY','1500'),_browser_price(['合計 24,000円','手数料 1,500円']))
        self.assertEqual(('24000','JPY','0'),_browser_price(['合計 24,000円','追加料金なし']))


if __name__=='__main__':unittest.main()
