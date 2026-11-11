"""User-reviewed text-field drafts in existing managed public browser sessions.

The fixed DOM setter intentionally emits no input/change/keyboard events. It
does not invoke submit, click, payment or reservation operations; users finish on the site.
"""
import hashlib
import ipaddress
import json
import math
import re
import threading
import time
import uuid
from urllib.parse import urlsplit, unquote
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from .permissions import capability_enabled

SENSITIVE=re.compile(r'password|passwd|passcode|secret|token|api[ _-]?key|one[ _-]?time|otp|\bpin\b|csrf|cc-|webauthn|verification|security[ _-]?code|cvv|cvc|iban|credit|debit|card|bank|routing|payment|billing|account|username|user[ _-]?id|ログイン|パスワード|暗証|認証|確認コード|ワンタイム|秘密|銀行|口座|カード|支払|決済',re.I)
SAFE_TYPES={'text','search','email','tel','url','date','time','datetime-local','month','week','textarea'}
IDENTITY=('session_id','process_id','url','origin','document_id')


def public_form_origin(url):
    try:
        if not isinstance(url,str) or len(url)>4096 or any(ord(c)<33 for c in url) or '\\' in url:raise ValueError()
        p=urlsplit(url);host=p.hostname or ''
        if p.scheme!='https' or not host or '.' not in host or p.username or p.password or p.port not in (None,443) or host.endswith(('.localhost','.local','.internal')):raise ValueError()
        try:ipaddress.ip_address(host)
        except ValueError:pass
        else:raise ValueError()
        if re.search(r'(?:^|[/_.-])(?:log[ _-]?in|sign[ _-]?in|password|checkout|payment|billing|oauth|authorize|authentication)(?:[/_.-]|$)',unquote(p.path),re.I):raise ValueError()
        return 'https://'+host
    except (ValueError,TypeError):raise ValueError('BROWSER_FORM_PUBLIC_PAGE_REQUIRED') from None


def safe_form_field(field):
    if not isinstance(field,dict):return False
    label=field.get('label');selector=field.get('selector');tag=field.get('tag');kind=field.get('type')
    return bool(isinstance(label,str) and 1<=len(label)<=160 and label==' '.join(label.split()) and not SENSITIVE.search(label) and isinstance(selector,str) and 1<=len(selector)<=512 and not any(c in selector for c in '{};\r\n\x00') and tag in ('input','textarea') and isinstance(kind,str) and kind in SAFE_TYPES and not SENSITIVE.search(str(field.get('metadata',''))))


def _encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


