import {useEffect,useState} from 'react';
import type {CodingTask} from './CodingTaskProgress';
import {failureText} from './LiveThinkingLog';

export function CodingModelProgress({task,ja}:{task:CodingTask;ja:boolean}){
  const [now,setNow]=useState(Date.now()/1000);
  const active=['PLANNING','RUNNING','QWEN_REPLANNING','FINAL_REPORTING'].includes(task.status);
  useEffect(()=>{
    if(!active)return;
    const timer=window.setInterval(()=>setNow(Date.now()/1000),1000);
    return()=>window.clearInterval(timer);
  },[task.id,active]);
  const invocation=task.model_invocation,progress=active?task.model_progress:undefined;
  const verification=active?(progress?.verification_kind?progress:invocation?.verification_kind?invocation:undefined):undefined;
  if(!invocation&&!progress)return null;
  const count=(value:number)=>value.toLocaleString(ja?'ja-JP':'en-US');
  let detail='';
  if(progress?.state==='slot_waiting')detail=ja?'推論の実行枠を待っています':'Waiting for an inference slot';
  else if(progress?.state==='fallback')detail=ja?`${progress.model||''} に切替中（${failureText(progress.reason,ja)}）`:`Switching to ${progress.model||''} (${failureText(progress.reason,ja)})`;
  else if(progress?.state==='worker_progress'){
    const event=progress.event||'';
    if(event==='structured_no_thinking_retry')detail=ja
      ?`${failureText(progress.failure,ja)} 同じモデルを思考なしで再試行中`
      :`${failureText(progress.failure,ja)} Retrying the same model without thinking`;
    else if(event.includes('model_load_started'))detail=ja?'モデルを読み込み中':'Loading model';
    else if(event.includes('prefill_progress'))detail=ja?`入力を処理中 (${count(progress.processed_tokens||0)} / ${count(progress.prompt_tokens||0)} tokens)`:`Reading input (${count(progress.processed_tokens||0)} / ${count(progress.prompt_tokens||0)} tokens)`;
    else if(event.includes('generation_progress')){
      detail=progress.channel==='reasoning'?(ja?'推論中':'Thinking'):(ja?'生成中':'Generating');
      if(typeof progress.generated_tokens==='number')detail+=` · ${count(progress.generated_tokens)} tokens`;
      else if(progress.reasoning_chars||progress.output_chars)detail+=ja?` · ${count((progress.reasoning_chars||0)+(progress.output_chars||0))} 文字受信`:` · ${count((progress.reasoning_chars||0)+(progress.output_chars||0))} characters received`;
    }
    else detail=ja?'モデル処理を実行中':'Model processing';
  }
  if(!detail&&invocation?.state==='request_failed')detail=`${ja?'呼び出し失敗':'Request failed'} (${failureText(invocation.failure,ja)})`;
  else if(!detail&&invocation?.state==='request_cancelled')detail=`${ja?'呼び出しを停止':'Request stopped'} (${failureText(invocation.failure,ja)})`;
  else if(!detail&&active&&invocation?.state==='request_started')detail=ja?'モデルの応答を待っています':'Waiting for the model response';
  if(verification){
    const kind=verification.verification_kind==='review'?(ja?'検証チェックを独立評価':'Independently reviewing checks'):(ja?'検証チェックを作成':'Authoring verification checks');
    const counter=`${verification.attempt||0}/${verification.max_attempts||0} · ${ja?'総呼出し':'total calls'} ${verification.model_calls||0}/${verification.max_model_calls||0}`;
    detail=[kind,counter,detail].filter(Boolean).join(' · ');
  }
  const last=progress?.created_at??invocation?.created_at;
  const age=last==null?0:Math.max(0,Math.floor(now-last));
  return <p className="coding-model-progress" aria-label={ja?'モデルの進行状況':'Model progress'}>
    {invocation&&<>{ja?'使用モデル: ':'Model: '}{invocation.model} · {['mlx','ollama'].includes(invocation.runtime)?(ja?'ローカル':'Local'):'API'}</>}
    {detail&&<>{invocation?' · ':''}{detail}</>}
    {active&&age>=15&&<> · {ja?`最終更新から ${age} 秒`:`Last update ${age}s ago`}</>}
  </p>;
}
