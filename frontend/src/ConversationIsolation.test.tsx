import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {App} from './main';
import {LanguageProvider} from './language';
import {configureApi} from './api';

vi.mock('./ScreenCapture',()=>({useScreenCapture:()=>({status:{running:false},targetApplication:null}),ScreenCapturePanel:()=>null}));
vi.mock('./brainModels',()=>({useBrainModels:()=>({refresh:vi.fn(),refreshUsage:vi.fn()})}));
vi.mock('./ChatComposer',()=>({ChatComposer:({text,setText,loading,disabled,send,pause,conversationId,setAttachment,editMode,onSubmitEdit,onCancelEdit}:any)=><form onSubmit={editMode?onSubmitEdit:send}><textarea aria-label="Draft" value={text} onChange={event=>setText(event.target.value)}/><button disabled={loading||disabled}>{editMode?'Save and resend':`Send ${conversationId||''}`}</button>{editMode&&<button type="button" onClick={onCancelEdit}>Cancel editing</button>}<button type="button" onClick={pause}>Stop</button><button type="button" onClick={()=>setAttachment({name:"note.txt",mimeType:"text/plain",content:"attached text"})}>Attach</button></form>}));
vi.mock('./ProjectContext',()=>({ProjectContext:()=>null}));
vi.mock('./BanterLoader',()=>({BanterLoader:()=>null}));
vi.mock('./ModelUiPreview',()=>({ModelUiPreview:()=>null}));

afterEach(()=>{cleanup();vi.unstubAllGlobals();sessionStorage.clear()});
const json=(value:unknown)=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
const deferred=<T,>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done});return {promise,resolve}};

