import {useEffect,useRef,useState} from 'react';
import {api,streamTranscribeAudio} from './api';
import {native} from './native';
import {microphone,pcmWav} from './handsfreeAudio';
import './handsfree.css';

type Profile={label:string;count:number;threshold:number};
type Status={phase:string;enabled:boolean;installed:boolean;session:string|null;downloaded_bytes:number;profile:Profile;error?:string};
type Mode='off'|'preparing'|'enroll'|'recording'|'wake'|'command'|'transcribing'|'waiting';
export function HandsFreePanel({language,busy,onCommand,onActive,onClose,startSignal=0}:{language:'ja'|'en';busy:boolean;onCommand:(text:string)=>void;onActive:(active:boolean)=>void;onClose:()=>void;startSignal?:number}) {
  const ja=language==='ja', [mode,setMode]=useState<Mode>('off'),[status,setStatus]=useState<Status|null>(null),[error,setError]=useState(''),[label,setLabel]=useState('OLCR'),[camera,setCamera]=useState(false),[gesture,setGesture]=useState(''),[caption,setCaption]=useState('');
  const modeRef=useRef<Mode>('off'),session=useRef<string|null>(null),epoch=useRef(0),stopMic=useRef<(()=>void)|null>(null),cameraStream=useRef<MediaStream|null>(null),nativeToken=useRef<string|null>(null);
  const video=useRef<HTMLVideoElement>(null),overlay=useRef<HTMLCanvasElement>(null),poll=useRef<ReturnType<typeof setInterval>|null>(null),cameraTimer=useRef<ReturnType<typeof setTimeout>|null>(null),abort=useRef<AbortController|null>(null);
  const chunks=useRef<Float32Array[]>([]),rate=useRef(48000),total=useRef(0),quiet=useRef(0),speech=useRef(false),lastWake=useRef(0),wakeBusy=useRef(false),sampleBusy=useRef(false),busyRef=useRef(busy),commandRef=useRef(onCommand);
  const pendingAudio=useRef<AudioContext|null>(null),cameraStarting=useRef(false);
  const liveAudio=useRef<AudioContext|null>(null);
  const [wantCamera,setWantCamera]=useState(Boolean(native())),wantCameraRef=useRef(wantCamera);wantCameraRef.current=wantCamera;
  busyRef.current=busy;commandRef.current=onCommand;
  const change=(value:Mode)=>{modeRef.current=value;setMode(value);chunks.current=[];total.current=quiet.current=0;speech.current=false};
  const post=(value:Record<string,unknown>,signal?:AbortSignal)=>api<any>('/handsfree/input',{method:'POST',signal,body:JSON.stringify({...value,session:session.current})});
  const releaseCamera=()=>{
    if(cameraTimer.current)clearTimeout(cameraTimer.current);cameraTimer.current=null;
    cameraStream.current?.getTracks().forEach(track=>track.stop());cameraStream.current=null;
    if(video.current)video.current.srcObject=null;
    const token=nativeToken.current;nativeToken.current=null;if(token)void native()?.core.invoke('end_human_input',{token});
    setCamera(false);setGesture('');
  };
  const stop=()=>{
    epoch.current++;const old=session.current;session.current=null;
    if(poll.current)clearInterval(poll.current);poll.current=null;
    abort.current?.abort();abort.current=null;stopMic.current?.();stopMic.current=null;
    liveAudio.current=null;wakeBusy.current=false;sampleBusy.current=false;
    if(pendingAudio.current&&pendingAudio.current.state!=='closed')void pendingAudio.current.close();pendingAudio.current=null;
    releaseCamera();change('off');onActive(false);
    if(old)void api('/handsfree/disable',{method:'POST',body:JSON.stringify({session:old})}).catch(()=>{});
  };
  useEffect(()=>()=>stop(),[language]);
  useEffect(()=>{if(!busy&&modeRef.current==='waiting'){lastWake.current=performance.now();change('wake')}},[busy]);

  const commandFinished=async(audio:string,generation:number)=>{
    change('transcribing');setCaption('');const controller=new AbortController();abort.current=controller;
    try{
      const result=await streamTranscribeAudio(audio,language,value=>{if(generation===epoch.current)setCaption(value)},controller.signal,session.current||undefined);
      if(generation!==epoch.current)return;
      const text=result.trim();
      if(/^(?:ハンズフリー(?:操作)?(?:を)?(?:停止|オフ)(?:にして|にする|して)?|hands[ -]?free off|stop hands[ -]?free)[。.!]?$/i.test(text)){stop();return}
      if(text){change('waiting');commandRef.current(text);setTimeout(()=>{if(generation===epoch.current&&!busyRef.current&&modeRef.current==='waiting'){lastWake.current=performance.now();change('wake')}},1500)}
      else {lastWake.current=performance.now();change('wake')}
    }catch(e){if(generation===epoch.current&&!controller.signal.aborted){setError(String(e));stop()}}
  };

  const samples=(chunk:Float32Array,sampleRate:number)=>{
    const current=modeRef.current;if(!session.current||['off','preparing','transcribing','waiting','enroll'].includes(current))return;
    rate.current=sampleRate;chunks.current.push(chunk);total.current+=chunk.length;
    const rms=Math.sqrt(chunk.reduce((sum,value)=>sum+value*value,0)/chunk.length);
    if(rms>.008){speech.current=true;quiet.current=0}else quiet.current+=chunk.length;
    const generation=epoch.current;
    if(current==='recording'&&total.current>=sampleRate*3&&!sampleBusy.current){
      const audio=pcmWav(chunks.current,sampleRate);change('enroll');sampleBusy.current=true;
      void post({op:'enroll',label,audio}).then(result=>{if(generation!==epoch.current)return;setStatus(old=>old?{...old,profile:result.profile}:old);if(result.profile.count>=3){lastWake.current=performance.now();change('wake')}}).catch(e=>{if(generation===epoch.current)setError(String(e))}).finally(()=>{if(generation===epoch.current)sampleBusy.current=false});return;
    }
    if(current==='command'){
      if((speech.current&&quiet.current>sampleRate*.85&&total.current>sampleRate*1.2)||total.current>sampleRate*20){
        if(speech.current)void commandFinished(pcmWav(chunks.current,sampleRate),generation);else change('wake');
      }return;
    }
    // Only the wake detector consumes audio in standby. It is completely gated during LFM capture/transcription.
    while(total.current>sampleRate*2.2&&chunks.current.length>1){total.current-=chunks.current.shift()!.length}
    if(total.current<sampleRate*1.8||wakeBusy.current||busyRef.current||performance.now()-lastWake.current<350)return;
    lastWake.current=performance.now();wakeBusy.current=true;
    void post({op:'wake',audio:pcmWav(chunks.current,sampleRate)}).then(result=>{
      if(generation!==epoch.current||modeRef.current!=='wake'||busyRef.current)return;
      if(result.detected){change('command');setCaption('');
        // A short local chime marks the microphone handoff; no audio is sent to a cloud service.
        const context=liveAudio.current||new AudioContext(),tone=context.createOscillator(),gain=context.createGain();gain.gain.value=.04;tone.frequency.value=880;tone.connect(gain);gain.connect(context.destination);tone.start();tone.stop(context.currentTime+.1);tone.onended=()=>{tone.disconnect();gain.disconnect();if(context!==liveAudio.current&&context.state!=='closed')void context.close()};
      }
    }).catch(e=>{if(generation===epoch.current){setError(String(e));stop()}}).finally(()=>{if(generation===epoch.current)wakeBusy.current=false});
  };
  const samplesRef=useRef(samples);samplesRef.current=samples;

  const enable=async()=>{
    if(modeRef.current!=='off')return;
    const generation=++epoch.current;setError('');change('preparing');onActive(true);
    try{
      // Unlock WebKit audio under the explicit ON gesture, before the first download.
      if(typeof AudioContext!=='undefined'){pendingAudio.current=new AudioContext();void pendingAudio.current.resume().catch(()=>{})}
      const initial=await api<Status>('/handsfree/enable',{method:'POST',body:JSON.stringify({language})});
      if(generation!==epoch.current){if(initial.session)void api('/handsfree/disable',{method:'POST',body:JSON.stringify({session:initial.session})});return}
      session.current=initial.session;setStatus(initial);setLabel(initial.profile.label);
      let checking=false,started=false;
      const check=async()=>{
        if(checking||generation!==epoch.current)return;checking=true;
        try{
          const state=await api<Status>(`/handsfree?session=${session.current}`);if(generation!==epoch.current)return;setStatus(state);
          if(!state.enabled||state.phase==='error')throw Error(state.error||'HANDSFREE_OFF');
          if(state.phase==='ready'&&!started){started=true;
            // Keep the preparation heartbeat alive even while macOS permission waits for the user.
            void microphone((chunk,sampleRate)=>samplesRef.current(chunk,sampleRate),pendingAudio.current||undefined).then(release=>{
              if(generation!==epoch.current){release();return}stopMic.current=release;
              liveAudio.current=pendingAudio.current;
              pendingAudio.current=null;
              change(state.profile.count>=3?'wake':'enroll');
              if(wantCameraRef.current)void startCamera();
            }).catch(e=>{if(generation===epoch.current){setError(String(e));stop()}});
          }
        }catch(e){if(generation===epoch.current){setError(String(e));stop()}}finally{checking=false}
      };
      poll.current=setInterval(()=>void check(),2000);void check();
    }catch(e){if(generation===epoch.current){setError(String(e));stop()}}
  };

  useEffect(()=>{if(!startSignal)return;const timer=setTimeout(()=>void enable(),0);return()=>clearTimeout(timer)},[startSignal]);
  const startCamera=async()=>{
    if(cameraStarting.current)return;cameraStarting.current=true;
    if(!native()){cameraStarting.current=false;setError(ja?'ジェスチャー操作はOLCRデスクトップで利用できます。':'Gesture input requires OLCR desktop.');return}
    const generation=epoch.current;setError('');
    try{
      const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{width:{ideal:640},height:{ideal:480},frameRate:{ideal:12,max:15}}});
      if(generation!==epoch.current){stream.getTracks().forEach(t=>t.stop());return}
      cameraStream.current=stream;setCamera(true);await new Promise(resolve=>setTimeout(resolve,0));
      const player=video.current;if(!player){releaseCamera();return}player.srcObject=stream;await player.play();
      const canvas=document.createElement('canvas');canvas.width=640;canvas.height=480;let sequence=0;
      const frame=async()=>{
        if(generation!==epoch.current||cameraStream.current!==stream)return;
        const capturedAt=Date.now();
        try{
          canvas.getContext('2d')!.drawImage(player,0,0,640,480);
          const result=await post({op:'gesture',image:canvas.toDataURL('image/jpeg',.65).split(',')[1],captured_at:capturedAt});
          if(generation!==epoch.current||cameraStream.current!==stream)return;
          if(Date.now()-capturedAt>=700){await post({op:'reset'});const token=nativeToken.current;nativeToken.current=null;if(token)await native()!.core.invoke('end_human_input',{token});}
          else{
            if(!nativeToken.current)nativeToken.current=await native()!.core.invoke<string>('begin_human_input');
            if(generation!==epoch.current){releaseCamera();return}
            await native()!.core.invoke('apply_human_input',{token:nativeToken.current,sequence:++sequence,capturedAt,events:result.events});
            setGesture(result.gesture|| (result.hand?(ja?'手を検出':'Hand detected'):(ja?'手が見えていません':'No hand')));
            const paint=overlay.current?.getContext('2d');if(paint){paint.clearRect(0,0,640,480);paint.fillStyle='#9bdf16';for(const p of result.landmarks){paint.beginPath();paint.arc(p[0]*640,p[1]*480,4,0,Math.PI*2);paint.fill()}}
          }
        }catch(e){if(generation===epoch.current){setError(String(e));releaseCamera();void post({op:'reset'}).catch(()=>{})}return}
        if(generation===epoch.current&&cameraStream.current===stream)cameraTimer.current=setTimeout(()=>void frame(),100);
      };void frame();
    }catch(e){if(generation===epoch.current){setError(String(e));releaseCamera()}}finally{cameraStarting.current=false}
  };

  const names:Record<Mode,string>=ja?{off:'OFF',preparing:'準備中',enroll:'ウェイク音声を登録',recording:'3秒間録音中…',wake:'ウェイク待機',command:'指示を聞いています',transcribing:'文字起こし中',waiting:'指示を処理中'}:{off:'OFF',preparing:'Preparing',enroll:'Enroll wake phrase',recording:'Recording for 3 seconds…',wake:'Wake standby',command:'Listening to your command',transcribing:'Transcribing',waiting:'Processing command'};
  return <section className="handsfree-panel" aria-label={ja?'ハンズフリー操作':'Hands-free input'}>
    <header><strong>{ja?'ハンズフリー':'Hands-free'} · {names[mode]}</strong><button type="button" onClick={()=>{stop();onClose()}} aria-label={ja?'ハンズフリーを停止して閉じる':'Stop hands-free and close'}>×</button></header>
    {mode==='off'&&<><p>{ja?'ハンズフリーモードを開始できませんでした。右上のトグルから再試行できます。初回はウェイク検出と手認識の機能（約250 MB）、未導入の場合は音声モデル（約3.7 GB）を取得します。':'Hands-free mode could not start. Retry with the switch at the upper right. The wake and gesture runtime (~250 MB), plus a missing speech model (~3.7 GB), may download on first use.'}</p><label className="handsfree-camera-choice"><input type="checkbox" checked={wantCamera} onChange={e=>setWantCamera(e.target.checked)}/>{ja?'カメラのジェスチャー操作も使う':'Also use webcam gestures'}</label></>}
    {mode==='preparing'&&<p role="status">{status?.phase==='ready'?(ja?'マイクの許可と開始を待っています…':'Waiting for microphone access…'):status?.phase==='preparing_audio'?(ja?'ローカル音声モデルを準備中…':'Preparing local speech model…'):status?.phase==='installing'?(ja?'取得した機能をインストール中…':'Installing downloaded runtime…'):(ja?'必要な機能を取得しています…':'Acquiring optional runtime…')} {status?.downloaded_bytes?`${(status.downloaded_bytes/1024/1024).toFixed(1)} MB`:''}</p>}
    {mode==='preparing'&&status?.phase==='ready'&&<button type="button" onClick={()=>void pendingAudio.current?.resume()}>{ja?'マイクの開始を続ける':'Continue microphone startup'}</button>}
    {mode!=='off'&&mode!=='preparing'&&<>
      <label>{ja?'ウェイクワード':'Wake phrase'}<input value={label} maxLength={40} disabled={mode==='recording'||Boolean(status?.profile.count)} onChange={e=>setLabel(e.target.value)}/></label>
      <p>{ja?`「${label}」を普段の声で3〜4回録音してください。`:`Record "${label}" 3–4 times in your normal voice.`} {status?.profile.count||0}/3</p>
      <div className="handsfree-actions"><button type="button" disabled={mode==='recording'||sampleBusy.current||busy||(status?.profile.count||0)>=6} onClick={()=>{setError('');change('recording')}}>{ja?'3秒録音':'Record 3 seconds'}</button><button type="button" disabled={mode==='recording'||sampleBusy.current} onClick={()=>{change('enroll');void post({op:'delete',label}).then(result=>setStatus(old=>old?{...old,profile:result.profile}:old)).catch(e=>setError(String(e)))}}>{ja?'録り直し・語句を変更':'Reset samples / change phrase'}</button></div>
      {(status?.profile.count||0)>=3&&<p className="muted">{ja?'ウェイク後の確認音が鳴ったら指示を話してください。無音で確定し、このチャットへ送信します。「ハンズフリーオフ」で停止できます。':'After the wake chime, speak your command. Silence submits it to this chat. Say "hands-free off" to stop.'}</p>}
      <button type="button" disabled={mode==='recording'} onClick={()=>{if(camera){setWantCamera(false);releaseCamera();void post({op:'reset'}).catch(()=>{})}else{setWantCamera(true);void startCamera()}}}>{camera?(ja?'ジェスチャー操作をOFF':'Disable gestures'):(ja?'カメラでジェスチャー操作をON':'Enable webcam gestures')}</button>
      {camera&&<><div className="handsfree-camera"><video ref={video} muted playsInline/><canvas ref={overlay} width={640} height={480}/></div><p role="status">{gesture}</p><p>{ja?'人差し指で移動・短いピンチでクリック・ピンチ長押しでドラッグ・Vサインを上下に動かしてスクロール・手のひらで停止。':'Point to move; short pinch to click; hold pinch to drag; move a V sign to scroll; open palm to pause.'}</p></>}
      {caption&&<p aria-live="polite">{caption}</p>}
    </>}
    {error&&<p role="alert">{error}</p>}
    {error.includes('ACCESSIBILITY_PERMISSION_REQUIRED')&&<><p>{ja?'ジェスチャーで操作するには、macOSでOLCRのアクセシビリティを許可してください。':'Allow OLCR Accessibility in macOS to use gesture input.'}</p><button type="button" onClick={()=>void native()?.core.invoke('request_accessibility_permission')}>{ja?'アクセシビリティの許可を要求':'Request Accessibility permission'}</button></>}
  </section>;
}
