import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import type {CodingTask} from './CodingTaskProgress';
import {DiffView} from './DiffView';
import {GitWorkspacePanel} from './GitWorkspacePanel';
import './coding-review.css';

type Change={path:string;status:string;diff?:string;source_diff?:string;source_reviewable?:boolean;inherited?:boolean;after_hash?:string|null;phase_ids?:string[];binary?:boolean;reviewable?:boolean;revertible?:boolean;truncated?:boolean;error?:string;externally_modified?:boolean;sensitive?:boolean};
type Workspace={mode:'direct'|'worktree';root:string;source_root:string;branch:string;removed?:boolean;source_dirty_files?:number;snapshot_complete?:boolean};
type Steering={id:string;message:string;status:string;received_revision:number;applied_revision:number|null};
type ProviderReference={provider_id:string;category:string;title:string;url:string;host:string;officiality:'VERIFIED'|'CANDIDATE';accessed_at:number;applicability:string;summary:string;phase_id:string|null;plan_revision:number|null};
type ProviderResearch={status:string;providers?:string[];blockers?:string[]};
type Review={available:boolean;revision:string;files:Change[];workspace:Workspace|null;steering:Steering[];reports:{id:string;phase_id:string;structured_report:Record<string,unknown>}[];references?:ProviderReference[];provider_research?:ProviderResearch|null};
const active=new Set(['QUEUED','PLANNING','RUNNING','FINAL_REPORTING','QWEN_REPLANNING']);

function providerReferences(rows:ProviderReference[]|undefined,currentRevision:number|undefined){
  const unique=new Map<string,ProviderReference>();
  for(const row of rows||[]){
    if(!row||typeof row.provider_id!=='string'||typeof row.url!=='string')continue;
    const key=`${row.provider_id}\u0000${row.url}`;const previous=unique.get(key);
    const rank=(value:ProviderReference)=>[currentRevision!=null&&value.plan_revision===currentRevision?1:0,value.officiality==='VERIFIED'?1:0,Number(value.accessed_at)||0];
    const nextRank=rank(row),previousRank=previous?rank(previous):[-1,-1,-1];
    if(!previous||nextRank[0]>previousRank[0]||(nextRank[0]===previousRank[0]&&(nextRank[1]>previousRank[1]||(nextRank[1]===previousRank[1]&&nextRank[2]>previousRank[2]))))unique.set(key,row);
  }
  return [...unique.values()].sort((a,b)=>a.provider_id.localeCompare(b.provider_id)||a.category.localeCompare(b.category)||a.url.localeCompare(b.url));
}

function safeProviderUrl(value:string){
  try{const parsed=new URL(value);if(parsed.protocol==='https:'&&!parsed.username&&!parsed.password&&(!parsed.port||parsed.port==='443'))return parsed.href}catch{}
  return undefined;
}

function providerHost(reference:ProviderReference){
  const safeUrl=safeProviderUrl(reference.url);
  try{return safeUrl?new URL(safeUrl).hostname:reference.host||''}catch{return reference.host||''}
}

function providerRetrievedAt(value:number){
  if(!Number.isFinite(value))return 'unknown';
  try{return new Date(value*1000).toISOString().replace('T',' ').replace(/\.\d{3}Z$/,' UTC')}catch{return 'unknown'}
}

function providerStatus(value:string,ja:boolean){
  const status=value.toUpperCase();
  const labels:Record<string,[string,string]>={
    COMPLETE:['確認済み資料を取得','Current references retrieved'],
    PARTIAL:['一部未確認','Partial · some sources unavailable'],
    UNAVAILABLE:['資料を取得できませんでした','Unavailable · no current source retrieved'],
    DISABLED:['調査は無効','Disabled · research was not run'],
  };
  return labels[status]?.[ja?0:1]||status|| (ja?'状態不明':'Unknown status');
}

