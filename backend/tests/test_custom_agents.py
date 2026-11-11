import tempfile
import unittest
from contextlib import nullcontext
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from olcr_api.custom_agent_api import register_agent_routes
from olcr_api.custom_agents import AgentStore
from olcr_api.custom_agent_runtime import AgentRuntime
from olcr_api.brain_models import active_configuration
from olcr_api.db import Database
from olcr_api.chat_maps import ChatMap
from olcr_api.models import Task
from olcr_api.skills import SkillManager


class CustomAgentConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / 'test.sqlite'))
        self.db.initialize()
        self.model_ids = ['ollama:local-model', 'api:openai_oauth:gpt-6-luna']
        self.brain = SimpleNamespace(catalog=lambda: {'models': [
            {'id': model, 'available': True} for model in self.model_ids]})
        self.host = {'db': self.db, 'runtime': None, 'settings': None,
                     'brain_models': self.brain, 'skills': SkillManager(Path(self.tmp.name) / 'skills')}
        app = FastAPI()
        register_agent_routes(app, lambda: self.host)
        self.client = TestClient(app)

    def test_local_and_oauth_models_save_update_and_open_bound_conversations(self):
        for model in self.model_ids:
            with self.subTest(model=model):
                definition = {'name': 'My agent', 'preferred_model': model, 'instructions': 'Help me.'}
                saved = self.client.post('/api/agents', json=definition)
                self.assertEqual(200, saved.status_code, saved.text)
                identity = saved.json()['id']
                edited = self.client.put(f'/api/agents/{identity}', json={**definition, 'description': 'Updated'})
                self.assertEqual(200, edited.status_code, edited.text)
                # Conversation creation does not need to preload models or skills.
                with patch('olcr_api.custom_agent_api.runtime_root', return_value=None), \
                     patch.object(AgentRuntime, 'prepare', side_effect=AssertionError('not needed to open chat')):
                    used = self.client.post(f'/api/agents/{identity}/conversations', json={'project_id': None})
                self.assertEqual(200, used.status_code, used.text)
                conversation = used.json()
                self.assertEqual(identity, conversation['custom_agent']['id'])
                self.assertEqual([], conversation['messages'])
                bound = AgentStore(self.db).bound(conversation['id'])
                self.assertEqual(model, bound['preferred_model'])
                self.assertEqual(2, bound['revision'])
                self.assertEqual(identity, self.db.conversation(conversation['id'])['custom_agent']['id'])

    def test_agent_model_override_reaches_generation_for_local_and_oauth(self):
        for model in self.model_ids:
            for mode, slot in [('unified', 'brain'), ('split', 'general_inference')]:
                with self.subTest(model=model, mode=mode):
                    calls = []
                    def generate(*args, **kwargs):
                        calls.append((active_configuration(), kwargs['model_name']))
                        return {'text': 'Agent answer'}
                    self.brain.configuration = lambda cid: {'mode': mode, 'selections': {
                        'brain': 'default', 'coder': 'default', 'short_coding': 'default', 'general_inference': 'default'}}
                    runtime = AgentRuntime(**{**self.host, 'runtime': SimpleNamespace(_generate_brain=generate)})
                    cid = self.db.create_conversation('Agent chat', 1, f'{mode}-{model}')
                    task = Task('Hello')
                    with patch('olcr_api.custom_agent_runtime.model_slot', return_value=nullcontext()):
                        result = runtime.model({'messages': [{'role': 'human', 'content': 'Hello'}]},
                                               {'preferred_model': model, 'preferred_router': 'normal_chat'}, cid, task)
                    self.assertEqual('Agent answer', result['content'])
                    self.assertEqual(model, calls[0][0]['selections'][slot])
                    self.assertEqual('role:normal_chat', calls[0][1])

    def agent_chat_fixture(self, vision):
        from olcr_api.custom_agents import AgentDefinition
        from olcr_api.custom_agent_api import agent_chat
        from fastapi.middleware.cors import CORSMiddleware
        from pydantic import BaseModel
        class Input(BaseModel):
            conversation_id: str
            project_id: str | None = None
            message: str
            image: dict | None = None
            attachment: dict | None = None
            core_context: str = ''
            concept_diagram: dict | None = None
            location_map: ChatMap | None = None
            computer_use_event: dict | None = None
            resume_control: str | None = None
            resume_task_id: str | None = None
        agent=AgentStore(self.db).save(AgentDefinition(name='Extract events',instructions='Return event CSV'))
        cid=AgentStore(self.db).bind(agent)['id']
        self.brain.configuration=lambda cid: {'mode':'unified','selections':{}}
        host={**self.host,'runtime':SimpleNamespace(model=SimpleNamespace(vision=vision),execute_image=lambda *args: self.fail('ordinary chat must not run'))}
        app=FastAPI()
        @app.post('/api/chat')
        def chat(value:Input): return agent_chat(host,value)
        # Annotation resolution uses the closure's concrete model.
        chat.__annotations__['value']=Input
        app.add_middleware(CORSMiddleware,allow_origins=['tauri://localhost'])
        return TestClient(app),cid

    def test_map_is_saved_and_passed_to_the_bound_agent(self):
        client,cid=self.agent_chat_fixture(lambda *args: {'text':'unused'})
        from olcr_api.models import TaskState
        task=Task('map');task.transition(TaskState.ROUTING);task.transition(TaskState.GENERATING);task.transition(TaskState.COMPLETED)
        self.db.save_task(task,cid)
        seen=[]
        def events(agent,conversation_id,message):
            seen.append(message)
            yield {'type':'done','response':'Map answer','task':{'id':task.id,'state':'completed'}}
        location={'type':'location_map','points':[{'lat':35.68,'lon':139.76,'name':'東京','pin':'A'}]}
        with patch.object(AgentRuntime,'events',side_effect=events):
            response=client.post('/api/chat',json={'conversation_id':cid,'message':'この地点を確認','location_map':location})
        self.assertEqual(200,response.status_code,response.text)
        self.assertIn('東京',seen[0]);self.assertIn('[USER_MAP_LOCATIONS]',seen[0])
        self.assertEqual('location_map',self.db.conversation(cid)['messages'][0]['blocks'][0]['type'])

    def test_image_is_read_once_then_answered_by_the_bound_agent(self):
        seen=[]
        def vision(*args): seen.append(args);return {'text':'2026-10-28 15:00 Test Hall'}
        client,cid=self.agent_chat_fixture(vision)
        from olcr_api.models import TaskState
        task=Task('agent');task.transition(TaskState.ROUTING);task.transition(TaskState.GENERATING);task.transition(TaskState.COMPLETED)
        self.db.save_task(task,cid)
        calls=[]
        def events(agent,conversation_id,message):
            calls.append((agent,conversation_id,message))
            yield {'type':'done','response':'2026-10-28,15:00,Test Hall','task':{'id':task.id,'state':'completed'}}
        image={'data_url':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode(),'name':'image.png'}
        with patch('olcr_api.custom_agent_runtime.model_slot',return_value=nullcontext()),patch.object(AgentRuntime,'events',side_effect=events):
            response=client.post('/api/chat',json={'conversation_id':cid,'message':'Read this event','image':image},headers={'Origin':'tauri://localhost'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('tauri://localhost',response.headers['access-control-allow-origin'])
        self.assertEqual(1,len(seen));self.assertIn('Return event CSV',seen[0][2]);self.assertEqual('role:general_image',seen[0][3])
        self.assertIn('2026-10-28 15:00 Test Hall',calls[0][2]);self.assertNotIn('data:image',calls[0][2])
        self.assertEqual(2,len(self.db.conversation(cid)['messages']))
        block=self.db.conversation(cid)['messages'][0]['blocks'][0]
        self.assertIn('preview_id',block)
        self.assertNotIn('data_url',block)
        with self.db.connect() as con:
            self.assertEqual(image['data_url'],con.execute('SELECT data_url FROM message_image_previews WHERE id=?',(block['preview_id'],)).fetchone()[0])

    def test_image_agent_on_migrated_database_saves_answer_and_received_reasoning(self):
        from olcr_api.models import TaskState
        from olcr_api.runtime import _model_call_metadata
        from olcr_api.thinking import received_reasoning
        with self.db.connect() as con:
            con.execute('DROP TABLE task_reasoning')
            con.execute('UPDATE schema_version SET version=37')
        self.db.initialize()
        client,cid=self.agent_chat_fixture(lambda *args: {'text':'Visible event details'})
        task=Task('image');task.transition(TaskState.ROUTING);task.transition(TaskState.GENERATING);task.transition(TaskState.COMPLETED)
        task.model_calls.append(_model_call_metadata({'text':'answer','reasoning':received_reasoning('received work')},'fixture'))
        self.db.save_task(task,cid)
        image={'data_url':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}
        def events(*args):
            yield {'type':'done','response':'CSV answer','task':{'id':task.id,'state':'completed'}}
        with patch('olcr_api.custom_agent_runtime.model_slot',return_value=nullcontext()),patch.object(AgentRuntime,'events',side_effect=events):
            response=client.post('/api/chat',json={'conversation_id':cid,'message':'Read image','image':image})
        self.assertEqual(200,response.status_code,response.text)
        messages=self.db.conversation(cid)['messages']
        self.assertEqual('CSV answer',messages[-1]['content'])
        self.assertEqual('reasoning_log',messages[-1]['blocks'][0]['type'])

    def test_closing_after_done_does_not_mark_success_as_generator_error(self):
        from unittest.mock import MagicMock
        import json
        cid=self.db.create_conversation('Cleanup',1,'cleanup')
        process=MagicMock();process.poll.return_value=None
        process.stdout.fileno.return_value=123
        runtime=AgentRuntime(**self.host)
        prepared_task=Task('Hello')
        prepared_task.model_calls=[{'model':'local','reasoning':{'text':'received local work','kind':'trace','complete':True}}]
        definition={'id':'fixture','revision':1,'subagents':[]}
        with patch('olcr_api.custom_agent_runtime.Task',return_value=prepared_task),patch('olcr_api.custom_agent_runtime.runtime_root',return_value=Path(self.tmp.name)),patch.object(runtime,'prepare',return_value=definition),patch('olcr_api.custom_agent_runtime.subprocess.Popen',return_value=process),patch('olcr_api.custom_agent_runtime.select.select',return_value=([process.stdout],[],[])),patch('olcr_api.custom_agent_runtime.os.read',return_value=(json.dumps({'type':'done','text':'answer'})+'\n').encode()):
            events=runtime.events(definition,cid,'Hello')
            meta=next(events);self.assertEqual('meta',meta['type'])
            reasoning=next(events);self.assertEqual('reasoning',reasoning['type'])
            self.assertEqual('received local work',reasoning['block']['logs'][0]['text'])
            self.assertEqual('chunk',next(events)['type'])
            done=next(events);self.assertEqual('done',done['type'])
            self.assertEqual([reasoning['block']],done['blocks']);events.close()
        with self.db.connect() as con:
            row=con.execute('SELECT state,error FROM tasks WHERE id=?',(meta['task_id'],)).fetchone()
        self.assertEqual(('completed',None),tuple(row))
        process.terminate.assert_called_once()

    def test_answer_save_failure_is_distinct_from_model_failure_and_closes_iterator(self):
        import sqlite3
        client,cid=self.agent_chat_fixture(lambda *args: {'text':'image evidence'})
        closed=[]
        def events(*args):
            try: yield {'type':'done','response':'Generated answer','task':{'id':'fixture','state':'completed'}}
            finally: closed.append(True)
        original=self.db.add_message
        def save(*args,**kwargs):
            if args[1]=='assistant': raise sqlite3.OperationalError('private-storage-detail')
            return original(*args,**kwargs)
        with patch.object(AgentRuntime,'events',side_effect=events),patch.object(self.db,'add_message',side_effect=save):
            response=client.post('/api/chat',json={'conversation_id':cid,'message':'Hello'},headers={'Origin':'tauri://localhost'})
        self.assertEqual(502,response.status_code)
        self.assertIn('AGENT_RESPONSE_SAVE_FAILED',response.text)
        self.assertNotIn('private-storage-detail',response.text)
        self.assertEqual('tauri://localhost',response.headers['access-control-allow-origin'])
        self.assertEqual([True],closed)

    def test_unexpected_image_failure_returns_a_cors_json_error_and_persists_failure(self):
        def vision(*args): raise OSError('provider disconnected')
        client,cid=self.agent_chat_fixture(vision)
        image={'data_url':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}
        with patch('olcr_api.custom_agent_runtime.model_slot',return_value=nullcontext()):
            response=client.post('/api/chat',json={'conversation_id':cid,'message':'Read image','image':image},headers={'Origin':'tauri://localhost'})
        self.assertEqual(502,response.status_code,response.text)
        self.assertEqual('tauri://localhost',response.headers['access-control-allow-origin'])
        self.assertEqual('AGENT_REQUEST_FAILED',response.json()['detail']['code'])
        with self.db.connect() as con:
            failed=con.execute("select count(*) from tasks where conversation_id=? and state='failed'",(cid,)).fetchone()[0]
        self.assertGreaterEqual(failed,1)
        self.assertNotIn('provider disconnected',response.text)

    def test_original_image_uses_vision_and_binary_is_not_sent_as_prompt_text(self):
        image=b'original-image-bytes'
        calls=[]
        model=SimpleNamespace(vision=lambda *args:(calls.append(args) or {'text':'A diagram connecting A and B'}))
        runtime=AgentRuntime(**{**self.host,'runtime':SimpleNamespace(model=model)})
        agent={'id':'a','name':'A','instructions':'Answer from references','knowledge':[{'name':'diagram.png','content':'OCR text',
               'mime_type':'image/png','file_base64':base64.b64encode(image).decode()}],'tools':[],'skills':[]}
        result=runtime.prepare(agent,'Explain the diagram')
        self.assertEqual(image,calls[0][0])
        self.assertEqual('role:general_image',calls[0][3])
        self.assertIn('A diagram connecting A and B',result['instructions'])
        self.assertNotIn('file_base64',str(result))

    def test_agent_can_call_native_youtube_search_without_an_api_key(self):
        from olcr_api.config import Settings
        from olcr_api.custom_agents import tool_catalog
        tool = next(row for row in tool_catalog() if row['id'] == 'youtube_search')
        self.assertEqual(['query'], tool['schema']['required'])
        runtime = AgentRuntime(**{**self.host, 'settings': Settings(external_access_enabled=True)})
        cid = self.db.create_conversation('Video research', 1, 'video-agent')
        task = Task('Find Python videos')
        payload = {'tool_id': 'youtube_search', 'args': {'query': 'Python tutorial'}}
        videos = [{'id':'dQw4w9WgXcQ','title':'Python tutorial','url':'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}]
        with patch('olcr_api.assistant_integrations.search_youtube', return_value=videos) as search:
            result = runtime.tool(payload, {'tools':['youtube_search']}, task, cid)
        search.assert_called_once_with('Python tutorial')
        self.assertEqual(videos, result['videos'])
        self.assertEqual('youtube_search', task.tool_executions[-1]['tool'])
        with self.assertRaisesRegex(ValueError, 'AGENT_TOOL_NOT_ALLOWED'):
            runtime.tool(payload, {'tools':[]}, task, cid)
        runtime.settings = Settings(external_access_enabled=False)
        with self.assertRaisesRegex(ValueError, 'EXTERNAL_ACCESS_DISABLED'):
            runtime.tool(payload, {'tools':['youtube_search']}, task, cid)


if __name__ == '__main__':
    unittest.main()
