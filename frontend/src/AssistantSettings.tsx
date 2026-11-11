import {useEffect,useRef,useState} from 'react';
import {native} from './native';
import {useLanguage} from './language';
import {AssistantApi,assistantChanged,assistantError} from './assistantBridge';
import {UiIcon} from './UiIcon';

export function AssistantSettings({api,embedded=false,categoryEnabled=true}:{api:AssistantApi;embedded?:boolean;categoryEnabled?:boolean}){
 const {language}=useLanguage(),ja=language==='ja';
 const refreshSequence=useRef(0);
 const [state,setState]=useState<any>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[statusMessage,setStatusMessage]=useState('');
 const [calendars,setCalendars]=useState<any[]>([]),[calendarId,setCalendarId]=useState('primary');
 const [zone,setZone]=useState(Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC');
 const [icon,setIcon]=useState('');
 const accept=(result:any)=>{if(!result?.calendar||!result?.preferences)throw Error('Assistant settings unavailable');setState(result);setCalendarId(result.preferences.calendar_id);if(result.time_zone_configured)setZone(result.preferences.time_zone);};
 const refresh=async()=>{const sequence=++refreshSequence.current;const result=await api('/assistant/settings');if(sequence===refreshSequence.current)accept(result);};
 useEffect(()=>{let live=true;const load=()=>{const sequence=++refreshSequence.current;void api('/assistant/settings').then(result=>{if(live&&sequence===refreshSequence.current)accept(result);}).catch(e=>{if(live)setError(assistantError(e,ja));});};load();window.addEventListener('focus',load);return()=>{live=false;refreshSequence.current++;window.removeEventListener('focus',load)};},[api,ja]);
 useEffect(()=>{if(!embedded)return;let live=true;void api('/assistant/calendar/icon').then(result=>{if(live&&typeof result?.png_base64==='string'&&result.png_base64.length<2_000_000)setIcon(result.png_base64)}).catch(()=>{});return()=>{live=false};},[api,embedded]);
 useEffect(()=>{if(!state?.calendar?.connected||!categoryEnabled){setCalendars([]);return;}let live=true;void api('/assistant/calendar/calendars').then(result=>{if(live)setCalendars(result.calendars||[]);}).catch(e=>{if(live)setError(assistantError(e,ja));});return()=>{live=false};},[state?.calendar?.connected,categoryEnabled,api,ja]);
 const run=async(work:()=>Promise<unknown>)=>{setBusy(true);setError('');setStatusMessage('');try{await work();await refresh();assistantChanged();setStatusMessage(ja?'接続設定を更新しました。':'Connection settings updated.');}catch(e){setError(assistantError(e,ja));}finally{setBusy(false);}};
 const open=(target:'privacy')=>run(async()=>{const bridge=native();if(!bridge)throw Error('macOS Calendar unavailable');await bridge.core.invoke('assistant_open_calendar',{target});});
 const connect=async()=>{
  try{await api('/assistant/calendar/connect',{method:'POST'});}catch(error){await refresh().catch(()=>{});throw error;}
  await api('/assistant/preferences',{method:'PUT',body:JSON.stringify({calendar_id:calendarId,time_zone:zone})});
 };
 const toggle=(enabled:boolean)=>void run(async()=>{
  if(enabled)await connect();
  else await api('/assistant/calendar/disconnect',{method:'POST'});
 });
 const denied=['denied','restricted','write_only'].includes(state?.calendar?.permission);
 const connected=Boolean(state?.calendar?.connected);
 const stateLabel=connected?(!categoryEnabled?(ja?'接続済み・カテゴリ許可オフ':'Connected · category access off'):(ja?'接続済み':'Connected')):denied?(ja?'macOSの許可が必要':'macOS permission required'):(ja?'未接続':'Not connected');
 const identity=<span className="service-connector-identity">
  <span className="service-brand-icon apple-calendar-brand">{icon?<img src={`data:image/png;base64,${icon}`} alt=""/>:<UiIcon name="calendar" size={24}/>}</span>
  <span>{ja?'Appleカレンダー':'Apple Calendar'}<small>{state?stateLabel:(ja?'接続状態を確認中…':'Checking connection…')}</small></span>
 </span>;
 const switchControl=<input type="checkbox" role="switch" aria-label={ja?'Appleカレンダー接続':'Apple Calendar connection'} checked={connected} disabled={!state||busy||state.calendar.available===false||denied||(!categoryEnabled&&!connected)} onChange={event=>toggle(event.target.checked)}/>;
 const details=<details className={`settings-disclosure assistant-calendar-details${embedded?' service-connector-details':''}`}>
   <summary>{ja?'接続設定':'Connection settings'}</summary>
   <div className="settings-disclosure-content">
    {denied&&<p className="setting-hint">{ja?'macOSの「プライバシーとセキュリティ」→「カレンダー」でOLCRのアクセスを許可してください。':'Allow OLCR in macOS Privacy & Security → Calendars.'}<button type="button" disabled={busy} onClick={()=>void open('privacy')}>{ja?'macOSの設定を開く':'Open macOS Settings'}</button></p>}
    {state?.calendar?.available===false&&<p role="status">{ja?'カレンダー機能を使うには、更新したOLCRを開き直してください。':'Reopen the updated OLCR app to use Calendar.'}</p>}
    {!categoryEnabled&&!connected&&<p className="setting-hint">{ja?'先に「カレンダー」のカテゴリ許可をオンにしてください。':'Turn on Calendar category access before connecting this service.'}</p>}
    {categoryEnabled&&!connected&&!denied&&state?.calendar?.available!==false&&<p className="setting-hint">{ja?'スイッチをオンにすると、このMacのカレンダーへのアクセスをmacOSに確認します。APIキーは不要です。':'Turn on the switch to let macOS ask for access to calendars on this Mac. No API key is needed.'}</p>}
    {connected&&<>
     <p className="setting-hint">{ja?'Macのカレンダーアプリに表示される予定表を使えます。予定の追加・変更・削除は、チャット内で内容を確認してから反映します。':'Use calendars shown in the Mac Calendar app. Review additions, changes and deletions in chat before applying them.'}</p>
     <form onSubmit={e=>{e.preventDefault();void run(()=>api('/assistant/preferences',{method:'PUT',body:JSON.stringify({calendar_id:calendarId,time_zone:zone})}));}}>
      <label>{ja?'使用するカレンダー':'Calendar'}<select value={calendarId} onChange={e=>setCalendarId(e.target.value)}><option value="primary">{ja?'Macのデフォルト':'Mac default'}</option>{calendars.filter(c=>c.id!=='primary').map(c=><option key={c.id} value={c.id}>{c.title}{!c.writable?(ja?'（読み取り専用）':' (read only)'):''}</option>)}</select></label>
      <label>{ja?'タイムゾーン':'Time zone'}<input value={zone} onChange={e=>setZone(e.target.value)} spellCheck={false}/></label>
      <button type="submit" disabled={busy}>{ja?'カレンダー設定を保存':'Save calendar settings'}</button>
     </form>
    </>}
   </div>
  </details>;
 if(embedded)return <div className="service-connector-tile native-calendar-tile" aria-label={ja?'Appleカレンダー接続':'Apple Calendar connection'}>
   <div className="setting-row capability-setting"><div className="native-calendar-label">{identity}</div>{switchControl}</div>
   {details}{state?.calendar?.available===false&&<p className="setting-hint">{ja?'macOSのOLCRで利用できます。':'Available in the macOS version of OLCR.'}</p>}
   {error&&<p role="alert">{error}</p>}
   {statusMessage&&<p role="status">{statusMessage}</p>}
  </div>;
 return <section className="panel assistant-settings" aria-label={ja?'カレンダー':'Calendar'}>
  <h2>{ja?'カレンダー':'Calendar'}</h2>
  {state? <>
   <h3>{ja?'Macのカレンダー':'Mac Calendar'}</h3>
   <p className="setting-hint">{ja?'このMacのカレンダーに接続します。APIキーや開発者向けの設定は不要です。初回にmacOSの確認画面でアクセスを許可してください。':'Use this Mac’s calendars. No API key or developer setup is needed. Allow access in the macOS prompt the first time.'}</p>
   <div className="assistant-action-heading"><span>{stateLabel}</span>
    {!connected&&!denied&&<button type="button" className="primary" disabled={busy||state.calendar.available===false} onClick={()=>void run(connect)}>{busy?(ja?'接続中…':'Connecting…'):(ja?'カレンダーに接続':'Connect Calendar')}</button>}
    {connected&&<button type="button" disabled={busy} onClick={()=>void run(()=>api('/assistant/calendar/disconnect',{method:'POST'}))}>{ja?'接続を解除':'Disconnect'}</button>}
   </div>
   {denied&&<p className="setting-hint">{ja?'macOSの「プライバシーとセキュリティ」→「カレンダー」でOLCRのアクセスを許可してください。':'Allow OLCR in macOS Privacy & Security → Calendars.'}<button type="button" disabled={busy} onClick={()=>void open('privacy')}>{ja?'macOSの設定を開く':'Open macOS Settings'}</button></p>}
   {state.calendar.available===false&&<p role="status">{ja?'カレンダー機能を使うには、更新したOLCRを開き直してください。':'Reopen the updated OLCR app to use Calendar.'}</p>}
   <p className="setting-hint">{ja?'Macのカレンダーアプリに表示される予定表を使えます。予定の追加・変更・削除は、チャット内で内容を確認してから反映します。':'Use calendars shown in the Mac Calendar app. Review additions, changes and deletions in chat before applying them.'}</p>
   {connected&&details}
  </>:<p role="status">{ja?'接続状態を確認中…':'Checking connections…'}</p>}
  {error&&<p role="alert">{error}</p>}
 </section>;

}
