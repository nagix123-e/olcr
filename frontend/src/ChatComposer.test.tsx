import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import {useState} from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock=vi.hoisted(()=>vi.fn());
const subscription=vi.hoisted(()=>({listener:null as ((taskId:string)=>void)|null}));
const subscribeMock=vi.hoisted(()=>vi.fn((listener:(taskId:string)=>void)=>{subscription.listener=listener;return ()=>{subscription.listener=null}}));
vi.mock("./api",()=>({api:apiMock,subscribeCodingTask:subscribeMock}));
import { ChatComposer,type Attachment } from "./ChatComposer";

type Deferred<T>={promise:Promise<T>;resolve:(value:T)=>void};
function deferred<T>():Deferred<T>{let resolve!: (value:T)=>void;const promise=new Promise<T>(value=>{resolve=value});return {promise,resolve}}
const graph=[{task_id:"t1",phase_id:"p1",goal:"Current task",depends_on:[],verification:[]}];
function task(id:string,status="RUNNING",goal="Current task") {return {id,status,activity:"QWEN_IMPLEMENTATION",current_phase_id:"p1",retry_count:0,max_retries:2,plan:{phases:[{id:"p1",goal,status:"pending",dependencies:[]}],tasks:[{...graph[0],task_id:`${id}-t`,goal}]},subtask_progress:[{task_id:`${id}-t`,phase_id:"p1",status:status==="COMPLETED"?"DONE":"RUNNING",verification_status:"UNVERIFIED",attempt:0}]}}
function props(conversationId="a"){return {text:"",setText:vi.fn(),disabled:false,loading:false,send:(event:React.FormEvent)=>event.preventDefault(),commands:[],setAttachment:vi.fn(),conversationId}}
const reports={reports:[]};

it('sends symptom questions through normal chat without a dedicated UI',async()=>{
  apiMock.mockResolvedValue({tasks:[],reports:[],skills:[]});
  const send=vi.fn();
  const {container}=render(<ChatComposer {...props()} text="今、頭痛があるのだけど何が疑われる？" send={send}/>);
  await flush();fireEvent.submit(container.querySelector('form.composer')!);await flush();
  expect(send).toHaveBeenCalledOnce();
  expect(screen.queryByRole('dialog',{name:'症状について相談'})).toBeNull();
  expect(apiMock.mock.calls.some(([path])=>String(path).startsWith('/symptom-consultations'))).toBe(false);
});

beforeEach(()=>{vi.useFakeTimers();apiMock.mockReset();subscribeMock.mockClear();subscription.listener=null});
afterEach(()=>{cleanup();delete document.documentElement.dataset.olcrActiveProjectId;delete document.documentElement.dataset.olcrProjectlessConversation;vi.useRealTimers()});

async function flush(){await act(async()=>{await Promise.resolve();await Promise.resolve()})}
function count(path:string){return apiMock.mock.calls.filter(([value])=>value===path).length}

it('persists console dismissal without archiving and restores the same task from chat',async()=>{
  let current={...task('a','RESUMABLE'),console_hidden:false};
  apiMock.mockImplementation((path:string,options?:RequestInit)=>{
    if(path==='/conversations/a/coding-tasks')return Promise.resolve({tasks:[current]});
    if(path==='/coding-tasks/a/console'){
      current={...current,console_hidden:JSON.parse(String(options?.body)).hidden};
      return Promise.resolve(current);
    }
    if(path==='/coding-tasks/a')return Promise.resolve(current);
    return Promise.resolve({reports:[],skills:[]});
  });
  const {container}=render(<ChatComposer {...props()}/>);await flush();await flush();
  expect(container.querySelector('.coding-task-progress')).not.toBeNull();
  fireEvent.click(screen.getByRole('button',{name:/Dismiss Coding Orchestrator card|コーディング オーケストレーターを閉じる/}));
  await flush();await flush();
  expect(container.querySelector('.coding-task-progress')).toBeNull();
  expect(apiMock.mock.calls.some(([path,options])=>path==='/coding-tasks/a'&&options?.method==='PATCH')).toBe(false);
  current={...current,console_hidden:false};
  await act(async()=>subscription.listener?.('a'));await flush();
  expect(container.querySelector('.coding-task-progress')).not.toBeNull();
});

it('keeps a persistently hidden console closed after loading the conversation',async()=>{
  const current={...task('a','RESUMABLE'),console_hidden:true};
  apiMock.mockImplementation((path:string)=>Promise.resolve(path==='/conversations/a/coding-tasks'
    ?{tasks:[current]}:path==='/coding-tasks/a'?current:{reports:[],skills:[]}));
  const {container}=render(<ChatComposer {...props()}/>);await flush();await flush();
  expect(container.querySelector('.coding-task-progress')).toBeNull();
});

