import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import {native} from './native';
import {BrainModel,PurposeModel} from './brainModels';
import {SettingHelp} from './SettingHelp';

type Choice={protocol:string;model?:string;base_url?:string;provider?:string;dimension?:number};
type Preset={id:string;repo:string;size_bytes:number|null;license:string;downloaded:boolean};
type Role={role:string;purpose:string;purpose_en:string;protocols:string[];selected:Choice;preset:{id:string;label:string;deployment:string;runtime:string}};
type VisionModel={id:string;label:string;provider:string;local:boolean;vision_supported?:boolean|null;configuration:Choice};
type Job={id:string;models:string[];status:string;completed:string[];current:string|null;error:string|null};
type Catalog={vision_models?:VisionModel[];roles:Role[];presets:Preset[];jobs:Job[];local_models:{model:string;base_url:string;capabilities:string[]}[]};
type ProfileCatalog={recommended_profile:string;tiers:{id:string;label:string}[];profile:{brain:Record<string,string>;roles:Record<string,string>;audio:Record<string,string>};models:Record<string,{id:string;brain_id?:string}>};
type Request=typeof api;
function checkCatalog(value:Catalog):Catalog{if(!Array.isArray(value?.roles)||!Array.isArray(value?.presets)||!Array.isArray(value?.jobs)||!Array.isArray(value?.local_models))throw Error('MODEL_CATALOG_UNAVAILABLE');return value}
function profileDownloadIds(profile:ProfileCatalog,catalog:Catalog){
 const presetIds=new Set(catalog.presets.map(item=>item.id)),ids=new Set<string>();
 const add=(id:string)=>{
  if(!id)return;
  if(presetIds.has(id)){ids.add(id);return}
  const descriptor=Object.values(profile.models||{}).find(model=>model.brain_id===id);
  if(descriptor&&presetIds.has(descriptor.id))ids.add(descriptor.id);
 };
 for(const id of Object.values({...profile.profile.brain,...profile.profile.roles}))add(id);
 for(const id of Object.values(profile.profile.audio))add(id);
 add('embeddinggemma');
 return catalog.presets.filter(item=>ids.has(item.id)).sort((left,right)=>left.id.localeCompare(right.id));
}
function memoryTierLabel(id:string,ja:boolean){
 if(ja)return memoryTierName(id,ja)+'用プリセット';
 return id==='256'?'256 GB+ preset':id==='16'?'16 GB and under preset':id+' GB preset';
}
function memoryTierName(id:string,ja:boolean){return ja?(id==='16'?'16GB以下':id==='256'?'256GB以上':id+'GB'):id==='256'?'256 GB+':id==='16'?'16 GB and under':id+' GB'}
const names:Record<string,string>={embeddinggemma:'EmbeddingGemma',laya_multilingual:'Laya multilingual',ternary_bonsai_2_27b:'Ternary Bonsai 2 · 27B',ternary_bonsai_2_27b_6bit:'Ternary Bonsai 2 · 27B · 6-bit',ternary_bonsai_2_27b_8bit:'Ternary Bonsai 2 · 27B · 8-bit',ternary_bonsai_2_27b_bf16:'Ternary Bonsai 2 · 27B · BF16',ornith_1_5_9b:'Ornith 1.5 · 9B',ornith_1_5_9b_8bit:'Ornith 1.5 · 9B · MLX 8-bit',ornith_1_5_35b_a3b_4bit:'Ornith 1.5 · 35B-A3B · 4-bit',ornith_1_5_122b_4bit:'Ornith 1.5 · 122B · 4-bit',ornith_1_5_122b_8bit:'Ornith 1.5 · 122B · 8-bit',fastvlm_mlx_bf16:'FastVLM',ui_venus_mlx_6bit:'UI-Venus',qwen3_vl_2b_instruct:'Qwen3-VL',qwen3_8_27b_mxfp8:'Qwen3.8 · 27B MXFP8',qwen3_8_27b_bf16:'Qwen3.8 · 27B BF16',plamo_3_translate_2b:'PLaMo 3 Translate 2B',plamo_3_translate_8b:'PLaMo 3 Translate 8B',plamo_3_translate_31b:'PLaMo 3 Translate 31B',whisper_large_v3_turbo:'Whisper large-v3-turbo',whisper_large_v3:'Whisper large-v3',lfm2_5_audio_1_5b:'LFM2.5 Audio · 1.5B',transcription_ja:'LFM2.5 Audio · JP',transcription_en:'LFM2.5 Audio · EN'};
const protocolName=(protocol:string,ja:boolean)=>({preset:ja?'推奨プリセット':'Recommended preset',ollama:ja?'このMacのOllamaモデル':'Local Ollama model',openai:ja?'互換API・ローカルサーバー':'Compatible API / local server',jev:ja?'Jev互換API・ローカルサーバー':'Jev-compatible API / local server',brain:ja?'接続済みAPIのモデル':'Connected API model',registered:ja?'インストール済み・接続済みのモデル':'Installed and connected models'}[protocol]||protocol);
const roleHelp:Record<string,[string,string]>={
 semantic_embedding:['資料や質問を意味で比較するためのモデルです。似た内容を検索する際に使い、回答文の生成はBrainが担当します。','Compare the meaning of questions and references for retrieval. Brain still generates the answer.'],
 typed_local_decisions:['決められた候補から処理を選ぶモデルです。自由な文章を生成するBrainとは役割が異なります。','Select processing options from a fixed set of candidates, separately from free-form Brain answers.'],
 general_image:['画像の内容を読み取る用途です。通常の画像理解に使うモデルを選びます。','Choose the model used to interpret ordinary images.'],
 gui_grounding:['アプリ画面の要素や操作位置を読み取る用途です。通常の画像理解とは別のモデルを設定できます。','Identify app controls and action locations in screenshots. You can select a different model from ordinary image understanding.'],
 transcription_ja:['日本語の音声を文字に変換するモデルです。一般設定でLFMの音声入力を選んだ場合などに使います。','Convert Japanese speech to text, including when LFM voice input is selected in General settings.'],
 transcription_en:['英語の音声を文字に変換するモデルです。一般設定でLFMの音声入力を選んだ場合などに使います。','Convert English speech to text, including when LFM voice input is selected in General settings.'],
 translation:['翻訳タスク専用のモデルです。翻訳依頼の実行時だけ使います。外部APIのモデルを選ぶと翻訳文がその接続先へ送信されます。','Dedicated to translation requests. Choosing a remote API sends translation text to that endpoint. General chat and other task models are unaffected.'],
};

