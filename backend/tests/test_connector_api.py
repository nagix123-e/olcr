import unittest
from unittest.mock import Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from olcr_api.connector_api import connector_router
from olcr_api.connector_chat import intent
from olcr_api.permissions import requires_request_approval
from backend.tests.test_service_connectors import ConnectorTests


class ConnectorApiTests(ConnectorTests):
    def setUp(self):
        super().setUp()
        app=FastAPI();app.include_router(connector_router(lambda:self.service));self.client=TestClient(app)

    def test_api_policy_and_read_dispatch(self):
        self.service.set_category('mail',True)
        response=self.client.patch('/api/connectors/'+self.id+'/policy',json={'enabled':True})
        self.assertEqual(200,response.status_code,response.text)
        self.assertTrue(response.json()['enabled'])
        self.assertEqual(200,self.client.post('/api/connectors/'+self.id+'/execute',json={'action':'list','arguments':{}}).status_code)
        self.client.patch('/api/connectors/'+self.id+'/policy',json={'enabled':False})
        self.assertEqual(403,self.client.post('/api/connectors/'+self.id+'/execute',json={'action':'list'}).status_code)
        self.assertEqual(422,self.client.patch('/api/connectors/'+self.id+'/policy',json={'enabled':'yes'}).status_code)

    def test_category_endpoint_grants_all_supported_actions_and_validates_ids(self):
        response=self.client.patch('/api/connectors/categories/shopping',json={'enabled':True})
        self.assertEqual(200,response.status_code,response.text)
        row=next(row for row in response.json()['connectors'] if row['category']=='shopping')
        self.assertEqual({'read':True,'send':False,'write':True,'purchase':True},row['permissions'])
        self.assertFalse(row['enabled'])
        self.assertEqual(422,self.client.patch('/api/connectors/categories/brain',json={'enabled':True}).status_code)
        self.assertEqual(422,self.client.patch('/api/connectors/categories/mail',json={'enabled':'yes'}).status_code)

    def test_intent_does_not_claim_coding_or_model_api_configuration(self):
        for message in ('Gmail APIを実装して','Google Drive connectorを作って','モデルをGoogleに設定して','カレンダーを表示して'):
            self.assertIsNone(intent(message))
        self.assertEqual(self.id,intent('/service service.google.gmail list {}')['id'])
        self.assertTrue(requires_request_approval('POST','/api/connectors/'+self.id+'/execute'))
        self.assertFalse(requires_request_approval('PATCH','/api/connectors/'+self.id+'/policy'))

    def test_purchase_expiry_deny_and_global_access(self):
        self.service.policy('service.commerce.checkout',{'enabled':True,'purchase':True})
        review=self.service.prepare_purchase(self.order())
        with self.db.connect() as con:con.execute('UPDATE service_purchase_reviews SET expires_at=0 WHERE id=?',(review['id'],))
        self.assertEqual(422,self.client.post('/api/connectors/purchases/'+review['id']+'/confirm',json={'approve':True,'order':self.order()}).status_code)
        review=self.service.prepare_purchase(self.order())
        denied=self.client.post('/api/connectors/purchases/'+review['id']+'/confirm',json={'approve':False,'order':self.order()})
        self.assertEqual('denied',denied.json()['status']);self.assertIsNone(denied.json()['url'])
        self.settings.permission_level='automatic';self.settings.external_access_enabled=False
        self.assertEqual(403,self.client.post('/api/connectors/'+self.id+'/oauth/start',json={'client_id':'test'}).status_code)

    def test_provider_errors_do_not_echo_credentials(self):
        self.service.google.start.side_effect=ValueError('sensitive token sample')
        result=self.client.post('/api/connectors/'+self.id+'/oauth/start',json={'client_id':'test'})
        self.assertEqual('CONNECTOR_REQUEST_FAILED',result.json()['detail'])
