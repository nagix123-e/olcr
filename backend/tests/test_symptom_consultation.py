import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from olcr_api.symptom_consultation import consultation_router
from olcr_api.symptom_provider import InfermedicaProvider


class ConsultationTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.diagnosis = {'should_stop': False, 'has_emergency_evidence': False,
                          'conditions': [{'id': 'c_1', 'common_name': '片頭痛', 'probability': .7}],
                          'question': {'type': 'single', 'text': '吐き気はありますか？',
                                       'items': [{'id': 's_2', 'name': '吐き気'}]}}
        self.triage = {'triage_level': 'consultation', 'serious': []}
        self.failure = None
        def transport(request):
            self.calls.append(request)
            if self.failure:
                return httpx.Response(self.failure, json={'secret': 'private health detail'})
            path = request.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={
                'search': [{'id': 's_1', 'label': '頭痛'}],
                'diagnosis': self.diagnosis, 'triage': self.triage,
                'recommend_specialist': {'recommended_specialist': {'id': 'sp_17', 'name': '神経内科'}},
            }[path])
        self.provider = InfermedicaProvider('test-id', 'test-key', transport=httpx.MockTransport(transport))
        self.settings = SimpleNamespace(external_access_enabled=True, permission_level='automatic')
        app = FastAPI()
        app.include_router(consultation_router(lambda: self.settings, lambda: self.provider))
        self.client = TestClient(app)
        self.env = patch.dict(os.environ, {'OLCR_SYMPTOM_ENABLED': '1'})
        self.env.start()
        self.payload = {'consent': True, 'age': 30, 'sex': 'female', 'country': 'JP',
                        'emergency': 'absent', 'high_risk': False, 'interview_id': str(uuid4()),
                        'language': 'ja', 'evidence': [{'id': 's_1', 'choice_id': 'present', 'source': 'initial'}],
                        'question_count': 0}

    def tearDown(self):
        self.env.stop()
        self.client.close()

    def assess(self, **changes):
        return self.client.post('/api/symptom-consultations/assess', json={**self.payload, **changes})

    def test_disabled_or_no_consent_never_sends_data(self):
        with patch.dict(os.environ, {'OLCR_SYMPTOM_ENABLED': '0'}):
            self.assertEqual(self.assess().status_code, 503)
        self.assertEqual(self.assess(consent=False).status_code, 422)
        self.settings.external_access_enabled = False
        self.assertEqual(self.assess().status_code, 403)
        self.assertEqual(self.calls, [])

    def test_emergency_and_unknown_short_circuit(self):
        result = self.assess(emergency='present').json()
        self.assertEqual(result['status'], 'urgent')
        self.assertEqual(result['possible_causes'], [])
        self.assertIn('119', result['advice'])
        self.assertEqual(self.assess(emergency='unknown').json()['status'], 'insufficient')
        self.assertEqual(self.calls, [])

    def test_out_of_scope_and_invalid_body_are_private(self):
        self.assertEqual(self.assess(high_risk=True).json()['status'], 'out_of_scope')
        result = self.assess(age=12, evidence=[{'id': 'private health detail'}])
        self.assertEqual(result.status_code, 422)
        self.assertNotIn('private health detail', result.text)
        self.assertEqual(self.calls, [])

    def test_follow_up_is_provider_generated_and_no_probability_exposed(self):
        result = self.assess().json()
        self.assertEqual(result['status'], 'questioning')
        self.assertEqual(result['question']['text'], '吐き気はありますか？')
        self.assertNotIn('probability', str(result))
        self.assertEqual(self.calls[0].headers['Model'], 'infermedica-ja')
        self.assertEqual(self.calls[0].headers['Interview-Id'], self.payload['interview_id'])

    def test_engine_emergency_overrides_stop_and_candidates(self):
        self.triage = {'triage_level': 'emergency', 'serious': []}
        result = self.assess().json()
        self.assertEqual(result['status'], 'urgent')
        self.assertIsNone(result['question'])
        self.assertEqual(result['possible_causes'], [])
        self.assertNotIn('recommend_specialist', str([r.url for r in self.calls]))

    def test_emergency_evidence_not_downgraded_by_triage(self):
        self.diagnosis['has_emergency_evidence'] = True
        self.assertEqual(self.assess().json()['status'], 'urgent')

    def test_upper_limit_does_not_claim_complete(self):
        result = self.assess(question_count=6).json()
        self.assertEqual(result['status'], 'insufficient')
        self.assertIsNone(result['question'])
        self.assertEqual(result['possible_causes'], [])

    def test_finished_result_contains_specialist(self):
        self.diagnosis['should_stop'] = True
        result = self.assess().json()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['department'], '脳神経内科')
        self.assertEqual(result['possible_causes'][0]['name'], '片頭痛')
        self.assertEqual(result['reference_url'], 'https://www.opensymptom.com/')
        self.assertIn('no-store', self.assess().headers['cache-control'])

    def test_provider_failure_is_redacted_and_no_model_fallback(self):
        self.failure = 403
        response = self.assess()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('private health detail', response.text)

    def test_search_uses_post_locally_and_only_patient_observations_remotely(self):
        result = self.client.post('/api/symptom-consultations/search', json={
            **{k: v for k, v in self.payload.items() if k not in ('evidence', 'question_count')}, 'phrase': '頭痛'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['items'][0]['label'], '頭痛')
        self.assertEqual(self.calls[0].url.params['include_pro'], 'false')

    def test_headache_self_care_only_after_complete_low_urgency_result(self):
        self.diagnosis['should_stop'] = True
        self.triage = {'triage_level': 'self_care', 'serious': []}
        result = self.assess(complaint='headache').json()
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['self_care'])
        self.triage['triage_level'] = 'consultation_24'
        self.assertEqual(self.assess(complaint='headache').json()['self_care'], [])

    def test_invalid_provider_response_and_unknown_seriousness_fail_closed(self):
        self.diagnosis['conditions'][0]['probability'] = 'not a number'
        self.assertEqual(self.assess().status_code, 502)
        self.diagnosis['conditions'][0]['probability'] = .7
        self.triage['serious'] = [{'seriousness': 'unexpected'}]
        self.assertEqual(self.assess().status_code, 502)

    def test_permission_confirmation_covers_health_requests(self):
        from olcr_api.permissions import requires_request_approval
        self.assertTrue(requires_request_approval('POST', '/api/symptom-consultations/assess'))
        self.assertFalse(requires_request_approval('GET', '/api/symptom-consultations/status'))

    def test_http_client_logging_does_not_record_search_phrase(self):
        with self.assertNoLogs('httpx', level='INFO'):
            self.provider.request('search', {'phrase': 'private health detail'},
                                  interview_id=self.payload['interview_id'], language='ja')

    def test_engine_unknown_root_cause_never_claims_self_care(self):
        self.diagnosis['should_stop'] = True
        self.triage = {'triage_level': 'self_care', 'root_cause': 'diagnosis_unknown', 'serious': []}
        result = self.assess().json()
        self.assertEqual(result['status'], 'insufficient')
        self.assertEqual(result['urgency'], 'undetermined')


if __name__ == '__main__':
    unittest.main()
