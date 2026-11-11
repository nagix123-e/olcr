from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from olcr_api.assistant_actions import AssistantActions,ActionError,action_intent,alarm_deadline
from olcr_api.db import Database

class AlarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'alarm.db');self.db.initialize()
        self.service=AssistantActions(self.db,lambda:SimpleNamespace(external_access_enabled=False),calendar=SimpleNamespace(native=True))
        self.service.save_preferences('primary','Asia/Tokyo')
    def test_wall_clock_next_occurrence_and_explicit_days(self):
        now=datetime.fromisoformat('2026-10-29T22:00:00+09:00')
        for text,expected in [('23時半にアラーム','2026-10-29T23:30:00+09:00'),('7時にアラーム','2026-10-30T07:00:00+09:00'),('明日の午前七時半にアラーム','2026-10-30T07:30:00+09:00'),('Set an alarm at 11:15 pm','2026-10-29T23:15:00+09:00'),('alarm at 7 AM','2026-10-30T07:00:00+09:00'),('Set an alarm at 7','2026-10-30T07:00:00+09:00'),('10月30日7時にアラーム','2026-10-30T07:00:00+09:00'),('2026-10-31 08:00にアラーム','2026-10-31T08:00:00+09:00')]:
            self.assertEqual(datetime.fromisoformat(expected).timestamp(),alarm_deadline(text,'Asia/Tokyo',now),text)
        for text in ['今日の7時','25時','23時99分','7時か8時','2026-11-20 07:00','-1時','123時']:
            with self.assertRaises(ActionError,msg=text):alarm_deadline(text,'Asia/Tokyo',now)
    def test_alarm_is_persistent_idempotent_and_does_not_shift_on_pause(self):
        now=datetime.fromisoformat('2026-10-29T22:00:00+09:00').timestamp()
        intent=action_intent('明日の7時にアラームをかけて')
        self.assertEqual('alarm',intent['service'])
        with patch('olcr_api.assistant_actions.time.time',return_value=now):
            result=self.service.handle(intent,'chat','one')
            self.assertEqual(result,self.service.handle(intent,'chat','one'))
            alarm=result['blocks'][0]['timers'][0]
            self.assertEqual('alarm',alarm['kind']);self.assertIn('2026-10-30 07:00 JST',result['response'])
            self.service.notification_result([alarm['id']],'scheduled')
            reload=AssistantActions(self.db,lambda:SimpleNamespace(external_access_enabled=False),calendar=SimpleNamespace(native=True))
            self.assertEqual(alarm['deadline'],reload.timer(alarm['id'])['deadline'])
            with self.assertRaisesRegex(ActionError,'ALARM_CONTROL_UNSUPPORTED'): reload.update_timer(alarm['id'],'pause')
            self.assertEqual('cancelled',reload.update_timer(alarm['id'],'cancel')['status'])
    def test_browser_timezone_overrides_saved_timezone(self):
        now=datetime.fromisoformat('2026-10-29T12:00:00+00:00').timestamp()
        intent={**action_intent('23時にアラームをかけて'),'time_zone':'Europe/London'}
        with patch('olcr_api.assistant_actions.time.time',return_value=now):
            result=self.service.handle(intent,'chat','two')
            self.assertEqual(datetime.fromisoformat('2026-10-29T23:00:00+00:00').timestamp(),result['blocks'][0]['timers'][0]['deadline'])
    def test_actions_do_not_hijack_help_or_implementation(self):
        for q in ['アラーム機能を実装して','How do I set an alarm?']:
            self.assertIsNone(action_intent(q))
        self.assertEqual('timer',action_intent('５分のたいまーをかけて')['service'])
        self.assertEqual('alarm',action_intent('午後三時にアラームをセットして')['service'])
        self.assertEqual('calendar',action_intent('カレンダーに明日15時の予定を書き込んで')['service'])

    def test_finished_alarm_keeps_notification_receipt_and_timer_migration(self):
        now=datetime.fromisoformat('2026-10-29T22:00:00+09:00').timestamp()
        with patch('olcr_api.assistant_actions.time.time',return_value=now):
            alarm=self.service.start_timer(10,'Alarm','chat',kind='alarm')
            self.service.notification_result([alarm['id']],'scheduled')
        with patch('olcr_api.assistant_actions.time.time',return_value=now+11):
            ended=self.service.timer(alarm['id'])
            self.assertEqual('finished',ended['status'])
            self.assertEqual('scheduled',ended['notification_status'])
        # A pre-alarm installation upgrades in place, retaining existing timers.
        with self.db.connect() as con: con.execute('ALTER TABLE assistant_timers DROP COLUMN kind')
        migrated=AssistantActions(self.db,lambda:SimpleNamespace(external_access_enabled=False),calendar=SimpleNamespace(native=True))
        self.assertEqual('timer',migrated.timer(alarm['id'])['kind'])
