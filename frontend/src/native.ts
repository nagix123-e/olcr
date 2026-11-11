type Native = {core:{invoke:<T>(command:string,args?:Record<string,unknown>)=>Promise<T>};event:{listen:<T>(name:string,cb:(event:{payload:T})=>void)=>Promise<()=>void>}};
export function native(){return (window as Window & {__TAURI__?:Native}).__TAURI__}
export async function openExternal(url:string){
 const bridge=native();if(!bridge) { window.open(url,'_blank','noopener,noreferrer'); return; }
 const candidate=url.startsWith('file:///')?url.slice('file://'.length):url;
 let localPath=candidate;
 try{localPath=decodeURIComponent(candidate)}catch{/* Keep the original path so the native handler can return a useful error. */}
 if(/^(?:\/Users\/|\/private\/|\/tmp\/|\/var\/|\/Volumes\/)/.test(localPath))
  return bridge.core.invoke('open_local_file',{path:localPath});
 return bridge.core.invoke('open_external_url',{url});
}
export async function pickPath(directory=false){
 const bridge=native();if(!bridge)throw Error('Native file chooser requires OLCR desktop');
 return bridge.core.invoke<string|null>('plugin:dialog|open',{options:{directory,multiple:false}});
}

export async function transcribeMacOSAudio(audioBase64:string,language:"ja"|"en",onText:(text:string)=>void,sessionId:string,signal:AbortSignal):Promise<string>{
 const bridge=native();if(!bridge)throw Error("MACOS_SPEECH_UNAVAILABLE");
 const unlisten=await bridge.event.listen<{session_id:string;text:string}>("olcr-macos-speech-partial",event=>{
  if(!signal.aborted&&event.payload.session_id===sessionId)onText(event.payload.text);
 });
 try{
  let text:string;
  try{text=await bridge.core.invoke<string>("transcribe_macos_audio",{audioBase64,language,sessionId});}
  catch(error){throw Error(typeof error==="string"?error:error instanceof Error?error.message:"MACOS_SPEECH_RECOGNITION_FAILED")}
  if(signal.aborted)throw Error("VOICE_TRANSCRIPTION_CANCELLED");
  return text;
 }finally{unlisten()}
}
