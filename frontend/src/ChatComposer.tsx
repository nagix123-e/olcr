import {DialogFrame} from './InterfacePrimitives';
import {isAssistantActionRequest} from './assistantIntent';
import {ChatMap as ChatMapData,emptyMap} from './chatMapData';
const MapEditor=lazy(()=>import('./ChatMap').then(module=>({default:module.ChatMap})));
import {PendingInstructions} from './PendingInstructions';
import './message-editor.css';
import {attachmentFromFile,attachmentFromFolderFiles,attachmentFromDirectoryEntry} from './chatAttachments';
import {isAgentCreationRequest} from './agentBuilder';
import {UiIcon} from './UiIcon';
import {FormEvent, Suspense, lazy, useEffect, useRef, useState} from 'react';
import './features.css';
import './concept-diagram.css';
import {ComposerActions} from './ComposerActions';
import {BanterLoader} from './BanterLoader';
import {ConceptDiagram, emptyDiagram, diagramError} from './conceptDiagramData';
const DiagramEditor = lazy(() => import('./ConceptDiagram').then(module => ({default: module.ConceptDiagramEditor})));
const HandsFreePanel = lazy(() => import('./HandsFreePanel').then(module => ({default: module.HandsFreePanel})));
// Keep the hands-free implementation available; hide its UI temporarily.
const HANDS_FREE_UI_VISIBLE = false;
import {api,subscribeCodingTask,audioModelStatus,prepareAudioModel,streamTranscribeAudio} from './api';
import {native,pickPath,transcribeMacOSAudio} from './native';
import {CodingPhaseReport,CodingTask,CodingTaskProgress,shouldAcceptPlanRevision} from './CodingTaskProgress';
import {openGroundedDisclosure} from './computerUse';
import {BrainModelPicker} from './BrainModelPicker';
import {PermissionMenu, PermissionIcon} from './PermissionMenu';
import {GitWorkspacePanel} from './GitWorkspacePanel';
import {CodingWorkControls} from './CodingWorkControls';
import {useLanguage} from "./language";

import {BrainState} from './brainModels';
export type Command = {id:string; command:string; description:string; arguments:string; enabled:boolean};
type InstalledSkill = {name:string;version?:string;description?:string;state:string;enabled:boolean;trusted:boolean;permission?:string;native?:boolean};
export type Attachment={name:string;content:string;mimeType:string;dataUrl?:string;bytesBase64?:string;kind?:'folder';directoryPath?:string;files?:{path:string;bytesBase64:string}[]};

