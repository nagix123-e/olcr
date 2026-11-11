import {useEffect,useRef,useState} from 'react';
import {streamReadAloud} from './api';
import type {Language} from './language';

export const READ_ALOUD_CHANGED='olcr-read-aloud-changed';
export const readAloudEnabled=()=>localStorage.getItem('olcr-read-aloud')==='true';
export function setReadAloudEnabled(enabled:boolean){const changed=enabled!==readAloudEnabled();localStorage.setItem('olcr-read-aloud',String(enabled));if(changed)window.dispatchEvent(new Event(READ_ALOUD_CHANGED))}
type Message={role:string;content:string};
type Status={active:boolean;preparing:boolean;error:boolean;errorCode?:string};

/** Remove visual Markdown from speech without ever reading fenced code. */
function spokenText(text:string){return text.replace(/```[\s\S]*?(?:```|$)/g,'').replace(/!\[[^\]]*\]\([^)]*\)/g,'').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/https?:\/\/\S+/g,'').replace(/(^|\n)\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+)/g,'$1').replace(/[*_`~|]/g,'').trim()}

class ReadAloudPlayer{
 owner='';language:Language='ja';conversationId?:string|null;private context:AudioContext|null=null;private resumePromise:Promise<void>|null=null;private nextTime=0;private sources=new Set<AudioBufferSourceNode>();private abort=new AbortController();private queue:string[]=[];private working=false;private epoch=0;
 constructor(private status:(value:Status)=>void,conversationId?:string|null){this.conversationId=conversationId}
 prime(){if(!readAloudEnabled())return;try{this.context??=new AudioContext();if(this.context.state!=='running'){this.resumePromise=this.context.resume().catch(()=>{})}}catch{this.fail()}}
 stop=()=>{this.epoch++;this.abort.abort();this.abort=new AbortController();this.queue=[];this.working=false;for(const source of this.sources){source.onended=null;try{source.stop()}catch{}}this.sources.clear();this.nextTime=0;this.owner='';this.status({active:false,preparing:false,error:false})};
 private fail(errorCode='AUDIO_SYNTHESIS_FAILED'){this.stop();this.status({active:false,preparing:false,error:true,errorCode})}
 scope(key:string,language:Language,conversationId?:string|null){if((this.owner&&this.owner!==key)||this.language!==language||this.conversationId!==conversationId)this.stop();this.language=language;this.conversationId=conversationId}
 private async resumeOutput(){
  const context=this.context;if(!context||context.state==='closed')throw Error('AUDIO_CONTEXT_UNAVAILABLE');
  if(context.state!=='running'){
   try{await (this.resumePromise||context.resume())}catch{}
   if((context.state as string)!=='running'){this.resumePromise=null;await context.resume()}
  }
  if(context.state!=='running')throw Error('AUDIO_PLAYBACK_BLOCKED');
  this.resumePromise=null;
 }
 private play(pcm:string,rate:number){
  const context=this.context;if(!context||context.state!=='running')throw Error('AUDIO_PLAYBACK_BLOCKED');
  const binary=atob(pcm),bytes=new Uint8Array(binary.length);for(let i=0;i<binary.length;i++)bytes[i]=binary.charCodeAt(i);
  if(!Number.isInteger(rate)||rate<=0||bytes.length<2||bytes.length%2)throw Error('AUDIO_PCM_INVALID');
  const view=new DataView(bytes.buffer),buffer=context.createBuffer(1,bytes.length/2,rate),samples=buffer.getChannelData(0);for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(i*2,true)/32768;
  const source=context.createBufferSource();source.buffer=buffer;source.connect(context.destination);this.sources.add(source);source.onended=()=>{source.disconnect();this.sources.delete(source);if(!this.working&&!this.queue.length&&!this.sources.size)this.status({active:false,preparing:false,error:false})};
  const start=Math.max(context.currentTime+0.03,this.nextTime);source.start(start);this.nextTime=start+buffer.duration;
 }
 private async drain(){
  if(this.working)return;this.working=true;const epoch=this.epoch,signal=this.abort.signal;
  let watchdog:number|undefined;
  const armWatchdog=()=>{if(watchdog!==undefined)window.clearTimeout(watchdog);watchdog=window.setTimeout(()=>{if(epoch===this.epoch)this.fail('AUDIO_SYNTHESIS_TIMEOUT')},90_000)};
  try{
   armWatchdog();
   await this.resumeOutput();
   while(this.queue.length&&epoch===this.epoch){
    const text=this.queue.shift()!;let chunks=0;armWatchdog();
    await streamReadAloud(text,this.language,signal,async(pcm,rate)=>{
     if(epoch!==this.epoch)return;
     await this.resumeOutput();if(epoch!==this.epoch)return;
     this.play(pcm,rate);chunks++;this.status({active:true,preparing:false,error:false});armWatchdog();
    },this.conversationId);
    if(epoch===this.epoch&&!chunks)throw Error('AUDIO_SYNTHESIS_EMPTY');
    if(watchdog!==undefined){window.clearTimeout(watchdog);watchdog=undefined}
   }
  }
  catch(error){if(epoch===this.epoch&&!signal.aborted)this.fail(error instanceof Error?error.message:'AUDIO_SYNTHESIS_FAILED')}
  finally{if(watchdog!==undefined)window.clearTimeout(watchdog);if(epoch===this.epoch){this.working=false;if(!this.sources.size)this.status({active:false,preparing:false,error:false})}}
 }
 begin(key:string,baseline:number){
  this.stop();if(!readAloudEnabled())return {update:(_messages:Message[])=>{},finish:()=>{}};
  this.owner=key;const epoch=this.epoch;let index=baseline,consumed=0,latest='';
  const enqueue=(text:string)=>{const clean=spokenText(text);if(clean){this.queue.push(clean);this.status({active:true,preparing:true,error:false});void this.drain()}};
  const flush=(final:boolean)=>{
   while(consumed<latest.length){
    const remaining=latest.slice(consumed),fence=remaining.indexOf('```');
    if(fence===0){const end=remaining.indexOf('```',3);if(end<0){if(final)consumed=latest.length;break}consumed+=end+3;continue}
    let limit=fence<0?remaining.length:fence;
    const boundary=remaining.slice(0,Math.min(limit,160)).match(/[。！？!?\n]|\.(?=\s|$)/);
    if(boundary)limit=boundary.index!+1;
    else if(limit>160){const prefix=remaining.slice(0,160),space=prefix.lastIndexOf(' ');limit=space>80?space+1:160}
    else if(!final&&fence<0)break;
    enqueue(remaining.slice(0,limit));consumed+=limit;
   }
  };
  return {
   update:(messages:Message[])=>{if(epoch!==this.epoch)return;for(let i=index;i<messages.length;i++){const message=messages[i];if(message.role!=='assistant')continue;if(i!==index){flush(true);consumed=0;latest='';index=i}if(!message.content.startsWith(latest)&&consumed>0){/* Never repeat already spoken text after a server revision. */latest=message.content;consumed=Math.min(consumed,latest.length)}else latest=message.content;flush(false)}},
   finish:()=>{if(epoch===this.epoch)flush(true)}
  };
 }
 dispose(){this.stop();void this.context?.close();this.context=null}
}

export function useReadAloud(key:string,language:Language,conversationId?:string|null){
 const [status,setStatus]=useState<Status>({active:false,preparing:false,error:false});const ref=useRef<ReadAloudPlayer|null>(null);if(!ref.current)ref.current=new ReadAloudPlayer(setStatus,conversationId);const player=ref.current;
 useEffect(()=>{player.scope(key,language,conversationId)},[key,language,conversationId,player]);
 useEffect(()=>{const stop=()=>player.stop();window.addEventListener(READ_ALOUD_CHANGED,stop);window.addEventListener('olcr-voice-input-start',stop);return()=>{window.removeEventListener(READ_ALOUD_CHANGED,stop);window.removeEventListener('olcr-voice-input-start',stop);player.dispose()}},[player]);
 return {player,...status};
}
