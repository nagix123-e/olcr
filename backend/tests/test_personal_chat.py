"""Chat journeys use a real temporary personal service and a mocked Brain planner."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from olcr_api.db import Database
from olcr_api.personal_tasks import PersonalTasks, TaskDefinition, GoalDefinition
from olcr_api import personal_chat


class PersonalChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / 'personal.db'))
        self.db.initialize()
        self.settings = SimpleNamespace(permission_level='automatic', external_access_enabled=True, web_mode='auto', web_provider='duckduckgo')
        self.connectors = Mock()
        self.host = {'db': self.db, 'settings': self.settings, 'service_connectors': self.connectors}
        self.service = PersonalTasks(self.db, lambda: self.host)
        self.host['personal_tasks'] = self.service
        self.generated = []

    def generate(self, system, payload, schema):
        return schema.model_validate(self.generated.pop(0)).model_dump(mode='json', exclude_none=True)

    def handle(self, text, state=None):
        return personal_chat.handle(self.host, 'chat', text, state, 'Asia/Tokyo', self.generate)

    def test_daily_reminder_reviews_schedule_and_scope_before_persisting(self):
        self.generated = [{'action': 'task_create', 'task': {'title': '水を飲む'}, 'schedule': '毎朝9時'}]
        now = datetime(2026, 11, 10, 10, tzinfo=ZoneInfo('Asia/Tokyo'))
        with patch('olcr_api.personal_chat._now', return_value=now):
            review = self.handle('毎朝9時に水を飲むようリマインドして')
        self.assertEqual([], self.service.tasks())
        self.assertEqual('review', review['state']['stage'])
        self.assertEqual(86400, review['state']['payload']['interval_seconds'])
        self.assertEqual(datetime(2026, 11, 11, 9, tzinfo=ZoneInfo('Asia/Tokyo')).timestamp(), review['state']['payload']['next_run_at'])
        self.assertEqual({'background_enabled': True}, review['state']['grants'])
        self.assertFalse(self.service.profile()['background_enabled'])
        restored = json.loads(json.dumps(review['state']))
        result = self.handle('はい', restored)
        self.assertIsNone(result['state'])
        self.assertTrue(self.service.profile()['background_enabled'])
        self.assertEqual('水を飲む', self.service.tasks()[0]['title'])

    def test_missing_schedule_collects_then_reviews(self):
        self.generated = [{'action':'task_create','task':{'title':'薬を飲む'}}, {'action':'task_create','task':{'title':'薬を飲む'}, 'schedule':'明日9時'}]
        draft = self.handle('薬を飲むようリマインドして')
        self.assertEqual('collecting', draft['state']['stage'])
        review = self.handle('明日9時', draft['state'])
        self.assertEqual('review', review['state']['stage'])
        self.assertEqual([], self.service.tasks())
        self.handle('cancel', review['state'])
        self.assertEqual([], self.service.tasks())

    def test_ambiguous_target_asks_and_never_controls_first_match(self):
        one=self.service.save_task(TaskDefinition(title='朝の運動',next_run_at=200))
        self.service.save_task(TaskDefinition(title='夜の運動',next_run_at=300))
        self.generated=[{'action':'task_resume','target':'運動'}]
        draft=self.handle('運動のタスクを再開して')
        self.assertEqual('collecting',draft['state']['stage'])
        self.assertIn('朝の運動',draft['response'])
        self.assertEqual(1,self.service.get_task(one['id'])['version'])

    def test_stale_task_version_and_profile_grants_do_not_apply(self):
        task=self.service.save_task(TaskDefinition(title='朝の運動', next_run_at=200))
        self.generated=[{'action':'task_resume','target':task['id']}]
        review=self.handle('朝の運動のタスクを再開して')
        self.service.control(task['id'],'pause')
        outcome=self.handle('confirm',review['state'])
        self.assertIsNone(outcome['state'])
        self.assertIn('変更',outcome['response'])
        self.assertFalse(self.service.profile()['background_enabled'])
        self.assertEqual('paused',self.service.get_task(task['id'])['status'])

    def test_external_access_is_never_enabled_by_task_request(self):
        self.settings.external_access_enabled=False
        self.generated=[{'action':'task_create','task':{'title':'メールを確認','kind':'mail_review'},'schedule':'毎朝9時'}]
        outcome=self.handle('毎朝9時にメールを確認して')
        self.assertEqual('collecting',outcome['state']['stage'])
        self.assertIn('外部',outcome['response'])
        self.assertFalse(self.settings.external_access_enabled)
        self.assertFalse(self.service.profile()['mail_enabled'])
        self.assertEqual([],self.service.tasks())
        self.connectors.execute.assert_not_called()

    def test_goal_create_update_and_profile_edit_require_review(self):
        self.generated=[{'action':'goal_create','goal':{'title':'本を読む','target':10,'unit':'冊'}}]
        review=self.handle('今月は本を10冊読む目標を登録して')
        self.assertEqual([],self.service.goals())
        self.handle('confirm',review['state'])
        goal=self.service.goals()[0]
        self.generated=[{'action':'goal_update','target':goal['id'],'goal':{'progress':3}}]
        review=self.handle('本を読む目標の進捗を3冊に更新して')
        self.handle('confirm',review['state'])
        self.assertEqual(3,self.service.goals()[0]['progress'])
        self.generated=[{'action':'profile_update','profile':{'display_name':'Nagi','background_enabled':False}}]
        review=self.handle('プロフィールの名前をNagiにして、バックグラウンド処理を無効にして')
        self.assertEqual('',self.service.profile()['display_name'])
        self.handle('confirm',review['state'])
        self.assertEqual('Nagi',self.service.profile()['display_name'])
        self.assertFalse(self.service.profile()['background_enabled'])

    def test_lists_and_negative_matching(self):
        self.generated=[{'action':'task_list'}]
        self.assertIsNone(self.handle('自分のタスク一覧を見せて')['state'])
        for text in ['タスク管理機能を実装して','プロフィール画面のバグを修正して','What is a good goal?', '京都旅行の計画を立てて','create a custom agent for reminders','タスクとは何ですか','Pythonでタスクを作る方法を教えて']:
            self.assertFalse(personal_chat.matches(text),text)
        for text in ['毎朝9時に薬を飲むようリマインドして','自分のタスク一覧を見せて','目標を登録して','プロフィールの名前をNagiにして','Pause my exercise task']:
            self.assertTrue(personal_chat.matches(text),text)

    def test_stale_goal_and_profile_reviews_are_rejected(self):
        goal=self.service.save_goal(GoalDefinition(title='読書',target=20))
        self.generated=[{'action':'goal_update','target':goal['id'],'goal':{'progress':3}}]
        review=self.handle('読書目標の進捗を3に更新して')
        self.service.save_goal(GoalDefinition(title='読書',target=25),goal['id'])
        outcome=self.handle('confirm',review['state'])
        self.assertIsNone(outcome['state'])
        self.assertEqual(0,self.service.goals()[0]['progress'])
        self.generated=[{'action':'profile_update','profile':{'display_name':'Nagi'}}]
        review=self.handle('プロフィールの名前をNagiにして')
        self.service.save_profile({**self.service.profile(),'display_name':'Other'})
        self.handle('confirm',review['state'])
        self.assertEqual('Other',self.service.profile()['display_name'])

    def test_update_and_controls_preserve_exact_target_and_schedule(self):
        task=self.service.save_task(TaskDefinition(title='Drink water',next_run_at=9999999999,interval_seconds=86400))
        self.generated=[{'action':'task_update','target':'Drink water','task':{'title':'Drink tea'}}]
        review=self.handle('Change my Drink water task title to Drink tea')
        self.assertEqual(task['id'],review['state']['target_id'])
        self.assertEqual(9999999999,review['state']['payload']['next_run_at'])
        self.handle('confirm',review['state'])
        for action in ['resume','pause','stop']:
            self.generated=[{'action':'task_'+action,'target':task['id']}]
            review=self.handle(action+' my Drink tea task')
            self.handle('confirm',review['state'])
        self.assertEqual('stopped',self.service.get_task(task['id'])['status'])
        self.assertEqual(1,len(self.service.tasks()))

    def test_mail_review_proposes_only_mail_and_background_scope(self):
        self.generated=[{'action':'task_create','task':{'title':'Inbox deadlines','kind':'mail_review'},'schedule':'明日9時'}]
        review=self.handle('明日9時にメールの期限を確認して')
        self.assertEqual({'background_enabled':True,'mail_enabled':True},review['state']['grants'])
        self.handle('confirm',review['state'])
        self.assertFalse(self.service.profile()['model_enabled'])
        self.connectors.execute.assert_not_called()

    def test_english_daily_clock_is_resolved_without_guessing_a_date(self):
        self.generated=[{'action':'task_create','task':{'title':'Exercise'},'schedule':'every morning at 9'}]
        now=datetime(2026,11,10,8,tzinfo=ZoneInfo('Asia/Tokyo'))
        with patch('olcr_api.personal_chat._now',return_value=now):
            review=self.handle('Remind me to exercise every morning at 9')
        self.assertEqual('review',review['state']['stage'])
        self.assertEqual(86400,review['state']['payload']['interval_seconds'])

    def test_profile_planner_cannot_add_unrequested_permission(self):
        self.generated=[{'action':'profile_update','profile':{'display_name':'Nagi','mail_enabled':True}}]
        review=self.handle('プロフィールの名前をNagiにして')
        self.assertNotEqual('review',review['state']['stage'])
        self.assertFalse(self.service.profile()['mail_enabled'])

    def test_planner_cannot_invent_missing_schedule(self):
        self.generated=[{'action':'task_create','task':{'title':'Exercise'},'schedule':'明日9時'}]
        outcome=self.handle('Remind me to exercise')
        self.assertEqual('collecting',outcome['state']['stage'])
        self.assertEqual([],self.service.tasks())

    def test_calendar_words_without_resolved_date_are_not_silently_next_day(self):
        now=datetime(2026,11,10,8,tzinfo=ZoneInfo('Asia/Tokyo'))
        for words in ['2026-12-20','毎週月曜9時','next Tuesday at 9','来月10日9時']:
            with self.subTest(words=words), patch('olcr_api.personal_chat._now',return_value=now):
                self.assertIsNone(personal_chat._schedule(words,'Asia/Tokyo'))

    def test_software_requests_with_personal_words_are_not_hijacked(self):
        for text in ['タスク管理アプリを作って','目標画面のUIを作成して','Create a task manager app','プロフィール機能を追加して']:
            self.assertFalse(personal_chat.matches(text),text)

    def test_review_and_profile_show_use_readable_permission_labels(self):
        self.generated=[{'action':'task_create','task':{'title':'Inbox deadlines','kind':'mail_review'},'schedule':'明日9時'}]
        review=self.handle('明日9時にメールの期限を確認して')
        details='\n'.join(review['details'])
        self.assertIn('バックグラウンド',details)
        self.assertIn('メール',details)
        for internal in ['mail_review','mail_enabled','background_enabled','next_run_at']:
            self.assertNotIn(internal,details)
        self.generated=[{'action':'profile_show'}]
        shown=self.handle('プロフィールを見せて')
        self.assertIn('名前','\n'.join(shown['details']))
        self.assertNotIn('display_name','\n'.join(shown['details']))

    def test_pending_workflow_does_not_capture_ordinary_questions(self):
        state={'domain':'personal','stage':'collecting'}
        for text in ['今日の東京の天気を教えて','What is a Python task?', '京都旅行の計画を立てて','How does background processing work?', 'こんにちは']:
            self.assertFalse(personal_chat.matches(text,state),text)
        for text in ['明日9時','朝の運動','はい','一時停止で保存']:
            self.assertTrue(personal_chat.matches(text,state),text)

