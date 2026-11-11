"""Finite medication occurrences with durable per-event delivery state."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import re
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo
from .medication_records import MedicationRecords, number, physical_key

_LOCK=threading.RLock()

class MedicationCalendar:
    def __init__(self,db,actions,connectors):
        self.db,self.actions,self.connectors=db,actions,connectors
        with closing(db.connect()) as con,con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS medication_calendar_plans(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                  data_json TEXT NOT NULL, expires_at REAL NOT NULL, approved INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS medication_calendar_occurrences(id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                  payload_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT);
            ''')

    def prepare(self,data,cid):
        with _LOCK:return self._prepare(data,cid)

    def _prepare(self,data,cid):
        records=MedicationRecords(self.db)
        med=records.resolve(data['medication_id'])
        if re.search(r'頓服|必要時|痛いとき|痛い時|症状.{0,5}(?:時|とき)|as needed|\bPRN\b',med['instructions'],re.I):
            raise ValueError('MEDICATION_PRN_NOT_SCHEDULED')
        if data.get('provider') not in {'apple','google'}: raise ValueError('MEDICATION_PROVIDER_INVALID')
        data=dict(data)
        self._guard(data)
        if data['provider']=='apple' and data.get('calendar_id','primary')=='primary':
            data['calendar_id']=self.actions._native_create_calendar('primary')
        if data['provider']=='google':data['connection']=self.connectors.google.connection_fingerprint('service.google.calendar')
        start=date.fromisoformat(data['start_date']); end=date.fromisoformat(data['end_date']); tz=ZoneInfo(data['zone'])
        if start<datetime.now(tz).date() or not 0<=(end-start).days<=365: raise ValueError('MEDICATION_DATE_RANGE_INVALID')
        times=data['times']
        if not isinstance(times,list) or not times or len(times)>12 or len(set(times))!=len(times): raise ValueError('MEDICATION_TIMES_INVALID')
        if (end-start).days*len(times)+len(times)>500: raise ValueError('MEDICATION_PLAN_TOO_LARGE')
        reminder=data.get('reminder')
        if reminder is not None and (type(reminder) is not int or not 0<=reminder<=40320): raise ValueError('MEDICATION_REMINDER_INVALID')
        if type(data.get('reveal',False)) is not bool: raise ValueError('MEDICATION_PRIVACY_INVALID')
        if not isinstance(data.get('calendar_id','primary'),str) or len(data.get('calendar_id','primary'))>512: raise ValueError('MEDICATION_CALENDAR_INVALID')
        payloads=[]; day=start
        while day<=end:
            for clock in sorted(times):
                if not re.fullmatch(r'\d{2}:\d{2}',clock): raise ValueError('MEDICATION_TIMES_INVALID')
                local=datetime.fromisoformat(day.isoformat()+'T'+clock)
                when=local.replace(tzinfo=tz)
                # Never shift a nonexistent or ambiguous wall time without asking.
                if (when.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)!=local
                    or when.replace(fold=1).utcoffset()!=when.utcoffset()): raise ValueError('MEDICATION_DST_TIME_INVALID')
                if when<=datetime.now(timezone.utc):raise ValueError('MEDICATION_PAST_TIME')
                ident='olcr'+uuid4().hex
                payload={'id':ident,'summary':f"{med['name']} {med['strength']} {med['dose']}{med['unit']} 服用" if data.get('reveal') else '服用予定',
                    'start':{'dateTime':when.isoformat(),'timeZone':data['zone']},
                    'end':{'dateTime':(when+timedelta(minutes=5)).isoformat(),'timeZone':data['zone']},
                    'description':'OLCR medication occurrence '+ident,
                    'reminders':{'useDefault':False,'overrides':[] if reminder is None else [{'method':'popup','minutes':reminder}]}}
                if len(payload['summary'])>200:raise ValueError('MEDICATION_TITLE_TOO_LONG')
                payloads.append(payload)
            day+=timedelta(days=1)
        timestamps=sorted(datetime.fromisoformat(p['start']['dateTime']).timestamp() for p in payloads)
        proposed=set(timestamps)
        quantities=[(t,number(med['dose'])) for t in timestamps]
        with closing(self.db.connect()) as con:
            existing=con.execute("SELECT o.payload_json,p.data_json FROM medication_calendar_occurrences o JOIN medication_calendar_plans p ON p.id=o.plan_id WHERE o.status IN ('pending','executing','uncertain','completed') AND (p.approved=1 OR p.expires_at>?)",(time.time(),)).fetchall()
        by_id={m['id']:m for m in records.medications()}
        for row in existing:
            old_plan=json.loads(row['data_json'])
            old_med=by_id.get(old_plan['medication_id'])
            if old_med is None:continue
            if physical_key(old_med)!=physical_key(med):continue
            old_payload=json.loads(row['payload_json'])
            when=datetime.fromisoformat(old_payload['start']['dateTime']).timestamp()
            if when in proposed:raise ValueError('MEDICATION_SCHEDULE_EXISTS')
            if min(proposed)-86400<when<max(proposed)+86400:
                quantities.append((when,number(old_med['dose'])))
        timestamps=sorted(t for t,q in quantities)
        for index,when in enumerate(timestamps):
            if med['min_interval_hours'] and index and (when in proposed or timestamps[index-1] in proposed) and when-timestamps[index-1]<float(number(med['min_interval_hours']))*3600:
                raise ValueError('MEDICATION_PRESCRIPTION_CONFLICT')
            if med['max_per_24h'] and any(when-86400<t<=when for t in proposed) and sum(q for t,q in quantities if when-86400<t<=when)>number(med['max_per_24h']):
                raise ValueError('MEDICATION_PRESCRIPTION_CONFLICT')
        ident=uuid4().hex
        plan={**data,'id':ident,'count':len(payloads),'calendar_id':data.get('calendar_id','primary')}
        with closing(self.db.connect()) as con,con:
            con.execute('INSERT INTO medication_calendar_plans VALUES(?,?,?,?,0)',(ident,cid,json.dumps(plan),time.time()+1800))
            con.executemany('INSERT INTO medication_calendar_occurrences VALUES(?,?,?,\'pending\',NULL)',[(p['id'],ident,json.dumps(p)) for p in payloads])
        return plan

    def _load(self,ident,cid):
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT * FROM medication_calendar_plans WHERE id=? AND conversation_id=?',(ident,cid)).fetchone()
        if not row: raise ValueError('MEDICATION_PLAN_NOT_FOUND')
        return row,json.loads(row['data_json'])

    def _guard(self,plan):
        if plan['provider']=='apple':
            if getattr(self.actions.calendar,'provider',None)!='apple':raise ValueError('CALENDAR_PROVIDER_CHANGED')
            self.actions.calendar_network()
            if not self.actions.calendar.status()['connected']: raise ValueError('CALENDAR_NOT_CONNECTED')
        else:
            if plan.get('connection') and plan['connection']!=self.connectors.google.connection_fingerprint('service.google.calendar'):
                raise ValueError('CALENDAR_PROVIDER_CHANGED')
            self.connectors.authorize('service.google.calendar','write')
            if self.connectors.google.status('service.google.calendar').get('write_consent_required'):
                raise ValueError('GOOGLE_CALENDAR_WRITE_CONSENT_REQUIRED')

    def _request(self,plan,method,ident='',payload=None,etag=None):
        self._guard(plan)
        if plan['provider']=='google':
            return self.connectors.calendar_request(method,plan['calendar_id'],ident,payload=payload,etag=etag)
        return self.actions.calendar.request(method,self.actions.calendar.event_path(plan['calendar_id'],ident),payload=payload,etag=etag)

    def _reconcile(self,plan,payload):
        if plan['provider']=='google':
            try:return self._request(plan,'GET',payload['id'])
            except ValueError as exc:
                if str(exc)=='CONNECTOR_RESOURCE_NOT_FOUND':return None
                raise
        # Native IDs differ from the durable operation ID; search only this occurrence's window.
        adapter=self.actions.calendar
        if not hasattr(adapter,'events'): return self._request(plan,'GET',payload['id'])
        self._guard(plan)
        start=datetime.fromisoformat(payload['start']['dateTime']); end=datetime.fromisoformat(payload['end']['dateTime'])
        result=adapter.events(plan['calendar_id'],start-timedelta(seconds=1),end+timedelta(seconds=1))
        matches=[e for e in result.get('events',[]) if e.get('description')==payload['description']]
        if not matches and not result.get('more'):return None
        if len(matches)!=1: raise ValueError('MEDICATION_DELIVERY_UNCERTAIN')
        return matches[0]

    def _save(self,ident,status,result=None):
        with closing(self.db.connect()) as con,con:
            con.execute('UPDATE medication_calendar_occurrences SET status=?,result_json=? WHERE id=?',
                        (status,json.dumps(result or {}),ident))

    def confirm(self,ident,cid):
        with _LOCK:
            row,plan=self._load(ident,cid); self._guard(plan)
            if not row['approved'] and row['expires_at']<time.time(): raise ValueError('MEDICATION_PLAN_EXPIRED')
            if plan['provider']=='apple' and plan['calendar_id']=='primary':
                plan['calendar_id']=self.actions._native_create_calendar('primary')
            with closing(self.db.connect()) as con,con:
                con.execute('UPDATE medication_calendar_plans SET approved=1,data_json=? WHERE id=?',(json.dumps(plan),ident))
                con.execute("UPDATE medication_calendar_occurrences SET status='uncertain' WHERE plan_id=? AND status='executing'",(ident,))
                occurrences=con.execute("SELECT * FROM medication_calendar_occurrences WHERE plan_id=? AND status IN ('pending','uncertain') ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,rowid LIMIT 20",(ident,)).fetchall()
            for item in occurrences:
                payload=json.loads(item['payload_json'])
                attempted=False
                try:
                    self._guard(plan)
                    if item['status']=='uncertain': result=self._reconcile(plan,payload)
                    else: result=None
                    if result is None:
                        self._save(item['id'],'executing')
                        attempted=True
                        result=self._request(plan,'POST',payload=payload)
                    self._save(item['id'],'completed',result)
                except (ValueError,OSError) as exc:
                    # An external write may have succeeded before any transport error. Never blindly retry.
                    status='failed' if str(exc)=='CONNECTOR_REQUEST_REJECTED' else 'uncertain' if attempted or item['status']=='uncertain' else 'pending'
                    self._save(item['id'],status,{'error':'DELIVERY_UNCONFIRMED'})
                    if 'PERMISSION' in str(exc) or 'DISABLED' in str(exc): break
            with closing(self.db.connect()) as con:
                counts={r['status']:r['n'] for r in con.execute('SELECT status,COUNT(*) n FROM medication_calendar_occurrences WHERE plan_id=? GROUP BY status',(ident,))}
            return {'id':ident,'counts':counts}

    def cancel_pending(self,ident,cid):
        with _LOCK:
            self._load(ident,cid)
            with closing(self.db.connect()) as con,con:
                con.execute("UPDATE medication_calendar_occurrences SET status='cancelled' WHERE plan_id=? AND status='pending'",(ident,))

    def stop(self,ident,cid):
        with _LOCK:
            row,plan=self._load(ident,cid); self._guard(plan); deleted=failed=0
            with closing(self.db.connect()) as con:
                occurrences=con.execute('SELECT * FROM medication_calendar_occurrences WHERE plan_id=?',(ident,)).fetchall()
            for item in occurrences:
                payload=json.loads(item['payload_json'])
                if datetime.fromisoformat(payload['start']['dateTime'])<=datetime.now(timezone.utc): continue
                if item['status']=='pending': self._save(item['id'],'cancelled'); continue
                if item['status']!='completed':
                    if item['status'] in {'uncertain','executing'}: failed+=1
                    continue
                saved=json.loads(item['result_json']); eid=saved['id']
                try:
                    current=self._request(plan,'GET',eid)
                    if current.get('etag')!=saved.get('etag'): raise ValueError('CALENDAR_EVENT_CHANGED')
                    self._request(plan,'DELETE',eid,etag=current.get('etag'))
                    self._save(item['id'],'cancelled'); deleted+=1
                except (ValueError,OSError): failed+=1
            return {'deleted':deleted,'failed':failed}