describe('conversation isolation in the actual App',()=>{
 it.each(['success','failure-with-new-draft','failure-with-empty-draft'])('clears a skill command immediately and preserves the next draft: %s',async outcome=>{
  const source='/skill apple-design\n一週間の献立プランナーを作ってください。';
  const command=deferred<Response>();
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health'||path==='/settings')return json({});
   if(path==='/commands'){
    if(init?.method==='POST'){expect(JSON.parse(String(init.body)).text).toBe(source);return command.promise}
    return json({commands:[]});
   }
   if(path==='/projects')return json({projects:[]});
   if(path==='/conversations')return json({conversations:[{id:'A',title:'Conversation A'}]});
   if(path==='/conversations/A')return json({id:'A',messages:[]});
   throw Error(path);
  }));configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'Conversation A'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'Send A'})).toBeEnabled());
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:source}});
  fireEvent.click(screen.getByRole('button',{name:'Send A'}));
  expect(screen.getByLabelText('Draft')).toHaveValue('');
  expect(screen.getByRole('button',{name:'Send A'})).toBeDisabled();
  const nextDraft=outcome==='failure-with-empty-draft'?'':'次の依頼の下書き';
  if(nextDraft)fireEvent.change(screen.getByLabelText('Draft'),{target:{value:nextDraft}});
  await act(async()=>command.resolve(outcome==='success'?json({conversation_id:'A',kind:'skill_execution',message:'Skill result',data:{}}):
   new Response(JSON.stringify({detail:'fixture failure'}),{status:422,headers:{'Content-Type':'application/json'}})));
  await waitFor(()=>expect(screen.getByRole('button',{name:'Send A'})).toBeEnabled());
  expect(screen.getByLabelText('Draft')).toHaveValue(nextDraft||source);
  if(outcome==='success')expect(screen.getByText('Skill result')).toBeInTheDocument();
 });

 it('clears before creating a conversation and keeps subsequent typing through the reply',async()=>{
  const created=deferred<Response>(),command=deferred<Response>();
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health'||path==='/settings')return json({});
   if(path==='/commands')return init?.method==='POST'?command.promise:json({commands:[]});
   if(path==='/projects')return json({projects:[]});
   if(path==='/conversations')return init?.method==='POST'?created.promise:json({conversations:[]});
   if(path==='/conversations/C')return json({id:'C',messages:[]});
   throw Error(path);
  }));configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);await screen.findByText('READY');
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'/skill apple-design review this project'}});
  fireEvent.click(screen.getByRole('button',{name:'Send'}));
  expect(screen.getByLabelText('Draft')).toHaveValue('');
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'New draft while conversation is created'}});
  await act(async()=>created.resolve(json({id:'C',title:'Created C'})));
  await screen.findByRole('button',{name:'Send C'});
  expect(screen.getByLabelText('Draft')).toHaveValue('New draft while conversation is created');
  await act(async()=>command.resolve(json({conversation_id:'C',message:'Skill result',data:{}})));
  await waitFor(()=>expect(screen.getByRole('button',{name:'Send C'})).toBeEnabled());
  expect(screen.getByLabelText('Draft')).toHaveValue('New draft while conversation is created');
 });

 it('shows the prompt and stops while conversation creation is pending',async()=>{
  let signal:AbortSignal|undefined;let dispatched=false;
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health'||path==='/settings')return json({});
   if(path==='/commands'){if(init?.method==='POST')dispatched=true;return json({commands:[]})}
   if(path==='/projects')return json({projects:[]});
   if(path==='/conversations'){
    if(init?.method==='POST'){signal=init.signal!;return new Promise<Response>((_,reject)=>signal!.addEventListener('abort',()=>reject(new DOMException('Stopped','AbortError')),{once:true}))}
    return json({conversations:[]});
   }
   throw Error(path);
  }));configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);await screen.findByText('READY');
  const prompt='/skill apple-design review pending project';
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:prompt}});
  fireEvent.click(screen.getByRole('button',{name:'Send'}));
  expect(screen.getByText(prompt)).toBeInTheDocument();
  await waitFor(()=>expect(signal).toBeDefined());
  fireEvent.click(screen.getByRole('button',{name:'Stop'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'Send'})).toBeEnabled());
  expect(signal!.aborted).toBe(true);expect(dispatched).toBe(false);
  expect(screen.getByText(prompt)).toBeInTheDocument();
 });

 it('creates an agent through the authoritative chat route and opens the refreshed agent list',async()=>{
  const source='カスタムエージェントを作成\n名前: Exact\n指示:  Keep this whitespace.\n';
  let created=false,attempts=0,requestId='';
  const agent={id:'created',name:'Exact',description:'',icon:'✦',instructions:'  Keep this whitespace.\n',preferred_router:'normal_chat',preferred_model:null,knowledge:[],tools:[],skills:[],subagent_ids:[],revision:1};
  const fetcher=vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health')return json({});if(path==='/commands')return json({commands:[]});if(path==='/settings')return json({});if(path==='/projects')return json({projects:[]});
   if(path==='/conversations')return json({conversations:[{id:'A',title:'Conversation A'}]});
   if(path==='/conversations/A')return json({id:'A',messages:[]});
   if(path==='/agents/builder/chat'){
    const value=JSON.parse(String(init?.body));expect(value.message).toBe(source);expect(value.agent_builder).toBe(true);
    attempts++;
    if(attempts===1){requestId=value.message_id;created=true;throw Error('Response lost after creation')}
    expect(value.message_id).toBe(requestId);
    created=true;return json({conversation_id:'A',response:'Agent created.',blocks:[{type:'agent_builder',status:'created',mode:'transfer',agent_id:'created'}]});
   }
   if(path==='/agents')return json({agents:created?[agent]:[]});
   if(path==='/agents/options')return json({tools:[],skills:[],models:[],runtime_available:true});
   throw Error(`Unexpected fixture path: ${path}`);
  });
  vi.stubGlobal('fetch',fetcher);configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'Conversation A'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'Send A'})).toBeEnabled());
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:source}});fireEvent.click(screen.getByRole('button',{name:'Send A'}));
  await waitFor(()=>{expect(screen.getByLabelText('Draft')).toHaveValue(source);expect(screen.getByRole('button',{name:'Send A'})).toBeEnabled()});
  fireEvent.click(screen.getByRole('button',{name:'Send A'}));
  await screen.findByText('Agent created.');
  expect(fetcher.mock.calls.some(([input])=>String(input).endsWith('/chat/stream'))).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:/Open custom agents|カスタムエージェント一覧を開く/}));
  expect(await screen.findByRole('heading',{name:'Exact'})).toBeVisible();
 });
 it('keeps concurrent streams, late history, drafts and errors with their originating conversation',async()=>{
  const encoder=new TextEncoder(),streams=new Map<string,ReadableStreamDefaultController<Uint8Array>>();
  const lateA=deferred<Response>(),historyB=deferred<Response>();let aReads=0,bReads=0;
  const fetcher=vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health')return json({});if(path==='/commands')return json({commands:[]});if(path==='/settings')return json({});if(path==='/projects')return json({projects:[]});
   if(path==='/conversations')return json({conversations:[{id:'A',title:'Conversation A'},{id:'B',title:'Conversation B'}]});
   if(path==='/conversations/A'){aReads++;return aReads===1?json({id:'A',messages:[{role:'assistant',content:'A private history'}]}):lateA.promise}
   if(path==='/conversations/B'){bReads++;return bReads===1?historyB.promise:json({id:'B',messages:[]})}
   if(path==='/chat/stream'){
    const id=JSON.parse(String(init?.body)).conversation_id;
    return new Response(new ReadableStream({start(controller){streams.set(id,controller);controller.enqueue(encoder.encode(`data: ${JSON.stringify({type:'meta',conversation_id:id,task_id:`task-${id}`})}\n\n`))}}));
   }
   if(path.endsWith('/cancel'))return json({});throw Error(`Unexpected fixture path: ${path}`);
  });
  vi.stubGlobal('fetch',fetcher);configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'Conversation A'}));
  await screen.findByText('A private history');
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'A question'}});fireEvent.click(screen.getByRole('button',{name:'Send A'}));
  await waitFor(()=>expect(streams.has('A')).toBe(true));
  const emit=async(id:string,event:unknown)=>act(async()=>{streams.get(id)!.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))});
  await emit('A',{type:'chunk',text:'A streamed secret'});
  expect(screen.getByText('A streamed secret')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:/^Conversation B/}));
  // Before the B fetch resolves, no stale A message or composer state may paint.
  expect(screen.queryByText('A private history')).toBeNull();expect(screen.queryByText('A streamed secret')).toBeNull();
  expect((screen.getByLabelText('Draft') as HTMLTextAreaElement).value).toBe('');
  await act(async()=>historyB.resolve(json({id:'B',messages:[{role:'assistant',content:'B private history'}]})));
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'B question'}});fireEvent.click(screen.getByRole('button',{name:'Send B'}));
  await waitFor(()=>expect(streams.size).toBe(2));
  await emit('A',{type:'chunk',text:' A background tail'});await emit('B',{type:'chunk',text:'B streamed answer'});
  expect(screen.queryByText(/A background tail/)).toBeNull();expect(screen.getByText('B streamed answer')).toBeTruthy();
  await emit('A',{type:'error',message:'A provider failure'});
  expect(screen.queryByText('A provider failure')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:/^Conversation A/}));
  expect(screen.getByText('A provider failure')).toBeTruthy();expect(screen.queryByText('B streamed answer')).toBeNull();
  expect((screen.getByLabelText('Draft') as HTMLTextAreaElement).value).toBe('A question');
  fireEvent.click(screen.getByRole('button',{name:/^Conversation B/}));
  await act(async()=>lateA.resolve(json({id:'A',messages:[{role:'assistant',content:'A late history response'}]})));
  expect(screen.queryByText('A late history response')).toBeNull();expect(screen.queryByText('A provider failure')).toBeNull();
  await emit('B',{type:'done'});await waitFor(()=>expect(screen.getByRole('button',{name:'Send B'}).hasAttribute('disabled')).toBe(false));
 });
 it('does not navigate back or leak output when conversation creation and attachment replies finish in the background',async()=>{
  const created=deferred<Response>(),rest=deferred<Response>(),encoder=new TextEncoder();let stream:ReadableStreamDefaultController<Uint8Array>|undefined;
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health'||path==='/settings')return json({});if(path==='/commands')return json({commands:[]});if(path==='/projects')return json({projects:[]});
   if(path==='/conversations')return init?.method==='POST'?created.promise:json({conversations:[{id:'B',title:'Conversation B'},{id:'C',title:'Created C'}]});
   if(path==='/conversations/B')return json({id:'B',messages:[{role:'assistant',content:'B history'}]});
   if(path==='/conversations/C')return json({id:'C',messages:[]});
   if(path==='/chat/stream'){expect(JSON.parse(String(init?.body)).conversation_id).toBe('C');return new Response(new ReadableStream({start(controller){stream=controller}}))}
   if(path==='/chat'){expect(JSON.parse(String(init?.body)).conversation_id).toBe('B');return rest.promise}
   throw Error(`Unexpected fixture path: ${path}`);
  }));configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);await screen.findByRole('button',{name:'Conversation B'});
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'C initial question'}});fireEvent.click(screen.getByRole('button',{name:'Send'}));
  fireEvent.click(screen.getByRole('button',{name:'Conversation B'}));await screen.findByText('B history');
  await act(async()=>created.resolve(json({id:'C',title:'Created C'})));await waitFor(()=>expect(stream).toBeDefined());
  expect(screen.getByRole('button',{name:'Send B'})).toBeTruthy();expect((screen.getByLabelText('Draft') as HTMLTextAreaElement).value).toBe('');
  fireEvent.click(screen.getByRole('button',{name:'Attach'}));fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'B attachment question'}});fireEvent.click(screen.getByRole('button',{name:'Send B'}));
  await act(async()=>{stream!.enqueue(encoder.encode(`data: ${JSON.stringify({type:'meta',conversation_id:'C',task_id:'task-C'})}\n\n`));stream!.enqueue(encoder.encode(`data: ${JSON.stringify({type:'reasoning_delta',reasoning:{text:'C received work',kind:'trace',complete:false,truncated:false}})}\n\n`));stream!.enqueue(encoder.encode(`data: ${JSON.stringify({type:'chunk',text:'C background answer'})}\n\n`));stream!.enqueue(encoder.encode(`data: ${JSON.stringify({type:'done'})}\n\n`))});
  expect(screen.queryByText('C background answer')).toBeNull();expect(screen.getByRole('button',{name:'Send B'}).hasAttribute('disabled')).toBe(true);expect(screen.queryByText('C received work')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:/^Created C/}));expect(screen.getByText('C background answer')).toBeTruthy();expect(screen.getByText('C received work')).toBeInTheDocument();
  await act(async()=>rest.resolve(json({conversation_id:'B',response:'B attachment answer'})));
  expect(screen.queryByText('B attachment answer')).toBeNull();expect(screen.queryByText('B attachment question')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:/^Conversation B/}));expect(screen.getByText('B attachment answer')).toBeTruthy();expect(screen.queryByText('C background answer')).toBeNull();
 });

 it.each(['ollama:local-model','api:openai_oauth:gpt-6-luna'])('opens %s agents without a project even when a project is selected',async model=>{
  const agent={id:'agent-one',name:'Fixture agent',icon:'✦',revision:2,archived:false,description:'Helper',instructions:'Help',preferred_model:model,preferred_router:'normal_chat',knowledge:[],tools:[],skills:[],subagent_ids:[]};
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health'||path==='/settings')return json({});if(path==='/commands')return json({commands:[]});if(path==='/projects')return json({projects:[{id:'P',name:'Selected project'}]});
   if(path==='/projects/P/conversations')return json({conversations:[{id:'project-chat',title:'Project history'}]});if(path==='/projects/P/core-context')return json({content:''});
   if(path==='/conversations')return json({conversations:[]});if(path==='/agents')return json({agents:[agent]});
   if(path==='/agents/options')return json({tools:[],skills:[],models:[{id:model,available:true}],runtime_available:true});
   if(path==='/agents/agent-one/conversations')expect(JSON.parse(String(init?.body))).toEqual({project_id:null});
   if(path==='/agents/agent-one/conversations'||path==='/conversations/agent-chat')return json({id:'agent-chat',project_id:null,title:agent.name,messages:[],custom_agent:agent});
   throw Error(`Unexpected fixture path: ${path}`);
  }));configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);await screen.findByText('READY');
  fireEvent.click(screen.getByRole('button',{name:/^Selected project/}));await screen.findByRole('button',{name:'Project history'});
  fireEvent.click(screen.getByRole('button',{name:/Custom agents|カスタムエージェント/}));
  fireEvent.click(await screen.findByRole('button',{name:/^(Use|使う)$/}));
  await screen.findByRole('button',{name:'Send agent-chat'});
  expect(screen.getByText(/Custom agent conversation|カスタムエージェントとの会話/)).toBeInTheDocument();
  expect(screen.getByRole('button',{name:/^(No project|プロジェクトなし)/})).toHaveClass('active');
  fireEvent.click(screen.getByRole('button',{name:/^Selected project/}));await screen.findByRole('button',{name:'Project history'});
  expect(screen.queryByRole('button',{name:'Fixture agent'})).toBeNull();
 });
 it('shows agent conversation creation errors on the agent list',async()=>{
  const agent={id:'agent-one',name:'Fixture agent',icon:'✦',revision:1,description:'',instructions:'',preferred_model:null,preferred_router:'normal_chat',knowledge:[],tools:[],skills:[],subagent_ids:[]};
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health'||path==='/settings')return json({});if(path==='/commands')return json({commands:[]});if(path==='/projects')return json({projects:[]});
   if(path==='/conversations')return json({conversations:[]});if(path==='/agents')return json({agents:[agent]});
   if(path==='/agents/options')return json({tools:[],skills:[],models:[],runtime_available:true});
   if(path==='/agents/agent-one/conversations')return new Response(JSON.stringify({detail:'AGENT_SUBAGENT_UNAVAILABLE'}),{status:422,headers:{'Content-Type':'application/json'}});
   throw Error(`Unexpected fixture path: ${path}`);
  }));configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);await screen.findByText('READY');
  fireEvent.click(screen.getByRole('button',{name:/Custom agents|カスタムエージェント/}));
  fireEvent.click(await screen.findByRole('button',{name:/^(Use|使う)$/}));
  expect(await screen.findByRole('alert')).toHaveTextContent(/AGENT_SUBAGENT_UNAVAILABLE|委譲|依頼/);
 });

 it('blocks conversation creation after startup failure and reconnects on retry',async()=>{
  configureApi('','');
  const invoke=vi.fn().mockRejectedValueOnce('backend_api_ready_timeout').mockResolvedValue({api_url:'http://fixture',session_token:'fixture'});
  vi.stubGlobal('__TAURI__',{core:{invoke}});
  const fetcher=vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const path=String(input).replace('http://fixture','');
   if(path==='/health'||path==='/settings')return json({});if(path==='/commands')return json({commands:[]});if(path==='/projects')return json({projects:[]});
   if(path==='/conversations')return init?.method==='POST'?json({id:'reconnected',title:'New chat'}):json({conversations:[]});
   if(path==='/conversations/reconnected')return json({id:'reconnected',messages:[]});
   throw Error(`Unexpected fixture path: ${path}`);
  });vi.stubGlobal('fetch',fetcher);
  render(<LanguageProvider><App/></LanguageProvider>);
  await screen.findByText('FAILED');
  expect(screen.getByRole('alert')).toHaveTextContent(/起動|startup/i);
  const create=screen.getByRole('button',{name:/^(New conversation|新しい会話)$/});
  expect(create).toBeDisabled();fireEvent.click(create);expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:/Retry connection|接続を再試行/}));
  await screen.findByText('READY');expect(invoke).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button',{name:/^(New conversation|新しい会話)$/}));
  await screen.findByRole('button',{name:'Send reconnected'});
  expect(fetcher.mock.calls.every(([url])=>String(url).startsWith('http://fixture/'))).toBe(true);
 });

});