function ChoiceForm({role,models,visionModels,localModels,ja,onSave,onCancel}:{role:Role;models:BrainModel[];visionModels?:VisionModel[];localModels:Catalog['local_models'];ja:boolean;onSave:(choice:Choice,key:string)=>Promise<void>;onCancel:()=>void}){
 const [choice,setChoice]=useState<Choice>({...role.selected}),[key,setKey]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const vision=['general_image','gui_grounding'].includes(role.role);
 const [connection,setConnection]=useState(vision&&['preset','brain','ollama'].includes(role.selected.protocol)?'registered':role.selected.protocol);
 const protocols=vision?['registered','preset',...role.protocols.filter(protocol=>!['brain','ollama'].includes(protocol))]:['preset',...role.protocols];
 const options=localModels.filter(model=>model.capabilities.includes(role.role==='semantic_embedding'?'embedding':'vision'));
 const registered:VisionModel[]=visionModels||[
  ...localModels.filter(model=>model.capabilities.includes('vision')).map(model=>({id:'ollama:'+model.model,label:model.model,provider:'Ollama',local:true,vision_supported:true,configuration:{protocol:'ollama',model:model.model,base_url:model.base_url}})),
  ...models.filter(model=>model.available&&model.id.startsWith('api:')&&model.vision_supported!==false).map(model=>{const [,provider,...name]=model.id.split(':');return {...model,configuration:{protocol:'brain',provider,model:name.join(':')}}})
 ];
 const selected=registered.find(model=>model.configuration.protocol===choice.protocol&&model.configuration.model===choice.model&&(choice.protocol==='brain'?model.configuration.provider===choice.provider:model.configuration.base_url===choice.base_url));
 const selectProtocol=(protocol:string)=>{setConnection(protocol);setKey('');setChoice(protocol==='registered'?{protocol:'brain'}:{protocol,base_url:protocol==='ollama'?'http://127.0.0.1:11434':protocol==='jev'?'https://api.typesafe.ai':'',model:''})};
 return <form className="purpose-choice-form" onSubmit={async event=>{event.preventDefault();setBusy(true);setError('');try{await onSave(choice,key);setKey('');onCancel()}catch(reason){setError(reason instanceof Error?reason.message:String(reason))}finally{setBusy(false)}}}>
  <label>{ja?'接続方法':'Connection'}<select disabled={busy} value={connection} onChange={event=>selectProtocol(event.target.value)}>{protocols.map(protocol=><option key={protocol} value={protocol}>{protocolName(protocol,ja)}</option>)}</select></label>
  {connection==='registered'?<>
   <label>{ja?'画像理解に使うモデル':'Model for vision'}<select disabled={busy} required value={selected?.id||''} onChange={event=>{const model=registered.find(item=>item.id===event.target.value);if(model)setChoice({...model.configuration})}}><option value="">{ja?'モデルを選択':'Choose a model'}</option>{[true,false].map(local=><optgroup key={String(local)} label={local?(ja?'このMacのモデル':'Models on this Mac'):(ja?'接続済みAPI':'Connected APIs')}>{registered.filter(model=>model.local===local).map(model=><option key={model.id} value={model.id}>{model.label} · {model.provider}</option>)}</optgroup>)}</select></label>
   {!registered.length&&<p role="status">{ja?'画像対応のローカルモデルをインストールするか、モデル設定からAPIを接続してください。':'Install a local vision model or connect an API in Models settings.'}</p>}
   {selected?.vision_supported==null&&<p className="muted">{ja?'このモデルの画像対応を、切り替え時に確認します。':'Image support will be checked before switching to this model.'}</p>}
  </>:choice.protocol!=='preset'&&<>
   <label>{ja?'接続先URL':'Base URL'}<input type="url" required disabled={busy} value={choice.base_url||''} onChange={event=>setChoice({...choice,base_url:event.target.value})}/></label>
   {choice.protocol==='ollama'&&options.length>0&&<label>{ja?'インストール済みモデル':'Installed models'}<select disabled={busy} value={options.some(model=>model.model===choice.model)?choice.model:''} onChange={event=>setChoice({...choice,model:event.target.value})}><option value="">{ja?'モデルを選択':'Choose a model'}</option>{options.map(model=><option key={model.model} value={model.model}>{model.model}</option>)}</select></label>}
   <label>{ja?'モデルID':'Model ID'}<input required disabled={busy} value={choice.model||''} onChange={event=>setChoice({...choice,model:event.target.value})} placeholder={choice.protocol==='jev'?'jev-latest':''}/></label>
   {choice.protocol!=='ollama'&&<label>{ja?'APIキー（保存済みの場合は空欄）':'API key (leave blank to reuse saved key)'}<input type="password" autoComplete="new-password" disabled={busy} value={key} onChange={event=>setKey(event.target.value)}/></label>}
  </>}
  <p className="muted">{connection==='registered'?(ja?'保存済みの接続を使用します。切り替え前に画像入力の互換性を確認します。APIを選ぶと確認用の画像を送信します。':'Uses your saved connection. Image compatibility is checked before switching; API models receive a small sample image.'):(ja?'互換性を確認してから切り替えます。外部APIでは確認用の短い入力を送信します。APIキーはこのMacのKeychainに保存します。':'Compatibility is checked before switching. Remote APIs receive a short test input. API keys are stored in this Mac’s Keychain.')}</p>
  {role.role==='semantic_embedding'&&<p className="muted">{ja?'切替後、許可済み文書の検索索引を再作成します。外部モデルを選ぶと、索引作成や検索のテキストをその接続先へ送信します。':'Authorized document indexes are rebuilt after switching. Remote models receive indexing and search text.'}</p>}
  {error&&<p role="alert">{error}</p>}
  <div className="brain-form-actions"><button type="button" disabled={busy} onClick={onCancel}>{ja?'キャンセル':'Cancel'}</button><button type="submit" disabled={busy} className="primary">{busy?(ja?'互換性を確認中…':'Checking compatibility…'):(ja?'互換性を確認して切り替え':'Check compatibility and switch')}</button></div>
 </form>;
}