it('shows the saved resume-control explanation when the task stays blocked',async()=>{
  const blocked={...task('a','BLOCKED'),recovery_reason:'GLOBAL_RECOVERY_LIMIT'};
  apiMock.mockImplementation((path:string,options?:RequestInit)=>{
    if(path==='/conversations/a/coding-tasks')return Promise.resolve({tasks:[blocked]});
    if(path==='/coding-tasks/a')return Promise.resolve(options?.method==='PATCH'
      ?{...blocked,control_response:'Recovery denied: bounded budget exhausted.'}:blocked);
    return Promise.resolve({reports:[],skills:[]});
  });
  render(<ChatComposer {...props()}/>);await flush();await flush();
  fireEvent.click(screen.getByRole('button',{name:/Recheck the blocker|停止条件を再確認/}));
  await flush();await flush();
  expect(screen.getByText('Recovery denied: bounded budget exhausted.')).toBeVisible();
});

it('stops generation once without submitting an empty message',async()=>{
  apiMock.mockResolvedValue({tasks:[],reports:[],skills:[]});
  const pause=vi.fn(),send=vi.fn();
  render(<ChatComposer {...props()} text="" loading pause={pause} send={send}/>);await flush();
  const stop=screen.getByRole('button',{name:/^(Stop generating|生成を停止)$/});
  expect(stop).toBeEnabled();expect(stop).toHaveAttribute('type','button');
  fireEvent.click(stop);expect(pause).toHaveBeenCalledOnce();expect(send).not.toHaveBeenCalled();
});

it('offers custom agent creation from the plus menu without modifying the draft',async()=>{
  apiMock.mockResolvedValue({tasks:[],reports:[],skills:[]});
  const onAgentCreation=vi.fn(),options=props();
  render(<ChatComposer {...options} text="Keep this exact prompt" onAgentCreation={onAgentCreation}/>);
  await flush();fireEvent.click(screen.getByRole('button',{name:/^(Add action|操作を追加)$/}));
  fireEvent.click(screen.getByRole('menuitem',{name:/^(Create custom agent|カスタムエージェント作成)$/}));
  expect(onAgentCreation).toHaveBeenCalledOnce();expect(options.setText).not.toHaveBeenCalled();
});

it('keeps creation mode visible and sends raw text through the creation route',async()=>{
  apiMock.mockResolvedValue({tasks:[],reports:[],skills:[]});
  const send=vi.fn((event:React.FormEvent)=>event.preventDefault()),cancel=vi.fn();
  render(<ChatComposer {...props()} text="名前: A\n指示:  exact\n" send={send} agentCreation onCancelAgentCreation={cancel}/>);
  await flush();
  expect(screen.getByText(/Custom agent creation mode|カスタムエージェント作成モード/)).toBeVisible();
  fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest('form')!);
  expect(send).toHaveBeenCalledWith(expect.anything());
  fireEvent.click(screen.getByRole('button',{name:/Cancel agent creation|エージェント作成を中止/}));expect(cancel).toHaveBeenCalledOnce();
});

