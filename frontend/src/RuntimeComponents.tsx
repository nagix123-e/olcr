import {useEffect,useState} from 'react';
import {useLanguage} from './language';
import {SettingHelp} from './SettingHelp';
type Api=(path:string,init?:RequestInit)=>Promise<any>;
type Component={name:string;state:string;message?:string;removable?:boolean};
type ConversionDownloads={downloaded:boolean;enabled:boolean;state:string;message?:string;has_conversion_data?:boolean};
const names:Record<string,[string,string]>={laya:['判断用モデル','Decision model'],agents:['カスタムエージェント','Custom agents'],'node-tools':['参照ツール','Reference tools'],browser:['ブラウザ','Browser'],serena:['Serena MCP','Serena MCP']};
names.convertx=['ConvertX変換環境','ConvertX runtime'];
const help:Record<string,[string,string]>={
 serena:['コードのシンボル参照に使うSerenaの専用環境です。接続確認ではコードの読み取りや変更は実行しません。','The dedicated Serena environment for code symbol lookup. Connection checks do not read or change project code.'],
 laya:['候補から処理や操作を選ぶための小さなモデルです。会話の回答を生成するBrainとは別に使います。','A small model that selects among actions or processing options, separate from the Brain that writes answers.'],
 agents:['作成したエージェントを動かす実行環境です。エージェントの名前・指示・知識は、エージェント画面から設定します。','The engine for running custom agents. Configure their names, instructions and knowledge in Agents.'],
 'node-tools':['検索や資料の参照に使うローカルツールの実行環境です。外部通信が必要な機能は、外部アクセスの設定にも従います。','Local tools for reference lookup. Tools requiring network access also follow the external access setting.'],
 browser:['スクレイピングやローカルE2Eで使う管理ブラウザーです。ダウンロードだけではページを開いたり操作したりしません。','The managed browser used for scraping and local E2E. Downloading it does not start browsing or actions.'],
};
function useComponents(api:Api,onConversion?:(status:ConversionDownloads|null)=>void){
 const [rows,setRows]=useState<Component[]>([]);
 useEffect(()=>{let live=true;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{
  try{const result=await api('/components');if(live){setRows(result.components||[]);onConversion?.(result.conversion||null)}}catch{}
  if(live)timer=setTimeout(()=>void poll(),2000);
 };void poll();return()=>{live=false;clearTimeout(timer)}},[api,onConversion]);
 return rows;
}
export function RuntimeSetupStatus({api}:{api:Api}){
 const ja=useLanguage().language==='ja';const rows=useComponents(api);
 const active=rows.filter(row=>['downloading','installing','error'].includes(row.state));
 if(!active.length)return null;
 return <div className="screen-capture-permission" role="status">{active.map(row=><div key={row.name}>
  <span>{names[row.name]?.[ja?0:1]||row.name} · {ja?(row.message||'準備中…'):(row.state==='error'?'Setup failed. Check your connection and retry.':'Preparing local tools…')}</span>
  {row.state==='error'&&<button type="button" onClick={()=>void api(`/components/${row.name}/prepare`,{method:'POST'}).catch(()=>{})}>{ja?'再試行':'Retry'}</button>}
 </div>)}</div>;
}
export function RuntimeComponentsSettings({api}:{api:Api}){
 const ja=useLanguage().language==='ja';const [convert,setConvert]=useState<ConversionDownloads|null>(null);const rows=useComponents(api,setConvert);const [error,setError]=useState('');const [removing,setRemoving]=useState('');
 const prepare=async(name:string)=>{setError('');try{await api(`/components/${name}/prepare`,{method:'POST'})}catch{setError(ja?'準備を開始できませんでした。':'Could not start setup.')}};
 const remove=async(name:string)=>{const label=names[name]?.[ja?0:1]||name;const warning=name==='convertx'?(ja?`${label}と関連する仮想マシン・変換環境のデータを削除します。保存しておきたい変換データがある場合は、先に別の場所へ保存してください。続けますか？`:`This removes ${label}, its virtual machine, and conversion data. Save any conversion data you want to keep elsewhere first. Continue?`):(ja?`${label}のダウンロード済みファイルを削除します。次回使うときは再ダウンロードが必要です。続けますか？`:`Delete the downloaded files for ${label}? It will need to download again before the next use.`);if(!window.confirm(warning))return;setRemoving(name);setError('');try{await api(name==='convertx'?'/file-conversion/runtime/downloads':`/components/${name}`,{method:'DELETE'})}catch(reason){setError(reason instanceof Error?reason.message:(ja?'削除を開始できませんでした。':'Could not start removal.'))}finally{setRemoving('')}};
 return <section className="panel"><h2>{ja?'追加機能のダウンロード':'Local feature downloads'}</h2>
  <p className="setting-hint">{ja?'判断用モデルは初回起動時、それ以外は機能を初めて使う際に取得します。各項目のダウンロード済みファイルはここから個別に削除できます。ConvertXの削除では仮想マシン内のデータも消去されます。':'The decision model downloads on initial setup. Other engines download when first used. Remove each downloaded item here. Removing ConvertX also deletes data stored in its virtual machine.'}</p>
  {rows.map(row=><div className="capability-item" key={row.name}><div className="setting-row"><span className="setting-label-with-help"><span>{names[row.name]?.[ja?0:1]||row.name}</span>{help[row.name]&&<SettingHelp label={names[row.name]?.[ja?0:1]||row.name}>{help[row.name][ja?0:1]}</SettingHelp>}</span>
   <span className="runtime-component-actions">{row.state==='removing'?<span>{ja?'削除中…':'Removing…'}</span>:<>{row.state==='ready'||row.removable?<><span>{row.state==='ready'?(ja?'準備完了':'Ready'):(ja?'保存データあり':'Local files found')}</span><button type="button" disabled={Boolean(removing)||['downloading','installing'].includes(row.state)} onClick={()=>void remove(row.name)}>{ja?'削除':'Remove'}</button>{row.state==='error'&&<button type="button" disabled={Boolean(removing)} onClick={()=>void prepare(row.name)}>{ja?'再試行':'Retry'}</button>}</>:<button type="button" disabled={Boolean(removing)||['downloading','installing'].includes(row.state)} onClick={()=>void prepare(row.name)}>{['downloading','installing'].includes(row.state)?(ja?'準備中…':'Preparing…'):row.state==='error'?(ja?'再試行':'Retry'):(ja?'ダウンロード':'Download')}</button>}</>}</span>
  </div>{row.message&&<p className="setting-hint" role="status">{ja?row.message:row.state==='error'?'Setup failed. Please retry.':row.state==='removing'?'Removing local files…':'Preparing local tools…'}</p>}</div>)}
  <div className="capability-item"><div className="setting-row"><span className="setting-label-with-help"><span>{names.convertx[ja?0:1]}</span></span><span className="runtime-component-actions">{convert?.downloaded?<><span>{convert.state==='removing'?(ja?'削除中…':'Removing…'):(ja?'ダウンロード済み':'Downloaded')}</span><button type="button" disabled={Boolean(removing)||convert.enabled||['preparing','downloading','stopping','removing'].includes(convert.state)} onClick={()=>void remove('convertx')}>{ja?'削除':'Remove'}</button></>:<span>{ja?'未取得':'Not downloaded'}</span>}</span></div>{convert?.enabled&&<p className="setting-hint">{ja?'削除するには、先に「チャットで使う機能」のConvertをOFFにしてください。':'Turn off Convert under Chat capabilities before removing its downloads.'}</p>}{convert?.state==='removing'&&<p className="setting-hint" role="status">{convert.message||(ja?'削除中…':'Removing…')}</p>}</div>
  {error&&<p role="alert" className="error">{error}</p>}
 </section>;
}
