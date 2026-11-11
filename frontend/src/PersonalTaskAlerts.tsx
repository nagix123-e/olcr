import {useEffect,useState} from 'react';
export function PersonalTaskAlerts({api,ja,onOpen,disabled=false}:{api:(path:string)=>Promise<any>;ja:boolean;onOpen:()=>void;disabled?:boolean}){
 const [count,setCount]=useState(0);
 useEffect(()=>{let live=true;const refresh=()=>void api('/personal/inbox').then(value=>{if(live)setCount((value.items||value.notifications||[]).filter((item:any)=>!item.read_at).length);}).catch(()=>{});refresh();const timer=window.setInterval(refresh,30000);return()=>{live=false;clearInterval(timer);};},[api]);
 return count?<div role="status" className="personal-task-alert"><button type="button" disabled={disabled} onClick={onOpen}>{ja?`タスクからのお知らせ ${count}件 — チャットで確認`:`${count} task updates — view in chat`}</button></div>:null;
}