it('resends an edited recent user turn after truncating its suffix, through the normal chat route',async()=>{
  let history=Array.from({length:3},(_,i)=>[{id:`u${i+1}`,role:'user',content:`Question ${i+1}`},{id:`a${i+1}`,role:'assistant',content:`Answer ${i+1}`}]).flat();
  const calls:string[]=[];
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
    const path=String(input).replace('http://fixture','');
    if(path==='/health'||path==='/settings')return json({});
    if(path==='/commands')return json({commands:[]});if(path==='/projects')return json({projects:[]});
    if(path==='/conversations')return json({conversations:[{id:'A',title:'Editable chat'}]});
    if(path==='/conversations/A')return json({id:'A',messages:history});
    if(path==='/conversations/A/rewind'){
      calls.push('rewind');expect(JSON.parse(String(init?.body))).toEqual({message_id:'u2',expected_last_message_id:'a3',project_id:null});
      history=history.slice(0,2);return json({id:'A',messages:history});
    }
    if(path==='/chat/stream'){
      calls.push('send');const value=JSON.parse(String(init?.body));
      expect(value.message).toBe('Edited question');expect(value.conversation_id).toBe('A');
      expect(value).not.toHaveProperty('coding_task_id');
      history=[...history,{id:'new-u',role:'user',content:value.message},{id:'new-a',role:'assistant',content:'Fresh answer'}];
      return new Response([{type:'meta',conversation_id:'A'},{type:'chunk',text:'Fresh answer'},{type:'done'}].map(v=>`data: ${JSON.stringify(v)}\n\n`).join(''),{headers:{'Content-Type':'text/event-stream'}});
    }
    throw Error(path);
  }));configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'Editable chat'}));
  await screen.findByText('Answer 3');
  const edits=screen.getAllByRole('button',{name:/^(メッセージを編集|Edit message)$/});expect(edits).toHaveLength(2);
  fireEvent.click(edits[0]);
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'Edited question'}});
  fireEvent.click(screen.getByRole('button',{name:/^(保存して再送|Save and resend)$/}));
  await screen.findByText('Fresh answer');
  expect(calls).toEqual(['rewind','send']);
  expect(screen.getByText('Answer 1')).toBeInTheDocument();
  for(const old of ['Question 2','Answer 2','Question 3','Answer 3'])expect(screen.queryByText(old)).toBeNull();
});

