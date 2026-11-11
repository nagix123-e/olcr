import type {ChatMap} from './chatMapData';
import type {ComposerEdit} from './composerEdit';
import {useRef,useSyncExternalStore,type SetStateAction} from 'react';
import type {Attachment} from './ChatComposer';
import type {CustomAgent} from './CustomAgents';

type Agent=Pick<CustomAgent,'id'|'name'|'icon'|'revision'>;
export type ConversationSession<M>={messages:M[];locationMap?:ChatMap|null;text:string;busy:boolean;error:string;attachment:Attachment|null;agent:Agent|null;progress:string;historyLoading:boolean;editing?:ComposerEdit;agentBuilder?:boolean;agentBuilderRequest?:{id:string;message:string;attachment:Attachment|null|undefined}};
export function conversationSessionKey(projectId:string|null|undefined,conversationId:string|null|undefined){return JSON.stringify([projectId||null,conversationId||null]);}

/** All asynchronous writers retain the session that owned their request. */
export class ConversationSessions<M>{
  private values=new Map<string,ConversationSession<M>>();
  private revisions=new Map<string,number>();
  private listeners=new Set<()=>void>();
  private version=0;
  subscribe=(listener:()=>void)=>{this.listeners.add(listener);return()=>{this.listeners.delete(listener)}};
  snapshot=()=>this.version;
  get(key:string):ConversationSession<M>{
    let value=this.values.get(key);
    if(!value){value={messages:[],text:'',busy:false,error:'',attachment:null,agent:null,progress:'',historyLoading:false};this.values.set(key,value)}
    return value;
  }
  revision(key:string){return this.revisions.get(key)||0}
  update(key:string,patch:Partial<ConversationSession<M>>){
    this.values.set(key,{...this.get(key),...patch});this.revisions.set(key,this.revision(key)+1);this.version++;this.listeners.forEach(listener=>listener());
  }
  setter<K extends keyof ConversationSession<M>>(key:string,field:K){return(next:SetStateAction<ConversationSession<M>[K]>)=>{
    const value=typeof next==='function'?(next as (old:ConversationSession<M>[K])=>ConversationSession<M>[K])(this.get(key)[field]):next;
    this.update(key,{[field]:value} as Pick<ConversationSession<M>,K>);
  }}
  bind(key:string){return {setMs:this.setter(key,'messages'),setText:this.setter(key,'text'),setBusy:this.setter(key,'busy'),setErr:this.setter(key,'error'),setAttachment:this.setter(key,'attachment'),setActiveAgent:this.setter(key,'agent'),setComputerUseProgress:this.setter(key,'progress')}}
  claim(key:string){if(this.get(key).busy)return false;this.update(key,{busy:true,error:''});return true}
  moveDraft(from:string,to:string){this.update(to,{...this.get(from),historyLoading:false});this.update(from,{messages:[],text:'',busy:false,error:'',attachment:null,agent:null,progress:'',historyLoading:false,editing:undefined,agentBuilder:false,agentBuilderRequest:undefined,locationMap:null})}
  acceptHistory(key:string,revision:number,messages:M[]){if(this.get(key).busy||this.revision(key)!==revision)return false;this.update(key,{messages,historyLoading:false});return true}
}
export function useConversationSessions<M>(){
  const ref=useRef<ConversationSessions<M>|null>(null);
  if(!ref.current)ref.current=new ConversationSessions<M>();
  useSyncExternalStore(ref.current.subscribe,ref.current.snapshot);
  return ref.current;
}
