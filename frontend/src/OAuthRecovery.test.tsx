import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
const invoke=vi.hoisted(()=>vi.fn());
vi.mock('./native',()=>({native:()=>({core:{invoke}}),openExternal:vi.fn()}));
import {BrainModelPicker} from './BrainModelPicker';
import {BrainState,INITIAL_BRAIN,persistBrainOAuthCredential,useBrainModels} from './brainModels';
afterEach(()=>{cleanup();vi.clearAllMocks();vi.useRealTimers()});
describe('OAuth credential recovery',()=>{
 it('serializes Keychain writes and acknowledges only the saved version',async()=>{
  let finish!:()=>void;invoke.mockImplementation(()=>new Promise<void>(resolve=>{finish=resolve}));
  const request=vi.fn().mockImplementation((path:string,init?:RequestInit)=>Promise.resolve(init?{}:{credential:'fixture-only',version:'version-1'}));
  const first=persistBrainOAuthCredential(request),second=persistBrainOAuthCredential(request);
  expect(first).toBe(second);
  await Promise.resolve();expect(invoke).toHaveBeenCalledTimes(1);
  expect(request.mock.calls.some(([,init])=>init?.method==='POST')).toBe(false);
  finish();await first;
  expect(request).toHaveBeenCalledWith('/brain/oauth-credential',{method:'POST',body:JSON.stringify({version:'version-1'})});
 });
 it('keeps background rotation silent and allows consent only for explicit sign-in',async()=>{
  invoke.mockResolvedValue(undefined);
  const request=vi.fn().mockResolvedValue({credential:'fixture-only',version:'v1'});
  await persistBrainOAuthCredential(request);
  expect(invoke).toHaveBeenLastCalledWith('set_external_credential',{handle:'OLCR_BRAIN_OPENAI_OAUTH',value:'fixture-only',interactive:false});
  await persistBrainOAuthCredential(request,'ja',true);
  expect(invoke).toHaveBeenLastCalledWith('set_external_credential',{handle:'OLCR_BRAIN_OPENAI_OAUTH',value:'fixture-only',interactive:true});
 });
 it('drains a newer rotation when the saved version was superseded',async()=>{
  invoke.mockResolvedValue(undefined);
  const request=vi.fn().mockResolvedValueOnce({credential:'old-fixture',version:'v1'}).mockResolvedValueOnce({saved:false,pending:true}).mockResolvedValueOnce({credential:'new-fixture',version:'v2'}).mockResolvedValueOnce({saved:true,pending:false});
  await persistBrainOAuthCredential(request);
  expect(invoke.mock.calls.map(([,value])=>value.value)).toEqual(['old-fixture','new-fixture']);
  expect(request).toHaveBeenLastCalledWith('/brain/oauth-credential',{method:'POST',body:JSON.stringify({version:'v2'})});
 });
 it('does not acknowledge a failed Keychain save and permits retry',async()=>{
  const request=vi.fn().mockResolvedValue({credential:'fixture-only',version:'version-1'});
  invoke.mockRejectedValueOnce(Error('Keychain unavailable'));
  await expect(persistBrainOAuthCredential(request)).rejects.toThrow('Keychain unavailable');
  expect(request.mock.calls.some(([,init])=>init?.method==='POST')).toBe(false);
  invoke.mockResolvedValue(undefined);await persistBrainOAuthCredential(request);
  expect(request.mock.calls.some(([,init])=>init?.method==='POST')).toBe(true);
 });
 it('keeps catalog usable if restoring Keychain fails and flushes rotations without focus',async()=>{
  vi.useFakeTimers();invoke.mockRejectedValue(Error('Keychain unavailable'));
  const request=vi.fn().mockImplementation((path:string)=>Promise.resolve(path==='/brain/models'?{models:[],providers:[{id:'openai_oauth',configured:true,auth_type:'oauth',label:'ChatGPT'}]}:path.startsWith('/brain/configuration')?INITIAL_BRAIN:path==='/brain/usage'?{models:{},week_starts_at:0}:{credential:null}));
  let state!:BrainState;function Probe(){state=useBrainModels(true,null,request);return <span>{state.catalog.providers.length}</span>}
  render(<Probe/>);await act(async()=>{});
  expect(screen.getByText('1')).toBeInTheDocument();expect(state.error).toBe('Keychain unavailable');
  const before=request.mock.calls.filter(([path])=>path==='/brain/oauth-credential').length;
  await act(async()=>{vi.advanceTimersByTime(3100)});
  expect(request.mock.calls.filter(([path])=>path==='/brain/oauth-credential').length).toBeGreaterThan(before);
 });
 it('distinguishes expired OAuth from an empty model catalog and offers reconnect',()=>{
  const state:BrainState={catalog:{models:[],providers:[{id:'openai_oauth',label:'ChatGPT',auth_type:'oauth',configured:true,connection_state:'reauth_required'}]},configuration:structuredClone(INITIAL_BRAIN),usage:{models:{},week_starts_at:0},loading:false,error:'',save:vi.fn(),refresh:vi.fn(),refreshUsage:vi.fn(),resetUsage:vi.fn()};
  render(<BrainModelPicker state={state}/>);
  fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));
  fireEvent.click(screen.getByRole('button',{name:'コーディングのモデルを選択'}));
  expect(screen.getByRole('button',{name:/OpenAI.*ChatGPTの再接続が必要/})).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:/OpenAI/}));
  expect(screen.getByRole('button',{name:'ChatGPTに再接続'})).toBeInTheDocument();
 });
});
