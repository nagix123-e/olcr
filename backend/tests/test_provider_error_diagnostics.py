import unittest,json,io
from urllib.error import HTTPError
from olcr_api.provider_errors import provider_http_failure,failure_diagnostics
from olcr_api.structured_output import openai_schema_supported
from olcr_api.coding_tasks import plan_schema
class ProviderErrors(unittest.TestCase):
 def test_schema_failure_is_safe_and_actionable(self):
  failure=provider_http_failure(HTTPError('https://example.test',400,'bad',{},io.BytesIO(json.dumps({'error':{'code':'invalid_json_schema','param':'text.format.schema','message':'SECRET PROMPT'}}).encode())))
  self.assertEqual('invalid_json_schema',failure.provider_code)
  self.assertEqual('text.format.schema',failure.parameter)
  self.assertTrue(failure.schema_format_rejection)
  self.assertNotIn('SECRET',str(failure))
  self.assertEqual(400,failure_diagnostics(failure)['http_status'])
 def test_reasoning_parameter_error_is_not_marked_as_schema_failure(self):
  failure=provider_http_failure(HTTPError('https://example.test',400,'bad',{},io.BytesIO(json.dumps({'error':{'code':'invalid_request_error','param':'reasoning.effort','message':'Unsupported reasoning.effort'}}).encode())))
  self.assertEqual('reasoning.effort',failure.parameter)
  self.assertFalse(failure.schema_format_rejection)
  self.assertNotIn('Unsupported reasoning',str(failure))
 def test_explicit_schema_error_code_is_not_overridden_by_a_schema_property_name(self):
  for property_name in ('messages','input','model'):
   with self.subTest(property_name=property_name):
    body={'error':{'code':'invalid_json_schema',
     'message':f'Invalid JSON schema at property {property_name}: additionalProperties must be false'}}
    failure=provider_http_failure(HTTPError('https://example.test',400,'bad',{},
     io.BytesIO(json.dumps(body).encode())))
    self.assertTrue(failure.schema_format_rejection)
 def test_google_response_mime_type_rejection_can_fall_back_to_prompted_json(self):
  body={'error':{'status':'INVALID_ARGUMENT','param':'generationConfig.responseMimeType',
   'message':'The response MIME type is unsupported'}}
  failure=provider_http_failure(HTTPError('https://example.test',400,'bad',{},
   io.BytesIO(json.dumps(body).encode())))
  self.assertTrue(failure.schema_format_rejection)
 def test_provider_schema_parameter_paths_are_recognized_without_leaking_body(self):
  for param in ('response_format.json_schema.schema','generationConfig.responseJsonSchema','output_config.format.schema'):
   with self.subTest(param=param):
    failure=provider_http_failure(HTTPError('https://example.test',400,'bad',{},io.BytesIO(json.dumps({'error':{'code':'invalid_request_error','param':param,'message':'private details'}}).encode())))
    self.assertEqual(param,failure.parameter)
    self.assertTrue(failure.schema_format_rejection)
    self.assertNotIn('private details',str(failure))
 def test_schema_error_messages_without_parameter_are_classified_narrowly(self):
  schema=provider_http_failure(HTTPError('https://example.test',400,'bad',{},io.BytesIO(
   b'{"error":{"code":"invalid_request_error","message":"JSON schema response format is unsupported"}}')))
  ordinary=provider_http_failure(HTTPError('https://example.test',400,'bad',{},io.BytesIO(
   b'{"error":{"code":"invalid_request_error","message":"Invalid token budget"}}')))
  self.assertTrue(schema.schema_format_rejection)
  self.assertFalse(ordinary.schema_format_rejection)
 def test_quota_auth_and_outage_are_different(self):
  for status,category in [(429,'rate_limited'),(401,'credential_expired'),(503,'provider_unavailable'),(403,'access_denied')]:
   failure=provider_http_failure(HTTPError('https://example.test',status,'bad',{'Retry-After':'60'},io.BytesIO(b'{"error":{"message":"SECRET"}}')))
   self.assertEqual(category,failure.category)
   self.assertIsNotNone(failure.retry_at)
 def test_quota_message_model_dimension_is_not_reported_as_request_parameter(self):
  body={'error':{'code':429,'status':'RESOURCE_EXHAUSTED',
   'message':'Quota exceeded for model gemini-4-argon; check project quota.',
   'details':[{'@type':'type.googleapis.com/google.rpc.QuotaFailure','violations':[
    {'quotaMetric':'generate_content_requests','quotaDimensions':{'model':'gemini-4-argon'}}]}]}}
  failure=provider_http_failure(HTTPError('https://example.test',429,'limited',{},
   io.BytesIO(json.dumps(body).encode())))
  self.assertEqual('rate_limited',failure.category)
  self.assertIsNone(failure.parameter)
  self.assertNotIn('parameter',failure_diagnostics(failure))
 def test_google_message_explicit_field_path_remains_actionable(self):
  body={'error':{'status':'INVALID_ARGUMENT',
   'message':"Invalid value at 'generationConfig.responseSchema': unsupported JSON schema"}}
  failure=provider_http_failure(HTTPError('https://example.test',400,'bad',{},
   io.BytesIO(json.dumps(body).encode())))
  self.assertEqual('generationConfig.responseSchema',failure.parameter)
  self.assertTrue(failure.schema_format_rejection)
 def test_plan_with_optional_fields_and_dynamic_maps_uses_prompted_json(self):
  self.assertFalse(openai_schema_supported(plan_schema()))
  self.assertTrue(openai_schema_supported({'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}))
