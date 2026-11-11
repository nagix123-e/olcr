"""Conservative, typed continuation of a recent chat action."""
import re
import unicodedata

NUMBER=r'[0-9零〇一二三四五六七八九十百千]+'
CLOCK=rf'(?:{NUMBER}時(?!間)|\d{{1,2}}:\d{{2}}|\d{{1,2}}\s*(?:am|pm)\b|\bat\s+\d{{1,2}}\b)'
DAY=r'今日|明日|明後日|\d{1,4}[年/月-]\d{1,2}|today|tomorrow'
SOFTWARE=re.compile(r'実装|コーディング|コード|ソース|バグ|アプリ|プロジェクト|ファイル|関数|リファクタ|\b(?:implement|coding|code|source|bug|app|project|file|function|refactor)\b',re.I)

def clean(text):
    text=unicodedata.normalize('NFKC',text).strip().rstrip('。.!！?？')
    text=re.sub(r'(?:です|だよ|だって|ね)$','',text)
    text=re.sub(r'^.*?(?:じゃなく(?:て)?|ではなく(?:て)?)(.+)$',r'\1',text)
    return re.sub(r'^(?:いや[、, ]*|違う[、, ]*|じゃなくて[、, ]*|訂正[、, :]*|あ[、, ]+|actually[, ]+|no[, ]+)', '',text,flags=re.I)

def fields(text):
    text=clean(text)
    bare=re.sub(r'[「『"].*?[」』"]','',text)
    if not text or len(text)>300 or SOFTWARE.search(bare):return None
    quoted=re.search(r'[「『"](.+?)[」』"]',text)
    if quoted and re.search(r'名前|名称|タイトル|name|title',text,re.I):return {'field':'name','name':quoted[1]}
    named=re.fullmatch(r'(?:名前|名称|タイトル)(?:は|を|:| )?(.+?)(?:にして|に変更して|に変えて|でお願い)?',text)
    if named:return {'field':'name','name':named[1].strip()}
    if re.fullmatch(r'(?:[一二三四五0-9]+(?:番目|つ目|番)|(?:the )?(?:first|second|third)(?: one)?)(?:を|に|で|の|にして|を再生して|を使って|お願い|して|に変更)*',text,re.I):return {'field':'selection'}
    rest=re.sub(rf'{CLOCK}|{DAY}|{NUMBER}(?:時間|分間?|秒間?)|\b\d+\s*(?:hours?|minutes?|seconds?)\b|午前|午後|夕方|朝|夜|半|\b(?:am|pm|at|on|hours?|minutes?|seconds?)\b','',text,flags=re.I)
    rest=re.sub(r'アラーム|タイマー|カレンダー|予定|それ|これ|\b(?:alarm|timer|calendar)\b|に変更して|に訂正して|してください|下さい|ください|して|変更して|変えて|ずらして|長くして|短くして|早くして|遅くして|追加して|延長して|にして|に変更|に訂正|設定し直して|設定して|お願い(?:します)?|の方|のほう|あと|後ろ|前倒し|早めて|遅らせて|後|前|追加|延長|短縮|\b(?:change|it|to|instead|please|more|less|later|earlier)\b','',rest,flags=re.I)
    rest=re.sub(r'[\s、,.:/\-のにをへからまで]','',rest)
    if rest:return None
    if re.search(CLOCK,text,re.I):return {'field':'clock','text':text}
    if re.search(rf'{NUMBER}(?:時間|分間?|秒間?)|\b\d+\s*(?:hours?|minutes?|seconds?)\b',text,re.I):return {'field':'duration','text':text}
    if re.search(DAY,text,re.I):return {'field':'day','text':text}
    return None

def previous_action(messages):
    # Do not carry an action across an unrelated user turn. Permit recovery
    # from a prior misrouted short correction with no action block in its reply.
    for message in reversed(messages[-10:]):
        if message.get('role')=='assistant':
            block=next((b for b in reversed(message.get('blocks') or []) if b.get('type')=='assistant_action'),None)
            if block:return block
        elif message.get('role')=='user' and not fields(message.get('content','')):return None
    return None