class BrowserForms:
    def __init__(self,db,browser_sessions,settings):
        self.db,self.browser_sessions,self.settings=db,browser_sessions,settings;self.lock=threading.RLock()
        with db.connect() as con:con.execute('CREATE TABLE IF NOT EXISTS browser_form_reviews (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, fingerprint TEXT NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL)')

    def _cleanup(self):
        with self.db.connect() as con:con.execute("DELETE FROM browser_form_reviews WHERE expires_at<=? OR status!='pending'",(time.time(),))

    def _permission(self):
        if not capability_enabled(self.settings(),'external_access_enabled'):raise ValueError('EXTERNAL_ACCESS_REQUIRED')
        return True

    def sessions(self):
        self._permission();self._cleanup();rows=[]
        for item in self.browser_sessions.list():
            try:public_form_origin(item.get('url'))
            except ValueError:continue
            rows.append({'session_id':item['session_id'],'url':item['url']})
        return {'sessions':rows}

    def inspect(self,session_id):
        self._permission();self._cleanup()
        value=self.browser_sessions.inspect_public_form(session_id)
        if value.get('status')!='AVAILABLE':raise ValueError('BROWSER_FORM_INSPECTION_FAILED')
        origin=public_form_origin(value.get('url'))
        if value.get('origin')!=origin or value.get('session_id')!=session_id or type(value.get('process_id')) is not int or value['process_id']<=0 or type(value.get('document_id')) not in (int,float) or not math.isfinite(value['document_id']):raise ValueError('BROWSER_FORM_IDENTITY_INVALID')
        if not isinstance(value.get('fields'),list) or len(value['fields'])>100:raise ValueError('BROWSER_FORM_INSPECTION_FAILED')
        fields=[{key:field[key] for key in ('selector','label','tag','type')} for field in value.get('fields',[]) if safe_form_field(field)]
        counts={field['label']:sum(other['label']==field['label'] for other in fields) for field in fields}
        fields=[field for field in fields if counts[field['label']]==1]
        return {key:value[key] for key in IDENTITY}|{'fields':fields}

    def prepare(self,session_id,values):
        self._permission()
        if not isinstance(values,dict) or not 1<=len(values)<=10:raise ValueError('BROWSER_FORM_VALUES_INVALID')
        evidence=self.inspect(session_id);available={field['label']:field for field in evidence['fields']};fields=[]
        for label,value in values.items():
            if label not in available or not isinstance(value,str) or not 1<=len(value)<=2000 or '\x00' in value or re.search(r'(?:\d[ -]?){13,19}|(?:sk-|AKIA)[A-Za-z0-9]{12,}|BEGIN [A-Z ]*PRIVATE KEY',value):raise ValueError('BROWSER_FORM_VALUES_INVALID')
            fields.append({**available[label],'value':value})
        rid=uuid.uuid4().hex;expires=time.time()+300
        payload={'id':rid,'session':{key:evidence[key] for key in IDENTITY},'fields':fields,'expires_at':expires,'status':'pending','submitted':False}
        encoded=_encoded(payload)
        with self.lock,self.db.connect() as con:
            con.execute("DELETE FROM browser_form_reviews WHERE expires_at<=? OR status!='pending'",(time.time(),))
            if con.execute("SELECT COUNT(*) FROM browser_form_reviews").fetchone()[0]>=100:raise ValueError('BROWSER_FORM_REVIEW_LIMIT')
            con.execute('INSERT INTO browser_form_reviews VALUES (?,?,?,?,?)',(rid,encoded,hashlib.sha256(encoded.encode()).hexdigest(),expires,'pending'))
        return payload

    def confirm(self,rid,approve,review):
        if type(approve) is not bool:raise ValueError('BROWSER_FORM_APPROVAL_INVALID')
        if approve:self._permission()
        encoded=_encoded(review)
        with self.lock:
            with self.db.connect() as con:
                row=con.execute('SELECT payload_json,fingerprint,expires_at,status FROM browser_form_reviews WHERE id=?',(rid,)).fetchone()
                if not row or row[3]!='pending' or row[2]<=time.time() or row[1]!=hashlib.sha256(encoded.encode()).hexdigest():raise ValueError('BROWSER_FORM_REVIEW_CHANGED_OR_EXPIRED')
                saved=json.loads(row[0])
            if approve:
                evidence=self.inspect(saved['session']['session_id'])
                if any(evidence[key]!=saved['session'][key] for key in IDENTITY):raise ValueError('BROWSER_FORM_PAGE_CHANGED')
                if any({key:field[key] for key in ('selector','label','tag','type')} not in evidence['fields'] for field in saved['fields']):raise ValueError('BROWSER_FORM_FIELD_CHANGED')
            # Consume before touching the browser: a failed/partial fill cannot be replayed.
            with self.db.connect() as con:
                updated=con.execute("UPDATE browser_form_reviews SET status=?,payload_json='{}' WHERE id=? AND status='pending' AND expires_at>?",('applying' if approve else 'denied',rid,time.time())).rowcount
                if updated!=1:raise ValueError('BROWSER_FORM_REVIEW_ALREADY_USED')
            if not approve:return {'status':'denied','submitted':False}
            self._permission()
            try:result=self.browser_sessions.fill_public_form(saved['session']['session_id'],saved['session'],saved['fields'],permission_check=self._permission)
            except Exception:
                with self.db.connect() as con:con.execute("UPDATE browser_form_reviews SET status='failed' WHERE id=?",(rid,))
                raise ValueError('BROWSER_FORM_APPLY_FAILED') from None
            status='applied' if result.get('status')=='AVAILABLE' else 'failed'
            with self.db.connect() as con:con.execute('UPDATE browser_form_reviews SET status=? WHERE id=?',(status,rid))
            if status!='applied':raise ValueError('BROWSER_FORM_APPLY_FAILED')
            return {'status':'applied','filled':len(saved['fields']),'submitted':False}


class FormValues(BaseModel):
    model_config=ConfigDict(extra='forbid')
    values:dict[str,str]
class FormConfirmation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    approve:StrictBool
    review:dict


