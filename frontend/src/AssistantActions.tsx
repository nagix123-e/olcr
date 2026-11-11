import {useEffect,useRef,useState} from 'react';
import {API} from './api';
import {native,openExternal} from './native';
import {AssistantApi,Timer,assistantChanged,assistantError} from './assistantBridge';
import './assistant-actions.css';

export type AssistantBlock={type:'assistant_action';id:string;search_url?:string;autoplay?:boolean;kind:'clarification'|'setup'|'timers'|'calendar_events'|'calendar_action'|'media'|'media_control';conversation_id?:string;created_at?:number;timers?:Timer[];action?:any;events?:any[];more?:boolean;calendar_id?:string;time_zone?:string;operation?:string;proposed_start?:string;start?:string;end?:string;candidates?:Array<{id:string;title:string;url:string;channel?:string}>;command?:'play'|'pause'|'stop'|'status';service?:string;error?:string};
export type AssistantMessage={id:string;role:string;content:string;blocks:AssistantBlock[]};
type Props={block:AssistantBlock;api:AssistantApi;ja:boolean;onMessage?:(message:AssistantMessage)=>void};
const startedAt=Date.now()/1000,executed=new Set<string>();
let mediaOwner='',mediaController:((command:string)=>void)|null=null,mediaState='idle';
function broadcastMedia(){window.dispatchEvent(new Event('olcr-assistant-media-state'));}
function setMediaState(state:string){mediaState=state;broadcastMedia();}

function TimerView({initial,api,ja}:{initial:Timer;api:AssistantApi;ja:boolean}){
 const [timer,setTimer]=useState(initial),[now,setNow]=useState(Date.now()/1000),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 useEffect(()=>{assistantChanged();},[initial.id]);
 useEffect(()=>{let live=true;const refresh=()=>void api('/assistant/timers/'+encodeURIComponent(initial.id)).then(next=>{if(live)setTimer(next);}).catch(()=>{});refresh();const poll=window.setInterval(refresh,2500),clock=window.setInterval(()=>setNow(Date.now()/1000),250);return()=>{live=false;window.clearInterval(poll);window.clearInterval(clock);};},[initial.id,api]);
 const remaining=Math.max(0,Math.ceil(timer.status==='running'&&timer.deadline!==null?timer.deadline-now:timer.remaining));
 const finished=timer.status==='finished'||(timer.status==='running'&&remaining===0);
 const status=finished?(ja?'終了':'Finished'):({running:ja?'実行中':'Running',paused:ja?'一時停止中':'Paused',cancelled:ja?'停止済み':'Cancelled'}[timer.status]||timer.status);
 const control=async(action:string)=>{setBusy(true);setError('');try{setTimer(await api('/assistant/timers/'+encodeURIComponent(timer.id)+'/control',{method:'POST',body:JSON.stringify({action})}));assistantChanged();}catch(e){setError(assistantError(e,ja));}finally{setBusy(false);}};
 return <div className="assistant-timer"><div className="assistant-action-heading"><strong>{timer.name}</strong><span>{status}</span></div>
  {timer.kind==='alarm'&&timer.deadline!==null&&<p>{new Intl.DateTimeFormat(ja?'ja-JP':'en-US',{dateStyle:'medium',timeStyle:'short'}).format(new Date(timer.deadline*1000))}</p>}
  <output className="assistant-countdown" aria-label={ja?'残り時間':'Time remaining'}>{Math.floor(remaining/3600)>0?`${Math.floor(remaining/3600)}:`:''}{String(Math.floor(remaining/60)%60).padStart(2,'0')}:{String(remaining%60).padStart(2,'0')}</output>
  {!finished&&['running','paused'].includes(timer.status)&&<div className="assistant-action-buttons">{timer.kind!=='alarm'&&<button type="button" disabled={busy} onClick={()=>void control(timer.status==='paused'?'resume':'pause')}>{timer.status==='paused'?(ja?'再開':'Resume'):(ja?'一時停止':'Pause')}</button>}<button type="button" disabled={busy} onClick={()=>void control('cancel')}>{ja?'停止':'Cancel'}</button></div>}
  {timer.status==='running'&&timer.notification_status!=='scheduled'&&<p className="setting-hint" role="status">{timer.notification_status==='denied'?(ja?'macOSの通知が許可されていません。システム設定の「通知」でOLCRを許可してください。':'macOS notifications are not allowed. Enable OLCR in System Settings → Notifications.'):timer.notification_status==='unavailable'?(ja?'macOS版OLCRで通知できます。':'Notifications require OLCR for macOS.'):timer.notification_status==='failed'?(ja?'通知を登録できませんでした。OLCRを開いている間は残り時間を表示します。':'The notification could not be scheduled. Keep OLCR open to see the countdown.'):(ja?'macOSへの通知登録を確認中…':'Scheduling the macOS notification…')}</p>}
  {error&&<p role="alert">{error}</p>}
 </div>;
}

