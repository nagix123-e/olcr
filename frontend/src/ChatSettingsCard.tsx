import {SettingsGroup} from './InterfacePrimitives';
import {ServiceConnectorsSettings} from './ServiceConnectorsSettings';
import {UiIcon} from './UiIcon';
import {SettingHelp} from './SettingHelp';
import "./settings.css";
import {useEffect, useState} from "react";
import {AccessibilityPanel} from "./AccessibilityPanel";
import {ExternalToolsSettings} from "./ExternalToolsSettings";
import {GitHubSettings} from "./GitHubSettings";
import {WebSettings} from "./WebSettings";
import {ReadAloudSetting} from "./ReadAloudSetting";
import {ModelTrafficLogs} from "./ModelTrafficLogs";
import {MemorySettings} from "./MemorySettings";
import {MobileConnectionSettings} from './MobileConnectionSettings';

import {BrainState,useBrainModels,registerBrainProvider} from "./brainModels";
import {ModelsPanel,ProviderRegistration} from "./ModelsPanel";
import {useLanguage} from "./language";

type Api = (path: string, init?: RequestInit) => Promise<any>;

/** Only the card descriptor is persisted; credentials stay in the native Keychain path. */
export function ChatSettingsCard({api,brainState,initialModels=false,onConfigureBrain,onOpenLanding}: {api: Api;brainState?:BrainState;initialModels?:boolean;conversationId?:string|null;onConfigureBrain?:(input:ProviderRegistration)=>Promise<void>;onOpenLanding?:()=>void}) {
  const {language,setLanguage}=useLanguage();
  const [speechEngine,setSpeechEngine]=useState<"lfm"|"macos">(()=>localStorage.getItem("olcr-speech-engine")==="macos"?"macos":"lfm");
  const selectSpeechEngine=(value:"lfm"|"macos")=>{localStorage.setItem("olcr-speech-engine",value);setSpeechEngine(value);window.dispatchEvent(new Event("olcr-speech-engine-change"));};
  const [settings, setSettings] = useState<Record<string, any> | null>(null);
  const [modelsOpen,setModelsOpen]=useState(initialModels);
  const [servicesOpen,setServicesOpen]=useState(false);
  const [trafficOpen,setTrafficOpen]=useState(false);
  const [mobileOpen,setMobileOpen]=useState(false);
  const ownedBrain=useBrainModels(!brainState&&Boolean(settings),undefined,api);
  const brain=brainState||ownedBrain;
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    let generation = 0;
    const refresh = () => {
      const request = ++generation;
      void api("/settings").then(next => {if (live && request === generation) setSettings(next);})
        .catch(reason => {if (live && request === generation) setError(reason?.message || String(reason));});
    };
    refresh();
    window.addEventListener("olcr-command-complete", refresh);
    return () => {live = false; window.removeEventListener("olcr-command-complete", refresh);};
  }, [api]);
  const save = async (path: string, next: Record<string, any>) => {
    const saved = await api(path, {method: path === "/settings/capabilities" || path === "/settings/memory" ? "PATCH" : "PUT",
      body: JSON.stringify(next)});
    setSettings(current => ({...current, ...saved}));
    window.dispatchEvent(new Event("olcr-command-complete"));
    return saved;
  };
  if(mobileOpen)return <MobileConnectionSettings api={api} onBack={()=>setMobileOpen(false)}/>;
  if(servicesOpen)return <ServiceConnectorsSettings api={api} onBack={()=>setServicesOpen(false)}/>;
  if(trafficOpen)return <ModelTrafficLogs api={api} onBack={()=>setTrafficOpen(false)}/>;
  if(modelsOpen)return <ModelsPanel state={brain} request={api} onBack={()=>setModelsOpen(false)} onOpenLanding={onOpenLanding} onConfigure={async input=>{if(onConfigureBrain)await onConfigureBrain(input);else await registerBrainProvider(input,api,language);await brain.refresh()}}/>;
  return <section className="chat-settings" aria-label={language==="ja"?"チャットの権限と設定":"Chat permissions and settings"}>
    <h2>{language==="ja"?"権限と設定":"Permissions and settings"}</h2>
    <p>{language==="ja"?"権限と認証情報をここで設定します。タスクの開始や操作はチャットから行えます。":"Set permissions and credentials here. Start and control tasks with messages in this chat."}</p>
    <section className="panel general-settings">
    <h2>{language==="ja"?"一般":"General"}</h2>
    <div className="setting-row language-setting"><label htmlFor="olcr-language">{language==="ja"?"言語":"Language"}</label><select id="olcr-language" value={language} onChange={event=>setLanguage(event.target.value as "ja"|"en")}><option value="ja">日本語</option><option value="en">English</option></select></div>
    <div className="setting-row language-setting"><span className="setting-label-with-help"><label htmlFor="olcr-speech-engine">{language==="ja"?"音声入力":"Voice input"}</label><SettingHelp label={language==='ja'?'音声入力':'Voice input'}>{language==='ja'?'LFMはダウンロードした専用モデルを使います。macOS標準は、このMacが対応するAppleのオンデバイス音声認識を使います。':'LFM uses a downloaded speech model. macOS Speech uses Apple’s on-device recognition when supported on this Mac.'}</SettingHelp></span><select id="olcr-speech-engine" value={speechEngine} onChange={event=>selectSpeechEngine(event.target.value as "lfm"|"macos")}><option value="lfm">{language==="ja"?"LFM（ローカルモデル）":"LFM (local model)"}</option><option value="macos">{language==="ja"?"macOS標準（オンデバイス）":"macOS Speech (on-device)"}</option></select></div>
    <p className="setting-hint">{language==="ja"?"macOS標準を選ぶと、Appleの音声認識許可が必要です。オンデバイス認識に対応しない言語・環境では利用できません。":"macOS Speech requires Apple’s speech recognition permission. It is unavailable when on-device recognition is not supported for your locale or Mac."}</p>
    <ReadAloudSetting api={api}/>
    </section>
    <button type="button" className="models-entry" onClick={()=>setMobileOpen(true)}><span><strong>{language==='ja'?'スマホを接続':'Connect your phone'}</strong><small>{language==='ja'?'QRを読み取って、このPCのOLCRとつなぐ':'Pair with OLCR on this PC using a QR code'}</small></span><UiIcon name="link" size={18}/></button>
    <button type="button" className="models-entry" onClick={()=>setModelsOpen(true)}><div><strong>{language==="ja"?"モデル":"Models"}</strong><small>{language==="ja"?"Brainの構成・モデルの接続設定":"Configure Brain and connect models"}</small></div><UiIcon name="back" className="entry-forward" size={16}/></button>
    <button type="button" className="models-entry services-entry" onClick={()=>setServicesOpen(true)}><span><UiIcon name="link" size={22} className="service-connections-icon"/><span><strong>{language==="ja"?"サービス接続":"Service connections"}</strong><small>{language==="ja"?"メール・カレンダー・ファイル・ショッピング":"Mail, calendar, files and shopping"}</small></span></span><UiIcon name="back" className="entry-forward" size={16}/></button>
    <SettingsGroup title={language==="ja"?"診断と通信履歴":"Diagnostics and traffic"}><button type="button" className="models-entry" onClick={()=>setTrafficOpen(true)}><span><strong>{language==="ja"?"トラフィックログ":"Traffic logs"}</strong><small>{language==="ja"?"モデルの通信をまとめて確認":"Review model traffic in one timeline"}</small></span><UiIcon name="back" className="entry-forward" size={16}/></button></SettingsGroup>
    <p className="setting-hint">{language==="ja"?"タスク・目標・フォーム入力・ツール作成はチャットで依頼できます。「明日までに資料を作るタスクを追加」「このフォームに入力して」「繰り返し使えるツールにして」のように話しかけてください。":"Ask in chat to manage tasks and goals, fill forms, or create tools. Try “Add a task to prepare the report by tomorrow”, “Fill this form”, or “Make this a reusable tool”."}</p>
    <SettingsGroup title={language==="ja"?"GitHub連携":"GitHub integration"}><GitHubSettings api={api}/></SettingsGroup>
    {error && <p role="alert">{error}</p>}
    {settings && <>
      <SettingsGroup title={language==="ja"?"会話の記憶":"Conversation memory"}><MemorySettings settings={settings} save={next => save("/settings/memory", next)}/></SettingsGroup>
      <SettingsGroup title={language==="ja"?"画面操作の権限":"Screen interaction permissions"}><AccessibilityPanel view="policy" /></SettingsGroup>
      <SettingsGroup title={language==="ja"?"ツールとチャット機能":"Tools and chat capabilities"}><ExternalToolsSettings api={api} settings={settings} save={next => save("/settings/capabilities", next)} /></SettingsGroup>
      <SettingsGroup title={language==="ja"?"Web検索":"Web search"}><WebSettings api={api} /></SettingsGroup>
    </>}
  </section>;
}