it('keeps the original history and edited text when a conflicting rewind is rejected',async()=>{
  const history=[{id:'u',role:'user',content:'Original question'},{id:'a',role:'assistant',content:'Original answer'}];
  let sent=false;
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL)=>{
    const path=String(input).replace('http://fixture','');
    if(path==='/health'||path==='/settings')return json({});if(path==='/commands')return json({commands:[]});if(path==='/projects')return json({projects:[]});
    if(path==='/conversations')return json({conversations:[{id:'A',title:'Conflict chat'}]});
    if(path==='/conversations/A')return json({id:'A',messages:history});
    if(path==='/conversations/A/rewind')return new Response(JSON.stringify({detail:'CONVERSATION_CHANGED'}),{status:409,headers:{'Content-Type':'application/json'}});
    if(path==='/chat'||path==='/chat/stream'){sent=true;return json({});}throw Error(path);
  }));configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'Conflict chat'}));
  fireEvent.click(await screen.findByRole('button',{name:/^(メッセージを編集|Edit message)$/}));
  const editor=screen.getByLabelText('Draft');
  fireEvent.change(editor,{target:{value:'Keep this draft'}});
  fireEvent.click(screen.getByRole('button',{name:/^(保存して再送|Save and resend)$/}));
  await screen.findByRole('alert');expect(sent).toBe(false);
  expect(editor).toHaveValue('Keep this draft');expect(screen.getByText('Original answer')).toBeInTheDocument();
});

