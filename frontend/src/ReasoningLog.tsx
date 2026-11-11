import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {useLanguage} from './language';
import './thinking.css';
import {UiIcon} from './UiIcon';

export type ReceivedReasoning = {text:string;kind:'trace'|'summary'|'status';complete:boolean;truncated:boolean;model?:string;stage?:string};
export type ReasoningLogBlock = {type:'reasoning_log';logs:ReceivedReasoning[]};

export function appendReasoning(block:ReasoningLogBlock|undefined,delta:ReceivedReasoning,model?:string):ReasoningLogBlock {
  const logs=[...(block?.logs||[])],previous=logs.at(-1),actualModel=model||delta.model||previous?.model;
  if(previous&&previous.kind===delta.kind&&previous.model===actualModel){
    const combined=previous.text+delta.text;
    logs[logs.length-1]={...delta,model:actualModel,text:combined.slice(0,64000),truncated:Boolean(previous.truncated||delta.truncated||combined.length>64000)};
  }else logs.push({...delta,model:actualModel});
  return {type:'reasoning_log',logs};
}

/** Plain text only: a received model channel is neither HTML nor an action. */
export function ReasoningLog({block,streaming=false}:{block:ReasoningLogBlock;streaming?:boolean}) {
  const {language}=useLanguage(),ja=language==='ja';
  const [open,setOpen]=useState(streaming);
  const root=useRef<HTMLDetailsElement>(null);
  useEffect(()=>{if(streaming)setOpen(true)},[streaming]);
  useLayoutEffect(()=>{
    const log=root.current,viewport=log?.closest<HTMLElement>('.messages');
    if(!streaming||!open||!log||!viewport)return;
    const follow=()=>{
      const overflow=log.getBoundingClientRect().bottom-viewport.getBoundingClientRect().bottom;
      if(overflow>0)viewport.scrollTop+=overflow;
    };
    follow();
    // LiveThinkingLog polls independently of the message list. Observe rendered
    // height so wrapped lines, font changes and new chunks all advance the chat.
    const observer=new ResizeObserver(follow);observer.observe(log);
    return()=>observer.disconnect();
  },[streaming,open,block]);
  const logs=block.logs.filter(log=>typeof log.text==='string'&&log.text.trim());
  if(!logs.length)return null;
  const complete=logs.every(log=>log.complete);
  return <details ref={root} className="reasoning-log" open={open} onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary><UiIcon className="reasoning-chevron" name="chevron" size={14}/>
      <span>{ja?'思考ログ':'Thinking log'}</span>
      <small>{streaming?(ja?'受信中':'Receiving'):complete?(ja?'受信済み':'Received'):(ja?'未完了':'Incomplete')}</small>
    </summary>
    <div className="reasoning-log-body">
      <p className="reasoning-log-note">{ja?'モデルから届いたログと、OLCRの処理状況です。':'Received model logs and OLCR processing status.'}</p>
      {logs.map((log,index)=><section key={index}>
        <div className="reasoning-log-meta"><span>{log.kind==='status'?(ja?'OLCRの処理状況':'OLCR status'):log.kind==='summary'?(ja?'プロバイダーの思考要約':'Provider thinking summary'):(ja?'モデルの思考出力':'Model thinking output')}</span>{log.model&&<span>{log.model}</span>}</div>
        <pre>{log.text}</pre>
        {log.truncated&&<small>{ja?'表示上限に達したため、末尾を省略しました。':'The end was omitted at the display limit.'}</small>}
        {!log.complete&&!streaming&&<small>{ja?'生成が終了する前までのログです。':'Log received before generation finished.'}</small>}
      </section>)}
    </div>
  </details>;
}

/** Preserve received channels on non-stream routes that return task metadata. */
export function responseReasoningBlocks(result:{blocks?:any[];task?:{model_calls?:any[]}}):any[] {
 const blocks=Array.isArray(result.blocks)?result.blocks:[];
 if(blocks.some(block=>block.type==='reasoning_log'))return blocks;
 const logs=(result.task?.model_calls||[]).flatMap(call=>{
  const raw=call.reasoning;
  return raw&&typeof raw.text==='string'&&raw.text.trim()?[{...raw,text:raw.text.slice(0,64000),
   truncated:Boolean(raw.truncated||raw.text.length>64000),model:call.model,stage:call.stage}]:[];
 });
 return logs.length?[...blocks,{type:'reasoning_log',logs}]:blocks;
}
