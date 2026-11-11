import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ProviderAccounts} from './ProviderAccounts';
import {BrainProvider,persistBrainOAuthCredential,registerBrainProvider,signInBrainOAuth} from './brainModels';
import {api} from './api';
const {invoke}=vi.hoisted(()=>({invoke:vi.fn().mockResolvedValue(undefined)}));
vi.mock('./native',()=>({native:()=>({core:{invoke}})}));
afterEach(()=>{cleanup();vi.clearAllMocks();vi.useRealTimers()});
const account=(slot:number):BrainProvider=>({id:'google',label:'Google',auth_type:'api_key',configured:true,account_id:slot,account_name:`Work ${slot}`,model_ids:['shared'],base_url:`https://account-${slot}.example`});
const provider=():BrainProvider=>({...account(1),accounts:[account(1),account(2),account(3)],account_order:[1,2,3]});
it('moves priority without copying credentials or changing stable account slots',async()=>{
 const request=vi.fn().mockResolvedValue({account_order:[2,1,3]}),changed=vi.fn().mockResolvedValue(undefined);
 render(<ProviderAccounts provider={provider()} label="Google" onEdit={vi.fn()} onChanged={changed} request={request as typeof api}/>);
 fireEvent.click(screen.getByText('アカウントと優先順位'));
 fireEvent.click(screen.getByRole('button',{name:'Googleの2位を上へ'}));
 await waitFor(()=>expect(changed).toHaveBeenCalledOnce());
 expect(request).toHaveBeenCalledWith('/brain/providers/google/accounts/order',{method:'PUT',body:JSON.stringify({account_order:[2,1,3]})});
 expect(invoke).not.toHaveBeenCalled();
});
it('opens the correct account settings and prevents a fourth account',()=>{
 const edit=vi.fn();render(<ProviderAccounts provider={provider()} label="Google" onEdit={edit} onChanged={vi.fn()}/>);
 fireEvent.click(screen.getByText('アカウントと優先順位'));
 fireEvent.click(screen.getByRole('button',{name:'Googleの2位を設定'}));
 expect(edit).toHaveBeenCalledWith(expect.objectContaining({account_id:2,account_name:'Work 2',base_url:'https://account-2.example'}));
 expect(screen.queryByRole('button',{name:'＋ アカウントを追加'})).toBeNull();
});
it('adds to the next free slot while keeping a legacy account in slot one',()=>{
 const edit=vi.fn();render(<ProviderAccounts provider={account(1)} label="Google" onEdit={edit} onChanged={vi.fn()}/>);
 fireEvent.click(screen.getByText('アカウントと優先順位'));
 fireEvent.click(screen.getByRole('button',{name:'＋ アカウントを追加'}));
 expect(edit).toHaveBeenCalledWith(expect.objectContaining({account_id:2,configured:false}));
});
it('keeps a failed reorder visible and does not pretend it was saved',async()=>{
 const changed=vi.fn(),request=vi.fn().mockRejectedValue(Error('Save failed'));
 render(<ProviderAccounts provider={provider()} label="Google" onEdit={vi.fn()} onChanged={changed} request={request as typeof api}/>);
 fireEvent.click(screen.getByText('アカウントと優先順位'));
 fireEvent.click(screen.getByRole('button',{name:'Googleの2位を上へ'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('Save failed');expect(changed).not.toHaveBeenCalled();
});
it('saves each API key to the stable account Keychain handle',async()=>{
 const request=vi.fn().mockResolvedValue({});
 const input={id:'google',auth_type:'api_key' as const,account_id:2,values:{api_key:'fixture-only',model_ids:'shared',base_url:'https://fixture.example'}};
 await registerBrainProvider(input,request as typeof api);
 expect(invoke).toHaveBeenCalledWith('set_external_credential',{handle:'OLCR_BRAIN_GOOGLE_2',value:'fixture-only'});
 expect(request).toHaveBeenCalledWith('/brain/providers',{method:'PUT',body:JSON.stringify(input)});
});
it('persists all pending OAuth accounts under their own handles',async()=>{
 let slot=0;
 const request=vi.fn(async(path:string)=>path==='/brain/oauth-credential'?{
  credential:`fixture-${slot+1}`,handle:`OLCR_BRAIN_OPENAI_OAUTH${slot?'_'+(slot+1):''}`,version:`v${slot+1}`
 }:{pending:++slot<3});
 // The acknowledgement uses the same endpoint with POST.
 request.mockImplementation(async(_path:string,init?:RequestInit)=>init?.method==='POST'?{pending:++slot<3}:{
  credential:`fixture-${slot+1}`,handle:`OLCR_BRAIN_OPENAI_OAUTH${slot?'_'+(slot+1):''}`,version:`v${slot+1}`
 });
 await persistBrainOAuthCredential(request as typeof api);
 expect(invoke.mock.calls.map(call=>(call[1] as {handle:string}).handle)).toEqual([
  'OLCR_BRAIN_OPENAI_OAUTH','OLCR_BRAIN_OPENAI_OAUTH_2','OLCR_BRAIN_OPENAI_OAUTH_3']);
});
it('starts OAuth for the chosen account slot',async()=>{
 vi.useFakeTimers();
 const request=vi.fn(async(path:string)=>path.startsWith('/brain/oauth/start')?{attempt_id:'attempt',authorization_url:'https://fixture.example/authorize'}:
  path==='/brain/oauth/attempt'?{status:'complete'}:{credential:null});
 const result=signInBrainOAuth(request as typeof api,'ja',3);
 await vi.advanceTimersByTimeAsync(1100);await result;
 expect(request).toHaveBeenCalledWith('/brain/oauth/start?account_id=3',{method:'POST'});
 expect(invoke).toHaveBeenCalledWith('open_external_url',{url:'https://fixture.example/authorize'});
});

it('offers three ChatGPT OAuth account slots and signs in the selected one',async()=>{
 vi.useFakeTimers();const changed=vi.fn().mockResolvedValue(undefined);
 const request=vi.fn(async(path:string)=>path.startsWith('/brain/oauth/start')?{attempt_id:'slot-two',authorization_url:'https://fixture.example/oauth'}:
  path==='/brain/oauth/slot-two'?{status:'complete'}:{credential:null});
 const oauth:BrainProvider={id:'openai_oauth',label:'ChatGPT',auth_type:'oauth',configured:true,accounts:[
  {id:'openai_oauth',auth_type:'oauth',label:'ChatGPT',configured:true,account_id:1,account_hint:'first@example.test'}],account_order:[1,2,3]};
 render(<ProviderAccounts provider={oauth} label="ChatGPT" onEdit={vi.fn()} onChanged={changed} request={request as typeof api}/>);
 fireEvent.click(screen.getByText('アカウントと優先順位'));
 expect(screen.getByRole('button',{name:'ChatGPTの3位を設定'})).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'ChatGPTの2位を設定'}));
 await vi.advanceTimersByTimeAsync(1100);
 expect(request).toHaveBeenCalledWith('/brain/oauth/start?account_id=2',{method:'POST'});
 expect(changed).toHaveBeenCalledOnce();
});
