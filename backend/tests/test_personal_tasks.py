import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from olcr_api.db import Database
from olcr_api.personal_tasks import PersonalTasks, TaskDefinition, ProfilePreferences, GoalDefinition
from olcr_api.personal_task_api import register_personal_task_routes


class PersonalTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / 'tasks.db'))
        self.db.initialize()
        self.settings = SimpleNamespace(permission_level='automatic', external_access_enabled=True, web_provider='duckduckgo', web_mode='auto')
        self.connectors = Mock()
        self.connectors.categories.return_value=[{'id':'calendar','enabled':True}]
        self.connectors.execute.side_effect = lambda cid, action, args: {'messages': [{'id': 'm1'}]} if action == 'list' else {'id': 'm1', 'subject': 'Report deadline', 'content': 'Please submit by 2026-12-20.'}
        self.host = lambda: {'db': self.db, 'settings': self.settings, 'service_connectors': lambda: self.connectors}
        self.service = PersonalTasks(self.db, self.host)
        self.service.save_profile(ProfilePreferences(background_enabled=True, mail_enabled=True, web_enabled=True, model_enabled=True))

    def task(self, **changes):
        return self.service.save_task(TaskDefinition(**{'title':'Review inbox', 'kind':'mail_review', 'enabled':True, 'next_run_at':10, 'interval_seconds':300, **changes}))

    def test_persistence_scope_and_goal_progress(self):
        goal = self.service.save_goal(GoalDefinition(title='Write report', target=10, progress=2))
        task = self.task(goal_id=goal['id'])
        fresh = PersonalTasks(self.db, self.host)
        self.assertEqual(task['id'], fresh.tasks()[0]['id'])
        self.assertEqual(2, fresh.goals()[0]['progress'])
        self.assertEqual([], PersonalTasks(self.db, self.host, owner='someone-else').tasks())
        with self.assertRaises(ValueError):
            PersonalTasks(self.db, self.host, owner='someone-else').control(task['id'], 'resume')

    def test_due_mail_review_is_read_only_and_deduplicated(self):
        task = self.task()
        self.service.tick(now=11)
        self.service.tick(now=12)
        self.assertEqual(['list','get'], [call.args[1] for call in self.connectors.execute.call_args_list])
        result = self.service.runs(task['id'])[0]
        self.assertEqual('completed', result['status'])
        self.assertEqual('2026-12-20', result['result']['proposals'][0]['date'])
        self.assertEqual(1, len(self.service.inbox()))
        self.assertEqual(311, self.service.get_task(task['id'])['next_run_at'])

    def test_permissions_revoked_and_pause_prevent_execution(self):
        task=self.task()
        self.settings.external_access_enabled=False
        self.service.tick(now=11)
        self.connectors.execute.assert_not_called()
        self.assertEqual('EXTERNAL_ACCESS_REQUIRED', self.service.runs(task['id'])[0]['error'])
        self.service.control(task['id'],'pause')
        self.settings.external_access_enabled=True
        self.service.tick(now=1000)
        self.connectors.execute.assert_not_called()
        self.service.control(task['id'],'resume',now=1000)
        self.service.tick(now=1000)
        self.assertEqual(2,self.connectors.execute.call_count)
        self.service.control(task['id'],'stop')
        with self.assertRaises(ValueError): self.service.control(task['id'],'resume')

    def test_disable_while_running_discards_result(self):
        task=self.task()
        def execute(cid,action,args):
            self.service.control(task['id'],'pause')
            return {'messages':[]}
        self.connectors.execute.side_effect=execute
        self.service.tick(now=11)
        self.assertEqual('cancelled',self.service.runs(task['id'])[0]['status'])
        self.assertEqual([],self.service.inbox())

    def test_expired_lease_is_not_replayed(self):
        task=self.task()
        claimed=self.service.claim_due(now=11)
        self.assertIsNotNone(claimed)
        fresh=PersonalTasks(self.db,self.host)
        fresh.tick(now=1000)
        self.connectors.execute.assert_not_called()
        self.assertEqual('interrupted',fresh.runs(task['id'])[0]['status'])

    def test_calendar_proposal_requires_explicit_preview_and_owner(self):
        task=self.task()
        self.service.tick(now=11)
        run=self.service.runs(task['id'])[0]
        assistant=Mock()
        assistant.prepare_calendar.return_value={'id':'preview','status':'pending'}
        self.db.create_conversation('Test',time.time(),'chat',None)
        self.host=lambda:{'db':self.db,'settings':self.settings,'service_connectors':lambda:self.connectors,'assistant_service':lambda:assistant}
        self.service.host=self.host
        value=self.service.preview_calendar(run['id'],0,'chat')
        self.assertEqual('pending',value['status'])
        assistant.prepare_calendar.assert_called_once()
        assistant.confirm_calendar.assert_not_called()
        self.assertEqual('2026-12-20',assistant.prepare_calendar.call_args.kwargs['payload']['start'])

    def test_api_validation_and_profile(self):
        app=FastAPI(); register_personal_task_routes(app,self.host)
        with TestClient(app) as client:
            self.assertEqual(422,client.post('/api/personal/tasks',json={'title':'bad','kind':'shell','command':'rm -rf /'}).status_code)
            self.assertEqual(422,client.post('/api/personal/tasks',json={'title':'bad','kind':'web_watch','query':'https://localhost/'}).status_code)
            self.assertEqual(200,client.put('/api/personal/profile',json={'display_name':'Nagi','time_zone':'Asia/Tokyo'}).status_code)
            self.assertEqual('Nagi',client.get('/api/personal/profile').json()['display_name'])
            self.assertEqual(422,client.put('/api/personal/profile',json={'time_zone':'not/a/zone'}).status_code)

    def test_model_review_is_bounded_and_unavailable_without_adapter(self):
        task=self.task(kind='model_review',prompt='Review my writing goal')
        self.service.tick(now=11)
        self.assertEqual('MODEL_REVIEW_UNAVAILABLE',self.service.runs(task['id'])[0]['error'])
        callback=Mock(return_value={'summary':'Draft paragraph 1','steps':['Draft','Review']})
        self.service.host=lambda:{**self.host(),'personal_task_model_review':callback}
        self.service.control(task['id'],'resume',now=500)
        self.service.tick(now=500)
        self.assertEqual('Draft paragraph 1',self.service.runs(task['id'])[0]['result']['summary'])
        self.assertEqual(180,callback.call_args.kwargs['max_seconds'])

    def test_model_extraction_requires_separate_permission_and_validates_source_ids(self):
        extractor=Mock(return_value={'proposals':[{'title':'Report','date':'2026-12-20','message_id':'m1'}]})
        self.service.host=lambda:{**self.host(),'personal_task_extract_deadlines':extractor}
        self.service.save_profile(ProfilePreferences(background_enabled=True,mail_enabled=True,model_enabled=False,instructions='private preference'))
        task=self.task()
        self.service.tick(now=11)
        extractor.assert_not_called()
        self.service.save_profile(ProfilePreferences(background_enabled=True,mail_enabled=True,model_enabled=True,instructions='private preference'))
        self.service.control(task['id'],'resume',now=500)
        self.service.tick(now=500)
        extractor.assert_called_once()
        self.assertNotIn('instructions',extractor.call_args.kwargs['profile'])
        extractor.return_value={'proposals':[{'title':'Unknown','date':'2026-12-20','message_id':'unknown'}]}
        self.service.control(task['id'],'resume',now=900)
        self.service.tick(now=900)
        self.assertEqual('DEADLINE_MESSAGE_INVALID',self.service.runs(task['id'])[0]['error'])

    def test_confirm_mode_background_network_and_category_revoke_are_blocked(self):
        self.settings.permission_level='confirm'
        task=self.task()
        self.service.tick(now=11)
        self.assertEqual('BACKGROUND_REQUIRES_AUTOMATIC_PERMISSION',self.service.runs(task['id'])[0]['error'])
        self.connectors.execute.assert_not_called()
        self.settings.permission_level='automatic'
        self.connectors.authorize.side_effect=ValueError('CONNECTOR_PERMISSION_DENIED')
        self.service.control(task['id'],'resume',now=500)
        self.service.tick(now=500)
        self.assertEqual('CONNECTOR_PERMISSION_DENIED',self.service.runs(task['id'])[0]['error'])
        self.connectors.execute.assert_not_called()

    def test_two_workers_claim_schedule_only_once(self):
        self.task()
        other=PersonalTasks(self.db,self.host)
        self.assertIsNotNone(self.service.claim_due(now=11))
        self.assertIsNone(other.claim_due(now=11))

    def test_profile_context_is_explicit_opt_in(self):
        value=ProfilePreferences(display_name='Nagi',instructions='Prefer concise answers')
        self.service.save_profile(value)
        self.assertEqual('',self.service.profile_context())
        self.service.save_profile(value.model_copy(update={'personalization_enabled':True}))
        self.assertIn('Prefer concise answers',self.service.profile_context())

    def test_web_watch_reuses_search_without_fetch_and_only_notifies_changes(self):
        task=self.task(kind='web_watch',query='OLCR release updates')
        with patch('olcr_api.web.search',return_value=[{'title':'Release','url':'https://example.com/news','snippet':'New version'}]) as search, patch('olcr_api.web.fetch') as fetch:
            self.service.tick(now=11)
            self.service.tick(now=312)
        search.assert_called_with('OLCR release updates',5)
        self.assertEqual(2,search.call_count)
        fetch.assert_not_called()
        self.assertEqual(1,len(self.service.inbox()))
        self.assertEqual('https://example.com/news',self.service.runs(task['id'])[0]['result']['sources'][0]['url'])

    def test_one_shot_reminder_and_native_calendar_category_gate(self):
        task=self.task(kind='reminder',interval_seconds=None)
        self.settings.external_access_enabled=False
        self.service.tick(now=11)
        self.assertEqual('completed',self.service.get_task(task['id'])['status'])
        self.connectors.execute.assert_not_called()
        self.assertEqual('Review inbox',self.service.inbox()[0]['result']['summary'])
        self.settings.external_access_enabled=True
        mail=self.task(title='Mail')
        self.service.tick(now=12)
        self.connectors.categories.return_value=[{'id':'calendar','enabled':False}]
        self.db.create_conversation('Test',time.time(),'chat',None)
        with self.assertRaisesRegex(ValueError,'CALENDAR_PERMISSION_DISABLED'):
            self.service.preview_calendar(self.service.runs(mail['id'])[0]['id'],0,'chat')


if __name__=='__main__': unittest.main()
