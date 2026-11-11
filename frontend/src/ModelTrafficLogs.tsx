import {useCallback,useEffect,useState} from 'react';
import {SettingsBack} from './SettingsBack';
import {UiIcon} from './UiIcon';
import {useLanguage} from './language';
import './model-traffic-logs.css';

type Api=(path:string,init?:RequestInit)=>Promise<any>;
type TrafficEntry={id:number;created_at:number;finished_at:number|null;project_name:string|null;conversation_title:string|null;role:string;model:string;runtime:string;status:string;duration_ms:number|null;error_code:string|null;internet:boolean};
type TrafficDetail=TrafficEntry&{request:unknown;response:unknown};
type TrafficCursor={id:number;created_at:number};
const PAGE_SIZE=50;
const retentionValues:Array<number|'unlimited'>=[1,7,30,90,'unlimited'];

function compactPayload(value:unknown,key=''):unknown{
  if(typeof value==='string'){
    if(/^data:([^;,]+);base64,/.test(value)){
      const match=value.match(/^data:([^;,]+);base64,(.*)$/s)!;
      return `[${match[1]}, 約 ${Math.floor(match[2].length*3/4).toLocaleString()} bytes]`;
    }
    if(/base64/i.test(key))return `[base64, 約 ${Math.floor(value.length*3/4).toLocaleString()} bytes]`;
    return value;
  }
  if(Array.isArray(value))return value.map(item=>compactPayload(item));
  if(value&&typeof value==='object'){
    const record=value as Record<string,unknown>;
    if(typeof record.$binary_base64==='string')return {type:'binary',size_bytes:record.size_bytes??Math.floor(record.$binary_base64.length*3/4)};
    return Object.fromEntries(Object.entries(record).map(([name,item])=>[name,compactPayload(item,name)]));
  }
  return value;
}

function prettyPayload(value:unknown){
  if(value===null||value===undefined)return '—';
  try{return JSON.stringify(compactPayload(value),null,2)}catch{return String(value)}
}

