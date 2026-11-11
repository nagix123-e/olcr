import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
vi.mock('./language', () => ({ useLanguage: () => ({ language: 'en', setLanguage: vi.fn() }) }));
import {WebSettings} from './WebSettings';

afterEach(()=>{
 cleanup();
 delete (window as Window & {__TAURI__?:unknown}).__TAURI__;
});

describe('Web Search settings',()=>{
 it('keeps the selected provider and mode when saving its Keychain credential',async()=>{
  let persisted={web_mode:'off',web_provider:'none'};
  const api=vi.fn(async(path:string,init?:RequestInit)=>{
   if(path==='/web/settings'&&init?.method==='PUT'){
    persisted=JSON.parse(String(init.body));
   }
   if(path==='/web/credentials')return {web_mode:persisted.web_mode,web_provider:persisted.web_provider,
    providers:['none','brave','tavily','duckduckgo'],
    credentials:{none:'Disabled',duckduckgo:'Not required',brave:'Configured',tavily:'Not configured'}};
   return {web_mode:persisted.web_mode,web_provider:persisted.web_provider,
    providers:['none','brave','tavily','duckduckgo'],
    credentials:{none:'Disabled',duckduckgo:'Not required',brave:'Configured',tavily:'Not configured'}};
  });
  const invoke=vi.fn().mockImplementation(()=>new Promise(resolve=>setTimeout(resolve,25)));
  (window as Window & {__TAURI__?:unknown}).__TAURI__={core:{invoke},event:{listen:vi.fn()}};
  render(<WebSettings api={api}/>);
  const provider=await screen.findByLabelText('Provider');
  expect(screen.getByText(/Brave API key: Configured/)).toBeInTheDocument();
  expect(screen.getByText(/Web Search: Disabled/)).toBeInTheDocument();
  fireEvent.change(provider,{target:{value:'brave'}});
  fireEvent.change(screen.getByLabelText('Mode'),{target:{value:'manual'}});
  fireEvent.click(screen.getByRole('button',{name:'Change'}));
  fireEvent.change(screen.getByLabelText('API key (saved in macOS Keychain)'),{target:{value:'test-key'}});
  fireEvent.click(screen.getByRole('button',{name:'Configure API key'}));
  await waitFor(()=>expect(screen.getByRole('status')).toHaveTextContent('macOS may ask for your login Keychain password'));
  await waitFor(()=>expect(screen.getByLabelText('Provider')).toHaveValue('brave'));
  expect(screen.getByLabelText('Mode')).toHaveValue('manual');
  fireEvent.click(screen.getByRole('button',{name:'Save Web settings'}));
  await waitFor(()=>expect(persisted).toEqual({web_mode:'manual',web_provider:'brave'}));
  expect(invoke).toHaveBeenCalledWith('set_web_credential',{provider:'brave',key:'test-key'});
  expect(api).toHaveBeenCalledWith('/web/settings',{method:'PUT',body:JSON.stringify({web_mode:'manual',web_provider:'brave'})});
 });

 it('explains how to recover when macOS Keychain approval times out',async()=>{
  const api=vi.fn(async()=>({web_mode:'auto',web_provider:'brave',providers:['none','brave','tavily'],credentials:{}}));
  const invoke=vi.fn().mockRejectedValue(new Error('credential_bundle_bridge_timeout'));
  (window as Window & {__TAURI__?:unknown}).__TAURI__={core:{invoke},event:{listen:vi.fn()}};
  render(<WebSettings api={api}/>);
  await screen.findByLabelText('Provider');
  fireEvent.change(screen.getByLabelText('API key (saved in macOS Keychain)'),{target:{value:'test-key'}});
  fireEvent.click(screen.getByRole('button',{name:'Configure API key'}));
  expect(await screen.findByRole('status')).toHaveTextContent('Complete the macOS Keychain dialog, then try saving again.');
 });
});
