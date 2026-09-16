import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock=vi.hoisted(()=>vi.fn());
const subscribeMock=vi.hoisted(()=>vi.fn(()=>()=>{}));
vi.mock("./api",()=>({api:apiMock,subscribeCodingTask:subscribeMock}));
import { ChatComposer } from "./ChatComposer";

type Deferred<T>={promise:Promise<T>;resolve:(value:T)=>void};
function deferred<T>():Deferred<T>{let resolve!: (value:T)=>void;const promise=new Promise<T>(value=>{resolve=value});return {promise,resolve}}
const graph=[{task_id:"t1",phase_id:"p1",goal:"Current task",depends_on:[],verification:[]}];
function task(id:string,status="RUNNING",goal="Current task") {return {id,status,activity:"QWEN_IMPLEMENTATION",current_phase_id:"p1",retry_count:0,max_retries:2,plan:{phases:[{id:"p1",goal,status:"pending",dependencies:[]}],tasks:[{...graph[0],task_id:`${id}-t`,goal}]},subtask_progress:[{task_id:`${id}-t`,phase_id:"p1",status:status==="COMPLETED"?"DONE":"RUNNING",verification_status:"UNVERIFIED",attempt:0}]}}
function props(conversationId="a"){return {text:"",setText:vi.fn(),disabled:false,loading:false,send:(event:React.FormEvent)=>event.preventDefault(),commands:[],setAttachment:vi.fn(),conversationId}}
const reports={reports:[]};

beforeEach(()=>{vi.useFakeTimers();apiMock.mockReset();subscribeMock.mockClear()});
afterEach(()=>{cleanup();vi.useRealTimers()});

async function flush(){await act(async()=>{await Promise.resolve();await Promise.resolve()})}
function count(path:string){return apiMock.mock.calls.filter(([value])=>value===path).length}

describe("ChatComposer persisted progress polling",()=>{
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
    const view=render(<ChatComposer {...props("a")}/>);await flush();view.rerender(<ChatComposer {...props("b")}/>);await flush();expect(screen.getByLabelText("Current task")).toHaveTextContent("New task");
    await act(async()=>{oldList.resolve({tasks:[a]});await Promise.resolve()});
    expect(screen.getByLabelText("Current task")).toHaveTextContent("New task");
  });
  it("cleans up a disposed active poller",async()=>{
    const active=task("a"),pending=deferred<ReturnType<typeof task>>();
    apiMock.mockImplementation((path:string)=>path==="/conversations/a/coding-tasks"?Promise.resolve({tasks:[active]}):path==="/coding-tasks/a"?pending.promise:Promise.resolve(reports));
    const view=render(<ChatComposer {...props()}/>);await flush();expect(count("/coding-tasks/a")).toBe(1);view.unmount();
    await act(async()=>{pending.resolve(active);await vi.advanceTimersByTimeAsync(4000)});expect(count("/coding-tasks/a")).toBe(1);
  });
  it("restores persisted progress and fetches phase reports",async()=>{
    const active=task("a");apiMock.mockImplementation((path:string)=>path==="/conversations/a/coding-tasks"?Promise.resolve({tasks:[active]}):path==="/coding-tasks/a"?Promise.resolve(active):Promise.resolve(reports));
    render(<ChatComposer {...props()}/>);await flush();expect(screen.getByLabelText("Current task")).toHaveTextContent("Current task");
    expect(count("/coding-tasks/a/phase-reports")).toBe(1);
  });
});
