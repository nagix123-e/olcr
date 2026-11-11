"""Allowlisted consumer-site handoffs and Yahoo's read-only developer search API.

A website handoff does not authenticate OLCR or observe the user's browser session.
No cookies, consumer passwords, cart mutations, or payment endpoints are used.
"""
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

YAHOO_HANDLE='OLCR_CONNECTOR_YAHOO_SHOPPING'
PROVIDERS={
 'service.commerce.amazon':{'label':'Amazon.co.jp','home':'https://www.amazon.co.jp/','search':'https://www.amazon.co.jp/s?','query':'k','products':[(r'www\.amazon\.co\.jp',r'/(?:[^/]+/)?(?:dp|gp/product)/[A-Z0-9]{10}/?')]},
 'service.commerce.rakuten':{'label':'楽天市場 / Rakuten','home':'https://www.rakuten.co.jp/','search':'https://search.rakuten.co.jp/search/mall/','query':None,'products':[(r'item\.rakuten\.co\.jp',r'/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/?')]},
 'service.commerce.yahoo':{'label':'Yahoo!ショッピング','home':'https://shopping.yahoo.co.jp/','search':'https://shopping.yahoo.co.jp/search?','query':'p','products':[(r'store\.shopping\.yahoo\.co\.jp',r'/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.html')]},
 'service.commerce.aupay':{'label':'au PAY マーケット','home':'https://wowma.jp/','search':'https://wowma.jp/itemlist?','query':'keyword','products':[(r'wowma\.jp',r'/item/[0-9]+/?')]},
 'service.commerce.qoo10':{'label':'Qoo10','home':'https://www.qoo10.jp/','search':'https://www.qoo10.jp/s/','query':None,'products':[(r'www\.qoo10\.jp',r'/g/[0-9]+/?')]},
 'service.commerce.mercari':{'label':'メルカリ / Mercari','home':'https://jp.mercari.com/','search':'https://jp.mercari.com/search?','query':'keyword','products':[(r'jp\.mercari\.com',r'/item/m[0-9]+/?')]},
}


# Account-browser connections for additional major services. Search/product APIs
# are intentionally not advertised without a verified service contract.
PROVIDERS.update({
 'service.commerce.yodobashi':{'label':'ヨドバシ.com','home':'https://www.yodobashi.com/','actions':('account',),'products':[]},
 'service.commerce.biccamera':{'label':'ビックカメラ.com','home':'https://www.biccamera.com/','actions':('account',),'products':[]},
 'service.commerce.zozo':{'label':'ZOZOTOWN','home':'https://zozo.jp/','actions':('account',),'products':[]},
 'service.commerce.ebay':{'label':'eBay','home':'https://www.ebay.com/','actions':('account',),'products':[]},
 'service.commerce.rakuma':{'label':'楽天ラクマ','home':'https://fril.jp/','actions':('account',),'products':[]},
 'service.commerce.yahooflea':{'label':'Yahoo!フリマ','home':'https://paypayfleamarket.yahoo.co.jp/','actions':('account',),'products':[]},
})


def product_url(cid,value):
    """Only canonical product paths, without redirect/tracking/query parameters."""
    try:
        if not isinstance(value,str) or any(ord(char)<33 for char in value):raise ValueError()
        parts=urllib.parse.urlsplit(value)
        if not isinstance(value,str) or len(value)>2048 or parts.scheme!='https' or parts.username or parts.password or parts.port not in (None,443) or parts.query or parts.fragment or '\\' in value:raise ValueError()
        if not any(re.fullmatch(host,parts.hostname or '') and re.fullmatch(path,parts.path) for host,path in PROVIDERS[cid]['products']):raise ValueError()
    except (ValueError,TypeError,KeyError):raise ValueError('CONNECTOR_PRODUCT_URL_INVALID') from None
    return 'https://'+parts.hostname+parts.path


