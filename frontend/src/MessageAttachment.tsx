import {useEffect,useState} from 'react';
import {api} from './api';
import {UiIcon} from './UiIcon';
import './message-attachment.css';
export type MessageAttachmentData={name:string;mime_type?:string;preview_id?:string;preview_expires_at?:number;preview_data_url?:string};
export function MessageAttachment({value}:{value:MessageAttachmentData}){
 const {preview_id:id,preview_expires_at:expires=0,preview_data_url:local}=value;
 const [url,setUrl]=useState(''),[expired,setExpired]=useState(expires<=Date.now()/1000);
 useEffect(()=>{
  let live=true;setUrl('');setExpired(expires<=Date.now()/1000);
  const delay=expires*1000-Date.now();if(delay<=0)return;
  const timer=window.setTimeout(()=>{setExpired(true);setUrl('')},delay);
  if(local?.startsWith('data:image/'))setUrl(local);
  else if(id)void api(`/message-image-previews/${encodeURIComponent(id)}`).then(result=>{if(live&&expires>Date.now()/1000&&result.expires_at>Date.now()/1000&&/^data:image\/(png|jpeg|webp);base64,/.test(result.data_url))setUrl(result.data_url)}).catch(()=>{});
  return()=>{live=false;window.clearTimeout(timer)};
 },[id,expires,local]);
 return !expired&&url?<figure className="message-image-preview"><img src={url} alt={value.name} onError={()=>setUrl('')}/><figcaption>{value.name}</figcaption></figure>:<div className="message-attachment" title={value.name}><UiIcon name="paperclip" size={14}/>{value.name}</div>;
}