it('cancels composer editing without changing history and restores the pre-edit draft and attachment',async()=>{
  const diagram={type:'concept_diagram',schema_version:2,title:'Unsent diagram',nodes:[{id:'n1',label:'Original draft box',position:{x:0,y:0}}],edges:[],intersections:[]};
  sessionStorage.setItem('olcr-concept-draft-v1:projectless:A',JSON.stringify(diagram));
  const history=[{id:'u',role:'user',content:'Original question'},{id:'a',role:'assistant',content:'Original answer'}];
  const mutations:string[]=[];let sent:any;
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
    const path=String(input).replace('http://fixture','');
    if(init?.method&&init.method!=='GET')mutations.push(path);
    if(path==='/health'||path==='/settings')return json({});if(path==='/commands')return json({commands:[]});if(path==='/projects')return json({projects:[]});
    if(path==='/conversations')return json({conversations:[{id:'A',title:'Cancel chat'}]});
    if(path==='/conversations/A')return json({id:'A',messages:history});
    if(path==='/chat'){sent=JSON.parse(String(init?.body));return json({conversation_id:'A',response:'New response'});}
    throw Error(path);
  }));configureApi('http://fixture','fixture');render(<LanguageProvider><App/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'Cancel chat'}));await screen.findByText('Original answer');
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'Unsent draft'}});fireEvent.click(screen.getByRole('button',{name:'Attach'}));
  fireEvent.click(screen.getByRole('button',{name:/^(メッセージを編集|Edit message)$/}));expect(screen.getByLabelText('Draft')).toHaveValue('Original question');
  expect(screen.getByText('Original question',{selector:'.message-markdown p'})).toBeInTheDocument();expect(screen.getByText('Original answer')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'Do not send this'}});fireEvent.click(screen.getByRole('button',{name:'Cancel editing'}));
  expect(JSON.parse(sessionStorage.getItem('olcr-concept-draft-v1:projectless:A')||'null')).toEqual(diagram);
  expect(screen.getByLabelText('Draft')).toHaveValue('Unsent draft');expect(mutations).toEqual([]);expect(screen.queryByRole('button',{name:'Save and resend'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'Send A'}));await screen.findByText('New response');
  expect(sent.concept_diagram).toEqual(diagram);
  expect(sent.message).toBe('Unsent draft');expect(sent.attachment).toMatchObject({name:'note.txt',content:'attached text'});expect(mutations).toEqual(['/chat']);
});

