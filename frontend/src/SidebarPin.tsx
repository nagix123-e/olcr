import {UiIcon} from './UiIcon';
import {useLanguage} from './language';

export function pinnedFirst<T extends {pinned?:boolean|number;updated_at?:number;created_at?:number}>(items:T[]):T[]{
  return [...items].sort((a,b)=>Number(Boolean(b.pinned))-Number(Boolean(a.pinned))||(b.updated_at??0)-(a.updated_at??0)||(b.created_at??0)-(a.created_at??0));
}
export function SidebarPin({kind,label,pinned,busy,onToggle}:{kind:'project'|'conversation';label:string;pinned?:boolean|number;busy:boolean;onToggle:()=>void}){
  const {language}=useLanguage(),ja=language==='ja';
  const name=ja?`${kind==='project'?'プロジェクト':'会話'}「${label}」の${pinned?'ピン留めを解除':'ピン留め'}`:`${pinned?'Unpin':'Pin'} ${kind} ${label}`;
  return <button type="button" className="sidebar-pin" aria-label={name} title={name} aria-pressed={Boolean(pinned)} disabled={busy} onClick={onToggle}><UiIcon name="pin" size={16}/></button>;
}
