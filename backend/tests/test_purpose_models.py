import base64
import io
import json
import tempfile
import unittest
import wave
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.purpose_models import PurposeModels, PurposeEmbeddingProvider, PurposeDecisionProvider, HANDLES
from olcr_api.semantic import EmbeddingFailure, LocalVectorStore
from olcr_api.model_assets import ModelDownloads


class PurposeModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'test.db'); self.db.initialize()
        self.settings = Settings(db_path=str(self.root / 'test.db'), allowed_roots=(str(self.root),))
        self.env = patch.dict('os.environ', {'OLCR_MODEL_ASSETS_DIR': str(self.root / 'models')}); self.env.start()
        self.models = PurposeModels(self.db, self.settings)

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def test_embedding_switch_normalizes_indices_and_persists_without_secret(self):
        config = {'protocol': 'openai', 'base_url': 'http://127.0.0.1:9001/v1', 'model': 'user-embedding'}
        response = {'data': [{'index': 1, 'embedding': [0., 1., 0.]}, {'index': 0, 'embedding': [1., 0., 0.]}]}
        with patch.object(self.models, 'http', return_value=response):
            prepared = self.models.prepare('semantic_embedding', config, 'TEST_ONLY_KEY')
            self.assertEqual({'protocol': 'preset'}, self.models.choice('semantic_embedding'))
            self.models.activate('semantic_embedding', prepared['token'])
            self.assertEqual([1., 0., 0.], self.models.embed(['a','b'])[0])
        self.assertEqual(3, self.models.choice('semantic_embedding')['dimension'])
        with self.db.connect() as con:
            self.assertNotIn('TEST_ONLY_KEY', con.execute('SELECT configuration_json FROM purpose_model_choices').fetchone()[0])
        restored = PurposeModels(self.db, self.settings)
        self.assertEqual(self.models.choice('semantic_embedding'), restored.choice('semantic_embedding'))
        self.assertEqual(self.models.namespace(), restored.namespace())

    def test_invalid_embeddings_preserve_binding_and_pending_credentials(self):
        config = {'protocol': 'openai', 'base_url': 'http://127.0.0.1:9001/v1', 'model': 'bad'}
        for rows in [[], [{'index': 0, 'embedding': [float('nan')]}], [{'index': 0, 'embedding': [1]}, {'index': 1, 'embedding': [1,2]}]]:
            with patch.object(self.models, 'http', return_value={'data': rows}), self.assertRaises(ValueError):
                self.models.prepare('semantic_embedding', config, 'TEST_ONLY_KEY')
            self.assertEqual({'protocol': 'preset'}, self.models.choice('semantic_embedding'))
            self.assertFalse(self.models.pending)

    def test_restarted_manager_restores_selected_bundle_slot(self):
        config={'protocol':'openai','base_url':'http://localhost:9001/v1','model':'embed'}
        with patch.object(self.models,'probe',return_value={'dimension':2}):
            self.models.save('semantic_embedding',config,'TEST_ONLY_KEY')
        handle=HANDLES['semantic_embedding']+self.models.choice('semantic_embedding')['key_slot']
        with patch.dict('os.environ',{handle:'TEST_ONLY_KEY'}):
            restored=PurposeModels(self.db,self.settings)
            self.assertEqual('TEST_ONLY_KEY',PurposeEmbeddingProvider(restored).key)
            self.assertEqual(self.models.bindings(),restored.bindings())

    def test_vision_content_parts_and_malformed_responses(self):
        config={'protocol':'openai','base_url':'http://localhost:9001/v1','model':'vlm'}
        with patch.object(self.models,'http',return_value={'choices':[{'message':{'content':[{'type':'text','text':'Image '},{'type':'text','text':'description'}]}}]}):
            self.assertEqual('Image description',self.models.vision('general_image',b'image','image/png','describe',config)['text'])
        for response in ({'choices':[]},{'choices':[None]},{'choices':[{'message':None}]}):
            with patch.object(self.models,'http',return_value=response),self.assertRaises(ValueError):
                self.models.vision('general_image',b'image','image/png','describe',config)

    def test_probe_single_use_and_concurrent_edit_guard(self):
        config = {'protocol': 'openai', 'base_url': 'http://localhost:9001/v1', 'model': 'embed'}
        with patch.object(self.models, 'probe', return_value={'dimension': 2}):
            first = self.models.prepare('semantic_embedding', config)
            with self.assertRaisesRegex(ValueError, 'MODEL_SWITCH_IN_PROGRESS'):
                self.models.prepare('semantic_embedding', {**config,'model':'other'})
            self.models.activate('semantic_embedding', first['token'])
            with self.assertRaises(ValueError): self.models.activate('semantic_embedding', first['token'])
            second = self.models.prepare('semantic_embedding', {**config,'model':'other'})
            self.models.cancel('semantic_embedding', second['token'])
            with self.assertRaises(ValueError): self.models.activate('semantic_embedding', second['token'])
            self.models.prepare('semantic_embedding', config)

    def test_frozen_embedding_consumers_do_not_change_space_mid_request(self):
        config = {'protocol': 'openai', 'base_url': 'http://localhost:9001/v1', 'model': 'one'}
        with patch.object(self.models, 'probe', return_value={'dimension': 2}):
            self.models.save('semantic_embedding', config, 'FIRST_TEST_KEY')
            namespace = self.models.namespace(); provider = PurposeEmbeddingProvider(self.models)
            self.models.save('semantic_embedding', {**config,'model':'two'}, 'SECOND_TEST_KEY')
        with patch.object(self.models, 'embed', return_value=[[1,0]]) as call:
            provider.embed(['query'], namespace)
            self.assertEqual('one', call.call_args.args[1]['model'])
            self.assertEqual('FIRST_TEST_KEY', call.call_args.args[2])
        self.assertNotEqual(namespace, self.models.namespace())

    def test_jev_adapter_uses_native_questions_for_all_primitives(self):
        config = {'protocol': 'jev', 'base_url': 'http://localhost:9001', 'model': 'clef-or-openjev'}
        candidates = [{'tool_id':'yes','criteria':'Direct match'},{'tool_id':'no','criteria':'No match'}]
        def http(role, config, path, payload, **kwargs):
            self.assertEqual('/v1/systemone', path)
            answers = {}
            for key, q in payload['questions'].items():
                if q['type']=='choice': answers[key]={'choice':'yes','probabilities':{'yes':.9,'no':.1},'confidence':.9}
                elif q['type']=='noul':
                    self.assertNotIn('criteria',q); answers[key]={'noul':.8 if key=='0' else .2}
                else:
                    self.assertIsInstance(q['criteria'],list);answers[key]={'score':1.8 if key=='0' else .2}
            return {'answers':answers}
        with patch.object(self.models,'http',side_effect=http):
            self.models.save('typed_local_decisions',config)
            provider=PurposeDecisionProvider(self.models,SimpleNamespace(loaded=False))
            self.assertEqual('yes',provider.choice(question='query',candidates=candidates)['tool_id'])
            self.assertEqual('yes',provider.noul(question='query',candidates=candidates)['tool_id'])
            self.assertAlmostEqual(.9,provider.score(question='query',candidates=candidates)['scores']['yes'])

    def test_vision_role_runtime_uses_selected_provider_and_keeps_json_contract(self):
        config = {'protocol':'openai','base_url':'http://localhost:9001/v1','model':'my-vlm'}
        raw = {'choices':[{'message':{'content':'{"elements":[],"text":[],"confidence":1,"uncertainty":[]}'}}]}
        with patch.object(self.models,'http',return_value=raw):
            self.models.save('gui_grounding',config)
            runtime=RoleAwareModelProvider(self.settings);runtime.purpose_models=self.models
            result=runtime.vision(b'PNG','image/png','Find the button.','role:gui_grounding')
            self.assertEqual('my-vlm',result['model']);self.assertEqual('gui_grounding',result['model_role'])
            runtime.close()
        with patch.object(self.models,'http',return_value={'choices':[{'message':{'content':'not json'}}]}),self.assertRaises(RuntimeError):
            self.models.prepare('gui_grounding',{**config,'model':'invalid'})
        self.assertEqual('my-vlm',self.models.choice('gui_grounding')['model'])

    def test_transcription_multipart_preserves_language_and_text(self):
        config={'protocol':'openai','base_url':'http://localhost:9001/v1','model':'user-asr'}
        buf=io.BytesIO()
        with wave.open(buf,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0'*3200)
        def http(role,config,path,**kwargs):
            self.assertEqual('/audio/transcriptions',path)
            self.assertIn(b'filename="clip.wav"',kwargs['data']);self.assertIn(b'user-asr',kwargs['data'])
            return {'text':'こんにちは'}
        with patch.object(self.models,'http',side_effect=http):
            self.models.save('transcription_ja',config)
            result=self.models.transcribe(base64.b64encode(buf.getvalue()).decode(),'ja')
            self.assertEqual({'text':'こんにちは','model':'user-asr'},result)

    def test_remote_disabled_and_wrong_role_protocol_are_rejected(self):
        self.settings.external_access_enabled=False
        with self.assertRaisesRegex(Exception,'外部アクセス'):
            self.models._headers('semantic_embedding',{'base_url':'https://api.example.com'})
        self.assertEqual({},self.models._headers('semantic_embedding',{'base_url':'http://localhost:9001'}))
        with self.assertRaises(ValueError):self.models.validate('semantic_embedding',{'protocol':'jev','model':'x'})
        with self.assertRaises(ValueError):self.models.validate('gui_grounding',{'protocol':'openai','base_url':'http://example.com','model':'x'})

    def test_download_consent_allowlist_and_duplicate_job_guard(self):
        downloads=ModelDownloads(self.settings)
        with self.assertRaises(ValueError):downloads.start(['embeddinggemma'],False)
        with self.assertRaises(ValueError):downloads.start(['arbitrary-url'],True)
        with patch('threading.Thread'):
            job=downloads.start(['embeddinggemma','embeddinggemma'],True)
            self.assertEqual(['embeddinggemma'],job['models'])
            with self.assertRaises(ValueError):downloads.start(['embeddinggemma'],True)

    def test_real_http_roundtrip_for_embedding_vision_decision_and_transcription(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']));seen.append(self.path)
                if self.path=='/embeddings':
                    payload=json.loads(raw);value={'data':[{'index':i,'embedding':[1.,float(i+1)]} for i,_ in enumerate(payload['input'])]}
                elif self.path=='/chat/completions':
                    payload=json.loads(raw);self.server.saw_image=payload['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
                    value={'choices':[{'message':{'content':'{"elements":[],"text":[],"confidence":1,"uncertainty":[]}'}}]}
                elif self.path=='/v1/systemone':
                    payload=json.loads(raw);value={'answers':{key:({'choice':'yes','confidence':.9,'probabilities':{'yes':.9,'no':.1}} if q['type']=='choice' else {'noul':.8} if q['type']=='noul' else {'score':1.8}) for key,q in payload['questions'].items()}}
                elif self.path=='/audio/transcriptions':
                    self.server.saw_wav=b'RIFF' in raw and b'filename="clip.wav"' in raw;value={'text':'Recognized text'}
                else: self.send_error(404);return
                response=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(response)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            for role,protocol in [('semantic_embedding','openai'),('typed_local_decisions','jev'),('gui_grounding','openai'),('transcription_ja','openai')]:
                with self.subTest(role=role):self.models.save(role,{'protocol':protocol,'base_url':base,'model':'test-model'})
            self.assertTrue(server.saw_image);self.assertTrue(server.saw_wav)
            self.assertEqual({'/embeddings','/v1/systemone','/chat/completions','/audio/transcriptions'},set(seen))
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
