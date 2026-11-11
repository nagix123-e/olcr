import {useEffect,useRef,useState} from 'react';
import {native,pickPath} from './native';
import {useLanguage} from './language';
type Client=(path:string,init?:RequestInit)=>Promise<any>;
export function ProjectContext({projectId,api}:{projectId:string;api:Client}) {
  const {language}=useLanguage();const ja=language==="ja";
  const [content,setContent]=useState(''),[source,setSource]=useState(''),[error,setError]=useState(''),[path,setPath]=useState('');
  const panel=useRef<HTMLDetailsElement>(null);const [drag,setDrag]=useState(false);
  const base=`/projects/${projectId}/core-context`;
  useEffect(()=>{document.documentElement.dataset.olcrActiveProjectId=projectId;return()=>{if(document.documentElement.dataset.olcrActiveProjectId===projectId)delete document.documentElement.dataset.olcrActiveProjectId}},[projectId]);
  useEffect(()=>{let alive=true;setContent('');setSource('');setError('');api(base).then(x=>{if(alive){setContent(x.content);setSource(x.source||'')}}).catch(e=>alive&&setError(e.message));return()=>{alive=false}},[projectId]);
  async function load(selected:string){try{setError('');const x=await api(base+'/load',{method:'POST',body:JSON.stringify({path:selected})});setContent(x.content);setSource(x.source||'');setPath(selected)}catch(e){setError(String(e))}}
  useEffect(()=>{let cancelled=false;let unlisten:(()=>void)|undefined;
    native()?.event.listen<{paths:string[];position:{x:number;y:number}}>('tauri://drag-drop',e=>{
      const bounds=panel.current?.getBoundingClientRect();const {x,y}=e.payload.position;const scale=window.devicePixelRatio;
      if(bounds&&x/scale>=bounds.left&&x/scale<=bounds.right&&y/scale>=bounds.top&&y/scale<=bounds.bottom&&e.payload.paths.length===1)void load(e.payload.paths[0]);
      setDrag(false);
    }).then(fn=>{if(cancelled)fn();else unlisten=fn});
    return()=>{cancelled=true;unlisten?.()};
  },[projectId]);
  useEffect(()=>{
    let disposed=false;const cleanup:Array<()=>void>=[];
    for(const name of ['tauri://drag-enter','tauri://drag-leave']){
      native()?.event.listen(name,()=>setDrag(name.endsWith('enter'))).then(fn=>{if(disposed)fn();else cleanup.push(fn)});
    }
    const refresh=()=>{void api(base).then(x=>{setContent(x.content);setSource(x.source||'')}).catch(e=>setError(String(e)))};
    window.addEventListener('olcr-command-complete',refresh);
    return()=>{disposed=true;cleanup.forEach(fn=>fn());window.removeEventListener('olcr-command-complete',refresh)};
  },[projectId]);
  async function action(kind:string){try{setError('');const x=await api(base+kind,{method:kind?'POST':'PUT',body:JSON.stringify(kind==='/load'?{path}:kind?{}:{content})});setContent(x.content);setSource(x.source||'')}catch(e){setError(String(e))}}
  return <details ref={panel} className={"project-context"+(drag?" drag":"")} onDragOver={e=>{e.preventDefault();setDrag(true)}} onDragLeave={()=>setDrag(false)}><summary>{ja?"プロジェクトの基本コンテキスト":"Project Core Context"}</summary><button onClick={()=>{void pickPath().then(p=>{if(p)return load(p)}).catch(e=>setError(String(e)))}}>{ja?"ファイルを選択":"Choose file"}</button><p>{ja?"このプロジェクトで許可されたワークスペースからテキストファイルをドロップしてください。":"Drop one text file from this Project’s authorized workspace here."}</p><small>{source|| (content?(ja?'保存済みテキスト':'Saved text'):(ja?'空':'Empty'))}</small><textarea aria-label={ja?"プロジェクトの基本コンテキスト":"Project core context"} value={content} onChange={e=>setContent(e.target.value)}/><div><button onClick={()=>void action('')}>{ja?"保存":"Save"}</button><button onClick={()=>void action('/reload')} disabled={!source}>{ja?"再読み込み":"Reload"}</button><button onClick={()=>{void api(base,{method:'PUT',body:JSON.stringify({content:''})}).then(()=>{setContent('');setSource('')}).catch(e=>setError(e.message))}}>{ja?"消去":"Clear"}</button></div><input aria-label={ja?"基本コンテキストのパス":"Core context path"} placeholder={ja?"許可されたワークスペースのファイルパス":"Authorized workspace file path"} value={path} onChange={e=>setPath(e.target.value)}/><button onClick={()=>void action('/load')}>{ja?"ファイルを読み込む":"Load file"}</button>{error&&<p role="alert">{error}</p>}</details>
}