function providerCategory(value:string,ja:boolean){
  const labels:Record<string,[string,string]>={technical_spec:['技術仕様','Technical specification'],api:['API資料','API documentation'],terms:['利用規約','Terms'],privacy:['プライバシー','Privacy'],review:['審査資料','Review guidelines'],publishing:['公開・配布','Publishing'],signing:['署名・公証','Signing and notarization']};
  return labels[value]?.[ja?0:1]||value;
}

function reviewError(reason:unknown,ja:boolean){
  const raw=reason instanceof Error?reason.message:String(reason);
  const messages:Record<string,[string,string]>={
    REVIEW_CHANGED_REFRESH_REQUIRED:['差分が更新されています。更新してから操作を確認してください。','Changes have updated. Refresh and review the operation again.'],
    SOURCE_CHANGED_CONFLICT:['元のWorkspaceに競合する変更があります。ファイルの内容を確認してください。','The source workspace contains conflicting changes. Review the affected file.'],
    WAIT_FOR_TASK_TO_PAUSE:['Taskが停止するまでお待ちください。','Wait for the task to stop before changing files.'],
    FILE_CANNOT_BE_REVERTED:['このファイルは自動で戻せません。開始前の記録と、その後の編集を確認してください。','This file cannot be restored automatically. Check its baseline and subsequent edits.'],
    PLAN_CHANGED_REFRESH_REQUIRED:['計画が更新されています。最新の計画を確認してください。','The plan has changed. Review its latest revision.'],
  };
  const key=Object.keys(messages).find(key=>raw.includes(key));
  return key?messages[key][ja?0:1]:raw;
}

function verificationText(report:Record<string,unknown>){
  const lines:string[]=[];
  for(const key of ['test_executed','test_pass','test_fail','errors','blockers']){
    const value=report[key];if(Array.isArray(value))for(const text of value)lines.push(String(text));
  }
  if(typeof report.build_pass==='string')lines.push(`Build: ${report.build_pass}`);
  const typed=report.typed_execution_summary as {operations?:{tool?:string;output?:{output?:string;stdout_summary?:string;stderr_summary?:string;returncode?:number}}[]}|undefined;
  for(const operation of typed?.operations||[]){
    const output=operation.output;
    if(output?.output||output?.stdout_summary||output?.stderr_summary)lines.push([operation.tool,output.output||output.stdout_summary,output.stderr_summary].filter(Boolean).join('\n'));
  }
  return lines.join('\n\n').slice(0,20000)||String(report.status||'UNVERIFIED');
}