def register_browser_form_routes(app,host_callable):
    services={}
    def service():
        context=host_callable();db=context['db'];key=id(db)
        if key not in services:services[key]=BrowserForms(db,context['browser_sessions'],lambda:host_callable()['settings'])
        return services[key]
    def invoke(call):
        try:return call()
        except ValueError as exc:
            code=str(exc);safe=code if re.fullmatch('[A-Z_]+',code) else 'BROWSER_FORM_REQUEST_FAILED'
            raise HTTPException(403 if safe=='EXTERNAL_ACCESS_REQUIRED' else 422,safe) from None
    @app.get('/api/browser-forms/sessions')
    def sessions():return invoke(lambda:service().sessions())
    @app.get('/api/browser-forms/sessions/{session_id}/fields')
    def fields(session_id:str):return invoke(lambda:service().inspect(session_id))
    @app.post('/api/browser-forms/sessions/{session_id}/prepare')
    def prepare(session_id:str,value:FormValues):return invoke(lambda:service().prepare(session_id,value.values))
    @app.post('/api/browser-forms/reviews/{rid}/confirm')
    def confirm(rid:str,value:FormConfirmation):return invoke(lambda:service().confirm(rid,value.approve,value.review))

# These host-owned functions are never supplied by the model or API caller.
PUBLIC_FORM_SCAN=r'''() => {
 const sensitive=new RegExp(__OLCR_SENSITIVE__,"i");
 const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=="hidden"&&s.display!=="none"&&!el.closest('[aria-hidden="true"],[inert]');};
 const labelOf=el=>{const ids=el.getAttribute('aria-labelledby');const refs=ids?ids.split(/\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' '):'';const associated=el.labels?[...el.labels].map(l=>l.innerText||l.textContent||'').join(' '):'';return (el.getAttribute('aria-label')||refs||associated).replace(/\s+/g,' ').trim();};
 const pathOf=el=>{const parts=[];for(let n=el;n&&n.nodeType===1;n=n.parentElement){let part=n.tagName.toLowerCase();if(n.parentElement){const peers=[...n.parentElement.children].filter(x=>x.tagName===n.tagName);part+=':nth-of-type('+(peers.indexOf(n)+1)+')';}parts.unshift(part);if(parts.length>30)return '';}return parts.join(' > ');};
 const metadata=el=>[el.name,el.id,el.getAttribute('autocomplete'),el.getAttribute('inputmode'),labelOf(el)].join(' ');
 const fields=[];
 for(const el of document.querySelectorAll('input,textarea')){
  if(!visible(el)||el.disabled||el.readOnly||el.matches(':disabled')||el.getAttribute('aria-disabled')==='true'||el.getAttribute('aria-readonly')==='true')continue;
  const tag=el.tagName.toLowerCase(),type=tag==='textarea'?'textarea':el.type.toLowerCase();
  if(!['text','search','email','tel','url','date','time','datetime-local','month','week','textarea'].includes(type))continue;
  const label=labelOf(el),selector=pathOf(el),meta=metadata(el);
  if(!label||label.length>160||!selector||selector.length>512||sensitive.test(meta))continue;
  const form=el.form;
  if(form&&(sensitive.test([form.id,form.name,form.getAttribute('action')].join(' '))||[...form.querySelectorAll('input')].some(x=>visible(x)&&(x.type==='password'||sensitive.test(metadata(x))))))continue;
  fields.push({selector,label,tag,type});if(fields.length>=100)break;
 }
 const unique=fields.filter(f=>fields.filter(other=>other.label===f.label).length===1);
 return JSON.stringify({url:location.href,origin:location.origin,document_id:performance.timeOrigin,fields:unique});
}'''.replace('__OLCR_SENSITIVE__',json.dumps(SENSITIVE.pattern))

PUBLIC_FORM_SET=r'''() => {
 const cfg=__OLCR_CONFIG__;
 if(location.href!==cfg.url||location.origin!==cfg.origin||performance.timeOrigin!==cfg.document_id)return JSON.stringify({ok:false,reason:'PAGE_CHANGED'});
 const evidence=JSON.parse((__OLCR_SCAN__)());
 const targets=[];
 for(const field of cfg.fields){
  if(!evidence.fields.some(f=>f.selector===field.selector&&f.label===field.label&&f.tag===field.tag&&f.type===field.type))return JSON.stringify({ok:false,reason:'FIELD_CHANGED'});
  const matches=[...document.querySelectorAll(field.selector)];if(matches.length!==1)return JSON.stringify({ok:false,reason:'TARGET_NOT_UNIQUE'});
  targets.push([matches[0],field]);
 }
 // Assign the DOM value only: no events, focus, keyboard input, click or submit.
 for(const [el,field] of targets){const proto=field.tag==='textarea'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;if(!setter)return JSON.stringify({ok:false,reason:'SETTER_UNAVAILABLE'});setter.call(el,field.value);}
 return JSON.stringify({ok:targets.every(([el,f])=>el.value===f.value),filled:targets.length,submitted:false});
}'''.replace('__OLCR_SCAN__',PUBLIC_FORM_SCAN)