export function PurposeModelsPanel({ja,brainModels,fallback=[],request=api,onChanged}:{ja:boolean;brainModels:BrainModel[];fallback?:PurposeModel[];request?:Request;onChanged?:()=>void}){
 const [catalog,setCatalog]=useState<Catalog|null>(null),[error,setError]=useState(''),[editing,setEditing]=useState<string|null>(null),[download,setDownload]=useState<Preset[]|null>(null),[downloadTier,setDownloadTier]=useState(''),[downloadTotal,setDownloadTotal]=useState(0),[profileTier,setProfileTier]=useState(''),[tiers,setTiers]=useState<ProfileCatalog['tiers']>([]),[busy,setBusy]=useState(false);
 const changed=useRef(onChanged),completed=useRef(new Set<string>());changed.current=onChanged;
 const refresh=async()=>{const next=checkCatalog(await request<Catalog>('/purpose-models'));setCatalog(next);setError('')};
 useEffect(()=>{let live=true;void request<ProfileCatalog>('/model-profiles').then(profile=>{if(live){setTiers(Array.isArray(profile.tiers)?profile.tiers:[]);setProfileTier(String(profile.recommended_profile||''))}}).catch(reason=>{if(live)setError(reason instanceof Error?reason.message:String(reason))});return()=>{live=false}},[request]);
 const prepareProfileDownload=async()=>{
  if(!profileTier||!catalog)return;
  setBusy(true);setError('');
  try{
   const [profile,latestCatalogValue]=await Promise.all([
    request<ProfileCatalog>(`/model-profiles?memory_gb=${encodeURIComponent(profileTier)}`),
    request<Catalog>('/purpose-models'),
   ]);
   const latestCatalog=checkCatalog(latestCatalogValue);
   setCatalog(latestCatalog);
   const models=profileDownloadIds(profile,latestCatalog);
   setDownloadTier(profileTier);setDownloadTotal(models.length);setDownload(models.filter(model=>!model.downloaded));
  }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
  finally{setBusy(false)}
 };
 const prepareSingleDownload=(model:Preset)=>{setDownloadTier('');setDownloadTotal(1);setDownload([model])};
 useEffect(()=>{let live=true;const poll=()=>{void request<Catalog>('/purpose-models').then(next=>{const checked=checkCatalog(next);if(live){setCatalog(checked);setError('');for(const job of checked.jobs){if(job.status==='complete'&&!completed.current.has(job.id)){completed.current.add(job.id);changed.current?.()}}}}).catch(reason=>{if(live)setError(reason instanceof Error?reason.message:String(reason))})};poll();const timer=window.setInterval(poll,5000);return()=>{live=false;window.clearInterval(timer)}},[request]);
 const switchModel=async(role:string,configuration:Choice,apiKey:string)=>{
  const bridge=native();if(apiKey&&!bridge)throw Error(ja?'APIキーの保存にはOLCRデスクトップが必要です。':'OLCR Desktop is required to store an API key.');
  const checked=await request<{token:string;credential_handle:string}>(`/purpose-models/${role}/probe`,{method:'POST',body:JSON.stringify({configuration,api_key:apiKey||undefined,confirm_remote:true})});
  try{if(apiKey)await bridge!.core.invoke('set_external_credential',{handle:checked.credential_handle,value:apiKey});
   await request(`/purpose-models/${role}/activate`,{method:'POST',body:JSON.stringify({token:checked.token})});
  }catch(reason){await request(`/purpose-models/${role}/cancel`,{method:'POST',body:JSON.stringify({token:checked.token})}).catch(()=>{});throw reason}
  await refresh();changed.current?.();window.dispatchEvent(new Event('olcr-command-complete'));
 };
 return <><section className="brain-setting-section" aria-label={ja?'用途別モデル一覧':'Models by purpose'}>
  <div className="purpose-section-heading"><h3>{ja?'用途別モデル':'Models by purpose'}</h3>{catalog&&<div className="purpose-batch-download"><label><span>{ja?'プリセットのメモリ':'Preset memory'}</span><select aria-label={ja?'プリセットのメモリ':'Preset memory'} value={profileTier} onChange={event=>setProfileTier(event.target.value)} disabled={busy||!tiers.length}>{tiers.map(tier=><option key={tier.id} value={tier.id}>{memoryTierLabel(tier.id,ja)}</option>)}</select></label><button type="button" onClick={()=>void prepareProfileDownload()} disabled={busy||!profileTier||!tiers.length||catalog.jobs.some(job=>['queued','downloading'].includes(job.status))}>{ja?'推奨プリセットを一括ダウンロード':'Download recommended presets'}</button></div>}</div>
  <p>{ja?'用途ごとに推奨モデル・このMacのモデル・自身のAPI接続先を選べます。Brainのモデル選択とは独立して保存されます。':'Choose recommended presets, local models, or your API connections for each purpose. These choices are saved independently of Brain.'}</p>
  {catalog?<div className="brain-purpose-groups">{catalog.roles.map(role=>{const preset=catalog.presets.find(item=>item.id===role.preset.id);return <section className="brain-purpose-group" key={role.role} aria-label={ja?role.purpose:role.purpose_en}>
   <div className="setting-help-heading"><h4>{ja?role.purpose:role.purpose_en}</h4>{roleHelp[role.role]&&<SettingHelp label={ja?role.purpose:role.purpose_en}>{roleHelp[role.role][ja?0:1]}</SettingHelp>}</div><div className="brain-purpose-row"><div><strong>{role.selected.protocol==='preset'?(names[role.preset.id]||role.preset.label):role.selected.model}</strong><small>{protocolName(role.selected.protocol,ja)}</small></div><button type="button" onClick={()=>setEditing(editing===role.role?null:role.role)}>{ja?'変更':'Change'}</button></div>
   <div className="purpose-preset"><span>{ja?'推奨':'Recommended'}: {names[role.preset.id]||role.preset.label}</span><button type="button" disabled={preset?.downloaded||busy||catalog.jobs.some(job=>['queued','downloading'].includes(job.status))} onClick={()=>preset&&prepareSingleDownload(preset)}>{preset?.downloaded?(ja?'ダウンロード済み':'Downloaded'):(ja?'ダウンロード':'Download')}</button></div>
   {editing===role.role&&<ChoiceForm role={role} models={brainModels} visionModels={catalog.vision_models} localModels={catalog.local_models} ja={ja} onCancel={()=>setEditing(null)} onSave={(choice,key)=>switchModel(role.role,choice,key)}/>}
  </section>})}</div>:<div className="brain-purpose-groups">{fallback.filter(model=>model.ready&&!['normal_chat','planner_replanner','short_coding_fix_debug','long_implementation','vision_fallback'].includes(model.role)).map(model=><section className="brain-purpose-group" key={model.role}><h4>{ja?model.purpose:model.purpose_en}</h4><strong>{model.label}</strong></section>)}</div>}
  {catalog?.jobs.map(job=><p role="status" key={job.id}>{job.status==='complete'?(ja?'ダウンロード完了':'Download complete'):job.status==='failed'?(ja?'ダウンロードに失敗しました。再試行できます。':'Download failed. You can retry.'):`${ja?'ダウンロード中':'Downloading'}: ${names[job.current||'']||job.current||''} (${job.completed.length}/${job.models.length})`}</p>)}
  {error&&<p role="alert">{ja?'モデル設定を読み込めませんでした。':'Could not load model settings.'} {error}</p>}
  {download&&<div className="modal" role="presentation"><section className="voice-model-consent" role="dialog" aria-modal="true" aria-labelledby="purpose-download-title"><h2 id="purpose-download-title">{downloadTier?(ja?`${memoryTierName(downloadTier,ja)}用モデルをダウンロードしますか？`:`Download ${memoryTierName(downloadTier,ja)} models?`):(ja?'モデルをダウンロードしますか？':'Download model?')}</h2>{download.length?<><p>{ja?`${download.length}件のモデルをこのデバイスに保存します。通信と空き容量が必要です。`:`${download.length} models will be saved on this device. Network access and free disk space are required.`}</p><ul>{download.map(model=><li key={model.id}>{names[model.id]||model.id} {model.size_bytes?`· ${(model.size_bytes/1e9).toFixed(1)} GB`:''} {model.license?`· ${model.license}`:''}</li>)}</ul></>:<p>{downloadTotal?ja?`この${memoryTierName(downloadTier,ja)}用プリセットのモデルはすべてダウンロード済みです。`:`All models in the ${memoryTierName(downloadTier,ja)} preset are downloaded.`:ja?'このメモリ用のプリセットに、ダウンロード可能なモデルがありません。':'This memory preset has no downloadable models.'}</p>}<footer><button type="button" disabled={busy} onClick={()=>setDownload(null)}>{ja?'キャンセル':'Cancel'}</button>{download.length>0&&<button type="button" className="primary" disabled={busy} onClick={async()=>{setBusy(true);try{await request('/model-downloads',{method:'POST',body:JSON.stringify({model_ids:download.map(model=>model.id),confirm_download:true})});setDownload(null);await refresh()}catch(reason){setError(reason instanceof Error?reason.message:String(reason))}finally{setBusy(false)}}}>{busy?(ja?'開始中…':'Starting…'):(ja?'同意してダウンロード':'Agree and download')}</button>}</footer></section></div>}
 </section>{catalog&&<section className="brain-setting-section" aria-label={ja?'汎用モデルのダウンロード':'General model downloads'}><h3>{ja?'汎用モデルの推奨プリセット':'Recommended general-purpose models'}</h3>{catalog.presets.filter(model=>['ornith_1_5_9b_8bit','ternary_bonsai_2_27b'].includes(model.id)).map(model=><div className="purpose-preset" key={model.id}><span>{names[model.id]}</span><button type="button" disabled={model.downloaded||busy||catalog.jobs.some(job=>['queued','downloading'].includes(job.status))} onClick={()=>prepareSingleDownload(model)}>{model.downloaded?(ja?'ダウンロード済み':'Downloaded'):(ja?'ダウンロード':'Download')}</button></div>)}</section>}</>;
}