const localDateTime=(value:string,zone:string)=>{
 if(!value)return '';
 if(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value))return value;
 try{const parts=new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value));return parts.replace(' ','T');}catch{return '';}
};
function CalendarAction({initial,api,ja,onMessage}: {initial:any;api:AssistantApi;ja:boolean;onMessage?:Props['onMessage']}){
 const [action,setAction]=useState(initial),[payload,setPayload]=useState(initial.payload),[busy,setBusy]=useState(false),[error,setError]=useState(''),[loaded,setLoaded]=useState(false);
 useEffect(()=>{let live=true;void api(`/assistant/calendar/actions/${encodeURIComponent(initial.id)}?conversation_id=${encodeURIComponent(initial.conversation_id)}`).then(next=>{if(live){setAction(next);setPayload(next.payload);setLoaded(true);}}).catch(e=>{if(live){setError(assistantError(e,ja));setLoaded(true);}});return()=>{live=false};},[initial.id,initial.conversation_id,api,ja]);
 const pending=action.status==='pending'&&action.expires_at>Date.now()/1000;
 const cancel=async()=>{setBusy(true);setError('');try{setAction(await api(`/assistant/calendar/actions/${encodeURIComponent(action.id)}/cancel`,{method:'POST',body:JSON.stringify({conversation_id:action.conversation_id,revision:action.revision})}));}catch(e){setError(assistantError(e,ja));}finally{setBusy(false);}};
 const confirm=async()=>{setBusy(true);setError('');try{const result=await api(`/assistant/calendar/actions/${encodeURIComponent(action.id)}/confirm`,{method:'POST',body:JSON.stringify({conversation_id:action.conversation_id,revision:action.revision,payload})});setAction(result.action||result);onMessage?.(result.message);assistantChanged();}catch(e){setError(assistantError(e,ja));try{setAction(await api(`/assistant/calendar/actions/${encodeURIComponent(action.id)}?conversation_id=${encodeURIComponent(action.conversation_id)}`));}catch{}}finally{setBusy(false);}};
 if(!loaded||action.status==='completed')return null;
 return <form className="assistant-calendar-action" onSubmit={e=>{e.preventDefault();void confirm();}}>
  <strong>{action.action==='delete'?(ja?'予定の削除を確認':'Review event deletion'):action.action==='update'?(ja?'予定の変更を確認':'Review event changes'):(ja?'予定の追加を確認':'Review new event')}</strong>
  <label>{ja?'タイトル':'Title'}<input value={payload.summary||''} disabled={!pending||busy||action.action==='delete'} required maxLength={200} onChange={e=>setPayload({...payload,summary:e.target.value})}/></label>
  <div className="assistant-datetime-fields">{(['start','end'] as const).map(key=><label key={key}>{key==='start'?(ja?'開始':'Start'):(ja?'終了':'End')}<input type={payload.all_day?'date':'datetime-local'} value={payload.all_day?payload[key]||'':localDateTime(payload[key],payload.timeZone)} required disabled={!pending||busy||action.action==='delete'} onChange={e=>setPayload({...payload,[key]:e.target.value})}/></label>)}</div>
  {(['location','description','url'] as const).filter(key=>payload[key]!==undefined).map(key=><label key={key}>{key==='location'?(ja?'場所':'Location'):key==='description'?(ja?'メモ':'Notes'):'URL'}<textarea value={payload[key]||''} disabled={!pending||busy||action.action==='delete'} onChange={e=>setPayload({...payload,[key]:e.target.value})}/></label>)}
  <small>{payload.timeZone} · {action.calendar_id}{action.action==='delete'&&(ja?' · この予定のみ':' · This occurrence only')}</small>
  {pending?<div className="assistant-action-buttons"><button type="submit" className={action.action==='delete'?'destructive':'primary'} disabled={busy}>{busy?(ja?'処理中…':'Applying…'):action.action==='delete'?(ja?'この予定を削除':'Delete this event'):action.action==='update'?(ja?'変更を反映':'Apply changes'):(ja?'この内容で登録':'Add this event')}</button><button type="button" disabled={busy} onClick={()=>void cancel()}>{ja?'キャンセル':'Cancel'}</button></div>:<p role="status">{action.status==='completed'?(ja?'反映済みです。':'Applied.'):action.status==='cancelled'?(ja?'取り消しました。':'Cancelled.'):action.status==='uncertain'?(ja?'操作結果を確認できませんでした。重複実行を避けるため、カレンダーで予定を確認してください。':'The result could not be confirmed. Check your calendar before trying another change.'):action.status==='executing'?(ja?'処理中です。':'Processing.'):(ja?'この確認画面は有効ではありません。予定を取得し直してください。':'This preview is no longer active. Fetch the event again.')}</p>}
  {action.provider==='apple'&&action.status==='completed'&&<button type="button" onClick={()=>void native()?.core.invoke('assistant_open_calendar',{target:'calendar'}).catch(e=>setError(assistantError(e,ja)))}>{ja?'カレンダーで開く':'Open Calendar'}</button>}
  {action.result?.htmlLink&&<button type="button" onClick={()=>void openExternal(action.result.htmlLink).catch(e=>setError(assistantError(e,ja)))}>{ja?'カレンダーで開く':'Open Calendar'}</button>}
  {error&&<p role="alert">{error}</p>}
 </form>;
}

