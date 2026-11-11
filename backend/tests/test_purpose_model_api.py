import base64
import copy
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.purpose_models import PurposeModels, PurposeDecisionProvider


class PurposeModelApiTests(unittest.TestCase):
    def test_whole_passage_spanish_translation_uses_translation_role(self):
        passage = ('檸檬\n梶井基次郎\n京都の丸善で画集を眺めた。\n\n' * 200).strip()
        for endpoint in ('/api/chat', '/api/chat/stream'):
            with self.subTest(endpoint=endpoint), patch.object(self.manager, 'translate', return_value={
                    'text': 'El limón', 'model': 'plamo_3_translate_2b',
                    'model_runtime': 'mlx'}) as translate, \
                 patch.object(api.runtime.model, 'generate', side_effect=AssertionError('normal Brain must not run')):
                response = self.client.post(endpoint, json={
                    'message': passage + '\n\n##スペイン語に全部訳して##'})
            self.assertEqual(200, response.status_code, response.text)
            if endpoint.endswith('/stream'):
                events = [json.loads(line[6:]) for line in response.text.splitlines()
                          if line.startswith('data: ')]
                answer = ''.join(event.get('text', '') for event in events if event['type'] == 'chunk')
                task = next(event['task'] for event in events if event['type'] == 'done')
            else:
                body = response.json()
                answer, task = body['response'], body['task']
            self.assertEqual('El limón', answer)
            self.assertEqual('TRANSLATION_MODEL', task['reason_category'])
            self.assertEqual('completed', task['state'])
            translate.assert_called_once_with(passage, 'ja', 'es')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=Database(self.root/'test.db');self.db.initialize()
        self.settings=Settings(db_path=str(self.root/'test.db'),allowed_roots=(str(self.root),))
        self.manager=PurposeModels(self.db,self.settings)
        self.patches=[patch.object(api,'db',self.db),patch.object(api,'settings',self.settings),
            patch.object(api,'purpose_models',self.manager),patch.object(api,'_gui_session_token','purpose-test-session'),
            patch.object(api,'_api_embedding_provider'),patch.object(api,'vectors'),patch.object(api,'conversation_memory'),
            patch.object(api.retrieval,'vectors'),patch.object(api.retrieval,'graphify'),
            patch.object(api,'laya_provider',PurposeDecisionProvider(self.manager,SimpleNamespace(loaded=False)))]
        for p in self.patches:p.start()
        self.client=TestClient(api.app);self.client.headers['X-OLCR-Session']='purpose-test-session'
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def activate(self,role,config,response):
        with patch.object(self.manager,'http',return_value=response):
            checked=self.client.post('/api/purpose-models/'+role+'/probe',json={'configuration':config,'confirm_remote':True})
            self.assertEqual(200,checked.status_code,checked.text)
        token=checked.json()['token']
        result=self.client.post('/api/purpose-models/'+role+'/activate',json={'token':token})
        self.assertEqual(200,result.status_code,result.text)

    def test_embedding_selection_changes_all_retrieval_consumers_and_restart_configuration(self):
        config={'protocol':'openai','base_url':'http://localhost:9001/v1','model':'my-embedding'}
        self.activate('semantic_embedding',config,{'data':[{'index':0,'embedding':[1,0]},{'index':1,'embedding':[0,1]}]})
        self.assertEqual('my-embedding',api.vectors.provider.config['model'])
        self.assertEqual('my-embedding',api.retrieval.graphify.embedding.config['model'])
        self.assertEqual('my-embedding',api.conversation_memory.provider.config['model'])
        self.assertEqual('my-embedding',api._api_embedding_provider.config['model'])
        self.assertTrue(api.settings.embedding_model.startswith('purpose:'))
        restored=PurposeModels(self.db,self.settings)
        self.assertEqual(self.manager.choice('semantic_embedding'),restored.choice('semantic_embedding'))

    def test_transcription_choice_is_used_by_plain_and_streaming_endpoints(self):
        config={'protocol':'openai','base_url':'http://localhost:9001/v1','model':'my-asr'}
        self.activate('transcription_ja',config,{'text':'こんにちは'})
        buf=io.BytesIO()
        with wave.open(buf,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0'*3200)
        body={'language':'ja','audio_base64':base64.b64encode(buf.getvalue()).decode()}
        with patch.object(self.manager,'http',return_value={'text':'日本語を認識'}):
            self.assertEqual('my-asr',self.client.get('/api/audio/model/status?language=ja').json()['model'])
            response=self.client.post('/api/audio/transcribe',json=body)
            self.assertEqual({'text':'日本語を認識','model':'my-asr'},response.json())
            events=[json.loads(line) for line in self.client.post('/api/audio/transcribe/stream',json=body).text.splitlines()]
            self.assertEqual('done',events[-1]['type']);self.assertEqual('my-asr',events[-1]['model'])

    def test_failed_switch_and_unauthorized_requests_do_not_change_selection(self):
        body={'configuration':{'protocol':'openai','base_url':'http://localhost:9001/v1','model':'bad'},'confirm_remote':True}
        with patch.object(self.manager,'http',return_value={'data':[]}):
            self.assertEqual(422,self.client.post('/api/purpose-models/semantic_embedding/probe',json=body).status_code)
        self.assertEqual({'protocol':'preset'},self.manager.choice('semantic_embedding'))
        self.assertEqual(401,self.client.post('/api/purpose-models/semantic_embedding/probe',json=body,headers={'X-OLCR-Session':'bad'}).status_code)
        self.assertEqual(422,self.client.post('/api/model-downloads',json={'model_ids':['embeddinggemma']}).status_code)

    def test_custom_decision_binding_is_used_for_existing_laya_contract(self):
        config={'protocol':'jev','base_url':'http://localhost:9001','model':'openjev'}
        def http(role,config,path,payload,**kwargs):
            return {'answers':{key:({'choice':'yes','confidence':.9,'probabilities':{'yes':.9,'no':.1}} if q['type']=='choice' else {'noul':.8} if q['type']=='noul' else {'score':1.8}) for key,q in payload['questions'].items()}}
        with patch.object(self.manager,'http',side_effect=http):
            checked=self.client.post('/api/purpose-models/typed_local_decisions/probe',json={'configuration':config,'confirm_remote':True})
            self.assertEqual(200,checked.status_code,checked.text)
            self.client.post('/api/purpose-models/typed_local_decisions/activate',json={'token':checked.json()['token']})
            result=api.laya_provider.choice(question='Choose',candidates=[{'tool_id':'yes'},{'tool_id':'no'}])
            self.assertEqual('yes',result['tool_id']);self.assertEqual('openjev',api.laya_provider.last_trace['decision_model'])
            health = self.client.get('/api/health').json()
            self.assertEqual('openjev', health['routing']['model'])
            self.assertEqual('jev', health['purpose_bindings']['typed_local_decisions']['protocol'])

    def test_cancelled_keychain_save_releases_slot_without_changing_binding(self):
        body={'configuration':{'protocol':'openai','base_url':'http://localhost:9001/v1','model':'embed'},'api_key':'TEST_ONLY_KEY'}
        with patch.object(self.manager,'probe',return_value={'dimension':2}):
            checked=self.client.post('/api/purpose-models/semantic_embedding/probe',json=body)
            token=checked.json()['token']
            self.assertEqual(422,self.client.post('/api/purpose-models/semantic_embedding/probe',json=body).status_code)
            self.assertEqual(200,self.client.post('/api/purpose-models/semantic_embedding/cancel',json={'token':token}).status_code)
            self.assertEqual({'protocol':'preset'},self.manager.choice('semantic_embedding'))
            self.assertEqual(200,self.client.post('/api/purpose-models/semantic_embedding/probe',json=body).status_code)