it('preserves original binary attachment bytes for format conversion',async()=>{
  apiMock.mockResolvedValue({tasks:[],reports:[],skills:[]});
  const options=props('a');
  const {container}=render(<ChatComposer {...options}/>);
  const bytes=new Uint8Array([0x50,0x4b,0,0xff,0x81]);
  const file=new File([bytes],'document.docx',{type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'});
  Object.defineProperty(file,'arrayBuffer',{value:()=>Promise.resolve(bytes.buffer)});
  Object.defineProperty(file,'text',{value:()=>{throw Error('binary must not be decoded as text')}});
  fireEvent.change(container.querySelector('#olcr-attachment')!,{target:{files:[file]}});
  await flush();
  expect(options.setAttachment).toHaveBeenCalledWith(expect.objectContaining({name:'document.docx',content:'',bytesBase64:'UEsA/4E='}));
});

describe("ChatComposer persisted progress polling",()=>{
  it('does not replace a streamed task with a late initial conversation snapshot',async()=>{
    const pending=deferred<{tasks:ReturnType<typeof task>[]}>();
    const active=task('streamed','RUNNING','New streamed task');
    apiMock.mockImplementation((path:string)=>path==='/conversations/a/coding-tasks'?pending.promise:path==='/coding-tasks/streamed'?Promise.resolve(active):Promise.resolve(reports));
    render(<ChatComposer {...props('a')}/>);await flush();
    await act(async()=>{subscription.listener?.('streamed');await Promise.resolve();await Promise.resolve()});
    await act(async()=>{pending.resolve({tasks:[]});await Promise.resolve()});
    expect(screen.getByLabelText(/Current task|現在のタスク/)).toHaveTextContent('New streamed task');
  });
  it.each(['pause','resume','archive'])("ignores a late %s response from the previous conversation",async action=>{
    const pending=deferred<ReturnType<typeof task>>();
    const a=task('a',action==='pause'?'RUNNING':'RESUMABLE','Old task'),b=task('b','RUNNING','New task');
    apiMock.mockImplementation((path:string,options?:{method?:string})=>{
      if(path==='/conversations/a/coding-tasks')return Promise.resolve({tasks:[a]});
      if(path==='/conversations/b/coding-tasks')return Promise.resolve({tasks:[b]});
      if(path==='/coding-tasks/a')return options?.method==='PATCH'?pending.promise:Promise.resolve(a);
      if(path==='/coding-tasks/b')return Promise.resolve(b);
      return Promise.resolve(reports);
    });
    const view=render(<ChatComposer {...props('a')}/>);await flush();
    fireEvent.click(screen.getByRole('button',{name:action==='pause'?/^(一時停止|Pause)$/:action==='resume'?/^(再開（|Resume \()/ :/^(アーカイブ|Archive)$/}));await flush();
    view.rerender(<ChatComposer {...props('b')}/>);await flush();
    await act(async()=>{pending.resolve({...a,status:'RESUMABLE'});await Promise.resolve()});
    expect(screen.getByLabelText(/Current task|現在のタスク/)).toHaveTextContent('New task');
    expect(screen.getByRole('button',{name:/^(一時停止|Pause)$/})).not.toBeDisabled();
  });

  it.each(["設定を開いて", "/settings", "/scrape manage", "/routines", "/computer use on"])(
    "allows global chat management before project selection: %s", text=>{
      delete document.documentElement.dataset.olcrActiveProjectId;
      const send=vi.fn((event:React.FormEvent)=>event.preventDefault());
      render(<ChatComposer {...props("")} text={text} send={send}/>);
      fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest("form")!);
      expect(send).toHaveBeenCalledOnce();
      expect(screen.queryByRole("alert")).toBeNull();
    });
  it("allows ordinary chat with no project selected",()=>{
    delete document.documentElement.dataset.olcrActiveProjectId;
    delete document.documentElement.dataset.olcrProjectlessConversation;
    const send=vi.fn((event:React.FormEvent)=>event.preventDefault());
    render(<ChatComposer {...props("")} send={send}/>);
    fireEvent.change(screen.getByLabelText(/Message|メッセージ/),{target:{value:"hello"}});
    fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest("form")!);
    expect(send).toHaveBeenCalledOnce();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("allows ordinary chat after the no-project conversation scope is selected",()=>{
    document.documentElement.dataset.olcrProjectlessConversation="true";
    const send=vi.fn((event:React.FormEvent)=>event.preventDefault());
    render(<ChatComposer {...props("")} send={send}/>);
    fireEvent.change(screen.getByLabelText(/Message|メッセージ/),{target:{value:"hello"}});
    fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest("form")!);
    expect(send).toHaveBeenCalledTimes(1);
    delete document.documentElement.dataset.olcrProjectlessConversation;
  });

  it("submits normally when a project is active",()=>{
    document.documentElement.dataset.olcrActiveProjectId="project-1";
    const send=vi.fn((event:React.FormEvent)=>event.preventDefault());
    render(<ChatComposer {...props("")} send={send}/>);
    fireEvent.change(screen.getByLabelText(/Message|メッセージ/),{target:{value:"hello"}});
    fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest("form")!);
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("submits a fully typed no-argument slash command on the first Enter",()=>{
    document.documentElement.dataset.olcrActiveProjectId="project-1";
    const send=vi.fn((event:React.FormEvent)=>event.preventDefault());
    const commands=[
      {id:"computer_use_on",command:"/computer use on",description:"Enable chat-triggered native Computer Use",arguments:"",enabled:true},
      {id:"computer_use_off",command:"/computer use off",description:"Disable chat-triggered native Computer Use",arguments:"",enabled:true},
    ];
    render(<ChatComposer {...props("")} text="/computer use on" commands={commands} send={send}/>);
    const input=screen.getByLabelText(/Message|メッセージ/);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    fireEvent.keyDown(input,{key:"Enter",code:"Enter"});
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("selects a native installed skill without review, intake, or activation",async()=>{
    const setText=vi.fn();
    const candidate={name:"design-helper",version:"1",description:"Local design helper",state:"ACTIVE",enabled:true,trusted:true,native:true,permission:"local_installation"};
    apiMock.mockImplementation((path:string)=>path==="/skills"?Promise.resolve({skills:[candidate]}):Promise.resolve({tasks:[]}));
    render(<ChatComposer {...props()} text="/skill" setText={setText}/>);await flush();
    expect(screen.getByRole("option",{name:/design-helper/})).toHaveTextContent('Local design helper');
    expect(screen.queryByText(/Installed locally · Ready to use|ローカルにインストール済み · 利用可能/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option",{name:/design-helper/}));await flush();
    expect(setText).toHaveBeenCalledWith("/skill design-helper ");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(apiMock.mock.calls.some(([path])=>/\/(review|intake|activate)$/.test(String(path)))).toBe(false);
  });

  it("continues polling an active task",async()=>{
    const active=task("a");apiMock.mockImplementation((path:string)=>path==="/conversations/a/coding-tasks"?Promise.resolve({tasks:[active]}):path==="/coding-tasks/a"?Promise.resolve(active):Promise.resolve(reports));
    render(<ChatComposer {...props()}/>);await flush();expect(count("/coding-tasks/a")).toBe(1);
    await act(async()=>{await vi.advanceTimersByTimeAsync(1000)});
    expect(count("/coding-tasks/a")).toBe(2);
  });
  it("stops polling after a terminal response",async()=>{
    const running=task("a"),completed=task("a","COMPLETED");let calls=0;
    apiMock.mockImplementation((path:string)=>{if(path==="/conversations/a/coding-tasks")return Promise.resolve({tasks:[running]});if(path==="/coding-tasks/a")return Promise.resolve(++calls===1?running:completed);return Promise.resolve(reports)});
    render(<ChatComposer {...props()}/>);await flush();await act(async()=>{await vi.advanceTimersByTimeAsync(1000)});expect(calls).toBe(2);
    await act(async()=>{await vi.advanceTimersByTimeAsync(3000)});expect(calls).toBe(2);
  });
  it("does not overlap an in-flight refresh",async()=>{
    const pending=deferred<ReturnType<typeof task>>(),active=task("a");
    apiMock.mockImplementation((path:string)=>path==="/conversations/a/coding-tasks"?Promise.resolve({tasks:[active]}):path==="/coding-tasks/a"?pending.promise:Promise.resolve(reports));
    render(<ChatComposer {...props()}/>);await flush();expect(count("/coding-tasks/a")).toBe(1);
    await act(async()=>{await vi.advanceTimersByTimeAsync(3000)});expect(count("/coding-tasks/a")).toBe(1);
    await act(async()=>{pending.resolve(active);await Promise.resolve()});
  });
  it("keeps the newer conversation visible when the old request resolves late",async()=>{
    const oldList=deferred<{tasks:ReturnType<typeof task>[]}>(),a=task("a","RUNNING","Old task"),b=task("b","RUNNING","New task");
    apiMock.mockImplementation((path:string)=>{if(path==="/conversations/a/coding-tasks")return oldList.promise;if(path==="/conversations/b/coding-tasks")return Promise.resolve({tasks:[b]});if(path==="/coding-tasks/b")return Promise.resolve(b);return Promise.resolve(reports)});
    const view=render(<ChatComposer {...props("a")}/>);await flush();view.rerender(<ChatComposer {...props("b")}/>);await flush();expect(screen.getByLabelText(/Current task|現在のタスク/)).toHaveTextContent("New task");
    await act(async()=>{oldList.resolve({tasks:[a]});await Promise.resolve()});
    expect(screen.getByLabelText(/Current task|現在のタスク/)).toHaveTextContent("New task");
  });
  it("cleans up a disposed active poller",async()=>{
    const active=task("a"),pending=deferred<ReturnType<typeof task>>();
    apiMock.mockImplementation((path:string)=>path==="/conversations/a/coding-tasks"?Promise.resolve({tasks:[active]}):path==="/coding-tasks/a"?pending.promise:Promise.resolve(reports));
    const view=render(<ChatComposer {...props()}/>);await flush();expect(count("/coding-tasks/a")).toBe(1);view.unmount();
    await act(async()=>{pending.resolve(active);await vi.advanceTimersByTimeAsync(4000)});expect(count("/coding-tasks/a")).toBe(1);
  });
  it("restores persisted progress and fetches phase reports",async()=>{
    const active=task("a");apiMock.mockImplementation((path:string)=>path==="/conversations/a/coding-tasks"?Promise.resolve({tasks:[active]}):path==="/coding-tasks/a"?Promise.resolve(active):Promise.resolve(reports));
    render(<ChatComposer {...props()}/>);await flush();expect(screen.getByLabelText(/Current task|現在のタスク/)).toHaveTextContent("Current task");
    expect(count("/coding-tasks/a/phase-reports")).toBe(1);
  });
  it("refreshes persisted progress when a streamed control event publishes the task id",async()=>{
    const active=task("stream-task");
    apiMock.mockImplementation((path:string)=>path==="/conversations/a/coding-tasks"?Promise.resolve({tasks:[]}):path==="/coding-tasks/stream-task"?Promise.resolve(active):Promise.resolve(reports));
    render(<ChatComposer {...props()}/>);await flush();
    expect(subscription.listener).toBeTypeOf("function");
    await act(async()=>{subscription.listener?.("stream-task");await Promise.resolve();await Promise.resolve()});
    expect(count("/coding-tasks/stream-task")).toBe(1);
    expect(screen.getByLabelText(/Current task|現在のタスク/)).toHaveTextContent("Current task");
  });
  it("refreshes without losing the card when a streamed control event republishes the same task id",async()=>{
    const running=task("a","RUNNING"),completed=task("a","COMPLETED");let calls=0;
    apiMock.mockImplementation((path:string)=>path==="/conversations/a/coding-tasks"?Promise.resolve({tasks:[running]}):path==="/coding-tasks/a"?Promise.resolve(++calls===1?running:completed):Promise.resolve(reports));
    render(<ChatComposer {...props()}/>);await flush();expect(screen.getByLabelText(/Current task|現在のタスク/)).toHaveTextContent("Current task");
    await act(async()=>{subscription.listener?.("a");await Promise.resolve();await Promise.resolve()});
    expect(count("/coding-tasks/a")).toBe(2);
    expect(screen.getByLabelText("コーディング オーケストレーター: DONE")).toBeInTheDocument();
  });
});

describe('Chat-first coding instructions',()=>{
  function existingTask(){apiMock.mockImplementation((path:string)=>{
    if(path.endsWith('/coding-tasks'))return Promise.resolve({tasks:[task('current')]});
    if(path==='/coding-tasks/current')return Promise.resolve(task('current'));
    if(path==='/chat')return Promise.resolve({coding_task_id:'current'});
    return Promise.resolve({tasks:[],files:[],reports:[]});
  })}
  it('executes an everyday timer request independently even when follow-ups are queued',async()=>{
    existingTask();const send=vi.fn((event:React.FormEvent)=>event.preventDefault());
    render(<ChatComposer {...props()} text="5分のタイマーをかけて" send={send}/>);await flush();
    fireEvent.change(screen.getByLabelText(/追加指示の扱い|Follow-up behavior/),{target:{value:'queue'}});
    fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest('form')!);await flush();
    expect(send).toHaveBeenCalledWith(expect.anything(),{coding_followup:'chat'});
    expect(apiMock.mock.calls.some(([path])=>path==='/chat')).toBe(false);
  });
  it('sends one message to current work without changing the selected delivery mode',async()=>{
  existingTask();const setText=vi.fn(),send=vi.fn();
  render(<ChatComposer {...props()} text="この1件だけ今の作業へ" setText={setText} send={send}/>);await flush();
  const mode=screen.getByLabelText(/追加指示の扱い|Follow-up behavior/);
  fireEvent.change(mode,{target:{value:'queue'}});
  fireEvent.click(screen.getByRole('button',{name:/^(今の作業へ送る|Send to current work)$/}));await flush();
  const body=JSON.parse(String(apiMock.mock.calls.find(([path])=>path==='/chat')?.[1]?.body));
  expect(body).toMatchObject({coding_task_id:'current',coding_followup:'steer',message:'この1件だけ今の作業へ',conversation_id:'a'});
  expect(mode).toHaveValue('queue');expect(send).not.toHaveBeenCalled();expect(setText).toHaveBeenCalledWith('');
  expect(apiMock.mock.calls.some(([path,init])=>path==='/settings'&&init?.method)).toBe(false);
});

  it('sends the main input to the active task as a queued follow-up',async()=>{
    existingTask();const setText=vi.fn(),send=vi.fn();
    render(<ChatComposer {...props()} text='余白を大きくして' setText={setText} send={send}/>);await flush();
    fireEvent.change(screen.getByLabelText(/追加指示の扱い|Follow-up behavior/),{target:{value:'queue'}});
    fireEvent.click(screen.getByRole('button',{name:/^(送信|Send)$/}));await flush();
    const call=apiMock.mock.calls.find(([path])=>path==='/chat');
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({coding_task_id:'current',coding_followup:'queue',message:'余白を大きくして',conversation_id:'a'});
    expect(send).not.toHaveBeenCalled();expect(setText).toHaveBeenCalledWith('');
    expect(screen.queryByLabelText('Taskへの追加指示')).toBeNull();
  });
  it('automatically sends API questions through the visible chat response path',async()=>{
    existingTask();const send=vi.fn((event:React.FormEvent)=>event.preventDefault());
    render(<ChatComposer {...props()} text='今日の東京の天気を教えて' send={send}/>);await flush();
    expect(screen.getByLabelText(/追加指示の扱い|Follow-up behavior/)).toHaveValue('auto');
    fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest('form')!);await flush();
    expect(send).toHaveBeenCalledWith(expect.anything(),{coding_task_id:'current',coding_followup:'auto'});
    expect(apiMock.mock.calls.some(([path])=>path==='/chat')).toBe(false);
  });
  it('honors ordinary chat without attaching the active coding task',async()=>{
    existingTask();const send=vi.fn((event:React.FormEvent)=>event.preventDefault());
    render(<ChatComposer {...props()} text='大谷翔平について教えて' send={send}/>);await flush();
    fireEvent.change(screen.getByLabelText(/追加指示の扱い|Follow-up behavior/),{target:{value:'chat'}});
    fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest('form')!);await flush();
    expect(send).toHaveBeenCalledWith(expect.anything(),{coding_followup:'chat'});
    expect(apiMock.mock.calls.some(([path])=>path==='/chat')).toBe(false);
  });
  it('supports explicit steering and retains the input on error',async()=>{
    existingTask();apiMock.mockImplementationOnce(()=>Promise.resolve({tasks:[]}));
    const setText=vi.fn();render(<ChatComposer {...props()} text='青くして' setText={setText}/>);await flush();
    act(()=>subscription.listener?.('current'));await flush();
    fireEvent.change(screen.getByLabelText(/追加指示の扱い|Follow-up behavior/),{target:{value:'steer'}});
    apiMock.mockImplementation((path:string)=>path==='/chat'?Promise.reject(Error('offline')):Promise.resolve(task('current')));
    fireEvent.submit(screen.getByLabelText(/Message|メッセージ/).closest('form')!);await flush();
    expect(JSON.parse(String(apiMock.mock.calls.find(([path])=>path==='/chat')?.[1]?.body))).toMatchObject({coding_followup:'steer'});
    expect(setText).not.toHaveBeenCalledWith('');expect(screen.getByRole('alert')).toHaveTextContent('offline');
  });
  it('puts a file instruction into the single input and selects steer',async()=>{
    existingTask();const setText=vi.fn();render(<ChatComposer {...props()} setText={setText}/>);await flush();
    act(()=>window.dispatchEvent(new CustomEvent('olcr-compose-coding-instruction',{detail:{taskId:'current',text:'app.js: '}})));await flush();
    expect(setText).toHaveBeenCalledWith('app.js: ');
    expect(screen.getByLabelText(/追加指示の扱い|Follow-up behavior/)).toHaveValue('steer');
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
  });
});


describe('Coding plan loader',()=>{
  it.each(['PLANNING','QWEN_REPLANNING','QUEUED'])("shows the task console while plan status is %s",async status=>{
    const current={...task('plan',status,'Create lunch roulette'),activity:'QWEN_PLANNING'};
    apiMock.mockImplementation((path:string)=>{
      if(path==='/conversations/a/coding-tasks')return Promise.resolve({tasks:[current]});
      if(path==='/coding-tasks/plan')return Promise.resolve(current);
      if(path==='/coding-tasks/plan/phase-reports')return Promise.resolve({reports:[]});
      return Promise.resolve({tasks:[],reports:[],files:[]});
    });
    render(<ChatComposer {...props()}/>);await flush();
    expect(screen.queryByRole('status',{name:'計画を作成中'})).toBeNull();
    expect(document.querySelector('.coding-task-progress')).toBeInTheDocument();
    expect(screen.queryByText(/計画を作成中です/)).toBeNull();
  });
});

describe('Coding execution loader',()=>{
  function mockTask(current:ReturnType<typeof task> & {archived?:boolean}){
    apiMock.mockImplementation((path:string)=>path==='/conversations/a/coding-tasks'?Promise.resolve({tasks:[current]}):path==='/coding-tasks/active'?Promise.resolve(current):Promise.resolve({tasks:[],reports:[],files:[]}));
  }
  it.each([
    ['QUEUED','コーディングタスクの開始待ち'],
    ['RUNNING','コーディングタスクを実行中'],
    ['FINAL_REPORTING','最終結果を作成中'],
  ])('shows a loader alongside progress in %s, even while composing a follow-up',async(status,name)=>{
    mockTask(task('active',status));
    render(<ChatComposer {...props()} text='余白を大きくして'/>);await flush();
    expect(screen.getByRole('status',{name})).toBeInTheDocument();
    expect(screen.getByLabelText(/コーディング オーケストレーター:/)).toBeInTheDocument();
  });
  it.each(['COMPLETED','RESUMABLE','WAITING_FOR_PLAN_APPROVAL','WAITING_FOR_USER','BLOCKED'])('hides the loader in %s',async status=>{
    mockTask(task('active',status));
    const {container}=render(<ChatComposer {...props()}/>);await flush();
    expect(container.querySelector('.loading-container')).toBeNull();
  });
  it('removes the loader when polling observes completion',async()=>{
    let current=task('active','RUNNING');
    apiMock.mockImplementation((path:string)=>path==='/conversations/a/coding-tasks'?Promise.resolve({tasks:[current]}):path==='/coding-tasks/active'?Promise.resolve(current):Promise.resolve({reports:[],files:[]}));
    render(<ChatComposer {...props()}/>);await flush();
    expect(screen.getByRole('status',{name:'コーディングタスクを実行中'})).toBeInTheDocument();
    current=task('active','COMPLETED');
    await act(async()=>{await vi.advanceTimersByTimeAsync(1000)});
    expect(screen.queryByRole('status',{name:'コーディングタスクを実行中'})).toBeNull();
  });
});

describe('Brain picker placement',()=>{
  it('anchors the Brain control to the message input container',()=>{
    apiMock.mockResolvedValue({tasks:[]});
    const brainModels={
      configuration:{mode:'unified',selections:{brain:'brain-model'},efforts:{brain:null}},
      catalog:{models:[],providers:[]},usage:{models:{},week_starts_at:0},loading:false,error:'',
      save:vi.fn(),refresh:vi.fn(),refreshUsage:vi.fn(),resetUsage:vi.fn(),
    } as any;
    const {container}=render(<ChatComposer {...props()} brainModels={brainModels}/>);
    const picker=container.querySelector('.brain-picker');
    expect(picker).not.toBeNull();
    expect(picker?.closest('.composer-input')).not.toBeNull();
  });
});


it('uses the normal input for edit submission and Escape cancellation',async()=>{
  apiMock.mockResolvedValue({tasks:[]});
  const save=vi.fn(),cancel=vi.fn(),send=vi.fn();
  render(<ChatComposer {...props()} text="Edited text" editMode="message" editKey="u1" onSubmitEdit={save} onCancelEdit={cancel} send={send}/>);await flush();
  const input=screen.getByRole('textbox',{name:/^(Message|メッセージ)$/});expect(input).toHaveValue('Edited text');
  expect(screen.getAllByRole('textbox')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button',{name:/^(保存して再送|Save and resend)$/}));expect(save).toHaveBeenCalledOnce();expect(send).not.toHaveBeenCalled();
  fireEvent.keyDown(input,{key:'Escape'});expect(cancel).toHaveBeenCalledOnce();
});

it.each(['Enter','button'])('resends an edited slash prompt via %s and clears it before the response',async(method)=>{
  apiMock.mockResolvedValue({tasks:[]});
  const saved=vi.fn(),send=vi.fn();
  function Editing(){
    const [text,setText]=useState('/ski');
    return <ChatComposer {...props()} text={text} setText={setText} editMode="message" editKey="sent-message"
      commands={[{id:'skill',command:'/skill',arguments:'name',description:'Use a skill',enabled:true}]}
      send={send} onSubmitEdit={event=>{event.preventDefault();saved(text);setText('')}}/>;
  }
  render(<Editing/>);await flush();
  const input=screen.getByRole('textbox',{name:/^(Message|メッセージ)$/});
  expect(screen.queryByRole('listbox')).toBeNull();
  if(method==='Enter')fireEvent.keyDown(input,{key:'Enter',code:'Enter'});
  else fireEvent.click(screen.getByRole('button',{name:/^(保存して再送|Save and resend)$/}));
  expect(saved).toHaveBeenCalledExactlyOnceWith('/ski');
  expect(input).toHaveValue('');expect(send).not.toHaveBeenCalled();
});

it('keeps Shift+Enter, IME confirmation and key repeats from resending an edit',async()=>{
  apiMock.mockResolvedValue({tasks:[]});const save=vi.fn();
  render(<ChatComposer {...props()} text="編集中" editMode="message" editKey="sent" onSubmitEdit={save}/>);await flush();
  const input=screen.getByRole('textbox',{name:/^(Message|メッセージ)$/});
  fireEvent.keyDown(input,{key:'Enter',shiftKey:true});
  fireEvent.keyDown(input,{key:'Enter',isComposing:true});
  fireEvent.keyDown(input,{key:'Enter',repeat:true});
  expect(save).not.toHaveBeenCalled();
  fireEvent.keyDown(input,{key:'Enter'});expect(save).toHaveBeenCalledOnce();
});

it('shows the attached working folder and detaches it without changing project settings',async()=>{
  apiMock.mockImplementation(async(path:string,init?:RequestInit)=>{
    if(path==='/conversations/a/working-folder')return init?.method==='DELETE'?{detached:true}:{id:'folder',name:'Source folder',root:'/local/source'};
    return {tasks:[],reports:[],skills:[]};
  });
  render(<ChatComposer {...props()}/>);
  await flush();
  expect(screen.getByText(/Source folder/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:/作業フォルダを解除|Detach working folder/}));
  await flush();
  expect(apiMock).toHaveBeenCalledWith('/conversations/a/working-folder',{method:'DELETE'});
  expect(screen.queryByText(/Source folder/)).toBeNull();
  expect(apiMock.mock.calls.some(([path,init])=>String(path).includes('/projects/')&&init?.method==='PATCH')).toBe(false);
});

it.each(['Enter','button'])('waits for the attachment and clears it when sent with %s',async(method)=>{
  apiMock.mockResolvedValue({tasks:[]});const bytes=deferred<ArrayBuffer>(),submitted=vi.fn();
  const file=new File(['data'],'recording.wav',{type:'audio/wav'});
  Object.defineProperty(file,'arrayBuffer',{value:()=>bytes.promise});
  function Composer(){
    const [attachment,setAttachment]=useState<Attachment|null>(null),[text,setText]=useState('文字起こしして'),[loading,setLoading]=useState(false);
    return <ChatComposer {...props()} attachment={attachment} setAttachment={setAttachment} text={text} setText={setText} loading={loading}
      send={event=>{event.preventDefault();submitted(attachment);setAttachment(null);setText('');setLoading(true)}}/>;
  }
  const view=render(<Composer/>);await flush();
  const input=screen.getByRole('textbox',{name:/^(Message|メッセージ)$/});
  fireEvent.change(view.container.querySelector('#olcr-attachment')!,{target:{files:[file]}});await flush();
  expect(screen.getByRole('button',{name:/^(送信|Send)$/})).toBeDisabled();
  fireEvent.keyDown(input,{key:'Enter'});expect(submitted).not.toHaveBeenCalled();
  await act(async()=>bytes.resolve(new Uint8Array([0,1,2]).buffer));await flush();
  expect(screen.getByText('recording.wav')).toBeInTheDocument();
  if(method==='Enter')fireEvent.keyDown(input,{key:'Enter'});
  else fireEvent.click(screen.getByRole('button',{name:/^(送信|Send)$/}));
  expect(submitted).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({name:'recording.wav',bytesBase64:'AAEC'}));
  expect(screen.queryByText('recording.wav')).toBeNull();expect(input).toHaveValue('');
});

it('does not restore an attachment from a read that finishes after removal',async()=>{
  apiMock.mockResolvedValue({tasks:[]});const bytes=deferred<ArrayBuffer>();
  const file=new File(['data'],'late.pdf',{type:'application/pdf'});
  Object.defineProperty(file,'arrayBuffer',{value:()=>bytes.promise});
  function Composer(){
    const [attachment,setAttachment]=useState<Attachment|null>({name:'old.pdf',content:'',mimeType:'application/pdf'});
    return <ChatComposer {...props()} attachment={attachment} setAttachment={setAttachment}/>;
  }
  const view=render(<Composer/>);await flush();
  fireEvent.change(view.container.querySelector('#olcr-attachment')!,{target:{files:[file]}});await flush();
  fireEvent.click(screen.getByRole('button',{name:/添付ファイルを削除|Remove attachment/}));
  await act(async()=>bytes.resolve(new Uint8Array([1]).buffer));await flush();
  expect(screen.queryByText('old.pdf')).toBeNull();expect(screen.queryByText('late.pdf')).toBeNull();
});

it('ignores a late attachment read after changing conversations',async()=>{
  apiMock.mockResolvedValue({tasks:[]});const bytes=deferred<ArrayBuffer>(),setAttachment=vi.fn();
  const file=new File(['data'],'old-conversation.pdf',{type:'application/pdf'});
  Object.defineProperty(file,'arrayBuffer',{value:()=>bytes.promise});
  const view=render(<ChatComposer {...props('a')} setAttachment={setAttachment}/>);await flush();
  fireEvent.change(view.container.querySelector('#olcr-attachment')!,{target:{files:[file]}});await flush();
  view.rerender(<ChatComposer {...props('b')} setAttachment={setAttachment}/>);await flush();
  await act(async()=>bytes.resolve(new Uint8Array([1]).buffer));await flush();
  expect(setAttachment).not.toHaveBeenCalled();
});

it('ignores native drops during submission and discards pending native reads when sending begins',async()=>{
  apiMock.mockResolvedValue({tasks:[]});const file=deferred<Attachment>(),setAttachment=vi.fn();
  let drop:((event:{payload:{paths:string[]}})=>void)|undefined;
  const invoke=vi.fn(()=>file.promise),unlisten=vi.fn();
  const bridgeWindow=window as Window&{__TAURI__?:unknown};const previous=bridgeWindow.__TAURI__;
  bridgeWindow.__TAURI__={core:{invoke},event:{listen:vi.fn(async(_name:string,callback:typeof drop)=>{drop=callback;return unlisten})}};
  try{
    const view=render(<ChatComposer {...props()} loading setAttachment={setAttachment}/>);await flush();
    act(()=>drop?.({payload:{paths:['/tmp/duplicate.pdf']}}));await flush();
    expect(invoke).not.toHaveBeenCalled();
    view.rerender(<ChatComposer {...props()} setAttachment={setAttachment}/>);await flush();
    act(()=>drop?.({payload:{paths:['/tmp/late.pdf']}}));await flush();
    expect(invoke).toHaveBeenCalledOnce();
    view.rerender(<ChatComposer {...props()} loading setAttachment={setAttachment}/>);await flush();
    await act(async()=>file.resolve({name:'late.pdf',content:'',mimeType:'application/pdf'}));await flush();
    expect(setAttachment).not.toHaveBeenCalled();view.unmount();await flush();
    expect(unlisten).toHaveBeenCalledOnce();
  }finally{bridgeWindow.__TAURI__=previous}
});

it('discovers a newly created task while submission is still waiting for its response',async()=>{
 let found=false;
 const current={...task('discovered','PLANNING'),activity:'QWEN_PLANNING'};
 apiMock.mockImplementation((path:string)=>{
  if(path==='/conversations/a/coding-tasks')return Promise.resolve({tasks:found?[current]:[]});
  if(path==='/coding-tasks/discovered')return Promise.resolve(current);
  return Promise.resolve({tasks:[],reports:[],files:[]});
 });
 render(<ChatComposer {...props()} loading={true}/>);await flush();
 expect(document.querySelector('.coding-task-progress')).toBeNull();
 found=true;await act(async()=>{vi.advanceTimersByTime(1500)});await flush();
 expect(document.querySelector('.coding-task-progress')).toBeInTheDocument();
});
