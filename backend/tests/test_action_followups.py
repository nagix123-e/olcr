from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from olcr_api.assistant_actions import AssistantActions,action_intent
from olcr_api.action_followups import fields,followup_intent,previous_action
from olcr_api.db import Database

class ActionFollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'followup.db');self.db.initialize();self.db.create_conversation('chat',1,'chat',None);self.db.create_conversation('other',1,'other',None)
        with self.db.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS service_connector_categories (id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0)')
            con.execute("INSERT INTO service_connector_categories (id,enabled) VALUES ('calendar',1) ON CONFLICT(id) DO UPDATE SET enabled=1")
        self.calendar=SimpleNamespace(native=True,provider='apple',status=lambda:{'connected':True},calendars=lambda:[{'id':'primary','writable':True,'primary':True}])
        self.service=AssistantActions(self.db,lambda:SimpleNamespace(external_access_enabled=True),calendar=self.calendar)
        self.service.save_preferences('primary','Asia/Tokyo')
        self.now=datetime.fromisoformat('2026-10-29T17:39:00+09:00').timestamp()
        self.clock=patch('olcr_api.assistant_actions.time.time',return_value=self.now);self.clock.start();self.addCleanup(self.clock.stop)
        self.seq=0
    def run_action(self,text,previous=None):
        intent=followup_intent(text,previous) if previous else action_intent(text)
        self.assertIsNotNone(intent,text);self.seq+=1
        return self.service.handle(intent,'chat',str(self.seq))
    def test_today_six_stays_in_alarm_flow_confirms_pm_and_updates_same_alarm(self):
        first=self.run_action('6時にアラームをセットして');original=first['blocks'][0]['timers'][0]
        self.service.notification_result([original['id']],'scheduled')
        correction=self.run_action('今日の六時',first['blocks'][0]);block=correction['blocks'][0]
        self.assertEqual('clarification',block['kind']);self.assertIn('2026-10-29 18:00 JST',correction['response'])
        self.assertEqual(original['deadline'],self.service.timer(original['id'])['deadline'])
        changed=self.run_action('はい',block)['blocks'][0]['timers'][0]
        self.assertEqual(original['id'],changed['id']);self.assertEqual('pending',changed['notification_status'])
        self.assertEqual(datetime.fromisoformat('2026-10-29T18:00:00+09:00').timestamp(),changed['deadline']);self.assertEqual(1,len(self.service.timers()))
    def test_multiple_future_times_require_am_or_pm_and_keep_the_old_alarm_until_selected(self):
        first=self.run_action('明日の午前6時にアラームをかけて');original=first['blocks'][0]['timers'][0]
        question=self.run_action('明日の七時',first['blocks'][0])
        self.assertEqual(2,len(question['blocks'][0]['followup']['choices']))
        changed=self.run_action('午後',question['blocks'][0])['blocks'][0]['timers'][0]
        self.assertEqual(datetime.fromisoformat('2026-10-30T19:00:00+09:00').timestamp(),changed['deadline']);self.assertEqual(original['id'],changed['id'])
    def test_stopped_alarm_is_not_restarted_without_confirmation(self):
        first=self.run_action('明日6時にアラームをかけて');original=first['blocks'][0]['timers'][0]
        self.service.update_timer(original['id'],'cancel')
        question=self.run_action('今日18時にして',first['blocks'][0]);self.assertIn('停止・終了済み',question['response'])
        self.assertEqual('cancelled',self.service.timer(original['id'])['status'])
        answer=self.run_action('はい',question['blocks'][0]);self.assertEqual('running',answer['blocks'][0]['timers'][0]['status'])
    def test_cancelled_change_keeps_original_alarm_and_cancelled_target_is_not_overwritten(self):
        first=self.run_action('6時にアラームをかけて');original=first['blocks'][0]['timers'][0]
        question=self.run_action('今日の六時',first['blocks'][0]);self.run_action('キャンセル',question['blocks'][0])
        self.assertEqual(original['deadline'],self.service.timer(original['id'])['deadline'])
        question=self.run_action('今日の六時',first['blocks'][0]);self.service.update_timer(original['id'],'cancel')
        self.run_action('はい',question['blocks'][0]);self.assertEqual('cancelled',self.service.timer(original['id'])['status'])
    def test_timer_duration_extension_and_rename_apply_to_the_current_target(self):
        other=self.service.start_timer(30,'Other chat','other')
        first=self.run_action('5分のタイマーをかけて');original=first['blocks'][0]['timers'][0]
        changed=self.run_action('あと5分追加して',first['blocks'][0]);self.assertEqual(600,changed['blocks'][0]['timers'][0]['remaining'])
        renamed=self.run_action('名前は「お茶」にして',changed['blocks'][0]);self.assertEqual('お茶',renamed['blocks'][0]['timers'][0]['name'])
        stopped=self.run_action('止めて',renamed['blocks'][0]);self.assertEqual('cancelled',stopped['blocks'][0]['timers'][0]['status']);self.assertEqual('running',self.service.timer(other['id'])['status'])
    def test_multiple_targets_are_not_selected_using_the_number_in_a_duration(self):
        one=self.service.start_timer(300,'one','chat');two=self.service.start_timer(600,'two','chat')
        previous={'kind':'timers','timers':[one,two]}
        question=self.run_action('1分にして',previous);self.assertEqual('clarification',question['blocks'][0]['kind'])
        changed=self.run_action('二番目',question['blocks'][0]);self.assertEqual(two['id'],changed['blocks'][0]['timers'][0]['id']);self.assertEqual(300,self.service.timer(one['id'])['duration'])
    def test_calendar_corrections_update_only_the_draft_and_keep_confirmation_required(self):
        action=self.service.prepare_calendar('chat','create',payload={'summary':'Meeting','start':'2026-10-30T15:00:00+09:00','end':'2026-10-30T16:00:00+09:00','timeZone':'Asia/Tokyo'})
        changed=self.run_action('18時にして',{'kind':'calendar_action','action':action})['blocks'][0]['action']
        self.assertEqual('pending',changed['status']);self.assertNotEqual(action['revision'],changed['revision']);self.assertTrue(changed['payload']['start'].endswith('18:00:00+09:00'));self.assertTrue(changed['payload']['end'].endswith('19:00:00+09:00'))
        renamed=self.run_action('タイトルは「受診」にして',{'kind':'calendar_action','action':changed})['blocks'][0]['action'];self.assertEqual('受診',renamed['payload']['summary']);self.assertEqual('pending',renamed['status'])
    def test_video_selection_reuses_candidates_without_searching(self):
        previous={'kind':'media','candidates':[{'id':'abcdefghij0','title':'one'},{'id':'abcdefghij1','title':'two'}]}
        selected=self.run_action('二番目にして',previous)['blocks'][0]
        self.assertEqual('abcdefghij1',selected['candidates'][0]['id']);self.assertTrue(selected['autoplay'])
    def test_day_and_clock_slots_are_merged_across_clarifying_replies(self):
        first=self.run_action('明日の午前6時にアラームをかけて')['blocks'][0]
        question=self.run_action('今日の七時',first)['blocks'][0]
        next_question=self.run_action('明日にして',question)['blocks'][0]
        changed=self.run_action('午後',next_question)['blocks'][0]['timers'][0]
        self.assertEqual(datetime.fromisoformat('2026-10-30T19:00:00+09:00').timestamp(),changed['deadline'])
    def test_short_status_question_reads_the_current_alarm(self):
        first=self.run_action('明日の午前6時にアラームをかけて')['blocks'][0]
        status=self.run_action('何時に設定した？',first)
        self.assertIn('2026-10-30 06:00 JST',status['response'])
    def test_explicit_alarm_change_does_not_create_another_alarm(self):
        first=self.run_action('明日の午前6時にアラームをかけて')['blocks'][0]
        changed=self.run_action('アラームを今日18時に変更して',first)
        self.assertEqual(first['timers'][0]['id'],changed['blocks'][0]['timers'][0]['id']);self.assertEqual(1,len(self.service.timers()))

    def test_editing_paused_timer_duration_does_not_resume_it(self):
        first=self.run_action('5分のタイマーをかけて')['blocks'][0]
        paused=self.run_action('一時停止して',first)['blocks'][0]
        updated=self.run_action('10分にして',paused)['blocks'][0]['timers'][0]
        self.assertEqual('paused',updated['status']);self.assertIsNone(updated['deadline']);self.assertEqual(600,updated['remaining'])
    def test_calendar_duration_followup_changes_the_draft_end_time(self):
        action=self.service.prepare_calendar('chat','create',payload={'summary':'Meeting','start':'2026-10-30T15:00:00+09:00','end':'2026-10-30T16:00:00+09:00','timeZone':'Asia/Tokyo'})
        changed=self.run_action('30分延長して',{'kind':'calendar_action','action':action})['blocks'][0]['action']
        self.assertTrue(changed['payload']['end'].endswith('16:30:00+09:00'));self.assertEqual('pending',changed['status'])

    def test_unspecified_correction_asks_for_fields_and_keeps_alarm_context(self):
        first=self.run_action('明日の午前6時にアラームをかけて')['blocks'][0]
        question=self.run_action('修正して',first)['blocks'][0]
        self.assertEqual('clarification',question['kind'])
        changed=self.run_action('今日の18時です',question)['blocks'][0]['timers'][0]
        self.assertEqual(datetime.fromisoformat('2026-10-29T18:00:00+09:00').timestamp(),changed['deadline'])

    def test_unrelated_questions_and_explicit_software_requests_do_not_reuse_alarm_context(self):
        first=self.run_action('6時にアラームをかけて')['blocks'][0]
        for text in ['今日の天気を教えて','アラーム機能を実装して','タイマーアプリを作って','別の話をしたい']:
            self.assertIsNone(followup_intent(text,first),text)
        self.assertIsNone(previous_action([{'role':'assistant','blocks':[first]},{'role':'user','content':'今日の天気を教えて'},{'role':'assistant','content':'weather'}]))
        self.assertEqual(first,previous_action([{'role':'assistant','blocks':[first]},{'role':'user','content':'今日の六時'},{'role':'assistant','content':'misrouted'}]))
