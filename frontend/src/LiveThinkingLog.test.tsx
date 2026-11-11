import {act,cleanup,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {LiveThinkingLog,liveLogBlock} from './LiveThinkingLog';
import {appendReasoning} from './ReasoningLog';
const request=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:request}));
vi.mock('./language',()=>({useLanguage:()=>({language:'ja'})}));
afterEach(()=>{cleanup();vi.useRealTimers();vi.resetAllMocks()});
it('updates received text during generation and keeps fallback notices in the same open log',async()=>{
 vi.useFakeTimers();const now=Date.now()/1000;
 let events:any[]=[{state:'request_started',invocation_id:'a',model:'Local model',runtime:'ollama',at:now},{state:'reasoning',invocation_id:'a',text:'first raw chunk',kind:'trace',complete:false,at:now}];
 request.mockImplementation(async()=>({events}));render(<LiveThinkingLog conversationId="chat" active/>);
 await act(async()=>{});expect(screen.getByText('first raw chunk')).toBeInTheDocument();
 expect(document.querySelector('.reasoning-log')).toHaveAttribute('open');
 events=[...events,{state:'request_failed',model:'Local model',failure:'timeout',at:now+1},{state:'fallback',model:'API model',reason:'timeout',at:now+1},{state:'request_started',invocation_id:'b',model:'API model',runtime:'google',at:now+1},{state:'reasoning',invocation_id:'b',text:'received summary',kind:'summary',at:now+1}];
 await act(async()=>{vi.advanceTimersByTime(500)});
 expect(screen.getByText(/フォールバック: API model/)).toBeInTheDocument();expect(screen.getByText('received summary')).toBeInTheDocument();expect(screen.getByText('first raw chunk')).toBeInTheDocument();
});
it('keeps model text verbatim and elapsed waiting time in an OLCR status entry',()=>{
 const block=liveLogBlock([{state:'reasoning',text:'  raw\ntext  ',kind:'trace',at:10}],true,true,17);
 expect(block.logs[0].text).toBe('  raw\ntext  ');expect(block.logs[1].kind).toBe('status');expect(block.logs[1].text).toContain('7秒');
});
it('does not concatenate thought channels from different models',()=>{
 const first=appendReasoning(undefined,{text:'old model',kind:'trace',complete:false,truncated:false},'first');
 const next=appendReasoning(first,{text:'new model',kind:'summary',complete:false,truncated:false},'second');
 expect(next.logs.map(log=>log.text)).toEqual(['old model','new model']);
});

it('explains a preflight skip and its retry time without calling it a model invocation',()=>{
 const block=liveLogBlock([{state:'model_skipped',model:'Unavailable model',reason:'rate_limited',retry_at:200,at:100}],true,false,100);
 expect(block.logs[0].kind).toBe('status');expect(block.logs[0].text).toContain('呼び出さずスキップ');expect(block.logs[0].text).toContain('再試行可能時刻');expect(block.logs[0].text).toContain('利用上限');
});

it('explains a structured reasoning budget failure in the selected language',()=>{
 const event={state:'request_failed',model:'Local model',failure:'reasoning_budget',at:100};
 expect(liveLogBlock([event],true,false,100).logs[0].text).toContain('実行用JSON');
 expect(liveLogBlock([event],false,false,100).logs[0].text).toContain('reasoning budget');
});

it('shows a terminal cancellation instead of leaving the log waiting on the last generation update',()=>{
 const block=liveLogBlock([{state:'request_cancelled',invocation_id:'stopped',model:'Local model',failure:'cancelled',at:100}],true,true,140);
 expect(block.logs).toHaveLength(1);
 expect(block.logs[0].text).toContain('停止');
});

it('explains that a structured reasoning-budget failure is retrying the same model without thinking',()=>{
 const event={state:'worker_progress',event:'structured_no_thinking_retry',model:'ornith-1.5:9b',failure:'reasoning_budget',at:100};
 const japanese=liveLogBlock([event],true,false,100).logs[0].text;
 const english=liveLogBlock([event],false,false,100).logs[0].text;
 expect(japanese).toContain('ornith-1.5:9b');expect(japanese).toContain('同じモデル');expect(japanese).toContain('思考なし');
 expect(english).toContain('ornith-1.5:9b');expect(english).toContain('same model');expect(english).toContain('without thinking');
});

it('distinguishes preflight schema fallback from an API rejection and retry',()=>{
 const preflight=liveLogBlock([{state:'schema_compatibility',model:'gpt-6-luna',reason:'schema_unsupported_by_native_mode',at:100}],true,false,100).logs[0].text;
 const rejected=liveLogBlock([{state:'schema_compatibility',model:'gpt-6-luna',reason:'provider_rejected_schema',http_status:400,provider_code:'invalid_json_schema',parameter:'response_format',at:100}],true,false,100).logs[0].text;
 expect(preflight).toContain('ネイティブ制約');expect(preflight).toContain('同じモデル');expect(preflight).not.toContain('再試行');
 expect(rejected).toContain('HTTP 400');expect(rejected).toContain('invalid_json_schema');expect(rejected).toContain('同じモデルを再試行');
});

it('renders unavailable token counts using received characters and compacts legacy progress',()=>{
 const events=Array.from({length:500},(_,i)=>({state:'worker_progress',invocation_id:'a',event:'ollama_generation_progress',generated_tokens:null,output_chars:i,at:i}));
 const block=liveLogBlock(events,true,false,500);
 expect(block.logs).toHaveLength(1);
 expect(block.logs[0].text).not.toContain('null');
 expect(block.logs[0].text).toContain('499');
});

it('uses the newest snapshot time when reasoning rows precede a live answer update',()=>{
 const block=liveLogBlock([{state:'worker_progress',event:'ollama_generation_progress',at:100},{state:'reasoning',text:'earlier thought',at:20}],false,true,101);
 expect(block.logs.at(-1)?.text).toContain('Since last log 1s.');
});
