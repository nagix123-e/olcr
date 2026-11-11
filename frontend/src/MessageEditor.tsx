import type {ReactNode} from 'react';
import {UiIcon} from './UiIcon';
import './message-editor.css';

export function editableMessageIds(messages:{id?:string;role:string}[]){
  return new Set(messages.filter(message=>message.role==='user').slice(-2).map(message=>message.id).filter(Boolean));
}
export function MessageEditor({children,disabled,ja,onEdit}:{children:ReactNode;disabled:boolean;ja:boolean;onEdit:()=>void}){
  return <>{children}<button type="button" className="message-edit-button" aria-label={ja?'メッセージを編集':'Edit message'} title={disabled?(ja?'処理を停止してから編集できます':'Stop the current run before editing'):(ja?'入力欄でメッセージを編集':'Edit message in composer')} disabled={disabled} onClick={onEdit}><UiIcon name="edit" size={16}/></button></>;
}
