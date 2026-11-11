import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {GitHubSettings} from './GitHubSettings';
import {openExternal} from './native';
vi.mock('./native',()=>({openExternal:vi.fn(async()=>{})}));
afterEach(()=>{cleanup();vi.clearAllMocks()});
it('opens browser sign-in from Settings and reflects the connected account automatically',async()=>{
 let connected=false;
 const api=vi.fn(async(path:string,init?:RequestInit)=>{
  if(path==='/github/connection')return {installed:true,connected,account:connected?'fixture':undefined,execution_mode:'autonomous'};
  if(path==='/github/connect')return {id:'one',status:'pending',code:'',url:'https://github.com/login/device'};
  if(path==='/github/connect/one')return {id:'one',status:connected?'connected':'pending',code:'ABCD-1234',url:'https://github.com/login/device'};
  if(path==='/github/policy')return JSON.parse(String(init?.body));
  throw Error(path);
 });
 render(<GitHubSettings api={api}/>);
 fireEvent.click(await screen.findByRole('button',{name:'GitHubに接続'}));
 await screen.findByText('ABCD-1234');expect(openExternal).toHaveBeenCalledWith('https://github.com/login/device');
 connected=true;await screen.findByText('接続済み · @fixture',{}, {timeout:3500});
 const toggle=screen.getByRole('switch');expect(toggle).toBeChecked();fireEvent.click(toggle);
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/github/policy',{method:'PUT',body:JSON.stringify({execution_mode:'review'})}));
 expect(openExternal).toHaveBeenCalledTimes(1);
});
it('offers CLI installation when the browser authentication dependency is missing',async()=>{
 render(<GitHubSettings api={vi.fn(async()=>({installed:false,connected:false,execution_mode:'review'}))}/>);
 fireEvent.click(await screen.findByRole('button',{name:'GitHub CLIをインストール'}));
 expect(openExternal).toHaveBeenCalledWith('https://cli.github.com/');
});