class MarketplaceConnectors:
    def __init__(self,network_allowed=lambda:True):
        self._network_allowed=network_allowed;self._lock=threading.RLock();self._bundle=None;self._disconnected=False;self._error=False

    def set_bundle(self,cid,bundle):
        if cid!='service.commerce.yahoo':raise ValueError('CONNECTOR_CREDENTIAL_UNSUPPORTED')
        try:
            value=json.loads(bundle)
            if not isinstance(value,dict) or set(value)!={'application_id'} or not isinstance(value['application_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,512}',value['application_id']):raise ValueError()
        except (ValueError,TypeError):raise ValueError('CONNECTOR_CREDENTIAL_INVALID') from None
        with self._lock:self._bundle=value;self._disconnected=False;self._error=False

    def _load(self):
        if self._bundle is None and not self._disconnected and os.environ.get(YAHOO_HANDLE):
            try:self.set_bundle('service.commerce.yahoo',os.environ[YAHOO_HANDLE])
            except ValueError:self._error=True
        return self._bundle

    def status(self,cid):
        with self._lock:
            bundle=self._load() if cid=='service.commerce.yahoo' else None
            return {'state':'reconnect_required' if cid=='service.commerce.yahoo' and self._error else 'developer_api_ready' if bundle else 'requires_user_sign_in','connection_mode':'developer_api' if bundle else 'browser_handoff','account_connected':False,'account_state':'requires_user_sign_in'}

    def disconnect(self,cid):
        if cid=='service.commerce.yahoo':
            with self._lock:self._bundle=None;self._disconnected=True;self._error=False;os.environ.pop(YAHOO_HANDLE,None)

    def _request(self,query,limit,bundle):
        # Recheck dispatch policy immediately before transmitting a credential.
        if not self._network_allowed():raise ValueError('EXTERNAL_ACCESS_REQUIRED')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        params=urllib.parse.urlencode({'appid':bundle['application_id'],'query':query,'results':limit})
        request=urllib.request.Request('https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch?'+params,headers={'Accept':'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
                raw=response.read(2*1024*1024+1)
                if len(raw)>2*1024*1024:raise ValueError('CONNECTOR_RESPONSE_TOO_LARGE')
                value=json.loads(raw)
                if not isinstance(value,dict) or value.get('Error') or value.get('error'):raise ValueError('CONNECTOR_PROVIDER_REJECTED')
                return value
        except urllib.error.HTTPError as exc:
            if exc.code in (401,403):self._error=True;raise ValueError('CONNECTOR_RECONNECT_REQUIRED') from None
            raise ValueError('CONNECTOR_PROVIDER_UNAVAILABLE') from None
        except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError):raise ValueError('CONNECTOR_PROVIDER_UNAVAILABLE') from None

    def execute(self,cid,action,arguments):
        if cid not in PROVIDERS:raise ValueError('CONNECTOR_UNKNOWN')
        if not isinstance(arguments,dict):raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        provider=PROVIDERS[cid]
        if action not in provider.get('actions',('search','account','product')):raise ValueError('CONNECTOR_ACTION_UNSUPPORTED')
        if action=='account':
            if arguments:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
            url=provider['home']
        elif action=='product':
            if set(arguments)!={'url'}:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
            url=product_url(cid,arguments['url'])
        elif action=='search':
            query=arguments.get('query','');limit=arguments.get('limit',10)
            if set(arguments)-{'query','limit'} or not isinstance(query,str) or not 1<=len(query.strip())<=200 or any(ord(c)<32 for c in query) or type(limit) is not int or not 1<=limit<=25:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
            query=query.strip()
            with self._lock:
                bundle=self._load() if cid=='service.commerce.yahoo' else None
                if bundle:
                    if self._error:raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
                    value=self._request(query,limit,bundle);hits=value.get('hits')
                    if not isinstance(hits,list):raise ValueError('CONNECTOR_INVALID_RESPONSE')
                    products=[]
                    for hit in hits[:limit]:
                        if not isinstance(hit,dict):continue
                        try:url=product_url(cid,hit.get('url'))
                        except ValueError:continue
                        name,price=hit.get('name'),hit.get('price')
                        if not isinstance(name,str) or not 1<=len(name)<=1000 or type(price) is not int or price<0:continue
                        products.append({'name':name,'price':str(price),'currency':'JPY','url':url})
                    return {'type':'marketplace_search','mode':'developer_api','provider':provider['label'],'products':products,'account_connected':False,'purchased':False,'prices_estimated':True}
            url=provider['search']+(urllib.parse.urlencode({provider['query']:query}) if provider['query'] else urllib.parse.quote(query,safe='')+'/')
        else:raise ValueError('CONNECTOR_ACTION_UNSUPPORTED')
        return {'type':'browser_handoff','mode':'browser_handoff','provider':provider['label'],'url':url,'account_state':'requires_user_sign_in','account_connected':False,'purchased':False}
