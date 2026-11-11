import { native } from "./native";

let sessionToken='';
const latestCodingTasks=new Map<string,string>();
type ActiveGeneration={id:string;conversationId?:string;taskId:string|null;cancelRequested:boolean;controller:AbortController;stopped:Promise<never>;stop:()=>void};
const activeStreams=new Map<string,ActiveGeneration>();
function beginGeneration(conversationId?:string){
 const id=crypto.randomUUID(),key=conversationId||id;
 if(activeStreams.has(key))throw Error('CONVERSATION_BUSY');
 let rejectStop!:(error:Error)=>void;
 const state:ActiveGeneration={id,conversationId,taskId:null,cancelRequested:false,controller:new AbortController(),stopped:new Promise<never>((_,reject)=>{rejectStop=reject}),stop:()=>{
  if(state.cancelRequested)return;state.cancelRequested=true;state.controller.abort();rejectStop(new DOMException('Generation stopped','AbortError'));
 }};
 activeStreams.set(key,state);
 return {key,state,finish:()=>{if(activeStreams.get(key)===state)activeStreams.delete(key)}};
}
const codingTaskListeners=new Set<{listener:(taskId:string)=>void;conversationId?:string|null}>();
function publishCodingTask(taskId:string,conversationId?:string|null){if(conversationId)latestCodingTasks.set(conversationId,taskId);codingTaskListeners.forEach(({listener,conversationId:owner})=>{if(owner===undefined||owner===conversationId)listener(taskId)})}
export function subscribeCodingTask(listener:(taskId:string)=>void,conversationId?:string|null){const entry={listener,conversationId};codingTaskListeners.add(entry);const latest=conversationId?latestCodingTasks.get(conversationId):null;if(latest)listener(latest);return()=>{codingTaskListeners.delete(entry)}}
export let API=import.meta.env.VITE_OLCR_API||'';
export function configureApi(url:string,token:string){API=url;sessionToken=token}
export function hasSession(){return Boolean(sessionToken)}
function responseLanguageHeaders(){
 const language=typeof localStorage!=="undefined"&&localStorage.getItem("olcr-language")==="en"?"en":"ja";
 return {"X-OLCR-Language":language};
}
/** Approval is requested before dispatch; retries reuse the exact request body. */
export async function permissionFetch(url:string,init:RequestInit):Promise<Response>{
 const response=await fetch(url,init);
 if(response.status!==409)return response;
 let detail:any;
 try{detail=(await response.clone().json()).detail}catch{return response}
 if(detail?.code!=="PERMISSION_CONFIRMATION_REQUIRED"||typeof detail.approval_id!=="string")return response;
 const ja=typeof localStorage==="undefined"||localStorage.getItem("olcr-language")!=="en";
 const approved=window.confirm(ja?detail.summary:"Allow this OLCR request? It may access the internet or modify files.");
 const headers=new Headers(init.headers);
 const result=await fetch(API+"/permissions/approvals/"+encodeURIComponent(detail.approval_id),{
  method:"POST",headers,body:JSON.stringify({approve:approved}),signal:init.signal,
 });
 if(!result.ok)throw Error("PERMISSION_APPROVAL_EXPIRED");
 if(!approved)throw Error(ja?"操作をキャンセルしました。":"Operation cancelled.");
 headers.set("X-OLCR-Approval",detail.approval_id);
 return fetch(url,{...init,headers});
}

export async function api<T=any>(path:string,init:RequestInit={}):Promise<T>{
 if(!API)throw Error('BACKEND_CONFIG_UNAVAILABLE');
 let payload:any;
 if(init.method==='POST'&&(path==='/chat'||path==='/commands')){try{payload=JSON.parse(String(init.body))}catch{}}
 const active=payload?beginGeneration(payload.conversation_id||undefined):null;
 try{
 let response:Response|undefined;let lastError:unknown;
 const request={...init,...(active?{signal:init.signal?AbortSignal.any([init.signal,active.state.controller.signal]):active.state.controller.signal}:{}),cache:'no-store' as RequestCache,headers:{...responseLanguageHeaders(),'Content-Type':'application/json',...(sessionToken?{'X-OLCR-Session':sessionToken}:{}),...(active?{'X-OLCR-Generation':active.state.id}:{}),...init.headers}};
 for(let attempt=0;attempt<40;attempt++){
  try{response=await (active?Promise.race([permissionFetch(API+path,request),active.state.stopped]):permissionFetch(API+path,request));break}
  catch(e){lastError=e;if(path!=='/health'||attempt===39)throw Error(`NETWORK_ERROR: ${path} (${e instanceof Error?e.name:'unknown'})`);await new Promise(resolve=>window.setTimeout(resolve,250))}
 }
 if(!response)throw Error(`NETWORK_ERROR: ${path} (${lastError instanceof Error?lastError.name:'unknown'})`)
 const text=await (active?Promise.race([response.text(),active.state.stopped]):response.text());let data;
 try{data=JSON.parse(text)}catch{throw Error(`RESPONSE_INVALID: ${path} HTTP ${response.status}`)}
 if(!response.ok)throw Error(`HTTP_${response.status}: ${typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||'Request failed')}`);
 if((path==='/chat'||path==='/commands')&&typeof data?.coding_task_id==='string')publishCodingTask(data.coding_task_id,data.conversation_id)
 return data;
 }catch(error){
  if(active?.state.cancelRequested)return {cancelled:true,conversation_id:active.state.conversationId,response:'',message:'',data:{},blocks:[]} as T;
  throw error;
 }finally{active?.finish()}
}