export function CodingTaskReview({task,conversationId,ja}:{task:CodingTask;conversationId?:string|null;ja:boolean}) {
  const [review,setReview]=useState<Review|null>(null),[path,setPath]=useState(''),[selection,setSelection]=useState<string[]>([]);
  const [error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[version,setVersion]=useState(0);
  const [confirm,setConfirm]=useState<{action:'revert'|'apply';revision:string;path:string;paths:string[];files?:Change[]}|null>(null);
  const identity=useRef(task.id);identity.current=task.id;
  const base=`/coding-tasks/${task.id}`;
  useEffect(()=>{setReview(null);setPath('');setSelection([]);setError('');setNotice('');setConfirm(null);setBusy(false)},[task.id]);
  useEffect(()=>{const refresh=()=>setVersion(value=>value+1);window.addEventListener('olcr-command-complete',refresh);return()=>window.removeEventListener('olcr-command-complete',refresh)},[]);
  useEffect(()=>{
    let live=true,polling=false;
    const refresh=async()=>{if(polling)return;polling=true;try{const value=await api<Review>(`${base}/review`);if(live&&Array.isArray(value?.files)){setReview(value);setPath(current=>value.files.some(file=>file.path===current)?current:value.files[0]?.path||'');setSelection(current=>current.filter(path=>value.files.some(file=>file.path===path)));setError('')}}catch(reason){if(live)setError(reviewError(reason,ja))}finally{polling=false}};
    void refresh();const timer=active.has(task.status)?window.setInterval(()=>void refresh(),3000):undefined;
    return()=>{live=false;if(timer)window.clearInterval(timer)};
  },[base,task.status,task.plan_revision,version]);
  const mutation=async(endpoint:string,body?:unknown)=>{
    if(busy)return;const requestTask=task.id;setBusy(true);setError('');setNotice('');
    try{await api(`${base}${endpoint}`,{method:'POST',body:JSON.stringify(body||{})});if(identity.current!==requestTask)return false;setVersion(value=>value+1);setConfirm(null);window.dispatchEvent(new Event('olcr-command-complete'));return true}
    catch(reason){if(identity.current===requestTask)setError(reviewError(reason,ja));return false}finally{if(identity.current===requestTask)setBusy(false)};
  };
  const selected=review?.files.find(file=>file.path===path),running=active.has(task.status);
  const label=(status:string)=>({ADDED:ja?'追加':'Added',MODIFIED:ja?'変更':'Modified',DELETED:ja?'削除':'Deleted',UNSUPPORTED:ja?'表示不可':'Unsupported',WAITING:ja?'完了後に進めます':'Waiting until completion',QUEUED:ja?'次の工程で受け取ります':'Queued for next boundary',APPLYING:ja?'計画に反映中':'Applying',APPLIED:ja?'反映済み':'Applied',CANCELLED:ja?'取り消し済み':'Cancelled',NEEDS_REVIEW:ja?'反映を確認してください':'Needs review'}[status]||status);
  const references=providerReferences(review?.references,task.plan_revision),research=review?.provider_research;
  return <section className="coding-review" aria-label={ja?'変更と追加指示':'Changes and instructions'}>
    {(references.length>0||research)&&<details className="provider-research" open>
      <summary>{ja?'プロバイダー資料':'Provider references'} · {references.length}{research?.status&&<span className={`research-status ${research.status.toLowerCase()}`} data-status={research.status}>{providerStatus(research.status,ja)}</span>}</summary>
      {research?.blockers?.length?<ul className="provider-research-blockers">{research.blockers.map((blocker,index)=><li key={`${index}-${blocker}`}>{blocker}</li>)}</ul>:null}
      {references.length>0?<ul className="provider-reference-list">{references.map(reference=>{
        const candidate=reference.officiality!=='VERIFIED';const stale=Number.isFinite(task.plan_revision)&&reference.plan_revision!=null&&reference.plan_revision!==task.plan_revision;const host=providerHost(reference);const safeUrl=safeProviderUrl(reference.url);
        return <li className={`provider-reference ${candidate?'candidate':'verified'} ${stale?'stale':''}`} data-officiality={candidate?'CANDIDATE':'VERIFIED'} key={`${reference.provider_id}:${reference.url}`}>
          <div className="provider-reference-heading"><strong>{reference.provider_id}</strong><span className={`provider-officiality ${candidate?'candidate':'verified'}`}>{candidate?(ja?'候補・公式性未確認':'Candidate · not verified'):(ja?'公式ドメインを確認':'Official domain verified')}</span>{stale&&<span className="provider-staleness">{ja?'以前の計画で取得':'Retrieved for an earlier plan'}</span>}</div>
          <dl><div><dt>{ja?'ホスト':'Host'}</dt><dd>{host||'unknown'}</dd></div>
            <div><dt>{ja?'資料種別':'Document'}</dt><dd>{providerCategory(reference.category,ja)}</dd></div>
            <div><dt>{ja?'取得日時 (UTC)':'Retrieved (UTC)'}</dt><dd>{providerRetrievedAt(reference.accessed_at)}</dd></div>
            <div><dt>{ja?'適用対象':'Applies to'}</dt><dd>{reference.applicability||'—'}</dd></div></dl>
          {reference.summary&&<p className="provider-reference-summary">{reference.summary}</p>}
          {safeUrl?<a href={safeUrl} target="_blank" rel="noopener noreferrer">{safeUrl}</a>:<span>{reference.url}</span>}
        </li>;
      })}</ul>:research?.status&&<p className="provider-research-empty">{research.status==='UNAVAILABLE'?(ja?'現行の資料を取得できませんでした。':'No current provider source was retrieved.'):research.status==='PARTIAL'?(ja?'出典を取得できていない項目があります。':'Some requested sources were not retrieved.'):research.status==='DISABLED'?(ja?'このTaskでは外部資料の調査を行っていません。':'Provider research was not run for this task.'):null}</p>}
    </details>}
    {review?.workspace&&<div className="task-workspace-summary"><strong>{review.workspace.mode==='worktree'?(ja?'独立した作業場所':'Isolated worktree'):(ja?'現在のWorkspace':'Current workspace')}</strong><small>{review.workspace.branch||review.workspace.root}</small>{Boolean(review.workspace.source_dirty_files)&&<p>{ja?'元のWorkspaceの未コミット変更は、この作業場所には含まれません。':'Uncommitted changes in the source workspace are not included here.'}</p>}
      {review.workspace.mode==='worktree'&&!running&&<button type="button" disabled={busy} onClick={()=>void mutation(review.workspace!.removed?'/worktree/restore':'/worktree/remove')}>{review.workspace.removed?(ja?'作業場所を復元':'Restore worktree'):(ja?'コミット済みの作業場所を片付ける':'Remove clean worktree')}</button>}
    </div>}

    <details className="task-changes" open><summary>{ja?'変更ファイル':'Changed files'} · {review?.files.length||0}<button type="button" disabled={busy} onClick={event=>{event.preventDefault();setVersion(value=>value+1)}}>{ja?'更新':'Refresh'}</button></summary>
      {!review?.available&&<p className="review-empty">{ja?'このTaskの開始前の状態はまだ記録されていません。':'The starting state for this task has not been recorded.'}</p>}
      {review?.available&&!review.files.length&&<p className="review-empty">{ja?'記録された変更はありません。':'No recorded changes.'}</p>}
      {review?.files.length? <div className="task-change-layout"><div className="task-file-list" role="group" aria-label={ja?'ファイル':'Files'}>{review.files.map(file=><div className="task-file" key={file.path}>
        {review.workspace?.mode==='worktree'&&<input type="checkbox" aria-label={`${ja?'取り込む':'Apply'} ${file.path}`} disabled={running||busy||file.sensitive||!(file.source_reviewable??file.reviewable)} checked={selection.includes(file.path)} onChange={event=>setSelection(previous=>event.target.checked?[...previous,file.path]:previous.filter(p=>p!==file.path))}/>}
        <button type="button" aria-pressed={file.path===path} onClick={()=>{setPath(file.path);setConfirm(null)}}><span className={`change-state ${file.status.toLowerCase()}`}>{label(file.status)}</span><span>{file.path}{file.inherited&&<small> · {ja?'前の作業':'Previous work'}</small>}</span></button>
      </div>)}</div><div className="task-file-review">{selected&&<><header><strong>{selected.path}</strong><span>{selected.phase_ids?.join(' · ')}</span></header>
        {selected.diff&&!selected.sensitive?<DiffView diff={selected.diff} label={`${selected.path} diff`}/>:<p className="review-empty">{selected.error||(selected.sensitive?(ja?'認証ファイルの内容はスナップショットや差分に保存しません。':'Credential file contents are not stored in snapshots or diffs.'):selected.binary?(ja?'バイナリファイルの変更です。':'Binary file changed.'):selected.reviewable===false?(ja?'ファイルが大きいため差分表示・復元の対象外です。':'File exceeds the review and restore limit.'):(ja?'内容は同じでファイル属性が変更されています。':'File attributes changed.'))}</p>}
        {selected.externally_modified&&<p role="status">{ja?'Taskの実行後にこのファイルが編集されています。内容の復元は行えません。':'This file was edited after task execution and cannot be restored automatically.'}</p>}
        {selected.truncated&&<p>{ja?'差分の一部を表示しています。':'Showing a partial diff.'}</p>}
        <div className="review-actions"><button type="button" onClick={()=>window.dispatchEvent(new CustomEvent('olcr-compose-coding-instruction',{detail:{taskId:task.id,text:`${selected.path}: `}}))}>{ja?'このファイルについて指示':'Add instruction for this file'}</button><button type="button" disabled={busy||!selected.revertible} onClick={()=>setConfirm({action:'revert',revision:review!.revision,path,paths:[]})}>{ja?'開始前に戻す':'Restore starting state'}</button></div>
      </>}</div></div>:null}
      {review?.workspace?.mode==='worktree'&&selection.length>0&&<button type="button" className="primary" disabled={busy||running} onClick={()=>setConfirm({action:'apply',revision:review!.revision,path:'',paths:[...selection],files:review.files.filter(file=>selection.includes(file.path))})}>{ja?`${selection.length}ファイルを元のWorkspaceへ取り込む`:`Apply ${selection.length} files to source workspace`}</button>}
      {confirm&&<div className="review-confirm" role="group" aria-label={ja?'変更内容の確認':'Confirm changes'}><p>{confirm.action==='revert'?(ja?`${confirm.path}をTask開始前の内容に戻します。`:`Restore ${confirm.path} to its starting content.`):(ja?`${confirm.paths.join(', ')}を ${review?.workspace?.source_root} に反映します。前のTaskからの変更を含む累積差分を確認してください。元のファイルに新しい変更があれば中止します。`:`Apply ${confirm.paths.join(', ')} to ${review?.workspace?.source_root}. Review cumulative changes including previous tasks. Conflicting source edits stop the operation.`)}</p>{confirm.files?.map(file=><details key={file.path} open><summary>{file.path}</summary>{!file.sensitive&&(file.source_diff??file.diff)?<DiffView diff={(file.source_diff??file.diff)!} label={`${file.path} source diff`}/>:<p className="review-empty">{ja?'内容以外の変更、またはバイナリの変更です。':'File attributes or binary content changed.'}</p>}</details>)}<button type="button" disabled={busy} onClick={()=>void mutation(confirm.action==='revert'?'/changes/revert':'/changes/apply',confirm.action==='revert'?{path:confirm.path,revision:confirm.revision}:{paths:confirm.paths,revision:confirm.revision})}>{ja?'確認して実行':'Confirm and execute'}</button><button type="button" disabled={busy} onClick={()=>setConfirm(null)}>{ja?'キャンセル':'Cancel'}</button></div>}
    </details>
    {(review?.reports?.length||0)>0&&<details className="task-verification"><summary>{ja?'検証・実行結果':'Verification and execution results'}</summary>{review?.reports?.map(report=><details key={report.id}><summary>{report.phase_id} · {String(report.structured_report.status||'UNVERIFIED')}</summary><pre>{verificationText(report.structured_report)}</pre></details>)}</details>}

    {(review?.steering?.length||0)>0&&<details className="task-instruction-history"><summary>{ja?'追加指示の履歴':'Instruction history'}</summary><ol>{review?.steering?.map(item=><li key={item.id}><p>{item.message}</p><small>{label(item.status)}{item.applied_revision!=null?` · ${ja?'計画':'Plan'} ${item.applied_revision}`:''}</small></li>)}</ol></details>}
    {busy&&<p role="status">{ja?'処理中…':'Working…'}</p>}{notice&&<p role="status">{notice}</p>}{error&&<p role="alert">{error}</p>}
    {conversationId&&!review?.workspace?.removed&&<GitWorkspacePanel conversationId={conversationId} taskId={task.id} ja={ja} suggestedTitle={task.original_goal?.split("\n")[0].slice(0,240)} suggestedBody={task.final_report?.text}/>}
  </section>;
}
