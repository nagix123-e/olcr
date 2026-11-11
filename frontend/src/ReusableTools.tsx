import {useEffect,useState} from 'react';
import {PageBack} from './InterfacePrimitives';
import {useLanguage} from './language';
import './personal-tools.css';
type Api=(path:string,init?:RequestInit)=>Promise<any>;
export function ReusableTools({api,onBack}:{api:Api;onBack:()=>void}){
 const {language}=useLanguage(),ja=language==='ja';
 const [tools,setTools]=useState<any[]>([]),[description,setDescription]=useState(''),[draft,setDraft]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const refresh=async()=>setTools((await api('/agent-tools')).tools);
 useEffect(()=>{void refresh().catch(e=>setError(e.message));},[api]);
 const act=async(fn:()=>Promise<any>)=>{setBusy(true);setError('');try{await fn();}catch(e:any){setError(e.message||String(e));}finally{setBusy(false);}};
 return <section className="personal-tools"><PageBack onBack={onBack} destination="settings"/><h2>{ja?'再利用できるツール':'Reusable tools'}</h2>
  <p>{ja?'計算やデータ取得など、既存の読み取りツールを組み合わせて専用ツールを作ります。保存すると、カスタムエージェントのツール一覧から選べます。':'Combine existing read-only tools into reusable tools, then select them in Custom agents.'}</p>
  <form onSubmit={e=>{e.preventDefault();void act(async()=>{const value=await api('/agent-tools/draft',{method:'POST',body:JSON.stringify({description})});setDraft(JSON.stringify(value,null,2));});}}>
   <label>{ja?'作りたいツール':'Describe your tool'}<textarea required minLength={3} maxLength={4000} value={description} onChange={e=>setDescription(e.target.value)}/></label>
   <button type="submit" disabled={busy}>{busy?(ja?'処理中…':'Working…'):(ja?'ツール案を作成':'Generate draft')}</button>
  </form>
  {draft&&<form onSubmit={e=>{e.preventDefault();void act(async()=>{await api('/agent-tools',{method:'POST',body:JSON.stringify(JSON.parse(draft))});setDraft('');await refresh();});}}>
   <label>{ja?'ツール定義を確認・編集':'Review and edit tool definition'}<textarea className="tool-definition" required maxLength={100000} value={draft} onChange={e=>setDraft(e.target.value)}/></label>
   <button type="submit" disabled={busy}>{ja?'保存':'Save'}</button>
  </form>}
  {error&&<p role="alert">{error}</p>}
  <ul>{tools.map(tool=><li key={tool.id}><strong>{tool.name}</strong><p>{tool.description}</p><small>{tool.steps.length} {ja?'ステップ':'steps'}</small><button type="button" disabled={busy} onClick={()=>void act(async()=>{await api('/agent-tools/'+encodeURIComponent(tool.id),{method:'DELETE'});await refresh();})}>{ja?'無効にする':'Disable'}</button></li>)}</ul>
 </section>;
}
