from contextlib import ExitStack
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from fastapi import HTTPException
from olcr_api.db import Database
from olcr_api.config import Settings
from olcr_api.personal_tasks import PersonalTasks
from olcr_api.assistant_workflows import AssistantWorkflows,events

class AssistantWorkflowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.db=Database(str(Path(self.tmp.name)/'db'));self.db.initialize()
  self.db.create_conversation('A',1,'a',None);self.db.create_conversation('B',1,'b',None)
  self.context={'db':self.db,'settings':Settings(external_access_enabled=False),'browser_sessions':Mock()}
  self.service=PersonalTasks(self.db,lambda:self.context);self.context['personal_tasks']=self.service
  def update_memory(updates):
   self.context['settings']=self.context['settings'].with_overrides(updates)
   return self.context['settings'].public_dict()
  self.context['update_memory']=update_memory
  self.generate=Mock();self.hub=AssistantWorkflows(lambda:self.context,self.generate);self.n=0
 def turn(self,text,cid='a',mid=None):
  self.n+=1;value=SimpleNamespace(message=text,conversation_id=cid,message_id=mid or str(self.n),client_time_zone='Asia/Tokyo')
  return self.hub.respond(value,lambda v:(cid,'',self.db.conversation(cid)))
 def plan(self,title='水を飲む'):
  self.generate.return_value={'action':'task_create','task':{'title':title,'kind':'reminder','enabled':True},'schedule':'毎朝9時'}
 def command(self,result,action='confirm'):
  return '/workflow '+result['blocks'][0]['id']+' '+action
 def test_review_restart_confirm_duplicate_and_scope(self):
  self.plan();first=self.turn('毎朝9時に水を飲むようリマインドして',mid='first')
  self.assertEqual(first['blocks'][0]['status'],'review');self.assertEqual(self.service.tasks(),[])
  self.assertEqual(self.turn('毎朝9時に水を飲むようリマインドして',mid='first'),first)
  self.hub=AssistantWorkflows(lambda:self.context,self.generate)
  other=self.turn(self.command(first),cid='b');self.assertEqual(other['blocks'][0]['status'],'error');self.assertEqual(self.service.tasks(),[])
  saved=self.turn(self.command(first),mid='approved');self.assertIn('保存',saved['response']);self.assertEqual(len(self.service.tasks()),1)
  self.assertEqual(self.turn(self.command(first),mid='approved'),saved)
  self.turn(self.command(first));self.assertEqual(len(self.service.tasks()),1)

 def test_interrupted_mail_attempt_is_never_shown_as_completed_or_resent(self):
  from unittest.mock import Mock
  from olcr_api.service_connectors import ServiceConnectors
  apple=Mock();apple.status.return_value={'state':'connected'}
  connectors=ServiceConnectors(self.db,lambda:self.context['settings'],Mock(),apple_mail=apple)
  connectors.set_category('mail',True);connectors.policy('service.apple.mail',{'enabled':True})
  self.context['service_connectors']=connectors
  self.generate.return_value={'subject':'旅行の相談','body':'旅行の予定を教えてください。'}
  draft=self.turn('friend@example.org宛てにApple Mailで旅行の相談メールを送って')
  draft_id=draft['blocks'][0]['id']
  with self.db.connect() as con:
   con.execute('INSERT INTO assistant_workflow_approvals(conversation_id,draft_id,consumed_at) VALUES(?,?,?)',('a',draft_id,1))
  replay=self.turn(self.command(draft),mid='interrupted-send-retry')
  self.assertEqual(replay['blocks'][0]['status'],'error')
  self.assertIn('送信済み',replay['response'])
  self.assertNotIn('処理済みです',replay['response'])
  self.assertEqual(replay['blocks'][0]['actions'],[])
  apple.execute.assert_not_called()

 def test_mail_outcome_is_saved_after_client_stream_is_cancelled(self):
  from unittest.mock import Mock,patch
  from olcr_api.service_connectors import ServiceConnectors
  self.context['settings']=Settings(external_access_enabled=True,permission_level='automatic')
  apple=Mock();apple.status.return_value={'state':'connected'};apple.execute.return_value={'sent':True}
  connectors=ServiceConnectors(self.db,lambda:self.context['settings'],Mock(),apple_mail=apple)
  connectors.set_category('mail',True);connectors.policy('service.apple.mail',{'enabled':True})
  self.context['service_connectors']=connectors
  self.generate.return_value={'subject':'旅行の相談','body':'旅行の予定を教えてください。'}
  draft=self.turn('friend@example.org宛てにApple Mailで旅行メールを送って')
  calls=[]
  def cancelled_after_send():
   calls.append(1)
   if len(calls)>1:raise RuntimeError('request cancelled after provider call')
  with patch('olcr_api.assistant_workflows.check_cancelled',side_effect=cancelled_after_send):
   result=self.turn(self.command(draft),mid='send-cancelled-client')
  self.assertIn('Apple Mail',result['response'])
  self.assertEqual(apple.execute.call_count,1)
  with self.db.connect() as con:
   row=con.execute('SELECT status FROM assistant_workflow_approvals WHERE conversation_id=? AND draft_id=?',('a',draft['blocks'][0]['id'])).fetchone()
  self.assertEqual(row[0],'accepted')
 def test_changed_draft_rejects_old_button_and_preserves_current(self):
  self.plan();first=self.turn('毎朝9時に水を飲むようリマインドして')
  self.generate.return_value={'action':'task_create','task':{'title':'水を飲む','kind':'reminder','enabled':True},'schedule':'毎朝8時'}
  revised=self.turn('やっぱり毎朝8時にして')
  self.assertNotEqual(first['blocks'][0]['id'],revised['blocks'][0]['id'])
  stale=self.turn(self.command(first));self.assertEqual(stale['blocks'][0]['status'],'error');self.assertEqual(stale['blocks'][0]['actions'],[])
  self.assertEqual(self.service.tasks(),[])
  self.turn(self.command(revised));self.assertEqual(len(self.service.tasks()),1)
 def test_memory_changes_are_immediate_scoped_and_retained(self):
  result=self.turn('プロジェクトをまたぐ記憶をオンにして');self.assertIn('変更',result['response'])
  self.assertTrue(self.context['settings'].conversation_memory_cross_project_enabled)
  self.turn('記憶の関連度の閾値を0.8にして');self.assertEqual(self.context['settings'].conversation_memory_min_relevance,.8)
  self.turn('プロジェクトをまたぐ記憶をオフにして');self.assertFalse(self.context['settings'].conversation_memory_cross_project_enabled)
  self.assertTrue(self.context['settings'].conversation_memory_enabled)
  self.assertGreater(len(self.db.conversation('a')['messages']),0)
 def test_cancel_and_unrelated_question_dont_execute(self):
  self.plan();result=self.turn('毎朝9時に水を飲むようリマインドして')
  count=self.generate.call_count
  self.assertIsNone(self.turn('PythonでAPIを実装して'))
  self.assertEqual(self.generate.call_count,count)
  self.turn(self.command(result,'cancel'));self.assertEqual(self.service.tasks(),[])
 def test_tool_creation_is_reviewed_and_stays_in_chat(self):
  self.generate.return_value={'name':'小文字化','description':'入力を小文字にする','parameters':[{'name':'text','type':'string','description':'入力'}],
    'steps':[{'tool_id':'lowercase','arguments_json':'{"text":"${inputs.text}"}'}]}
  draft=self.turn('文字を小文字にする再利用ツールを作って')
  self.assertEqual(draft['blocks'][0]['status'],'review')
  result=self.turn(self.command(draft));self.assertIn('保存',result['response'])
  from olcr_api.agent_tools import ToolStore
  self.assertEqual(len(ToolStore(self.db).list()),1)
  frames=[json.loads(x.removeprefix('data: ')) for x in events(result)];self.assertEqual(frames[-1]['response_owner'],'ASSISTANT_WORKFLOW')
 def test_expired_approval_cannot_write(self):
  self.plan();draft=self.turn('毎朝9時に水を飲むようリマインドして')
  with self.db.connect() as con:
   row=con.execute('SELECT * FROM assistant_workflow_turns WHERE conversation_id="a"').fetchone();state=json.loads(row['state_json']);state['expires_at']=1
   con.execute('UPDATE assistant_workflow_turns SET state_json=? WHERE conversation_id="a"',(json.dumps(state),))
  self.assertIn('期限',self.turn(self.command(draft))['response']);self.assertEqual(self.service.tasks(),[])

 def test_list_and_named_stop_remain_available_without_model(self):
  from olcr_api.personal_tasks import TaskDefinition
  row=self.service.save_task(TaskDefinition(title='水を飲む',kind='reminder',enabled=False,next_run_at=2000000000))
  self.generate.side_effect=RuntimeError('model offline')
  self.assertIn('水を飲む',self.turn('タスク一覧を見せて')['response'])
  draft=self.turn('水を飲むのタスクを止めて')
  self.assertEqual(draft['blocks'][0]['status'],'review')
  self.turn(self.command(draft))
  self.assertEqual(self.service.get_task(row['id'])['status'],'stopped')
  self.generate.assert_not_called()

 def test_form_preview_and_apply_are_inline_and_source_bound(self):
  self.context['settings']=Settings(external_access_enabled=True)
  browser=self.context['browser_sessions']
  browser.list.return_value=[{'session_id':'s1','url':'https://example.org/contact'}]
  browser.inspect_public_form.return_value={'status':'AVAILABLE','session_id':'s1','process_id':123,'url':'https://example.org/contact',
   'origin':'https://example.org','document_id':12345,'fields':[{'label':'件名','selector':'#subject','tag':'input','type':'text'}]}
  browser.fill_public_form.return_value={'status':'AVAILABLE'}
  self.generate.return_value={'fields':[{'label':'件名','value':'Hello'}]}
  draft=self.turn('https://example.org/contact のフォームの件名にHelloと入力して')
  self.assertEqual(draft['blocks'][0]['status'],'review');self.assertEqual(draft['blocks'][0]['browser_session_id'],'s1')
  browser.fill_public_form.assert_not_called()
  result=self.turn(self.command(draft));self.assertIn('1項目',result['response']);self.assertEqual(result['blocks'][0]['browser_session_id'],'s1')
  browser.fill_public_form.assert_called_once()
 def test_notices_can_prepare_existing_calendar_review_in_chat(self):
  from olcr_api.personal_tasks import TaskDefinition,ProfilePreferences
  connectors=Mock();connectors.categories.return_value=[{'id':'calendar','enabled':True}]
  connectors.execute.side_effect=lambda cid,action,args:{'messages':[{'id':'m1'}]} if action=='list' else {'id':'m1','subject':'提出期限','content':'提出期限は2026-12-20まで'}
  assistant=Mock()
  self.context.update(settings=Settings(external_access_enabled=True),service_connectors=lambda:connectors,assistant_service=lambda:assistant)
  assistant.prepare_calendar.return_value={'id':'calendar-draft','conversation_id':'a','status':'pending'}
  self.service.save_profile(ProfilePreferences(background_enabled=True,mail_enabled=True))
  self.service.save_task(TaskDefinition(title='締切を確認',kind='mail_review',enabled=True,next_run_at=10))
  self.service.tick(now=11)
  result=self.turn('/workflow inbox');self.assertIn('締切候補1',''.join(result['blocks'][0]['details']))
  self.assertTrue(self.service.inbox()[0]['read_at'])
  preview=self.turn('締切候補1を予定に入れて')
  self.assertEqual(preview['blocks'][1]['type'],'assistant_action')
  assistant.prepare_calendar.assert_called_once()
  assistant.confirm_calendar.assert_not_called()
 def test_connection_review_explicitly_grants_and_opens_provider_browser(self):
  service=Mock();service.categories.return_value=[];self.context['service_connectors']=service
  self.context['browser_sessions'].list.return_value=[]
  self.context['browser_sessions'].open.return_value={'status':'READY','session_id':'market-session'}
  self.context['browser_sessions'].navigate.return_value={'status':'AVAILABLE'}
  def update(updates):self.context['settings']=self.context['settings'].with_overrides(updates)
  self.context['update_capabilities']=update
  draft=self.turn('Amazonに接続して');self.assertEqual(draft['blocks'][0]['status'],'review')
  self.context['browser_sessions'].open.assert_not_called()
  result=self.turn(self.command(draft));self.assertIn('ブラウザー',result['response'])
  self.assertTrue(self.context['settings'].external_access_enabled)
  service.set_category.assert_called_once_with('shopping',True)
  self.context['browser_sessions'].navigate.assert_called_once_with('market-session','https://www.amazon.co.jp/')

 def test_memory_ascii_on_off_next_to_japanese_text(self):
  self.turn('プロジェクトをまたぐ記憶をONにして')
  self.assertTrue(self.context['settings'].conversation_memory_cross_project_enabled)
  self.turn('プロジェクトをまたぐ記憶をOFFにして')
  self.assertFalse(self.context['settings'].conversation_memory_cross_project_enabled)