export function ModelTrafficLogs({api,onBack}:{api:Api;onBack:()=>void}){
  const {language}=useLanguage(),ja=language==='ja';
  const [entries,setEntries]=useState<TrafficEntry[]>([]);
  const [cursor,setCursor]=useState<TrafficCursor|null>(null);
  const [retention,setRetention]=useState<number|'unlimited'>(30);
  const [openId,setOpenId]=useState<number|null>(null);
  const [details,setDetails]=useState<Record<number,TrafficDetail>>({});
  const [busy,setBusy]=useState(false),[retentionBusy,setRetentionBusy]=useState(false);
  const [error,setError]=useState('');

  const loadFirst=useCallback(async()=>{
    setBusy(true);setError('');
    try{
      const [settings,page]=await Promise.all([api('/model-traffic/settings'),api(`/model-traffic?limit=${PAGE_SIZE}`)]);
      setRetention(settings.retention_days===null?'unlimited':settings.retention_days);
      setEntries(Array.isArray(page.entries)?page.entries:[]);setCursor(page.next_cursor??null);
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
    finally{setBusy(false)}
  },[api]);
  useEffect(()=>{void loadFirst()},[loadFirst]);

  const loadOlder=async()=>{
    if(cursor===null||busy)return;
    setBusy(true);setError('');
    try{
      const page=await api(`/model-traffic?before_created_at=${encodeURIComponent(cursor.created_at)}&before_id=${cursor.id}&limit=${PAGE_SIZE}`);
      setEntries(current=>[...current,...(Array.isArray(page.entries)?page.entries:[])]);setCursor(page.next_cursor??null);
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
    finally{setBusy(false)}
  };
  const chooseRetention=async(value:string)=>{
    const next=value==='unlimited'?null:Number(value);
    setRetentionBusy(true);setError('');
    try{
      const result=await api('/model-traffic/settings',{method:'PUT',body:JSON.stringify({retention_days:next})});
      setRetention(result.retention_days===null?'unlimited':result.retention_days);
      setOpenId(null);setDetails({});
      const page=await api(`/model-traffic?limit=${PAGE_SIZE}`);
      setEntries(Array.isArray(page.entries)?page.entries:[]);setCursor(page.next_cursor??null);
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
    finally{setRetentionBusy(false)}
  };
  const toggleEntry=async(id:number)=>{
    if(openId===id){setOpenId(null);return}
    setOpenId(id);
    if(details[id])return;
    try{
      const detail=await api(`/model-traffic/${id}`);
      setDetails(current=>({...current,[id]:detail}));
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
  };

  const retentionLabel=retention==='unlimited'? (ja?'無期限':'Never') : ja?`${retention}日`:`${retention} days`;
  const statusLabel=(status:string)=>status==='success'?(ja?'完了':'Success'):status==='error'?(ja?'失敗':'Failed'):status==='cancelled'?(ja?'中断':'Cancelled'):(ja?'処理中':'Pending');
  return <section className="chat-settings model-traffic-settings" aria-label={ja?'トラフィックログ':'Traffic logs'}>
    <SettingsBack onBack={onBack}/>
    <header className="model-traffic-header">
      <div className="model-traffic-heading">
        <h2>{ja?'トラフィックログ':'Traffic logs'}</h2>
        <p>{ja?'OLCRが管理するモデルと外部APIの通信を、プロジェクトをまたいで時系列に表示します。モデルへの入力と応答の本文もローカルに保存し、項目を開くと確認できます。':'A chronological view of model and external API traffic across all projects. Model inputs and responses are stored locally and shown when an entry is opened.'}</p>
        <p className="model-traffic-legend"><span className="model-traffic-legend-swatch" aria-hidden="true"/>{ja?'外部通信（APIを含む）を行ったログは、オレンジ色の背景で表示されます。':'An orange background marks traffic that includes external communication, including APIs.'}</p>
      </div>
      <button className="model-traffic-refresh" type="button" onClick={()=>void loadFirst()} disabled={busy} aria-label={ja?'ログを更新':'Refresh traffic logs'} title={ja?'ログを更新':'Refresh traffic logs'}><UiIcon name="refresh" size={18}/></button>
    </header>
    <section className="model-traffic-retention" aria-label={ja?'保存期間':'Retention'}>
      <label htmlFor="model-traffic-retention">{ja?'ログの保存期間':'Keep logs for'}</label>
      <select id="model-traffic-retention" value={retention} disabled={retentionBusy} onChange={event=>void chooseRetention(event.target.value)}>
        {retentionValues.map(value=><option key={value} value={value}>{value==='unlimited'?(ja?'無期限':'Never'):ja?`${value}日`:`${value} days`}</option>)}
      </select>
      <small>{ja?`期限を過ぎた記録は自動削除されます（現在：${retentionLabel}）。`:`Expired records are deleted automatically (currently: ${retentionLabel}).`}</small>
    </section>
    {error&&<p className="model-traffic-error" role="alert">{error}</p>}
    {busy&&entries.length===0&&<p className="model-traffic-empty">{ja?'読み込み中…':'Loading…'}</p>}
    {!busy&&entries.length===0&&<p className="model-traffic-empty">{ja?'まだトラフィックログはありません。':'No model traffic has been recorded yet.'}</p>}
    <ol className="model-traffic-list" aria-label={ja?'通信の時系列':'Traffic timeline'}>
      {entries.map(entry=>{
        const open=openId===entry.id,detail=details[entry.id],externalApi=entry.role==='external_api';
        return <li key={entry.id}>
          <article className={`model-traffic-entry ${entry.internet?'is-internet':''} ${open?'is-open':''}`}>
            <button type="button" className="model-traffic-summary" aria-expanded={open} onClick={()=>void toggleEntry(entry.id)}>
              <span className="model-traffic-time">{new Date(entry.created_at*1000).toLocaleString(ja?'ja-JP':undefined)}</span>
              <span className={`model-traffic-status status-${entry.status}`}>{statusLabel(entry.status)}{entry.duration_ms!==null&&` · ${Math.round(entry.duration_ms)} ms`}</span>
              <strong className="model-traffic-model">{entry.model}</strong>
              <span className="model-traffic-role">{entry.role} · {entry.runtime}</span>
              <span className="model-traffic-labels"><span>{entry.project_name|| (ja?'プロジェクトなし':'No project')}</span><span>{entry.conversation_title|| (ja?'会話なし':'No conversation')}</span></span>
              <span className="model-traffic-chevron" aria-hidden="true">{open?'−':'+'}</span>
            </button>
            {open&&<div className="model-traffic-detail">
              {!detail?<p>{ja?'本文を読み込み中…':'Loading request and response…'}</p>:<>
                {entry.error_code&&<p className="model-traffic-error-code">{ja?'エラーコード':'Error code'}: {entry.error_code}</p>}
                <section><h3>{externalApi?(ja?'リクエスト情報':'Request details'):(ja?'モデルへの入力':'Model input')}</h3><pre>{prettyPayload(detail.request)}</pre></section>
                <section><h3>{externalApi?(ja?'通信結果':'Response details'):(ja?'モデルからの応答':'Model response')}</h3><pre>{prettyPayload(detail.response)}</pre></section>
              </>}
            </div>}
          </article>
        </li>;
      })}
    </ol>
    {cursor!==null&&<button type="button" className="model-traffic-load-more" disabled={busy} onClick={()=>void loadOlder()}>{busy?(ja?'読み込み中…':'Loading…'):(ja?'過去のログを読み込む':'Load older logs')}</button>}
  </section>;
}
