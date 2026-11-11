import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from olcr_api.db import Database
from olcr_api.apple_mail import AppleMailConnector
from olcr_api.service_connectors import ServiceConnectors, ConnectorError, connector_network_allowed


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'connectors.db');self.db.initialize()
        self.settings=SimpleNamespace(permission_level='full',external_access_enabled=True)
        self.google=Mock();self.google.status.return_value={'state':'connected'}
        self.google.execute.return_value={'items':[]}
        self.service=ServiceConnectors(self.db,lambda:self.settings,self.google)
        self.id='service.google.gmail'

    def test_disabled_cannot_execute_even_in_full_mode_and_scope_is_independent(self):
        with self.assertRaises(ConnectorError):self.service.execute(self.id,'list',{})
        self.service.set_category('mail',True);self.service.policy(self.id,{'enabled':True})
        self.service.execute(self.id,'list',{});self.google.execute.assert_called_once()
        self.service.policy(self.id,{'enabled':False})
        with self.assertRaises(ConnectorError):self.service.execute(self.id,'list',{})
        self.service.set_category('mail',False);self.service.policy(self.id,{'enabled':True})
        with self.assertRaises(ConnectorError):self.service.execute(self.id,'list',{})

    def test_policy_survives_restart_and_disconnect_does_not_restore_on_launch(self):
        self.service.set_category('mail',True);self.service.policy(self.id,{'enabled':True});self.service.disconnect(self.id)
        restarted=ServiceConnectors(self.db,lambda:self.settings,self.google)
        row=next(x for x in restarted.inventory() if x['id']==self.id)
        self.assertEqual('disconnected',row['state']);self.assertFalse(row['enabled'])
        with self.assertRaises(ConnectorError):restarted.execute(self.id,'list',{})

    def order(self):
        return {'merchant':'shop.example','url':'https://shop.example/checkout','items':[{'name':'Book','quantity':1,'unit_price':'10.00'}],'subtotal':'10.00','shipping':'2.00','tax':'1.00','total':'13.00','currency':'USD'}

    def test_purchase_confirmation_exact_single_use_and_never_claims_paid(self):
        self.service.policy('service.commerce.checkout',{'enabled':True,'purchase':True})
        review=self.service.prepare_purchase(self.order())
        changed=self.order();changed['total']='14.00'
        with self.assertRaises(ConnectorError):self.service.confirm_purchase(review['id'],True,changed)
        result=self.service.confirm_purchase(review['id'],True,self.order())
        self.assertEqual('checkout_ready',result['status']);self.assertFalse(result['purchased'])
        with self.assertRaises(ConnectorError):self.service.confirm_purchase(review['id'],True,self.order())

    def test_purchase_rechecks_permission_and_rejects_inconsistent_total(self):
        self.service.policy('service.commerce.checkout',{'enabled':True,'purchase':True})
        bad=self.order();bad['subtotal']='9.00'
        with self.assertRaises(ConnectorError):self.service.prepare_purchase(bad)
        review=self.service.prepare_purchase(self.order())
        self.service.policy('service.commerce.checkout',{'enabled':False})
        with self.assertRaises(ConnectorError):self.service.confirm_purchase(review['id'],True,self.order())

    def test_service_policies_do_not_rewrite_existing_settings(self):
        with self.db.connect() as conn:
            baseline=[tuple(r) for r in conn.execute('SELECT * FROM application_settings')]
        self.service.set_category('mail',True);self.service.policy(self.id,{'enabled':True})
        with self.db.connect() as conn:
            self.assertEqual(baseline,[tuple(r) for r in conn.execute('SELECT * FROM application_settings')])

    def test_revocation_between_provider_steps_prevents_next_network_call(self):
        import threading
        entered=threading.Event();resume=threading.Event();allowed=[]
        def provider(*args):
            entered.set();resume.wait(2)
            allowed.append(connector_network_allowed(self.settings))
            return {}
        self.google.execute.side_effect=provider
        self.service.set_category('mail',True);self.service.policy(self.id,{'enabled':True})
        worker=threading.Thread(target=lambda:self.service.execute(self.id,'list',{}))
        worker.start();self.assertTrue(entered.wait(1))
        try:self.service.policy(self.id,{'enabled':False})
        finally:resume.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual([False],allowed)

    def test_cart_preparation_has_estimated_review_and_unknown_final_charges(self):
        self.service.shopify=Mock()
        self.service.shopify.status.return_value={'state':'connected'}
        self.service.shopify.execute.return_value={'merchant':'shop.example','checkout_url':'https://shop.example/checkout','total':'10.00','currency':'USD','items':[{'quantity':1,'merchandise':{'title':'Default','product':{'title':'Book'},'price':{'amount':'10.00'}}}]}
        self.service.policy('service.commerce.checkout',{'enabled':True,'write':True,'purchase':True})
        cart=self.service.execute('service.commerce.checkout','cart',{'lines':[]})
        review=cart['review'];self.assertTrue(review['order']['estimated'])
        self.assertIsNone(review['order']['shipping']);self.assertIsNone(review['order']['tax'])
        self.assertEqual('checkout_ready',self.service.confirm_purchase(review['id'],True,review['order'])['status'])
        self.assertNotIn('checkout_url',cart)
        self.service.policy('service.commerce.checkout',{'purchase':False})
        self.service.shopify.execute.return_value={'checkout_url':'https://shop.example/checkout','items':[]}
        with self.assertRaises(ConnectorError):self.service.execute('service.commerce.checkout','cart',{'lines':[]})

    def test_apple_mail_helper_receives_utf8_json_bytes(self):
        connector = AppleMailConnector()
        result = SimpleNamespace(stdout=b'{"available":true,"authorized":false}', returncode=0)
        with patch.object(connector, 'helper', return_value=Path('/fixture/mail-helper')), \
             patch('olcr_api.apple_mail.subprocess.run', return_value=result) as run:
            self.assertEqual('permission_required', connector.status()['state'])
        self.assertIsInstance(run.call_args.kwargs['input'], bytes)
