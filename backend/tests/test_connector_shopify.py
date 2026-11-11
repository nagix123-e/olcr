import json
import unittest
from unittest.mock import patch
from olcr_api.connector_shopify import ShopifyConnector


class ShopifyConnectorTests(unittest.TestCase):
    def setUp(self):
        self.client=ShopifyConnector()

    def connect(self):
        self.client.set_bundle(json.dumps({'shop':'fixture.myshopify.com','storefront_token':'fixture-only-token'}))

    def test_requires_fixed_shopify_host_and_no_credentials_in_status(self):
        for host in ['localhost','127.0.0.1','fixture.myshopify.com.evil.invalid','https://fixture.myshopify.com','a.b.myshopify.com']:
            with self.assertRaises(ValueError):self.client.set_bundle(json.dumps({'shop':host,'storefront_token':'fixture-only-token'}))
        self.connect()
        self.assertEqual('connected',self.client.status()['state'])
        self.assertNotIn('fixture-only-token',json.dumps(self.client.status()))

    def test_search_uses_fixed_query_with_variables_and_bounded_results(self):
        self.connect()
        products=[{'id':'p1','title':'Tee','variants':{'nodes':[{'id':'gid://shopify/ProductVariant/1','price':{'amount':'12','currencyCode':'USD'}}]}}]
        with patch.object(self.client,'_graphql',return_value={'products':{'nodes':products}}) as call:
            result=self.client.execute('search',{'query':'tee','limit':5})
        self.assertEqual(products,result['products'])
        self.assertEqual({'query':'tee','first':5},call.call_args.args[1])
        with self.assertRaises(ValueError):self.client.execute('search',{'limit':1000})

    def test_cart_preparation_returns_estimated_total_and_never_marks_paid(self):
        self.connect()
        cart={'id':'cart1','checkoutUrl':'https://fixture.myshopify.com/checkouts/xyz','cost':{'totalAmount':{'amount':'24.00','currencyCode':'USD'}},'lines':{'nodes':[{'quantity':2,'merchandise':{'id':'gid://shopify/ProductVariant/1','title':'Red','product':{'title':'Tee'},'price':{'amount':'12','currencyCode':'USD'}}}]}}
        with patch.object(self.client,'_graphql',return_value={'cartCreate':{'cart':cart,'userErrors':[]}}) as call:
            result=self.client.execute('cart',{'lines':[{'variant_id':'gid://shopify/ProductVariant/1','quantity':2}]})
        self.assertEqual('24.00',result['total'])
        self.assertTrue(result['estimated'])
        self.assertFalse(result['purchased'])
        self.assertEqual('gid://shopify/ProductVariant/1',call.call_args.args[1]['input']['lines'][0]['merchandiseId'])

    def test_cart_rejects_mutation_injection_and_unknown_fields(self):
        self.connect()
        with patch.object(self.client,'_graphql') as call:
            for args in [{'lines':[{'variant_id':'mutation {bad}','quantity':1}]},{'lines':[]},{'lines':[{'variant_id':'gid://shopify/ProductVariant/1','quantity':-1}]},{'lines':[{'variant_id':'gid://shopify/ProductVariant/1','quantity':1}],'card_number':'secret'}]:
                with self.assertRaises(ValueError):self.client.execute('cart',args)
        call.assert_not_called()

    def test_cart_provider_errors_never_return_checkout_or_paid(self):
        self.connect()
        with patch.object(self.client,'_graphql',return_value={'cartCreate':{'cart':None,'userErrors':[{'message':'fixture secret response'}]}}):
            with self.assertRaisesRegex(ValueError,'CONNECTOR_CART_REJECTED'):
                self.client.execute('cart',{'lines':[{'variant_id':'gid://shopify/ProductVariant/1','quantity':1}]})
