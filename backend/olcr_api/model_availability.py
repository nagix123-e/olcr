"""Preflight facts and short-lived failure evidence, never a probe completion."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import time


def availability(state='unknown',reason='not_verified',retry_at=None):
    return {'state':state,'reason':reason,'retry_at':retry_at,'checked_at':time.time()}


def retry_time(value,now=None):
    now=time.time() if now is None else now
    if value is None:return None
    value=str(value).strip()
    try:
        number=float(value)
        if math.isfinite(number):return min(now+604800,number if number>1_000_000_000 else now+max(0,number))
    except ValueError:pass
    try:
        date=parsedate_to_datetime(value)
        if date.tzinfo is None:date=date.replace(tzinfo=timezone.utc)
        return max(now,min(now+604800,date.timestamp()))
    except (ValueError,TypeError,OverflowError):pass
    try:
        date=datetime.fromisoformat(value.replace('Z','+00:00'))
        if date.tzinfo:return max(now,min(now+604800,date.timestamp()))
    except ValueError:pass
    pieces=re.findall(r'(\d+(?:\.\d+)?)(ms|h|m|s)',value)
    if pieces and ''.join(n+u for n,u in pieces)==value:
        seconds=sum(float(n)*{'ms':.001,'s':1,'m':60,'h':3600}[u] for n,u in pieces)
        return now+min(604800,seconds)
    return None


def attach_http_failure(failure,response):
    failure.http_status=getattr(response,'code',None)
    headers=getattr(response,'headers',{}) or {}
    failure.retry_at=retry_time(headers.get('Retry-After') or headers.get('retry-after'))
    return failure


class AvailabilityStore:
    def __init__(self,db):
        self.db=db
        with db.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS model_availability(provider TEXT NOT NULL,account INTEGER NOT NULL,base_url TEXT NOT NULL,model TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,retry_at REAL,checked_at REAL NOT NULL,PRIMARY KEY(provider,account,base_url,model))')
    def clear(self,provider,account):
        with self.db.connect() as con:con.execute('DELETE FROM model_availability WHERE provider=? AND account=?',(provider,account))
    def get(self,provider,account,base,model):
        with self.db.connect() as con:
            rows=con.execute("SELECT state,reason,retry_at,checked_at FROM model_availability WHERE provider=? AND account=? AND base_url=? AND model IN (?, '*') ORDER BY checked_at DESC",(provider,account,base,model)).fetchall()
        now=time.time()
        for row in rows:
            state,reason,retry,checked=row
            if state=='unavailable' and (retry is None or retry>now):return {'state':state,'reason':reason,'retry_at':retry,'checked_at':checked}
        for row in rows:
            if row[0]=='available' and now-row[3]<300:return {'state':'available','reason':'recent_success','retry_at':None,'checked_at':row[3]}
        return availability()
    def save(self,provider,account,base,model,state,reason,retry=None):
        with self.db.connect() as con:
            con.execute('INSERT OR REPLACE INTO model_availability VALUES(?,?,?,?,?,?,?,?)',(provider,account,base,model,state,reason,retry,time.time()))
    def success(self,provider,account,base,model,headers=None):
        with self.db.connect() as con:con.execute("DELETE FROM model_availability WHERE provider=? AND account=? AND base_url=? AND model='*'",(provider,account,base))
        self.save(provider,account,base,model,'available','recent_success')
        headers=headers or {};deadlines=[]
        for unit in ('requests','tokens'):
            try: exhausted=float(headers.get('x-ratelimit-remaining-'+unit,'nan'))<=0
            except (TypeError,ValueError):exhausted=False
            if exhausted:deadlines.append(retry_time(headers.get('x-ratelimit-reset-'+unit)) or retry_time(headers.get('retry-after')) or time.time()+30)
        if deadlines:self.save(provider,account,base,model,'unavailable','rate_limited',max(deadlines))
    def failure(self,provider,account,base,model,failure):
        status=getattr(failure,'http_status',None);category=failure.category
        deadline=getattr(failure,'retry_at',None)
        if category in {'credential_expired','credential_required'} or status==401:
            model='*';reason='reauth_required';seconds=900
        elif status==429 or category=='model_concurrency_limit':reason='rate_limited';seconds=30
        elif status==403:reason='access_denied';seconds=300
        elif status==404:reason='model_or_endpoint_unavailable';seconds=300
        elif status in {500,502,503,504} or category in {'provider_unavailable','timeout','unavailable'}:
            reason='recent_connection_failure';seconds=15
        else:return # A bad prompt/schema/image does not make a model unavailable.
        self.save(provider,account,base,model,'unavailable',reason,max(deadline,time.time()) if deadline is not None else time.time()+seconds)
