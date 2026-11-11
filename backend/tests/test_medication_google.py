import json
import time
import unittest
from unittest.mock import patch
from olcr_api.connector_google import GoogleConnectors, CONNECTORS

class MedicationGoogleTests(unittest.TestCase):
    def setUp(self):
        self.client=GoogleConnectors(); self.addCleanup(self.client.close)
    def bundle(self,write=False):
        cid='service.google.calendar'
        return json.dumps(dict(client_id='fixture.apps.googleusercontent.com',access_token='fake',refresh_token='fake',
            expires_at=time.time()+3600,scope=CONNECTORS[cid]['scope']+(' https://www.googleapis.com/auth/calendar.events.owned' if write else '')))
    def test_readonly_grant_keeps_reading_but_cannot_create(self):
        self.client.set_bundle('service.google.calendar',self.bundle())
        self.assertTrue(self.client.status('service.google.calendar').get('write_consent_required'))
        with self.assertRaisesRegex(ValueError,'WRITE_CONSENT'):
            self.client.calendar_request('POST','primary',payload={'id':'olcr123'})
    def test_create_uses_existing_google_credential_and_bounded_event_endpoint(self):
        self.client.set_bundle('service.google.calendar',self.bundle(True))
        with patch.object(self.client,'_request',return_value={'id':'olcr123'}) as request:
            result=self.client.calendar_request('POST','primary',payload={'id':'olcr123'})
            self.assertEqual(result['id'],'olcr123')
            self.assertEqual(request.call_args.kwargs['method'],'POST')
            self.assertIn('/calendars/primary/events',request.call_args.args[0])
        with self.assertRaises(ValueError): self.client.calendar_request('POST','../../bad',payload={})

    def test_no_refresh_accounts_are_not_given_the_same_binding(self):
        def bundle(token):
            return json.dumps(dict(client_id='fixture.apps.googleusercontent.com',access_token=token,refresh_token='',
                expires_at=time.time()+3600,scope=CONNECTORS['service.google.calendar']['scope']))
        self.client.set_bundle('service.google.calendar',bundle('account-one'))
        first=self.client.connection_fingerprint('service.google.calendar')
        self.client.set_bundle('service.google.calendar',bundle('account-two'))
        self.assertNotEqual(first,self.client.connection_fingerprint('service.google.calendar'))

    def test_calendar_delete_accepts_empty_204_and_404_is_distinguishable(self):
        import io
        from urllib.error import HTTPError
        from unittest.mock import MagicMock
        response=MagicMock(); response.__enter__.return_value=response; response.read.return_value=b''
        opener=MagicMock(); opener.open.return_value=response
        with patch('olcr_api.connector_google.urllib.request.build_opener',return_value=opener):
            self.assertEqual(self.client._request('https://www.googleapis.com/calendar/v3/test',method='DELETE',resource_errors=True),{})
        opener.open.side_effect=HTTPError('https://www.googleapis.com/calendar/v3/test',404,'missing',{},io.BytesIO(b''))
        with patch('olcr_api.connector_google.urllib.request.build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'RESOURCE_NOT_FOUND'):
                self.client._request('https://www.googleapis.com/calendar/v3/test',method='GET',resource_errors=True)