def followup_intent(text,previous):
    if not previous:return None
    generic=bool(re.fullmatch(r'違う|いや|それじゃない|変更して|修正して|やり直して|設定し直して|not that|change it|fix that',unicodedata.normalize('NFKC',text).strip().rstrip('。.!！?？'),re.I))
    text=clean(text)
    if SOFTWARE.search(re.sub(r'[「『"].*?[」』"]','',text)):return None
    pending=previous.get('followup') if previous.get('kind')=='clarification' else None
    timer_items=previous.get('timers') or []
    service=pending.get('service') if pending else timer_items[0].get('kind','timer') if timer_items else 'calendar' if previous.get('kind')=='calendar_action' else 'youtube' if previous.get('kind')=='media' else None
    explicit='alarm' if re.search(r'アラーム|\balarm\b',text,re.I) else 'timer' if re.search(r'タイマー|\btimer\b',text,re.I) else 'calendar' if re.search(r'カレンダー|\bcalendar\b',text,re.I) else None
    if explicit and service!=explicit:return None
    if pending and re.fullmatch(r'やめて|取り消して|キャンセル|cancel|never mind',text,re.I):return {'service':pending['service'],'text':text,'followup':{**pending,'cancel':True}}
    query=re.fullmatch(r'何時(?:に(?:した|設定した|なってる|鳴る)(?:の|っけ|か)?)?|いつ(?:鳴る|だっけ)?|残り(?:は|時間)?|あと(?:どれくらい|何分|何秒)|what time(?: is it set for)?|how (?:long|much time) is left',text,re.I)
    controls=re.fullmatch(r'一時停止(?:して)?|停止(?:して)?|止めて|再開(?:して)?|pause|stop|cancel|resume',text,re.I)
    ids=pending.get('target_ids') if pending else [item['id'] for item in timer_items]
    if query and ids:return {'service':service,'text':text,'followup':{'service':service,'operation':'timer_control','target_ids':ids,'control':'status'}}
    if controls and ids:
        control='pause' if re.search(r'一時停止|pause',text,re.I) else 'resume' if re.search(r'再開|resume',text,re.I) else 'cancel'
        return {'service':service,'text':text,'followup':{'service':service,'operation':'timer_control','target_ids':ids,'control':control}}
    if generic:
        if ids:return {'service':service,'text':text,'followup':{'service':service,'operation':'timer_edit','target_ids':ids,'field':'unknown'}}
        if previous.get('kind')=='calendar_action' and previous.get('action',{}).get('status')=='pending':
            action=previous['action']
            return {'service':'calendar','text':text,'followup':{'service':'calendar','operation':'calendar_edit','target_id':action['id'],'revision':action['revision'],'field':'unknown'}}
    if pending:
        if re.fullmatch(r'やめて|取り消して|キャンセル|cancel|never mind',text,re.I):return {'service':pending['service'],'text':text,'followup':{**pending,'cancel':True}}
        if re.fullmatch(r'はい|それで|お願い|うん|yes|confirm|go ahead|午前(?:の方|のほう)?|午後(?:の方|のほう)?|am|pm|[一二三四五0-9]+(?:番目|つ目|番)|first|second',text,re.I):return {'service':pending['service'],'text':text,'followup':{**pending,'reply':text}}
    change=fields(text)
    if not change:return None
    if pending:
        updated={**pending,**change,'reply':None}
        original=pending.get('text','')
        clock=re.search(r'(?:(?:午前|午後|朝|夕方|夜)\s*)?(?:[0-9零〇一二三四五六七八九十]+時(?:[0-9零〇一二三四五六七八九十]+分|半)?|\d{1,2}:\d{2})(?:\s*(?:am|pm))?|\bat\s+\d{1,2}(?:\s*(?:am|pm))?',original,re.I)
        day=re.search(r'(?:\d{4}[年/-])?\d{1,2}[月/-]\d{1,2}日?|明後日|明日|今日|today|tomorrow',original,re.I)
        if change['field']=='day' and clock:updated.update(field='clock',text=text+' '+clock[0])
        elif change['field']=='clock' and day and not re.search(DAY,text,re.I):updated['text']=day[0]+' '+text
        for key in ('choices','question','chosen','expected'):updated.pop(key,None)
        return {'service':pending['service'],'text':text,'followup':updated}
    if previous.get('kind')=='timers':
        timers=previous.get('timers') or []
        if not timers:return None
        service=timers[0].get('kind','timer')
        return {'service':service,'text':text,'followup':{'service':service,'operation':'timer_edit','target_ids':[item['id'] for item in timers],**change}}
    if previous.get('kind')=='calendar_action' and (previous.get('action') or {}).get('status')=='pending' and change['field']!='selection':
        action=previous['action']
        return {'service':'calendar','text':text,'followup':{'service':'calendar','operation':'calendar_edit','target_id':action['id'],'revision':action['revision'],**change}}
    if previous.get('kind')=='media' and change['field']=='selection':
        return {'service':'youtube','text':text,'followup':{'service':'youtube','operation':'media_select','candidates':previous.get('candidates') or [],**change}}
    return None