function CalendarEvents({block,api,ja,onMessage}:Props){
 const [error,setError]=useState(''),[busy,setBusy]=useState(false);
 const prepare=async(event:any,action:string)=>{setBusy(true);setError('');try{const result=await api('/assistant/calendar/actions',{method:'POST',body:JSON.stringify({conversation_id:block.conversation_id,calendar_id:block.calendar_id,action,event_id:event.id,...(action==='update'&&block.proposed_start?{payload:{start:block.proposed_start}}:{})})});onMessage?.(result.message);}catch(e){setError(assistantError(e,ja));}finally{setBusy(false);}};
 const format=(event:any)=>{const value=event.start?.dateTime||event.start?.date||'';if(!event.start?.dateTime)return value;try{return new Intl.DateTimeFormat(ja?'ja-JP':'en-US',{timeZone:block.time_zone,dateStyle:'medium',timeStyle:'short'}).format(new Date(value));}catch{return value;}};
 return <div className="assistant-calendar-events">{block.events?.length?<ul>{block.events.map(event=><li key={event.id}><div><strong>{event.summary||(ja?'タイトルなし':'Untitled event')}</strong><span>{format(event)}</span></div><div className="assistant-action-buttons"><button type="button" disabled={busy} onClick={()=>void prepare(event,'update')}>{ja?'変更':'Change'}</button><button type="button" disabled={busy} onClick={()=>void prepare(event,'delete')}>{ja?'削除':'Delete'}</button></div></li>)}</ul>:<p>{ja?'この期間の予定はありません。':'No events in this date range.'}</p>}{block.more&&<p className="setting-hint">{ja?'100件まで表示しています。期間を短くして取得し直せます。':'Showing up to 100 events. Request a shorter date range for more detail.'}</p>}{error&&<p role="alert">{error}</p>}</div>;
}

