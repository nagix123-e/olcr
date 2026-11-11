"""Explicit personal-service chat requests; never intercept coding or model setup."""
import json
import re
import uuid
import time
from .service_connectors import ConnectorError

def intent(text):
    if text.strip()=='/connectors':return {'settings':True}
    match=re.fullmatch(r'\s*/service\s+(service\.[a-z0-9.]+)\s+(list|get|search|cart|review|send|account|product)\s*(\{.*\})?\s*',text,re.S)
    if match:
        try:arguments=json.loads(match[3] or '{}')
        except ValueError:raise ConnectorError('CONNECTOR_ARGUMENTS_INVALID') from None
        return {'id':match[1],'action':match[2],'arguments':arguments}
    for name,key in [('Amazon.co.jp','amazon'),('Amazon','amazon'),('アマゾン','amazon'),('楽天市場','rakuten'),('楽天','rakuten'),('Rakuten','rakuten'),('Yahoo!ショッピング','yahoo'),('Yahoo Shopping','yahoo'),('au PAY マーケット','aupay'),('Qoo10','qoo10'),('メルカリ','mercari'),('Mercari','mercari')]:
        if re.match(r'^\s*'+re.escape(name)+r'(?:で|の|から|を|\s)',text,re.I) and re.search(r'探|検索|search|find',text,re.I):
            quoted=re.search(r'[「"]([^」"]+)[」"]',text)
            if quoted:return {'id':'service.commerce.'+key,'action':'search','arguments':{'query':quoted[1]}}
    # Explicit product names + reading verbs only; coding intent is excluded by caller.
    for name,cid in [('Gmail','service.google.gmail'),('Apple Mail','service.apple.mail'),('Appleのメール','service.apple.mail'),('Macのメール','service.apple.mail'),('メールアプリ','service.apple.mail'),('Google Drive','service.google.drive'),('Google Calendar','service.google.calendar')]:
        if re.match(r'^\s*'+re.escape(name)+r'(?:で|の|から|を|\s)',text,re.I) and re.search(r'読|表示|一覧|探|検索|見せ|確認|list|search|show|read|find',text,re.I):
            quoted=re.search(r'[「"]([^」"]+)[」"]',text)
            query=quoted[1] if quoted else ''
            return {'id':cid,'action':'list','arguments':{'query':query} if cid!='service.google.calendar' else {}}
    return None

def handle(db,value,request,service,resolve):
    cid,_,_=resolve(value)
    if request.get('settings'):
        response='外部サービスの接続と権限を設定してください。 /service service.google.gmail list {} で取得できます。商品検索は /service service.commerce.amazon search {"query":"充電器"} などを使えます。メール送信は /service service.google.gmail send に宛先・件名・本文を指定します。購入確認は /service service.commerce.checkout review に注文JSONを指定します。'
        blocks=[{'type':'settings_control'}]
    elif request['id']=='service.commerce.checkout' and request['action']=='review':
        blocks=[service.prepare_purchase(request['arguments'])];response='販売店・商品・合計金額を確認してください。承認すると販売店の決済画面を開けます。決済はその画面で行います。'
    else:
        result=service.execute(request['id'],request['action'],request['arguments'])
        blocks=[result.pop('review')] if isinstance(result,dict) and result.get('review') else []
        if isinstance(result,dict) and result.get('type')=='browser_handoff':
            response='[公式サイトを開く]('+result['url']+')\n\nログインと購入は公式サイトで行ってください。OLCRはログインや支払いの完了を確認していません。'
        elif request['action']=='send' and isinstance(result,dict) and result.get('sent'):
            response='メールを送信しました。AIエージェントによる送信の注記を末尾に追加しています。'
        else:
            response=json.dumps(result,ensure_ascii=False,indent=2)
        if request['id'].startswith('service.google.') and service.google.status(request['id']).get('credential_sync_required') is True:
            blocks.append({'type':'service_credential_sync','connector_id':request['id']})
    source=value.message_id or uuid.uuid4().hex
    for role,content,items in [('user',value.message,None),('assistant',response,blocks)]:
        mid=uuid.uuid5(uuid.NAMESPACE_URL,cid+':connector:'+source+':'+role).hex
        with db.connect() as con:exists=con.execute('SELECT 1 FROM messages WHERE id=?',(mid,)).fetchone()
        if not exists:db.add_message(cid,role,content,time.time(),mid,blocks=items)
    return {'conversation_id':cid,'response':response,'blocks':blocks,'sources':[]}
