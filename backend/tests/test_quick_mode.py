import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from olcr_api.brain_models import BrainModels, INITIAL, brain_scope
from olcr_api.brain_providers import quick_profile, DEFAULT_BASE_URLS
from olcr_api.config import Settings
from olcr_api.db import Database

class QuickModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'quick.db');self.db.initialize()
        self.models=BrainModels(self.db,Settings(external_access_enabled=True))
    def tearDown(self): self.tmp.cleanup()
    def test_only_supported_models_on_official_endpoints(self):
        self.assertEqual('openai_fast',quick_profile('openai_oauth','gpt-6-luna',DEFAULT_BASE_URLS['openai']))
        self.assertIsNone(quick_profile('openai','gpt-6-luna','https://proxy.example/v1'))
        self.assertIsNone(quick_profile('google','gemini-3.1-pro',DEFAULT_BASE_URLS['google']))
        self.assertIsNone(quick_profile('anthropic','claude-opus-4-6',DEFAULT_BASE_URLS['anthropic']))
    def test_transport_parameters_and_disabled_requests(self):
        cases=[('openai','gpt-6-luna'),('openai_oauth','gpt-6-luna'),('anthropic','claude-opus-5-5'),('anthropic','claude-opus-4-8')]
        for provider,model in cases:
            for quick in (False,True):
                with self.subTest(provider=provider,model=model,quick=quick):
                    metadata={'model_ids':[model],'base_url':DEFAULT_BASE_URLS['openai' if provider=='openai_oauth' else provider]}
                    payloads=[]
                    def call(url,payload,headers):
                        payloads.append((copy.deepcopy(payload),dict(headers)))
                        return {'choices':[{'message':{'content':'answer'}}],'content':[{'type':'text','text':'answer'}]}
                    def stream(url,payload,token,headers):
                        payloads.append((copy.deepcopy(payload),dict(headers)));return 'answer',{}
                    p=self.models.providers
                    with patch.object(p,'metadata',return_value=metadata),patch.object(p,'token',return_value='fixture'),patch('olcr_api.brain_providers.call_json',side_effect=call),patch.object(p,'_responses_stream',side_effect=stream):
                        p._generate_account(provider,model,[{'role':'user','content':'hello'}],None,True,quick=quick)
                    body,headers=payloads[0]
                    if quick and provider.startswith('openai'): self.assertEqual('fast',body['service_tier'])
                    elif quick: self.assertEqual('fast',body['speed'])
                    elif provider.startswith('openai'): self.assertEqual('default',body['service_tier'])
                    else: self.assertNotIn('service_tier',body);self.assertNotIn('speed',body)
                    self.assertEqual(quick and model=='claude-opus-4-8','anthropic-beta' in headers)
    def test_persistence_scope_and_unsupported_rejection(self):
        mid='api:openai:gpt-6-luna'
        value=copy.deepcopy(INITIAL);value['quick_models']={mid:True}
        with patch.object(self.models,'catalog',return_value={'models':[{'id':mid,'available':True,'quick_supported':True}]}):
            saved=self.models.save(value)
            self.assertTrue(self.models.configuration()['quick_models'][mid])
            with brain_scope({**saved,'quick_models':{}}): self.assertFalse(self.models.quick_for_model(mid))
            self.assertTrue(self.models.quick_for_model(mid))
            value['quick_models']['ollama:unsupported']=True
            with self.assertRaises(ValueError): self.models.save(value)
