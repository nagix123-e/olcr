import {SettingHelp} from './SettingHelp';
import {StatusBadge,Feedback} from './InterfacePrimitives';
import {useEffect,useId,useRef,useState} from 'react';
import {CredentialFields} from './CredentialFields';
import {native,openExternal} from './native';
import {useLanguage} from './language';
import {SettingsBack} from './SettingsBack';
import {AssistantSettings} from './AssistantSettings';
import {ServiceConnectorIcon} from './ServiceConnectorIcon';
import './service-connectors.css';

type Api=(path:string,init?:RequestInit)=>Promise<any>;
type CategoryId='mail'|'calendar'|'files'|'shopping';
type Category={id:CategoryId;label:string;enabled:boolean};
const categoriesById:CategoryId[]=['mail','calendar','files','shopping'];
const categoryFor=(row:Connector):CategoryId=>row.category||({'service.google.gmail':'mail','service.google.calendar':'calendar','service.google.drive':'files','service.commerce.checkout':'shopping'} as Record<string,CategoryId>)[row.id]||'files';
const marketplaces=new Set(['amazon','rakuten','yahoo','aupay','qoo10','mercari','yodobashi','biccamera','zozo','ebay','rakuma','yahooflea'].map(key=>`service.commerce.${key}`));
const marketplaceKey=(cid:string)=>cid.replace('service.commerce.','');
type Connector={id:string;label:string;state:string;enabled:boolean;category?:CategoryId;actions:unknown[];send_consent_required?:boolean;write_consent_required?:boolean};
type Attempt={id:string;connectorId:string};
const connectorError=(reason:unknown,ja:boolean)=>{const code=reason instanceof Error?reason.message:String(reason);if(code==='APPLE_MAIL_AUTOMATION_DENIED')return ja?'macOSで拒否されています。システム設定 → プライバシーとセキュリティ → オートメーションで、OLCRによるMailの操作を許可し、接続をやり直してください。':'Allow OLCR to control Mail in System Settings → Privacy & Security → Automation, then reconnect.';if(code==='APPLE_MAIL_AUTOMATION_PERMISSION_REQUIRED')return ja?'macOSの許可が完了していません。接続をやり直し、OLCRによるMailの操作を許可してください。':'The macOS permission request is not complete. Reconnect and allow OLCR to control Mail.';if(code==='APPLE_MAIL_NOT_RUNNING')return ja?'Mailを起動できませんでした。Mailアプリを開いて接続をやり直してください。':'Mail could not be started. Open Mail and reconnect.';if(code==='APPLE_MAIL_PERMISSION_CHECK_FAILED')return ja?'macOSの許可状態を確認できませんでした。OLCRを再起動し、接続をやり直してください。':'Could not check macOS permission. Restart OLCR and reconnect.';if(code==='GMAIL_SEND_CONSENT_REQUIRED')return ja?'Gmailの送信にはGoogleでの再認証が必要です。':'Reauthorize Google to allow Gmail sending.';return code};
const storedGoogleClientId=()=>{try{return localStorage.getItem('olcr-google-connector-client-id')||''}catch{return ''}};
const handles:Record<string,string>={
 'service.google.gmail':'OLCR_CONNECTOR_GOOGLE_GMAIL',
 'service.google.calendar':'OLCR_CONNECTOR_GOOGLE_CALENDAR',
 'service.google.drive':'OLCR_CONNECTOR_GOOGLE_DRIVE',
 'service.commerce.checkout':'OLCR_CONNECTOR_SHOPIFY',
 'service.commerce.yahoo':'OLCR_CONNECTOR_YAHOO_SHOPPING',
};

