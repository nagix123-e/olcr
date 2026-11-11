import {useCallback, useEffect, useState, useRef} from 'react';
import {api} from './api';
import {native} from './native';
import {useLanguage} from './language';
export type BrainRole = 'brain' | 'coder' | 'short_coding' | 'general_inference';
export type BrainConfiguration = {mode:'unified'|'split'; selections:Record<BrainRole,string>; efforts:Record<BrainRole,string|null>;fallback_models:string[];quick_models?:Record<string,boolean>};
export type BrainModel = {id:string;label:string;provider:string;available:boolean;kind:'language'|'vision'|'embedding'|'audio'|'reranker';local:boolean;quick_supported?:boolean;vision_supported?:boolean|null;description?:string;effort_levels?:string[];effort_default?:string|null;usage_url?:string};
export type PurposeModel = {id:string;role:string;purpose:string;purpose_en:string;label:string;provider:string;deployment:string;runtime:string;local_path:string;ready:boolean};
export type BrainProvider = {account_id?:number;account_name?:string;rank?:number;accounts?:BrainProvider[];account_order?:number[];id:string;label:string;configured:boolean;auth_type:'api_key'|'oauth';base_url?:string;model_ids?:string[];credential_pending?:boolean;account_hint?:string;availability_message?:string;connection_state?:'connected'|'disconnected'|'reauth_required'};
export type BrainCatalog = {models:BrainModel[];providers:BrainProvider[];purpose_models?:PurposeModel[]};
export type BrainModelUsage = {input_tokens:number;output_tokens:number;total_tokens:number;window_start:number;window_kind:'weekly';last_used:number|null;limit_tokens:number|null;cost_usd:number|null;provider_rate_limits?:Record<string,string>|null;rate_limits_observed_at?:number|null;request_count?:number;token_reported_requests?:number;unreported_requests?:number;total_only_requests?:number;failed_requests?:number;pending_requests?:number};
export type BrainUsage = {models:Record<string,BrainModelUsage>;week_starts_at:number};
export type BrainState = {catalog:BrainCatalog;configuration:BrainConfiguration;usage:BrainUsage;loading:boolean;error:string;save:(configuration:BrainConfiguration)=>Promise<void>;refresh:()=>Promise<void>;refreshUsage:()=>Promise<void>;resetUsage:(modelId:string)=>Promise<void>};
export const INITIAL_BRAIN:BrainConfiguration = {mode:'split',selections:{brain:'mlx:ornith_1_5_9b_8bit',coder:'mlx:ornith_1_5_9b_8bit',short_coding:'mlx:ternary_bonsai_2_27b',general_inference:'mlx:ternary_bonsai_2_27b'},efforts:{brain:null,coder:null,short_coding:null,general_inference:null},fallback_models:[]};
export const BRAIN_LABELS:Record<BrainRole,string> = {brain:'Brain',coder:'Coder',short_coding:'Short coding / fix',general_inference:'Inference'};
export function brainRoleLabel(role:BrainRole,language:'ja'|'en'):string {
 if(language==='en')return BRAIN_LABELS[role];
 return {brain:'Brain',coder:'コーディング',short_coding:'単純なコーディング、修正',general_inference:'推論'}[role];
}
export function providerDisplayLabel(provider:string,language:'ja'|'en'):string {
 if(language==='en')return provider;
 const labels:Record<string,string>={'OpenAI compatible API':'OpenAI互換API','Google compatible API':'Google互換API','Anthropic compatible API':'Anthropic互換API','OpenAI Codex · ChatGPT plan':'OpenAI Codex・ChatGPTプラン'};
 return labels[provider]||provider;
}
export function brainEffortLabel(level:string,language:'ja'|'en'):string {
 const normalized=level.toLowerCase();
 if(language==='ja'){const labels:Record<string,string>={off:'ゼロ',auto:'自動',on:'MAX',minimal:'最小限',low:'低',medium:'中',high:'高',xhigh:'最高',max:'最大',maximum:'最大',ultra:'超高',none:'なし'};return labels[normalized]||level;}
 if(normalized==='off')return 'Zero';if(normalized==='on')return 'MAX';
 return level.charAt(0).toUpperCase()+level.slice(1);
}
export function modelEffortLevels(model:BrainModel|undefined):string[]{
 return model?.local?['off','auto','on']:(model?.effort_levels??[]);
}
export function useBrainModels(enabled:boolean,conversationId?:string|null,request:typeof api=api):BrainState {
 const {language}=useLanguage();
 const [catalog,setCatalog]=useState<BrainCatalog>({models:[],providers:[]}),[usage,setUsage]=useState<BrainUsage>({models:{},week_starts_at:0});
 const [configurations,setConfigurations]=useState<Record<string,BrainConfiguration>>({}),[loading,setLoading]=useState(false),[errors,setErrors]=useState<Record<string,string>>({});
 const scope=conversationId?`?conversation_id=${encodeURIComponent(conversationId)}`:'';
 const configuration=configurations[scope]||INITIAL_BRAIN,error=errors[scope]||'';
 const setError=(next:string)=>setErrors(old=>({...old,[scope]:next}));
 const setConfiguration=(next:BrainConfiguration)=>setConfigurations(old=>({...old,[scope]:next}));
 const epoch=useRef(0),credentialsRestored=useRef(false);
 const refresh=useCallback(async()=>{if(!enabled)return;const current=++epoch.current;setLoading(true);try{const bridge=native();let restoreError='';if(bridge&&!credentialsRestored.current){try{await bridge.core.invoke('restore_external_api_credentials');credentialsRestored.current=true}catch(error){restoreError=error instanceof Error?error.message:String(error)}}const [c,s,u]=await Promise.all([request<BrainCatalog>('/brain/models'),request<BrainConfiguration>('/brain/configuration'+scope),request<BrainUsage>('/brain/usage')]);if(current!==epoch.current)return;setCatalog(c);setConfiguration(s);setUsage(u);setError(restoreError);if(c.providers.some(p=>p.credential_pending))await persistBrainOAuthCredential(request,language)}catch(e){if(current===epoch.current)setError(e instanceof Error?e.message:String(e))}finally{if(current===epoch.current)setLoading(false)}},[enabled,scope,request,language]);
 useEffect(()=>{if(!enabled||!native())return;let live=true;const flush=()=>{void persistBrainOAuthCredential(request,language).catch(error=>{if(live)setError(error instanceof Error?error.message:String(error))})};flush();const timer=window.setInterval(flush,3000);return()=>{live=false;window.clearInterval(timer)}},[enabled,request,language,scope]);
 const refreshUsage=useCallback(async()=>{if(!enabled)return;try{setUsage(await request<BrainUsage>('/brain/usage'))}catch{/* Keep the last known usage visible during brief backend interruptions. */}},[enabled,request]);
 useEffect(()=>{void refresh();const focus=()=>{void refresh()};window.addEventListener('focus',focus);return()=>{epoch.current++;window.removeEventListener('focus',focus)}},[refresh]);
 const save=async(next:BrainConfiguration)=>{const current=++epoch.current;setLoading(true);try{const saved=await request<BrainConfiguration>('/brain/configuration'+scope,{method:'PUT',body:JSON.stringify(next)});if(current===epoch.current){setConfiguration(saved);setError('')}}catch(e){if(current===epoch.current)setError(e instanceof Error?e.message:String(e));throw e}finally{if(current===epoch.current)setLoading(false)}};
 const resetUsage=async(modelId:string)=>{await request('/brain/usage/reset',{method:'POST',body:JSON.stringify({model_id:modelId})});await refresh()};
 return {catalog,configuration,usage,loading:loading||!enabled||!configurations[scope],error,save,refresh,refreshUsage,resetUsage};
}

