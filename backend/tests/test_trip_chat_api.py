from contextlib import ExitStack,redirect_stdout,redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from olcr_api.db import Database
from olcr_api.config import Settings
from backend.tests.test_trip_planning import FORM, PLAN
from olcr_api.travel_bookings import (BookingSearchRequest,ProviderOffer,ProviderReview,ProviderResult,
    TravelBookingService)


class ChatBookingProvider:
    provider_id='fixture.chat'
    def __init__(self):self.commits=0
    def search(self,request):
        return [ProviderOffer(provider_id=self.provider_id,external_ref='chat-offer',category=request.category,
            title='京都の宿',price_amount='48000',currency='JPY',price_kind='live',fees_amount='3000',
            start_date=request.start_date,end_date=request.end_date,party_count=request.travelers,
            start_time=request.start_time,end_time=request.end_time,
            cancellation_terms='12月10日まで無料、以降返金不可',evidence_id='evidence-1',expires_at=9999999999)]
    def prepare(self,offer,details):
        return ProviderReview(provider_id=self.provider_id,external_ref=offer.external_ref,title=offer.title,
            category=offer.category,price_amount='48000',currency='JPY',fees_amount='3000',
            start_date=details.start_date,end_date=details.end_date,party_count=details.travelers,
            start_time=details.start_time,end_time=details.end_time,
            cancellation_terms='12月10日まで無料、以降返金不可',evidence_fingerprint='evidence-1',expires_at=9999999999)
    def commit(self,review,confirmation_id):
        self.commits+=1
        return ProviderResult(status='confirmed',provider_reference='CHAT-123')
    def reconcile(self,attempt):return ProviderResult(status='uncertain')

class TripApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.startup=tempfile.TemporaryDirectory()
        with patch.dict('os.environ',{'OLCR_DB_PATH':str(Path(cls.startup.name)/'startup.db')}):
            import olcr_api.app as api
        cls.api=api;api.stop_coding_scheduler()
    @classmethod
    def tearDownClass(cls):cls.startup.cleanup()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'test.db'));self.db.initialize()
        self.db.create_project('P',self.tmp.name,1,'p');self.db.create_project('Q',self.tmp.name,1,'q')
        self.db.create_conversation('Trip',1,'c','p')
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for key,value in [('db',self.db),('settings',Settings(permission_level='automatic',external_access_enabled=False)),('_gui_session_token','trip-test-session')]:
            self.stack.enter_context(patch.object(self.api,key,value))
        self.stack.enter_context(patch.object(self.api.brain_models,'configuration',return_value={}))
        plan=json.loads(json.dumps(PLAN))
        for d in plan['days']:d['items'][0]['source_ids']=[]
        self.model=self.stack.enter_context(patch.object(self.api.runtime,'_generate_brain',return_value={'text':json.dumps(plan)}))
        self.client=TestClient(self.api.app);self.client.headers['X-OLCR-Session']='trip-test-session'
    def test_sync_and_stream_share_saved_itinerary_and_no_coding(self):
        r=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p','message':FORM,'message_id':'trip-first'})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['blocks'][0]['status'],'ready')
        r=self.client.post('/api/chat/stream',json={'conversation_id':'c','project_id':'p','message':'旅程を見せて','message_id':'trip-show'})
        self.assertEqual(r.status_code,200,r.text);self.assertIn('TRIP_PLANNING',r.text)
        self.assertEqual(self.model.call_count,1)
        with self.db.connect() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM coding_tasks').fetchone()[0],0)
    def test_project_scope_and_auth_enforced_before_model(self):
        r=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'q','message':FORM})
        self.assertEqual(r.status_code,403,r.text);self.model.assert_not_called()
        r=self.client.post('/api/chat',json={'message':FORM},headers={'X-OLCR-Session':'wrong'})
        self.assertEqual(r.status_code,401)
    def test_confirmation_policy_is_preserved(self):
        self.api.settings.permission_level='confirm'
        r=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p','message':FORM})
        self.assertEqual(r.status_code,409,r.text);self.model.assert_not_called()

    def _booking_service(self):
        provider=ChatBookingProvider()
        return TravelBookingService(self.db,{'fixture.chat':provider}),provider

    def _create_trip(self,service):
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            r=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p','message':FORM,'message_id':'trip-for-booking'})
        self.assertEqual(200,r.status_code,r.text)

    def test_natural_booking_request_creates_review_without_commit(self):
        service,provider=self._booking_service();self._create_trip(service)
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            r=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p','message':'宿を予約して','message_id':'hotel-booking'})
        self.assertEqual(200,r.status_code,r.text)
        block=next(x for x in r.json()['blocks'] if x['type']=='travel_booking')
        self.assertEqual('review',block['status'])
        self.assertEqual('48000',block['review']['price_amount'])
        self.assertNotIn('confirmation_id',block['review'])
        self.assertEqual(0,provider.commits)

    def test_direct_booking_collects_only_booking_details_without_a_trip_plan_or_budget(self):
        service,provider=self._booking_service()
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            first=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':'ホテルを予約して','message_id':'direct-hotel-start'}).json()
            self.assertEqual('collecting',next(x for x in first['blocks'] if x['type']=='travel_booking')['status'])
            result=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':'目的地：京都\n開始日：2026-12-12\n終了日：2026-12-15\n人数：2',
                'message_id':'direct-hotel-details'})
        self.assertEqual(200,result.status_code,result.text)
        review=next(x for x in result.json()['blocks'] if x['type']=='travel_booking')
        self.assertEqual('review',review['status'])
        self.assertEqual(0,provider.commits)

    def test_direct_food_booking_keeps_date_and_time_while_gathering_party_details(self):
        service,provider=self._booking_service()
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':'レストランを予約して','message_id':'direct-food-start'})
            second=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':'2026-12-12 19:00','message_id':'direct-food-date-time'}).json()
            self.assertEqual('collecting',next(x for x in second['blocks'] if x['type']=='travel_booking')['status'])
            result=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':'目的地：京都\n人数：2','message_id':'direct-food-party'})
        self.assertEqual(200,result.status_code,result.text)
        review=next(x for x in result.json()['blocks'] if x['type']=='travel_booking')
        self.assertEqual('review',review['status'])
        self.assertEqual(('2026-12-12','19:00'),(review['review']['start_date'],review['review']['start_time']))
        self.assertEqual(0,provider.commits)

    def test_booking_credentials_entered_in_chat_are_redacted_before_persistence(self):
        service,provider=self._booking_service()
        logs=StringIO()
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            with redirect_stdout(logs),redirect_stderr(logs):
                result=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                    'message':'ホテル予約です。連絡先はalice@example.com、パスワードはswordfish、カード番号4111111111111111',
                    'message_id':'sensitive-booking'})
        self.assertEqual(200,result.status_code,result.text)
        stored='\n'.join(message['content'] for message in self.db.conversation('c')['messages'])
        self.assertNotIn('alice@example.com',stored)
        self.assertNotIn('swordfish',stored)
        self.assertNotIn('4111111111111111',stored)
        self.assertIn('個人情報は伏せました',stored)
        for secret in ('alice@example.com','swordfish','4111111111111111'):
            self.assertNotIn(secret,logs.getvalue())
        self.assertEqual(0,provider.commits)

    def test_explicit_confirmation_commits_only_bound_offer(self):
        service,provider=self._booking_service();self._create_trip(service)
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            review=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p','message':'宿を予約して','message_id':'hotel-review'}).json()['blocks'][1]['review']
            r=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':f"レビューID {review['id']} の内容で予約を確定して",'message_id':'hotel-confirm'})
        self.assertEqual(200,r.status_code,r.text)
        block=next(x for x in r.json()['blocks'] if x['type']=='travel_booking')
        self.assertEqual('confirmed',block['status'])
        self.assertEqual('CHAT-123',block['provider_reference'])
        self.assertEqual(1,provider.commits)

    def test_offer_selection_opens_exact_review_without_committing(self):
        class MultiProvider(ChatBookingProvider):
            def search(self,request):
                return [item.model_copy(update={'external_ref':f'offer-{index}','title':f'京都の宿 {index}'})
                        for item in super().search(request) for index in (1,2)]
        provider=MultiProvider();service=TravelBookingService(self.db,{'fixture.chat':provider});self._create_trip(service)
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            options=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':'宿を予約して','message_id':'multi-hotel-review'}).json()['blocks'][1]
            self.assertEqual('offers',options['status']);self.assertEqual(2,len(options['offers']))
            selected=options['offers'][1]['id']
            r=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':f'候補ID {selected} を確認して','message_id':'select-hotel-offer'})
        self.assertEqual(200,r.status_code,r.text)
        self.assertIn('travel_booking',[x['type'] for x in r.json()['blocks']],r.json())
        review=next(x for x in r.json()['blocks'] if x['type']=='travel_booking')
        self.assertEqual('review',review['status']);self.assertEqual('京都の宿 2',review['review']['title'])
        self.assertEqual(0,provider.commits)

    def test_rental_followup_collects_both_dates_and_times(self):
        service,provider=self._booking_service();self._create_trip(service)
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            first=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p',
                'message':'レンタカーを予約して','message_id':'rental-start'}).json()
            block=next(x for x in first['blocks'] if x['type']=='travel_booking')
            self.assertEqual('collecting',block['status'])
            r=self.client.post('/api/chat/stream',json={'conversation_id':'c','project_id':'p',
                'message':'2026-12-12 10:00受取、2026-12-15 18:00返却','message_id':'rental-details'})
        self.assertEqual(200,r.status_code,r.text);self.assertIn('reasoning_delta',r.text)
        frames=[json.loads(line.removeprefix('data: ')) for line in r.text.splitlines() if line.startswith('data: ')]
        review=next(x['block'] for x in frames if x['type']=='block' and x['block']['type']=='travel_booking')
        self.assertEqual('review',review['status'])
        self.assertEqual(('2026-12-12','2026-12-15','10:00','18:00'),
            (review['review']['start_date'],review['review']['end_date'],review['review']['start_time'],review['review']['end_time']))
        self.assertEqual(0,provider.commits)

    def test_stream_emits_booking_progress_before_provider_wait(self):
        service,provider=self._booking_service();self._create_trip(service)
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            r=self.client.post('/api/chat/stream',json={'conversation_id':'c','project_id':'p',
                'message':'宿を予約して','message_id':'hotel-stream'})
        self.assertEqual(200,r.status_code,r.text)
        self.assertLess(r.text.index('reasoning_delta'),r.text.index('"type": "chunk"'))
        self.assertIn('現在の候補を予約サイトで確認しています',r.text)
        self.assertEqual(0,provider.commits)

    def test_unrelated_chat_is_not_captured_by_trip_booking(self):
        from olcr_api.app import ChatInput
        service,provider=self._booking_service();self._create_trip(service)
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            result=self.api._chat_trip(ChatInput(conversation_id='c',project_id='p',message='大谷翔平って誰'))
        self.assertIsNone(result)
        self.assertEqual(0,provider.commits)

    def test_project_scope_and_auth_are_enforced_for_booking(self):
        service,provider=self._booking_service();self._create_trip(service)
        with patch.object(self.api,'_travel_booking_service',return_value=service):
            wrong_project=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'q','message':'宿を予約して'})
            wrong_session=self.client.post('/api/chat',json={'conversation_id':'c','project_id':'p','message':'宿を予約して'},headers={'X-OLCR-Session':'wrong'})
        self.assertEqual(403,wrong_project.status_code)
        self.assertEqual(401,wrong_session.status_code)
        self.assertEqual(0,provider.commits)