/** New personal-service connections retain their own permissions and credential slots. */
export function ServiceConnectorsSettings({api,onBack}:{api:Api;onBack?:()=>void}){
 const {language}=useLanguage(),ja=language==='ja',id=useId();
 const [connectors,setConnectors]=useState<Connector[]>([]),[categories,setCategories]=useState<Category[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(''),[googleClientId,setGoogleClientId]=useState(''),[externalAccessEnabled,setExternalAccessEnabled]=useState(false);
 const [editing,setEditing]=useState(''),[clientId,setClientId]=useState('');
 const [yahooId,setYahooId]=useState('');
 const [shop,setShop]=useState(''),[storefrontToken,setStorefrontToken]=useState('');
 const [attempt,setAttempt]=useState<Attempt|null>(null),[status,setStatus]=useState('');
 const mounted=useRef(true);
 const authGeneration=useRef(0);
 const applyInventory=(next:any)=>{if(!mounted.current)return;const rows:Connector[]=Array.isArray(next.connectors)?next.connectors:[];setConnectors(rows);setCategories(Array.isArray(next.categories)?next.categories:categoriesById.filter(id=>rows.some(row=>categoryFor(row)===id)).map(id=>({id,label:id,enabled:false})));// Mutation responses omit global settings; absence must not revoke the loaded permission.
 if(typeof next.external_access_enabled==='boolean')setExternalAccessEnabled(next.external_access_enabled);const configured=typeof next.google_oauth_client_id==='string'&&next.google_oauth_client_id?next.google_oauth_client_id:storedGoogleClientId();setGoogleClientId(configured);if(configured){try{localStorage.setItem('olcr-google-connector-client-id',configured)}catch{}}};
 const load=async()=>applyInventory(await api('/connectors'));
 useEffect(()=>{mounted.current=true;void load().catch(reason=>{if(mounted.current)setError(reason instanceof Error?reason.message:String(reason))});return()=>{mounted.current=false;authGeneration.current++}},[api]);
 useEffect(()=>{
  if(!attempt)return;
  let live=true,checking=false;
  const poll=async()=>{if(checking)return;checking=true;try{
   const next=await api(`/connectors/oauth/${encodeURIComponent(attempt.id)}`);if(!live)return;
   if(next.status==='ready'){
    if(next.connector_id!==attempt.connectorId||typeof next.bundle!=='string'||!handles[attempt.connectorId])throw Error('CONNECTOR_CREDENTIAL_MISMATCH');
    const bridge=native();if(!bridge)throw Error(ja?'認証情報の保存にはOLCRデスクトップが必要です。':'OLCR Desktop is required to save credentials.');
    await bridge.core.invoke('set_external_credential',{handle:handles[attempt.connectorId],value:next.bundle});
    if(!live)return;
    await api(`/connectors/${encodeURIComponent(attempt.connectorId)}/credential`,{method:'POST',body:JSON.stringify({bundle:next.bundle})});
    if(!live)return;
    setAttempt(null);setEditing('');setStatus(ja?'接続しました。カテゴリの利用権限とサービス接続をオンにしてください。':'Connected. Enable the category and the service connection you want to use.');await load();
   }else if(next.status==='error')throw Error(next.error||'CONNECTOR_SIGN_IN_FAILED');
  }catch(reason){if(live){setAttempt(null);setError(reason instanceof Error?reason.message:String(reason))}}finally{checking=false}};
  void poll();const timer=window.setInterval(()=>void poll(),1500);
  return()=>{live=false;window.clearInterval(timer)};
 },[attempt?.id,api,ja]);
 const update=async(connector:Connector,patch:{enabled:boolean})=>{
  if(busy)return;setBusy(connector.id);setError('');
  try{const path=(connector.id==='service.apple.mail'||marketplaces.has(connector.id))&&patch.enabled?`/connectors/${encodeURIComponent(connector.id)}/connect`:`/connectors/${encodeURIComponent(connector.id)}/policy`;const init:RequestInit=path.endsWith('/policy')?{method:'PATCH',body:JSON.stringify(patch)}:{method:'POST'};const next=await api(path,init);if(mounted.current){if((connector.id==='service.apple.mail'||marketplaces.has(connector.id))&&patch.enabled)applyInventory(next);else setConnectors(rows=>rows.map(row=>row.id===connector.id?{...row,...next}:row))}if(marketplaces.has(connector.id)&&!patch.enabled)await api(`/commerce-sessions/${marketplaceKey(connector.id)}/session`,{method:'DELETE'});}
  catch(reason){if(mounted.current)setError(connectorError(reason,ja))}finally{if(mounted.current)setBusy('')}
 };
 const updateCategory=async(category:Category,enabled:boolean)=>{
  if(busy)return;setBusy(`category:${category.id}`);setError('');
  try{applyInventory(await api(`/connectors/categories/${encodeURIComponent(category.id)}`,{method:'PATCH',body:JSON.stringify({enabled})}));if(category.id==='shopping'&&!enabled)for(const connector of connectors.filter(row=>marketplaces.has(row.id)))await api(`/commerce-sessions/${marketplaceKey(connector.id)}/session`,{method:'DELETE'});}
  catch(reason){if(mounted.current)setError(reason instanceof Error?reason.message:String(reason))}finally{if(mounted.current)setBusy('')}
 };
 const start=async(connectorId=editing,clientIdValue=clientId)=>{
  if(!connectorId||busy)return;setBusy(connectorId);setError('');setStatus('');
  const generation=++authGeneration.current;
  try{
   if(connectorId==='service.commerce.yahoo'){
    const applicationId=yahooId.trim();if(!/^[A-Za-z0-9_.-]{1,512}$/.test(applicationId))throw Error('CONNECTOR_CREDENTIAL_INVALID');
    const bridge=native();if(!bridge)throw Error(ja?'認証情報の保存にはOLCRデスクトップが必要です。':'OLCR Desktop is required to save credentials.');
    const bundle=JSON.stringify({application_id:applicationId});
    await bridge.core.invoke('set_external_credential',{handle:handles[connectorId],value:bundle});
    if(!mounted.current||generation!==authGeneration.current)return;
    await api(`/connectors/${encodeURIComponent(connectorId)}/credential`,{method:'POST',body:JSON.stringify({bundle})});
    if(!mounted.current||generation!==authGeneration.current)return;
    setYahooId('');setEditing('');setStatus(ja?'商品検索APIの設定を保存しました。購入者アカウントのログインとは別です。':'Developer search API saved. Consumer account sign-in is separate.');await load();return;
   }
   if(connectorId==='service.commerce.checkout'){
    const domain=shop.trim().toLowerCase();if(!/^[a-z0-9][a-z0-9-]*\.myshopify\.com$/.test(domain))throw Error(ja?'Shopifyのショップドメインを入力してください。':'Enter a Shopify shop domain.');
    const bridge=native();if(!bridge)throw Error(ja?'認証情報の保存にはOLCRデスクトップが必要です。':'OLCR Desktop is required to save credentials.');
    const bundle=JSON.stringify({shop:domain,storefront_token:storefrontToken.trim()});
    await bridge.core.invoke('set_external_credential',{handle:handles[connectorId],value:bundle});
    if(!mounted.current||generation!==authGeneration.current)return;
    await api(`/connectors/${encodeURIComponent(connectorId)}/credential`,{method:'POST',body:JSON.stringify({bundle})});
    if(!mounted.current||generation!==authGeneration.current)return;
    setStorefrontToken('');setEditing('');setStatus(ja?'接続を保存しました。カテゴリの利用権限とサービス接続をオンにしてください。':'Connection saved. Enable the category and the service connection you want to use.');await load();return;
   }
   const selectedClientId=clientIdValue.trim()||googleClientId;
   if(selectedClientId){setGoogleClientId(selectedClientId);try{localStorage.setItem('olcr-google-connector-client-id',selectedClientId)}catch{}}
   const next=await api(`/connectors/${encodeURIComponent(connectorId)}/oauth/start`,{method:'POST',body:JSON.stringify({client_id:selectedClientId})});
   if(!mounted.current||generation!==authGeneration.current){if(typeof next.id==='string')void api(`/connectors/oauth/${encodeURIComponent(next.id)}`,{method:'DELETE'}).catch(()=>{});return;}
   const url=new URL(next.url);if(url.protocol!=='https:'||url.hostname!=='accounts.google.com')throw Error('CONNECTOR_SIGN_IN_URL_INVALID');
   setStatus(ja?'ブラウザでGoogleアカウントの認証を完了してください。':'Finish Google sign-in in your browser.');await openExternal(url.href);if(mounted.current&&generation===authGeneration.current)setAttempt({id:next.id,connectorId});
  }catch(reason){if(mounted.current)setError(reason instanceof Error?reason.message:String(reason))}finally{if(mounted.current)setBusy('')}
 };
 const quickGoogleConnect=(connector:Connector)=>{
  if(!googleClientId||busy||attempt)return;
  setEditing(connector.id);setClientId(googleClientId);
  void start(connector.id,googleClientId);
 };
 const disconnect=async(connector:Connector)=>{
  if(busy)return;setBusy(connector.id);setError('');authGeneration.current++;setAttempt(null);
  try{await api(`/connectors/${encodeURIComponent(connector.id)}/connection`,{method:'DELETE'});if(marketplaces.has(connector.id))await api(`/commerce-sessions/${marketplaceKey(connector.id)}/session`,{method:'DELETE'});await load()}
  catch(reason){if(mounted.current)setError(reason instanceof Error?reason.message:String(reason))}finally{if(mounted.current)setBusy('')}
 };
 const marketplaceSession=async(connector:Connector,forget=false)=>{
  if(busy)return;setBusy(connector.id);setError('');setStatus('');
  try{await api(`/commerce-sessions/${marketplaceKey(connector.id)}/${forget?'session/forget':'open'}`,{method:forget?'DELETE':'POST'});if(mounted.current)setStatus(forget?(ja?'このサービスのローカルブラウザーセッションを削除しました。':'Local browser session removed for this service.'):(ja?'OLCRブラウザーでログインを完了してください。ログイン状態はOLCRでは検証しません。':'Complete sign-in in the OLCR browser. OLCR does not verify sign-in.'));}
  catch(reason){if(mounted.current)setError(reason instanceof Error?reason.message:String(reason));}finally{if(mounted.current)setBusy('');}
 };
 const stateLabel=(state:string)=>({requires_user_sign_in:ja?'ブラウザーでのログインが必要':'Sign in in browser',developer_api_ready:ja?'商品検索APIの設定済み':'Developer search API configured',write_consent_required:ja?'予定登録にはGoogle再認証が必要':'Reauthorize Google to create events',ready:ja?'準備完了':'Ready',connected:ja?'接続済み':'Connected',send_consent_required:ja?'送信にはGoogle再認証が必要':'Reauthorize Google for sending',permission_required:ja?'macOSの許可が必要':'macOS permission required',permission_denied:ja?'macOSで拒否済み':'Denied in macOS',not_running:ja?'Mailを起動して接続':'Start Mail to connect',permission_error:ja?'macOSの許可確認に失敗':'macOS permission check failed',unavailable:ja?'このMacでは利用できません':'Unavailable on this Mac',reconnect_required:ja?'再接続が必要':'Reconnect required',not_connected:ja?'未接続':'Not connected',disconnected:ja?'未接続':'Not connected'}[state]||state);
 const categoryLabel=(category:CategoryId)=>({mail:ja?'メール':'Mail',calendar:ja?'カレンダー':'Calendar',files:ja?'ファイル':'Files',shopping:ja?'ショッピング':'Shopping'}[category]);
 return <section className="chat-settings service-connectors-page" aria-label={ja?'サービス接続設定':'Service connection settings'}>
  {onBack&&<SettingsBack onBack={onBack}/>}<h2>{ja?'サービス接続':'Service connections'}</h2><p>{ja?'カテゴリごとに利用を許可し、接続するサービスを選びます。':'Allow access by category, then enable the services you want to use.'}</p>
  {categories.map(category=><section className="service-category" key={category.id} aria-label={categoryLabel(category.id)}>
   <div className="setting-row capability-setting service-category-heading"><h3><label htmlFor={`${id}-category-${category.id}`}>{categoryLabel(category.id)}</label><SettingHelp label={categoryLabel(category.id)}>{ja?'このカテゴリに属するサービスの利用をまとめて許可します。接続状態は各サービス名の下に表示されます。オフにしても認証情報は削除されません。':'Allow services in this category together. Each service shows its connection state below its name. Switching off does not delete credentials.'}</SettingHelp></h3><input id={`${id}-category-${category.id}`} aria-label={`${categoryLabel(category.id)} ${ja?'カテゴリ権限':'category access'}`} type="checkbox" role="switch" checked={category.enabled} disabled={Boolean(busy)} onChange={event=>void updateCategory(category,event.target.checked)}/></div>
   {category.id==='mail'&&<p className="setting-hint">{ja?'メールカテゴリの許可で、接続中のメールサービスからの読み取りと送信を許可します。OLCRが送るメールにはAI送信の注記が必ず末尾に付きます。':'Mail access allows reading and sending through enabled mail providers. OLCR always appends its AI-agent disclosure to sent messages.'}</p>}
   <div className="service-connector-grid">{category.id==='calendar'&&<AssistantSettings api={api} embedded categoryEnabled={category.enabled} />}{connectors.filter(connector=>categoryFor(connector)===category.id).map((connector,index)=>{
   const marketplace=marketplaces.has(connector.id);
   const connected=connector.state==='connected'||connector.state==='ready'||connector.state==='send_consent_required'||connector.state==='write_consent_required';
   const needsCalendarWriteConsent=connector.id==='service.google.calendar'&&connector.state==='write_consent_required';
   const needsGmailSendConsent=connector.id==='service.google.gmail'&&connector.state==='send_consent_required';
   return <div className="service-connector-tile" key={connector.id}>
    <div className="setting-row capability-setting"><label className="service-connector-identity" htmlFor={`${id}-${category.id}-${index}-enabled`}><ServiceConnectorIcon connectorId={connector.id} networkAllowed={externalAccessEnabled} api={api}/><span>{connector.label}<small><StatusBadge state={connector.state}>{stateLabel(connector.state)}</StatusBadge></small></span></label><input id={`${id}-${category.id}-${index}-enabled`} aria-label={`${connector.label} ${ja?'利用を許可':'allow access'}`} type="checkbox" role="switch" checked={connector.enabled&&(connector.id!=='service.apple.mail'||connector.state==='connected')} disabled={Boolean(busy)||connector.state==='unavailable'||(connector.id==='service.apple.mail'&&(!externalAccessEnabled||!category.enabled)&&!connector.enabled)} onChange={event=>void update(connector,{enabled:event.target.checked})}/></div>
    {connector.id==='service.apple.mail'&&!connector.enabled&&<p className="setting-hint">{!externalAccessEnabled?(ja?'接続するには、設定の「ツールとチャット機能」で外部アクセスをオンにしてください。':'To connect, enable External access under Tools and chat capabilities in settings.'):!category.enabled?(ja?'先に、このセクション右上のメールカテゴリの利用許可をオンにしてください。':'First enable Mail category access at the top of this section.'):connector.state==='permission_denied'?(ja?'システム設定 → プライバシーとセキュリティ → オートメーションで、OLCRによるMailの操作を許可してください。':'Allow OLCR to control Mail in System Settings → Privacy & Security → Automation.'):connector.state==='not_running'?(ja?'このスイッチをオンにするとMailを起動して接続します。':'Turn this switch on to start Mail and connect.'):connector.state==='permission_required'?(ja?'このスイッチをオンにすると、macOSにメール操作の許可を求めます。':'Turn this switch on to request macOS permission to control Mail.'):null}</p>}
    {connector.id.startsWith('service.google.')&&googleClientId&&(!connected||needsGmailSendConsent||needsCalendarWriteConsent)&&<button type="button" className="service-google-connect" disabled={Boolean(busy)||Boolean(attempt)} onClick={()=>quickGoogleConnect(connector)}>{busy===connector.id?(ja?'Googleに接続中…':'Connecting to Google…'):(needsCalendarWriteConsent?(ja?'カレンダーへの登録を許可':'Allow calendar events'):needsGmailSendConsent?(ja?'Gmailの送信を許可':'Allow Gmail sending'):(connector.state==='reconnect_required'?(ja?'Googleで再接続':'Reconnect with Google'):(ja?'Googleに接続':'Connect with Google')))}</button>}
    {needsCalendarWriteConsent&&<p className="setting-hint">{ja?'Googleで再認証すると、自分が所有するカレンダーに確認済みの服用予定を登録できます。':'Reauthorize Google to add confirmed medication events to calendars you own.'}</p>}
    {marketplace&&<><p className="setting-hint">{ja?'OLCR専用ブラウザーでログインし、このMacにセッションを保存します。OLCRはログイン状態を検証しません。商品検索と公式商品ページへの案内に対応し、注文・決済の自動確定には対応しません。':'Sign in in a dedicated OLCR browser; its session stays on this Mac. OLCR does not verify sign-in. Search and official product links are supported; automatic orders and payments are unavailable.'}</p><button type="button" aria-label={`${ja?'OLCRブラウザーでログイン':'Sign in with OLCR browser'}: ${connector.label}`} disabled={Boolean(busy)||!externalAccessEnabled||!category.enabled||!connector.enabled||connector.state==='disconnected'} onClick={()=>void marketplaceSession(connector)}>{ja?'OLCRブラウザーでログイン':'Sign in with OLCR browser'}</button></>}
    <details className="provider-account-details"><summary>{ja?'接続設定':'Connection settings'}</summary>
     {marketplace&&<div className="brain-form-actions"><button type="button" disabled={Boolean(busy)} onClick={()=>void marketplaceSession(connector,true)}>{ja?'保存したブラウザーログインを削除':'Forget browser sign-in'}</button><button type="button" disabled={Boolean(busy)} onClick={()=>void disconnect(connector)}>{ja?'サービス利用を解除':'Disconnect service'}</button></div>}
     {connector.id==='service.apple.mail'&&<div className="apple-mail-connection-info"><p>{ja?'このMacのメールアプリに設定済みのアカウントを使います。受信箱の一覧、個別メールの読み取り、依頼されたメールの送信に対応します。送信本文の末尾にはAI送信の注記が必ず付きます。Apple IDやメールパスワードをOLCRに入力する必要はありません。':'Uses the accounts already configured in Mail on this Mac. OLCR can list inbox messages, read a selected message, and send requested plain-text emails. Every sent message ends with OLCR’s AI-agent disclosure. No Apple ID or mail password is entered into OLCR.'}</p><p>{ja?'最初に接続すると、macOSが「OLCRによるMailの操作」を確認します。トグルをオフにするとOLCR内での利用を止めます。macOSの許可自体を取り消す場合はシステム設定の「プライバシーとセキュリティ」→「オートメーション」で解除してください。':'macOS will ask once whether OLCR may control Mail when you connect. Turning this switch off disables OLCR access; revoke the macOS grant separately in System Settings → Privacy & Security → Automation.'}</p>{!externalAccessEnabled&&<p>{ja?'接続するには、先に「外部アクセス」をオンにしてください。':'Turn on External access before connecting.'}</p>}</div>}
     {needsGmailSendConsent&&<p className="setting-hint">{ja?'このGoogleアカウントは現在読み取り権限のみです。ボタンから再認証すると、既存の受信機能を保ったまま送信権限を追加できます。':'This Google account currently has read-only access. Reauthorize to add sending while retaining inbox access.'}</p>}
     {handles[connector.id]&&<div className="brain-form-actions"><button type="button" aria-label={`Configure ${connector.label}`} disabled={Boolean(busy)||Boolean(attempt)} onClick={()=>{setEditing(connector.id);setClientId(googleClientId||storedGoogleClientId());setShop('');setStorefrontToken('');setError('')}}>{connector.id==='service.commerce.yahoo'?(ja?'商品検索APIの設定':'Configure'):(connected?(ja?'接続し直す':'Reconnect'):(ja?'接続を設定':'Configure'))}</button>{connected&&<button type="button" disabled={Boolean(busy)} onClick={()=>void disconnect(connector)}>{ja?'接続を解除':'Disconnect'}</button>}</div>}
     {editing===connector.id&&<form className="credential-setup" onSubmit={event=>{event.preventDefault();void start()}}>
      {connector.id==='service.commerce.yahoo'?<><p>{ja?'任意設定です。Yahoo!デベロッパーネットワークのClient IDで公式の商品検索APIを利用します。購入者のログインや注文権限は得られません。':'Optional: use the official search API with a Yahoo developer Client ID. This grants no consumer sign-in or ordering access.'}</p><CredentialFields fields={[{id:'yahoo_id',label:ja?'Yahoo開発者Client ID':'Yahoo developer Client ID',input_type:'password'}]} values={{yahoo_id:yahooId}} disabled={Boolean(busy)} onChange={(_,value)=>setYahooId(value)}/><button type="submit" disabled={Boolean(busy)||!yahooId.trim()}>{ja?'開発者API設定を保存':'Save developer API credentials'}</button></>:connector.id==='service.commerce.checkout'?<>
      <p>{ja?'対応するShopifyストアのドメインとStorefront公開アクセストークンを登録します。カード情報は入力しません。':'Connect a supported Shopify store with its domain and Storefront public access token. Do not enter payment card details.'}</p>
      <a href="https://shopify.dev/docs/api/storefront" target="_blank" rel="noopener noreferrer" onClick={event=>{event.preventDefault();void openExternal('https://shopify.dev/docs/api/storefront').catch(()=>setError(ja?'ブラウザを開けませんでした。':'Could not open the browser.'))}}>{ja?'Shopify Storefrontの設定を確認':'Shopify Storefront setup'}</a>
      <CredentialFields fields={[{id:'shop',label:ja?'Shopifyショップのドメイン':'Shopify shop domain',input_type:'text',placeholder:'example.myshopify.com'},{id:'storefront_token',label:ja?'Storefront公開アクセストークン':'Storefront public access token',input_type:'password'}]} values={{shop,storefront_token:storefrontToken}} disabled={Boolean(busy)} onChange={(field,value)=>field==='shop'?setShop(value):setStorefrontToken(value)}/>
      <button type="submit" disabled={Boolean(busy)||!shop.trim()||!storefrontToken.trim()}>{ja?'接続を保存':'Save connection'}</button>
      </>:<><p>{googleClientId?(ja?'Googleの認証画面を開いて、このサービスのアクセスを許可します。':'Continue to Google and authorize this service.'):(ja?'初回のみGoogle Cloudのデスクトップアプリ用OAuthクライアントIDを入力します。アプリ共通の設定がある場合は自動入力されます。':'For first-time setup, enter a Google Cloud Desktop OAuth client ID. OLCR reuses a configured app client automatically.')}</p>
      <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer" onClick={event=>{event.preventDefault();void openExternal('https://console.cloud.google.com/apis/credentials').catch(()=>setError(ja?'ブラウザを開けませんでした。':'Could not open the browser.'))}}>{ja?'Googleの認証情報設定を開く':'Open Google credential settings'}</a>
      <CredentialFields fields={[{id:'client_id',label:ja?'Google OAuthクライアントID':'Google OAuth client ID',input_type:'text'}]} values={{client_id:clientId||googleClientId}} disabled={Boolean(busy)||Boolean(attempt)} onChange={(_,value)=>setClientId(value)}/>
      {attempt?<p role="status">{ja?'ブラウザでの認証を待っています…':'Waiting for browser sign-in…'}</p>:<button type="submit" disabled={Boolean(busy)||!(clientId||googleClientId).trim()}>{ja?'Googleで続行':'Continue with Google'}</button>}</>}
      <button type="button" onClick={()=>{authGeneration.current++;if(attempt)void api(`/connectors/oauth/${encodeURIComponent(attempt.id)}`,{method:'DELETE'}).catch(()=>{});setAttempt(null);setEditing('');setStorefrontToken('')}}>{ja?'キャンセル':'Cancel'}</button>
     </form>}
    </details>
   </div>;
  })}</div>
  {category.id==='shopping'&&<p className="setting-hint">{ja?'注文内容を確認後、店舗のサイトで購入を確定します。':'Review each order, then complete payment on the merchant website.'}</p>}
  </section>)}
  {status&&<p role="status">{status}</p>}{error&&<Feedback kind="error">{error}</Feedback>}
 </section>;
}
