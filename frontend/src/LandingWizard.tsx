import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {DialogFrame, StatusBadge} from './InterfacePrimitives';
import {useLanguage} from './language';
import './landing-wizard.css';

type Api = (path:string, init?:RequestInit)=>Promise<any>;
type ModelOption = {id:string;download_id?:string|null;label?:string;runtime?:string;size_bytes?:number|null;weight_gb?:number;license?:string;downloaded?:boolean;fits_memory?:boolean;available?:boolean;experimental?:boolean;installed_custom?:boolean};
type DownloadModel = {id:string;label?:string;repo?:string;runtime?:string;size_bytes?:number|null;weight_gb?:number;license?:string;downloaded?:boolean;verified?:boolean;experimental?:boolean};
type ComponentStatus = {name:string;state:string;message?:string};
type Stage = 'models'|'downloads'|'progress'|'done';

const MODEL_SLOTS = [
  {key:'brain',labelJa:'Brain',labelEn:'Brain',group:'brain'},
  {key:'coder',labelJa:'コーディング',labelEn:'Coding',group:'brain'},
  {key:'long_implementation',labelJa:'長いコーディング',labelEn:'Long coding tasks',group:'purpose'},
  {key:'normal_chat',labelJa:'通常のチャット',labelEn:'General chat',group:'purpose'},
  {key:'short_coding',labelJa:'短いコード修正',labelEn:'Short coding fixes',group:'brain'},
  {key:'general_inference',labelJa:'純粋推論',labelEn:'General reasoning',group:'brain'},
  {key:'translation',labelJa:'翻訳',labelEn:'Translation',group:'purpose'},
  {key:'transcription_ja',labelJa:'日本語の文字起こし',labelEn:'Japanese transcription',group:'audio'},
  {key:'transcription_en',labelJa:'英語の文字起こし',labelEn:'English transcription',group:'audio'},
  {key:'read_aloud',labelJa:'自動読み上げ',labelEn:'Read aloud',group:'audio'},
] as const;
const COMPONENTS = [
  {id:'laya',ja:'ローカル判断モデル',en:'Local decision model'},
  {id:'agents',ja:'エージェント実行環境',en:'Agent runtime'},
  {id:'node-tools',ja:'Node.jsツールと参照データ',en:'Node.js tools and reference data'},
  {id:'browser',ja:'ブラウザー操作環境',en:'Browser automation runtime'},
  {id:'serena',ja:'コード解析環境',en:'Code intelligence runtime'},
  {id:'convertx',ja:'文書変換の基本ファイル',en:'Document conversion base files'},
] as const;
const GB = 1_000_000_000;
const sizeLabel=(bytes?:number|null)=>typeof bytes==='number'&&bytes>0?`${(bytes/GB).toFixed(bytes/GB<10?1:0)} GB`:'';
const sameRecord=(left:any,right:any)=>{
  if(!left||!right)return false;
  const a=Object.keys(left).sort(),b=Object.keys(right).sort();
  return a.length===b.length&&a.every((key,index)=>key===b[index]&&left[key]===right[key]);
};
function downloadIdsForSelection(roleModels:Record<string,string>,audioModels:Record<string,string>,catalog:any,
  presets:DownloadModel[],customRoles:string[]){
  const presetIds=new Set(presets.map(item=>item.id));
  const modelCatalog=Object.values(catalog?.models||{}) as Array<Record<string,any>>;
  const ids=new Set<string>();
  const addModel=(modelId:string)=>{
    if(!modelId)return;
    if(presetIds.has(modelId)){ids.add(modelId);return}
    const descriptor=modelCatalog.find(row=>row.brain_id===modelId);
    if(descriptor?.id&&presetIds.has(String(descriptor.id)))ids.add(String(descriptor.id));
  };
  for(const [role,modelId] of Object.entries(roleModels)){
    if(!customRoles.includes(role))addModel(modelId);
  }
  for(const [role,modelId] of Object.entries(audioModels)){
    if(role!=='read_aloud'&&!customRoles.includes(role))addModel(modelId);
  }
  if(!customRoles.includes('semantic_embedding'))addModel('embeddinggemma');
  // LFM Audio also powers Japanese and English read-aloud. Keep it available
  // on tiers that select Whisper for speech recognition.
  if(audioModels.read_aloud==='lfm2_5_audio_1_5b'){
    addModel('transcription_ja');addModel('transcription_en');
  }
  return [...ids];
}

