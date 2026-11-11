import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from olcr_api.db import Database
from olcr_api.medication_records import MedicationRecords

class MedicationCalendarTests(unittest.TestCase):
    def setUp(self):
        from olcr_api.medication_calendar import MedicationCalendar
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'test.db')); self.db.initialize()
        self.records=MedicationRecords(self.db)
        self.med=self.records.register(dict(name='薬A',strength='5mg',dose='1',unit='錠',instructions='朝夕各1錠'),'rx')
        self.events={}; self.enabled=True; self.fail=False; self.posts=0
        def guard():
            if not self.enabled: raise ValueError('CALENDAR_PERMISSION_DISABLED')
        def request(method,path,payload=None,etag=None):
            if method=='POST':
                self.posts+=1
                self.events[payload['id']]={'id':payload['id'],'etag':'v1',**payload}
                if self.fail: raise ValueError('CALENDAR_NETWORK_FAILED')
                return self.events[payload['id']]
            if method=='GET': return self.events[path]
            if method=='DELETE': del self.events[path]; return {}
        adapter=SimpleNamespace(provider='apple',native=True,status=lambda:{'connected':True},
                request=request,event_path=lambda cal,ident='':ident,public_event=lambda e:e)
        self.actions=SimpleNamespace(calendar=adapter,calendar_network=guard,_native_create_calendar=lambda cal:cal)
        self.calendar=MedicationCalendar(self.db,self.actions,None)
    def plan(self,**changes):
        return self.calendar.prepare(dict(medication_id=self.med['id'],start_date='2030-01-01',end_date='2030-01-03',
            times=['08:00','20:00'],zone='Asia/Tokyo',provider='apple',calendar_id='primary',reminder=5,reveal=False,**changes),'chat')
    def test_finite_plan_is_not_intake_and_repeated_confirmation_is_idempotent(self):
        plan=self.plan(); self.assertEqual(plan['count'],6)
        self.assertEqual(self.records.summary(datetime.now(timezone.utc),'UTC')['medications'],[])
        result=self.calendar.confirm(plan['id'],'chat'); self.assertEqual(result['counts']['completed'],6)
        self.calendar.confirm(plan['id'],'chat'); self.assertEqual(self.posts,6)
        self.assertEqual(next(iter(self.events.values()))['summary'],'服用予定')
        self.assertEqual(next(iter(self.events.values()))['reminders']['overrides'][0]['minutes'],5)
    def test_revocation_and_conversation_binding_block_mutation(self):
        plan=self.plan(); self.enabled=False
        with self.assertRaises(ValueError): self.calendar.confirm(plan['id'],'chat')
        self.assertEqual(self.posts,0)
        with self.assertRaises(ValueError): self.calendar.confirm(plan['id'],'other')
    def test_uncertain_create_is_reconciled_without_second_post(self):
        plan=self.plan(); self.fail=True
        result=self.calendar.confirm(plan['id'],'chat'); self.assertGreater(result['counts']['uncertain'],0)
        self.fail=False
        result=self.calendar.confirm(plan['id'],'chat')
        self.assertEqual(result['counts']['completed'],6); self.assertEqual(self.posts,6)
    def test_prn_and_nonexistent_wall_time_are_not_scheduled(self):
        self.med=self.records.register(dict(name='薬B',strength='5mg',dose='1',unit='錠',instructions='痛いときのみ頓服'),'b')
        with self.assertRaises(ValueError): self.plan()
        self.med=self.records.register(dict(name='薬C',strength='5mg',dose='1',unit='錠',instructions='定時'),'c')
        with self.assertRaises(ValueError):
            self.calendar.prepare(dict(medication_id=self.med['id'],start_date='2030-03-10',end_date='2030-03-10',
                times=['02:30'],zone='America/New_York',provider='apple',calendar_id='primary',reminder=None,reveal=False),'chat')

    def test_prescription_limit_and_interval_reject_conflicting_schedule(self):
        self.med=self.records.register(dict(name='薬D',strength='5mg',dose='1',unit='錠',instructions='1回1錠、24時間で1錠まで',max_per_24h='1'),'d')
        with self.assertRaisesRegex(ValueError,'PRESCRIPTION'): self.plan()

    def test_stop_checks_external_edit_before_deletion(self):
        plan=self.plan(); self.calendar.confirm(plan['id'],'chat')
        first=next(iter(self.events)); self.events[first]={**self.events[first],'etag':'edited'}
        result=self.calendar.stop(plan['id'],'chat')
        self.assertEqual(result,{'deleted':5,'failed':1})
        self.assertIn(first,self.events)

    def test_overlapping_second_plan_cannot_duplicate_reminders(self):
        self.plan()
        with self.assertRaisesRegex(ValueError,'EXISTS'): self.plan()

    def test_google_not_found_reconciles_then_reuses_same_event_id(self):
        self.calendar.connectors=SimpleNamespace(authorize=lambda *a:None,
            google=SimpleNamespace(status=lambda cid:{'write_consent_required':False},connection_fingerprint=lambda cid:'account'))
        remote={}; attempts=[]; failing=[True]
        def request(method,cal,eid='',payload=None,etag=None):
            if method=='GET':
                if eid not in remote:raise ValueError('CONNECTOR_RESOURCE_NOT_FOUND')
                return remote[eid]
            attempts.append(payload['id'])
            if failing[0]:raise ValueError('CONNECTOR_PROVIDER_UNAVAILABLE')
            remote[payload['id']]={'id':payload['id'],'etag':'v1',**payload};return remote[payload['id']]
        self.calendar.connectors.calendar_request=request
        plan=self.calendar.prepare(dict(medication_id=self.med['id'],start_date='2030-01-01',end_date='2030-01-01',
            times=['08:00'],zone='Asia/Tokyo',provider='google',calendar_id='primary',reminder=None,reveal=False),'chat')
        self.assertEqual(self.calendar.confirm(plan['id'],'chat')['counts']['uncertain'],1)
        failing[0]=False
        self.assertEqual(self.calendar.confirm(plan['id'],'chat')['counts']['completed'],1)
        self.assertEqual(len(set(attempts)),1)

    def test_cancel_draft_releases_unregistered_schedule_without_remote_delete(self):
        plan=self.plan(); self.calendar.cancel_pending(plan['id'],'chat')
        self.assertEqual(self.posts,0)
        self.assertEqual(self.plan()['count'],6)
