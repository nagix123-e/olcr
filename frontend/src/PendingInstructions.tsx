import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import {UiIcon} from './UiIcon';
import type {CodingTask} from './CodingTaskProgress';
import './pending-instructions.css';

type Instruction={id:string;message:string;status:string;received_revision:number};
export function PendingInstructions({task,ja,onEdit}:{task:CodingTask;ja:boolean;onEdit:(item:Instruction)=>void}){
  const [rows,setRows]=useState<Instruction[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[version,setVersion]=useState(0);
  const identity=useRef(task.id);identity.current=task.id;
  const requestVersion=useRef(0),mutating=useRef(false);
  useEffect(()=>{setRows([]);setError('');setNotice('');setBusy(false);mutating.current=false},[task.id]);
  useEffect(()=>{
    let live=true;
    const refresh=async()=>{if(mutating.current)return;const generation=++requestVersion.current;
      try{const value=await api<{steering:Instruction[]}>(`/coding-tasks/${task.id}/instructions`);if(live&&generation===requestVersion.current&&Array.isArray(value.steering))setRows(value.steering)}
      catch(reason){if(live&&generation===requestVersion.current)setError(reason instanceof Error?reason.message:String(reason))}
    };
    void refresh();const timer=window.setInterval(()=>void refresh(),2000);const changed=()=>void refresh();window.addEventListener('olcr-command-complete',changed);
    return()=>{live=false;window.clearInterval(timer);requestVersion.current++;window.removeEventListener('olcr-command-complete',changed)};
  },[task.id,version]);
  const change=async(item:Instruction,action:'cancel'|'send_now'|'retry')=>{
    if(mutating.current)return;const owner=task.id;mutating.current=true;requestVersion.current++;setBusy(true);setError('');setNotice('');
    try{
      const updated=await api<Instruction>(`/coding-tasks/${owner}/instructions/${encodeURIComponent(item.id)}`,{method:'PATCH',body:JSON.stringify({action,expected_message:item.message,expected_status:item.status})});
      if(identity.current!==owner)return;
      setRows(current=>current.map(row=>row.id===updated.id?updated:row));setVersion(n=>n+1);
      setNotice(action==='cancel'?(ja?'追加指示を取り消しました。':'Instruction cancelled.'):(ja?'この指示を次の工程・計画の区切りで反映します。':'This instruction will be applied at the next phase or planning boundary.'));
      window.dispatchEvent(new Event('olcr-command-complete'));
    }catch(reason){if(identity.current!==owner)return;const raw=reason instanceof Error?reason.message:String(reason);setError(/INSTRUCTION_ALREADY_CLAIMED|INSTRUCTION_CHANGED|TASK_NO_LONGER/.test(raw)?(ja?'指示の状態が変わりました。すでに反映中の指示は編集・取り消しできません。':'The instruction changed. Instructions already being applied cannot be edited or cancelled.'):raw)}
    finally{if(identity.current===owner){mutating.current=false;setBusy(false)}}
  };
  const pending=rows.filter(row=>row.status==='WAITING'||row.status==='QUEUED'||row.status==='NEEDS_REVIEW');
  if(!pending.length&&!notice&&!error)return null;
  return <section className="pending-instructions" aria-label={ja?'保留中の追加指示':'Pending instructions'}>
    <h3>{ja?'保留中の追加指示':'Pending instructions'}{pending.length>0&&<span>{pending.length}</span>}</h3>
    {pending.length>0&&<ul>{pending.map(item=><li key={item.id}>
      <small>{item.status==='NEEDS_REVIEW'?(ja?'前回は未反映。確認後に再試行':'Not applied previously. Review before retrying.'):item.status==='WAITING'?(ja?'完了後に実行':'After completion'):(ja?'次の区切りで反映':'Next boundary')}</small>
      <><p>{item.message}</p><div className="pending-instruction-actions">
        {item.status!=='QUEUED'&&<button type="button" disabled={busy||task.archived||task.status==='COMPLETED'} onClick={()=>onEdit(item)}><UiIcon name="edit" size={16}/>{ja?'編集':'Edit'}</button>}
        {item.status!=='QUEUED'&&<button type="button" disabled={busy||task.archived||task.status==='COMPLETED'} onClick={()=>void change(item,'cancel')}><UiIcon name="close" size={16}/>{ja?'取り消し':'Cancel instruction'}</button>}
        {item.status==='WAITING'&&<button type="button" disabled={busy||task.archived||task.status==='COMPLETED'} onClick={()=>void change(item,'send_now')}><UiIcon name="steer" size={16}/>{ja?'今の作業へ送る':'Send to current work'}</button>}
        {item.status==='NEEDS_REVIEW'&&<button type="button" disabled={busy||task.archived||task.status==='COMPLETED'} onClick={()=>void change(item,'retry')}><UiIcon name="steer" size={16}/>{ja?'確認して再試行':'Review and retry'}</button>}
      </div></>
    </li>)}</ul>}
    {notice&&<p role="status">{notice}</p>}{error&&<p role="alert">{error}</p>}
  </section>;
}