function useMediaState(){const [state,setState]=useState(mediaState);useEffect(()=>{const change=()=>setState(mediaState);window.addEventListener('olcr-assistant-media-state',change);return()=>window.removeEventListener('olcr-assistant-media-state',change);},[]);return state;}
function MediaCard({block,api,ja}:Props){
 const choices=(block.candidates||[]).slice(0,5),frame=useRef<HTMLIFrameElement>(null),generation=useRef(0);
 const inView=useRef(true),playerReady=useRef(false),wantPlay=useRef(false);
 const [thumbnails,setThumbnails]=useState(false);
 const [selected,setSelected]=useState(0),[player,setPlayer]=useState<{ticket:string;url:string}|null>(null),[state,setState]=useState('idle'),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 // Fullscreen is controlled by YouTube's own iframe controls so exiting fullscreen
 // returns directly to the same OLCR player without replacing or reloading it.
 useEffect(()=>{
  let live=true;
  const policy=()=>{void api('/assistant/settings').then(settings=>{if(live)setThumbnails(settings?.external_access_enabled===true);}).catch(()=>{if(live)setThumbnails(false);});};
  policy();window.addEventListener('olcr-command-complete',policy);
  return()=>{live=false;window.removeEventListener('olcr-command-complete',policy);};
 },[block.id]);
 const command=(cmd:string)=>{if(!player)return;if(cmd==='play'){wantPlay.current=true;if(document.hidden||!playerReady.current)return;if(!inView.current){window.requestAnimationFrame(()=>{if(wantPlay.current)frame.current?.scrollIntoView?.({block:'nearest'});});return;}wantPlay.current=false;}else{wantPlay.current=false;}frame.current?.contentWindow?.postMessage({ticket:player.ticket,command:cmd},new URL(player.url).origin);};
 const start=async(index=selected)=>{const sequence=++generation.current;executed.add(block.id);setBusy(true);setError('');try{const result=await api('/assistant/media/player',{method:'POST',body:JSON.stringify({video_id:choices[index].id})});if(sequence!==generation.current)return;mediaOwner=block.id;broadcastMedia();setSelected(index);playerReady.current=false;wantPlay.current=true;inView.current=typeof IntersectionObserver==='undefined';setPlayer({ticket:result.ticket,url:new URL(result.path,new URL(API).origin).href});setState('loading');}catch(e){if(sequence===generation.current)setError(assistantError(e,ja));}finally{if(sequence===generation.current)setBusy(false);}};
 useEffect(()=>{const timer=window.setTimeout(()=>{if(block.autoplay!==false&&block.created_at&&block.created_at>=startedAt&&!executed.has(block.id)&&choices.length&&!document.hidden){executed.add(block.id);void start(0);}},0);return()=>window.clearTimeout(timer);},[block.id]);
 useEffect(()=>()=>{generation.current++;if(mediaOwner===block.id){mediaOwner='';mediaController=null;setMediaState('idle');}},[block.id]);
 useEffect(()=>{const other=()=>{if(mediaOwner!==block.id){generation.current++;setPlayer(null);setState('idle');setBusy(false);}};window.addEventListener('olcr-assistant-media-state',other);return()=>window.removeEventListener('olcr-assistant-media-state',other);},[block.id]);
 useEffect(()=>{
  if(!player)return;
  mediaController=command;
  const message=(event:MessageEvent)=>{if(event.source!==frame.current?.contentWindow||event.origin!==new URL(player.url).origin||event.data?.olcrPlayer!==player.ticket)return;const next=event.data.state;setState(next);setMediaState(next);if(next==='ready'){playerReady.current=true;setError('');if(wantPlay.current&&!document.hidden&&inView.current)command('play');}if(next==='error')setError(ja?'この動画は埋め込み再生できません。YouTubeで開いて再生してください。':'This video cannot be played here. Open it on YouTube.');};
  const visibility=()=>{if(document.hidden)command('pause');};
  let live=true;
  const policy=()=>{void api('/assistant/settings').then(state=>{if(live&&state?.external_access_enabled===false){command('stop');setPlayer(null);setState('idle');setMediaState('idle');}}).catch(()=>command('pause'));};
  window.addEventListener('message',message);document.addEventListener('visibilitychange',visibility);
  window.addEventListener('olcr-command-complete',policy);
  const unlisten=native()?.event?.listen('tauri://blur',()=>command('pause'));
  // Pause when this card scrolls out of view, and when the containing chat unmounts.
  const observer=typeof IntersectionObserver!=='undefined'?new IntersectionObserver(entries=>{if(!entries[0])return;inView.current=entries[0].isIntersecting;if(!inView.current)command('pause');else if(wantPlay.current&&playerReady.current)command('play');},{threshold:0.1}):null;
  if(frame.current)observer?.observe(frame.current);
  const timeout=window.setTimeout(()=>{if(!playerReady.current){setState('error');setError(ja?'プレイヤーを読み込めませんでした。YouTubeで開いて再生できます。':'The player did not load. You can open the video on YouTube.');}},15000);
  return()=>{live=false;command('pause');window.clearTimeout(timeout);observer?.disconnect();void unlisten?.then(stop=>stop()).catch(()=>{});window.removeEventListener('message',message);window.removeEventListener('olcr-command-complete',policy);document.removeEventListener('visibilitychange',visibility);if(mediaOwner===block.id)mediaController=null;};
 },[player,ja]);
 const label=({idle:ja?'再生待ち':'Ready to play',loading:ja?'読み込み中…':'Loading…',ready:ja?'準備完了':'Ready',cued:ja?'再生待ち':'Ready to play',stopped:ja?'停止中':'Stopped',unstarted:ja?'再生待ち':'Ready to play',playing:ja?'再生中':'Playing',paused:ja?'一時停止中':'Paused',buffering:ja?'読み込み中…':'Buffering…',ended:ja?'再生終了':'Ended',blocked:ja?'プレイヤーの再生ボタンを押してください':'Press Play in the player',error:ja?'再生できません':'Playback unavailable'} as Record<string,string>)[state]||state;
 return <div className="assistant-media"><div className="assistant-action-heading"><strong>YouTube</strong><span role="status">{label}</span></div>
  <ol className="assistant-video-results" aria-label={ja?'YouTubeの検索結果':'YouTube search results'}>{choices.map((choice,index)=><li key={choice.id}>
   <button type="button" className="assistant-video-candidate" aria-label={`${ja?'再生':'Play'}: ${choice.title}`} aria-pressed={!!player&&selected===index} disabled={busy} onClick={()=>void start(index)}>
    <span className="assistant-video-thumbnail">{thumbnails&&/^[A-Za-z0-9_-]{11}$/.test(choice.id)&&<img src={`https://i.ytimg.com/vi/${choice.id}/hqdefault.jpg`} alt="" loading="lazy" referrerPolicy="no-referrer" onError={event=>{event.currentTarget.style.visibility='hidden';}}/>}<span className="assistant-video-play" aria-hidden="true">▶</span><span className="assistant-video-rank" aria-hidden="true">{index+1}</span></span>
    <span className="assistant-video-title">{choice.title}</span>{choice.channel&&<span className="assistant-video-channel">{choice.channel}</span>}
   </button>
  </li>)}</ol>
  {player&&<div className="assistant-player-stage">
   <iframe className="assistant-youtube-player" ref={frame} src={player.url} title={choices[selected]?.title||'YouTube'} allow="autoplay; encrypted-media; picture-in-picture; fullscreen" referrerPolicy="strict-origin-when-cross-origin" allowFullScreen/>
  </div>}
  {error&&<p role="alert">{error}</p>}
 </div>;
}

