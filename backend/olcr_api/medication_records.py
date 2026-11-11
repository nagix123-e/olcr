"""Local medication identities and reported intakes; never infer a prescription."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo


def key(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value)).strip().casefold()


def physical_key(med):
    return (key(med['name']),unicodedata.normalize('NFKC',med['strength']).strip(),unicodedata.normalize('NFKC',med['unit']).strip())


def number(value, code='MEDICATION_QUANTITY_INVALID'):
    if isinstance(value, bool): raise ValueError(code)
    try: result = Decimal(str(value))
    except (InvalidOperation, ValueError): raise ValueError(code) from None
    if not result.is_finite() or not 0 < result <= 1_000_000: raise ValueError(code)
    return result


def aware(value):
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError: raise ValueError('MEDICATION_TIME_INVALID') from None
    if not isinstance(value, datetime) or value.tzinfo is None: raise ValueError('MEDICATION_TIME_INVALID')
    return value.astimezone(timezone.utc)


def validate_drug(data):
    allowed = {'name', 'strength', 'dose', 'unit', 'instructions', 'ingredient', 'purpose', 'max_per_24h', 'min_interval_hours'}
    if not isinstance(data, dict) or set(data)-allowed: raise ValueError('MEDICATION_FIELDS_INVALID')
    result = {}
    for field, limit in [('name',200), ('strength',100), ('unit',30), ('instructions',4000)]:
        value = data.get(field)
        if not isinstance(value,str) or not value.strip() or len(value)>limit or any(ord(c)<32 and (field!='instructions' or c not in '\n\t') for c in value):
            raise ValueError('MEDICATION_REQUIRED:'+field)
        result[field] = value.strip()
    result['dose'] = str(number(data.get('dose')))
    ingredient = data.get('ingredient', '')
    if not isinstance(ingredient,str) or len(ingredient)>200: raise ValueError('MEDICATION_INGREDIENT_INVALID')
    result['ingredient'] = ingredient.strip()
    purpose=data.get('purpose','')
    if not isinstance(purpose,str) or len(purpose)>200: raise ValueError('MEDICATION_PURPOSE_INVALID')
    result['purpose']=purpose.strip()
    for field in ['max_per_24h','min_interval_hours']:
        value = data.get(field)
        result[field] = str(number(value)) if value not in (None,'') else None
    return result


class MedicationRecords:
    def __init__(self, db):
        self.db = db
        with closing(db.connect()) as con, con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS medication_drugs(id TEXT PRIMARY KEY, data_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS medication_intakes(id TEXT PRIMARY KEY, medication_id TEXT NOT NULL,
                  quantity TEXT NOT NULL, taken_at REAL NOT NULL, source_id TEXT NOT NULL UNIQUE,
                  request_hash TEXT NOT NULL, voided INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS medication_intake_time ON medication_intakes(taken_at);
                CREATE TABLE IF NOT EXISTS medication_intake_audit(id TEXT PRIMARY KEY, intake_id TEXT NOT NULL,
                  previous_json TEXT NOT NULL, changed_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS medication_chat_turns(source_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                  response_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS medication_chat_states(conversation_id TEXT PRIMARY KEY,
                  data_json TEXT NOT NULL, expires_at REAL NOT NULL);
            ''')

    def register(self, data, source_id=''):
        data = validate_drug(data)
        # Include the prescribed instructions and limits: a changed prescription is distinct.
        canonical = {k:(key(v) if k in {'name','ingredient','purpose'} else unicodedata.normalize('NFKC',v).strip()) if isinstance(v,str) else v for k,v in data.items()}
        ident = hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
        with closing(self.db.connect()) as con, con:
            con.execute('INSERT OR IGNORE INTO medication_drugs VALUES(?,?)',(ident,json.dumps(data,ensure_ascii=False)))
        return {'id':ident, **data}

    def medications(self):
        with closing(self.db.connect()) as con, con: rows = con.execute('SELECT * FROM medication_drugs ORDER BY rowid').fetchall()
        return [{'id':r['id'],**json.loads(r['data_json'])} for r in rows]

    def resolve(self, name):
        matches = [m for m in self.medications() if name==m['id'] or key(name)==key(m['name']) or key(name)==key(m['name']+' '+m['strength'])]
        if not matches: raise ValueError('MEDICATION_NOT_FOUND')
        if len(matches)!=1: raise ValueError('MEDICATION_AMBIGUOUS')
        return matches[0]

    def record(self, medication_id, quantity, taken_at, source_id, *, now=None):
        med = self.resolve(medication_id)
        quantity = str(number(quantity)); at = aware(taken_at)
        now = aware(now or datetime.now(timezone.utc))
        if at > now+timedelta(minutes=1): raise ValueError('MEDICATION_FUTURE_TIME')
        digest = hashlib.sha256(json.dumps([med['id'],quantity,at.isoformat()]).encode()).hexdigest()
        with closing(self.db.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM medication_intakes WHERE source_id=?',(source_id,)).fetchone()
            if row:
                if row['request_hash']!=digest: raise ValueError('MEDICATION_SOURCE_CONFLICT')
                return dict(row)
            ident = uuid4().hex
            con.execute('INSERT INTO medication_intakes VALUES(?,?,?,?,?,?,0,?)',
                        (ident,med['id'],quantity,at.timestamp(),source_id,digest,now.timestamp()))
            return dict(con.execute('SELECT * FROM medication_intakes WHERE id=?',(ident,)).fetchone())

    def void(self, ident):
        with closing(self.db.connect()) as con, con:
            row = con.execute('SELECT * FROM medication_intakes WHERE id=?',(ident,)).fetchone()
            if not row: raise ValueError('MEDICATION_RECORD_NOT_FOUND')
            con.execute('UPDATE medication_intakes SET voided=1 WHERE id=?',(ident,))
        return dict(row)

    def amend(self, ident, quantity, taken_at, *, now=None):
        quantity=str(number(quantity)); at=aware(taken_at); now=aware(now or datetime.now(timezone.utc))
        if at>now+timedelta(minutes=1): raise ValueError('MEDICATION_FUTURE_TIME')
        with closing(self.db.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('SELECT * FROM medication_intakes WHERE id=? AND voided=0',(ident,)).fetchone()
            if not row: raise ValueError('MEDICATION_RECORD_NOT_FOUND')
            con.execute('INSERT INTO medication_intake_audit VALUES(?,?,?,?)',
                        (uuid4().hex,ident,json.dumps(dict(row)),now.timestamp()))
            con.execute('UPDATE medication_intakes SET quantity=?,taken_at=? WHERE id=?',(quantity,at.timestamp(),ident))
        return ident

    def delete_all(self):
        with closing(self.db.connect()) as con, con:
            for table in ['medication_intake_audit','medication_intakes','medication_drugs','medication_chat_states','medication_chat_turns']:
                con.execute('DELETE FROM '+table)

    def summary(self, now, zone):
        now=aware(now); tz=ZoneInfo(zone); first=now.astimezone(tz).date()-timedelta(days=29)
        with closing(self.db.connect()) as con, con:
            rows=[dict(r) for r in con.execute('SELECT * FROM medication_intakes WHERE voided=0 AND taken_at<=? ORDER BY taken_at',(now.timestamp(),))]
        reports=[]; alerts=[]; ingredients={}; frequency={}
        medications=self.medications()
        for med in medications:
            same_ids={m['id'] for m in medications if physical_key(m)==physical_key(med)}
            all_rows=[r for r in rows if r['medication_id'] in same_ids]
            current=[r for r in all_rows if r['medication_id']==med['id']]
            current=[r for r in current if datetime.fromtimestamp(r['taken_at'],tz).date()>=first]
            if not current: continue
            codes=set()
            for r in current:
                if number(r['quantity'])>number(med['dose']): codes.add('SINGLE_DOSE')
                if med['max_per_24h']:
                    total=sum((number(a['quantity']) for a in all_rows if r['taken_at']-86400<a['taken_at']<=r['taken_at']),Decimal(0))
                    if total>number(med['max_per_24h']): codes.add('ROLLING_24H_LIMIT')
                prior=[a for a in all_rows if a['id']!=r['id'] and a['taken_at']<=r['taken_at']]
                if med['min_interval_hours'] and prior and r['taken_at']-prior[-1]['taken_at']<float(number(med['min_interval_hours']))*3600:
                    codes.add('INTERVAL')
            alerts.extend({'code':code,'medication_id':med['id'],'name':med['name'],'strength':med['strength']} for code in sorted(codes))
            if med['ingredient'] and any(now.timestamp()-86400<r['taken_at'] for r in current):
                ingredients.setdefault(key(med['ingredient']),{})[physical_key(med)]=med['id']
            from .medication_rules import INGREDIENT_CLASSES, PURPOSES
            group=INGREDIENT_CLASSES.get(key(med['ingredient'])) if key(med.get('purpose','')) in PURPOSES else None
            if group:
                frequency.setdefault(group,set()).update(datetime.fromtimestamp(r['taken_at'],tz).date() for r in current)
            reports.append({**med,'count':len(current),'days':len({datetime.fromtimestamp(r['taken_at'],tz).date() for r in current}),
                'total':str(sum((number(r['quantity']) for r in current),Decimal(0))),
                'limit_status':'known' if med['max_per_24h'] else 'unknown','frequency_status':'enabled' if group else 'unconfirmed','records':current})
        for ids in ingredients.values():
            if len(ids)>1: alerts.append({'code':'SAME_INGREDIENT','medication_ids':list(ids.values())})
        from .medication_rules import THRESHOLDS, NONOPIOID_SOURCE, TRIPTAN_SOURCE, BASE
        for group,days in frequency.items():
            if len(days)>=THRESHOLDS[group]:
                alerts.append({'code':'FREQUENT_HEADACHE_USE','group':group,'days':len(days),
                    'source':NONOPIOID_SOURCE if group=='nonopioid' else TRIPTAN_SOURCE})
        if len(frequency)>1 and all(len(days)<THRESHOLDS[group] for group,days in frequency.items()):
            days=set().union(*frequency.values())
            if len(days)>=10:alerts.append({'code':'FREQUENT_HEADACHE_USE','group':'mixed','days':len(days),
                'source':BASE+'8-2-6-medication-overuse-headache-attributed-to-multiple-drug-classes-not-individually-overused/'})
        return {'from':first.isoformat(),'to':now.astimezone(tz).date().isoformat(),'zone':zone,'medications':reports,'alerts':alerts,
                'note':'記録された服用のみの集計です。記録漏れや併用薬の影響は判断できません。上限未確認の薬の過量判定はできません。'}