type VoiceCapture={stream:MediaStream;context:AudioContext;source:MediaStreamAudioSourceNode;processor:ScriptProcessorNode;silence:GainNode;chunks:Float32Array[];sampleCount:number;totalSamples:number;quietSamples:number;hasSpeech:boolean;sampleRate:number;language:"ja"|"en"};
function encodePcmWav(chunks:Float32Array[],sampleCount:number,sampleRate:number){
  const source=new Float32Array(sampleCount);let offset=0;for(const chunk of chunks){source.set(chunk,offset);offset+=chunk.length}
  const targetRate=24000,targetLength=Math.max(1,Math.round(source.length*targetRate/sampleRate)),samples=new Int16Array(targetLength);
  for(let i=0;i<targetLength;i++){const position=i*sampleRate/targetRate,left=Math.floor(position),fraction=position-left,a=source[Math.min(left,source.length-1)]||0,b=source[Math.min(left+1,source.length-1)]||0,value=Math.max(-1,Math.min(1,a+(b-a)*fraction));samples[i]=value<0?value*32768:value*32767}
  const buffer=new ArrayBuffer(44+samples.byteLength),view=new DataView(buffer);const write=(at:number,value:string)=>{for(let i=0;i<value.length;i++)view.setUint8(at+i,value.charCodeAt(i))};
  write(0,"RIFF");view.setUint32(4,36+samples.byteLength,true);write(8,"WAVE");write(12,"fmt ");view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,targetRate,true);view.setUint32(28,targetRate*2,true);view.setUint16(32,2,true);view.setUint16(34,16,true);write(36,"data");view.setUint32(40,samples.byteLength,true);for(let i=0;i<samples.length;i++)view.setInt16(44+i*2,samples[i],true);return new Uint8Array(buffer);
}
function bytesToBase64(bytes:Uint8Array){let binary="";for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,Math.min(i+0x8000,bytes.length)));return btoa(binary)}
export function ChatComposer({text,setText,disabled,loading,pause,send,commands,attachment,setAttachment,conversationId,projectId,requestedCodingTask,brainModels,onModelSettings,locationMap,setLocationMap,conceptDiagram,setConceptDiagram,customAgent=false,agentCreation=false,onAgentCreation,onCancelAgentCreation,editMode,editKey,editHasAttachment=false,onCancelEdit,onSubmitEdit,onEditInstruction}:{text:string;setText:(s:string)=>void;disabled:boolean;loading:boolean;pause?:()=>void;send:(e:FormEvent,options?:{coding_followup:'auto'|'chat';coding_task_id?:string})=>void;commands:Command[];attachment?:Attachment|null;setAttachment:(a:Attachment|null)=>void;conversationId?:string|null;projectId?:string|null;requestedCodingTask?:{taskId:string}|null;brainModels?:BrainState;onModelSettings?:()=>void;locationMap?:ChatMapData|null;setLocationMap?:(value:ChatMapData|null)=>void;conceptDiagram?:ConceptDiagram|null;setConceptDiagram?:(value:ConceptDiagram|null)=>void;customAgent?:boolean;agentCreation?:boolean;onAgentCreation?:()=>void;onCancelAgentCreation?:()=>void;editMode?:'message'|'instruction';editKey?:string;editHasAttachment?:boolean;onCancelEdit?:()=>void;onSubmitEdit?:(event:FormEvent)=>void;onEditInstruction?:(taskId:string,item:{id:string;message:string;status:string})=>void}) {
  const {language}=useLanguage();const ja=language==="ja";
  const [diagramOpen,setDiagramOpen]=useState(false);const [mapOpen,setMapOpen]=useState(false);
  useEffect(()=>{if(!locationMap)setMapOpen(false)},[locationMap]);
  const [permissionLevel, setPermissionLevel] = useState<'confirm' | 'automatic' | 'full'>('automatic');
  const [isPermissionMenuOpen, setIsPermissionMenuOpen] = useState(false);
  const [permissionSaving,setPermissionSaving]=useState(false),[permissionError,setPermissionError]=useState('');
  useEffect(()=>{
    let live=true;
    const refresh=()=>{Promise.resolve(api<{level:'confirm'|'automatic'|'full'}>('/permissions')).then(value=>{if(live&&value&&['confirm','automatic','full'].includes(value.level))setPermissionLevel(value.level)}).catch(()=>{})};
    refresh();window.addEventListener('olcr-permissions-changed',refresh);
    return()=>{live=false;window.removeEventListener('olcr-permissions-changed',refresh)};
  },[isPermissionMenuOpen]);
  const savePermissionLevel=async(level:'confirm'|'automatic'|'full')=>{
    if(permissionSaving)return;
    setPermissionSaving(true);setPermissionError('');
    try{
      const result=await api<{level:'confirm'|'automatic'|'full'}>('/permissions',{method:'PUT',body:JSON.stringify({level})});
      if(!result||!['confirm','automatic','full'].includes(result.level))throw Error('PERMISSION_RESPONSE_INVALID');
      setPermissionLevel(result.level);setIsPermissionMenuOpen(false);
      window.dispatchEvent(new Event('olcr-permissions-changed'));window.dispatchEvent(new Event('olcr-command-complete'));
    }catch(error){setPermissionError(error instanceof Error?error.message:String(error))}
    finally{setPermissionSaving(false)}
  };

  useEffect(()=>{if(!conceptDiagram)setDiagramOpen(false)},[conceptDiagram]);
  useEffect(()=>{const open=(event:Event)=>{const value=(event as CustomEvent<ConceptDiagram>).detail;if(value){if(diagramError(value))return;if(conceptDiagram?.nodes.length&&!window.confirm(ja?'編集中の図をこの図に置き換えますか？':'Replace your current diagram draft?'))return;setConceptDiagram?.(value);}setDiagramOpen(true);};window.addEventListener('olcr-edit-concept-diagram',open);return()=>window.removeEventListener('olcr-edit-concept-diagram',open)},[conceptDiagram,setConceptDiagram,ja]);
  useEffect(()=>setDiagramOpen(false),[conversationId]);
  const [closed,setClosed]=useState(false), [selected,setSelected]=useState(0);
  const [codingTask,setCodingTask]=useState<CodingTask|null>(null), [codingTaskId,setCodingTaskId]=useState<string|null>(null);
  const [phaseReports,setPhaseReports]=useState<CodingPhaseReport[]>([]), [progressLoading,setProgressLoading]=useState(false), [progressError,setProgressError]=useState(''), [pausePending,setPausePending]=useState(false), [refreshNonce,setRefreshNonce]=useState(0);
  const [followupMode,setFollowupMode]=useState<'auto'|'queue'|'steer'|'chat'>('auto'),[followupBusy,setFollowupBusy]=useState(false);
  const draftTextRef=useRef(text);draftTextRef.current=text;
  const [instructionNotice,setInstructionNotice]=useState('');
  const followupSending=useRef(false),conversationRef=useRef(conversationId);conversationRef.current=conversationId;
  useEffect(()=>{setFollowupMode('auto');setFollowupBusy(false)},[conversationId]);
  useEffect(()=>{const compose=(event:Event)=>{const detail=(event as CustomEvent<{taskId:string;text:string}>).detail;if(!detail)return;codingTaskIdRef.current=detail.taskId;setCodingTaskId(detail.taskId);setFollowupMode('steer');setText(detail.text);requestAnimationFrame(()=>input.current?.focus())};window.addEventListener('olcr-compose-coding-instruction',compose);return()=>window.removeEventListener('olcr-compose-coding-instruction',compose)},[setText]);
  const input=useRef<HTMLTextAreaElement>(null), fileInput=useRef<HTMLInputElement>(null), folderInput=useRef<HTMLInputElement>(null), composing=useRef(false), codingTaskIdRef=useRef<string|null>(null);
  const attachmentReadEpoch=useRef(0);
  const [attachmentReading,setAttachmentReading]=useState(false);
  const attachmentOwner=useRef({conversationId,disabled,loading,setAttachment});
  attachmentOwner.current={conversationId,disabled,loading,setAttachment};
  function invalidateAttachmentRead(){attachmentReadEpoch.current++;setAttachmentReading(false);if(fileInput.current)fileInput.current.value='';if(folderInput.current)folderInput.current.value='';}
  useEffect(()=>{invalidateAttachmentRead();return()=>{attachmentReadEpoch.current++}},[conversationId]);
  useEffect(()=>{if(loading)invalidateAttachmentRead()},[loading]);
  async function readAttachment(read:()=>Promise<Attachment|null>){
    if(attachmentOwner.current.disabled||attachmentOwner.current.loading)return;
    const owner=attachmentOwner.current;
    const epoch=++attachmentReadEpoch.current;setAttachmentReading(true);setAttachmentError('');
    try{const file=await read();if(epoch===attachmentReadEpoch.current&&owner.conversationId===attachmentOwner.current.conversationId&&!attachmentOwner.current.loading&&file)owner.setAttachment(file)}
    catch(error){if(epoch===attachmentReadEpoch.current)setAttachmentError(error instanceof Error?error.message:String(error))}
    finally{if(epoch===attachmentReadEpoch.current)setAttachmentReading(false)}
  }
  useEffect(()=>{if(!editMode)return;const frame=requestAnimationFrame(()=>{input.current?.focus();input.current?.scrollIntoView?.({block:'nearest'})});return()=>cancelAnimationFrame(frame)},[editMode,editKey]);

  const [attachmentError,setAttachmentError]=useState(''), [dragging,setDragging]=useState(false), [skills,setSkills]=useState<InstalledSkill[]>([]);
  const cancelEdit=()=>{if(loading)return;setAttachmentError('');onCancelEdit?.();requestAnimationFrame(()=>input.current?.focus())};
  const [computerActionBusy,setComputerActionBusy]=useState(false),[computerActionResult,setComputerActionResult]=useState(''),[computerActionError,setComputerActionError]=useState('');
  const [voiceState,setVoiceState]=useState<"idle"|"recording"|"transcribing">("idle"),[voiceError,setVoiceError]=useState("");
  const [speechEngine,setSpeechEngine]=useState<"lfm"|"macos">(()=>localStorage.getItem("olcr-speech-engine")==="macos"?"macos":"lfm");
  useEffect(()=>{const update=()=>setSpeechEngine(localStorage.getItem("olcr-speech-engine")==="macos"?"macos":"lfm");window.addEventListener("olcr-speech-engine-change",update);return()=>window.removeEventListener("olcr-speech-engine-change",update)},[]);
  const [handsFreeOpen,setHandsFreeOpen]=useState(false),[handsFreeActive,setHandsFreeActive]=useState(false),[handsFreeSignal,setHandsFreeSignal]=useState(0);
  const voiceSubmission=useRef<string|null>(null);
  const receiveHandsFreeCommand=(value:string)=>{
    if(disabled||loading){setText(text.trim()?text+'\n'+value:value);setVoiceError(ja?'処理中のため、認識結果を入力欄に保持しました。':'The recognized command has been kept in the draft while processing.');return;}
    if(text.trim()||attachment||conceptDiagram?.nodes.length||locationMap?.points.length){setText(text.trim()?text+'\n'+value:value);setVoiceError(ja?'認識結果を入力欄に追加しました。編集中の内容があるため自動送信を保留しました。':'Voice command added to the draft. Automatic submission paused because you are editing.');return}
    voiceSubmission.current=value;setText(value);
  };
  useEffect(()=>{if(voiceSubmission.current&&text===voiceSubmission.current&&!disabled&&!loading){voiceSubmission.current=null;input.current?.form?.requestSubmit()}},[text,disabled,loading]);
  const [voiceModelPrompt,setVoiceModelPrompt]=useState<{language:"ja"|"en";model:string}|null>(null),[voiceModelChecking,setVoiceModelChecking]=useState(false),[voiceModelDownloading,setVoiceModelDownloading]=useState(false);
  const readyVoiceModels=useRef(new Set<string>()),voiceModelCheckBusy=useRef(false);
  const latestVoiceText=useRef(text);latestVoiceText.current=text;
  const voiceEpoch=useRef(0),pendingVoiceContext=useRef<AudioContext|null>(null);
  function primeVoiceContext(){if(!pendingVoiceContext.current||pendingVoiceContext.current.state==="closed")pendingVoiceContext.current=new AudioContext();void pendingVoiceContext.current.resume().catch(()=>{});}
  function releaseVoiceContext(){const context=pendingVoiceContext.current;pendingVoiceContext.current=null;if(context&&context.state!=="closed")void context.close();}
  const voiceSession=useRef<{id:string;controller:AbortController;queue:Promise<void>;text:string;failed:boolean}|null>(null);
  const voiceCapture=useRef<VoiceCapture|null>(null),stopVoiceRef=useRef<(()=>void)|null>(null);
  const matches=commands.filter(c=>c.enabled && c.command.toLowerCase().startsWith(text.toLowerCase()));
  const exactNoArgumentCommand=commands.some(c=>c.enabled&&!c.arguments&&c.command.toLowerCase()===text.trim().toLowerCase());
  const skillQuery=text.startsWith('/skill')?text.slice('/skill'.length).trim().toLowerCase():'';
  const skillMatches=skills.filter(skill=>skill.state==='ACTIVE'&&skill.enabled&&skill.trusted&&(!skillQuery||skill.name.toLowerCase().includes(skillQuery)));
  const skillOptions=skillMatches.map(skill=>({skill,source:'active' as const}));
  const skillPalette=text==='/skill'||text.startsWith('/skill ');
  // Once a complete no-argument command is typed, Enter should execute it.
  // Keeping its own suggestion open made the first Enter only select the
  // already-entered command, forcing users to press Enter twice.
  const visible=!editMode&&!closed && (skillPalette?skillOptions.length>0:(!exactNoArgumentCommand&&text.startsWith('/')&&matches.length>0));
  useEffect(()=>{setSelected(0)},[text]);
  function queueVoiceSegment(capture:VoiceCapture){
    const session=voiceSession.current;if(!session||!capture.sampleCount)return;
    const chunks=capture.chunks,count=capture.sampleCount,speech=capture.hasSpeech;
    capture.chunks=[];capture.sampleCount=0;capture.quietSamples=0;capture.hasSpeech=false;
    if(!speech||count<capture.sampleRate*0.3)return;
    const audio=bytesToBase64(encodePcmWav(chunks,count,capture.sampleRate));
    session.queue=session.queue.then(async()=>{
      if(voiceSession.current!==session||session.controller.signal.aborted)return;
      const prefix=session.text,join=(value:string)=>prefix+(prefix&&value&&capture.language==="en"?" ":"")+value;
      try{
        const onText=(value:string)=>{
          if(voiceSession.current===session){session.text=join(value);setText(session.text);if(input.current)input.current.scrollTop=input.current.scrollHeight;}
        };
        const result=speechEngine==="macos"
          ?await transcribeMacOSAudio(audio,capture.language,onText,session.id,session.controller.signal)
          :await streamTranscribeAudio(audio,capture.language,onText,session.controller.signal,undefined,conversationId);
        if(voiceSession.current===session)session.text=join(result);
      }catch(error){
        if(voiceSession.current!==session||session.controller.signal.aborted)return;
        session.failed=true;setText(session.text);
        if(error instanceof Error&&error.message.includes("AUDIO_MODEL_NOT_DOWNLOADED"))readyVoiceModels.current.delete(capture.language);
        const code=error instanceof Error?error.message:"";
        const nativeError=code.includes("MACOS_SPEECH_");
        const nativeMessages:Record<string,string>={
          MACOS_SPEECH_PERMISSION_DENIED:ja?"macOSの音声認識が許可されていません。システム設定のプライバシーとセキュリティを確認してください。":"macOS speech recognition permission was denied. Check Privacy & Security in System Settings.",
          MACOS_SPEECH_ON_DEVICE_UNAVAILABLE:ja?"このMacまたは言語ではmacOSのオンデバイス音声認識を利用できません。LFMに切り替えてください。":"macOS on-device speech recognition is unavailable for this Mac or language. Switch to LFM.",
          MACOS_SPEECH_UNAVAILABLE:ja?"macOSの音声認識を現在利用できません。":"macOS speech recognition is currently unavailable.",
          MACOS_SPEECH_HELPER_MISSING:ja?"macOS音声認識コンポーネントがありません。OLCRを再ビルドしてください。":"The macOS speech component is missing. Rebuild OLCR.",
          MACOS_SPEECH_NO_SPEECH:ja?"音声から文字を認識できませんでした。":"No speech was recognized."
        };
        setVoiceError(nativeError?nativeMessages[code]||(ja?"macOSの音声認識に失敗しました。":"macOS speech recognition failed."):(ja?"文字起こしに失敗しました。認識済みの文は保持しています。":"Transcription failed. Recognized text has been preserved."));
      }
    });
  }
  const beginVoiceCapture=async(selectedLanguage:"ja"|"en"=language)=>{
    const epoch=voiceEpoch.current;
    if(!navigator.mediaDevices?.getUserMedia||typeof window.AudioContext==="undefined"){
      setVoiceError(ja?"この環境ではマイク入力を利用できません。":"Microphone input is unavailable in this environment.");return;
    }
    let stream:MediaStream|undefined,context:AudioContext|undefined;
    try{
      context=pendingVoiceContext.current||new AudioContext();pendingVoiceContext.current=null;const resumed=context.resume();void resumed.catch(()=>{});
      stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
      if(epoch!==voiceEpoch.current){stream.getTracks().forEach(track=>track.stop());void context.close();return;}
      await resumed;
      if(epoch!==voiceEpoch.current){stream.getTracks().forEach(track=>track.stop());void context.close();return;}
      const source=context.createMediaStreamSource(stream),processor=context.createScriptProcessor(4096,1,1),silence=context.createGain();silence.gain.value=0;
      const capture:VoiceCapture={stream,context,source,processor,silence,chunks:[],sampleCount:0,sampleRate:context.sampleRate,language:selectedLanguage,totalSamples:0,quietSamples:0,hasSpeech:false};
      voiceSession.current={id:crypto.randomUUID().replaceAll("-",""),controller:new AbortController(),queue:Promise.resolve(),text:latestVoiceText.current.trim(),failed:false};
      processor.onaudioprocess=event=>{
        if(voiceCapture.current!==capture)return;
        const samples=event.inputBuffer.getChannelData(0).slice();capture.chunks.push(samples);capture.sampleCount+=samples.length;capture.totalSamples+=samples.length;
        const rms=Math.sqrt(samples.reduce((sum,value)=>sum+value*value,0)/samples.length);
        if(rms>0.003){capture.hasSpeech=true;capture.quietSamples=0}else capture.quietSamples+=samples.length;
        if(capture.sampleCount>=capture.sampleRate*6||(capture.sampleCount>=capture.sampleRate*1.5&&capture.quietSamples>=capture.sampleRate*0.6))queueVoiceSegment(capture);
        if(capture.totalSamples>=capture.sampleRate*120)void stopVoiceRef.current?.();
      };
      source.connect(processor);processor.connect(silence);silence.connect(context.destination);voiceCapture.current=capture;setVoiceState("recording");
    }catch(error){stream?.getTracks().forEach(track=>track.stop());if(context&&context.state!=="closed")void context.close();if(epoch!==voiceEpoch.current)return;const name=error instanceof Error?error.name:"";setVoiceError(name==="NotAllowedError"||name==="PermissionDeniedError"?(ja?"マイクの使用が許可されていません。macOSのプライバシー設定を確認してください。":"Microphone access was denied. Check macOS privacy settings."):(ja?"音声入力を開始できませんでした。":"Could not start voice input."))}
  };
  const startVoice=async()=>{
    if(voiceState!=="idle"||disabled||voiceModelCheckBusy.current||handsFreeActive)return;
    window.dispatchEvent(new Event("olcr-voice-input-start"));
    setVoiceError("");
    if(!navigator.mediaDevices?.getUserMedia||typeof window.AudioContext==="undefined"){
      setVoiceError(ja?"この環境ではマイク入力を利用できません。":"Microphone input is unavailable in this environment.");return;
    }
    const epoch=voiceEpoch.current;voiceModelCheckBusy.current=true;setVoiceModelChecking(true);
    try{
      // Resume under the button gesture, before waiting for the model API.
      primeVoiceContext();
      if(speechEngine==="lfm"&&!readyVoiceModels.current.has(language)){
        const status=await audioModelStatus(language);
        if(epoch!==voiceEpoch.current)return;
        if(!status.downloaded){setVoiceModelPrompt({language,model:status.model});return;}
        readyVoiceModels.current.add(language);
      }
      if(epoch===voiceEpoch.current)await beginVoiceCapture();
    }catch(error){if(epoch!==voiceEpoch.current)return;releaseVoiceContext();const message=error instanceof Error?error.message:"";const needsUpdate=message.includes("AUDIO_RUNTIME_")||message.includes("HTTP_404");setVoiceError(needsUpdate?(ja?"音声入力を使うには、OLCRを更新してから再起動してください。":"Update and restart OLCR to use voice input."):(ja?"音声モデルを確認できませんでした。":"Could not check the speech model."))}
    finally{voiceModelCheckBusy.current=false;setVoiceModelChecking(false)}
  };
  const confirmVoiceModelDownload=async()=>{
    const pending=voiceModelPrompt;if(!pending||voiceModelDownloading)return;
    const epoch=voiceEpoch.current;setVoiceModelDownloading(true);setVoiceError("");
    try{
      primeVoiceContext();
      await prepareAudioModel(pending.language);readyVoiceModels.current.add(pending.language);setVoiceModelPrompt(null);if(epoch===voiceEpoch.current)await beginVoiceCapture(pending.language);
    }catch{if(epoch!==voiceEpoch.current)return;releaseVoiceContext();setVoiceError(ja?"音声モデルをダウンロードできませんでした。ネットワーク接続を確認してください。":"Could not download the speech model. Check your network connection.");setVoiceModelPrompt(null)}
    finally{setVoiceModelDownloading(false)}
  };
  const stopVoice=async()=>{
    const capture=voiceCapture.current,session=voiceSession.current;if(!capture||!session)return;
    voiceCapture.current=null;capture.processor.onaudioprocess=null;capture.source.disconnect();capture.processor.disconnect();capture.silence.disconnect();capture.stream.getTracks().forEach(track=>track.stop());setVoiceState("transcribing");
    queueVoiceSegment(capture);if(capture.context.state!=="closed")void capture.context.close();
    await session.queue;if(voiceSession.current!==session)return;
    if(!session.text.trim()&&!session.failed)setVoiceError(capture.language==="ja"?"音声から文字を認識できませんでした。":"No speech was recognized.");
    voiceSession.current=null;setVoiceState("idle");input.current?.focus();
  };
  stopVoiceRef.current=()=>{void stopVoice()};
  useEffect(()=>{
    setVoiceState("idle");setVoiceError("");setVoiceModelPrompt(null);
    return()=>{
      voiceEpoch.current++;releaseVoiceContext();
      voiceSession.current?.controller.abort();voiceSession.current=null;
      const capture=voiceCapture.current;voiceCapture.current=null;
      if(capture){capture.processor.onaudioprocess=null;capture.source.disconnect();capture.processor.disconnect();capture.silence.disconnect();capture.stream.getTracks().forEach(track=>track.stop());if(capture.context.state!=="closed")void capture.context.close()}
    };
  },[conversationId,language,speechEngine]);
  // The composer mounts before the Tauri bootstrap has configured the API.
  // Fetching only once at mount races that bootstrap and leaves `/skill`
  // without candidates even though the backend is healthy.  Refresh when the
  // skill palette is actually opened, after the API contract is available.
  useEffect(()=>{
    if(!skillPalette)return;
    let live=true;
    void api<{skills:InstalledSkill[]}>('/skills').then(installed=>{if(live)setSkills(Array.isArray(installed.skills)?installed.skills:[])}).catch(()=>{});
    return()=>{live=false};
  },[skillPalette]);
  useEffect(()=>{const refresh=()=>setRefreshNonce(value=>value+1);window.addEventListener('olcr-command-complete',refresh);return()=>window.removeEventListener('olcr-command-complete',refresh)},[]);
  useEffect(()=>{const rewind=(event:Event)=>{if((event as CustomEvent).detail?.conversationId!==conversationRef.current)return;codingTaskIdRef.current=null;setCodingTaskId(null);setCodingTask(null);setPhaseReports([]);setProgressError('');setPausePending(false);setFollowupMode('auto');setRefreshNonce(value=>value+1)};window.addEventListener('olcr-conversation-rewound',rewind);return()=>window.removeEventListener('olcr-conversation-rewound',rewind)},[]);
  useEffect(()=>{codingTaskIdRef.current=null;setCodingTaskId(null);setCodingTask(null);setPhaseReports([]);setProgressError('');setPausePending(false);setInstructionNotice('')},[conversationId]);
  useEffect(()=>{
    if(!conversationId||requestedCodingTask)return;let live=true,inFlight=false;
    const discover=async()=>{if(inFlight)return;inFlight=true;try{
      const value=await api<{tasks:CodingTask[]}>(`/conversations/${conversationId}/coding-tasks`);if(!live)return;
      const task=(value.tasks||[]).find(item=>!item.archived);if(!task||codingTaskIdRef.current===task.id)return;
      if(codingTaskIdRef.current&&!loading)return;
      codingTaskIdRef.current=task.id;setCodingTaskId(task.id);setCodingTask(task);setPhaseReports([]);setProgressError('');
    }catch(error){if(live)setProgressError(error instanceof Error?error.message:String(error))}finally{inFlight=false}};
    void discover();const timer=loading?window.setInterval(()=>void discover(),1500):undefined;
    return()=>{live=false;if(timer!==undefined)window.clearInterval(timer)};
  },[conversationId,loading,requestedCodingTask]);
  useEffect(()=>subscribeCodingTask(taskId=>{
    // Streamed approval/control replies can publish the same task ID again.
    // Clearing the card without changing codingTaskId leaves the polling effect
    // asleep forever, so refresh the persisted record while retaining the card.
    if(codingTaskIdRef.current===taskId){setRefreshNonce(value=>value+1);return}
    codingTaskIdRef.current=taskId;setCodingTaskId(taskId);setCodingTask(null);setPhaseReports([]);setProgressError('');
  },conversationId),[conversationId]);
  // Apply the explicit project-task selection after subscription's cached
  // latest-task replay, so an older task remains the user's chosen target.
  useEffect(()=>{
    if(!requestedCodingTask)return;
    codingTaskIdRef.current=requestedCodingTask.taskId;setCodingTaskId(requestedCodingTask.taskId);
    setCodingTask(null);setPhaseReports([]);setProgressError('');setPausePending(false);setInstructionNotice('');
    setRefreshNonce(value=>value+1);
  },[conversationId,requestedCodingTask]);

  useEffect(()=>{
    if(!codingTaskId)return;
    let live=true,inFlight=false,timer:number|undefined,requestId=0;
    const refresh=async()=>{
      if(inFlight)return;
      inFlight=true;const current=++requestId;setProgressLoading(true);
      try{
        const [task,reportResult]=await Promise.all([api<CodingTask>(`/coding-tasks/${codingTaskId}`),api<{reports:CodingPhaseReport[]}>(`/coding-tasks/${codingTaskId}/phase-reports`)]);
        if(!live||current!==requestId)return;
        setCodingTask(previous=>shouldAcceptPlanRevision(previous,task)?task:previous);setPhaseReports(Array.isArray(reportResult.reports)?reportResult.reports:[]);setProgressError('');
        if(["QUEUED","PLANNING","RUNNING","FINAL_REPORTING","QWEN_REPLANNING"].includes(task.status))timer=window.setTimeout(refresh,1000);
      }catch(error){if(live&&current===requestId){setProgressError(error instanceof Error?error.message:'Progress unavailable');timer=window.setTimeout(refresh,1000)}}
      finally{if(live&&current===requestId)setProgressLoading(false);inFlight=false}
    };
    void refresh();
    return()=>{live=false;if(timer!==undefined)window.clearTimeout(timer)};
  },[codingTaskId,refreshNonce]);
  // A task can be stopped while it is still queued/planning as well as while
  // a model call is running.  The previous check only treated active model
  // states as stoppable, leaving the visible stop button disabled during the
  // initial queue/planning window.
  const codingActive=Boolean(codingTask&&!codingTask.archived&&["QUEUED","PLANNING","RUNNING","FINAL_REPORTING","QWEN_REPLANNING"].includes(codingTask.status));
  const codingPlanning=Boolean(codingActive&&codingTask&&(codingTask.status==="PLANNING"||codingTask.status==="QWEN_REPLANNING"||(codingTask.status==="QUEUED"&&codingTask.activity==="QWEN_PLANNING")));
  function updateTaskControl(task:CodingTask){if(codingTaskIdRef.current!==task.id)return;setCodingTask(previous=>shouldAcceptPlanRevision(previous,task)?task:previous);setRefreshNonce(value=>value+1)}
  function taskControlError(taskId:string,error:unknown){if(codingTaskIdRef.current===taskId)setProgressError(error instanceof Error?error.message:String(error))}
  const pauseTask=()=>{if(!codingTask||pausePending)return;const taskId=codingTask.id;setPausePending(true);void api<CodingTask>(`/coding-tasks/${taskId}`,{method:'PATCH',body:JSON.stringify({pause_requested:true})}).then(task=>{updateTaskControl(task);if(loading)pause?.()}).catch(error=>taskControlError(taskId,error)).finally(()=>{if(codingTaskIdRef.current===taskId)setPausePending(false)})};
  const resumeTask=()=>{if(codingTask){const taskId=codingTask.id;void api<CodingTask&{control_response?:string}>(`/coding-tasks/${taskId}`,{method:'PATCH',body:JSON.stringify({resume:true})}).then(task=>{if(codingTaskIdRef.current!==taskId)return;updateTaskControl(task);setInstructionNotice(task.control_response||'')}).catch(error=>taskControlError(taskId,error))}};
  const dismissTask=()=>{if(codingTask){const taskId=codingTask.id;void api<CodingTask>(`/coding-tasks/${taskId}/console`,{method:'PUT',body:JSON.stringify({hidden:true})}).then(task=>{if(codingTaskIdRef.current!==taskId)return;updateTaskControl(task);setInstructionNotice(ja?'履歴を保存しました。「再開して」でこのタスクに戻れます。':'History saved. Say “resume” to return to this task.')}).catch(error=>taskControlError(taskId,error))}};
  const archiveTask=()=>{if(codingTask){const taskId=codingTask.id;void api<CodingTask>(`/coding-tasks/${taskId}`,{method:'PATCH',body:JSON.stringify({archived:true})}).then(updateTaskControl).catch(error=>taskControlError(taskId,error))}};
  function insertActionPrompt(kind:'scrape'|'gui-e2e'){
    const prompt=kind==='scrape'
      ?(ja?'スクレイピングを開始してください。\n対象URL: [URL]\n抽出する項目: [項目]':'Please scrape the following page.\nTarget URL: [URL]\nFields to extract: [fields]')
      :(ja?'「[アプリ名]」を対象にしてコンピュータユーズで実GUI E2Eを開始して。\n確認する操作・期待結果: [確認内容]':'Start real GUI E2E with Computer Use for "[app name]".\nActions and expected results: [details]');
    const next=text.trim()?prompt+'\n\n'+text:prompt;
    setText(next);setClosed(true);
    requestAnimationFrame(()=>{const field=input.current;if(!field)return;field.focus();const placeholder=next.match(/\[[^\]]+\]/);if(placeholder&&placeholder.index!==undefined)field.setSelectionRange(placeholder.index,placeholder.index+placeholder[0].length);});
  }
  function choose(c:Command){setText(c.command+(c.arguments?' ':''));setClosed(true);input.current?.focus()}
  function chooseSkill(skill:InstalledSkill){setText(`/skill ${skill.name} `);setClosed(true);input.current?.focus()}
  const clearAttachment=()=>{invalidateAttachmentRead();setAttachment(null);setAttachmentError('')};
  function submitTaskInstruction(mode:'queue'|'steer'){
    if(followupSending.current||!codingTask||codingTask.archived||!conversationId||!text.trim()||disabled||attachment||conceptDiagram?.nodes.length||locationMap?.points.length||voiceState!=="idle")return;
    const requestConversation=conversationId,message=text;followupSending.current=true;setFollowupBusy(true);setAttachmentError('');setInstructionNotice('');
    void api<{coding_task_id?:string}>('/chat',{method:'POST',body:JSON.stringify({conversation_id:conversationId,message,coding_task_id:codingTask.id,coding_followup:mode,message_id:crypto.randomUUID()})})
      .then(result=>{if(conversationRef.current!==requestConversation)return;if(draftTextRef.current===message)setText('');if(result.coding_task_id){codingTaskIdRef.current=result.coding_task_id;setCodingTaskId(result.coding_task_id)}setRefreshNonce(value=>value+1);setInstructionNotice(mode==='steer'?(ja?'この指示を次の工程・計画の区切りで反映します。':'This instruction will be applied at the next phase or planning boundary.'):(ja?'現在の作業が完了してから進めます。':'Queued until the current work completes.'));window.dispatchEvent(new Event('olcr-command-complete'))})
      .catch(reason=>{if(conversationRef.current===requestConversation)setAttachmentError(reason instanceof Error?reason.message:String(reason))})
      .finally(()=>{followupSending.current=false;if(conversationRef.current===requestConversation)setFollowupBusy(false)});
  }
  function handleSubmit(event:FormEvent){
    event.preventDefault();
    if(attachmentReading||disabled||followupBusy)return;
    invalidateAttachmentRead();
    if(editMode){event.preventDefault();if(!loading&&!disabled&&text.trim())onSubmitEdit?.(event);return;}

    if(agentCreation||isAgentCreationRequest(text)){send(event);return;}
    const hands=text.trim().match(/^(?:\/handsfree\s+(on|off)|ハンズフリー(?:操作|モード)?(?:を)?(オン|オフ|停止|有効|無効)(?:にして|にする|して)?)$/i);
    if(hands){event.preventDefault();const enabled=/^(on|オン|有効)$/i.test(hands[1]||hands[2]);setHandsFreeOpen(enabled);if(!enabled)setHandsFreeSignal(0);if(enabled)setHandsFreeSignal(value=>value+1);setText('');return;}
    if(voiceState!=="idle"){event.preventDefault();return;}
    setAttachmentError('');
    if(isAssistantActionRequest(text)&&!attachment&&!conceptDiagram?.nodes.length&&!locationMap?.points.length){send(event,{coding_followup:'chat'});return;}
    if(codingTask&&!codingTask.archived&&conversationId&&text.trim()&&!text.startsWith('/')&&!attachment&&!conceptDiagram?.nodes.length&&!locationMap?.points.length){
      if(followupMode==='auto'||followupMode==='chat'){send(event,{coding_followup:followupMode,...(followupMode==='auto'?{coding_task_id:codingTask.id}:{})});return;}
      event.preventDefault();submitTaskInstruction(followupMode);
      return;
    }
    send(event);
  }
  const [workingFolder, setWorkingFolder] = useState<{id:string;name:string;root:string}|null>(null);
  useEffect(() => {
    let live = true;
    setWorkingFolder(null);
    if (conversationId) void api(`/conversations/${conversationId}/working-folder`).then(value => {
      if (live) setWorkingFolder(value?.id ? value : null);
    }).catch(() => {});
    return () => {live = false;};
  }, [conversationId, attachment]);
  async function detachWorkingFolder() {
    const folderId = workingFolder?.id;
    try {await api(`/conversations/${conversationId}/working-folder`, {method:'DELETE'}); setWorkingFolder(current => current?.id === folderId ? null : current);}
    catch (error) {setAttachmentError(error instanceof Error ? error.message : String(error));}
  }
  async function attachFolder() {
    setAttachmentError('');
    const bridge = native();
    if (!bridge) {folderInput.current?.click(); return;}
    await readAttachment(async()=>{
      const path = await pickPath(true);
      return path?bridge.core.invoke<Attachment>('read_dropped_file', {path}):null;
    });
  }
  function dropAttachment(event: React.DragEvent<HTMLFormElement>) {
    event.preventDefault(); setDragging(false);
    // The desktop uses native paths; the DOM File for a folder is not readable.
    if (native()) return;
    const entry = event.dataTransfer.items?.[0]?.webkitGetAsEntry?.();
    if (entry?.isDirectory) {
      void readAttachment(()=>attachmentFromDirectoryEntry(entry as FileSystemDirectoryEntry));
    } else handleFile(event.dataTransfer.files[0]);
  }
  const handleFile=(file:File|undefined)=>{if(file)void readAttachment(()=>attachmentFromFile(file))};
  useEffect(()=>{
    const bridge=native();
    if(!bridge?.event)return;
    let live=true;
    const subscription=bridge.event.listen<{paths?:string[]}>('tauri://drag-drop',event=>{
      const path=event.payload.paths?.[0];
      if(!path)return;
      if(live)void readAttachment(()=>bridge.core.invoke<Attachment>('read_dropped_file',{path}));
    });
    return()=>{live=false;void subscription.then(unlisten=>unlisten())};
  },[]);
  useEffect(()=>{
    const form=input.current?.form;
    if(!form)return;
    const intercept=(event:Event)=>{
      const match=text.trim().match(/^\/computer\s+open\s+(.+)$/i);
      if(!match||conceptDiagram||customAgent)return;
      event.preventDefault();event.stopImmediatePropagation();
      if(computerActionBusy)return;
      if(!conversationId){setAttachmentError('ACTIVE_CONVERSATION_REQUIRED');return}
      const projectId=document.documentElement.dataset.olcrActiveProjectId;
      if(!projectId){setAttachmentError('SEMANTIC_ACTION_PROJECT_NOT_AVAILABLE');return}
      setComputerActionBusy(true);setAttachmentError('');
      void openGroundedDisclosure(`Open ${match[1].trim()}`,projectId,conversationId||null,api)
        .then(()=>{setText('');setAttachmentError('');window.dispatchEvent(new Event('olcr-command-complete'))})
        .catch(error=>setAttachmentError(`Semantic action failed: ${error instanceof Error?error.message:String(error)}`))
        .finally(()=>setComputerActionBusy(false));
    };
    form.addEventListener('submit',intercept,true);
    return()=>form.removeEventListener('submit',intercept,true);
  },[text,conversationId,computerActionBusy,setText,conceptDiagram,customAgent]);
  const hasContent=Boolean(text.trim()||attachment||conceptDiagram?.nodes.length||(!editMode&&locationMap?.points.length)),showingLoader=!editMode&&(loading||codingActive)&&!hasContent,voiceRecording=voiceState==="recording",voiceTranscribing=voiceState==="transcribing";
  return <>{codingTask&&!codingTask.archived&&<PendingInstructions task={codingTask} ja={ja} onEdit={item=>onEditInstruction?.(codingTask.id,item)}/>} {HANDS_FREE_UI_VISIBLE&&<div className="chat-utility-controls"><CodingWorkControls projectId={projectId} conversationId={conversationId} selectedId={codingTaskId} ja={ja} onSelect={taskId=>{codingTaskIdRef.current=taskId;setCodingTaskId(taskId);setCodingTask(null);setPhaseReports([]);setRefreshNonce(value=>value+1)}}/>{!codingTaskId&&<GitWorkspacePanel conversationId={conversationId} ja={ja}/>}<div className="handsfree-mode-control"><span className="handsfree-mode-label">{ja?"ハンズフリーモード":"Hands-free mode"}</span><button type="button" role="switch" aria-checked={handsFreeActive} className="handsfree-mode-switch" aria-label={ja?"ハンズフリーモード":"Hands-free mode"} disabled={voiceState!=="idle"||(!handsFreeActive&&(disabled||loading||codingActive))} onClick={()=>{if(handsFreeActive){setHandsFreeSignal(0);setHandsFreeOpen(false)}else{setHandsFreeOpen(true);setHandsFreeSignal(value=>value+1)}}}><span className="handsfree-switch-track" aria-hidden="true"><span className="handsfree-switch-thumb"/></span><span className="handsfree-switch-state">{handsFreeActive?(ja?"オン":"On"):(ja?"オフ":"Off")}</span></button></div></div>}{codingTask&&!codingTask.archived&&!codingTask.console_hidden&&<CodingTaskProgress conversationId={conversationId} task={codingTask} reports={phaseReports} loading={progressLoading} error={progressError} pausePending={pausePending} onPause={pauseTask} onResume={resumeTask} onArchive={archiveTask} onDismiss={dismissTask} activityLabel={codingActive?(codingTask?.status==='QUEUED'?(ja?'コーディングタスクの開始待ち':'Waiting to start Coding Task'):codingTask?.status==='FINAL_REPORTING'?(ja?'最終結果を作成中':'Preparing final result'):(ja?'コーディングタスクを実行中':'Running Coding Task')):undefined}/>}  {codingTaskId&&!codingTask&&progressError&&<p className="coding-task-unavailable" role="alert">Coding Orchestrator の進行状況を取得できませんでした。再度メッセージを開いてください。</p>}{HANDS_FREE_UI_VISIBLE&&handsFreeOpen&&<Suspense fallback={<p role="status">{ja?"ハンズフリー操作を開いています…":"Opening hands-free input…"}</p>}><HandsFreePanel language={language} busy={disabled||loading||codingActive||voiceState!=="idle"} onCommand={receiveHandsFreeCommand} onActive={active=>{setHandsFreeActive(active);if(!active)voiceSubmission.current=null}} onClose={()=>{setHandsFreeSignal(0);setHandsFreeOpen(false)}} startSignal={handsFreeSignal}/></Suspense>}<form className={dragging?'composer dragging':'composer'} onSubmit={handleSubmit} onDragOver={e=>{e.preventDefault();e.dataTransfer.dropEffect='copy';setDragging(true)}} onDragLeave={e=>{if(e.currentTarget===e.target)setDragging(false)}} onDrop={dropAttachment}>
    {visible&&<div className="command-palette" role="listbox" id="commands" aria-label={ja?(skillPalette?'Skills':'OLCRコマンド'):(skillPalette?'Skills':'OLCR commands')}>{skillPalette?skillOptions.map(({skill,source},i)=><button type="button" role="option" id={'command-'+i} aria-selected={i===selected} className={i===selected?'selected':''} key={`${source}-${skill.name}`} onMouseDown={e=>e.preventDefault()} onClick={()=>chooseSkill(skill)}><strong>/skill {skill.name}</strong><small>{skill.description||(ja?'このスキルを使って作業・回答する':'Use this skill for work or answers')}</small></button>):matches.map((c,i)=><button type="button" role="option" id={'command-'+i} aria-selected={i===selected} className={i===selected?'selected':''} key={c.id} onMouseDown={e=>e.preventDefault()} onClick={()=>choose(c)}><strong>{c.command}</strong><small>{c.command.startsWith("/coding-orchestrator ")?(ja?`コーディングオーケストレーター ${c.command.endsWith(" on")?"オン":"オフ"}`:c.description):c.description}</small></button>)}</div>}
    {!editMode&&locationMap&&setLocationMap&&<>{mapOpen?<Suspense fallback={<p role="status">Loading map…</p>}><MapEditor value={locationMap} onChange={setLocationMap} onClose={()=>setMapOpen(false)} disabled={disabled||loading||codingActive}/></Suspense>:<button type="button" className="diagram-draft-chip" onClick={()=>setMapOpen(true)}>{ja?'地点マップ':'Location map'} · {locationMap.points.length}/2</button>}<button type="button" className="map-detach" disabled={disabled||loading} onClick={()=>setLocationMap(null)}>{ja?'地図を取り外す':'Detach map'}</button></>}
    {diagramOpen&&conceptDiagram&&setConceptDiagram&&<Suspense fallback={<p role="status">{ja?'概念図を開いています…':'Opening diagram…'}</p>}><DiagramEditor value={conceptDiagram} onChange={setConceptDiagram} onClose={()=>setDiagramOpen(false)} disabled={disabled||loading||codingActive}/></Suspense>}
    {editMode&&<div className="composer-edit-status" role="region" aria-label={ja?'編集中':'Editing'}><div><strong><UiIcon name="edit" size={16}/>{editMode==='message'?(ja?'送信済みメッセージを編集中':'Editing a sent message'):(ja?'保留中の追加指示を編集中':'Editing a pending instruction')}</strong><p>{editMode==='message'?(ja?'再送すると、このメッセージ以降の会話を置き換えます。':'Resending replaces this message and the following conversation.'):(ja?'保存するまで保留中の指示は変わりません。':'The pending instruction stays unchanged until saved.')}</p>{editHasAttachment&&<p>{ja?'元の添付を使う場合は、＋からファイルを選び直してください。':'To include the original attachment, reselect it from +.'}</p>}</div><button type="button" className="composer-edit-cancel" disabled={loading} onClick={cancelEdit}><UiIcon name="close" size={16}/>{ja?'キャンセル':'Cancel'}</button></div>}
    <div className="composer-entry">
    {attachmentReading&&<p className="attachment-chip" role="status">{ja?'添付ファイルを読み込み中…':'Reading attachment…'}</p>}
    <input ref={folderInput} type="file" aria-label={ja?'作業フォルダを添付':'Attach working folder'} hidden {...({webkitdirectory: ''} as Record<string,string>)} onChange={e=>{const files=Array.from(e.currentTarget.files||[]);e.currentTarget.value='';setAttachmentError('');void readAttachment(()=>attachmentFromFolderFiles(files))}}/>
    <input ref={fileInput} type="file" id="olcr-attachment" hidden onChange={e=>{handleFile(e.currentTarget.files?.[0]);e.currentTarget.value=''}}/><div className="composer-input">{workingFolder&&attachment?.kind!=='folder'&&<div className="attachment-chip" role="status" title={workingFolder.root}>{ja?'作業フォルダ: ':'Working folder: '}{workingFolder.name}<button type="button" disabled={disabled||loading} aria-label={ja?'作業フォルダを解除':'Detach working folder'} title={ja?'今後の依頼から外します。実行中のタスクの作業対象は維持します。':'Detach for future requests. Running tasks keep their working files.'} onClick={()=>void detachWorkingFolder()}>×</button></div>}{agentCreation&&!editMode&&<div className="attachment-chip" role="status"><UiIcon name="agent"/>{ja?'カスタムエージェント作成モード':'Custom agent creation mode'}<button type="button" disabled={loading} aria-label={ja?'エージェント作成を中止':'Cancel agent creation'} onClick={onCancelAgentCreation}>×</button></div>}{!diagramOpen&&Boolean(conceptDiagram?.nodes.length)&&<button type="button" className="diagram-draft-chip" onClick={()=>setDiagramOpen(true)}>{ja?"概念図のドラフト":"Diagram draft"} · {conceptDiagram?.nodes.length} {ja?"ボックス":"boxes"}</button>}{attachment&&<div className="attachment-chip" title={attachment.name} role="status"><UiIcon name="paperclip" size={14}/>{attachment.kind==='folder'?(ja?'作業フォルダ: ':'Working folder: '):''}{attachment.name}<button type="button" aria-label={ja?"添付ファイルを削除":"Remove attachment"} onClick={clearAttachment}>×</button></div>}{attachmentError&&<div className="attachment-error" role="alert" aria-live="polite">{attachmentError}</div>}{voiceError&&<div className="attachment-error voice-error" role="alert" aria-live="polite">{voiceError}</div>}{voiceRecording&&<div className="voice-model-status" role="status">{ja?"録音中…認識した文を順次入力します。":"Listening… recognized text appears as you speak."}</div>}{voiceTranscribing&&<div className="voice-model-status" role="status">{ja?"残りの音声を文字起こし中…":"Transcribing the remaining audio…"}</div>}{voiceModelChecking&&<div className="voice-model-status" role="status" aria-live="polite">{ja?"音声モデルを確認中…":"Checking speech model…"}</div>}<textarea ref={input} maxLength={editMode==='instruction'?20000:editMode==='message'?100000:undefined} aria-label={ja?"メッセージ":"Message"} aria-controls={visible?'commands':undefined} aria-activedescendant={visible?'command-'+selected:undefined} placeholder={ja?"OLCRに質問…":"Ask OLCR…"} value={text} disabled={disabled||followupBusy||Boolean(editMode&&loading)} readOnly={voiceRecording||voiceTranscribing}
      onPaste={e=>{const image=Array.from(e.clipboardData.items).find(item=>item.type.startsWith('image/'))?.getAsFile()||e.clipboardData.files[0];if(image){e.preventDefault();handleFile(image)}}}
      onCompositionStart={()=>{composing.current=true}} onCompositionEnd={()=>{composing.current=false}}
      onChange={e=>{setText(e.target.value);setClosed(false)}} onKeyDown={e=>{
        if(composing.current||e.nativeEvent.isComposing||e.keyCode===229)return;
        if(editMode&&e.key==='Escape'&&!loading){e.preventDefault();cancelEdit();return}
        if(visible&&['ArrowDown','ArrowUp','Escape'].includes(e.key)){e.preventDefault();if(e.key==='Escape')setClosed(true);else {const count=skillPalette?skillOptions.length:matches.length;setSelected(i=>(i+(e.key==='ArrowDown'?1:-1)+count)%count)}return}
        if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(e.repeat)return;if(visible){if(skillPalette){const option=skillOptions[selected]||skillOptions[0];if(option)chooseSkill(option.skill)}else choose(matches[selected]||matches[0])}else if(!disabled&&(text.trim()||attachment||conceptDiagram?.nodes.length))handleSubmit(e)}
    }}/><div className="permission-control"><button type="button" className="permission-toggle" aria-label={ja ? "権限設定" : "Permission settings"} aria-haspopup="dialog" aria-expanded={isPermissionMenuOpen} onClick={() => setIsPermissionMenuOpen(value=>!value)} title={ja ? "権限設定" : "Permission settings"}><PermissionIcon/></button><PermissionMenu isOpen={isPermissionMenuOpen} level={permissionLevel} setLevel={savePermissionLevel} saving={permissionSaving} error={permissionError} onClose={() => setIsPermissionMenuOpen(false)} ja={ja}/></div>{brainModels&&<BrainModelPicker state={brainModels} onSettings={onModelSettings}/>}</div><ComposerActions onMap={editMode||agentCreation?undefined:()=>{if(!locationMap)setLocationMap?.(emptyMap());setMapOpen(true)}} onAgentCreate={editMode?undefined:onAgentCreation} onPrompt={insertActionPrompt} disabled={disabled||loading||codingActive||voiceRecording||voiceTranscribing} onFolder={()=>void attachFolder()} onFile={()=>fileInput.current?.click()} onDiagram={()=>{if(!conceptDiagram)setConceptDiagram?.(emptyDiagram());setDiagramOpen(true)}}/><button type={editMode?"submit":!voiceRecording&&!voiceTranscribing&&!showingLoader&&hasContent?"submit":"button"} onClick={editMode?undefined:voiceRecording?()=>void stopVoice():voiceTranscribing?undefined:showingLoader?(codingActive?pauseTask:()=>pause?.()):hasContent?undefined:()=>void startVoice()} aria-label={editMode?(loading?(ja?"保存中…":"Saving…"):editMode==='message'?(ja?"保存して再送":"Save and resend"):(ja?"変更を保存":"Save changes")):showingLoader?(codingActive?(ja?"コーディングタスクを一時停止":"Pause Coding Task"):(ja?"生成を停止":"Stop generating")):voiceRecording?(ja?"音声入力を停止":"Stop voice input"):voiceTranscribing?(ja?"文字起こし中":"Transcribing"):voiceModelChecking?(ja?"音声モデルを確認中":"Checking speech model"):hasContent?(ja?"送信":"Send"):(ja?"音声入力を開始":"Start voice input")} title={editMode?(editMode==='message'?(ja?"保存して再送":"Save and resend"):(ja?"変更を保存":"Save changes")):showingLoader?(codingActive?(ja?"コーディングタスクを一時停止":"Pause Coding Task"):(ja?"生成を停止":"Stop generating")):voiceRecording?(ja?"音声入力を停止":"Stop voice input"):voiceTranscribing?(ja?"文字起こし中":"Transcribing"):voiceModelChecking?(ja?"音声モデルを確認中":"Checking speech model"):hasContent?(ja?"送信":"Send"):(ja?"音声入力を開始":"Start voice input")} className={showingLoader?"send-button loading":"send-button"} disabled={attachmentReading||(editMode?(disabled||loading||followupBusy||!text.trim()):followupBusy|| (showingLoader?(codingActive?pausePending:!pause):voiceTranscribing||voiceModelChecking?true:disabled||(handsFreeActive&&!hasContent)))}>{editMode?<UiIcon name={editMode==='message'?"refresh":"check"} size={18}/>:showingLoader?<span className="loading-square" aria-hidden="true"/>:voiceTranscribing?<span className="voice-transcribing" aria-hidden="true"/>:voiceRecording?<span className="loading-square" aria-hidden="true"/>:hasContent?"↑":<span className="voice-bars" aria-hidden="true"><i/><i/><i/><i/></span>}</button>
    </div>
    {!editMode&&codingTask&&!codingTask.archived&&<div className="coding-followup-controls"><label className="coding-followup-mode">{ja?'送信先':'Send to'} <select aria-label={ja?'追加指示の扱い':'Follow-up behavior'} value={followupMode} disabled={followupBusy} onChange={event=>setFollowupMode(event.target.value as 'auto'|'queue'|'steer'|'chat')}><option value="auto">{ja?'自動で振り分け':'Automatic'}</option><option value="queue">{codingActive?(ja?'完了後に進める':'Queue after completion'):(ja?'このTaskを続ける':'Continue this task')}</option><option value="steer">{ja?'今の作業へ反映':'Steer current work'}</option><option value="chat">{ja?'通常のチャット':'Ordinary chat'}</option></select></label>{codingActive&&<button type="button" className="one-off-steer" title={ja?'送信先の設定を変えずに、この1件を次の工程・計画の区切りで反映します':'Apply this message at the next phase or planning boundary without changing the send mode'} disabled={disabled||loading||followupBusy||!text.trim()||Boolean(attachment)||Boolean(conceptDiagram?.nodes.length)||voiceState!=="idle"} onClick={()=>submitTaskInstruction('steer')}><UiIcon name="steer" size={16}/>{ja?'今の作業へ送る':'Send to current work'}</button>}</div>}
    {instructionNotice&&!editMode&&<p className="instruction-notice" role="status">{instructionNotice}</p>}
  </form>{voiceModelPrompt&&<DialogFrame title={ja?"音声入力モデルをダウンロードしますか？":"Download the voice input model?"} onClose={()=>{setVoiceModelPrompt(null);releaseVoiceContext()}} closeDisabled={voiceModelDownloading}><section className="voice-model-consent"><p id="voice-model-description">{ja?"音声入力を使うには、このモデルをダウンロードしてこのデバイスに保存します。":"Voice input needs this model downloaded and stored on this device."}</p><code>{voiceModelPrompt.model}</code><p className="muted">{ja?"ダウンロードは初回のみです。約3.7 GBの空き容量が必要です。":"This is a one-time download. About 3.7 GB of free space is required."}</p>{voiceModelDownloading&&<p className="voice-model-status" role="status" aria-live="polite">{ja?"モデルをダウンロード中です。完了後、録音を開始します…":"Downloading the model. Recording will start when it is ready…"}</p>}<footer><button type="button" disabled={voiceModelDownloading} onClick={()=>{setVoiceModelPrompt(null);releaseVoiceContext()}}>{ja?"キャンセル":"Cancel"}</button><button type="button" className="primary" disabled={voiceModelDownloading} onClick={()=>void confirmVoiceModelDownload()}>{voiceModelDownloading?(ja?"ダウンロード中…":"Downloading…"):(ja?"同意してダウンロード":"Agree and download")}</button></footer></section></DialogFrame>}</>
}
