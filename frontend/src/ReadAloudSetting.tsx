import {useEffect,useRef,useState} from 'react';
import {useLanguage,type Language} from './language';
import {READ_ALOUD_CHANGED,readAloudEnabled,setReadAloudEnabled} from './readAloud';

type Api=(path:string,init?:RequestInit)=>Promise<any>;
export function ReadAloudSetting({api}:{api:Api}){
 const {language}=useLanguage(),ja=language==='ja';
 const [enabled,setEnabled]=useState(readAloudEnabled),[checking,setChecking]=useState(false),[downloading,setDownloading]=useState(false),[error,setError]=useState(''),[prompt,setPrompt]=useState<{language:Language;model:string}|null>(null);
 const generation=useRef(0),dialog=useRef<HTMLElement>(null);
 useEffect(()=>{if(!prompt)return;const previous=document.activeElement as HTMLElement|null;dialog.current?.querySelector<HTMLButtonElement>("button")?.focus();const keyboard=(event:KeyboardEvent)=>{if(event.key==="Escape"&&!downloading){event.preventDefault();setPrompt(null)}if(event.key==="Tab"){const controls=Array.from(dialog.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")||[]);const first=controls[0],last=controls[controls.length-1];if(!first){event.preventDefault();return}if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}};document.addEventListener("keydown",keyboard);return()=>{document.removeEventListener("keydown",keyboard);previous?.focus()}},[prompt,downloading]);
 const enable=async()=>{
  const request=++generation.current;setChecking(true);setError('');
  try{const status=await api(`/audio/synthesis/status?language=${language}`);if(request!==generation.current)return;if(status.downloaded)setReadAloudEnabled(true);else{setReadAloudEnabled(false);setPrompt({language,model:status.model})}}
  catch{if(request===generation.current){setReadAloudEnabled(false);setError(ja?'読み上げモデルを確認できませんでした。OLCRの再起動後にお試しください。':'Could not check the read-aloud model. Restart OLCR and try again.')}}
  finally{if(request===generation.current)setChecking(false)}
 };
 useEffect(()=>{const update=()=>setEnabled(readAloudEnabled());window.addEventListener(READ_ALOUD_CHANGED,update);return()=>window.removeEventListener(READ_ALOUD_CHANGED,update)},[]);
 useEffect(()=>{generation.current++;setPrompt(null);setDownloading(false);setChecking(false);setError('');if(readAloudEnabled())void enable();return()=>{generation.current++}},[language]);
 const download=async()=>{if(!prompt)return;const request=++generation.current;setDownloading(true);setError('');try{await api('/audio/model/prepare',{method:'POST',body:JSON.stringify({language:prompt.language,confirm_download:true})});const status=await api(`/audio/synthesis/status?language=${prompt.language}`);if(!status.downloaded)throw Error('AUDIO_MODEL_INCOMPLETE');if(request===generation.current){setReadAloudEnabled(true);setPrompt(null)}}catch{if(request===generation.current)setError(ja?'音声モデルをダウンロードできませんでした。ネットワーク接続と空き容量を確認してください。':'Could not download the speech model. Check your connection and free disk space.')}finally{if(request===generation.current)setDownloading(false)}};
 return <div className="read-aloud-setting">
  <label className="capability-setting"><span>{ja?'読み上げ':'Read aloud'}</span><input type="checkbox" role="switch" checked={enabled} disabled={checking||downloading} onChange={event=>{if(event.target.checked)void enable();else{generation.current++;setReadAloudEnabled(false);setPrompt(null);setError('')}}}/></label>
  <p className="setting-hint">{ja?'回答を生成できた部分から順に読み上げます。':'Read answers aloud as they are generated.'} {ja?'日本語：LFM2.5-Audio-1.5B-JP':'English: LFM2.5-Audio-1.5B'}</p>
  {checking&&<p role="status" className="setting-hint">{ja?'音声モデルを確認中…':'Checking speech model…'}</p>}
  {error&&!prompt&&<p role="alert">{error}</p>}
  {prompt&&<div className="modal" role="presentation"><section ref={dialog} className="voice-model-consent" role="dialog" aria-modal="true" aria-labelledby="read-aloud-download-title"><h2 id="read-aloud-download-title">{ja?'読み上げモデルをダウンロードしますか？':'Download the read-aloud model?'}</h2><p>{ja?'このモデルをデバイスに保存し、回答をローカルで音声にします。音声入力と同じモデルを共用します。':'Store this model on your device to synthesize answers locally. Voice input shares the same model.'}</p><code>{prompt.model}</code><p className="muted">{ja?'初回のみ、約3.7 GBのダウンロードが必要です。':'A one-time download of about 3.7 GB is required.'}</p>{downloading&&<p role="status">{ja?'ダウンロード中…完了後に読み上げをオンにします。':'Downloading… read aloud will turn on when ready.'}</p>}{error&&<p role="alert">{error}</p>}<footer><button type="button" disabled={downloading} onClick={()=>setPrompt(null)}>{ja?'キャンセル':'Cancel'}</button><button type="button" className="primary" disabled={downloading} onClick={()=>void download()}>{downloading?(ja?'ダウンロード中…':'Downloading…'):(ja?'同意してダウンロード':'Agree and download')}</button></footer></section></div>}
 </div>;
}
