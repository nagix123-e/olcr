import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
const invoke=vi.hoisted(()=>vi.fn());
vi.mock('./native',()=>({native:()=>({core:{invoke}})}));
import {CredentialAccessNotice} from './CredentialAccessNotice';
afterEach(()=>{cleanup();vi.resetAllMocks()});
it('authorizes only on an explicit click and then refreshes provider status',async()=>{
  invoke.mockResolvedValue(2);const refresh=vi.fn().mockResolvedValue(undefined);
  const view=render(<CredentialAccessNotice error="credential_bundle_authorization_required" onRestored={refresh}/>);
  view.rerender(<CredentialAccessNotice error="credential_bundle_authorization_required" onRestored={refresh}/>);
  expect(invoke).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'認証情報へのアクセスを許可'}));
  await waitFor(()=>expect(refresh).toHaveBeenCalledOnce());
  expect(invoke).toHaveBeenCalledExactlyOnceWith('restore_external_api_credentials',{authorize:true});
});
it('does not retry a denied authorization automatically',async()=>{
  invoke.mockRejectedValue(Error('credential_bundle_authorization_required'));const refresh=vi.fn();
  render(<CredentialAccessNotice error="credential_bundle_authorization_required" onRestored={refresh}/>);
  fireEvent.click(screen.getByRole('button',{name:'認証情報へのアクセスを許可'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('許可が完了していません');
  expect(invoke).toHaveBeenCalledTimes(1);expect(refresh).not.toHaveBeenCalled();
});
