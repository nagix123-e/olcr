import {useState} from 'react';
import {useLanguage} from './language';
import {saveTextFile} from './api';
import {UiIcon} from './UiIcon';
import './text-file-block.css';

export type TextFileBlockData={type:'text_file';filename:string;content:string;mime_type?:string};

export function TextFileBlock({block}:{block:TextFileBlockData}){
 const {language}=useLanguage(),ja=language==='ja';
 const [busy,setBusy]=useState(false),[saved,setSaved]=useState(false),[error,setError]=useState('');
 const save=async()=>{
  if(busy||typeof block.content!=='string')return;
  setBusy(true);setSaved(false);setError('');
  try{await saveTextFile(block.content,block.filename||'translation.txt');setSaved(true)}
  catch(reason){const message=reason instanceof Error?reason.message:String(reason);if(message!=='EXPORT_CANCELLED')setError(message)}
  finally{setBusy(false)}
 };
 return <section className="text-file-block" aria-label={ja?'テキストファイル':'Text file'}>
  <span className="text-file-name"><UiIcon name="files" size={16}/>{block.filename||'translation.txt'}</span>
  <button type="button" disabled={busy} onClick={()=>void save()}><UiIcon name="download" size={15}/>{busy?(ja?'保存中…':'Saving…'):(ja?'TXTを保存':'Save TXT')}</button>
  {saved&&<small role="status">{ja?'テキストファイルを保存しました。':'Text file saved.'}</small>}
  {error&&<small role="alert">{error}</small>}
 </section>;
}