it('keeps composer editing and its cancellation snapshot in the owning conversation',async()=>{
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL)=>{
    const path=String(input).replace('http://fixture','');if(path==='/health'||path==='/settings')return json({});if(path==='/commands')return json({commands:[]});if(path==='/projects')return json({projects:[]});
    if(path==='/conversations')return json({conversations:[{id:'A',title:'Edit A'},{id:'B',title:'Edit B'}]});
    if(path==='/conversations/A')return json({id:'A',messages:[{id:'u',role:'user',content:'Original A'}]});
    if(path==='/conversations/B')return json({id:'B',messages:[]});throw Error(path);
  }));configureApi('http://fixture','fixture');render(<LanguageProvider><App/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'Edit A'}));await screen.findByText('Original A');fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'A unsent'}});
  fireEvent.click(screen.getByRole('button',{name:/^(メッセージを編集|Edit message)$/}));fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'A edit'}});
  fireEvent.click(screen.getByRole('button',{name:'Edit B'}));await waitFor(()=>expect(screen.getByLabelText('Draft')).toHaveValue(''));expect(screen.queryByRole('button',{name:'Cancel editing'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'Edit A'}));await waitFor(()=>expect(screen.getByLabelText('Draft')).toHaveValue('A edit'));
  fireEvent.click(screen.getByRole('button',{name:'Cancel editing'}));expect(screen.getByLabelText('Draft')).toHaveValue('A unsent');
});