/** Transcribe locally recorded PCM audio with OLCR's language-selected model. */
export async function transcribeAudio(audioBase64:string,language:"ja"|"en",conversationId?:string|null):Promise<{text:string;model:string}>{
 return api<{text:string;model:string}>("/audio/transcribe",{method:"POST",body:JSON.stringify({audio_base64:audioBase64,language,...(conversationId?{conversation_id:conversationId}:{})})});
}

/** Each event replaces the recognized prefix for this audio segment. */
export async function streamTranscribeAudio(audioBase64:string,language:"ja"|"en",onText:(text:string)=>void,signal:AbortSignal,handsfreeSession?:string,conversationId?:string|null):Promise<string>{
 const response=await fetch(API+"/audio/transcribe/stream",{method:"POST",signal,headers:{...responseLanguageHeaders(),"Content-Type":"application/json",...(sessionToken?{"X-OLCR-Session":sessionToken}:{})},body:JSON.stringify({audio_base64:audioBase64,language,...(handsfreeSession?{handsfree_session:handsfreeSession}:{}),...(conversationId?{conversation_id:conversationId}:{})})});
 if(!response.ok)throw Error(`HTTP_${response.status}: ${await response.text()}`);
 if(!response.body)throw Error("AUDIO_STREAM_UNAVAILABLE");
 const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="",result="",done=false;
 const consume=(line:string)=>{if(!line.trim())return;const event=JSON.parse(line);if(event.type==="error")throw Error(event.error);if(event.type==="text"||event.type==="done"){result=event.text;onText(result)}if(event.type==="done")done=true;};
 try{while(true){const chunk=await reader.read();buffer+=decoder.decode(chunk.value,{stream:!chunk.done});let boundary;while((boundary=buffer.indexOf("\n"))>=0){consume(buffer.slice(0,boundary));buffer=buffer.slice(boundary+1)}if(chunk.done)break;}consume(buffer);if(!done)throw Error("AUDIO_STREAM_INCOMPLETE");return result;}finally{await reader.cancel();reader.releaseLock();}
}

export async function audioModelStatus(language:"ja"|"en"):Promise<{language:string;model:string;downloaded:boolean}>{
 return api<{language:string;model:string;downloaded:boolean}>(`/audio/model/status?language=${language}`);
}

export async function streamReadAloud(text:string,language:"ja"|"en",signal:AbortSignal,onAudio:(pcm:string,rate:number)=>void|Promise<void>,conversationId?:string|null){
 if(!API)throw Error('BACKEND_CONFIG_UNAVAILABLE');
 const response=await fetch(API+'/audio/synthesize/stream',{method:'POST',signal,headers:{...responseLanguageHeaders(),'Content-Type':'application/json',...(sessionToken?{'X-OLCR-Session':sessionToken}:{})},body:JSON.stringify({text,language,...(conversationId?{conversation_id:conversationId}:{})})});
 if(!response.ok||!response.body)throw Error('AUDIO_SYNTHESIS_UNAVAILABLE');
 const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',done=false;
 const consume=async(line:string)=>{if(!line.trim())return;const event=JSON.parse(line);if(event.type==='error')throw Error(event.error);if(event.type==='audio')await onAudio(event.pcm_base64,event.sample_rate);if(event.type==='done')done=true};
 try{while(true){const chunk=await reader.read();buffer+=decoder.decode(chunk.value,{stream:!chunk.done});let i;while((i=buffer.indexOf('\n'))>=0){await consume(buffer.slice(0,i));buffer=buffer.slice(i+1)}if(chunk.done)break}await consume(buffer);if(!done)throw Error('AUDIO_STREAM_INCOMPLETE')}
 finally{await reader.cancel();reader.releaseLock()}
}

export async function prepareAudioModel(language:"ja"|"en"):Promise<{language:string;model:string;downloaded:boolean}>{
 return api<{language:string;model:string;downloaded:boolean}>("/audio/model/prepare",{method:"POST",body:JSON.stringify({language,confirm_download:true})});
}

