import {useEffect} from 'react';
import {api} from './api';
import {native} from './native';

export type AssistantApi = (path:string, init?:RequestInit)=>Promise<any>;
export type Timer = {id:string;kind?:'timer'|'alarm';name:string;duration:number;deadline:number|null;remaining:number;status:string;notification_status:string};
export function assistantChanged(){window.dispatchEvent(new Event('olcr-assistant-changed'));}
export function assistantError(error:unknown, ja:boolean){
 const text=error instanceof Error?error.message:String(error);
 try{const detail=JSON.parse(text.slice(text.indexOf('{')));if(typeof detail.message==='string')return detail.message;}catch{/* Native errors are translated below. */}
 if(/credential|Keychain|keychain/.test(text))return ja?'キーチェーンへの保存を完了できませんでした。macOSの確認画面を確認して再試行してください。':'Keychain storage did not complete. Check the macOS prompt and try again.';
 return ja?'操作を完了できませんでした。接続と設定を確認して再試行してください。':'Could not complete the action. Check the connection and settings, then try again.';
}

const saving=new Map<AssistantApi,Promise<void>>();
export function persistCalendar(request:AssistantApi,interactive=false):Promise<void>{
 const current=saving.get(request);if(current)return interactive?current.catch(()=>{}).then(()=>persistCalendar(request,true)):current;
 const work=(async()=>{
  const bridge=native();if(!bridge)throw Error('Native Keychain required');
  const secret=await request('/assistant/calendar/credential/export',{method:'POST'});
  await bridge.core.invoke('set_external_credential',{handle:secret.handle,value:secret.value,interactive});
  await request('/assistant/calendar/credential/ack',{method:'POST',body:JSON.stringify({generation:secret.generation})});
 })();saving.set(request,work);void work.finally(()=>saving.delete(request)).catch(()=>{});return work;
}

/** No visible UI. Timer delivery belongs to macOS, outside any chat component. */
export function useAssistantBridge(enabled:boolean,language:'ja'|'en'){
 useEffect(()=>{
  if(!enabled)return;
  let live=true,busy=false,queued=false,fingerprint='',settingsAt=0;
  const refresh=async()=>{
   if(!live)return;if(busy){queued=true;return;}busy=true;
   try{
    const data=await api('/assistant/timers');if(!live||!Array.isArray(data?.timers))return;
    const timers=(data.timers as Timer[]).filter(t=>t.deadline!==null&&(t.status==='running'||(t.status==='finished'&&['pending','failed'].includes(t.notification_status)&&t.deadline>Date.now()/1000-30))).map(t=>({id:t.id,name:t.name,deadline:t.deadline,kind:t.kind||'timer'}));
    const cancelledIds=(data.timers as Timer[]).filter(t=>['paused','cancelled'].includes(t.status)).slice(0,200).map(t=>t.id);
    const signature=JSON.stringify({timers,cancelledIds,language});
    if(signature!==fingerprint){
     const bridge=native();
     if(bridge){
      let receipt:any;
      try{receipt=await bridge.core.invoke('assistant_sync_notifications',{timers,cancelledIds,language});}
      catch{receipt={status:'failed',scheduled_ids:[],failed_ids:timers.map(t=>t.id)};}
      if(receipt?.scheduled_ids?.length)await api('/assistant/notifications',{method:'POST',body:JSON.stringify({ids:receipt.scheduled_ids,status:'scheduled'})});
      if(receipt?.failed_ids?.length)await api('/assistant/notifications',{method:'POST',body:JSON.stringify({ids:receipt.failed_ids,status:receipt.status==='denied'?'denied':'failed'})});
      // Retry transient native failures; permission denial needs a Settings change.
      if(receipt.status!=='failed')fingerprint=signature;
     }else{
      if(timers.length)await api('/assistant/notifications',{method:'POST',body:JSON.stringify({ids:timers.map(t=>t.id),status:'unavailable'})});
      fingerprint=signature;
     }
    }
    if(Date.now()-settingsAt>10000){
     settingsAt=Date.now();const state=await api('/assistant/settings');if(!live||!state)return;
     if(!state.time_zone_configured)await api('/assistant/preferences',{method:'PUT',body:JSON.stringify({calendar_id:state.preferences?.calendar_id||'primary',time_zone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'})});
     if(state.calendar?.credential_pending&&native())await persistCalendar(api,false).catch(()=>{});
    }
   }catch{/* Inline controls and Settings show actionable errors; retry the passive sync. */}
   finally{busy=false;if(queued&&live){queued=false;void refresh();}}
  };
  const changed=()=>{settingsAt=0;void refresh();};
  void refresh();const timer=window.setInterval(()=>void refresh(),2000);
  const focused=()=>{fingerprint='';changed();};
  window.addEventListener('olcr-assistant-changed',changed);window.addEventListener('focus',focused);
  return()=>{live=false;window.clearInterval(timer);window.removeEventListener('olcr-assistant-changed',changed);window.removeEventListener('focus',focused);};
 },[enabled,language]);
}
