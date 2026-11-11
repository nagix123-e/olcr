"""Merchant-configured Shopify Storefront search and cart preparation, not payment.

Contracts: https://shopify.dev/docs/api/storefront/2026-10/queries/products
https://shopify.dev/docs/api/storefront/2026-10/mutations/cartCreate
Shopify's cart costs are estimates; checkout determines final tax/shipping/total.
"""
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

HANDLE='OLCR_CONNECTOR_SHOPIFY'
SEARCH='''query Products($first:Int!,$query:String!){products(first:$first,query:$query){nodes{id title handle onlineStoreUrl variants(first:10){nodes{id title availableForSale price{amount currencyCode}}}}}}'''
CART='''mutation CreateCart($input:CartInput!){cartCreate(input:$input){cart{id checkoutUrl cost{totalAmount{amount currencyCode}} lines(first:50){nodes{quantity merchandise{... on ProductVariant{id title product{title} price{amount currencyCode}}}}}} userErrors{field code message}}}'''


class ShopifyConnector:
    def __init__(self,network_allowed=lambda:True):
        self._lock=threading.RLock();self._bundle=None;self._disconnected=False;self._error=False
        self._network_allowed=network_allowed

    def set_bundle(self,bundle):
        try:
            value=json.loads(bundle)
            if not isinstance(value,dict) or set(value)!={'shop','storefront_token'}:raise ValueError()
            if not isinstance(value['shop'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,61}\.myshopify\.com',value['shop']):raise ValueError()
            token=value['storefront_token']
            if not isinstance(token,str) or not 1<=len(token)<=4096 or any(c.isspace() for c in token):raise ValueError()
        except (ValueError,TypeError,KeyError):raise ValueError('CONNECTOR_CREDENTIAL_INVALID') from None
        with self._lock:self._bundle=value;self._disconnected=False;self._error=False

    def _load(self):
        if self._bundle is None and not self._disconnected and os.environ.get(HANDLE):
            try:self.set_bundle(os.environ[HANDLE])
            except ValueError:self._error=True
        return self._bundle

    def status(self):
        with self._lock:
            bundle=self._load()
            return {'state':'reconnect_required' if self._error else 'connected' if bundle else 'disconnected','credential_handle':HANDLE,**({'shop':bundle['shop']} if bundle else {})}

    def disconnect(self):
        with self._lock:self._bundle=None;self._disconnected=True;self._error=False;os.environ.pop(HANDLE,None)

    def _graphql(self,query,variables):
        bundle=self._load()
        if not bundle or self._error:raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
        if not self._network_allowed():raise ValueError('EXTERNAL_ACCESS_REQUIRED')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        request=urllib.request.Request('https://'+bundle['shop']+'/api/2026-10/graphql.json',data=json.dumps({'query':query,'variables':variables}).encode(),headers={'Content-Type':'application/json','Accept':'application/json','X-Shopify-Storefront-Access-Token':bundle['storefront_token']})
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
                raw=response.read(2*1024*1024+1)
                if len(raw)>2*1024*1024:raise ValueError('CONNECTOR_RESPONSE_TOO_LARGE')
                value=json.loads(raw)
                if not isinstance(value,dict) or value.get('errors') or not isinstance(value.get('data'),dict):raise ValueError('CONNECTOR_PROVIDER_REJECTED')
                return value['data']
        except urllib.error.HTTPError as exc:
            if exc.code in {401,403}:self._error=True;raise ValueError('CONNECTOR_RECONNECT_REQUIRED') from None
            raise ValueError('CONNECTOR_PROVIDER_UNAVAILABLE') from None
        except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError):raise ValueError('CONNECTOR_PROVIDER_UNAVAILABLE') from None

    def execute(self,action,arguments):
        if not isinstance(arguments,dict):raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        with self._lock:
            if action=='search':
                if set(arguments)-{'query','limit'}:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
                query=arguments.get('query','');limit=arguments.get('limit',10)
                if not isinstance(query,str) or len(query)>1000 or type(limit) is not int or not 1<=limit<=25:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
                result=self._graphql(SEARCH,{'query':query,'first':limit})
                products=result.get('products',{}).get('nodes')
                if not isinstance(products,list):raise ValueError('CONNECTOR_INVALID_RESPONSE')
                return {'products':products,'purchased':False}
            if action=='cart':
                lines=arguments.get('lines')
                if set(arguments)!={'lines'} or not isinstance(lines,list) or not 1<=len(lines)<=50:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
                for line in lines:
                    if not isinstance(line,dict) or set(line)!={'variant_id','quantity'} or not isinstance(line['variant_id'],str) or not re.fullmatch(r'gid://shopify/ProductVariant/[0-9]{1,30}',line['variant_id']) or type(line['quantity']) is not int or not 1<=line['quantity']<=100:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
                value=self._graphql(CART,{'input':{'lines':[{'merchandiseId':line['variant_id'],'quantity':line['quantity']} for line in lines]}}).get('cartCreate')
                if not isinstance(value,dict) or value.get('userErrors') or not isinstance(value.get('cart'),dict):raise ValueError('CONNECTOR_CART_REJECTED')
                cart=value['cart']
                try:
                    checkout=urllib.parse.urlsplit(cart['checkoutUrl'])
                    if checkout.scheme!='https' or not checkout.hostname or checkout.username or checkout.password or checkout.port not in {None,443}:raise ValueError()
                    total=cart['cost']['totalAmount'];items=cart['lines']['nodes']
                    if not isinstance(total['amount'],str) or not re.fullmatch(r'\d{1,12}(?:\.\d{1,4})?',total['amount']) or not re.fullmatch('[A-Z]{3}',total['currencyCode']) or not isinstance(items,list):raise ValueError()
                    return {'id':cart['id'],'checkout_url':cart['checkoutUrl'],'merchant':checkout.hostname,'total':total['amount'],'currency':total['currencyCode'],'estimated':True,'items':items,'purchased':False}
                except (KeyError,TypeError,ValueError):raise ValueError('CONNECTOR_INVALID_RESPONSE') from None
            raise ValueError('CONNECTOR_ACTION_UNSUPPORTED')