it('pins both sidebar lists without changing the selected conversation or draft, and preserves state on failure',async()=>{
 let projects=[{id:'new',name:'New project',workspace_path:'/tmp',updated_at:2,pinned:false},{id:'old',name:'Old project',workspace_path:null,updated_at:1,pinned:false}];
 let conversations=[{id:'new-chat',title:'New chat',updated_at:2,pinned:false},{id:'old-chat',title:'Old chat',updated_at:1,pinned:false}];let historyReads=0,fail=false;
 const fetcher=vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
  const path=String(input).replace('http://fixture','');
  if(path==='/health')return json({});if(path==='/commands')return json({commands:[]});if(path==='/settings')return json({});
  if(path==='/projects')return json({projects});if(path==='/conversations')return json({conversations});
  if(path==='/conversations/old-chat'){historyReads++;return json({id:'old-chat',messages:[]})}
  if(path==='/projects/old/pin'){const value=JSON.parse(String(init?.body));projects=projects.map(p=>p.id==='old'?{...p,pinned:value.pinned}:p);return json(projects[1])}
  if(path==='/conversations/old-chat/pin'){if(fail)return new Response(JSON.stringify({detail:'Pin save failed'}),{status:500});const value=JSON.parse(String(init?.body));conversations=conversations.map(c=>c.id==='old-chat'?{...c,pinned:value.pinned}:c);return json(conversations[1])}
  return json({});
 });vi.stubGlobal('fetch',fetcher);configureApi('http://fixture','fixture');
 render(<LanguageProvider><App/></LanguageProvider>);
 fireEvent.click(await screen.findByRole('button',{name:'Old chat'}));await waitFor(()=>expect(historyReads).toBe(1));
 fireEvent.change(screen.getByLabelText('Draft'),{target:{value:'Keep draft'}});
 fireEvent.click(screen.getByRole('button',{name:'プロジェクト「Old project」のピン留め'}));
 await waitFor(()=>expect(screen.getByRole('button',{name:'プロジェクト「Old project」のピン留めを解除'})).toHaveAttribute('aria-pressed','true'));
 expect([...document.querySelectorAll('.project-row .project-item')].map(el=>el.textContent)).toEqual(['Old projectNo workspace','New project/tmp']);
 fireEvent.click(screen.getByRole('button',{name:'会話「Old chat」のピン留め'}));
 await waitFor(()=>expect(screen.getByRole('button',{name:'会話「Old chat」のピン留めを解除'})).toHaveAttribute('aria-pressed','true'));
 expect([...document.querySelectorAll('.conversation-row .item')].map(el=>el.textContent)).toEqual(['Old chat','New chat']);
 expect(screen.getByLabelText('Draft')).toHaveValue('Keep draft');expect(historyReads).toBe(1);
 fail=true;fireEvent.click(screen.getByRole('button',{name:'会話「Old chat」のピン留めを解除'}));await screen.findByText(/Pin save failed/);
 expect(screen.getByRole('button',{name:'会話「Old chat」のピン留めを解除'})).toHaveAttribute('aria-pressed','true');
 fail=false;fireEvent.click(screen.getByRole('button',{name:'会話「Old chat」のピン留めを解除'}));
 await waitFor(()=>expect(screen.getByRole('button',{name:'会話「Old chat」のピン留め'})).toHaveAttribute('aria-pressed','false'));
 expect([...document.querySelectorAll('.conversation-row .item')].map(el=>el.textContent)).toEqual(['New chat','Old chat']);
 expect(screen.getByLabelText('Draft')).toHaveValue('Keep draft');
});
