"""Personal-service policies. No Brain, knowledge-tool, or coding/MCP registration."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import re
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from contextvars import ContextVar
from .permissions import capability_enabled
from .connector_marketplaces import MarketplaceConnectors, PROVIDERS

_dispatch_scope=ContextVar('service_connector_dispatch',default=None)

def connector_network_allowed(settings):
    if not capability_enabled(settings,'external_access_enabled'):return False
    current=_dispatch_scope.get()
    if current:
        try:current[0].authorize(current[1],current[2])
        except ConnectorError:return False
    return True

CATALOG={
 'service.google.gmail':('Gmail',('list','get','send')),
 'service.apple.mail':('Apple Mail',('list','get','send')),
 'service.google.calendar':('Google Calendar',('list','get')),
 'service.google.drive':('Google Drive',('list','get')),
 'service.commerce.checkout':('Merchant checkout / Shopify',('search','cart','review','checkout')),
}
CATALOG.update({cid:(provider['label'],provider.get('actions',('search','account','product'))) for cid,provider in PROVIDERS.items()})
CATEGORIES={'mail':('Mail',('read','send')), 'calendar':('Calendar',('read','write')), 'files':('Files',('read',)), 'shopping':('Shopping',('read','write','purchase'))}
CONNECTOR_CATEGORY={'service.google.gmail':'mail','service.apple.mail':'mail','service.google.calendar':'calendar','service.google.drive':'files','service.commerce.checkout':'shopping'}

CONNECTOR_CATEGORY.update({cid:'shopping' for cid in PROVIDERS})

class ConnectorError(ValueError):pass


def purchase_order(value):
    """Canonical exact-value review; money is decimal strings, never floats."""
    if not isinstance(value,dict) or set(value)-{'estimated'}!={'merchant','url','items','subtotal','shipping','tax','total','currency'} or ('estimated' in value and type(value['estimated']) is not bool):
        raise ConnectorError('PURCHASE_FIELDS_INVALID')
    url=value['url'];parts=urlsplit(url) if isinstance(url,str) else None
    if not parts or parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.fragment or parts.port not in (None,443):
        raise ConnectorError('PURCHASE_URL_INVALID')
    host=parts.hostname.lower()
    if host!=value['merchant'] or '.' not in host or host.endswith(('.local','.localhost','.internal')):
        raise ConnectorError('PURCHASE_MERCHANT_INVALID')
    try:ipaddress.ip_address(host)
    except ValueError:pass
    else:raise ConnectorError('PURCHASE_MERCHANT_INVALID')
    if not isinstance(value['currency'],str) or not re.fullmatch('[A-Z]{3}',value['currency']):raise ConnectorError('PURCHASE_CURRENCY_INVALID')
    def money(v):
        if not isinstance(v,str) or not re.fullmatch(r'\d{1,12}(?:\.\d{1,4})?',v):raise ConnectorError('PURCHASE_MONEY_INVALID')
        return Decimal(v)
    items=value['items']
    if not isinstance(items,list) or not 1<=len(items)<=100:raise ConnectorError('PURCHASE_ITEMS_INVALID')
    subtotal=Decimal(0)
    for item in items:
        if not isinstance(item,dict) or set(item)!={'name','quantity','unit_price'}:raise ConnectorError('PURCHASE_ITEMS_INVALID')
        if not isinstance(item['name'],str) or not 1<=len(item['name'])<=200 or type(item['quantity']) is not int or not 1<=item['quantity']<=1000:raise ConnectorError('PURCHASE_ITEMS_INVALID')
        subtotal+=money(item['unit_price'])*item['quantity']
    if value.get('estimated'):
        money(value['total'])
        if value['shipping'] is not None or value['tax'] is not None:raise ConnectorError('PURCHASE_ESTIMATE_INVALID')
        if subtotal!=money(value['subtotal']):raise ConnectorError('PURCHASE_TOTAL_INVALID')
    elif subtotal!=money(value['subtotal']) or subtotal+money(value['shipping'])+money(value['tax'])!=money(value['total']):
        raise ConnectorError('PURCHASE_TOTAL_INVALID')
    return json.loads(json.dumps(value,ensure_ascii=False,sort_keys=True))


class ServiceConnectors:
    def __init__(self,db,settings,google,shopify=None,apple_mail=None):
        self.db,self.settings,self.google=db,settings,google
        if apple_mail is None:
            from .apple_mail import AppleMailConnector
            apple_mail=AppleMailConnector(network_allowed=lambda:connector_network_allowed(self.settings()))
        self.apple_mail=apple_mail
        if shopify is None:
            from .connector_shopify import ShopifyConnector
            shopify=ShopifyConnector(network_allowed=lambda:connector_network_allowed(self.settings()))
        self.shopify=shopify
        self.marketplaces=MarketplaceConnectors(network_allowed=lambda:connector_network_allowed(self.settings()))
        self.lock=threading.RLock()
        with db.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS service_connector_policies (id TEXT PRIMARY KEY, policy_json TEXT NOT NULL, disconnected INTEGER NOT NULL DEFAULT 0)')
            con.execute('CREATE TABLE IF NOT EXISTS service_purchase_reviews (id TEXT PRIMARY KEY, order_json TEXT NOT NULL, fingerprint TEXT NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS service_connector_categories (id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, grant_version INTEGER NOT NULL DEFAULT 1)')
            category_columns={row[1] for row in con.execute('PRAGMA table_info(service_connector_categories)')}
            if 'grant_version' not in category_columns:
                con.execute('ALTER TABLE service_connector_categories ADD COLUMN grant_version INTEGER NOT NULL DEFAULT 1')
            # Only a complete legacy grant can migrate to an enabled category.
            # Partial grants require explicit category consent, never expansion.
            for category,(_,scopes) in CATEGORIES.items():
                policies=[]
                for cid in CONNECTOR_CATEGORY:
                    if CONNECTOR_CATEGORY[cid]!=category:continue
                    # Apple Mail is new to the category; its per-service switch
                    # remains OFF independently of existing Gmail consent.
                    if cid=='service.apple.mail' or cid in PROVIDERS:continue
                    row=con.execute('SELECT policy_json FROM service_connector_policies WHERE id=?',(cid,)).fetchone()
                    policies.append(json.loads(row[0]) if row else {})
                enabled=bool(policies) and all(all(policy.get(scope,False) for scope in scopes) for policy in policies)
                grant_version=2 if category=='mail' else 1
                row=con.execute('SELECT enabled,grant_version FROM service_connector_categories WHERE id=?',(category,)).fetchone()
                if row is None:
                    con.execute('INSERT INTO service_connector_categories (id,enabled,grant_version) VALUES (?,?,?)',(category,int(enabled),grant_version))
                elif category=='mail' and int(row[1])<grant_version:
                    # Reading consent from older builds must not silently authorize external mail sending.
                    con.execute('UPDATE service_connector_categories SET enabled=0,grant_version=? WHERE id=?',(grant_version,category))

    def categories(self):
        with self.db.connect() as con:enabled=dict(con.execute('SELECT id,enabled FROM service_connector_categories'))
        return [{'id':key,'label':value[0],'enabled':bool(enabled.get(key,False))} for key,value in CATEGORIES.items()]

    def set_category(self,category,enabled):
        if category not in CATEGORIES or type(enabled) is not bool:raise ConnectorError('CONNECTOR_CATEGORY_INVALID')
        with self.lock:
            with self.db.connect() as con:con.execute('INSERT INTO service_connector_categories (id,enabled,grant_version) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled',(category,int(enabled),2 if category=='mail' else 1))
        return {'connectors':self.inventory(),'categories':self.categories()}

    def _category_enabled(self,cid):
        if cid not in CONNECTOR_CATEGORY:raise ConnectorError('CONNECTOR_UNKNOWN')
        with self.db.connect() as con:row=con.execute('SELECT enabled FROM service_connector_categories WHERE id=?',(CONNECTOR_CATEGORY[cid],)).fetchone()
        return bool(row and row[0])

    def _row(self,cid):
        if cid not in CATALOG:raise ConnectorError('CONNECTOR_UNKNOWN')
        with self.db.connect() as con:row=con.execute('SELECT policy_json,disconnected FROM service_connector_policies WHERE id=?',(cid,)).fetchone()
        return (json.loads(row[0]),bool(row[1])) if row else ({'enabled':False,'read':False,'write':False,'purchase':False},False)

    def policy(self,cid,changes):
        if not isinstance(changes,dict) or not changes or set(changes)-{'enabled','read','write','purchase'} or any(type(v) is not bool for v in changes.values()):raise ConnectorError('CONNECTOR_POLICY_INVALID')
        if (changes.get('write') or changes.get('purchase')) and cid!='service.commerce.checkout':raise ConnectorError('CONNECTOR_SCOPE_UNSUPPORTED')
        legacy_scopes=[value for key,value in changes.items() if key!='enabled']
        if cid in {'service.google.gmail','service.apple.mail'} and legacy_scopes:raise ConnectorError('CONNECTOR_USE_CATEGORY_PERMISSION')
        if legacy_scopes and len(set(legacy_scopes))!=1:raise ConnectorError('CONNECTOR_USE_CATEGORY_PERMISSION')
        with self.lock:
            current,disconnected=self._row(cid);current.update(changes)
            with self.db.connect() as con:con.execute('INSERT INTO service_connector_policies VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET policy_json=excluded.policy_json',(cid,json.dumps(current),int(disconnected)))
            # Backward-compatible clients now modify the whole category grant.
            if legacy_scopes:
                with self.db.connect() as con:con.execute('UPDATE service_connector_categories SET enabled=? WHERE id=?',(int(legacy_scopes[0]),CONNECTOR_CATEGORY[cid]))
        return next(row for row in self.inventory() if row['id']==cid)

    def inventory(self):
        rows=[]
        for cid,(label,actions) in CATALOG.items():
            policy,disconnected=self._row(cid)
            if disconnected:status={'state':'disconnected'}
            elif cid in PROVIDERS:status=self.marketplaces.status(cid)
            elif cid=='service.commerce.checkout':status=self.shopify.status()
            elif cid=='service.apple.mail':status=self.apple_mail.status()
            else:status=self.google.status(cid)
            category=CONNECTOR_CATEGORY[cid];allowed=self._category_enabled(cid)
            enabled=policy['enabled'] and (cid!='service.apple.mail' or status.get('state')=='connected')
            rows.append({**({key:status.get(key,False if key=='account_connected' else 'requires_user_sign_in' if key=='account_state' else 'browser_handoff') for key in ('connection_mode','account_connected','account_state')} if cid in PROVIDERS else {}),'id':cid,'label':label,'category':category,'state':status.get('state','disconnected'),'enabled':enabled,'send_consent_required':bool(status.get('send_consent_required',False)),'write_consent_required':bool(status.get('write_consent_required',False)),'permissions':{key:allowed and key in CATEGORIES[category][1] and (cid not in PROVIDERS or key=='read') for key in ('read','send','write','purchase')},'actions':list(actions)})
        return rows

    def connect(self,cid):
        if cid!='service.apple.mail' and cid not in PROVIDERS:raise ConnectorError('CONNECTOR_ACTION_UNSUPPORTED')
        if not capability_enabled(self.settings(),'external_access_enabled'):raise ConnectorError('EXTERNAL_ACCESS_REQUIRED')
        if not self._category_enabled(cid):raise ConnectorError('CONNECTOR_PERMISSION_DENIED')
        try:
            if cid=='service.apple.mail':self.apple_mail.connect()
        except ValueError as exc:raise ConnectorError(str(exc)) from None
        with self.lock:
            policy,_=self._row(cid);policy['enabled']=True
            with self.db.connect() as con:con.execute('INSERT INTO service_connector_policies VALUES (?,?,0) ON CONFLICT(id) DO UPDATE SET policy_json=excluded.policy_json,disconnected=0',(cid,json.dumps(policy)))
        return {'connectors':self.inventory(),'categories':self.categories()}

    def authorize(self,cid,scope):
        policy,disconnected=self._row(cid)
        if not capability_enabled(self.settings(),'external_access_enabled'):raise ConnectorError('EXTERNAL_ACCESS_REQUIRED')
        if (cid in PROVIDERS and scope!='read') or disconnected or not policy['enabled'] or not self._category_enabled(cid) or scope not in CATEGORIES[CONNECTOR_CATEGORY[cid]][1]:raise ConnectorError('CONNECTOR_PERMISSION_DENIED')

    def calendar_request(self,method,calendar_id,event_id='',*,payload=None,etag=None):
        cid='service.google.calendar';scope='read' if method=='GET' else 'write'
        self.authorize(cid,scope)
        token=_dispatch_scope.set((self,cid,scope))
        try:return self.google.calendar_request(method,calendar_id,event_id,payload=payload,etag=etag)
        except ValueError as exc:raise ConnectorError(str(exc)) from None
        finally:_dispatch_scope.reset(token)

    def disconnect(self,cid):
        with self.lock:
            policy,_=self._row(cid);policy['enabled']=False
            with self.db.connect() as con:con.execute('INSERT INTO service_connector_policies VALUES (?,?,1) ON CONFLICT(id) DO UPDATE SET policy_json=excluded.policy_json,disconnected=1',(cid,json.dumps(policy)))
            if cid.startswith('service.google.'):self.google.disconnect(cid)
            elif cid=='service.commerce.checkout':self.shopify.disconnect()
            elif cid in PROVIDERS:self.marketplaces.disconnect(cid)
        return {'disconnected':True}

    def credential(self,cid,bundle):
        if cid not in CATALOG:raise ConnectorError('CONNECTOR_UNKNOWN')
        with self.lock:
            if cid in PROVIDERS:
                try:self.marketplaces.set_bundle(cid,bundle)
                except ValueError as exc:raise ConnectorError(str(exc)) from None
            elif cid=='service.commerce.checkout':self.shopify.set_bundle(bundle)
            elif cid=='service.apple.mail':raise ConnectorError('CONNECTOR_CREDENTIAL_UNSUPPORTED')
            else:self.google.set_bundle(cid,bundle)
            policy,_=self._row(cid);policy['enabled']=False
            with self.db.connect() as con:con.execute('INSERT INTO service_connector_policies VALUES (?,?,0) ON CONFLICT(id) DO UPDATE SET policy_json=excluded.policy_json,disconnected=0',(cid,json.dumps(policy)))
        return {'saved':True}

    def execute(self,cid,action,arguments):
        scope='send' if cid in {'service.google.gmail','service.apple.mail'} and action=='send' else 'write' if cid=='service.commerce.checkout' and action=='cart' else 'read'
        token=_dispatch_scope.set((self,cid,scope))
        try:
            return self._execute(cid,action,arguments)
        finally:
            _dispatch_scope.reset(token)

    def _execute(self,cid,action,arguments):
            self.authorize(cid,'send' if cid in {'service.google.gmail','service.apple.mail'} and action=='send' else 'write' if cid=='service.commerce.checkout' and action=='cart' else 'read')
            if action not in CATALOG[cid][1]:raise ConnectorError('CONNECTOR_ACTION_UNSUPPORTED')
            if cid in PROVIDERS:
                try:return self.marketplaces.execute(cid,action,arguments)
                except ValueError as exc:raise ConnectorError(str(exc)) from None
            if cid=='service.commerce.checkout':
                if action not in ('search','cart'):raise ConnectorError('CONNECTOR_ACTION_UNSUPPORTED')
                try:result=self.shopify.execute(action,arguments)
                except ValueError as exc:raise ConnectorError(str(exc)) from None
                if action=='cart':
                    if self._category_enabled(cid):
                        try:
                            items=[{'name':line['merchandise']['product']['title']+' / '+line['merchandise']['title'],'quantity':line['quantity'],'unit_price':line['merchandise']['price']['amount']} for line in result['items']]
                            subtotal=sum(Decimal(x['unit_price'])*x['quantity'] for x in items)
                            result['review']=self.prepare_purchase({'merchant':result['merchant'],'url':result['checkout_url'],'items':items,'subtotal':str(subtotal),'shipping':None,'tax':None,'total':result['total'],'currency':result['currency'],'estimated':True})
                        except (KeyError,TypeError,InvalidOperation) as exc:raise ConnectorError('CONNECTOR_INVALID_RESPONSE') from None
                    result.pop('checkout_url',None)
                return result
            if cid=='service.apple.mail':
                try:return self.apple_mail.execute(action,arguments)
                except ValueError as exc:raise ConnectorError(str(exc)) from None
            try:return self.google.execute(cid,action,arguments)
            except ValueError as exc:raise ConnectorError(str(exc)) from None

    def prepare_purchase(self,order):
        with self.lock:
            self.authorize('service.commerce.checkout','purchase')
            order=purchase_order(order);encoded=json.dumps(order,sort_keys=True,ensure_ascii=False);rid=uuid.uuid4().hex
            with self.db.connect() as con:con.execute('INSERT INTO service_purchase_reviews VALUES (?,?,?,?,?)',(rid,encoded,hashlib.sha256(encoded.encode()).hexdigest(),time.time()+300,'pending'))
        return {'id':rid,'order':order,'status':'pending','type':'service_purchase'}

    def confirm_purchase(self,rid,approve,order):
        with self.lock:
            self.authorize('service.commerce.checkout','purchase')
            if type(approve) is not bool:raise ConnectorError('PURCHASE_APPROVAL_INVALID')
            encoded=json.dumps(purchase_order(order),sort_keys=True,ensure_ascii=False)
            with self.db.connect() as con:
                row=con.execute('SELECT fingerprint,expires_at,status FROM service_purchase_reviews WHERE id=?',(rid,)).fetchone()
                if not row or row[2]!='pending' or row[1]<=time.time() or row[0]!=hashlib.sha256(encoded.encode()).hexdigest():raise ConnectorError('PURCHASE_APPROVAL_INVALID_OR_EXPIRED')
                changed=con.execute("UPDATE service_purchase_reviews SET status=? WHERE id=? AND status='pending'",('checkout_ready' if approve else 'denied',rid)).rowcount
                if changed!=1:raise ConnectorError('PURCHASE_APPROVAL_ALREADY_USED')
            return {'status':'checkout_ready' if approve else 'denied','url':order['url'] if approve else None,'purchased':False}
