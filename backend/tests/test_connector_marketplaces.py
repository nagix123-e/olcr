"""Consumer marketplace handoffs never impersonate an account connection."""
import json
import unittest
from unittest.mock import patch
from backend.tests import test_service_connectors as connector_fixtures
from olcr_api.service_connectors import ConnectorError, ServiceConnectors


class MarketplaceTests(unittest.TestCase):
    setUp=connector_fixtures.ConnectorTests.setUp
    def test_major_marketplaces_are_handoffs_not_connected_accounts(self):
        rows={row['id']:row for row in self.service.inventory()}
        for name in ('amazon','rakuten','yahoo','aupay','qoo10','mercari'):
            row=rows['service.commerce.'+name]
            self.assertEqual('shopping',row['category'])
            self.assertEqual('requires_user_sign_in',row['state'])
            self.assertEqual('browser_handoff',row['connection_mode'])
            self.assertFalse(row['account_connected'])
            self.assertNotIn('checkout',row['actions'])

    def enable(self,cid):
        self.service.set_category('shopping',True)
        self.service.policy(cid,{'enabled':True})

    def test_search_is_fixed_official_link_and_cannot_take_arbitrary_urls_or_purchase(self):
        cid='service.commerce.amazon';self.enable(cid)
        value=self.service.execute(cid,'search',{'query':'本 & USB'})
        self.assertEqual('https://www.amazon.co.jp/s?k=%E6%9C%AC+%26+USB',value['url'])
        self.assertFalse(value['purchased']);self.assertFalse(value['account_connected'])
        for action,args in [('search',{'query':'x','url':'https://evil.invalid'}),('checkout',{}),('cart',{})]:
            with self.assertRaises(ConnectorError):self.service.execute(cid,action,args)
        with self.assertRaises(ConnectorError):self.service.credential(cid,json.dumps({'password':'x'}))

    def test_handoff_rechecks_permissions_and_disconnect_survives_restart(self):
        cid='service.commerce.rakuten';self.enable(cid)
        self.assertEqual('browser_handoff',self.service.execute(cid,'account',{})['type'])
        self.service.set_category('shopping',False)
        with self.assertRaises(ConnectorError):self.service.execute(cid,'account',{})
        self.service.set_category('shopping',True);self.service.disconnect(cid)
        self.service.policy(cid,{'enabled':True})
        restarted=ServiceConnectors(self.db,lambda:self.settings,self.google)
        with self.assertRaises(ConnectorError):restarted.execute(cid,'search',{'query':'本'})
        restarted.connect(cid)
        self.assertFalse(next(row for row in restarted.inventory() if row['id']==cid)['account_connected'])
        self.settings.permission_level='automatic';self.settings.external_access_enabled=False
        with self.assertRaises(ConnectorError):restarted.execute(cid,'account',{})

    def test_product_handoff_rejects_checkout_paths_and_lookalike_hosts(self):
        cid='service.commerce.amazon';self.enable(cid)
        self.assertEqual('https://www.amazon.co.jp/dp/B000123456',self.service.execute(cid,'product',{'url':'https://www.amazon.co.jp/dp/B000123456'})['url'])
        for url in ['https://www.amazon.co.jp.evil.invalid/dp/B000123456','https://www.amazon.co.jp/checkout','https://www.amazon.co.jp/dp/B000123456?redirect=https://evil.invalid','https://user@www.amazon.co.jp/dp/B000123456','http://www.amazon.co.jp/dp/B000123456']:
            with self.assertRaises(ConnectorError):self.service.execute(cid,'product',{'url':url})

    def test_yahoo_developer_search_is_not_consumer_account_connection(self):
        cid='service.commerce.yahoo';self.enable(cid)
        self.service.credential(cid,json.dumps({'application_id':'fixture-only-id'}));self.service.policy(cid,{'enabled':True})
        row=next(row for row in self.service.inventory() if row['id']==cid)
        self.assertEqual('developer_api_ready',row['state']);self.assertFalse(row['account_connected'])
        from olcr_api.connector_marketplaces import MarketplaceConnectors
        payload={'hits':[{'name':'Book','price':1000,'url':'https://store.shopping.yahoo.co.jp/books/one.html'},{'name':'Bad','price':1,'url':'https://evil.invalid/'}]}
        with patch.object(MarketplaceConnectors,'_request',return_value=payload) as request:
            value=self.service.execute(cid,'search',{'query':'本','limit':2})
        self.assertEqual('developer_api',value['mode']);self.assertEqual(1,len(value['products']))
        self.assertEqual('1000',value['products'][0]['price']);self.assertFalse(value['purchased'])
        self.assertNotIn('fixture-only-id',json.dumps(value))
        self.service.disconnect(cid)
        self.service.policy(cid,{'enabled':True})
        with self.assertRaises(ConnectorError):self.service.execute(cid,'search',{'query':'本'})

    def test_api_network_permission_is_rechecked_before_request(self):
        cid='service.commerce.yahoo';self.enable(cid)
        self.service.credential(cid,json.dumps({'application_id':'fixture-only-id'}));self.service.policy(cid,{'enabled':True})
        self.service.marketplaces._network_allowed=lambda:False
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(ConnectorError,'EXTERNAL_ACCESS_REQUIRED'):self.service.execute(cid,'search',{'query':'本'})
            opener.assert_not_called()


class MarketplaceIntentTests(unittest.TestCase):
    def test_explicit_product_and_account_commands_and_named_search(self):
        from olcr_api.connector_chat import intent
        self.assertEqual('account',intent('/service service.commerce.amazon account {}')['action'])
        self.assertEqual('product',intent('/service service.commerce.mercari product {"url":"https://jp.mercari.com/item/m123"}')['action'])
        self.assertEqual({'id':'service.commerce.rakuten','action':'search','arguments':{'query':'充電器'}},intent('楽天市場で「充電器」を検索して'))
        self.assertIsNone(intent('Amazon APIを実装する'))

class MarketplaceBoundaryTests(unittest.TestCase):
    setUp=connector_fixtures.ConnectorTests.setUp
    enable=MarketplaceTests.enable
    def test_marketplaces_cannot_inherit_shopping_mutation_scopes(self):
        cid='service.commerce.amazon';self.enable(cid)
        for scope in ('write','purchase'):
            with self.assertRaises(ConnectorError):self.service.authorize(cid,scope)

    def test_invalid_product_types_and_control_characters_are_controlled(self):
        cid='service.commerce.amazon';self.enable(cid)
        for value in (None,[],{},'https://www.amazon.co.jp/dp/B000123456\n'):
            with self.assertRaises(ConnectorError):self.service.execute(cid,'product',{'url':value})

if __name__=='__main__':unittest.main()
