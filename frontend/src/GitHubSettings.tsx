import {useEffect,useId,useRef,useState} from 'react';
import {SettingHelp} from './SettingHelp';
import {openExternal} from './native';
import {useLanguage} from './language';

type Connection={installed:boolean;connected:boolean;account?:string;execution_mode:'review'|'autonomous'};
type Attempt={id:string;status:string;code:string;url:string};
export function GitHubSettings({api}:{api:(path:string,init?:RequestInit)=>Promise<any>}){
  const {language}=useLanguage(),ja=language==='ja';
  const id=useId();
  const [connection,setConnection]=useState<Connection|null>(null),[attempt,setAttempt]=useState<Attempt|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const opened=useRef('');
  const load=async()=>setConnection(await api('/github/connection'));
  useEffect(()=>{let live=true;void api('/github/connection').then(value=>{if(live)setConnection(value)}).catch(e=>{if(live)setError(String(e.message||e))});return()=>{live=false}},[api]);
  useEffect(()=>{
    if(!attempt||attempt.status!=='pending')return;
    let live=true,checking=false;
    const poll=async()=>{if(checking)return;checking=true;try{
      const next:Attempt=await api(`/github/connect/${encodeURIComponent(attempt.id)}`);if(!live)return;
      if(next.status==='connected'){setAttempt(next);await load();window.dispatchEvent(new Event('olcr-command-complete'));return;}
      if(next.status!=='pending'){setAttempt(next);setError(ja?'GitHubへの接続が完了しませんでした。もう一度お試しください。':'GitHub sign-in did not complete. Please try again.');return;}
      setAttempt(next);
      if(next.code&&opened.current!==next.id){opened.current=next.id;await openExternal(next.url);}
    }catch(e:any){if(live)setError(e.message||String(e))}finally{checking=false}};
    void poll();const timer=window.setInterval(()=>void poll(),1500);return()=>{live=false;window.clearInterval(timer)};
  },[attempt?.id,attempt?.status,api,ja]);
  const start=async()=>{setBusy(true);setError('');try{const value=await api('/github/connect',{method:'POST'});setAttempt(value)}catch(e:any){setError(e.message||String(e))}finally{setBusy(false)}};
  const pending=attempt?.status==='pending';
  return <section className="panel github-settings" aria-label={ja?'GitHub連携':'GitHub connection'}>
    <h2>{ja?'GitHub連携':'GitHub connection'}</h2>
    <p role="status">{connection?.connected?`${ja?'接続済み':'Connected'} · @${connection.account}`:connection?(ja?'未接続':'Not connected'):(ja?'接続状態を確認中…':'Checking connection…')}</p>
    <p>{ja?'ブラウザでGitHubにログインすると、プロジェクトの変更をステージ・コミット・プッシュできます。':'Sign in through your browser to stage, commit and push changes in your projects.'}</p>
    {connection?.installed===false?<button type="button" onClick={()=>void openExternal('https://cli.github.com/')}>{ja?'GitHub CLIをインストール':'Install GitHub CLI'}</button>:<button type="button" className="primary" disabled={busy||pending||!connection} onClick={()=>void start()}>{pending?(ja?'ブラウザでの認証を待っています…':'Waiting for browser sign-in…'):connection?.connected?(ja?'GitHubに接続し直す':'Reconnect GitHub'):(ja?'GitHubに接続':'Connect GitHub')}</button>}
    {pending&&<div className="github-sign-in"><p>{ja?'ブラウザに次のコードを入力して接続を許可してください。完了すると自動で反映します。':'Enter this code in your browser and authorize the connection. This screen updates automatically.'}</p>{attempt.code?<><code>{attempt.code}</code><button type="button" onClick={()=>void navigator.clipboard.writeText(attempt.code)}>{ja?'コードをコピー':'Copy code'}</button><button type="button" onClick={()=>void openExternal(attempt.url)}>{ja?'ブラウザを開く':'Open browser'}</button></>:<p>{ja?'認証コードを準備中…':'Preparing sign-in code…'}</p>}<button type="button" onClick={()=>void api(`/github/connect/${attempt.id}`,{method:'DELETE'}).then(()=>setAttempt(null)).catch(e=>setError(e.message))}>{ja?'キャンセル':'Cancel'}</button></div>}
    {connection?.connected&&<div className="language-setting"><span className="setting-label-with-help"><label htmlFor={`${id}-autonomous`}>{ja?'Git操作を自律実行':'Run Git operations autonomously'}</label><SettingHelp label={ja?'Git操作を自律実行':'Autonomous Git operations'}>{ja?'オンにすると、依頼やコーディング完了に伴うコミット・プッシュを自動で進めます。実行前に毎回確認したい場合はオフにします。':'Allow commits and pushes requested in chat or triggered by completed coding work. Turn off to review each operation first.'}</SettingHelp></span><input id={`${id}-autonomous`} type="checkbox" role="switch" checked={connection.execution_mode==='autonomous'} onChange={event=>{const mode=event.target.checked?'autonomous':'review';void api('/github/policy',{method:'PUT',body:JSON.stringify({execution_mode:mode})}).then(()=>setConnection({...connection,execution_mode:mode})).catch(e=>setError(e.message))}}/></div>}
    {connection?.connected&&<p className="setting-hint">{ja?'オンの場合、口語指示や完了したコーディング作業からGit操作を実行します。「プッシュしない」などの指示にも従います。オフの場合は実行前に内容を確認できます。':'When enabled, Git actions run from your messages and completed coding tasks. Instructions such as “do not push” are respected. Turn it off to review each operation before execution.'}</p>}
    {error&&<p role="alert">{error}</p>}
  </section>;
}
