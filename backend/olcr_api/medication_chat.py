"""Medication management in ordinary chat. All extraction is an unconfirmed draft."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import threading
import time
import unicodedata
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .medication_records import MedicationRecords, validate_drug, aware

_LOCK = threading.RLock()
_TURN_LOCKS = {}
_FIELDS = {'薬名':'name','お薬の名前':'name','規格':'strength','成分':'ingredient','用途':'purpose','1回量':'dose_text',
    '用法':'instructions','服用方法':'instructions','用法・用量':'instructions','用法及び用量':'instructions',
    '24時間上限':'max_text','最小間隔':'interval_text','開始':'start_date','終了':'end_date','時刻':'times',
    '接続先':'provider','カレンダー':'calendar_id','通知':'reminder','薬名を予定に表示':'reveal'}
_LABELS = {'name':'薬名','strength':'規格','dose':'1回量','unit':'単位','instructions':'用法'}
_TAKE = re.compile(r'^(?P<when>今|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)\s*[、, ]*(?P<name>.+?)を(?:(?P<quantity>\d+(?:\.\d+)?)\s*(?P<unit>[^\d\s]+?))?(?:飲んだ|服用した|使った)[。.!！]?$')
_EXCLUDE = re.compile(r'実装|\bAPI\b|コード|プログラム|小説|翻訳|母(?:が|の)|父(?:が|の)|子供|子ども|家族|患者|友人|\bimplement\b|\bcode\b',re.I)


def may_handle(message):
    if _EXCLUDE.search(message):return False
    text=unicodedata.normalize('NFKC',message).strip()
    return bool(_TAKE.fullmatch(text) or fields(text) or text in {'キャンセル','中止'} or re.search(
        r'服薬|服用(?:記録|予定)|薬を登録|登録した薬|薬一覧|薬の管理|(?:処方箋|薬袋|お薬の説明).*(?:登録|予定|管理)',text))


def fields(text):
    result={}
    for line in text.splitlines():
        match=re.match(r'^\s*([^:：]+)[:：]\s*(.*?)\s*$',line)
        if match and match[1].strip() in _FIELDS: result[_FIELDS[match[1].strip()]]=match[2]
    return result


def _record_id(value):
    ident=value.strip()
    if len(ident)>=2 and ident[0]=='[' and ident[-1]==']': ident=ident[1:-1].strip()
    return ident if re.fullmatch(r'[a-fA-F0-9]{32}',ident) else None


def _record_not_found(ident):
    return f'服用記録ID「{ident}」は登録されていません。正しいIDは「服薬履歴を見せて」で確認できます。'


def prescription_fields(text):
    """Read labelled pharmacy text; never mix directions from multiple medicines."""
    result={}; waiting=None; names=0
    for line in unicodedata.normalize('NFKC',text).splitlines():
        line=line.strip()
        match=re.match(r'^([^:]+):\s*(.*)$',line)
        label=match[1].strip() if match else line
        field=_FIELDS.get(label)
        if field:
            if field=='name': names+=1
            if names>1: raise ValueError('MEDICATION_MULTIPLE_PRESCRIPTIONS')
            value=match[2].strip() if match else ''
            if value: result[field]=value; waiting=None
            else: waiting=field
        elif waiting and line:
            result[waiting]=line; waiting=None
    if not result.get('name'):
        candidates=re.findall(r'^([^\n:]{1,100}?(?:錠|カプセル|細粒|散)[^\n]*?\d+(?:\.\d+)?\s*(?:mg|μg|g|%))\s*$',text,re.M)
        if len(candidates)>1: raise ValueError('MEDICATION_MULTIPLE_PRESCRIPTIONS')
        if candidates: result['name']=candidates[0].strip()
    if result.get('name') and not result.get('strength'):
        strength=re.search(r'(\d+(?:\.\d+)?\s*(?:mg|μg|g|%))',result['name'])
        if strength: result['strength']=strength[1]
    if not result.get('dose_text'):
        dose=re.search(r'1回\s*(\d+(?:\.\d+)?\s*(?:錠|包|カプセル|mL|mg))',text)
        if dose: result['dose_text']=dose[1]
    if not result.get('instructions'):
        directions=re.search(r'^.*1日\s*\d+\s*回[^\n]*$',text,re.M)
        if directions: result['instructions']=directions[0].strip()
    return result


def quantity_text(text):
    match=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([^\d\s]+)\s*',unicodedata.normalize('NFKC',text))
    if not match: raise ValueError('1回量や上限は「1錠」「5mL」のように量と単位を指定してください。')
    return match[1],match[2]


def drug_from_fields(data):
    drug={k:data[k] for k in ['name','strength','ingredient','instructions','purpose'] if k in data}
    if data.get('dose_text'): drug['dose'],drug['unit']=quantity_text(data['dose_text'])
    if data.get('max_text'):
        maximum,unit=quantity_text(data['max_text'])
        if unit!=drug.get('unit'): raise ValueError('24時間上限の単位を1回量と同じ単位で確認してください。自動換算しません。')
        drug['max_per_24h']=maximum
    if data.get('interval_text'):
        match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*時間(?:以上)?',data['interval_text'])
        if not match: raise ValueError('最小間隔は処方に明記された「6時間」のように指定してください。')
        drug['min_interval_hours']=match[1]
    return validate_drug(drug)


def draft_text(data, source=''):
    lines=['処方の読み取り案です。原本と照合してください（まだ登録していません）。']
    for field,label in [('name','薬名'),('strength','規格'),('dose_text','1回量'),('instructions','用法'),('ingredient','成分'),('purpose','用途'),('max_text','24時間上限'),('interval_text','最小間隔')]:
        lines.append(f'{label}：{data.get(field) or "未確認"}')
    lines.append('不足や誤読は「規格：5mg」のように訂正してください。全て確認できたら「服薬を確定」、やめる場合は「キャンセル」。')
    lines.append('記録はこのMacのOLCRプロファイルに保存し、他の会話からも参照できます。処方された量や間隔をAIが補完・変更することはありません。')
    if source: lines.append('画像の文字（未確認）：\n'+source[:4000])
    return '\n'.join(lines)


def render_summary(report):
    lines=[f"服薬記録：{report['from']}〜{report['to']}（{report['zone']}）"]
    for med in report['medications']:
        lines.append(f"{med['name']} / {med['strength']}：{med['count']}回、{med['days']}日、計{med['total']}{med['unit']}（薬ID：{med['id']}）")
        if med.get('frequency_status')!='enabled':lines.append('使用日数による月間判定：成分別基準または用途が未確認。日数だけで飲み過ぎとは判定しません。')
        if med['limit_status']=='unknown': lines.append('24時間上限は未確認のため、過量かどうかは判断できません。')
        for record in med['records'][-50:]:
            at=datetime.fromtimestamp(record['taken_at'],ZoneInfo(report['zone'])).strftime('%Y-%m-%d %H:%M')
            lines.append(f"  {at} {record['quantity']}{med['unit']} / 記録ID：{record['id']}")
    messages={'SINGLE_DOSE':'確認済みの1回量を超える服用記録があります。',
              'ROLLING_24H_LIMIT':'確認済みの24時間上限を超える服用記録があります。',
              'INTERVAL':'確認済みの最小服用間隔より短い記録があります。',
              'SAME_INGREDIENT':'直近24時間に同じ成分として登録した別の薬の服用記録があります。併用について薬剤師に確認してください。'}
    for alert in report['alerts']:
        if alert['code']=='FREQUENT_HEADACHE_USE':
            lines.append(f"注意：確認済み成分の頭痛時使用が直近30日に{alert['days']}日あります。使用頻度について医師・薬剤師へ相談してください。1か月の記録から診断するものではありません。[基準の出典]({alert['source']})")
            continue
        lines.append('注意：'+(alert.get('name','')+' '+alert.get('strength','')).strip()+' '+messages[alert['code']])
    if report['alerts']: lines.append('処方内容と記録を確認し、医師・薬剤師へ相談してください。過量服用した可能性や体調の異常がある場合は速やかに医療機関へ相談してください。')
    if not report['medications']: lines.append('この期間の服用実績はまだありません。')
    lines.append(report['note'])
    return '\n'.join(lines)


def _ocr(value):
    if not getattr(value,'image',None): return ''
    from .runtime import _validated_ocr_text
    texts=_validated_ocr_text(value.image)
    return '\n'.join(texts or [])


def _turn(store, message, state, source_id, zone, calendar, cid, image_text=''):
    now=datetime.now(timezone.utc); text=unicodedata.normalize('NFKC',message).strip(); data=fields(text)
    if state and state['stage']=='rewound':
        return ('会話編集前の服用記録とカレンダー予定は保持されています。重複を防ぐため、この再送では追加しません。'
                '「服薬履歴を見せて」で確認し、記録IDを指定して訂正・取り消しをしてください。'),None
    if state and text in {'キャンセル','中止','服薬管理を中止'}:
        if state['stage']=='plan':
            calendar.cancel_pending(state['id'],cid)
            return '未登録の服用予定を取り消しました。登録済み・結果不明の予定は保持しています。',None
        return '服薬操作を取り消しました。',None
    if state and state['stage']=='drug' and (data or text=='服薬を確定'):
        draft={**state['data'],**data}
        if text!='服薬を確定': return draft_text(draft),{**state,'data':draft}
        try: med=store.register(drug_from_fields(draft),source_id)
        except ValueError as exc:
            missing=str(exc).removeprefix('MEDICATION_REQUIRED:')
            return ('内容が不足または不正です。'+_LABELS.get(missing, '量・単位・上限・間隔')+'を原本で確認し、項目名付きで教えてください。'),{**state,'data':draft}
        if state.get('remaining'):
            next_draft,*remaining=state['remaining']
            return f"登録しました：{med['name']} / {med['strength']}。次の薬を確認してください。\n"+draft_text(next_draft),{'stage':'drug','data':next_draft,'remaining':remaining}
        return (f"登録しました：{med['name']} / {med['strength']}、1回{med['dose']}{med['unit']}。薬ID：{med['id']}\n"
                '服用後は「今、薬名を1錠飲んだ」のように、実際の量を伝えてください。予定は「服用予定を作成」と開始・終了日・時刻を指定できます。'),None
    if state and state['stage']=='plan' and text=='服用予定を確定':
        result=calendar.confirm(state['id'],cid)
        remaining=result['counts'].get('pending',0)
        return (f"カレンダー登録：完了{result['counts'].get('completed',0)}件、未登録{remaining}件、不明{result['counts'].get('uncertain',0)}件、失敗{result['counts'].get('failed',0)}件。"
                +(' 続きを登録するには「服用予定を確定」と返信してください。' if remaining else '')
                +(' 不明な予定は重複作成せず、接続回復後に同じ操作で照合します。' if result['counts'].get('uncertain') else '')),state if remaining or result['counts'].get('uncertain') else None
    if text in {'服薬履歴','服薬履歴を見せて','服薬記録を見せて','薬の記録を見せて','服薬の飲み過ぎを確認'}:
        return render_summary(store.summary(now,zone)),None
    if text in {'服用予定一覧','服用予定を見せて'}:
        with closing(store.db.connect()) as con:
            rows=con.execute('SELECT * FROM medication_calendar_plans WHERE conversation_id=?',(cid,)).fetchall()
        plans=[json.loads(r['data_json']) for r in rows]
        return '\n'.join(f"{p['start_date']}〜{p['end_date']} / {p['provider']} / {p['count']}件 / 予定ID：{p['id']}" for p in plans) or 'この会話の服用予定はありません。',None
    if text in {'登録した薬を見せて','薬一覧','服薬一覧'}:
        meds=store.medications()
        return '\n'.join(f"{m['name']} / {m['strength']} / {m['instructions']} / 薬ID：{m['id']}" for m in meds) or '登録済みの薬はありません。',None
    match=re.fullmatch(r'服用記録を取り消し\s+(.+)',text)
    if match:
        ident=_record_id(match[1])
        if not ident: return _record_not_found(match[1].strip()),None
        try: store.void(ident)
        except ValueError as exc:
            if 'MEDICATION_RECORD_NOT_FOUND' in str(exc): return _record_not_found(ident),None
            raise
        return '指定した服用記録を取り消しました。\n'+render_summary(store.summary(now,zone)),None
    match=re.fullmatch(r'服用記録を訂正\s+(.+?)\s+(\d+(?:\.\d+)?)\s+(.+)',text)
    if match:
        ident=_record_id(match[1])
        if not ident: return _record_not_found(match[1].strip()),None
        try: store.amend(ident,match[2],aware(match[3]))
        except ValueError as exc:
            if 'MEDICATION_RECORD_NOT_FOUND' in str(exc): return _record_not_found(ident),None
            raise
        return '指定した記録を訂正しました。\n'+render_summary(store.summary(now,zone)),None
    if text=='服薬データを全削除':
        return '薬・服用実績・管理用状態を削除します。カレンダー予定と会話履歴は別に残ります。「服薬データの全削除を確定」で実行します。',{'stage':'delete'}
    if state and state['stage']=='delete' and text=='服薬データの全削除を確定':
        store.delete_all(); return 'ローカルの薬と服用実績を削除しました。カレンダー予定と会話履歴はそれぞれの削除操作で管理できます。',None
    if state and state['stage']=='stop' and text=='服用予定の停止を確定':
        result=calendar.stop(state['id'],cid)
        return f"今後の予定停止：削除{result['deleted']}件、失敗または変更検出{result['failed']}件。服用実績は保持します。",None
    if text.startswith('服用予定を再開 '):
        plan_id=text.split(' ',1)[1].strip(); calendar._load(plan_id,cid)
        return '指定した予定を確認・再開します。「服用予定を確定」で実行します。予定ID：'+plan_id,{'stage':'plan','id':plan_id}
    if text.startswith('服用予定を停止 '):
        plan_id=text.split(' ',1)[1].strip()
        calendar._load(plan_id,cid)
        return '予定ID '+plan_id+' の今後のOLCR服用予定を削除します。「服用予定の停止を確定」で実行、「キャンセル」で戻ります。',{'stage':'stop','id':plan_id}
    if text.startswith('服用予定を作成') or (state and state['stage']=='schedule' and data):
        draft={**(state.get('data',{}) if state else {}),**data}
        required=['name','start_date','end_date','times','provider']
        missing=[f for f in required if not draft.get(f)]
        if missing:
            return '予定に必要な項目を確認してください：薬名（または薬ID）、開始：YYYY-MM-DD、終了：YYYY-MM-DD、時刻：08:00,20:00、接続先：Apple または Google。頓服は定期予定にしません。',{'stage':'schedule','data':draft}
        med=store.resolve(draft['name'])
        provider={'Apple':'apple','apple':'apple','Google':'google','google':'google'}.get(draft['provider'])
        reminder=None if draft.get('reminder','なし')=='なし' else int(draft['reminder'].removesuffix('分'))
        plan=calendar.prepare({'medication_id':med['id'],'start_date':draft['start_date'],'end_date':draft['end_date'],
            'times':re.split(r'[,、\s]+',draft['times']), 'zone':zone,'provider':provider,'calendar_id':draft.get('calendar_id','primary'),
            'reminder':reminder,'reveal':draft.get('reveal','いいえ')=='はい'},cid)
        return (f"服用予定の確認：{med['name']} / {med['strength']}、1回{med['dose']}{med['unit']}\n{plan['start_date']}〜{plan['end_date']}、時刻 {', '.join(plan['times'])}（{zone}）、{plan['count']}件、接続先 {provider}。\n"
                f"通知：{reminder if reminder is not None else 'なし'}。予定名に薬名：{'表示' if plan['reveal'] else '非表示'}。\n"
                '薬名を表示する場合はカレンダーの提供元・共有相手・通知画面にも表示され得ます。\n「服用予定を確定」で登録、「キャンセル」で取り消し。予定ID：'+plan['id']),{'stage':'plan','id':plan['id']}
    taken=_TAKE.fullmatch(text)
    if taken:
        if not taken['quantity']: return '実際に飲んだ量と単位を「今、薬名を1錠飲んだ」のように教えてください。',None
        try: med=store.resolve(taken['name'])
        except ValueError as exc:
            if 'AMBIGUOUS' in str(exc): return '同じ薬名の規格・用法が複数あります。「今、薬IDを1錠飲んだ」のように指定してください。\n'+ '\n'.join(f"{m['name']} {m['strength']} {m['instructions']}：{m['id']}" for m in store.medications()),None
            return 'この薬はまだ登録されていません。処方箋や説明を「服薬を登録」と一緒に送り、薬名・規格・1回量・用法を確認してください。',None
        if taken['unit']!=med['unit']: return f"登録単位は{med['unit']}です。自動換算できないため、実際の量と単位を確認してください。",None
        at=now if taken['when']=='今' else datetime.fromisoformat(taken['when'].replace('Z','+00:00'))
        if at.tzinfo is None: at=at.replace(tzinfo=ZoneInfo(zone))
        result=store.record(med['id'],taken['quantity'],at,source_id,now=now)
        return f"記録しました：{med['name']} / {med['strength']}、{taken['quantity']}{med['unit']}（記録ID：{result['id']}）。\n"+render_summary(store.summary(now,zone)),None
    if re.search(r'服薬を登録|薬を登録|(?:処方箋|薬袋|お薬の説明).*(?:登録|予定|管理)',text):
        raw=text+'\n'+image_text
        segments=re.split(r'(?m)(?=^\s*(?:薬名|お薬の名前)\s*[:：]?)',raw)
        segments=[segment for segment in segments if prescription_fields(segment).get('name')]
        drafts=[prescription_fields(segment) for segment in segments] if segments else [prescription_fields(raw)]
        draft=drafts[0]
        # Unlabelled pharmacy text is evidence only; never silently attach one drug's directions to another.
        return draft_text(draft,image_text),{'stage':'drug','data':draft,'remaining':drafts[1:]}
    if text in {'服薬管理','薬の管理をしたい'}:
        return '「服薬を登録」と薬名・規格・1回量・用法を送ってください。処方箋や薬袋の画像も添付できます。薬ごとに原本を確認してから登録します。',None
    return None


def respond_in_chat(db,value,resolve_context,calendar_service):
    if getattr(value,'resume_control',False) or getattr(value,'resume_task_id',None): return None
    message=value.message
    if not may_handle(message): return None
    existing=db.conversation(value.conversation_id) if value.conversation_id else None
    if existing and existing.get('custom_agent'): return None
    cid=getattr(value,'conversation_id',None)
    store=MedicationRecords(db)
    with _LOCK:
        turn_lock=_TURN_LOCKS.setdefault((db.path,cid),threading.RLock())
    with turn_lock:
        with closing(db.connect()) as con:
            row=con.execute('SELECT data_json FROM medication_chat_states WHERE conversation_id=? AND expires_at>?',(cid,time.time())).fetchone()
        state=json.loads(row[0]) if row else None
        matches=bool(_TAKE.fullmatch(unicodedata.normalize('NFKC',message).strip()))
        if not state and not matches and not re.search(r'服薬|服用(?:記録|予定)|薬を登録|登録した薬|薬一覧|薬の管理|(?:処方箋|薬袋|お薬の説明).*(?:登録|予定|管理)',message): return None
        cid,_,_=resolve_context(value)
        zone=getattr(value,'client_time_zone',None) or 'Asia/Tokyo'
        try: ZoneInfo(zone)
        except (ZoneInfoNotFoundError,ValueError): zone='Asia/Tokyo'
        source_id=cid+':'+(getattr(value,'message_id',None) or uuid4().hex)
        digest=sha256(json.dumps([message,getattr(value,'image',None)],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with closing(db.connect()) as con:
            cached=con.execute('SELECT * FROM medication_chat_turns WHERE source_id=?',(source_id,)).fetchone()
        if cached:
            if cached['request_hash']!=digest: return {'conversation_id':cid,'response':'この送信IDは処理済みです。変更内容は新しいメッセージで送ってください。','blocks':[],'sources':[]}
            return json.loads(cached['response_json'])
        try:
            input_message=message
            if getattr(value,'image',None) and re.search(r'処方箋|薬袋|服薬|服用予定|薬を登録|お薬の説明',message):
                input_message='服薬を登録\n'+message
                state=None
            turn=_turn(store,input_message,state,source_id,zone,calendar_service,cid,_ocr(value))
        except (ValueError,TypeError,KeyError) as exc:
            code=str(exc)
            errors={'MEDICATION_PLAN_EXPIRED':'予定案の確認期限が切れました。開始・終了日・時刻を確認して作り直してください。','MEDICATION_PAST_TIME':'過去の時刻を含む服用予定は登録できません。開始日・時刻を確認してください。','CALENDAR_PROVIDER_CHANGED':'カレンダーの接続先が変わりました。予定案を確認し直してください。','CALENDAR_PERMISSION_DISABLED':'設定のサービス接続でカレンダーカテゴリをオンにしてください。','CONNECTOR_PERMISSION_DENIED':'設定のサービス接続でカテゴリとGoogleカレンダーをオンにしてください。','MEDICATION_SCHEDULE_EXISTS':'同じ薬の同時刻の予定が既にあります。「服用予定一覧」で既存の予定IDを確認してください。','MEDICATION_MULTIPLE_PRESCRIPTIONS':'複数の薬が含まれています。用法を取り違えないよう、薬ごとに画像を切り出すか薬名・規格・1回量・用法を1種類ずつ送ってください。','MEDICATION_NOT_FOUND':'登録した薬が見つかりません。薬名・規格または薬IDを確認してください。',
                'MEDICATION_AMBIGUOUS':'同じ薬名が複数あります。薬IDで指定してください。','MEDICATION_FUTURE_TIME':'未来の服用実績は記録できません。',
                'MEDICATION_RECORD_NOT_FOUND':'指定した服用記録が見つかりません。','MEDICATION_PRN_NOT_SCHEDULED':'頓服は定期の服用予定に登録できません。実際に服用したときに記録してください。','MEDICATION_PRESCRIPTION_CONFLICT':'この時刻では確認済みの服用上限または最小間隔に反します。処方と登録時刻を確認してください。','GOOGLE_CALENDAR_WRITE_CONSENT_REQUIRED':'設定のサービス接続でGoogleカレンダーの予定登録を許可してから再試行してください。','MEDICATION_TIME_INVALID':'実服用時刻をタイムゾーン付きで確認してください。'}
            turn=(errors.get(code,'内容・単位・日時・接続権限を確認してください。確認できない予定や実績は登録完了として扱いません。'),state)
        if turn is None: return None
        response,next_state=turn
        result={'conversation_id':cid,'response':response,'blocks':[],'sources':[]}
        with closing(db.connect()) as con,con:
            con.execute('INSERT OR REPLACE INTO medication_chat_states VALUES(?,?,?)',(cid,json.dumps(next_state,ensure_ascii=False),time.time()+1800)) if next_state else con.execute('DELETE FROM medication_chat_states WHERE conversation_id=?',(cid,))
            con.execute('INSERT INTO medication_chat_turns VALUES(?,?,?)',(source_id,digest,json.dumps(result,ensure_ascii=False)))
        current=db.conversation(cid)
        if not any(m.get('id')==getattr(value,'message_id',None) for m in (current or {}).get('messages',[])):
            db.add_message(cid,'user',message,time.time(),getattr(value,'message_id',None) or uuid4().hex,blocks=[{'type':'medication_management'}])
        db.add_message(cid,'assistant',response,time.time(),uuid4().hex,blocks=[{'type':'medication_management'}])
        return result


def events(result):
    for event in [{'type':'meta','conversation_id':result['conversation_id'],'task_id':None},
                  {'type':'chunk','text':result['response']},
                  {'type':'done','conversation_id':result['conversation_id'],'response_owner':'MEDICATION_MANAGEMENT'}]:
        yield 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