export async function registerBrainProvider(input:import('./ModelsPanel').ProviderRegistration,request:typeof api=api,language:'ja'|'en'='ja'):Promise<void> {
 if(input.values.api_key){const bridge=native();if(!bridge)throw Error(language==='ja'?'認証情報の保存にはOLCRデスクトップのKeychainが必要です。':'OLCR Desktop with Keychain is required to save credentials.');await bridge.core.invoke('set_external_credential',{handle:'OLCR_BRAIN_'+input.id.toUpperCase()+((input.account_id||1)>1?'_'+input.account_id:''),value:input.values.api_key});}
 await request('/brain/providers',{method:'PUT',body:JSON.stringify(input)});
}

export async function signInBrainOAuth(request:typeof api=api,language:'ja'|'en'='ja',accountId=1):Promise<void> {
 const bridge=native();if(!bridge)throw Error(language==='ja'?'OAuth接続にはOLCRデスクトップが必要です。':'OLCR Desktop is required for OAuth sign-in.');
 const started=await request<{attempt_id:string;authorization_url:string}>('/brain/oauth/start'+(accountId===1?'':`?account_id=${accountId}`),{method:'POST'});
 await bridge.core.invoke('open_external_url',{url:started.authorization_url});
 const deadline=Date.now()+305000;
 while(Date.now()<deadline){
  await new Promise(resolve=>window.setTimeout(resolve,1000));
  const result=await request<{status:string;message?:string}>('/brain/oauth/'+encodeURIComponent(started.attempt_id));
  if(result.status==='complete'){await persistBrainOAuthCredential(request,language,true);return;}
  if(result.status!=='pending')throw Error(result.message||(language==='ja'?'ChatGPTへの接続が完了しませんでした。もう一度サインインしてください。':'ChatGPT sign-in did not complete. Please try again.'));
 }
 throw Error(language==='ja'?'OAuth接続の待機時間を超えました。':'Timed out while waiting for OAuth sign-in.');
}

async function savePendingBrainOAuthCredential(request:typeof api,language:'ja'|'en'='ja',interactive=false):Promise<void>{
 for(let attempt=0;attempt<8;attempt++){
  const pending=await request<{credential:string|null;version?:string;handle?:string}>('/brain/oauth-credential');
  if(!pending?.credential)return;
  const bridge=native();if(!bridge)throw Error(language==='ja'?'OAuth認証情報を保存するにはOLCRデスクトップが必要です。':'OLCR Desktop is required to save OAuth credentials.');
  await bridge.core.invoke('set_external_credential',{handle:pending.handle||'OLCR_BRAIN_OPENAI_OAUTH',value:pending.credential,interactive});
  const saved=await request<{pending?:boolean}>('/brain/oauth-credential',{method:'POST',body:JSON.stringify({version:pending.version})});
  if(!saved?.pending)return;
 }
 throw Error(language==='ja'?'OAuth認証情報の更新が続いています。保存を再試行します。':'OAuth credentials are still changing; saving will retry.');
}

// Multiple conversations/focus refreshes must never overwrite a newer rotated token.
const credentialSaves=new WeakMap<typeof api,Promise<void>>();
export function persistBrainOAuthCredential(request:typeof api,language:'ja'|'en'='ja',interactive=false):Promise<void>{
 const current=credentialSaves.get(request);if(current)return interactive?current.catch(()=>{}).then(()=>persistBrainOAuthCredential(request,language,true)):current;
 const save=savePendingBrainOAuthCredential(request,language,interactive).finally(()=>{if(credentialSaves.get(request)===save)credentialSaves.delete(request)});
 credentialSaves.set(request,save);return save;
}
