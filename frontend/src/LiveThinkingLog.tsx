import {useEffect,useState} from 'react';
import {api} from './api';
import {useLanguage} from './language';
import {ReasoningLog,type ReasoningLogBlock} from './ReasoningLog';
type Event={state:string;invocation_id?:string;text?:string;kind?:'trace'|'summary';model?:string;runtime?:string;reason?:string;failure?:string;complete?:boolean;truncated?:boolean;at:number;event?:string;generated_tokens?:number|null;output_chars?:number;reasoning_chars?:number;processed_tokens?:number;prompt_tokens?:number;account?:number;retry_at?:number|null;http_status?:number;provider_code?:string;parameter?:string;verification_kind?:string;attempt?:number;max_attempts?:number;model_calls?:number;max_model_calls?:number;criteria_count?:number};
export function failureText(reason:string|undefined,ja:boolean){
 const known:Record<string,string[]>={reasoning_budget:['推論の出力枠を使い切る前に停止しました。実行用JSONはまだ生成されていません','Stopped at the reasoning budget before executable JSON was returned'],output_limit:['出力上限に達し、回答または実行用JSONを生成できませんでした','Output limit reached before an answer or executable JSON was returned'],reasoning_loop:['思考の同一文が反復したため停止。保存済みの作業は保持しています','Stopped repeated reasoning; saved work is retained'],cancelled:['モデル呼び出しを停止しました。保存済みの作業は保持しています','Model request was stopped. Saved work is retained'],rate_limited:['利用上限に達しました','Rate limit or quota exceeded'],access_denied:['アクセス権限がありません','Access denied'],model_or_endpoint_unavailable:['モデルまたは接続先が見つかりません','Model or endpoint not found'],credential_required:['認証情報が必要','Credentials required'],reauth_required:['再認証が必要','Sign in again'],model_not_configured:['接続設定にモデルが登録されていません','Model is not configured'],local_runtime_missing:['ローカル実行環境がありません','Local runtime is missing'],local_model_missing:['ローカルモデルがインストールされていません','Local model is not installed'],local_service_unavailable:['ローカル推論サーバーに接続できません','Local inference server is unavailable'],recent_connection_failure:['直前の通信失敗を受け、再試行まで待機中','Waiting briefly after a connection failure'],timeout:['通信待ち時間を超過','Connection/read timed out'],credential_expired:['再認証が必要','Sign in again'],provider_unavailable:['接続または応答の取得に失敗','Could not connect or receive a response'],provider_accounts_exhausted:['接続済みアカウントで利用できませんでした','Connected accounts could not complete the request'],invalid_response:['有効な応答を受信できませんでした','No valid response received'],provider_rejected:['プロバイダーが要求を拒否','Provider rejected the request'],model_concurrency_limit:['同時実行数の上限','Concurrent request limit'],external_access_disabled:['外部アクセスがオフ','External access is off']};
 return known[reason||'']?.[ja?0:1]||reason||(ja?'不明':'Unknown');
}