export function LandingWizard({api,open,onClose}:{api:Api;open:boolean;onClose:()=>void}){
  const ja=useLanguage().language==='ja';
  const [stage,setStage]=useState<Stage>('models');
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState('');
  const [catalog,setCatalog]=useState<any>(null);
  const [models,setModels]=useState<DownloadModel[]>([]);
  const [components,setComponents]=useState<ComponentStatus[]>([]);
  const [customPurposeRoles,setCustomPurposeRoles]=useState<string[]>([]);
  const [memoryGb,setMemoryGb]=useState(32);
  const [memoryNeedsRecheck,setMemoryNeedsRecheck]=useState(false);
  const [profileId,setProfileId]=useState('32');
  const [customize,setCustomize]=useState(false);
  const [roleModels,setRoleModels]=useState<Record<string,string>>({});
  const [audioModels,setAudioModels]=useState<Record<string,string>>({});
  const [requiredModelIds,setRequiredModelIds]=useState<string[]>([]);
  const [selectionMade,setSelectionMade]=useState(false);
  const [selectedModelIds,setSelectedModelIds]=useState<string[]>([]);
  const [selectedComponents,setSelectedComponents]=useState<string[]>([]);
  const [individual,setIndividual]=useState(false);
  const [jobId,setJobId]=useState('');
  const [job,setJob]=useState<any>(null);
  const [applied,setApplied]=useState<boolean|null>(null);
  const initialized=useRef(false);
  const terminalHandled=useRef(false);
  const operationGeneration=useRef(0);
  const closeRef=useRef<()=>void>(()=>{});

  const loadData=useCallback(async(memory?:number)=>{
    setLoading(true);setError('');
    try{
      const profilePath=typeof memory==='number'?`/model-profiles?memory_gb=${Math.max(1,Math.min(1024,Math.round(memory)))}`:'/model-profiles';
      const [profileData,downloadData,componentData,purposeData]=await Promise.all([
        api(profilePath),api('/model-downloads'),api('/components'),api('/purpose-models'),
      ]);
      setCatalog(profileData);
      const detected=Number(profileData?.hardware?.memory_gb)||memory||32;
      setMemoryGb(Math.round(detected));
      setMemoryNeedsRecheck(false);
      setProfileId(String(profileData?.recommended_profile||'32'));
      setCustomize(false);
      const current=profileData?.profile?.current?.profile_id===String(profileData?.recommended_profile)?profileData.profile.current:null;
      const nextRoles={...(current?.brain||profileData?.profile?.brain),...(current?.roles||profileData?.profile?.roles)};
      const nextAudio={...(current?.audio||profileData?.profile?.audio)};
      const nextModels=Array.isArray(downloadData?.presets)?downloadData.presets:[];
      const nextCustomPurposeRoles=Array.isArray(purposeData?.roles)?purposeData.roles.filter((row:any)=>row?.selected?.protocol&&row.selected.protocol!=='preset').map((row:any)=>row.role):[];
      const currentMissing=current?.applied===false?downloadIdsForSelection(nextRoles,nextAudio,profileData,nextModels,nextCustomPurposeRoles)
        .filter(id=>!nextModels.find((model:DownloadModel)=>model.id===id)?.downloaded):[];
      setRoleModels(nextRoles);
      setAudioModels(nextAudio);
      const currentCustom=Boolean(current&&(
        !sameRecord(current.brain,profileData?.profile?.brain)||
        !sameRecord(current.roles,profileData?.profile?.roles)||
        !sameRecord(current.audio,profileData?.profile?.audio)));
      setCustomize(currentCustom);
      setSelectionMade(Boolean(current?.applied===false));
      setModels(nextModels);
      setComponents(Array.isArray(componentData?.components)?componentData.components:[]);
      setCustomPurposeRoles(nextCustomPurposeRoles);
      setRequiredModelIds(currentMissing);setSelectedModelIds(currentMissing);setSelectedComponents([]);
      setApplied(current?.applied===true?true:null);setJob(null);setJobId('');setIndividual(false);setStage('models');
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
    finally{setLoading(false)}
  },[api]);

  useEffect(()=>{
    if(!open){initialized.current=false;return}
    if(initialized.current)return;
    initialized.current=true;
    void loadData();
  },[open,loadData]);

  const updateMemory=async(value:number)=>{
    if(!Number.isFinite(value)||value<1||value>1024)return;
    setMemoryGb(Math.round(value));
    try{
      setError('');
      const next=await api(`/model-profiles?memory_gb=${Math.round(value)}`);
      setCatalog(next);
      setProfileId(String(next?.recommended_profile||'32'));
      setCustomize(false);
      const current=next?.profile?.current?.profile_id===String(next?.recommended_profile)?next.profile.current:null;
      const nextRoles={...(current?.brain||next?.profile?.brain),...(current?.roles||next?.profile?.roles)};
      const nextAudio={...(current?.audio||next?.profile?.audio)};
      setRoleModels(nextRoles);
      setAudioModels(nextAudio);
      const currentCustom=Boolean(current&&(
        !sameRecord(current.brain,next?.profile?.brain)||
        !sameRecord(current.roles,next?.profile?.roles)||
        !sameRecord(current.audio,next?.profile?.audio)));
      setCustomize(currentCustom);
      const missing=current?.applied===false?downloadIdsForSelection(nextRoles,nextAudio,next,models,customPurposeRoles)
        .filter(id=>!models.find(item=>item.id===id)?.downloaded):[];
      setSelectionMade(Boolean(current?.applied===false));
      setRequiredModelIds(missing);setSelectedModelIds(missing);setApplied(current?.applied===true?true:null);
      setMemoryNeedsRecheck(false);
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
  };

  const closeWizard=useCallback(()=>{
    operationGeneration.current+=1;
    const activeJob=jobId;
    const finalStatus=stage==='done'?'completed':'aborted';
    if(activeJob&&stage==='progress')void api(`/landing/downloads/${encodeURIComponent(activeJob)}`,{method:'DELETE'}).catch(()=>{});
    void api('/landing/state',{method:'PUT',body:JSON.stringify({seen:true,status:finalStatus,...(profileId?{profile_id:profileId}:{})})}).catch(()=>{});
    onClose();
  },[api,jobId,stage,profileId,onClose]);
  closeRef.current=closeWizard;

  const saveModelSelection=async(skip=false)=>{
    setError('');setLoading(true);
    try{
      if(skip){setSelectionMade(false);setRequiredModelIds([]);setProfileId('');setStage('downloads');return}
      const result=await api('/model-profiles/selection',{method:'PUT',body:JSON.stringify({profile_id:profileId,role_models:roleModels,audio_models:audioModels,memory_gb:memoryGb,confirm_apply:true})});
      setSelectionMade(true);
      const missing=Array.isArray(result?.download_ids)?result.download_ids:[];
      setRequiredModelIds(missing);
      setApplied(result?.applied===true);
      setSelectedModelIds(missing);
      setSelectedComponents([]);
      setStage('downloads');
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
    finally{setLoading(false)}
  };

  const componentChoices=useMemo(()=>COMPONENTS.filter(item=>item.id==='convertx'||components.find(row=>row.name===item.id)?.state!=='ready'),[components]);
  const targetModelIds=useMemo(()=>new Set(downloadIdsForSelection(
    Object.keys(roleModels).length?roleModels:{...catalog?.profile?.brain,...catalog?.profile?.roles},
    Object.keys(audioModels).length?audioModels:(catalog?.profile?.audio||{}),catalog,models,customPurposeRoles)),
  [roleModels,audioModels,catalog,models,customPurposeRoles]);
  const modelChoices=useMemo(()=>models.filter(item=>{
    if(!targetModelIds.has(item.id))return false;
    if(item.downloaded)return false;
    if(requiredModelIds.includes(item.id))return item.verified!==false&&!item.experimental;
    if(item.verified===false||item.experimental)return false;
    const tier=profileId||String(catalog?.recommended_profile||'32');
    const reserve=Number(catalog?.memory_policy?.reserved_system_gb?.[tier]??6);
    const overhead=Number(catalog?.memory_policy?.runtime_overhead_gb??3);
    const descriptor=catalog?.models?.[item.id];
    const weight=Number(descriptor?.weight_gb??item.weight_gb??(Number(item.size_bytes)||0)/(1024**3));
    return weight>0&&weight+reserve+overhead<=memoryGb;
  }),[models,profileId,catalog,memoryGb,requiredModelIds,targetModelIds]);
  const currentSelectionSize=useMemo(()=>modelChoices.filter(item=>selectedModelIds.includes(item.id)).reduce((sum,item)=>sum+(Number(item.size_bytes)||0),0),[modelChoices,selectedModelIds]);
  const allModelSize=useMemo(()=>modelChoices.reduce((sum,item)=>sum+(Number(item.size_bytes)||0),0),[modelChoices]);

  const beginDownload=async(mode:'all'|'selected')=>{
    setError('');
    const modelIds=mode==='all'?modelChoices.map(item=>item.id):selectedModelIds;
    const componentIds=mode==='all'?componentChoices.map(item=>item.id):selectedComponents;
    if(!modelIds.length&&!componentIds.length){await finishWithoutDownload();return}
    const generation=++operationGeneration.current;
    setLoading(true);
    try{
      const result=await api('/landing/downloads',{method:'POST',body:JSON.stringify({model_ids:modelIds,components:componentIds,confirm_download:true})});
      if(generation!==operationGeneration.current){void api(`/landing/downloads/${encodeURIComponent(result.id)}`,{method:'DELETE'}).catch(()=>{});return}
      setJobId(result.id);setJob(result);setStage('progress');terminalHandled.current=false;
      void api('/landing/state',{method:'PUT',body:JSON.stringify({seen:true,status:'in_progress',...(profileId?{profile_id:profileId}:{})})}).catch(()=>{});
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
    finally{setLoading(false)}
  };

  const finishWithoutDownload=async()=>{
    setLoading(true);setError('');
    try{
      if(selectionMade){
        const result=await api('/model-profiles/selection',{method:'PUT',body:JSON.stringify({profile_id:profileId,role_models:roleModels,audio_models:audioModels,memory_gb:memoryGb,confirm_apply:true})});
        setApplied(result?.applied===true);
        if(Array.isArray(result?.download_ids))setRequiredModelIds(result.download_ids);
      }
      await api('/landing/state',{method:'PUT',body:JSON.stringify({seen:true,status:'completed',...(profileId?{profile_id:profileId}:{})})});
      setStage('done');
    }catch(reason){setError(reason instanceof Error?reason.message:String(reason))}
    finally{setLoading(false)}
  };

  useEffect(()=>{
    if(!open||stage!=='progress'||!jobId)return;
    const generation=operationGeneration.current;
    let live=true;let timer:ReturnType<typeof setTimeout>|undefined;
    const poll=async()=>{
      try{
        const next=await api(`/landing/downloads/${encodeURIComponent(jobId)}`);
        if(!live||generation!==operationGeneration.current)return;
        setJob(next);
        if(['complete','failed','cancelled'].includes(next?.status)&&!terminalHandled.current){
          terminalHandled.current=true;
          if(next.status==='complete'){
            let didApply=false;
            if(selectionMade){
              try{
                const result=await api('/model-profiles/selection',{method:'PUT',body:JSON.stringify({profile_id:profileId,role_models:roleModels,audio_models:audioModels,memory_gb:memoryGb,confirm_apply:true})});
                didApply=result?.applied===true;setApplied(didApply);
                if(Array.isArray(result?.download_ids))setRequiredModelIds(result.download_ids);
              }catch(reason){if(live)setError(reason instanceof Error?reason.message:String(reason))}
            }
            if(live){
              await api('/landing/state',{method:'PUT',body:JSON.stringify({seen:true,status:'completed',...(profileId?{profile_id:profileId}:{})})}).catch(()=>{});
              setStage('done');
            }
          }else if(next.status==='failed'){
            if(live){setError(ja?'ダウンロードに失敗しました。残りの項目を選び直して再試行できます。':'A download failed. Select the remaining items and try again.');setStage('downloads');}
          }else if(live){setStage('done');setApplied(false)}
          return;
        }
      }catch(reason){if(live)setError(reason instanceof Error?reason.message:String(reason))}
      if(live)timer=setTimeout(()=>void poll(),900);
    };
    void poll();
    return()=>{live=false;if(timer)clearTimeout(timer)};
  },[api,open,stage,jobId,selectionMade,profileId,roleModels,audioModels,memoryGb,ja]);

  const roleOptions=(key:string):ModelOption[]=>catalog?.options?.[key]||[];
  const selectedValue=(key:string,group:string)=>group==='audio'?audioModels[key]:roleModels[key];
  const selectedFits=MODEL_SLOTS.every(slot=>{
    const options=roleOptions(slot.key);
    const value=selectedValue(slot.key,slot.group);
    const recommended=slot.group==='audio'?catalog?.profile?.audio?.[slot.key]:{
      ...catalog?.profile?.brain,...catalog?.profile?.roles,
    }[slot.key];
    // Recommended tiers are curated for their memory budget. Some defaults
    // (for example legacy manifest-backed roles) are not represented in the
    // custom-choice catalog, so an absent option must not invalidate the tier.
    if(value&&value===recommended){
      const option=options.find(row=>row.id===value);
      return !option||option.fits_memory!==false;
    }
    return !value||!options.length||options.some(option=>option.id===value&&option.fits_memory&&option.available!==false);
  });
  const enterCustom=()=>{
    const nextRoles={...roleModels};const nextAudio={...audioModels};
    for(const slot of MODEL_SLOTS){
      const options=roleOptions(slot.key).filter(option=>option.fits_memory&&option.available!==false);
      const target=slot.group==='audio'?nextAudio:nextRoles;
      if(options.length&&!options.some(option=>option.id===target[slot.key]))target[slot.key]=options[0].id;
    }
    setRoleModels(nextRoles);setAudioModels(nextAudio);setCustomize(true);
  };
  const setRole=(key:string,value:string,group:string)=>{
    setCustomize(true);
    if(group==='audio')setAudioModels(current=>({...current,[key]:value}));
    else setRoleModels(current=>({...current,[key]:value}));
    setSelectionMade(true);
  };
  const toggleModel=(id:string)=>setSelectedModelIds(current=>current.includes(id)?current.filter(value=>value!==id):[...current,id]);
  const toggleComponent=(id:string)=>setSelectedComponents(current=>current.includes(id)?current.filter(value=>value!==id):[...current,id]);
  const heading=ja?'OLCRをこのMacに合わせる':'Set up OLCR for this Mac';
  const modelLabel=(model:ModelOption)=>model.label||model.id;
  const itemLabel=(model:DownloadModel)=>model.label||model.id;
  const progressItems=(job?.model_ids?.length||0)+(job?.components?.length||0);
  const finishedItems=(job?.completed_models?.length||0)+(job?.completed_components?.length||0);

  if(!open)return null;
  return <DialogFrame title={heading} onClose={()=>closeRef.current()} confirmDiscard={false}>
    <div className="landing-wizard">
      {loading&&stage==='models'&&!catalog?<p role="status">{ja?'メモリとモデルを確認中…':'Checking memory and models…'}</p>:null}
      {error&&<div className="landing-error" role="alert">{error}</div>}
      {stage==='models'&&!catalog&&!loading&&<button type="button" onClick={()=>void loadData()}>{ja?'再読み込み':'Retry'}</button>}
      {stage==='models'&&catalog&&<>
        <div className="landing-intro"><p>{ja?'ユニファイドメモリに合うモデルを提案します。モデルは用途ごとに変更でき、適合しないサイズは選べません。':'OLCR recommends models for your unified memory. Choose different models by task; options that exceed the memory budget are unavailable.'}</p>
          <div className="landing-memory">
            <label htmlFor="landing-memory">{ja?'ユニファイドメモリ':'Unified memory'}</label>
            <div><input id="landing-memory" type="number" min="1" max="1024" value={memoryGb} onChange={event=>{setMemoryGb(Number(event.target.value));setMemoryNeedsRecheck(true)}}/><span>GB</span><button type="button" onClick={()=>void updateMemory(memoryGb)} disabled={loading||!memoryNeedsRecheck}>{ja?'再判定':'Recheck'}</button></div>
            <small>{catalog.hardware?.source==='unknown'?(ja?'自動検出できませんでした。Macの「このMacについて」で表示される値を入力してください。':'Could not detect it. Enter the value shown in About This Mac.'):ja?`検出: ${catalog.hardware?.chip||'Apple silicon'} · 推奨ティア: ${catalog.tiers?.find((item:any)=>item.id===catalog.recommended_profile)?.label||catalog.recommended_profile}`:`Detected: ${catalog.hardware?.chip||'Apple silicon'} · Recommended tier: ${catalog.tiers?.find((item:any)=>item.id===catalog.recommended_profile)?.label||catalog.recommended_profile}`}</small>
            {memoryNeedsRecheck&&<small role="status">{ja?'メモリを変更しました。「再判定」を押して適合モデルを更新してください。':'Memory changed. Recheck to update compatible models.'}</small>}
          </div>
        </div>
        {!customize&&<div className="landing-recommendation"><strong>{ja?'おすすめプリセット':'Recommended preset'} · {catalog.tiers?.find((item:any)=>item.id===profileId)?.label||profileId}</strong><p>{ja?'用途に合わせたモデルを使います。必要ならカスタムに切り替えて、各用途のモデルを変更できます。':'Uses models selected for each task. Switch to custom to choose a different model for each task.'}</p>{!selectedFits&&<p className="landing-warning">{ja?'このメモリではプリセットの一部を実行できません。カスタムで適合するモデルを選ぶか、モデル設定をスキップしてください。':'Some preset models exceed this memory budget. Choose compatible models in Custom or skip model selection.'}</p>}</div>}
        {customize&&<div className="landing-model-list">
          {MODEL_SLOTS.map(slot=>{
            if(customPurposeRoles.includes(slot.key))return null;
            const options=roleOptions(slot.key).filter((option:ModelOption)=>option.fits_memory&&option.available!==false);
            const value=selectedValue(slot.key,slot.group);
            const selected=options.some(option=>option.id===value)?value:(options[0]?.id||'');
            if(!options.length)return null;
            return <label className="landing-model-row" key={slot.key}>
              <span><strong>{ja?slot.labelJa:slot.labelEn}</strong>{slot.group==='audio'&&slot.key==='read_aloud'?<small>{ja?'現在のLFMモデルを維持します':'Keeps the current LFM model'}</small>:null}</span>
              <select value={selected} disabled={loading||memoryNeedsRecheck} onChange={event=>setRole(slot.key,event.target.value,slot.group)}>
                {options.map(option=><option key={option.id} value={option.id}>{modelLabel(option)}{option.weight_gb?` · ${option.weight_gb} GB`:sizeLabel(option.size_bytes)?` · ${sizeLabel(option.size_bytes)}`:''}{option.downloaded?` · ${ja?'ダウンロード済み':'Downloaded'}`:''}{option.experimental?` · ${ja?'実験的':'Experimental'}`:''}</option>)}
              </select>
            </label>;
          })}
        </div>}
        <div className="landing-actions">
          {!customize&&<button type="button" className="landing-primary" onClick={()=>void saveModelSelection(false)} disabled={loading||memoryNeedsRecheck||!selectedFits}>{ja?'おすすめプリセットで続ける':'Continue with recommended preset'}</button>}
          {customize&&<button type="button" className="landing-primary" onClick={()=>void saveModelSelection(false)} disabled={loading||memoryNeedsRecheck||!selectedFits}>{ja?'カスタム設定で続ける':'Continue with custom models'}</button>}
          {!customize&&<button type="button" onClick={enterCustom} disabled={loading||memoryNeedsRecheck}>{ja?'カスタムでモデルを選ぶ':'Choose models manually'}</button>}
          {customize&&<button type="button" onClick={()=>{setCustomize(false);setRoleModels({...catalog.profile?.brain,...catalog.profile?.roles});setAudioModels({...catalog.profile?.audio})}} disabled={loading||memoryNeedsRecheck}>{ja?'おすすめに戻す':'Use recommended models'}</button>}
          <button type="button" onClick={()=>void saveModelSelection(true)} disabled={loading||memoryNeedsRecheck}>{ja?'モデル選択をスキップ':'Skip model selection'}</button>
        </div>
      </>}
      {stage==='downloads'&&<>
        <div className="landing-intro"><p>{ja?'選択したモデルのほか、OLCRの追加機能に必要なモデルや実行環境を今ダウンロードできます。':'Download the selected models and the model/runtime packages needed by OLCR’s other features now.'}</p>
          {requiredModelIds.length>0&&<StatusBadge state="required">{ja?`選択した設定に必要なモデル ${requiredModelIds.length} 件`:`${requiredModelIds.length} models required by your selection`}</StatusBadge>}
          <p className="landing-download-note">{ja?'容量はモデル分の概算です。追加環境の容量と所要時間はネットワークや構成により変わります。ConvertXのコンテナイメージは機能を有効にすると取得されます。':'The estimate covers model files only. Runtime sizes and download times vary; ConvertX downloads its container image when the feature is enabled.'}</p>
        </div>
        {!individual?<div className="landing-choice-actions">
          <button type="button" className="landing-primary" onClick={()=>void beginDownload('all')} disabled={loading}>{ja?`すべて今ダウンロード（モデル ${modelChoices.length} 件・約 ${(allModelSize/GB).toFixed(1)} GB・追加環境 ${componentChoices.length} 件）`:`Download everything now (${modelChoices.length} models, about ${(allModelSize/GB).toFixed(1)} GB, ${componentChoices.length} runtimes)`}</button>
          <button type="button" onClick={()=>{setIndividual(true);setSelectedModelIds(requiredModelIds);setSelectedComponents([])}} disabled={loading}>{ja?'項目を選んでダウンロード':'Choose items to download'}</button>
          <button type="button" onClick={()=>void finishWithoutDownload()} disabled={loading}>{ja?'後でダウンロード':'Download later'}</button>
        </div>:<>
          <div className="landing-choice-actions compact"><button type="button" onClick={()=>{setSelectedModelIds(modelChoices.map(item=>item.id));setSelectedComponents(componentChoices.map(item=>item.id))}}>{ja?'すべて選択':'Select all'}</button><button type="button" onClick={()=>{setSelectedModelIds(requiredModelIds);setSelectedComponents([])}}>{ja?'選択した設定に必要なものだけ':'Required for selected models only'}</button></div>
          <div className="landing-download-list">
            <h3>{ja?'モデル':'Models'}</h3>
            {modelChoices.length?modelChoices.map(item=><label className="landing-download-row" key={item.id}><input type="checkbox" checked={selectedModelIds.includes(item.id)} onChange={()=>toggleModel(item.id)}/><span><strong>{itemLabel(item)}</strong><small>{[item.runtime,item.license,sizeLabel(item.size_bytes),requiredModelIds.includes(item.id)?(ja?'選択設定に必要':'Required by selection'):null].filter(Boolean).join(' · ')||item.repo||item.id}</small></span></label>):<p>{ja?'未ダウンロードのモデルはありません。':'All model packages are downloaded.'}</p>}
            <h3>{ja?'追加機能の実行環境':'Feature runtimes'}</h3>
            {componentChoices.map(item=>{
              const status=components.find(row=>row.name===item.id);
              return <label className="landing-download-row" key={item.id}><input type="checkbox" checked={selectedComponents.includes(item.id)} onChange={()=>toggleComponent(item.id)}/><span><strong>{ja?item.ja:item.en}</strong><small>{item.id==='convertx'?(ja?'基本ファイルのみ。コンテナイメージは機能を有効にしたとき取得します。':'Base files only; the container image is fetched when enabled.'):status?.message||item.id}</small></span></label>;
            })}
          </div>
          <div className="landing-actions"><button type="button" className="landing-primary" onClick={()=>void beginDownload('selected')} disabled={loading}>{ja?`選択した項目をダウンロード${currentSelectionSize?` · 約 ${(currentSelectionSize/GB).toFixed(1)} GB`:''}`:`Download selected items${currentSelectionSize?` · about ${(currentSelectionSize/GB).toFixed(1)} GB`:''}`}</button><button type="button" onClick={()=>setIndividual(false)}>{ja?'戻る':'Back'}</button><button type="button" onClick={()=>void finishWithoutDownload()}>{ja?'後で':'Later'}</button></div>
        </>}
      </>}
      {stage==='progress'&&<div className="landing-progress">
        <StatusBadge state={job?.status||'downloading'}>{ja?'追加データを準備中':'Preparing downloads'}</StatusBadge>
        <p>{job?.current?`${ja?'現在':'Now'}: ${job.current}`:ja?'ダウンロード項目を確認中…':'Checking download items…'}</p>
        <progress max={Math.max(1,progressItems)} value={Math.min(progressItems,finishedItems)}/>
        <small>{ja?`${finishedItems} / ${progressItems} 項目が完了`:`${finishedItems} of ${progressItems} items complete`}</small>
        <p className="landing-download-note">{ja?'右上の×でいつでも中断できます。進行中のファイルは完了する場合がありますが、後続の項目は開始しません。':'Close with × at any time. An active file may finish, but no later item will start.'}</p>
      </div>}
      {stage==='done'&&<div className="landing-done">
        <StatusBadge state="completed">{ja?'初期設定は完了しました':'Setup is complete'}</StatusBadge>
        {job?.status==='complete'&&<p>{ja?`モデル ${job.completed_models?.length||0} 件、追加環境 ${job.completed_components?.length||0} 件を準備しました。`:`Prepared ${job.completed_models?.length||0} models and ${job.completed_components?.length||0} runtimes.`}</p>}
        {selectionMade&&applied===false&&requiredModelIds.length>0&&<p className="landing-warning">{ja?'選択したモデルに必要なファイルが残っているため、モデル設定はまだ有効になっていません。設定から再開できます。':'The model selection is still pending because some required files were not downloaded. Continue from Settings.'}</p>}
        {selectionMade&&applied===true&&<p>{ja?'モデル設定を適用しました。':'Model selection applied.'}</p>}
        {!selectionMade&&<p>{ja?'モデル設定は変更していません。':'Model settings were left unchanged.'}</p>}
        <div className="landing-actions"><button type="button" className="landing-primary" onClick={()=>closeRef.current()}>{ja?'完了':'Done'}</button></div>
      </div>}
      {loading&&stage!=='models'&&stage!=='progress'&&<p role="status">{ja?'処理中…':'Working…'}</p>}
    </div>
  </DialogFrame>;
}
