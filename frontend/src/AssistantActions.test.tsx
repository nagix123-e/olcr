import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {AssistantActionBlockView,type AssistantBlock} from './AssistantActions';
import {AssistantSettings} from './AssistantSettings';
import {configureApi} from './api';

vi.mock('./language',()=>({useLanguage:()=>({language:'en'})}));
const invoke=vi.hoisted(()=>vi.fn().mockResolvedValue(undefined));
vi.mock('./native',()=>({native:()=>({core:{invoke}}),openExternal:vi.fn().mockResolvedValue(undefined)}));
afterEach(()=>{cleanup();vi.useRealTimers();vi.clearAllMocks();});

describe('Everyday chat actions',()=>{
 it('shows fresh agent video search results without automatically playing them',async()=>{
  const request=vi.fn(async()=>({ticket:'ticket',path:'/assistant-player/ticket'}));
  render(<AssistantActionBlockView block={{type:'assistant_action',kind:'media',id:'search-only',created_at:Date.now()/1000+10,autoplay:false,candidates:[{id:'dQw4w9WgXcQ',title:'Python tutorial',url:'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}]}} api={request} ja={false}/>);
  await new Promise(resolve=>setTimeout(resolve,20));
  expect(request).not.toHaveBeenCalledWith('/assistant/media/player',expect.anything());expect(screen.queryByTitle('Python tutorial')).toBeNull();
  expect(screen.getByRole('button',{name:'Play: Python tutorial'})).toBeEnabled();
 });
 it('controls the specific persisted timer and explains denied macOS notification access',async()=>{
  let timer={id:'one',name:'Tea',duration:300,deadline:Date.now()/1000+300,remaining:300,status:'running',notification_status:'denied'};
  const request=vi.fn(async(path:string,init?:RequestInit)=>{if(init?.method==='POST'){const {action}=JSON.parse(String(init.body));timer={...timer,status:action==='pause'?'paused':'cancelled'};}return timer;});
  render(<AssistantActionBlockView block={{type:'assistant_action',id:'timer-message',kind:'timers',timers:[timer]}} api={request} ja={false}/>);
  expect(await screen.findByText(/macOS notifications are not allowed/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Pause'}));
  await screen.findByRole('button',{name:'Resume'});
  expect(request).toHaveBeenCalledWith('/assistant/timers/one/control',{method:'POST',body:JSON.stringify({action:'pause'})});
  fireEvent.click(screen.getByRole('button',{name:'Cancel'}));
  await waitFor(()=>expect(screen.getByText('Cancelled')).toBeInTheDocument());
 });

 it('counts down a persisted timer from its deadline and keeps alarms at their fixed time',async()=>{
  vi.useFakeTimers();vi.setSystemTime(new Date('2026-10-29T12:00:00Z'));
  const timer={id:'countdown',name:'Tea',duration:60,deadline:Date.now()/1000+60,remaining:60,status:'running',notification_status:'scheduled'};
  const request=vi.fn(async()=>timer);
  const view=render(<AssistantActionBlockView block={{type:'assistant_action',id:'countdown-message',kind:'timers',timers:[timer]}} api={request} ja={true}/>);
  await act(async()=>{});
  expect(screen.getByLabelText('残り時間')).toHaveTextContent('01:00');
  await act(async()=>{vi.advanceTimersByTime(5000)});
  expect(screen.getByLabelText('残り時間')).toHaveTextContent('00:55');
  view.unmount();
  const alarm={...timer,kind:'alarm' as const,name:'Wake up'};
  render(<AssistantActionBlockView block={{type:'assistant_action',id:'alarm-message',kind:'timers',timers:[alarm]}} api={vi.fn(async()=>alarm)} ja={false}/>);
  await act(async()=>{});
  expect(screen.queryByRole('button',{name:'Pause'})).toBeNull();
  expect(screen.getByRole('button',{name:'Cancel'})).toBeEnabled();
  expect(screen.getByText(/Oct 29, 2026/)).toBeInTheDocument();
 });

 it('confirms the parsed Japan-time event without editing any fields',async()=>{
  const action={id:'school',conversation_id:'chat',action:'create',provider:'apple',calendar_id:'home',revision:'rev1',status:'pending',expires_at:Date.now()/1000+600,payload:{summary:'学校に行く',start:'2026-10-30T08:00:00+09:00',end:'2026-10-30T09:00:00+09:00',timeZone:'Asia/Tokyo'}};
  const request=vi.fn(async(path:string,init?:RequestInit)=>path.endsWith('/confirm')?{action:{...action,status:'completed'}}:action);
  render(<AssistantActionBlockView block={{type:'assistant_action',kind:'calendar_action',id:'school-preview',action}} api={request} ja={true}/>);
  await waitFor(()=>expect(request).toHaveBeenCalledTimes(1));
  expect(screen.getByLabelText('開始')).toHaveValue('2026-10-30T08:00');
  expect(screen.getByLabelText('終了')).toHaveValue('2026-10-30T09:00');
  fireEvent.click(screen.getByRole('button',{name:'この内容で登録'}));
  await waitFor(()=>expect(screen.queryByRole('button',{name:'この内容で登録'})).toBeNull());
  expect(request).toHaveBeenCalledWith('/assistant/calendar/actions/school/confirm',{method:'POST',body:JSON.stringify({conversation_id:'chat',revision:'rev1',payload:action.payload})});
 });

 it('shows a calendar preview without writing and sends only the reviewed fields when confirmed',async()=>{
  const action={id:'draft',conversation_id:'chat',action:'create',calendar_id:'primary',revision:'rev1',status:'pending',expires_at:Date.now()/1000+600,payload:{summary:'Dentist',location:'Clinic',description:'Bring insurance card',url:'https://example.com/visit',start:'2026-10-27T15:00:00+09:00',end:'2026-10-27T16:00:00+09:00',timeZone:'Asia/Tokyo'}};
  const message={id:'result',role:'assistant',content:'Event added.',blocks:[]};
  const request=vi.fn(async(path:string,init?:RequestInit)=>path.endsWith('/confirm')?{action:{...action,status:'completed'},message}:action);
  const onMessage=vi.fn();
  render(<AssistantActionBlockView block={{type:'assistant_action',kind:'calendar_action',id:'preview',action}} api={request} ja={false} onMessage={onMessage}/>);
  await waitFor(()=>expect(request).toHaveBeenCalledTimes(1));
  expect(request.mock.calls.some(([path])=>path.endsWith('/confirm'))).toBe(false);
  expect(screen.getByLabelText('Location')).toHaveValue('Clinic');
  expect(screen.getByLabelText('Notes')).toHaveValue('Bring insurance card');
  expect(screen.getByLabelText('URL')).toHaveValue('https://example.com/visit');
  fireEvent.change(screen.getByLabelText('Title'),{target:{value:'Dentist checkup'}});
  fireEvent.change(screen.getByLabelText('Start'),{target:{value:'2026-10-27T15:30'}});
  fireEvent.click(screen.getByRole('button',{name:'Add this event'}));
  await waitFor(()=>expect(screen.queryByRole('button',{name:'Add this event'})).toBeNull());
  expect(request).toHaveBeenCalledWith('/assistant/calendar/actions/draft/confirm',{method:'POST',body:JSON.stringify({conversation_id:'chat',revision:'rev1',payload:{...action.payload,summary:'Dentist checkup',start:'2026-10-27T15:30'}})});
  expect(onMessage).toHaveBeenCalledWith(message);
 });

 it('does not replay an archived video request, and trusts playback state only from its own ticket frame',async()=>{
  configureApi('http://127.0.0.1:9999/api','test');
  const request=vi.fn(async()=>({ticket:'ticket',path:'/assistant-player/ticket'}));
  const block:AssistantBlock={type:'assistant_action',kind:'media',id:'archived',created_at:1,candidates:[{id:'dQw4w9WgXcQ',title:'Song',url:'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}]};
  render(<AssistantActionBlockView block={block} api={request} ja={false}/>);
  expect(request).not.toHaveBeenCalledWith('/assistant/media/player',expect.anything());
  fireEvent.click(screen.getByRole('button',{name:'Play: Song'}));
  const frame=await screen.findByTitle('Song');
  expect(frame).toHaveAttribute('src','http://127.0.0.1:9999/assistant-player/ticket');
  const source=(frame as HTMLIFrameElement).contentWindow;
  fireEvent(window,new MessageEvent('message',{source,origin:'https://attacker.test',data:{olcrPlayer:'ticket',state:'playing'}}));
  expect(screen.queryByRole('button',{name:'Pause'})).not.toBeInTheDocument();
  fireEvent(window,new MessageEvent('message',{source,origin:'http://127.0.0.1:9999',data:{olcrPlayer:'wrong-ticket',state:'playing'}}));
  expect(screen.queryByRole('button',{name:'Pause'})).not.toBeInTheDocument();
  fireEvent(window,new MessageEvent('message',{source,origin:'http://127.0.0.1:9999',data:{olcrPlayer:'ticket',state:'playing'}}));
  expect(await screen.findByText('Playing')).toBeInTheDocument();
  expect(frame).toHaveAttribute('allowfullscreen');
  expect(screen.queryByRole('button',{name:'Pause'})).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Stop'})).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Play fullscreen'})).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Open on YouTube'})).not.toBeInTheDocument();
 });

 it('shows the top five thumbnail candidates and starts the chosen video',async()=>{
  const request=vi.fn(async(path:string)=>path==='/assistant/settings'?{external_access_enabled:true}:{ticket:'chosen',path:'/assistant-player/chosen'});
  const candidates=Array.from({length:7},(_,i)=>({id:`abcdefghij${i}`,title:`Video ${i+1}`,channel:`Channel ${i+1}`,url:`https://www.youtube.com/watch?v=abcdefghij${i}`}));
  render(<AssistantActionBlockView block={{type:'assistant_action',kind:'media',id:'candidates',autoplay:false,candidates}} api={request} ja={false}/>);
  expect(screen.getAllByRole('button',{name:/Play: Video/})).toHaveLength(5);
  await waitFor(()=>expect(document.querySelectorAll('.assistant-video-thumbnail img')).toHaveLength(5));
  expect(document.querySelector('img')).toHaveAttribute('src','https://i.ytimg.com/vi/abcdefghij0/hqdefault.jpg');
  expect(screen.queryByRole('combobox')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'Play: Video 2'}));
  await screen.findByTitle('Video 2');
  expect(request).toHaveBeenCalledWith('/assistant/media/player',{method:'POST',body:JSON.stringify({video_id:'abcdefghij1'})});
  expect(screen.getByRole('button',{name:'Play: Video 2'})).toHaveAttribute('aria-pressed','true');
 });
 it('keeps remote thumbnails disabled when external access is off',async()=>{
  const request=vi.fn(async()=>({external_access_enabled:false}));
  render(<AssistantActionBlockView block={{type:'assistant_action',kind:'media',id:'private-results',autoplay:false,candidates:[{id:'dQw4w9WgXcQ',title:'Private',url:'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}]}} api={request} ja={false}/>);
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/assistant/settings'));
  expect(document.querySelector('img')).toBeNull();
 });
 it('delegates fullscreen and its exit to the YouTube iframe controls',async()=>{
  const request=vi.fn(async(path:string)=>path==='/assistant/settings'?{external_access_enabled:false}:{ticket:'same',path:'/assistant-player/same'});
  render(<AssistantActionBlockView block={{type:'assistant_action',kind:'media',id:'fullscreen',autoplay:false,candidates:[{id:'dQw4w9WgXcQ',title:'Same video',url:'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}]}} api={request} ja={true}/>);
  fireEvent.click(screen.getByRole('button',{name:'再生: Same video'}));
  const frame=await screen.findByTitle('Same video');
  expect(frame).toHaveAttribute('allowfullscreen');
  expect(screen.queryByRole('button',{name:'全画面で再生'})).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'全画面を終了'})).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'OLCRに戻る'})).not.toBeInTheDocument();
  expect(request.mock.calls.filter(([path])=>path==='/assistant/media/player')).toHaveLength(1);
 });

 it('connects Mac Calendar in one step without API credentials or YouTube setup',async()=>{
  let connected=false;
  const calendar=()=>({provider:'apple',native:true,connected,available:true,authorized:connected,permission:connected?'authorized':'not_determined'});
  const request=vi.fn(async(path:string,init?:RequestInit)=>{
   if(path==='/assistant/calendar/connect'){connected=true;return calendar()}
   if(path==='/assistant/calendar/calendars')return {calendars:[{id:'local',title:'Home',writable:true}]};
   if(path==='/assistant/preferences')return {};
   return {calendar:calendar(),preferences:{calendar_id:'primary',time_zone:'Asia/Tokyo'},time_zone_configured:true,youtube_search_configured:true};
  });
  render(<AssistantSettings api={request}/>);
  const connect=await screen.findByRole('button',{name:'Connect Calendar'});
  expect(screen.queryByLabelText('API key')).toBeNull();expect(screen.queryByLabelText('Client ID')).toBeNull();
  expect(screen.queryByRole('heading',{name:'YouTube'})).toBeNull();
  expect(screen.queryByRole('button',{name:'Open Calendar app'})).toBeNull();
  fireEvent.click(connect);await screen.findByText('Connected');
  expect(request).toHaveBeenCalledWith('/assistant/calendar/connect',{method:'POST'});
  expect(request).toHaveBeenCalledWith('/assistant/preferences',{method:'PUT',body:JSON.stringify({calendar_id:'primary',time_zone:'Asia/Tokyo'})});
  expect(invoke).not.toHaveBeenCalledWith('set_external_credential',expect.anything());
  expect(await screen.findByRole('option',{name:'Home'})).toBeInTheDocument();
 });
 it('opens macOS Calendar permissions when access was denied',async()=>{
  const request=vi.fn(async()=>({calendar:{provider:'apple',connected:false,available:true,permission:'denied'},preferences:{calendar_id:'primary',time_zone:'UTC'},time_zone_configured:true}));
  render(<AssistantSettings api={request}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Open macOS Settings'}));
  expect(screen.queryByRole('button',{name:'Connect Calendar'})).toBeNull();
  await waitFor(()=>expect(invoke).toHaveBeenCalledWith('assistant_open_calendar',{target:'privacy'}));
 });
 it('shows the permission recovery button immediately after declining the macOS prompt',async()=>{
  let denied=false;
  const request=vi.fn(async(path:string)=>{
   if(path==='/assistant/calendar/connect'){denied=true;throw Error('HTTP_422: {"message":"Calendar access denied"}')}
   return {calendar:{provider:'apple',connected:false,available:true,permission:denied?'denied':'not_determined'},preferences:{calendar_id:'primary',time_zone:'UTC'},time_zone_configured:true};
  });
  render(<AssistantSettings api={request}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Connect Calendar'}));
  expect(await screen.findByRole('button',{name:'Open macOS Settings'})).toBeEnabled();
  expect(await screen.findByRole('alert')).toHaveTextContent('Calendar access denied');
 });
});
