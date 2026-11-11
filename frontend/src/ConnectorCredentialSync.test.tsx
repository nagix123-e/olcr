import {cleanup,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
const invoke=vi.hoisted(()=>vi.fn());
vi.mock('./native',()=>({native:()=>({core:{invoke}})}));
vi.mock('./language',()=>({useLanguage:()=>({language:'en'})}));
import {ConnectorCredentialSync} from './ConnectorCredentialSync';
afterEach(()=>{cleanup();vi.clearAllMocks()});
const block={type:'service_credential_sync' as const,connector_id:'service.google.gmail'};
it('persists a refreshed bundle in Keychain before acknowledging it to the backend',async()=>{
 const events:string[]=[];invoke.mockImplementation(async()=>{events.push('keychain')});
 const api=vi.fn(async(_path:string,init?:RequestInit)=>{if(init){events.push('ack');return{}}return{bundle:'TEST_ONLY_OAUTH_BUNDLE'}});
 const view=render(<ConnectorCredentialSync block={block} api={api}/>);
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/connectors/service.google.gmail/credential-sync',{method:'POST',body:JSON.stringify({bundle:'TEST_ONLY_OAUTH_BUNDLE'})}));
 expect(invoke).toHaveBeenCalledWith('set_external_credential',{handle:'OLCR_CONNECTOR_GOOGLE_GMAIL',value:'TEST_ONLY_OAUTH_BUNDLE'});expect(events).toEqual(['keychain','ack']);
 expect(view.container.textContent).toBe('');expect(document.body.textContent).not.toContain('TEST_ONLY_OAUTH_BUNDLE');
});
it('performs no Keychain write or acknowledgement when no refreshed bundle is pending',async()=>{
 const api=vi.fn().mockResolvedValue({bundle:null});render(<ConnectorCredentialSync block={block} api={api}/>);
 await waitFor(()=>expect(api).toHaveBeenCalledOnce());await Promise.resolve();
 expect(invoke).not.toHaveBeenCalled();expect(api).toHaveBeenCalledWith('/connectors/service.google.gmail/credential-sync');
});
it('reports only a generic error and does not acknowledge after Keychain failure',async()=>{
 invoke.mockRejectedValueOnce(Error('SECRET_TOKEN_IN_ERROR'));
 const api=vi.fn().mockResolvedValue({bundle:'SECRET_TOKEN'});render(<ConnectorCredentialSync block={block} api={api}/>);
 expect(await screen.findByRole('alert')).toHaveTextContent('Could not save the refreshed connection. Reconnect in Settings.');
 expect(document.body.textContent).not.toContain('SECRET_TOKEN');expect(api).toHaveBeenCalledOnce();
});
