"""Shared conversation transport for personal workflows; existing services own execution."""
from __future__ import annotations
from contextlib import closing
from hashlib import sha256
import json
import re
import threading
import time
import weakref
from uuid import uuid4
from .generation_control import check_cancelled

_LOCK=threading.RLock()
_LOCKS=weakref.WeakValueDictionary()
_CONFIRM=re.compile(r'^(?:confirm|はい|お願い(?:します)?|実行して|確定|登録して|保存して|この内容で(?:登録|保存|実行|お願い)(?:して|します)?)[。.!！]*$',re.I)
_CANCEL=re.compile(r'^(?:cancel|キャンセル|やめて|中止|取り消して)[。.!！]*$',re.I)
_EXCLUDE=re.compile(r'実装|コーディング|コードを書|機能を作|アプリを作|プログラム|翻訳|\b(?:implement|codebase|translate)\b',re.I)

class AssistantWorkflows:
    def __init__(self,host,generate):
        self.host,self.generate=host,generate;self.db=host()['db']
        with closing(self.db.connect()) as con,con:
            con.execute('CREATE TABLE IF NOT EXISTS assistant_workflow_turns(conversation_id TEXT NOT NULL,source_id TEXT NOT NULL,digest TEXT NOT NULL,response_json TEXT NOT NULL,state_json TEXT,assistant_id TEXT NOT NULL,PRIMARY KEY(conversation_id,source_id))')
            con.execute('CREATE TABLE IF NOT EXISTS assistant_workflow_approvals(conversation_id TEXT NOT NULL,draft_id TEXT NOT NULL,consumed_at REAL NOT NULL,status TEXT NOT NULL DEFAULT \'unknown\',PRIMARY KEY(conversation_id,draft_id))')
            columns={row[1] for row in con.execute('PRAGMA table_info(assistant_workflow_approvals)')}
            if 'status' not in columns:
                con.execute("ALTER TABLE assistant_workflow_approvals ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
    def state(self,cid):
        if not cid:return None
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT w.state_json FROM assistant_workflow_turns w JOIN messages m ON m.id=w.assistant_id AND m.conversation_id=w.conversation_id WHERE w.conversation_id=? ORDER BY m.ordinal DESC LIMIT 1',(cid,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None
    def _current_state(self,value):
        state=self.state(value.conversation_id)
        if not state:return None
        record=self.db.conversation(value.conversation_id)
        last=(record or {}).get('messages',[])[-1:]
        if last and any(b.get('type')=='assistant_workflow' and b.get('id')==state['draft_id'] for b in (last[0].get('blocks') or [])):return state
        if value.message.strip().startswith('/workflow '):return state
        return None
    def is_mail_turn(self,value):
        from . import mail_chat
        state=self._current_state(value);text=value.message.strip()
        command=re.fullmatch(r'/workflow\s+([a-f0-9]{32})\s+(confirm|cancel)',text)
        return bool(mail_chat.matches(text,state) or state and state.get('domain')=='mail' and (command or mail_chat.is_confirm(text)))
    def is_mail_confirmation(self,value):
        from . import mail_chat
        state=self._current_state(value);text=value.message.strip()
        command=re.fullmatch(r'/workflow\s+([a-f0-9]{32})\s+(confirm|cancel)',text)
        return bool(state and state.get('domain')=='mail' and ((command and command[2]=='confirm') or mail_chat.is_confirm(text)))
    def respond(self,value,resolve):
        if any(getattr(value,k,None) for k in ('resume_control','resume_task_id','computer_use_event','image','attachment','agent_builder','location_map','concept_diagram')):return None
        text=value.message.strip();state=self._current_state(value)
        command=re.fullmatch(r'/workflow\s+([a-f0-9]{32})\s+(confirm|cancel)',text)
        inbox_request=text=='/workflow inbox' or bool(re.search(r'(?:お知らせ|実行結果|締切候補).{0,16}(?:見せ|一覧|表示|確認|予定)|(?:show|list).{0,12}(?:updates|notifications|results)',text,re.I))
        from . import personal_chat,tool_form_chat,mail_chat
        domain=(state or {}).get('domain')
        capability_request=bool(re.fullmatch(r'外部アクセス(?:を|は)?(?:オン|オフ|ON|OFF|有効|無効)(?:にして|にする|にしてください|化して)?[。.!！]*',text,re.I))
        explicit=bool(command or text.startswith('/workflow ') and not inbox_request)
        is_confirm=lambda t: bool(_CONFIRM.fullmatch(t) or personal_chat._CONFIRM.fullmatch(t) or tool_form_chat._CONFIRM.fullmatch(t) or domain=='mail' and mail_chat.is_confirm(t))
        is_cancel=lambda t: bool(_CANCEL.fullmatch(t) or personal_chat._CANCEL.fullmatch(t) or tool_form_chat._CANCEL.fullmatch(t))
        if not explicit and _EXCLUDE.search(text):return None
        if capability_request:selected=domain or 'memory'
        elif inbox_request:selected='personal'
        elif state and (is_confirm(text) or is_cancel(text)):selected=domain
        elif mail_chat.matches(text):selected='mail'
        elif self.memory_matches(text):selected='memory'
        elif self.commerce_matches(text):selected='commerce'
        elif tool_form_chat.matches(text):selected='tools_forms'
        elif personal_chat.matches(text):selected='personal'
        elif state and domain=='personal' and personal_chat.matches(text,state):selected='personal'
        elif state and domain=='tools_forms' and tool_form_chat.matches(text,state):selected='tools_forms'
        elif state and domain=='mail' and mail_chat.matches(text,state):selected='mail'
        elif state and domain=='memory':selected='memory'
        elif state and domain=='commerce':selected='commerce'
        elif explicit:selected=domain or 'personal'
        else:return None
        cid,_,_=resolve(value)
        with _LOCK:lock=_LOCKS.setdefault((str(self.db.path),cid),threading.RLock())
        approval_draft=None
        with lock:
            check_cancelled();state=self._current_state(value)
            ident=getattr(value,'message_id',None) or uuid4().hex;digest=sha256(text.encode()).hexdigest()
            with closing(self.db.connect()) as con:
                previous=con.execute('SELECT w.* FROM assistant_workflow_turns w JOIN messages m ON m.id=w.assistant_id AND m.conversation_id=w.conversation_id WHERE w.conversation_id=? AND w.source_id=?',(cid,ident)).fetchone()
            if previous:
                if previous['digest']==digest:return json.loads(previous['response_json'])
                return self._result(cid,'同じ送信IDの内容が変わりました。新しいメッセージとして送ってください。',None,selected,'error')
            normalized=text
            if explicit:
                if not command or not state or state.get('draft_id')!=command[1]:
                    output={'response':'この確認は古くなっています。変更内容をもう一度指示してください。','state':state,'status':'error','preserve_draft':True}
                    return self._persist(cid,ident,digest,text,output,selected)
                selected=state['domain'];normalized=command[2]
            elif state and is_confirm(text):normalized='confirm'
            elif state and is_cancel(text):normalized='cancel'
            if normalized=='cancel' and state and selected!='tools_forms':
                return self._persist(cid,ident,digest,text,{'response':'取り消しました。','state':None},selected)
            if normalized=='confirm':
                if state and state.get('expires_at',0)<time.time():
                    return self._persist(cid,ident,digest,text,{'response':'確認の有効期限が切れました。内容をもう一度指示してください。','state':None,'status':'error'},selected)
                if not state or state.get('stage')!='review':
                    return self._persist(cid,ident,digest,text,{'response':'実行する内容がありません。依頼内容を送ってください。','state':None},selected)
                approval_draft=state['draft_id']
                with closing(self.db.connect()) as con,con:
                    claimed=con.execute("INSERT OR IGNORE INTO assistant_workflow_approvals(conversation_id,draft_id,consumed_at,status) VALUES(?,?,?,'in_progress')",(cid,approval_draft,time.time())).rowcount
                if not claimed:
                    with closing(self.db.connect()) as con:
                        attempt=con.execute('SELECT status FROM assistant_workflow_approvals WHERE conversation_id=? AND draft_id=?',(cid,approval_draft)).fetchone()
                    outcome=attempt['status'] if attempt else 'unknown'
                    if selected=='mail' and outcome not in {'accepted','sent','not_sent'}:
                        return self._persist(cid,ident,digest,text,{'response':'前回の送信要求への応答が保存されていません。送信できたか分からないため、メールアプリの送信済みを確認してください。重複を防ぐため再送していません。','state':None,'status':'error','title':'送信結果を確認してください','details':['前回の送信要求は開始済みです。','メールアプリの送信済みを確認し、届いていない場合はメール内容をもう一度準備してください。']},selected)
                    if selected=='mail' and outcome in {'accepted','sent'}:
                        notice='Apple Mailへの送信依頼は受付済みです。メールアプリの送信済みで結果を確認してください。' if outcome=='accepted' else 'このメールは送信済みとして記録されています。再送はしません。'
                        return self._persist(cid,ident,digest,text,{'response':notice,'state':None,'title':'送信依頼済み' if outcome=='accepted' else 'メールを送信しました','status':'accepted' if outcome=='accepted' else 'completed'},selected)
                    if selected=='mail' and outcome=='not_sent':
                        return self._persist(cid,ident,digest,text,{'response':'前回の送信要求は送信前に拒否されました。接続と送信権限を確認し、内容を準備し直してください。','state':None,'status':'error','title':'メールを送信できませんでした'},selected)
                    return self._persist(cid,ident,digest,text,{'response':'この確認は処理済みです。結果が不明な場合は一覧で確認してください。','state':None,'status':'error','title':'実行結果を確認してください'},selected)
            current=state if state and state.get('domain')==selected else None
            mail_confirm=selected=='mail' and normalized=='confirm'
            try:
                if capability_request:
                    enabled=bool(re.search(r'オン|on|有効',text,re.I))
                    self.host()['update_capabilities']({'external_access_enabled':enabled})
                    output={'response':('外部アクセスをONにしました。' if enabled else '外部アクセスをOFFにしました。')+('先ほどの依頼は保持しています。続けられます。' if current else ''),'state':current,'actions':[{'label':'続ける','message':'先ほどの依頼を続けて'}] if current else []}
                elif inbox_request:output=self.inbox_handle(cid,normalized,current)
                elif selected=='personal':output=personal_chat.handle(self.host(),cid,normalized,current if not (current or {}).get('inbox') else None,getattr(value,'client_time_zone',None) or 'Asia/Tokyo',self.generate)
                elif selected=='tools_forms':output=tool_form_chat.handle(self.host(),cid,normalized,current,getattr(value,'client_time_zone',None) or 'Asia/Tokyo',self.generate)
                elif selected=='mail':output=mail_chat.handle(self.host(),cid,normalized,current,self.generate)
                elif selected=='memory':output=self.memory_handle(normalized,current)
                else:output=self.commerce_handle(normalized,current)
                if not mail_confirm:check_cancelled()
            except Exception as exc:
                if mail_confirm:
                    output={'response':'メールへの送信要求は開始されましたが、結果を確認できませんでした。メールアプリの送信済みを確認してください。','state':None,'status':'error','title':'送信結果を確認してください','details':['自動再送はしていません。','メールアプリの送信済みを確認し、届いていない場合は新しいメールとして準備してください。'],'mail_delivery_status':'uncertain'}
                else:
                    check_cancelled()
                    output={'response':'処理できませんでした。対象や接続状態を確認して、もう一度具体的に指示してください。','state':None if normalized=='confirm' else current,'status':'error'}
            if output is None:return None
            if mail_confirm:
                return self._persist(cid,ident,digest,text,output,selected,durable=True,approval=(cid,approval_draft,output.get('mail_delivery_status','uncertain')))
            return self._persist(cid,ident,digest,text,output,selected)
    def _result(self,cid,response,state,domain,status=None,details=None,actions=None,title=None,blocks=None,browser_session_id=None):
        stage=(state or {}).get('stage')
        status=status or ('review' if stage=='review' else 'collecting' if state else 'completed')
        bid=(state or {}).get('draft_id') or uuid4().hex
        buttons=list(actions or [])
        if stage=='review' and not buttons and status!='error':
            buttons=[{'label':'この内容で送信' if domain=='mail' else 'この内容で実行','message':f'/workflow {bid} confirm'},{'label':'取り消す','message':f'/workflow {bid} cancel'}]
        return {'conversation_id':cid,'response':response,'sources':[], 'blocks':[{'type':'assistant_workflow','id':bid,'conversation_id':cid,'domain':domain,'status':status,
            'title':title or {'personal':'タスク・目標・プロフィール','tools_forms':'フォーム・ツール','memory':'会話の記憶','commerce':'ショッピング','mail':'メール'}[domain],
            'details':details or [],'actions':buttons,**({'browser_session_id':browser_session_id} if browser_session_id else {})},*(blocks or [])]}
    def _persist(self,cid,ident,digest,text,output,domain,durable=False,approval=None):
        state=output.get('state')
        if state:state={**state,'domain':domain,'draft_id':state.get('draft_id') if output.get('preserve_draft') else uuid4().hex,'expires_at':state.get('expires_at',time.time()+1800) if output.get('preserve_draft') else time.time()+1800}
        result=self._result(cid,output['response'],state,domain,output.get('status'),output.get('details'),output.get('actions'),output.get('title'),output.get('blocks'),output.get('browser_session_id') or (state or {}).get('payload',{}).get('session_id'))
        if not durable:check_cancelled()
        assistant_id=uuid4().hex
        with closing(self.db.connect()) as con,con:
            con.execute('BEGIN IMMEDIATE')
            ordinal=con.execute('SELECT COALESCE(MAX(ordinal),-1)+1 FROM messages WHERE conversation_id=?',(cid,)).fetchone()[0]
            for offset,(role,content,blocks) in enumerate([('user',text,None),('assistant',result['response'],result['blocks'])]):
                con.execute('INSERT INTO messages(id,conversation_id,role,content,ordinal,created_at,blocks_json) VALUES(?,?,?,?,?,?,?)',
                    (uuid4().hex if role=='assistant' else ident,cid,role,content,ordinal+offset,time.time(),json.dumps(blocks,ensure_ascii=False) if blocks else None))
                if role=='assistant':assistant_id=con.execute('SELECT id FROM messages WHERE conversation_id=? AND ordinal=?',(cid,ordinal+offset)).fetchone()[0]
            con.execute('UPDATE conversations SET updated_at=? WHERE id=?',(time.time(),cid))
            con.execute('INSERT OR REPLACE INTO assistant_workflow_turns VALUES(?,?,?,?,?,?)',(cid,ident,digest,json.dumps(result,ensure_ascii=False),json.dumps(state,ensure_ascii=False) if state else None,assistant_id))
            if approval:
                con.execute('UPDATE assistant_workflow_approvals SET status=? WHERE conversation_id=? AND draft_id=?',(approval[2],approval[0],approval[1]))
        return result
    def inbox_handle(self,cid,text,state):
        service=self.host()['personal_tasks']
        match=re.search(r'締切候補\s*(\d+).{0,12}予定',text)
        if match:
            index=int(match[1])-1
            choices=(state or {}).get('choices',[])
            if not state or not state.get('inbox') or not 0<=index<len(choices):
                return {'response':'先に「タスクのお知らせを見せて」と依頼し、表示された締切候補を選んでください。','state':None}
            choice=choices[index]
            action=service.preview_calendar(choice['run_id'],choice['proposal_index'],cid)
            return {'response':'予定の下書きを作りました。内容を確認して、このチャット内で登録できます。','state':None,
                    'blocks':[{'type':'assistant_action','kind':'calendar_action','id':action['id'],'action':action}]}
        notices=service.inbox()[:10];details=[];choices=[]
        for item in notices:
            result=item.get('result',{})
            details.append(item['title']+'：'+str(result.get('summary',''))[:500])
            details.extend(str(x)[:500] for x in result.get('steps',[])[:5])
            for index,proposal in enumerate(result.get('proposals',[])[:5]):
                choices.append({'run_id':item['id'],'proposal_index':index})
                details.append(f"締切候補{len(choices)}：{proposal['date']} {proposal['title']}")
            if not item.get('read_at'):service.mark_read(item['id'])
        return {'response':'タスクからのお知らせです。締切候補は「締切候補1を予定に入れて」のように選べます。' if notices else 'タスクからの新しいお知らせはありません。',
                'state':{'inbox':True,'stage':'collecting','choices':choices} if choices else None,'details':details}

    @staticmethod
    def memory_matches(text,state=None):
        return bool(re.search(r'(?:プロジェクト.{0,8}(?:また|跨)|会話の記憶|記憶設定|記憶.{0,15}(?:閾値|しきい値|関連度)|cross.project.memory|memory.{0,12}threshold)',text,re.I) or state)
    def memory_handle(self,text,state):
        context=self.host();settings=context['settings'];updates={}
        if 'threshold' in text.lower() or re.search(r'閾値|しきい値|関連度',text):
            number=re.search(r'(?<![\d.])-?\d+(?:\.\d+)?',text)
            if not number:return {'response':'関連度の閾値を0〜1で指定してください。例：0.8','state':{'stage':'collecting','kind':'threshold'}}
            score=float(number[0])
            if not 0<=score<=1:return {'response':'閾値は0〜1で指定してください。','state':{'stage':'collecting','kind':'threshold'}}
            updates['conversation_memory_min_relevance']=score
        elif state and state.get('kind')=='threshold' and re.fullmatch(r'\d+(?:\.\d+)?',text):
            score=float(text)
            if not 0<=score<=1:return {'response':'閾値は0〜1で指定してください。','state':state}
            updates['conversation_memory_min_relevance']=score
        off=bool(re.search(r'(?<![a-z])off(?![a-z])|オフ|使わない|参照しない|無効',text,re.I))
        on=bool(re.search(r'(?<![a-z])on(?![a-z])|オン|有効|参照して|使って',text,re.I))
        if on!=off:updates['conversation_memory_cross_project_enabled' if re.search(r'また|跨|cross',text,re.I) else 'conversation_memory_enabled']=on
        if updates:
            saved=context['update_memory'](updates)
            return {'response':'記憶の参照方法を変更しました。保存済みの記憶は削除しません。','state':None,'details':self.memory_details(saved)}
        return {'response':'現在の記憶設定です。「プロジェクトをまたぐ記憶をオフにして」「記憶の関連度を0.8にして」のように変更できます。','state':None,'details':self.memory_details(settings.public_dict())}
    @staticmethod
    def memory_details(settings):
        return [f"過去の会話の参照：{'ON' if settings['conversation_memory_enabled'] else 'OFF'}",f"プロジェクトをまたぐ参照：{'ON' if settings['conversation_memory_cross_project_enabled'] else 'OFF'}",f"関連度の閾値：{settings['conversation_memory_min_relevance']:.2f}"]
    @staticmethod
    def commerce_matches(text,state=None):
        return bool(state or re.search(r'(?:Amazon|アマゾン|楽天|Yahoo|ヤフー|Qoo10|メルカリ|ヨドバシ|ビックカメラ|ZOZO|eBay|ラクマ|au PAY).{0,18}(?:接続|ログイン|連携|切断)',text,re.I))
    def commerce_handle(self,text,state):
        from .connector_marketplaces import PROVIDERS
        context=self.host();service=context['service_connectors'];browser=context['browser_sessions']
        if text=='confirm' and state:
            cid=state['provider'];key=cid.removeprefix('service.commerce.')
            if state.get('operation')=='disconnect':
                service.policy(cid,{'enabled':False})
                for entry in browser.list():
                    if entry.get('profile_id')=='market-'+key:browser.close(entry['session_id'])
                return {'response':'接続をOFFにし、開いていた専用ブラウザーを閉じました。保存済みのログインは残しています。','state':None}
            if state['enable_external']:context['update_capabilities']({'external_access_enabled':True})
            if state['enable_shopping']:service.set_category('shopping',True)
            service.connect(cid)
            service.authorize(cid,'read')
            opened=next((s for s in browser.list() if s.get('profile_id')=='market-'+key),None)
            if not opened:
                opened=browser.open(profile_id='market-'+key,interactive_login=True)
                if opened.get('status')!='READY':raise ValueError('MARKETPLACE_BROWSER_UNAVAILABLE')
                service.authorize(cid,'read')
                result=browser.navigate(opened['session_id'],PROVIDERS[cid]['home'])
                if result.get('status')!='AVAILABLE':browser.close(opened['session_id']);raise ValueError('MARKETPLACE_BROWSER_NAVIGATION_FAILED')
            return {'response':'専用ブラウザーを開きました。ログインや認証は開いた画面で行ってください。ログイン情報はこのMacに保持し、チャットには読み取りません。','state':None}
        aliases={'amazon':['Amazon','アマゾン'],'rakuten':['楽天市場','楽天'],'yahoo':['Yahoo!ショッピング','Yahoo','ヤフー'],'aupay':['au PAY'],'qoo10':['Qoo10'],'mercari':['メルカリ'],'yodobashi':['ヨドバシ'],'biccamera':['ビックカメラ'],'zozo':['ZOZO'],'ebay':['eBay'],'rakuma':['ラクマ'],'yahooflea':['Yahoo!フリマ','ヤフーフリマ']}
        selected=[(key,max(len(name) for name in names if name.lower() in text.lower())) for key,names in aliases.items() if any(name.lower() in text.lower() for name in names)]
        if not selected:return {'response':'接続するサービス名を教えてください。例：Amazon、楽天市場、Yahoo!ショッピング。','state':{'stage':'collecting'}}
        key=max(selected,key=lambda row:row[1])[0];cid='service.commerce.'+key
        from .permissions import capability_enabled
        shopping=any(c['id']=='shopping' and c['enabled'] for c in service.categories())
        draft={'stage':'review','provider':cid,'operation':'disconnect' if re.search(r'切断|解除|オフ|OFF',text,re.I) else 'connect',
               'enable_external':not capability_enabled(context['settings'],'external_access_enabled'),'enable_shopping':not shopping}
        if draft['operation']=='disconnect':
            draft.update(enable_external=False,enable_shopping=False)
            return {'response':'この内容で接続をOFFにします。','state':draft,'details':[PROVIDERS[cid]['label']+'の接続をOFFにし、専用ブラウザーを閉じます。','保存済みのログインは保持します。']}
        details=[PROVIDERS[cid]['label']+'の専用ブラウザーを利用します。','ログイン・認証はご自身で行います。注文・決済は確定しません。']
        if draft['enable_external']:details.append('外部アクセスをONにします。')
        if draft['enable_shopping']:details.append('ショッピングの利用許可をONにします。')
        return {'response':'この内容で接続します。','state':draft,'details':details}


def events(result):
    yield 'data: '+json.dumps({'type':'meta','conversation_id':result['conversation_id'],'task_id':None})+'\n\n'
    yield 'data: '+json.dumps({'type':'chunk','text':result['response']},ensure_ascii=False)+'\n\n'
    for block in result['blocks']:yield 'data: '+json.dumps({'type':'block','block':block},ensure_ascii=False)+'\n\n'
    yield 'data: '+json.dumps({'type':'done','conversation_id':result['conversation_id'],'response_owner':'ASSISTANT_WORKFLOW'})+'\n\n'
