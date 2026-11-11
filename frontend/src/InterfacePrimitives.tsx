import {useEffect, useId, useRef, useState, type ReactNode} from 'react';
import {UiIcon} from './UiIcon';
import {useLanguage} from './language';
import './interface-consistency.css';

export function PageBack({onBack, destination='chat'}:{onBack:()=>void;destination?:'chat'|'settings'}){
 const ja=useLanguage().language==='ja';
 return <button type="button" className={destination==='settings'?'page-back settings-back':'page-back'} onClick={onBack}><UiIcon name="back" size={16}/>{destination==='settings'?(ja?'設定に戻る':'Back to settings'):(ja?'チャットに戻る':'Back to chat')}</button>;
}
export function StatusBadge({state,children,announce=true}:{state:string;children:ReactNode;announce?:boolean}){
 const key=state.toLowerCase();
 const tone=/^(connected|ready|completed|pass|success)$/.test(key)?'success':/failed|error|denied/.test(key)?'error':/running|checking|loading|planning/.test(key)?'busy':/required|waiting|resumable/.test(key)?'attention':'neutral';
 return <span className={`ui-status ui-status-${tone}`} role={announce?'status':undefined}><span aria-hidden="true">{tone==='success'?'✓':tone==='error'?'!':tone==='busy'?'…':tone==='attention'?'!':'—'}</span>{children}</span>;
}
export function Feedback({kind='loading',children,onRetry}:{kind?:'loading'|'empty'|'error';children:ReactNode;onRetry?:()=>void}){
 const ja=useLanguage().language==='ja';
 return <div className={`ui-feedback ui-feedback-${kind}`} role={kind==='error'?'alert':'status'}><div>{kind==='error'&&<strong>{ja?'操作を完了できませんでした。接続や入力内容を確認してください。':'Could not complete the operation. Check your connection and input.'}</strong>}{children}</div>{onRetry&&<button type="button" onClick={onRetry}><UiIcon name="refresh" size={16}/>{ja?'再試行':'Try again'}</button>}</div>;
}
export function SettingsGroup({title,children,open=false}:{title:string;children:ReactNode;open?:boolean}){
 return <details className="settings-disclosure settings-group" open={open}><summary>{title}</summary><div className="settings-group-content">{children}</div></details>;
}
/** Escape and backdrop never silently discard edited fields. Tab stays in the active dialog. */
export function DialogFrame({title,children,onClose,dirty=false,closeDisabled=false,confirmDiscard=true}:{title:ReactNode;children:ReactNode;onClose:()=>void;dirty?:boolean;closeDisabled?:boolean;confirmDiscard?:boolean}){
 const id=useId(), ref=useRef<HTMLDivElement>(null), ja=useLanguage().language==='ja';
 const [touched,setTouched]=useState(false);
 const origin=useRef(document.activeElement as HTMLElement|null);
 const close=()=>{if(closeDisabled)return;if(!confirmDiscard||!(dirty||touched)||window.confirm(ja?'入力した内容を破棄して閉じますか？':'Discard the entered changes and close?'))onClose()};
 const closeRef=useRef(close);closeRef.current=close;
 useEffect(()=>{
  const previous=origin.current;
  const dialog=ref.current;
  const focusable=()=>Array.from(dialog?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]')||[]).filter(item=>item.getClientRects().length>0);
  if(!dialog?.contains(document.activeElement))(focusable()[0]||dialog)?.focus();
  const key=(event:KeyboardEvent)=>{
   if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeRef.current()}
   if(event.key==='Tab'){const items=focusable(),first=items[0],last=items.at(-1);if(!first){event.preventDefault();dialog?.focus()}else if(event.shiftKey&&(document.activeElement===first||document.activeElement===dialog)){event.preventDefault();last?.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}
  };
  dialog?.addEventListener('keydown',key);
  return()=>{dialog?.removeEventListener('keydown',key);if(previous?.isConnected)previous.focus()};
 },[]);
 return <div className="modal ui-dialog-backdrop" onMouseDown={event=>{if(event.target===event.currentTarget)close()}}><div className="ui-dialog" onInputCapture={()=>setTouched(true)} ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby={id}><header className="ui-dialog-heading"><h2 id={id}>{title}</h2><button type="button" className="ui-icon-button" onClick={close} disabled={closeDisabled} aria-label={ja?'閉じる':'Close'}><UiIcon name="close" size={18}/></button></header>{children}</div></div>;
}
