import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {GitWorkspacePanel} from './GitWorkspacePanel';
const request=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:request}));
vi.mock('./native',()=>({openExternal:vi.fn(async()=>{})}));
afterEach(()=>{cleanup();vi.restoreAllMocks()});
const pending={id:'review1',action:'commit',status:'PENDING',workspace:'/fixture',branch:'main',head:'abcdef',message:'Fix',paths:['a.py'],diff:'+fixed'};
describe('Git review',()=>{
  it('shows pending diff without executing and requires the approve button',async()=>{
    let approved=false;
    request.mockImplementation(async(path:string)=>{
      if(path.endsWith('/approve')){approved=true;return {...pending,status:'SUCCEEDED'}}
      return {available:true,operations:[{...pending,status:approved?'SUCCEEDED':'PENDING'}]};
    });
    render(<GitWorkspacePanel conversationId="c" ja/>);
    await screen.findByRole('button',{name:'承認して実行'});
    expect(request.mock.calls.some(([path])=>String(path).endsWith('/approve'))).toBe(false);
    const disclosure=screen.getByText('コミット対象の全文差分');
    fireEvent.click(disclosure);(disclosure.parentElement as HTMLDetailsElement).open=true;
    await act(async()=>window.dispatchEvent(new Event('focus')));
    expect((disclosure.parentElement as HTMLDetailsElement).open).toBe(true);
    fireEvent.click(screen.getByRole('button',{name:'承認して実行'}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith('/conversations/c/git/operations/review1/approve',{method:'POST'}));
    await waitFor(()=>expect(screen.queryByRole('button',{name:'承認して実行'})).toBeNull());
  });
  it('rejection uses only the reject endpoint',async()=>{
    let rejected=false;
    request.mockImplementation(async(path:string)=>{
      if(path.endsWith('/reject')){rejected=true;return {...pending,status:'REJECTED'}}
      return {available:true,operations:[{...pending,status:rejected?'REJECTED':'PENDING'}]};
    });
    render(<GitWorkspacePanel conversationId="c" ja/>);
    fireEvent.click(await screen.findByRole('button',{name:'取り消す'}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith('/conversations/c/git/operations/review1/reject',{method:'POST'}));
    expect(request.mock.calls.some(([path])=>String(path).endsWith('/approve'))).toBe(false);
  });
  it('selects exact files and prepares a review before mutation',async()=>{
    const status={workspace:'/fixture',branch:'main',head:'abc',files:[{path:'a.py',status:' M'},{path:'user.py',status:'M '}],remotes:[],setup_command:'gh auth login',setup_url:'https://cli.github.com/manual/gh_auth_login'};
    request.mockImplementation(async(path:string)=>path.endsWith('/status')?status:path.endsWith('/prepare')?pending:{available:true,operations:[]});
    render(<GitWorkspacePanel conversationId="c" ja/>);
    await waitFor(()=>expect(request).toHaveBeenCalledWith('/conversations/c/git/operations'));
    fireEvent.click(screen.getByRole('button',{name:/Git \/ GitHub/}));
    fireEvent.click(await screen.findByRole('checkbox',{name:/a.py/}));
    fireEvent.change(screen.getByLabelText('コミットメッセージ'),{target:{value:'Fix'}});
    fireEvent.click(screen.getByRole('button',{name:'コミット差分を確認'}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith('/conversations/c/git/prepare',{method:'POST',body:JSON.stringify({action:'commit',paths:['a.py'],message:'Fix'})}));
    expect(request.mock.calls.some(([path])=>String(path).endsWith('/approve'))).toBe(false);
  });
});
