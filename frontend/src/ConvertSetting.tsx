import {useEffect,useState} from 'react';
import {useLanguage} from './language';

type State={enabled?:boolean;state:string;message?:string;error?:string};
type Api=(path:string,init?:RequestInit)=>Promise<any>;

/** Same capability row, switch, hints and error presentation as the other tools. */
export function ConvertSetting({api,enabled,save}:{api:Api;enabled:boolean;save:(value:Record<string,boolean>)=>Promise<any>}){
  const ja=useLanguage().language==='ja';
  const [status,setStatus]=useState<State>({state:'idle'}),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{
    let live=true,timer:ReturnType<typeof setTimeout>;
    if(!enabled){setStatus({state:'off'});return}
    const poll=async()=>{
      try{const next=await api('/file-conversion/runtime');if(live){setStatus(next);setError('')}}
      catch(reason){if(live)setError(reason instanceof Error?reason.message:String(reason))}
      if(live)timer=setTimeout(()=>void poll(),1500);
    };
    void poll();return()=>{live=false;clearTimeout(timer)};
  },[api,enabled]);
  const toggle=async(value:boolean)=>{
    setBusy(true);setError('');
    try{await save({convert_enabled:value})}
    catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
    finally{setBusy(false)}
  };
  const retry=async()=>{setBusy(true);setError('');try{setStatus(await api('/file-conversion/runtime/retry',{method:'POST'}))}catch(reason){setError(reason instanceof Error?reason.message:String(reason))}finally{setBusy(false)}};
  const labels:Record<string,string>=ja?{preparing:'変換環境を準備中…',ready:'準備完了',failed:'準備に失敗しました',idle:'変換時に起動します',stopping:'停止中…'}:{preparing:'Preparing conversion tools…',ready:'Ready',failed:'Setup failed',idle:'Starts when converting',stopping:'Stopping…'};
  return <div className="capability-item">
    <label className="setting-row capability-setting"><span>Convert</span><input type="checkbox" role="switch" checked={enabled} disabled={busy} aria-describedby="hint-convert" onChange={event=>void toggle(event.target.checked)}/></label>
    <p className="setting-hint" id="hint-convert">{ja?'添付ファイルを指定の形式に変換します。ONにすると初回のみ変換ツールをダウンロードします（数GB）。保存先はプロジェクトの設定フォルダ、未設定ならDownloadsです。':'Convert attached files to the requested format. Turning ON downloads conversion tools once (several GB). Results go to the project folder, or Downloads when unset.'}</p>
    {enabled&&<p className="setting-hint" role="status">{ja?(status.message||labels[status.state]):labels[status.state]||status.state}</p>}
    {enabled&&status.state==='failed'&&<button type="button" disabled={busy} onClick={()=>void retry()}>{ja?'再試行':'Retry'}</button>}
    {error&&<p role="alert" className="error">{error}</p>}
  </div>;
}
