import { afterEach, describe, expect, it, vi } from "vitest";
import { api, configureApi, streamChat, subscribeCodingTask } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("streamChat coding task progress", () => {
  it("publishes the managed task id from the control stream done event", async () => {
    const encoder = new TextEncoder();
    const events = [
      { type: "meta", task_id: "invocation-1", conversation_id: "conversation-1" },
      { type: "done", task: { type: "coding_task_progress", task_id: "coding-task-42", status: "RUNNING" } },
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new ReadableStream({
      start(controller) {
        for (const event of events) controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { "Content-Type": "text/event-stream" } })));
    configureApi("http://127.0.0.1:9999", "test-token");
    const listener = vi.fn();
    const unsubscribe = subscribeCodingTask(listener);
    const onEvent = vi.fn();

    await streamChat({ message: "continue" }, onEvent);

    expect(listener).toHaveBeenCalledWith("coding-task-42");
    expect(listener).not.toHaveBeenCalledWith("invocation-1");
    expect(onEvent).toHaveBeenCalledTimes(2);
    unsubscribe();
  });
});

describe("streamChat terminal lifecycle", () => {
  function responseWith(events: unknown[], close = false) {
    const encoder = new TextEncoder();
    const cancel = vi.fn();
    const response = new Response(new ReadableStream({
      start(controller) {
        for (const event of events) controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
        if (close) controller.close();
      },
      cancel,
    }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
    return cancel;
  }

  it.each(["done", "error", "cancelled"])("releases a %s response without waiting for socket EOF", async type => {
    const cancel = responseWith([{ type: "meta", task_id: "task" }, { type, task: { state: "failed" } }]);
    const listener = vi.fn();
    await streamChat({ message: "fixture" }, listener);
    expect(listener).toHaveBeenCalledTimes(2);
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("reports a disconnected stream with no terminal event", async () => {
    responseWith([{ type: "meta", task_id: "task" }], true);
    await expect(streamChat({}, vi.fn())).rejects.toThrow("CHAT_STREAM_INCOMPLETE");
  });

  it("propagates a UI event-handler failure rather than swallowing it", async () => {
    const cancel = responseWith([{ type: "chunk", text: "answer" }]);
    await expect(streamChat({}, () => { throw Error("UI fixture error"); })).rejects.toThrow("UI fixture error");
    expect(cancel).toHaveBeenCalledOnce();
  });
});

describe('conversation-owned stream cancellation',()=>{
 it('stops before meta immediately and leaves the other conversation running',async()=>{
  const {cancelActiveStream}=await import('./api');
  const controllers=new Map<string,ReadableStreamDefaultController<Uint8Array>>();const encoder=new TextEncoder();
  const fetcher=vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   if(String(input).endsWith('/chat/stream')){const id=JSON.parse(String(init?.body)).conversation_id;return new Response(new ReadableStream({start(controller){controllers.set(id,controller)}}))}
   return new Response('{}',{status:200});
  });vi.stubGlobal('fetch',fetcher);
  const listener=vi.fn();const a=streamChat({conversation_id:'cancel-A'},vi.fn()),b=streamChat({conversation_id:'cancel-B'},listener);
  await cancelActiveStream('cancel-B');await b;
  expect(listener).toHaveBeenCalledWith(expect.objectContaining({type:'cancelled',conversation_id:'cancel-B'}));
  const bRequest=fetcher.mock.calls.find(([,init])=>String(init?.body).includes('cancel-B'))![1]!;
  const generation=new Headers(bRequest.headers).get('X-OLCR-Generation');
  expect(bRequest.signal?.aborted).toBe(true);
  expect(fetcher.mock.calls.some(([url])=>String(url).endsWith(`/generations/${generation}/cancel`))).toBe(true);
  expect(fetcher.mock.calls.filter(([url])=>String(url).includes('/generations/'))).toHaveLength(1);
  controllers.get('cancel-A')!.enqueue(encoder.encode('data: {"type":"done"}\n\n'));await a;
 });
 it('stops even while waiting for HTTP headers without displaying a network error',async()=>{
  const {cancelActiveStream}=await import('./api');let signal:AbortSignal|null|undefined;
  vi.stubGlobal('fetch',vi.fn((url:RequestInfo|URL,init?:RequestInit)=>{
   if(String(url).endsWith('/chat/stream')){signal=init?.signal;return new Promise(()=>{})}
   return Promise.resolve(new Response('{}'));
  }));
  const listener=vi.fn(),pending=streamChat({conversation_id:'before-headers'},listener);
  await cancelActiveStream('before-headers');await pending;
  expect(signal?.aborted).toBe(true);expect(listener).toHaveBeenCalledOnce();
  expect(listener).toHaveBeenCalledWith(expect.objectContaining({type:'cancelled'}));
 });
 it('keeps partial text and stops a pending read without waiting for another token',async()=>{
  const {cancelActiveStream}=await import('./api');const encoder=new TextEncoder();
  vi.stubGlobal('fetch',vi.fn((url:RequestInfo|URL)=>Promise.resolve(String(url).endsWith('/chat/stream')?
   new Response(new ReadableStream({start(controller){controller.enqueue(encoder.encode('data: {"type":"chunk","text":"Partial answer"}\n\n'))}})):new Response('{}'))));
  const listener=vi.fn(),pending=streamChat({conversation_id:'partial'},listener);
  await vi.waitFor(()=>expect(listener).toHaveBeenCalledWith({type:'chunk',text:'Partial answer'}));
  await cancelActiveStream('partial');await pending;
  expect(listener.mock.calls.map(([event])=>event.type)).toEqual(['chunk','cancelled']);
 });
 it.each(['/chat','/commands'])('also stops a buffered %s answer before meta exists',async path=>{
  const {cancelActiveStream}=await import('./api');
  vi.stubGlobal('fetch',vi.fn((url:RequestInfo|URL)=>String(url).endsWith(path)?new Promise(()=>{}):Promise.resolve(new Response('{}'))));
  const pending=api(path,{method:'POST',body:JSON.stringify({conversation_id:'buffered',message:'fixture'})});
  await cancelActiveStream('buffered');await expect(pending).resolves.toMatchObject({cancelled:true,conversation_id:'buffered'});
 });
 it('does not deliver task notifications or mismatched frames to another conversation',async()=>{
  const encoder=new TextEncoder();vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(new ReadableStream({start(controller){controller.enqueue(encoder.encode(`data: ${JSON.stringify({type:'meta',conversation_id:'wrong',task_id:'wrong-task'})}\n\n`));controller.close()}}))));
  const owner=vi.fn(),other=vi.fn(),offA=subscribeCodingTask(owner,'owner'),offB=subscribeCodingTask(other,'other');
  await expect(streamChat({conversation_id:'owner'},owner)).rejects.toThrow('CONVERSATION_RESPONSE_MISMATCH');expect(owner).not.toHaveBeenCalled();expect(other).not.toHaveBeenCalled();offA();offB();
 });
});

