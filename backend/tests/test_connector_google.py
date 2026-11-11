import json
import time
import unittest
from unittest.mock import Mock, patch

from olcr_api.connector_google import GoogleConnectors, CONNECTORS


class GoogleConnectorTests(unittest.TestCase):
    def setUp(self):
        self.client = GoogleConnectors()
        self.addCleanup(self.client.close)

    def bundle(self, connector='service.google.gmail', **extra):
        return json.dumps(dict(client_id='test.apps.googleusercontent.com', access_token='fixture-token',
                               refresh_token='fixture-refresh', expires_at=time.time()+3600,
                               scope=' '.join((CONNECTORS[connector]['scope'],*CONNECTORS[connector].get('additional_scopes',()))), **extra))

    def test_default_disconnected_and_invalid_bundle_never_connects(self):
        self.assertEqual('disconnected', self.client.status('service.google.gmail')['state'])
        with self.assertRaises(ValueError):
            self.client.set_bundle('service.google.gmail', '{"access_token":"x"}')

    def test_expired_token_refreshes_before_read_and_scopes_are_isolated(self):
        value=json.loads(self.bundle());value['expires_at']=0
        self.client.set_bundle('service.google.gmail',json.dumps(value))
        with patch.object(self.client,'_request',side_effect=[{'access_token':'fresh','expires_in':3600}, {'messages':[{'id':'abc'}]}]) as call:
            result=self.client.execute('service.google.gmail','list',{'query':'from:example.invalid','limit':5})
        self.assertEqual('abc',result['messages'][0]['id'])
        pending=self.client.pending_credential('service.google.gmail')
        self.assertIn('fresh',pending)
        with self.assertRaises(ValueError):self.client.acknowledge_credential('service.google.gmail','changed')
        self.client.acknowledge_credential('service.google.gmail',pending)
        self.assertIsNone(self.client.pending_credential('service.google.gmail'))
        self.assertIn('oauth2.googleapis.com/token',call.call_args_list[0].args[0])
        self.assertIn('maxResults=5',call.call_args_list[1].args[0])
        self.assertEqual('disconnected',self.client.status('service.google.drive')['state'])

    def test_read_only_action_validation_and_no_arbitrary_destination(self):
        self.client.set_bundle('service.google.gmail',self.bundle())
        with patch.object(self.client,'_request') as call:
            for action,args in [('send',{}),('get',{'id':'../../malicious'}),('list',{'url':'https://evil.invalid'})]:
                with self.assertRaises(ValueError):self.client.execute('service.google.gmail',action,args)
        call.assert_not_called()

    def test_provider_auth_error_becomes_safe_reconnect_state(self):
        self.client.set_bundle('service.google.gmail',self.bundle())
        with patch.object(self.client,'_request',side_effect=ValueError('CONNECTOR_RECONNECT_REQUIRED')):
            with self.assertRaisesRegex(ValueError,'RECONNECT_REQUIRED'):
                self.client.execute('service.google.gmail','list',{})
        self.assertEqual('reconnect_required',self.client.status('service.google.gmail')['state'])

    def test_oauth_uses_pkce_state_scoped_loopback_and_retries_until_keychain_ack(self):
        from urllib.parse import parse_qs,urlsplit
        server=Mock(server_address=('127.0.0.1',45678))
        with patch('olcr_api.connector_google.HTTPServer',return_value=server),patch('threading.Thread.start'):
            attempt=self.client.start('service.google.calendar','test.apps.googleusercontent.com')
        query=parse_qs(urlsplit(attempt['url']).query)
        self.assertEqual(['S256'],query['code_challenge_method'])
        self.assertEqual([' '.join((CONNECTORS['service.google.calendar']['scope'],*CONNECTORS['service.google.calendar'].get('additional_scopes',())))],query['scope'])
        self.assertTrue(query['redirect_uri'][0].startswith('http://127.0.0.1:'))
        self.assertFalse(self.client._complete(attempt['id'],'wrong','code'))
        with patch.object(self.client,'_request',return_value={'access_token':'fixture','refresh_token':'refresh','expires_in':3600}):
            self.assertTrue(self.client._complete(attempt['id'],query['state'][0],'code'))
        ready=self.client.poll(attempt['id'])
        self.assertEqual('ready',ready['status'])
        self.assertIn('refresh',ready['bundle'])
        self.assertEqual(ready,self.client.poll(attempt['id']))
        self.assertEqual('disconnected',self.client.status('service.google.calendar')['state'])
        self.client.set_bundle('service.google.calendar',ready['bundle'])
        self.assertEqual('connected',self.client.status('service.google.calendar')['state'])
        with self.assertRaises(ValueError):self.client.poll(attempt['id'])

    def test_disconnect_does_not_reload_environment_credentials(self):
        connector='service.google.gmail'
        with patch.dict('os.environ',{CONNECTORS[connector]['handle']:self.bundle()}):
            client=GoogleConnectors();self.addCleanup(client.close)
            self.assertEqual('connected',client.status(connector)['state'])
            client.disconnect(connector)
            self.assertEqual('disconnected',client.status(connector)['state'])

    def test_drive_get_exports_google_doc_as_bounded_text(self):
        connector='service.google.drive'
        self.client.set_bundle(connector,self.bundle(connector))
        with patch.object(self.client,'_request',return_value={'id':'doc1','mimeType':'application/vnd.google-apps.document','name':'Notes'}),patch.object(self.client,'_request_text',return_value={'content':'Original notes','truncated':False}) as text:
            value=self.client.execute(connector,'get',{'id':'doc1'})
        self.assertEqual('Original notes',value['content'])
        self.assertIn('/files/doc1/export?mimeType=text%2Fplain',text.call_args.args[0])

    def test_drive_binary_metadata_never_decoded_as_text(self):
        connector='service.google.drive'
        self.client.set_bundle(connector,self.bundle(connector))
        with patch.object(self.client,'_request',return_value={'id':'doc1','mimeType':'application/pdf'}),patch.object(self.client,'_request_text') as text:
            value=self.client.execute(connector,'get',{'id':'doc1'})
        text.assert_not_called()
        self.assertEqual('unsupported_binary',value['content_state'])

    def test_network_revocation_checked_before_oauth_and_read_transport(self):
        client=GoogleConnectors(network_allowed=lambda:False);self.addCleanup(client.close)
        client.set_bundle('service.google.gmail',self.bundle())
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(ValueError,'EXTERNAL_ACCESS_REQUIRED'):
                client.execute('service.google.gmail','list',{})
            with self.assertRaisesRegex(ValueError,'EXTERNAL_ACCESS_REQUIRED'):
                client._request('https://oauth2.googleapis.com/token',form={'code':'fixture'})
        opener.assert_not_called()
