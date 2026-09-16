import {FormEvent, useEffect, useRef, useState} from 'react';
import './features.css';
import {BanterLoader} from './BanterLoader';
import {api,subscribeCodingTask} from './api';
import {native} from './native';
import {CodingPhaseReport,CodingTask,CodingTaskProgress,shouldAcceptPlanRevision} from './CodingTaskProgress';
export type Command = {id:string; command:string; description:string; arguments:string; enabled:boolean};
export type Attachment={name:string;content:string;mimeType:string;dataUrl?:string};
export function ChatComposer({text,setText,disabled,loading,pause,send,commands,attachment,setAttachment,conversationId}:{text:string;setText:(s:string)=>void;disabled:boolean;loading:boolean;pause?:()=>void;send:(e:FormEvent)=>void;commands:Command[];attachment?:Attachment|null;setAttachment:(a:Attachment|null)=>void;conversationId?:string|null}) {
  const [closed,setClosed]=useState(false), [selected,setSelected]=useState(0);
  const [codingTask,setCodingTask]=useState<CodingTask|null>(null), [codingTaskId,setCodingTaskId]=useState<string|null>(null);
  const [phaseReports,setPhaseReports]=useState<CodingPhaseReport[]>([]), [progressLoading,setProgressLoading]=useState(false), [progressError,setProgressError]=useState(''), [pausePending,setPausePending]=useState(false), [refreshNonce,setRefreshNonce]=useState(0);
  const input=useRef<HTMLTextAreaElement>(null), fileInput=useRef<HTMLInputElement>(null), composing=useRef(false);
  const [attachmentError,setAttachmentError]=useState(''), [dragging,setDragging]=useState(false);
  const matches=commands.filter(c=>c.enabled && c.command.toLowerCase().startsWith(text.toLowerCase()));
  const visible=!closed && text.startsWith('/') && matches.length>0;
  useEffect(()=>{setSelected(0)},[text]);
  useEffect(()=>subscribeCodingTask(taskId=>{setCodingTaskId(taskId);setCodingTask(null);setPhaseReports([]);setProgressError('')}),[]);
  useEffect(()=>{if(!conversationId){setCodingTaskId(null);setCodingTask(null);setPhaseReports([]);setProgressError('');return}let live=true;void api<{tasks:CodingTask[]}>(`/conversations/${conversationId}/coding-tasks`).then(value=>{if(!live)return;const task=(value.tasks||[]).find(item=>!item.archived);setCodingTaskId(task?.id||null);setCodingTask(task||null);setPhaseReports([]);setProgressError('')}).catch(error=>{if(live)setProgressError(error instanceof Error?error.message:String(error))});return()=>{live=false}},[conversationId]);
  useEffect(()=>{
    if(!codingTaskId)return;
    let live=true,inFlight=false,timer:number|undefined,requestId=0;
    const refresh=async()=>{
      if(inFlight)return;
      inFlight=true;const current=++requestId;setProgressLoading(true);
      try{
        const [task,reportResult]=await Promise.all([api<CodingTask>(`/coding-tasks/${codingTaskId}`),api<{reports:CodingPhaseReport[]}>(`/coding-tasks/${codingTaskId}/phase-reports`)]);
        if(!live||current!==requestId)return;
        setCodingTask(previous=>shouldAcceptPlanRevision(previous,task)?task:previous);setPhaseReports(Array.isArray(reportResult.reports)?reportResult.reports:[]);setProgressError('');
        if(["QUEUED","PLANNING","RUNNING","FINAL_REPORTING","QWEN_REPLANNING"].includes(task.status))timer=window.setTimeout(refresh,1000);
      }catch(error){if(live&&current===requestId){setProgressError(error instanceof Error?error.message:'Progress unavailable');timer=window.setTimeout(refresh,1000)}}
      finally{if(live&&current===requestId)setProgressLoading(false);inFlight=false}
    };
    void refresh();
    return()=>{live=false;if(timer!==undefined)window.clearTimeout(timer)};
  },[codingTaskId,refreshNonce]);
  // A task can be stopped while it is still queued/planning as well as while
  // a model call is running.  The previous check only treated active model
  // states as stoppable, leaving the visible stop button disabled during the
  // initial queue/planning window.
  const codingActive=Boolean(codingTask&&["QUEUED","PLANNING","RUNNING","FINAL_REPORTING"].includes(codingTask.status));
  const pauseTask=()=>{if(!codingTask||pausePending)return;setPausePending(true);void api<CodingTask>(`/coding-tasks/${codingTask.id}`,{method:'PATCH',body:JSON.stringify({pause_requested:true})}).then(task=>{setCodingTask(task);setRefreshNonce(value=>value+1)}).catch(error=>setProgressError(error instanceof Error?error.message:String(error))).finally(()=>setPausePending(false))};
  const resumeTask=()=>{if(codingTask)void api<CodingTask>(`/coding-tasks/${codingTask.id}`,{method:'PATCH',body:JSON.stringify({resume:true})}).then(task=>{setCodingTask(task);setRefreshNonce(value=>value+1)}).catch(error=>setProgressError(error instanceof Error?error.message:String(error)))};
  const archiveTask=()=>{if(codingTask)void api<CodingTask>(`/coding-tasks/${codingTask.id}`,{method:'PATCH',body:JSON.stringify({archived:true})}).then(task=>{setCodingTask(task);setRefreshNonce(value=>value+1)}).catch(error=>setProgressError(error instanceof Error?error.message:String(error)))};
  function choose(c:Command){setText(c.command+(c.arguments?' ':''));setClosed(true);input.current?.focus()}
  const clearAttachment=()=>{setAttachment(null);setAttachmentError('');if(fileInput.current)fileInput.current.value=''};
  async function attach(file:File){
    if(!file) return;
    setAttachmentError('');
    if(file.size===0) throw Error('The selected file is empty');
    if(file.size>5_000_000) throw Error('Attached file is too large (max 5 MB)');
    // Some macOS file providers omit File.type. Infer only the image MIME
    // needed by the vision path; all other files remain text attachments.
    const extension=file.name.toLowerCase().split('.').pop()||'';
    const inferredImage=extension==='png'?'image/png':extension==='jpg'||extension==='jpeg'?'image/jpeg':extension==='webp'?'image/webp':'';
    const mimeType=file.type.startsWith('image/')?file.type:inferredImage||file.type||'text/plain';
    if(mimeType.startsWith('image/')){
      const dataUrl=await new Promise<string>((resolve,reject)=>{
        const reader=new FileReader();
        reader.onload=()=>typeof reader.result==='string'&&reader.result.startsWith('data:image/')?resolve(reader.result):reject(Error('The image could not be read'));
        reader.onerror=()=>reject(reader.error||Error('The image could not be read'));
        reader.readAsDataURL(file);
      });
      setAttachment({name:file.name,content:'',mimeType,dataUrl});
      return;
    }
    let content:string;
    try{content=await file.text()}catch(error){throw Error(`Unable to read ${file.name}: ${error instanceof Error?error.message:'unknown error'}`)}
    if(!content && file.size>0) throw Error(`Unable to read ${file.name} as text`);
    setAttachment({name:file.name,content:content.slice(0,200000),mimeType});
  }
  const handleFile=(file:File|undefined)=>{if(!file)return;void attach(file).catch(error=>{setAttachment(null);setAttachmentError(error instanceof Error?error.message:String(error))})};
  useEffect(()=>{
    const bridge=native();
    if(!bridge?.event)return;
    let live=true;
    const subscription=bridge.event.listen<{paths?:string[]}>('tauri://drag-drop',event=>{
      const path=event.payload.paths?.[0];
      if(!path)return;
      void bridge.core.invoke<Attachment>('read_dropped_file',{path}).then(file=>{
        if(live){setAttachmentError('');setAttachment(file)}
      }).catch(error=>{if(live)setAttachmentError(error instanceof Error?error.message:'このファイルを追加できませんでした')});
    });
    return()=>{live=false;void subscription.then(unlisten=>unlisten())};
  },[setAttachment]);
  const showingLoader=loading||codingActive;
  return <>{showingLoader&&<BanterLoader/>}{codingTask&&!codingTask.archived&&<CodingTaskProgress task={codingTask} reports={phaseReports} loading={progressLoading} error={progressError} pausePending={pausePending} onPause={pauseTask} onResume={resumeTask} onArchive={archiveTask}/>} {codingTaskId&&!codingTask&&progressError&&<p className="coding-task-unavailable" role="alert">Coding Orchestrator の進行状況を取得できませんでした。再度メッセージを開いてください。</p>}<form className={dragging?'composer dragging':'composer'} onSubmit={send} onDragOver={e=>{e.preventDefault();e.dataTransfer.dropEffect='copy';setDragging(true)}} onDragLeave={e=>{if(e.currentTarget===e.target)setDragging(false)}} onDrop={e=>{e.preventDefault();setDragging(false);handleFile(e.dataTransfer.files[0])}}>
    {visible&&<div className="command-palette" role="listbox" id="commands" aria-label="OLCR commands">{matches.map((c,i)=><button type="button" role="option" id={'command-'+i} aria-selected={i===selected} className={i===selected?'selected':''} key={c.id} onMouseDown={e=>e.preventDefault()} onClick={()=>choose(c)}><strong>{c.command}</strong><small>{c.description}</small></button>)}</div>}
    <input ref={fileInput} type="file" id="olcr-attachment" hidden onChange={e=>{handleFile(e.currentTarget.files?.[0]);e.currentTarget.value=''}}/><div className="composer-input">{attachment&&<div className="attachment-chip" title={attachment.name} role="status">📎 {attachment.name}<button type="button" aria-label="Remove attachment" onClick={clearAttachment}>×</button></div>}{attachmentError&&<div className="attachment-error" role="alert" aria-live="polite">{attachmentError}</div>}<textarea ref={input} aria-label="Message" aria-controls={visible?'commands':undefined} aria-activedescendant={visible?'command-'+selected:undefined} placeholder="Ask OLCR…" value={text} disabled={disabled}
      onPaste={e=>{const image=Array.from(e.clipboardData.items).find(item=>item.type.startsWith('image/'))?.getAsFile()||e.clipboardData.files[0];if(image){e.preventDefault();handleFile(image)}}}
      onCompositionStart={()=>{composing.current=true}} onCompositionEnd={()=>{composing.current=false}}
      onChange={e=>{setText(e.target.value);setClosed(false)}} onKeyDown={e=>{
        if(composing.current||e.nativeEvent.isComposing||e.keyCode===229)return;
        if(visible&&['ArrowDown','ArrowUp','Escape'].includes(e.key)){e.preventDefault();if(e.key==='Escape')setClosed(true);else setSelected(i=>(i+(e.key==='ArrowDown'?1:-1)+matches.length)%matches.length);return}
        if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(visible)choose(matches[selected]||matches[0]);else if(!disabled&&(text.trim()||attachment))e.currentTarget.form?.requestSubmit()}
    }}/></div><button type="button" className="attach-button" aria-label="Attach file" title="Attach file" disabled={disabled||codingActive} onClick={()=>fileInput.current?.click()}>＋</button><button type={showingLoader?"button":"submit"} onClick={codingActive?pauseTask:showingLoader?()=>pause?.():undefined} aria-label={showingLoader?(codingActive?"Coding Taskを一時停止":"Stop generating"):"Send"} title={showingLoader?(codingActive?"Coding Taskを一時停止":"Stop generating"):undefined} className={showingLoader?"send-button loading":"send-button"} disabled={showingLoader?(codingActive?pausePending:!pause):disabled}>{showingLoader?<span className="loading-square" aria-hidden="true"/>:"↑"}</button>
  </form></>
}
