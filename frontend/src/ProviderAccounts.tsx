import {useState} from 'react';
import type {ReactNode} from 'react';
import {api} from './api';
import {BrainProvider,signInBrainOAuth} from './brainModels';
import {useLanguage} from './language';

export function ProviderAccounts({provider,label,onEdit,onChanged,request=api,editor}:{
 provider:BrainProvider;label:string;onEdit:(account:BrainProvider)=>void;onChanged:()=>Promise<void>;request?:typeof api;editor?:ReactNode;
}) {
 const {language}=useLanguage();const ja=language==='ja';
 const [busy,setBusy]=useState(false),[error,setError]=useState('');
 const order=provider.account_order||[1,2,3];
 const accounts=order.map((slot,index)=>{
  const account=provider.accounts?.find(row=>row.account_id===slot);
  return account||{...(slot===1?provider:{id:provider.id,label:provider.label,auth_type:provider.auth_type,configured:false}),account_id:slot,rank:index+1};
 });
 const primary=accounts.find(account=>account.configured)||accounts[0];
 const count=accounts.filter(account=>account.configured).length;
 const oauth=provider.auth_type==='oauth';
 const configure=async(account:BrainProvider)=>{
  if(!oauth){onEdit(account);return;}
  setBusy(true);setError('');
  try{await signInBrainOAuth(request,language,account.account_id||1);await onChanged()}
  catch(e){setError(e instanceof Error?e.message:String(e))}
  finally{setBusy(false)}
 };
 const move=async(index:number,direction:-1|1)=>{
  const next=[...order];[next[index],next[index+direction]]=[next[index+direction],next[index]];
  setBusy(true);setError('');
  try{await request(`/brain/providers/${encodeURIComponent(provider.id)}/accounts/order`,{method:'PUT',body:JSON.stringify({account_order:next})});await onChanged()}
  catch(e){setError(e instanceof Error?e.message:String(e))}
  finally{setBusy(false)}
 };
 const status=(account:BrainProvider)=>account.connection_state==='reauth_required'?(ja?'再接続が必要':'Reconnect required'):
  account.configured?(account.account_hint||(ja?'設定済み':'Configured')):(ja?'未接続':'Not connected');
 const buttonLabel=(account:BrainProvider)=>oauth?(ja?(busy?'ChatGPTで認証中…':account.configured?'ChatGPTに再接続':'ChatGPTで続行'):(busy?'Connecting to ChatGPT…':account.configured?'Reconnect ChatGPT':'Continue with ChatGPT')):
  ja?(account.configured?'編集':'設定'):(account.configured?'Edit':'Configure');
 return <div className="brain-provider-group">
  <div className="brain-provider-row"><div><strong>{label}</strong><small>{status(primary)} · {count}/3</small>
   {primary.availability_message&&<small role="status">{primary.availability_message}</small>}</div>
   <button type="button" disabled={busy} onClick={()=>void configure(primary)}>{buttonLabel(primary)}</button>
  </div>
  <details className="provider-account-details"><summary>{ja?'アカウントと優先順位':'Accounts and priority'}</summary>
   <p>{ja?'同じモデルを1位から順に試し、すべて利用できない場合に次のモデルへ進みます。':'Try this model with each account in priority order before moving to the next model.'}</p>
   <ol className="provider-account-list" aria-label={ja?`${label}のアカウント優先順位`:`${label} account priority`}>
    {accounts.map((account,index)=><li key={account.account_id}>
     <div><strong>{ja?`${index+1}位`:`Priority ${index+1}`} · {account.account_name||(ja?`アカウント ${account.account_id}`:`Account ${account.account_id}`)}</strong><small>{status(account)}</small></div>
     <div className="provider-account-actions">
      <button type="button" disabled={busy||index===0} aria-label={ja?`${label}の${index+1}位を上へ`:`Move ${label} priority ${index+1} up`} onClick={()=>void move(index,-1)}>↑</button>
      <button type="button" disabled={busy||index===2} aria-label={ja?`${label}の${index+1}位を下へ`:`Move ${label} priority ${index+1} down`} onClick={()=>void move(index,1)}>↓</button>
      <button type="button" disabled={busy} aria-label={ja?`${label}の${index+1}位を設定`:`Configure ${label} priority ${index+1}`} onClick={()=>void configure(account)}>{buttonLabel(account)}</button>
     </div>
    </li>)}
   </ol>
   {count<3&&<button type="button" disabled={busy} onClick={()=>void configure(accounts.find(account=>!account.configured)!)}>{ja?'＋ アカウントを追加':'＋ Add account'}</button>}
  </details>
  {editor}
  {error&&<p role="alert">{error}</p>}
 </div>;
}