describe('conversation-owned coding notifications',()=>{
 it('publishes and replays a coding task only in the conversation that created it',async()=>{
  const encoder=new TextEncoder(),owner=vi.fn(),other=vi.fn();
  const offA=subscribeCodingTask(owner,'notify-A'),offB=subscribeCodingTask(other,'notify-B');
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(new ReadableStream({start(controller){controller.enqueue(encoder.encode(`data: ${JSON.stringify({type:'done',task:{type:'coding_task_progress',task_id:'coding-notify-A'}})}\n\n`));controller.close()}}))));
  await streamChat({conversation_id:'notify-A'},vi.fn());expect(owner).toHaveBeenCalledWith('coding-notify-A');expect(other).not.toHaveBeenCalled();offA();offB();
  const replay=vi.fn(),unrelated=vi.fn(),offReplay=subscribeCodingTask(replay,'notify-A'),offOther=subscribeCodingTask(unrelated,'notify-B');
  expect(replay).toHaveBeenCalledWith('coding-notify-A');expect(unrelated).not.toHaveBeenCalled();offReplay();offOther();
 });
});


describe("unconfigured backend",()=>{
 it("does not send conversation requests to the frontend origin",async()=>{
  const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher);configureApi('','');
  await expect(api('/conversations',{method:'POST'})).rejects.toThrow('BACKEND_CONFIG_UNAVAILABLE');
  expect(fetcher).not.toHaveBeenCalled();
 });
});


describe('skill command coding handoff',()=>{
 it('publishes a managed coding task returned by a slash command',async()=>{
  configureApi('http://127.0.0.1:9999/api','fixture');
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({conversation_id:'skill-chat',coding_task_id:'skill-task',kind:'coding_task'}))));
  const listener=vi.fn();const unsubscribe=subscribeCodingTask(listener,'skill-chat');
  try{await api('/commands',{method:'POST',body:JSON.stringify({text:'/skill apple-design Build an app',conversation_id:'skill-chat'})});
   expect(listener).toHaveBeenCalledWith('skill-task');
  }finally{unsubscribe();}
 });
});
