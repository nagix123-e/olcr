"""Google service connectors. OAuth secrets live only in memory/Keychain bundles.

Provider contracts:
https://developers.google.com/identity/protocols/oauth2/native-app
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages
https://developers.google.com/workspace/calendar/api/v3/reference/events/list
https://developers.google.com/workspace/drive/api/reference/rest/v3/files
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

CONNECTORS = {
    'service.google.gmail': {'handle': 'OLCR_CONNECTOR_GOOGLE_GMAIL', 'scope': 'https://www.googleapis.com/auth/gmail.readonly',
                             'additional_scopes': ('https://www.googleapis.com/auth/gmail.send',)},
    'service.google.calendar': {'handle': 'OLCR_CONNECTOR_GOOGLE_CALENDAR', 'scope': 'https://www.googleapis.com/auth/calendar.events.readonly',
                                'additional_scopes': ('https://www.googleapis.com/auth/calendar.events.owned',)},
    'service.google.drive': {'handle': 'OLCR_CONNECTOR_GOOGLE_DRIVE', 'scope': 'https://www.googleapis.com/auth/drive.readonly'},
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OAUTH_TTL = 300


class GoogleConnectors:
    def __init__(self, network_allowed=lambda:True):
        self._lock = threading.RLock()
        self._bundles: dict[str, dict] = {}
        self._errors: set[str] = set()
        self._dirty: set[str] = set()
        self._disconnected: set[str] = set()
        self._attempts: dict[str, dict] = {}
        self._network_allowed=network_allowed
        self._configured_client_id=os.environ.get('OLCR_CONNECTOR_GOOGLE_CLIENT_ID','').strip()

    def oauth_client_id(self):
        """Return only the public OAuth client ID used to start Google sign-in."""
        with self._lock:
            candidates=[self._configured_client_id]
            for connector_id in CONNECTORS:
                bundle=self._load(connector_id)
                if bundle:candidates.append(bundle.get('client_id',''))
            return next((value for value in candidates if isinstance(value,str) and re.fullmatch(
                r'[A-Za-z0-9._-]{1,300}\.apps\.googleusercontent\.com',value)), '')

    def _info(self, connector_id):
        if connector_id not in CONNECTORS: raise ValueError('CONNECTOR_NOT_FOUND')
        return CONNECTORS[connector_id]

    def _load(self, connector_id):
        info=self._info(connector_id)
        if connector_id not in self._bundles and connector_id not in self._disconnected:
            raw=os.environ.get(info['handle'])
            if raw:
                try:self.set_bundle(connector_id,raw)
                except ValueError:self._errors.add(connector_id)
        return self._bundles.get(connector_id)

    def status(self, connector_id):
        with self._lock:
            bundle=self._load(connector_id)
            state='reconnect_required' if connector_id in self._errors else 'connected' if bundle else 'disconnected'
            if bundle and not bundle.get('refresh_token') and bundle['expires_at']<=time.time():state='reconnect_required'
            extra_scopes=CONNECTORS[connector_id].get('additional_scopes',())
            missing=bool(bundle and any(scope not in bundle.get('scope','').split() for scope in extra_scopes))
            send_required=missing and connector_id=='service.google.gmail'
            write_required=missing and connector_id=='service.google.calendar'
            if state=='connected' and missing:state='send_consent_required' if send_required else 'write_consent_required'
            return {'state':state,'connected':state in {'connected','send_consent_required','write_consent_required'},
                    'send_consent_required':send_required,'write_consent_required':write_required,
                    'credential_handle':self._info(connector_id)['handle'],'credential_sync_required':connector_id in self._dirty}

    def set_bundle(self, connector_id, bundle: str):
        info=self._info(connector_id)
        try:
            value=json.loads(bundle)
            if not isinstance(value,dict) or len(bundle)>16_384:raise ValueError()
            if not all(isinstance(value.get(key),str) and value[key] for key in ('client_id','access_token','scope')):raise ValueError()
            if info['scope'] not in value['scope'].split():raise ValueError()
            if not isinstance(value.get('expires_at'),(int,float)) or not 0<=value['expires_at']<10**12:raise ValueError()
            if not all(isinstance(value.get(key,''),str) for key in ('client_secret','refresh_token')):raise ValueError()
        except (TypeError,ValueError,KeyError) as exc:raise ValueError('CONNECTOR_CREDENTIAL_INVALID') from exc
        with self._lock:
            self._bundles[connector_id]={key:value[key] for key in ('client_id','client_secret','access_token','refresh_token','expires_at','scope') if key in value}
            self._errors.discard(connector_id);self._disconnected.discard(connector_id)
            self._dirty.discard(connector_id)
            for attempt_id,item in list(self._attempts.items()):
                if item['connector_id']==connector_id and item.get('bundle')==bundle:
                    self._stop(item);self._attempts.pop(attempt_id)

    def disconnect(self, connector_id):
        self._info(connector_id)
        with self._lock:
            self._bundles.pop(connector_id,None);self._errors.discard(connector_id);self._disconnected.add(connector_id)
            self._dirty.discard(connector_id)
            os.environ.pop(CONNECTORS[connector_id]['handle'],None)
            for attempt in self._attempts.values():
                if attempt['connector_id']==connector_id:
                    attempt['status']='cancelled';attempt.pop('bundle',None)
                    self._stop(attempt)

    def _request(self, url, *, form=None, token=None, json_body=None, method=None, etag=None, resource_errors=False):
        if not self._network_allowed():raise ValueError('EXTERNAL_ACCESS_REQUIRED')
        if form is not None and json_body is not None:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        headers={'Accept':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        if etag:headers['If-Match']=etag
        data=urllib.parse.urlencode(form).encode() if form is not None else json.dumps(json_body,separators=(',',':')).encode() if json_body is not None else None
        if form is not None:headers['Content-Type']='application/x-www-form-urlencoded'
        elif json_body is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(url,data=data,headers=headers,method=method)
        # No redirect can carry credentials to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
                raw=response.read(MAX_RESPONSE_BYTES+1)
                if method=='DELETE' and not raw:return {}
                if len(raw)>MAX_RESPONSE_BYTES:raise ValueError('CONNECTOR_RESPONSE_TOO_LARGE')
                value=json.loads(raw)
                if not isinstance(value,dict):raise ValueError('CONNECTOR_INVALID_RESPONSE')
                return value
        except urllib.error.HTTPError as exc:
            if resource_errors and exc.code in {400,404,409,412}:
                raise ValueError({400:'CONNECTOR_REQUEST_REJECTED',404:'CONNECTOR_RESOURCE_NOT_FOUND',409:'CONNECTOR_RESOURCE_CONFLICT',412:'CONNECTOR_RESOURCE_CHANGED'}[exc.code]) from None
            raise ValueError('CONNECTOR_RECONNECT_REQUIRED' if exc.code==401 or (exc.code==400 and form is not None) else 'CONNECTOR_PROVIDER_UNAVAILABLE') from None
        except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError) as exc:
            raise ValueError('CONNECTOR_PROVIDER_UNAVAILABLE') from None

    def _request_text(self, url, *, token):
        if not self._network_allowed():raise ValueError('EXTERNAL_ACCESS_REQUIRED')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Accept':'text/plain'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
                raw=response.read(200_001)
                return {'content':raw[:200_000].decode('utf-8',errors='replace'),'truncated':len(raw)>200_000}
        except urllib.error.HTTPError as exc:
            raise ValueError('CONNECTOR_RECONNECT_REQUIRED' if exc.code==401 else 'CONNECTOR_PROVIDER_UNAVAILABLE') from None
        except (urllib.error.URLError,TimeoutError,OSError):raise ValueError('CONNECTOR_PROVIDER_UNAVAILABLE') from None

    def start(self, connector_id, client_id, client_secret=''):
        info=self._info(connector_id)
        client_id=client_id.strip() if isinstance(client_id,str) else ''
        if not client_id:client_id=self.oauth_client_id()
        if not isinstance(client_id,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,300}\.apps\.googleusercontent\.com',client_id):
            raise ValueError('CONNECTOR_GOOGLE_CLIENT_ID_REQUIRED')
        if not isinstance(client_secret,str) or len(client_secret)>1000:raise ValueError('CONNECTOR_CREDENTIAL_INVALID')
        with self._lock:
            for key,item in list(self._attempts.items()):
                if item['expires_at']<=time.time():self._stop(item);self._attempts.pop(key)
            if len(self._attempts)>=8:raise ValueError('CONNECTOR_OAUTH_BUSY')
            attempt_id=secrets.token_urlsafe(24)
            state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
            manager=self
            class Callback(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    valid=urllib.parse.urlsplit(self.path).path=='/callback' and manager._complete(attempt_id,query.get('state',[''])[0],query.get('code',[''])[0],query.get('error',[''])[0])
                    payload=(b'OLCR: You may close this browser tab.' if valid else b'OLCR: Invalid or expired authorization response.')
                    self.send_response(200 if valid else 400);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
            try:server=HTTPServer(('127.0.0.1',0),Callback)
            except OSError:raise ValueError('CONNECTOR_OAUTH_LISTENER_UNAVAILABLE') from None
            server.timeout=0.5
            redirect=f'http://127.0.0.1:{server.server_address[1]}/callback'
            attempt={'connector_id':connector_id,'status':'pending','expires_at':time.time()+OAUTH_TTL,'state':state,'verifier':verifier,'client_id':client_id,'client_secret':client_secret,'redirect_uri':redirect,'server':server}
            self._attempts[attempt_id]=attempt
            def serve():
                try:
                    while attempt['status']=='pending' and time.time()<attempt['expires_at']:server.handle_request()
                except (OSError,ValueError):pass
                finally:server.server_close()
            threading.Thread(target=serve,daemon=True,name='olcr-connector-oauth').start()
            challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            requested_scopes=' '.join((info['scope'],*info.get('additional_scopes',())))
            url='https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode({'client_id':client_id,'redirect_uri':redirect,'response_type':'code','scope':requested_scopes,'state':state,'code_challenge':challenge,'code_challenge_method':'S256','access_type':'offline','prompt':'consent'})
            return {'id':attempt_id,'url':url,'expires_in':OAUTH_TTL,'connector_id':connector_id}

    def _complete(self, attempt_id, state, code, error=''):
        with self._lock:
            item=self._attempts.get(attempt_id)
            if not item or item['status']!='pending' or item['expires_at']<=time.time() or not secrets.compare_digest(state,item['state']):return False
            if error:
                item['status']='failed';item['error']='CONNECTOR_OAUTH_DENIED';return True
            if not code:return False
            item['status']='exchanging'
        try:
            token=self._request('https://oauth2.googleapis.com/token',form={'client_id':item['client_id'],'client_secret':item['client_secret'],'code':code,'code_verifier':item['verifier'],'redirect_uri':item['redirect_uri'],'grant_type':'authorization_code'})
            if not isinstance(token.get('access_token'),str) or not token['access_token']:raise ValueError('CONNECTOR_OAUTH_FAILED')
            info=self._info(item['connector_id'])
            requested_scopes=' '.join((info['scope'],*info.get('additional_scopes',())))
            scope=token.get('scope',requested_scopes)
            if self._info(item['connector_id'])['scope'] not in scope.split():raise ValueError('CONNECTOR_SCOPE_REQUIRED')
            bundle=json.dumps({'client_id':item['client_id'],'client_secret':item['client_secret'],'access_token':token['access_token'],'refresh_token':token.get('refresh_token',''),'expires_at':time.time()+min(86400,max(0,int(token.get('expires_in',3600)))),'scope':scope},separators=(',',':'))
            if len(bundle)>16_384:raise ValueError('CONNECTOR_CREDENTIAL_INVALID')
            with self._lock:
                if item['status']=='exchanging':item.update(status='ready',bundle=bundle)
        except (ValueError,TypeError,OverflowError):
            with self._lock:
                if item['status']=='exchanging':item.update(status='failed',error='CONNECTOR_OAUTH_FAILED')
        finally:
            item.pop('verifier',None);item.pop('client_secret',None)
        return True

    def poll(self, attempt_id):
        with self._lock:
            item=self._attempts.get(attempt_id)
            if not item:raise ValueError('CONNECTOR_OAUTH_NOT_FOUND')
            if item['expires_at']<=time.time():
                self._stop(item);self._attempts.pop(attempt_id);return {'status':'error','error':'CONNECTOR_OAUTH_EXPIRED'}
            result={'status':'pending' if item['status']=='exchanging' else item['status'],'connector_id':item['connector_id']}
            if item['status']=='ready':
                result.update(bundle=item['bundle'],credential_handle=self._info(item['connector_id'])['handle'])
            elif item['status'] in {'failed','cancelled'}:result.update(status='error',error=item.get('error','CONNECTOR_OAUTH_CANCELLED'))
            return result

    def finish(self, attempt_id):
        with self._lock:
            item=self._attempts.pop(attempt_id,None)
            if item:self._stop(item)

    @staticmethod
    def _stop(item):
        if item['status'] in {'pending','exchanging'}:item['status']='cancelled'
        item['server'].server_close()

    def close(self):
        with self._lock:
            for item in self._attempts.values():self._stop(item)
            self._attempts.clear()

    def _access_token(self, connector_id):
        bundle=self._load(connector_id)
        if not bundle or connector_id in self._errors:raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
        if bundle['expires_at']>time.time()+30:return bundle['access_token']
        if not bundle.get('refresh_token'):raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
        token=self._request('https://oauth2.googleapis.com/token',form={'client_id':bundle['client_id'],'client_secret':bundle.get('client_secret',''),'refresh_token':bundle['refresh_token'],'grant_type':'refresh_token'})
        if not token.get('access_token'):raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
        bundle.update(access_token=token['access_token'],expires_at=time.time()+min(86400,max(0,int(token.get('expires_in',3600)))))
        if token.get('refresh_token'):bundle['refresh_token']=token['refresh_token']
        self._dirty.add(connector_id)
        return bundle['access_token']

    def pending_credential(self,connector_id):
        with self._lock:
            self._info(connector_id)
            return self.credential_bundle(connector_id) if connector_id in self._dirty else None

    def acknowledge_credential(self,connector_id,bundle):
        with self._lock:
            if not secrets.compare_digest(self.credential_bundle(connector_id),bundle):raise ValueError('CONNECTOR_CREDENTIAL_CHANGED')
            self._dirty.discard(connector_id)

    def credential_bundle(self, connector_id):
        """Only for authenticated desktop-to-Keychain sync; never include in chat."""
        with self._lock:
            bundle=self._load(connector_id)
            if not bundle:raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
            return json.dumps(bundle,separators=(',',':'))

    def connection_fingerprint(self,connector_id):
        with self._lock:
            bundle=self._load(connector_id)
            if not bundle:raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
            return hashlib.sha256((bundle['client_id']+'\n'+(bundle.get('refresh_token') or bundle['access_token'])).encode()).hexdigest()

    def calendar_request(self, method, calendar_id, event_id='', *, payload=None, etag=None):
        if method not in {'GET','POST','PATCH','DELETE'} or not isinstance(calendar_id,str) or not re.fullmatch(r'[A-Za-z0-9_@.\-]{1,512}',calendar_id):
            raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        if event_id and not re.fullmatch(r'[a-z0-9]{5,128}',event_id):raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        if method!='POST' and not event_id:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        cid='service.google.calendar'
        with self._lock:
            bundle=self._load(cid)
            if not bundle:raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
            if method!='GET' and any(scope not in bundle.get('scope','').split() for scope in CONNECTORS[cid]['additional_scopes']):
                raise ValueError('GOOGLE_CALENDAR_WRITE_CONSENT_REQUIRED')
            token=self._access_token(cid)
            url='https://www.googleapis.com/calendar/v3/calendars/'+urllib.parse.quote(calendar_id,safe='')+'/events'
            if event_id:url+='/'+event_id
            return self._request(url,token=token,json_body=payload,method=method,etag=etag,resource_errors=True)

    def execute(self, connector_id, action, arguments):
        self._info(connector_id)
        if not isinstance(arguments,dict):raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        allowed={'list':{'query','limit'},'get':{'id'},'send':{'to','cc','bcc','subject','body'}}
        if action not in allowed or set(arguments)-allowed[action]:raise ValueError('CONNECTOR_ACTION_UNSUPPORTED')
        suffix=connector_id.rsplit('.',1)[1]
        if action=='send':
            if suffix!='gmail':raise ValueError('CONNECTOR_ACTION_UNSUPPORTED')
            from .mail_message import gmail_raw_message,prepare_mail
            mail=prepare_mail(arguments)
            info=CONNECTORS[connector_id]
            with self._lock:
                bundle=self._load(connector_id)
                if not bundle or connector_id in self._errors:raise ValueError('CONNECTOR_RECONNECT_REQUIRED')
                if any(scope not in bundle.get('scope','').split() for scope in info.get('additional_scopes',())):
                    raise ValueError('GMAIL_SEND_CONSENT_REQUIRED')
                token=self._access_token(connector_id)
                result=self._request('https://gmail.googleapis.com/gmail/v1/users/me/messages/send',token=token,
                                     json_body={'raw':gmail_raw_message(mail)})
            return {'sent':True,'provider':'gmail','id':result.get('id'),
                    'threadId':result.get('threadId'),'recipient_count':mail['recipient_count']}
        if action=='get' and (not isinstance(arguments.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}',arguments['id'])):raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        query=arguments.get('query','')
        if not isinstance(query,str) or len(query)>1000:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        limit=arguments.get('limit',10)
        if type(limit) is not int or not 1<=limit<=50:raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        params={}
        if suffix=='gmail':
            url='https://gmail.googleapis.com/gmail/v1/users/me/messages'
            if action=='list':params={'q':query,'maxResults':limit}
            else:url+='/'+arguments['id'];params={'format':'full'}
        elif suffix=='calendar':
            url='https://www.googleapis.com/calendar/v3/calendars/primary/events'
            if action=='get':url+='/'+arguments['id']
            else:params={'q':query,'maxResults':limit,'singleEvents':'true','orderBy':'startTime','timeMin':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        else:
            url='https://www.googleapis.com/drive/v3/files'
            if action=='list':params={'q':"trashed = false"+(' and fullText contains '+"'"+query.replace('\\','\\\\').replace("'","\\'")+"'" if query else ''),'pageSize':limit,'fields':'files(id,name,mimeType,modifiedTime,webViewLink),nextPageToken'}
            else:url+='/'+arguments['id'];params={'fields':'id,name,mimeType,modifiedTime,webViewLink,description'}
        url+='?'+urllib.parse.urlencode(params)
        with self._lock:
            try:
                token=self._access_token(connector_id)
                result=self._request(url,token=token)
                if suffix=='gmail' and action=='get':
                    payload=result.pop('payload',{})
                    result['headers']=[item for item in payload.get('headers',[]) if item.get('name','').lower() in {'from','to','date','subject'}]
                    parts=[payload];texts=[]
                    while parts:
                        part=parts.pop();parts.extend(part.get('parts',[]))
                        encoded=part.get('body',{}).get('data')
                        if part.get('mimeType')=='text/plain' and isinstance(encoded,str):
                            try:texts.append(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)).decode('utf-8',errors='replace'))
                            except (ValueError,TypeError):pass
                    content='\n'.join(texts)
                    result['content']=content[:200_000]
                    result['content_truncated']=len(content)>200_000
                    if not content:result['content_state']='no_plain_text'
                if suffix=='drive' and action=='get':
                    mime=result.get('mimeType','')
                    content_url='https://www.googleapis.com/drive/v3/files/'+arguments['id']
                    if mime=='application/vnd.google-apps.document':
                        result.update(self._request_text(content_url+'/export?'+urllib.parse.urlencode({'mimeType':'text/plain'}),token=token))
                    elif mime.startswith('text/') or mime in {'application/json','application/xml'}:
                        result.update(self._request_text(content_url+'?alt=media',token=token))
                    else:result['content_state']='unsupported_binary'
                return result
            except ValueError as exc:
                if str(exc)=='CONNECTOR_RECONNECT_REQUIRED':self._errors.add(connector_id)
                raise