function statusText(event:Event,ja:boolean){
 const model=event.model||'';
 const diagnostic=[event.http_status?`HTTP ${event.http_status}`:'',event.provider_code,event.parameter].filter(Boolean).join(' · ');
 const detail=diagnostic?` [${diagnostic}]`:'';
 if(event.state==='schema_compatibility'){
  if(event.reason==='schema_unsupported_by_native_mode')return ja
   ?'このタスクのJSON形式は接続先のネイティブ制約に合わないため、同じモデルへJSON指示形式で送信します。'
   :'This task’s JSON schema is outside the endpoint’s native constraints; sending it to the same model as JSON instructions.';
  if(event.reason==='provider_rejected_schema')return ja
   ?`接続先が構造化出力を拒否しました${diagnostic?`（${diagnostic}）`:''}。JSON指示形式に切り替えて同じモデルを再試行します。`
   :`The endpoint rejected structured output${diagnostic?` (${diagnostic})`:''}. Retrying the same model with JSON instructions.`;
  return ja?'出力形式を互換モードに切り替えます。':'Switching to a compatible output format.';
 }
 if(event.state==='model_skipped'||event.state==='account_skipped'){
  const retry=event.retry_at?new Date(event.retry_at*1000).toLocaleString(ja?'ja-JP':'en-US'):null;
  return ja?`${model}${event.account?`（アカウント${event.account}）`:''} を呼び出さずスキップしました。理由: ${failureText(event.reason,ja)}${retry?`。再試行可能時刻: ${retry}`:''}`:`Skipped ${model}${event.account?` (account ${event.account})`:''} before invocation: ${failureText(event.reason,ja)}${retry?`. Retry after ${retry}`:''}`;
 }
 if(event.state==='slot_waiting')return ja?'推論の実行枠を確認しています。空きがない場合は先行処理の終了を待ちます。':'Checking the inference slot; waiting for earlier work if it is occupied.';
 if(event.state==='slot_acquired')return ja?'推論の実行枠を取得しました。':'Inference slot acquired.';
 if(event.state==='request_started')return ja?`${model} を呼び出しました（${['mlx','ollama'].includes(event.runtime||'')?'ローカル':'API'}）。応答を待っています。`:`Called ${model} (${['mlx','ollama'].includes(event.runtime||'')?'local':'API'}). Waiting for a response.`;
 if(event.state==='request_completed')return ja?`${model} の応答を受信しました。`:`Received the response from ${model}.`;
 if(event.state==='account_attempt')return ja?`${model}: アカウント${event.account}で接続中。`:`${model}: connecting with account ${event.account}.`;
 if(event.state==='account_failed')return ja?`${model}: アカウント${event.account}で失敗（${failureText(event.failure,ja)}${detail}）。`:`${model}: account ${event.account} failed (${failureText(event.failure,ja)}${detail}).`;
 if(event.state==='request_failed')return ja?`${model} の呼び出しに失敗しました。理由: ${failureText(event.failure,ja)}`:`${model} request failed. Reason: ${failureText(event.failure,ja)}`;
 if(event.state==='request_cancelled')return ja?`${model} の呼び出しを停止しました。${failureText(event.failure,ja)}`:`${model} request stopped. ${failureText(event.failure,ja)}`;
 if(event.state==='fallback')return ja?`フォールバック: ${model} に切り替えます。理由: ${failureText(event.reason,ja)}`:`Fallback: switching to ${model}. Reason: ${failureText(event.reason,ja)}`;
 if(event.state==='worker_progress'){
  const key=event.event||'';
  if(key==='structured_no_thinking_retry')return ja
   ?`${model||'ローカルモデル'} の構造化出力が失敗しました（${failureText(event.failure,ja)}）。同じモデルを思考なしで再試行しています。`
   :`${model||'Local model'} structured output failed (${failureText(event.failure,ja)}). Retrying the same model without thinking.`;
  const label=key.includes('verification_author_progress')?(ja?`検証チェック作成 ${event.attempt||0}/${event.max_attempts||0}回 · 総呼出し ${event.model_calls||0}/${event.max_model_calls||0}`:`Verification check authoring ${event.attempt||0}/${event.max_attempts||0} · total calls ${event.model_calls||0}/${event.max_model_calls||0}`):key.includes('verification_review_progress')?(ja?`検証チェック評価 ${event.attempt||0}/${event.max_attempts||0}回 · 総呼出し ${event.model_calls||0}/${event.max_model_calls||0}`:`Verification check review ${event.attempt||0}/${event.max_attempts||0} · total calls ${event.model_calls||0}/${event.max_model_calls||0}`):key.includes('model_load_started')?(ja?'モデルを読み込み中':'Loading model'):key.includes('model_load_completed')?(ja?'モデルの読み込み完了':'Model loaded'):key.includes('prompt_ready')?(ja?'入力の準備完了':'Input ready'):key.includes('generation_started')?(ja?'生成を開始':'Generation started'):key.includes('generation_completed')?(ja?'生成完了':'Generation completed'):key.includes('generation_progress')?(ja?'生成中':'Generating'):key.includes('prefill_progress')?(ja?'入力を処理中':'Processing input'):key==='structured_generation_enabled'?(ja?'構造化出力を準備':'Preparing structured output'):key;
  const detail=typeof event.generated_tokens==='number'?` (${event.generated_tokens} tokens)`:typeof event.output_chars==='number'?` (${event.output_chars+(event.reasoning_chars||0)} ${ja?'文字受信':'characters received'})`:'';
  return key.includes('verification_')?`${label}${detail}`:`${ja?'ローカルモデル':'Local model'}: ${label}${detail}`;
 }
 return event.text||event.state;
}
export function liveLogBlock(events:Event[],ja:boolean,active:boolean,now:number):ReasoningLogBlock{
 const compact:Event[]=[];
 for(const event of events){const last=compact.at(-1);if(event.state==='worker_progress'&&last?.state==='worker_progress'&&event.invocation_id===last.invocation_id&&event.event===last.event)compact[compact.length-1]=event;else compact.push(event)}
 events=compact;
 const models=new Map(events.filter(e=>['request_started','account_attempt'].includes(e.state)).map(e=>[e.invocation_id,e.model]));
 const last=events.reduce<Event|undefined>((latest,event)=>!latest||event.at>=latest.at?event:latest,undefined);
 const logs=events.map(event=>{let text=event.state==='reasoning'?event.text||'':statusText(event,ja);if(event.state!=='reasoning'&&event.verification_kind&&!String(event.event||'').startsWith('verification_'))text=`${ja?(event.verification_kind==='review'?'検証チェック評価':'検証チェック作成'):(event.verification_kind==='review'?'Verification review':'Verification authoring')} ${event.attempt||0}/${event.max_attempts||0} · ${ja?'総呼出し':'total calls'} ${event.model_calls||0}/${event.max_model_calls||0} · ${text}`;return{text,kind:event.state==='reasoning'?event.kind||'trace':'status' as const,model:event.model||models.get(event.invocation_id),complete:event.state==='reasoning'?Boolean(event.complete):true,truncated:Boolean(event.truncated)}});
 if(active&&last&&!['request_completed','request_failed','request_cancelled','model_skipped','account_skipped'].includes(last.state))logs.push({text:`${ja?'最終ログから':'Since last log'} ${Math.max(0,Math.floor(now-last.at))}${ja?'秒。応答を待っています。':'s. Waiting for a response.'}`,kind:'status',model:models.get(last.invocation_id),complete:true,truncated:false});
 return {type:'reasoning_log',logs};
}
export function LiveThinkingLog({conversationId,active}:{conversationId?:string|null;active:boolean}){
 const {language}=useLanguage(),ja=language==='ja';
 const [events,setEvents]=useState<Event[]>([]),[now,setNow]=useState(Date.now()/1000);
 useEffect(()=>{setEvents([]);if(!conversationId)return;let live=true,busy=false;
  const refresh=async()=>{if(busy)return;busy=true;try{const result=await api(`/conversations/${encodeURIComponent(conversationId)}/model-logs`);if(live)setEvents(result.events||[])}catch{/* Progress must not interrupt generation. */}finally{busy=false}};
  void refresh();const timer=window.setInterval(()=>{setNow(Date.now()/1000);void refresh()},active?500:3000);
  return()=>{live=false;window.clearInterval(timer)};
 },[conversationId,active]);
 if(!events.length)return null;
 return <article className="assistant live-thinking-message"><ReasoningLog block={liveLogBlock(events,ja,active,now)} streaming={active}/></article>;
}