function MediaControl({block,ja}:Props){
 const state=useMediaState(),[missing,setMissing]=useState(false);
 useEffect(()=>{if(!block.created_at||block.created_at<startedAt||executed.has(block.id))return;executed.add(block.id);if(!mediaController||!mediaOwner){setMissing(true);return;}if(block.command&&block.command!=='status')mediaController(block.command);},[block.id,block.created_at,block.command]);
 const labels:Record<string,string>={idle:ja?'再生待ち':'Ready to play',ready:ja?'再生準備完了':'Ready',cued:ja?'再生待ち':'Ready to play',stopped:ja?'停止中':'Stopped',unstarted:ja?'再生待ち':'Ready to play',playing:ja?'再生中':'Playing',paused:ja?'一時停止中':'Paused',ended:ja?'再生終了':'Ended',buffering:ja?'読み込み中':'Buffering',blocked:ja?'プレイヤーで再生を開始してください':'Start playback in the player',error:ja?'再生エラー':'Playback error'};
 return <p role="status">{missing?(ja?'この会話で先に動画を再生してください。':'Start a video in this chat first.'):labels[state]||state}</p>;
}

export function AssistantActionBlockView(props:Props){
 const {block,api,ja,onMessage}=props;
 if(block.kind==='clarification')return null;
 return <section className="assistant-action" aria-label={ja?'チャットの操作':'Chat action'}>
  {block.kind==='timers'&&block.timers?.map(timer=><TimerView initial={timer} key={timer.id} api={api} ja={ja}/>)}
  {block.kind==='calendar_action'&&block.action&&<CalendarAction initial={block.action} api={api} ja={ja} onMessage={onMessage}/>}
  {block.kind==='calendar_events'&&<CalendarEvents {...props}/>}
  {block.kind==='media'&&<MediaCard {...props}/>}
  {block.kind==='media_control'&&<MediaControl {...props}/>}
  {block.kind==='setup'&&(block.service==='youtube'&&block.search_url?.startsWith('https://www.youtube.com/results?')?<button type="button" onClick={()=>void openExternal(block.search_url!)}>{ja?'YouTubeで検索して再生':'Search and play on YouTube'}</button>:<button type="button" onClick={()=>window.dispatchEvent(new Event('olcr-open-assistant-settings'))}>{ja?'設定を開く':'Open Settings'}</button>)}
 </section>;
}
