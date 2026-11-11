import unittest
from unittest.mock import Mock
from backend.tests.test_service_connectors import ConnectorTests
from olcr_api.service_connectors import ConnectorError, ServiceConnectors


class CategoryPermissions(ConnectorTests):
    def test_shopping_grants_search_cart_and_review_together(self):
        cid='service.commerce.checkout'
        self.service.shopify=Mock();self.service.shopify.status.return_value={'state':'connected'}
        self.service.shopify.execute.return_value={'products':[]}
        self.service.policy(cid,{'enabled':True})
        self.service.set_category('shopping',True)
        for scope in ('read','write','purchase'):self.service.authorize(cid,scope)
        self.service.execute(cid,'search',{})
        self.service.set_category('shopping',False)
        for scope in ('read','write','purchase'):
            with self.assertRaises(ConnectorError):self.service.authorize(cid,scope)
        self.assertTrue(next(row for row in self.service.inventory() if row['id']==cid)['enabled'])

    def test_mail_permission_does_not_grant_other_categories_or_enable_connections(self):
        self.service.set_category('mail',True)
        with self.assertRaises(ConnectorError):self.service.execute(self.id,'list',{})
        self.service.policy(self.id,{'enabled':True})
        self.service.execute(self.id,'list',{})
        self.service.policy('service.google.drive',{'enabled':True})
        with self.assertRaises(ConnectorError):self.service.execute('service.google.drive','list',{})
        restarted=ServiceConnectors(self.db,lambda:self.settings,self.google)
        self.assertTrue(next(row for row in restarted.categories() if row['id']=='mail')['enabled'])
        self.assertFalse(next(row for row in restarted.categories() if row['id']=='shopping')['enabled'])

    def test_partial_old_shopping_scopes_do_not_silently_gain_new_permissions(self):
        import json
        with self.db.connect() as con:
            con.execute('DELETE FROM service_connector_categories')
            con.execute('INSERT INTO service_connector_policies VALUES (?,?,0)',('service.commerce.checkout',json.dumps({'enabled':True,'read':True,'write':False,'purchase':False})))
        restarted=ServiceConnectors(self.db,lambda:self.settings,self.google)
        with self.assertRaises(ConnectorError):restarted.authorize('service.commerce.checkout','purchase')
        with self.assertRaises(ConnectorError):restarted.authorize('service.commerce.checkout','read')
        self.assertFalse(next(row for row in restarted.categories() if row['id']=='shopping')['enabled'])
