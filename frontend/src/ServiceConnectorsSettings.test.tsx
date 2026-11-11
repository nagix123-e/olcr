import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
const invoke=vi.hoisted(()=>vi.fn());
const openExternal=vi.hoisted(()=>vi.fn());
vi.mock('./native',()=>({native:()=>({core:{invoke}}),openExternal}));
vi.mock('./language',()=>({useLanguage:()=>({language:'en'})}));
import {ServiceConnectorsSettings} from './ServiceConnectorsSettings';
afterEach(()=>{cleanup();vi.clearAllMocks()});
const google={id:'service.google.gmail',label:'Gmail',state:'not_connected',enabled:false,permissions:{read:false,write:false,purchase:false},actions:['read']};
const commerce={id:'service.commerce.checkout',label:'Shopping',state:'ready',enabled:false,permissions:{read:false,write:false,purchase:false},actions:['read','write','purchase']};
it('uses the existing settings toggle UI and persists only the requested permission',async()=>{
 const api=vi.fn(async(path:string,init?:RequestInit)=>init?{...commerce,enabled:true}:{connectors:[commerce]});
 render(<ServiceConnectorsSettings api={api}/>);
 const toggle=await screen.findByRole('switch',{name:'Shopping allow access'});
 expect(toggle).not.toBeChecked();
 fireEvent.click(toggle);
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/connectors/service.commerce.checkout/policy',{method:'PATCH',body:JSON.stringify({enabled:true})}));
 await waitFor(()=>expect(toggle).toBeChecked());
 expect(screen.queryByRole('switch',{name:'Shopping purchase access'})).not.toBeInTheDocument();
 expect(screen.queryByRole('switch',{name:'Shopping write access'})).not.toBeInTheDocument();
});
it('groups service tiles under category grants and updates the entire category',async()=>{
 const inventory={connectors:[{...google,category:'mail'}],categories:[{id:'mail',label:'Mail',enabled:false}]};
 const api=vi.fn(async(_path:string,init?:RequestInit)=>init?{...inventory,categories:[{id:'mail',label:'Mail',enabled:true}]}:inventory);
 render(<ServiceConnectorsSettings api={api}/>);
 const category=await screen.findByRole('switch',{name:'Mail category access'});
 fireEvent.click(category);
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/connectors/categories/mail',{method:'PATCH',body:JSON.stringify({enabled:true})}));
 await waitFor(()=>expect(category).toBeChecked());
 expect(screen.getByRole('switch',{name:'Gmail allow access'})).not.toBeChecked();
 expect(screen.getByText('Gmail').closest('.service-connector-tile')).toBeInTheDocument();
});
it('opens Google sign-in and saves the returned credential to Keychain before runtime registration',async()=>{
 const events:string[]=[];invoke.mockImplementation(async()=>{events.push('keychain')});
 const api=vi.fn(async(path:string,init?:RequestInit)=>{
  if(path.endsWith('/oauth/start'))return{id:'attempt',url:'https://accounts.google.com/o/oauth2/v2/auth?state=test'};
  if(path==='/connectors/oauth/attempt')return{status:'ready',bundle:'{"refresh_token":"TEST_ONLY"}',connector_id:google.id};
  if(path.endsWith('/credential')){events.push('runtime');return{}};
  return{connectors:[google]};
 });
 render(<ServiceConnectorsSettings api={api}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Configure Gmail'}));
 fireEvent.change(screen.getByLabelText('Google OAuth client ID'),{target:{value:'client.apps.googleusercontent.com'}});
 fireEvent.click(screen.getByRole('button',{name:'Continue with Google'}));
 await waitFor(()=>expect(invoke).toHaveBeenCalledWith('set_external_credential',{handle:'OLCR_CONNECTOR_GOOGLE_GMAIL',value:'{"refresh_token":"TEST_ONLY"}'}));
 expect(events).toEqual(['keychain','runtime']);
 expect(openExternal).toHaveBeenCalledWith('https://accounts.google.com/o/oauth2/v2/auth?state=test');
 expect(document.body.textContent).not.toContain('TEST_ONLY');
});
it('retains previous policy when saving fails',async()=>{
 const api=vi.fn(async(_path:string,init?:RequestInit)=>{if(init)throw Error('Permission save failed');return{connectors:[commerce]}});
 render(<ServiceConnectorsSettings api={api}/>);
 const toggle=await screen.findByRole('switch',{name:'Shopping allow access'});fireEvent.click(toggle);
 expect(await screen.findByRole('alert')).toHaveTextContent('Permission save failed');expect(toggle).not.toBeChecked();
});
it('does not open a sign-in response after cancellation',async()=>{
 let finish!:(value:any)=>void;
 const api=vi.fn(async(path:string)=>path.endsWith('/oauth/start')?new Promise(resolve=>{finish=resolve}):{connectors:[google]});
 render(<ServiceConnectorsSettings api={api}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Configure Gmail'}));
 fireEvent.change(screen.getByLabelText('Google OAuth client ID'),{target:{value:'client'}});
 fireEvent.click(screen.getByRole('button',{name:'Continue with Google'}));
 fireEvent.click(screen.getByRole('button',{name:'Cancel'}));
 finish({id:'cancelled',url:'https://accounts.google.com/o/oauth2/v2/auth'});
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/connectors/oauth/cancelled',{method:'DELETE'}));
 await waitFor(()=>expect(screen.queryByLabelText('Google OAuth client ID')).not.toBeInTheDocument());
 expect(openExternal).not.toHaveBeenCalled();expect(invoke).not.toHaveBeenCalled();
});
it('stores Shopify credentials in its dedicated Keychain slot before registering runtime',async()=>{
 const events:string[]=[];invoke.mockImplementation(async()=>{events.push('keychain')});
 const api=vi.fn(async(path:string)=>{if(path.endsWith('/credential')){events.push('runtime');return{}}return{connectors:[{...commerce,state:'disconnected'}]}});
 render(<ServiceConnectorsSettings api={api}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Configure Shopping'}));
 fireEvent.change(screen.getByLabelText('Shopify shop domain'),{target:{value:'example.myshopify.com'}});
 fireEvent.change(screen.getByLabelText('Storefront public access token'),{target:{value:'TEST_SHOPIFY_TOKEN'}});
 fireEvent.click(screen.getByRole('button',{name:'Save connection'}));
 const bundle=JSON.stringify({shop:'example.myshopify.com',storefront_token:'TEST_SHOPIFY_TOKEN'});
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/connectors/service.commerce.checkout/credential',{method:'POST',body:JSON.stringify({bundle})}));
 expect(invoke).toHaveBeenCalledWith('set_external_credential',{handle:'OLCR_CONNECTOR_SHOPIFY',value:bundle});expect(events).toEqual(['keychain','runtime']);
 expect(document.body.textContent).not.toContain('TEST_SHOPIFY_TOKEN');expect(openExternal).not.toHaveBeenCalled();
});
it('cancels a pending OAuth listener without waiting for the server response',async()=>{
 const api=vi.fn(async(path:string,init?:RequestInit)=>{
  if(path.endsWith('/oauth/start'))return{id:'pending-attempt',url:'https://accounts.google.com/o/oauth2/v2/auth'};
  if(path==='/connectors/oauth/pending-attempt')return init?.method==='DELETE'?new Promise(()=>{}):{status:'pending'};
  return{connectors:[google]};
 });
 render(<ServiceConnectorsSettings api={api}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Configure Gmail'}));fireEvent.change(screen.getByLabelText('Google OAuth client ID'),{target:{value:'client'}});fireEvent.click(screen.getByRole('button',{name:'Continue with Google'}));
 await screen.findByText('Waiting for browser sign-in…');fireEvent.click(screen.getByRole('button',{name:'Cancel'}));
 expect(api).toHaveBeenCalledWith('/connectors/oauth/pending-attempt',{method:'DELETE'});
 expect(screen.queryByLabelText('Google OAuth client ID')).not.toBeInTheDocument();expect(invoke).not.toHaveBeenCalled();
});
it('keeps Apple Mail connect available after category updates that omit global settings',async()=>{
 const apple={id:'service.apple.mail',label:'Apple Mail',category:'mail',state:'permission_required',enabled:false,permissions:{read:false,send:false},actions:['list','get','send']};
 const initial={connectors:[apple],categories:[{id:'mail',label:'Mail',enabled:false}],external_access_enabled:true};
 const enabled={connectors:[apple],categories:[{id:'mail',label:'Mail',enabled:true}]};
 const api=vi.fn(async(path:string)=>{
  if(path==='/connectors/categories/mail')return enabled;
  if(path==='/connectors/service.apple.mail/connect')return {...enabled,connectors:[{...apple,state:'connected',enabled:true}]};
  return initial;
 });
 render(<ServiceConnectorsSettings api={api}/>);
 const category=await screen.findByRole('switch',{name:'Mail category access'});
 const mail=screen.getByRole('switch',{name:'Apple Mail allow access'});
 expect(mail).toBeDisabled();fireEvent.click(category);
 await waitFor(()=>expect(category).toBeChecked());
 expect(mail).toBeEnabled();fireEvent.click(mail);
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/connectors/service.apple.mail/connect',{method:'POST'}));
 await waitFor(()=>expect(mail).toBeChecked());expect(mail).toBeEnabled();
 expect(screen.getByText('Connected')).toBeVisible();
});
it('respects an explicit external-access revocation in a category response',async()=>{
 const apple={id:'service.apple.mail',label:'Apple Mail',category:'mail',state:'permission_required',enabled:false,permissions:{read:false,send:false},actions:[]};
 const initial={connectors:[apple],categories:[{id:'mail',label:'Mail',enabled:false}],external_access_enabled:true};
 const api=vi.fn(async(_path:string,init?:RequestInit)=>init?{...initial,external_access_enabled:false,categories:[{id:'mail',label:'Mail',enabled:true}]}:initial);
 render(<ServiceConnectorsSettings api={api}/>);
 fireEvent.click(await screen.findByRole('switch',{name:'Mail category access'}));
 await waitFor(()=>expect(screen.getByRole('switch',{name:'Mail category access'})).toBeChecked());
 expect(screen.getByRole('switch',{name:'Apple Mail allow access'})).toBeDisabled();
 expect(api.mock.calls.some(([path])=>path.endsWith('/connect'))).toBe(false);
});
it('opens a persistent OLCR marketplace browser without claiming account connection',async()=>{
 const marketplace={id:'service.commerce.amazon',label:'Amazon.co.jp',category:'shopping',state:'requires_user_sign_in',enabled:true,connection_mode:'browser_handoff',account_connected:false,actions:['search','account','product']};
 const api=vi.fn(async(path:string)=>path==='/commerce-sessions/amazon/open'?{status:'session_open',sign_in_state:'sign_in_unverified'}:{connectors:[marketplace],categories:[{id:'shopping',label:'Shopping',enabled:true}],external_access_enabled:true});
 render(<ServiceConnectorsSettings api={api}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Sign in with OLCR browser: Amazon.co.jp'}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/commerce-sessions/amazon/open',{method:'POST'}));
 expect(await screen.findByText('Complete sign-in in the OLCR browser. OLCR does not verify sign-in.')).toBeInTheDocument();
 expect(screen.queryByText('Connected')).not.toBeInTheDocument();expect(openExternal).not.toHaveBeenCalled();
 expect(screen.getByText(/Sign in in a dedicated OLCR browser/)).toBeInTheDocument();
});
it('stores Yahoo developer search credentials separately from consumer sign-in',async()=>{
 const marketplace={id:'service.commerce.yahoo',label:'Yahoo! Shopping',category:'shopping',state:'requires_user_sign_in',enabled:false,connection_mode:'browser_handoff',account_connected:false,actions:['search','account','product']};
 const api=vi.fn(async()=>({connectors:[marketplace],categories:[{id:'shopping',label:'Shopping',enabled:true}],external_access_enabled:true}));
 render(<ServiceConnectorsSettings api={api}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Configure Yahoo! Shopping'}));
 fireEvent.change(screen.getByLabelText('Yahoo developer Client ID'),{target:{value:'fixture-app-id'}});
 fireEvent.click(screen.getByRole('button',{name:'Save developer API credentials'}));
 const bundle=JSON.stringify({application_id:'fixture-app-id'});
 await waitFor(()=>expect(invoke).toHaveBeenCalledWith('set_external_credential',{handle:'OLCR_CONNECTOR_YAHOO_SHOPPING',value:bundle}));
 expect(api).toHaveBeenCalledWith('/connectors/service.commerce.yahoo/credential',{method:'POST',body:JSON.stringify({bundle})});
 expect(openExternal).not.toHaveBeenCalled();expect(screen.queryByText('Connected')).not.toBeInTheDocument();
});
it('closes marketplace browsers when category access is revoked',async()=>{
 const marketplace={id:'service.commerce.amazon',label:'Amazon.co.jp',category:'shopping',state:'requires_user_sign_in',enabled:true,actions:[]};
 const inventory={connectors:[marketplace],categories:[{id:'shopping',label:'Shopping',enabled:true}],external_access_enabled:true};
 const api=vi.fn(async(path:string)=>path==='/connectors/categories/shopping'?{...inventory,categories:[{id:'shopping',label:'Shopping',enabled:false}]}:inventory);
 render(<ServiceConnectorsSettings api={api}/>);
 fireEvent.click(await screen.findByRole('switch',{name:'Shopping category access'}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/commerce-sessions/amazon/session',{method:'DELETE'}));
 expect(screen.getByRole('button',{name:'Sign in with OLCR browser: Amazon.co.jp'})).toBeDisabled();
});
it('distinguishes an Apple Mail denial and names the responsible OLCR app',async()=>{
 const apple={id:'service.apple.mail',label:'Apple Mail',category:'mail',state:'permission_denied',enabled:false,permissions:{read:true,send:true},actions:['list','get','send']};
 const api=vi.fn(async()=>({connectors:[apple],categories:[{id:'mail',label:'Mail',enabled:true}],external_access_enabled:true}));
 render(<ServiceConnectorsSettings api={api}/>);
 expect(await screen.findByText('Denied in macOS')).toBeVisible();
 expect(screen.getByText(/Allow OLCR to control Mail/)).toBeVisible();
 expect(screen.getByRole('switch',{name:'Apple Mail allow access'})).toBeEnabled();
});