export async function downloadApi(path:string,filename:string):Promise<void>{
 const response=await fetch(API+path,{cache:'no-store',headers:sessionToken?{'X-OLCR-Session':sessionToken}:{}});
 if(!response.ok){const body=await response.text();throw Error(`HTTP_${response.status}: ${body.slice(0,500)}`)}
 const objectUrl=URL.createObjectURL(await response.blob());
 const anchor=document.createElement('a');anchor.href=objectUrl;anchor.download=filename;anchor.style.display='none';document.body.appendChild(anchor);anchor.click();anchor.remove();
 window.setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);
}

/** Save a server export as a real local file through the native Save dialog. */
export async function saveExportApi(path:string,filename:string):Promise<void>{
 const bridge=native();
 if(!bridge){await downloadApi(path,filename);return;}
 const response=await fetch(API+path,{cache:'no-store',headers:sessionToken?{'X-OLCR-Session':sessionToken}:{}});
 if(!response.ok){const body=await response.text();throw Error(`HTTP_${response.status}: ${body.slice(0,500)}`)}
 const contents=Array.from(new Uint8Array(await response.arrayBuffer()));
 const saved=await bridge.core.invoke<string|null>('save_export_file',{fileName:filename,contents});
 if(saved===null)throw Error('EXPORT_CANCELLED');
}

export type StreamEvent={type:string;task_id?:string;conversation_id?:string;text?:string;message?:string;block?:any;task?:any;reasoning?:import('./ReasoningLog').ReceivedReasoning;model?:string};

/** Each conversation owns its stream and cancellation, including cancellation before meta. */
export async function streamChat(payload:unknown,onEvent:(event:StreamEvent)=>void):Promise<void>{
 const conversationId=(payload as {conversation_id?:string})?.conversation_id;
 const active=beginGeneration(conversationId),state=active.state;
 let owner=conversationId;
 try{
 let response:Response;
 try{response=await Promise.race([permissionFetch(API+'/chat/stream',{method:'POST',signal:state.controller.signal,cache:'no-store',headers:{...responseLanguageHeaders(),'Content-Type':'application/json','X-OLCR-Generation':state.id,...(sessionToken?{'X-OLCR-Session':sessionToken}:{} )},body:JSON.stringify(payload)}),state.stopped])}
 catch(e){throw Error(`NETWORK_ERROR: /chat/stream (${e instanceof Error?e.name:'unknown'})`)}
 if(!response.ok){const body=await response.text();throw Error(`HTTP_${response.status}: ${body}`)}
 if(!response.body)throw Error('STREAM_UNAVAILABLE');
 const reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';let terminal=false;
 const consume=(chunk:string)=>{
  buffer+=chunk;const frames=buffer.split('\n\n');buffer=frames.pop()||'';
  for(const frame of frames){
   const line=frame.split('\n').find(x=>x.startsWith('data:'));if(!line)continue;
   let event:StreamEvent;
   try{event=JSON.parse(line.slice(5).trim()) as StreamEvent}catch{continue}
   if(event.conversation_id){if(owner&&event.conversation_id!==owner)throw Error('CONVERSATION_RESPONSE_MISMATCH');owner=event.conversation_id;}
   if(state.cancelRequested)return;
   if(event.type==='meta'&&event.task_id)state.taskId=event.task_id;
   if(event.type==='done'&&event.task?.type==='coding_task_progress'&&typeof event.task.task_id==='string')publishCodingTask(event.task.task_id,owner);
   // Completion owns the UI lifecycle even if the server keeps the socket open.
   terminal=['done','cancelled','error'].includes(event.type);
   onEvent(event);
   if(terminal)break;
  }
 };
 try{
  while(!terminal){const part=await Promise.race([reader.read(),state.stopped]);if(part.done)break;consume(decoder.decode(part.value,{stream:true}))}
  if(!terminal){consume(decoder.decode());if(buffer.trim())consume('\n\n')}
  if(!terminal)throw Error('CHAT_STREAM_INCOMPLETE');
 }finally{
  // Do not wait for a provider's network cleanup before enabling the composer.
  void reader.cancel().catch(()=>{});reader.releaseLock();
 }
 }catch(error){if(state.cancelRequested){onEvent({type:'cancelled',conversation_id:owner,task_id:state.taskId||undefined});return}throw error}
 finally{active.finish()}
}

export async function cancelActiveStream(conversationId?:string|null){
 const unowned=[...activeStreams.values()].filter(state=>!state.conversationId);
 const state=conversationId?activeStreams.get(conversationId):unowned.length===1?unowned[0]:undefined;
 if(!state||state.cancelRequested)return;
 state.stop();
 await api(`/generations/${encodeURIComponent(state.id)}/cancel`,{method:'POST'});
}
