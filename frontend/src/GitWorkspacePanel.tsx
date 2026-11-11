import {useEffect, useRef, useState} from 'react';
import {api} from './api';
import {openExternal} from './native';
import './git-workspace.css';
import {DiffView} from './DiffView';

type Operation = {id:string; action:'stage'|'commit'|'push'|'branch'|'switch_branch'|'pull_request'; status:string; workspace:string; branch:string; head:string;
  task_id?:string;title?:string;body?:string;base?:string;draft?:boolean;pr_url?:string;target_branch?:string;target_head?:string;switched_branch?:string;new_branch?:string;created_branch?:string;message?:string; paths?:string[]; diff?:string; remote?:string; remote_url?:string; destination_branch?:string;
  commit?:string; pushed_commit?:string; error?:string; push_preparation_error?:string; output?:string; commits?:string[]; commit_scope?:string};
type Status = {branches?:string[];workspace:string; branch:string; head:string; files:Array<{path:string;status:string}>;
  remotes:Array<{name:string;url:string;github:boolean}>; credential_helper_configured:boolean; setup_command:string; setup_url:string};

export function GitWorkspacePanel({conversationId, taskId, ja, suggestedTitle, suggestedBody}:{conversationId?:string|null;taskId?:string;ja:boolean;suggestedTitle?:string;suggestedBody?:string}) {
  const [open,setOpen]=useState(false),[operations,setOperations]=useState<Operation[]>([]);
  const [status,setStatus]=useState<Status|null>(null),[selected,setSelected]=useState<string[]>([]);
  const [message,setMessage]=useState(''),[remote,setRemote]=useState('origin'),[branch,setBranch]=useState('');
  const [details,setDetails]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[available,setAvailable]=useState(false);
  const latestPending=useRef(''),generation=useRef(0),busyRef=useRef(false),reviewRevision=useRef(0);
  const base=`/conversations/${conversationId}/git`;
  const url=(path:string)=>`${base}${path}${taskId?`?task_id=${encodeURIComponent(taskId)}`:''}`;
  type PullRequest={number:number;title:string;url:string;state:string;isDraft:boolean;headRefName:string;baseRefName:string;body?:string;diff?:string;mergeable?:string;reviewDecision?:string;comments?:{id:string;body:string;author?:{login:string}}[];reviews?:{id:string;body:string;state:string;author?:{login:string}}[];statusCheckRollup?:{name?:string;status?:string;conclusion?:string;state?:string}[]};
  const [switchBranch,setSwitchBranch]=useState(''),[newBranch,setNewBranch]=useState(''),[prTitle,setPrTitle]=useState(''),[prBody,setPrBody]=useState(''),[prBase,setPrBase]=useState('main'),[draft,setDraft]=useState(true),[prs,setPrs]=useState<PullRequest[]>([]),[pr,setPr]=useState<PullRequest|null>(null);
  useEffect(()=>{
    const current=++generation.current;
    setOpen(false);setStatus(null);setOperations([]);setSelected([]);setPrs([]);setPr(null);setPrTitle(suggestedTitle||'');setPrBody('');setNewBranch('');setPrBase('main');setDraft(true);setDetails('');setError('');setBusy(false);setAvailable(false);latestPending.current='';busyRef.current=false;
    if(!conversationId)return;
    let live=true,polling=false;
    const poll=async()=>{
      if(polling||busyRef.current)return;
      polling=true;const revision=reviewRevision.current;
      try {
        const value=await api<{available:boolean;operations:Operation[]}>(url("/operations"));
        if(!live||generation.current!==current||reviewRevision.current!==revision)return;
        if(!Array.isArray(value.operations))throw Error('GIT_OPERATIONS_RESPONSE_INVALID');
        setAvailable(value.available);setOperations(value.operations);
        const pending=value.operations.filter(op=>op.status==='PENDING').map(op=>op.id).join(',');
        if(pending&&pending!==latestPending.current)setOpen(true);
        latestPending.current=pending;
      } catch(e) {if(live&&generation.current===current)setError(e instanceof Error?e.message:String(e));}
      finally {polling=false;}
    };
    void poll();const timer=window.setInterval(()=>void poll(),5000);
    return()=>{live=false;window.clearInterval(timer)};
  },[conversationId,taskId]);
  if(!conversationId)return null;
  const run=async(work:()=>Promise<void>)=>{
    if(busyRef.current)return;
    busyRef.current=true;reviewRevision.current++;
    const current=generation.current;setBusy(true);setError('');
    try {await work();} catch(e){if(generation.current===current)setError(e instanceof Error?e.message:String(e));}
    finally {if(generation.current===current){setBusy(false);busyRef.current=false;}}
  };
  const refresh=async()=>{
    const current=generation.current;
    const value=await api<Status>(url("/status"));
    if(generation.current!==current)return;
    setStatus(value);setSelected([]);setBranch(value.branch);setSwitchBranch(value.branch);
    setRemote(value.remotes.find(item=>item.github&&item.name==='origin')?.name||value.remotes.find(item=>item.github)?.name||'origin');
  };
  const prepare=async(action:'stage'|'commit'|'push')=>{
    const current=generation.current;
    const value=await api<Operation>(`${base}/prepare`,{method:'POST',body:JSON.stringify(action!=='push'?{action,paths:selected,message,task_id:taskId}:{action,remote,branch,task_id:taskId})});
    if(generation.current!==current)return;
    setOperations(previous=>[value,...previous]);
  };
  const decide=async(op:Operation,decision:'approve'|'reject')=>{
    const current=generation.current;
    await api(`${base}/operations/${op.id}/${decision}`,{method:'POST'});
    const value=await api<{operations:Operation[]}>(url("/operations"));
    if(generation.current!==current)return;
    setOperations(value.operations);if(status)await refresh();window.dispatchEvent(new Event("olcr-command-complete"));
  };
  const prepareMore=async(payload:Record<string,unknown>)=>{
    const current=generation.current;
    const value=await api<Operation>(`${base}/prepare`,{method:'POST',body:JSON.stringify({...payload,task_id:taskId})});
    if(generation.current===current)setOperations(previous=>[value,...previous]);
  };
  const githubUrl=(path:string)=>`${url(path)}${taskId?'&':'?'}remote=${encodeURIComponent(remote)}`;
  const loadPullRequests=async()=>{const current=generation.current;const value=await api<{pull_requests:PullRequest[]}>(githubUrl('/pull-requests'));if(generation.current===current)setPrs(value.pull_requests)};
  const loadPullRequest=async(number:number)=>{const current=generation.current;const value=await api<PullRequest>(githubUrl(`/pull-requests/${number}`));if(generation.current===current)setPr(value)};
  return <section className="git-workspace" aria-label="Git / GitHub">
    <button type="button" aria-expanded={open} onClick={()=>{setOpen(value=>!value);if(!open&&available&&!status)void run(refresh)}}>
      Git / GitHub {operations.some(op=>op.status==='PENDING')?(ja?'· 確認待ち':'· Review pending'):''} {open?'▴':'▾'}
    </button>
    {open&&<div className="git-workspace-content">
      {busy&&<p role="status">{ja?'Git操作を処理中…':'Processing Git operation…'}</p>}
      {!available&&<p>{ja?'GitリポジトリのルートをプロジェクトのWorkspaceに設定してください。':'Set the repository root as your project Workspace.'}</p>}
      {available&&<button type="button" disabled={busy} onClick={()=>void run(refresh)}>{ja?'状態を更新':'Refresh status'}</button>}
      {status&&<>
        <p>{status.workspace}<br/>{ja?'ブランチ':'Branch'}: {status.branch||'detached HEAD'} · {status.head.slice(0,12)||'No commits'}</p>
        <details><summary>{ja?'GitHub接続・認証':'GitHub connection and authentication'}</summary>
          <p>{ja?'既存のGit認証設定とSSHを使用します。接続の成否はプッシュ時に確認します。':'Uses your Git credential setup and SSH. Push verifies access.'}</p>
          <pre>{status.setup_command}</pre>
          <button type="button" onClick={()=>void openExternal(status.setup_url).catch(e=>setError(String(e)))}>{ja?'GitHub CLIの接続手順':'GitHub CLI login instructions'}</button>
        </details>
        <div className="git-files" role="group" aria-label={ja?'コミット対象ファイル':'Files to commit'}>
          {status.files.length===0&&<p>{ja?'未コミットの変更はありません。':'No uncommitted changes.'}</p>}
          {status.files.map(file=><label key={file.path}><input type="checkbox" disabled={busy} checked={selected.includes(file.path)}
            onChange={event=>setSelected(previous=>event.target.checked?[...previous,file.path]:previous.filter(path=>path!==file.path))}/><code>{file.status}</code> {file.path}</label>)}
        </div>
        <label>{ja?'コミットメッセージ':'Commit message'}<input value={message} onChange={event=>setMessage(event.target.value)} disabled={busy} maxLength={5000}/></label>
        <button type="button" disabled={busy||!selected.length} onClick={()=>void run(()=>prepare('stage'))}>{ja?'ステージ差分を確認':'Review staging'}</button>
        <button type="button" disabled={busy||!selected.length||!message.trim()} onClick={()=>void run(()=>prepare('commit'))}>{ja?'コミット差分を確認':'Review commit'}</button>
        <label>{ja?'送信先リモート':'Remote'}<select value={remote} disabled={busy} onChange={event=>setRemote(event.target.value)}>
          {status.remotes.filter(item=>item.github).map(item=><option key={item.name} value={item.name}>{item.name} · {item.url}</option>)}
        </select></label>
        <label>{ja?'送信先ブランチ':'Destination branch'}<input value={branch} disabled={busy} onChange={event=>setBranch(event.target.value)}/></label>
        <button type="button" disabled={busy||!status.head||!status.remotes.some(item=>item.name===remote&&item.github)||!branch} onClick={()=>void run(()=>prepare('push'))}>{ja?'プッシュ内容を確認':'Review push'}</button>
        <button type="button" disabled={busy} onClick={()=>void run(async()=>{const current=generation.current;const value=await api<{working_diff:string;staged_diff:string;untracked_files:string[]}>(url("/diff"));if(generation.current===current)setDetails(value.working_diff+'\n'+value.staged_diff+'\nUntracked: '+value.untracked_files.join(', '))})}>{ja?'全体の差分':'All diffs'}</button>
        <button type="button" disabled={busy} onClick={()=>void run(async()=>{const current=generation.current;const value=await api<{log:string}>(url("/log"));if(generation.current===current)setDetails(value.log)})}>{ja?'コミット履歴':'Commit history'}</button>
        <details className="github-pr-fields"><summary>{ja?'ブランチとPull request':'Branches and pull requests'}</summary>
          {Boolean(status.branches?.length)&&<><label>{ja?'作業するブランチ':'Working branch'}<select value={switchBranch} disabled={busy} onChange={event=>setSwitchBranch(event.target.value)}>{status.branches!.map(name=><option key={name} value={name}>{name}</option>)}</select></label><button type="button" disabled={busy||!switchBranch||switchBranch===status.branch} onClick={()=>void run(()=>prepareMore({action:'switch_branch',branch:switchBranch}))}>{ja?'ブランチ切替を確認':'Review branch switch'}</button></>}
          <label>{ja?'新しいブランチ':'New branch'}<input value={newBranch} onChange={event=>setNewBranch(event.target.value)} placeholder="olcr/my-feature" disabled={busy}/></label>
          <button type="button" disabled={busy||!newBranch.trim()} onClick={()=>void run(()=>prepareMore({action:'branch',branch:newBranch}))}>{ja?'ブランチ作成を確認':'Review branch creation'}</button>
          {suggestedBody&&<button type="button" disabled={busy} onClick={()=>{if(!prTitle)setPrTitle(suggestedTitle||'');setPrBody(suggestedBody)}}>{ja?'Taskの結果をPR説明に挿入':'Use task results in PR description'}</button>}
          <label>{ja?'PRタイトル':'PR title'}<input value={prTitle} onChange={event=>setPrTitle(event.target.value)} disabled={busy} maxLength={240}/></label>
          <label>{ja?'PRの説明':'PR description'}<textarea value={prBody} onChange={event=>setPrBody(event.target.value)} disabled={busy} maxLength={60000}/></label>
          <label>{ja?'取り込み先ブランチ':'Base branch'}<input value={prBase} onChange={event=>setPrBase(event.target.value)} disabled={busy}/></label>
          <label><input type="checkbox" checked={draft} onChange={event=>setDraft(event.target.checked)} disabled={busy}/>Draft</label>
          <button type="button" disabled={busy||!prTitle.trim()||!prBase.trim()} onClick={()=>void run(()=>prepareMore({action:'pull_request',remote,base:prBase,title:prTitle,body:prBody,draft}))}>{ja?'PR作成内容を確認':'Review pull request creation'}</button>
          <button type="button" disabled={busy} onClick={()=>void run(loadPullRequests)}>{ja?'GitHubのPRを取得':'Load GitHub pull requests'}</button>
          <div className="pr-list">{prs.map(item=><button type="button" disabled={busy} key={item.number} onClick={()=>void run(()=>loadPullRequest(item.number))}>#{item.number} · {item.title} · {item.isDraft?'Draft':item.state}</button>)}</div>
          {pr&&<article className="pr-review"><header><strong>#{pr.number} {pr.title}</strong><button type="button" onClick={()=>void openExternal(pr.url)}>GitHub ↗</button></header><p>{pr.headRefName} → {pr.baseRefName} · {pr.state} · {pr.reviewDecision||pr.mergeable}</p><p>{pr.body}</p><ul>{pr.statusCheckRollup?.map((check,index)=><li key={index}>{check.name||'Check'} · {check.conclusion||check.state||check.status}</li>)}</ul>{pr.diff&&<DiffView diff={pr.diff} label="Pull request diff"/>}<div className="pr-comments">{[...(pr.comments||[]),...(pr.reviews||[])].map((comment,index)=><article key={comment.id||index}><strong>{comment.author?.login||'GitHub'}</strong><p>{comment.body}</p></article>)}</div></article>}
        </details>
        {details&&<DiffView diff={details} label={ja?"Git差分":"Git diff"}/>}
      </>}
      {operations.map(op=><article className="git-operation" key={op.id}>
        <strong>{op.action==='stage'?(ja?'ステージ':'Stage'):op.action==='commit'?(ja?'コミット':'Commit'):op.action==='branch'?(ja?'ブランチ作成':'Create branch'):op.action==='switch_branch'?(ja?'ブランチ切替':'Switch branch'):op.action==='pull_request'?'Pull request':(ja?'プッシュ':'Push')} · {op.status}</strong>
        <p>{op.workspace} · {op.branch}<br/>{op.message||op.title||op.target_branch||op.new_branch||`${op.head.slice(0,12)} → ${op.remote_url} / ${op.destination_branch}`}
          {op.action==='pull_request'&&<><br/>{op.destination_branch} → {op.base} · {op.draft?'Draft':'Ready for review'}<br/>{op.body}</>}</p>
        {op.paths&&<ul>{op.paths.map(path=><li key={path}>{path}</li>)}</ul>}
        {op.diff&&<details><summary>{ja?'コミット対象の全文差分':'Full commit diff'}</summary><DiffView diff={op.diff} label={ja?"承認対象の差分":"Reviewed diff"}/></details>}
        {op.commits&&<pre className="git-diff">{op.commits.join('\n')}</pre>}
        {op.commit_scope==='ALL_LOCAL_HISTORY_REMOTE_OBJECT_NOT_PRESENT'&&<p>{ja?'リモート側の先頭コミットがローカルにないため、確認用にローカルの全履歴を表示しています。':'The remote tip is not present locally; the review shows the complete local history.'}</p>}
        {op.status==='PENDING'&&<><p>{ja?'確認した内容を実行します。Gitフックはこの操作では実行しません。':'Executes the reviewed operation. Git hooks are disabled for this operation.'}</p>
          <button type="button" disabled={busy} onClick={()=>void run(()=>decide(op,'approve'))}>{ja?'承認して実行':'Approve and execute'}</button>
          <button type="button" disabled={busy} onClick={()=>void run(()=>decide(op,'reject'))}>{ja?'取り消す':'Reject'}</button></>}
        {op.pr_url&&<button type="button" onClick={()=>void openExternal(op.pr_url!)}>{ja?"GitHubでPRを開く":"Open pull request on GitHub"}</button>}
        {op.switched_branch&&<p>{op.switched_branch}</p>}
        {op.created_branch&&<p>{op.created_branch}</p>}
        {(op.commit||op.pushed_commit)&&<code>{op.commit||op.pushed_commit}</code>}
        {(op.error||op.push_preparation_error)&&<p role="alert">{op.error||op.push_preparation_error}</p>}
      </article>)}
      {error&&<p role="alert">{error}</p>}
    </div>}
  </section>;
}
