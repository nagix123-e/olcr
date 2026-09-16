let sessionToken='';
let latestCodingTaskId:string|null=null;
let activeStreamTaskId:string|null=null;
let streamCancelRequested=false;
const codingTaskListeners=new Set<(taskId:string)=>void>();
export function subscribeCodingTask(listener:(taskId:string)=>void){codingTaskListeners.add(listener);if(latestCodingTaskId)listener(latestCodingTaskId);return()=>{codingTaskListeners.delete(listener)}}
export let API=import.meta.env.VITE_OLCR_API||'';
export function configureApi(url:string,token:string){API=url;sessionToken=token}
export function hasSession(){return Boolean(sessionToken)}
export async function api<T=any>(path:string,init:RequestInit={}):Promise<T>{
 let response:Response;
 try{response=await fetch(API+path,{...init,cache:'no-store',headers:{'Content-Type':'application/json',...(sessionToken?{'X-OLCR-Session':sessionToken}:{}),...init.headers}})}
 catch(e){throw Error(`NETWORK_ERROR: ${path} (${e instanceof Error?e.name:'unknown'})`)}
 const text=await response.text();let data;
 try{data=JSON.parse(text)}catch{throw Error(`RESPONSE_INVALID: ${path} HTTP ${response.status}`)}
 if(!response.ok)throw Error(`HTTP_${response.status}: ${typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||'Request failed')}`);
 if(path==='/chat'&&typeof data?.coding_task_id==='string'){latestCodingTaskId=data.coding_task_id;codingTaskListeners.forEach(listener=>listener(latestCodingTaskId!))}
 return data;
}

export type StreamEvent={type:string;task_id?:string;conversation_id?:string;text?:string;message?:string;task?:any};

/** Stream ordinary Brain responses so the shared stop button can cancel them. */
export async function streamChat(payload:unknown,onEvent:(event:StreamEvent)=>void):Promise<void>{
 streamCancelRequested=false;
 let response:Response;
 try{response=await fetch(API+'/chat/stream',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json',...(sessionToken?{'X-OLCR-Session':sessionToken}:{} )},body:JSON.stringify(payload)})}
 catch(e){throw Error(`NETWORK_ERROR: /chat/stream (${e instanceof Error?e.name:'unknown'})`)}
 if(!response.ok){const body=await response.text();throw Error(`HTTP_${response.status}: ${body}`)}
 if(!response.body)throw Error('STREAM_UNAVAILABLE');
 const reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';
 const consume=(chunk:string)=>{buffer+=chunk;const frames=buffer.split('\n\n');buffer=frames.pop()||'';for(const frame of frames){const line=frame.split('\n').find(x=>x.startsWith('data:'));if(!line)continue;try{const event=JSON.parse(line.slice(5).trim()) as StreamEvent;if(event.type==='meta'&&event.task_id){activeStreamTaskId=event.task_id;if(streamCancelRequested)void api(`/tasks/${event.task_id}/cancel`,{method:'POST'});}onEvent(event)}catch{/* ignore malformed keep-alive frames */}}};
 try{while(true){const part=await reader.read();if(part.done)break;consume(decoder.decode(part.value,{stream:true}))}consume(decoder.decode())}finally{reader.releaseLock();activeStreamTaskId=null;streamCancelRequested=false}
}

export async function cancelActiveStream(){streamCancelRequested=true;if(!activeStreamTaskId)return;await api(`/tasks/${activeStreamTaskId}/cancel`,{method:'POST'})}
