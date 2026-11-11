import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
const mocks=vi.hoisted(()=>({api:vi.fn(),transcribe:vi.fn(),microphone:vi.fn(),release:vi.fn(),sample:null as null|((chunk:Float32Array,rate:number)=>void)}));
vi.mock('./api',()=>({api:mocks.api,streamTranscribeAudio:mocks.transcribe}));
vi.mock('./handsfreeAudio',async()=>({...await vi.importActual('./handsfreeAudio'),microphone:mocks.microphone}));
vi.mock('./native',()=>({native:()=>undefined}));
import {HandsFreePanel} from './HandsFreePanel';
const status={phase:'ready',enabled:true,installed:true,session:'test-session',downloaded_bytes:0,profile:{label:'OLCR',count:3,threshold:.1}};
function renderPanel(onCommand:(text:string)=>void=()=>{}){
 const props={language:'ja' as const,busy:false,onCommand,onActive:()=>{},onClose:()=>{}};
 const view=render(<HandsFreePanel {...props} startSignal={0}/>);
 return {start:async()=>{view.rerender(<HandsFreePanel {...props} startSignal={1}/>);await act(async()=>{await vi.advanceTimersByTimeAsync(0)});await flush()},
 stop:()=>fireEvent.click(screen.getByRole('button',{name:'ハンズフリーを停止して閉じる'}))};
}
async function flush(){await act(async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve()})}
beforeEach(()=>{vi.useFakeTimers();mocks.api.mockReset();mocks.transcribe.mockReset();mocks.release.mockReset();mocks.sample=null;mocks.microphone.mockImplementation(async(callback)=>{mocks.sample=callback;return mocks.release});mocks.api.mockImplementation(async(path:string)=>path==='/handsfree/enable'||path.startsWith('/handsfree?')?status:{});});
afterEach(()=>{cleanup();vi.useRealTimers()});

describe('Hands-free opt-in and microphone handoff',()=>{
 it('does not acquire assets or open devices until ON, and stops input on OFF',async()=>{
  const panel=renderPanel();
  expect(mocks.api).not.toHaveBeenCalled();expect(mocks.microphone).not.toHaveBeenCalled();
  await panel.start();
  expect(mocks.api).toHaveBeenCalledWith('/handsfree/enable',expect.objectContaining({method:'POST'}));expect(mocks.microphone).toHaveBeenCalledOnce();
  panel.stop();await flush();
  expect(mocks.release).toHaveBeenCalledOnce();
  expect(mocks.api).toHaveBeenCalledWith('/handsfree/disable',expect.objectContaining({body:JSON.stringify({session:'test-session'})}));
 });
 it('keeps the lease alive during macOS permission wait and releases a late microphone',async()=>{
  let accept:(stop:()=>void)=>void=()=>{};mocks.microphone.mockImplementation(()=>new Promise(resolve=>{accept=resolve}));
  const panel=renderPanel();
  await panel.start();
  await act(async()=>{await vi.advanceTimersByTimeAsync(6000)});
  expect(mocks.api.mock.calls.filter(([path])=>path.startsWith('/handsfree?')).length).toBeGreaterThan(2);
  panel.stop();await act(async()=>accept(mocks.release));await flush();
  expect(mocks.release).toHaveBeenCalledOnce();
 });
 it('cuts wake detection during voice capture and sends the recognized command once',async()=>{
  const onCommand=vi.fn();mocks.api.mockImplementation(async(path:string,options?:RequestInit)=>{
   if(path==='/handsfree/enable'||path.startsWith('/handsfree?'))return status;
   if(path==='/handsfree/input'&&JSON.parse(String(options?.body)).op==='wake')return {detected:true};return {};
  });mocks.transcribe.mockResolvedValue('入力テスト');
  const panel=renderPanel(onCommand);
  await panel.start();
  // No AudioContext is needed for the quiet chime in this device-free test.
  const audioContext=vi.fn(function(this:any){this.currentTime=0;this.destination={};this.createOscillator=()=>({frequency:{},connect(){},start(){},stop(){},onended:null});this.createGain=()=>({gain:{},connect(){}});this.close=async()=>{}});
  vi.stubGlobal('AudioContext',audioContext);
  await act(async()=>{vi.advanceTimersByTime(1000);mocks.sample!(new Float32Array(32000).fill(.05),16000)});await flush();
  expect(screen.getByText(/指示を聞いています/)).toBeTruthy();
  const wakeCalls=()=>mocks.api.mock.calls.filter(([,o])=>o?.body&&JSON.parse(String(o.body)).op==='wake').length;
  expect(wakeCalls()).toBe(1);
  await act(async()=>{mocks.sample!(new Float32Array(6400).fill(.05),16000);mocks.sample!(new Float32Array(16000),16000)});await flush();
  expect(mocks.transcribe).toHaveBeenCalledOnce();expect(wakeCalls()).toBe(1);expect(onCommand).toHaveBeenCalledExactlyOnceWith('入力テスト');
  vi.unstubAllGlobals();
 });
 it('cancels a pending enable without leaving a late backend session running',async()=>{
  let finish:(value:typeof status)=>void=()=>{};mocks.api.mockImplementation((path)=>path==='/handsfree/enable'?new Promise(resolve=>{finish=resolve}):Promise.resolve({}));
  const panel=renderPanel();
  await panel.start();panel.stop();
  await act(async()=>finish(status));await flush();expect(mocks.microphone).not.toHaveBeenCalled();
  expect(mocks.api).toHaveBeenCalledWith('/handsfree/disable',expect.objectContaining({body:JSON.stringify({session:'test-session'})}));
 });
});
