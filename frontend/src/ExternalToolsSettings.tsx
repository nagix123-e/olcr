import {DialogFrame,StatusBadge} from './InterfacePrimitives';
import {CredentialAccessNotice} from './CredentialAccessNotice';
import { useEffect, useId, useState } from 'react';
import {SettingHelp} from './SettingHelp';
import {useLanguage} from './language';
import {native} from './native';
import {ConvertSetting} from './ConvertSetting';
import {RuntimeComponentsSettings} from './RuntimeComponents';
import {CredentialSetupCard, type CredentialSetupBlock} from './CredentialSetup';

type Tool = { tool_id: string; provider: string; availability: string; external_authorized: boolean; credential: string; endpoint_mode: string; credential_setup?: CredentialSetupBlock|null };
type State = { external_access_enabled: boolean; tools: Tool[] };

export function ExternalToolsSettings({ api, settings, save }: { api: (path: string, init?: RequestInit) => Promise<any>; settings: Record<string, any> | null; save: (next: Record<string, any>) => Promise<any> }) {
  const {language}=useLanguage();const ja=language==="ja";
  const id = useId();
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState('');
  const [credentialSetup,setCredentialSetup]=useState<CredentialSetupBlock|null>(null);
  const load = async () => {
    const bridge = native();
    if (bridge) {
      try {
        await bridge.core.invoke('restore_external_api_credentials');
        setError('');
      } catch (error) {
        setError(error instanceof Error ? error.message : 'Keychain credential restore failed');
      }
    }
    try { setState(await api('/external-tools')); }
    catch (error) { setError(error instanceof Error ? error.message : 'External tool status unavailable'); }
  };
  useEffect(() => { void load(); }, []);
  const [busy, setBusy] = useState(false);
  const update = async (next: Record<string, boolean>) => {
    if (!settings || busy) return;
    setBusy(true);
    setError('');
    try { await save(next); if ('external_access_enabled' in next) await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };
  const features = [
    {key: 'yarn_enabled', title: 'YaRN', description: ja ? 'MLX版Ornith・Bonsaiのコンテキスト長を延長します。延長先は「モデル」のコンテキスト設定で調整できます。Ollamaを使う場合はオフにしてください。' : 'Extend context for Ornith and Bonsai in MLX. Adjust the target in Models → Local model context. Turn this off when using Ollama.'},
    {key: 'context_auto_compress', title: ja ? 'コンテキスト自動圧縮' : 'Automatic context compression', description: ja ? 'ローカルモデルへ送る過去の文脈を自動で短縮します。元の会話は保持します。コンテキスト長は「モデル」のコンテキスト設定で調整できます。' : 'Automatically shorten older context sent to local models while keeping original conversations. Adjust the context window in Models → Local model context.'},
    {key: 'task_manager_enabled', title: ja ? 'コーディング オーケストレーター' : 'Coding Orchestrator', description: ja ? 'OFFの場合、コーディング依頼は通常のBrain会話で処理されます。' : 'When OFF, coding requests use the normal Brain conversation.'},
    {key: 'web_scraping_enabled', title: ja ? 'ウェブスクレイピング' : 'Web scraping', description: ja ? 'ONの場合、チャットでURLのスクレイピングを依頼すると会話内で管理ブラウザーを開きます。公開HTTP(S)宛てかどうかの安全確認は引き続き適用されます。' : 'When ON, an explicit URL-scraping request in chat opens the existing managed browser in the conversation. Safe public-HTTP(S) destination checks still apply.'},
    {key: 'computer_use_enabled', title: ja ? 'コンピューター操作' : 'Computer Use', description: ja ? 'ONの場合、チャットで操作するアプリ名を指定します。OLCRは起動中のアプリを特定してセッションを紐付けます。画面収録とアクセシビリティの許可はmacOSで管理されます。' : 'When ON, name the app to control in a chat message. OLCR resolves the running app and binds the session to that process; macOS Screen Recording and Accessibility permissions remain controlled by macOS.'},
    {key: 'local_e2e_enabled', title: ja ? 'ローカルプロジェクトE2E' : 'Local project E2E', description: ja ? 'ONの場合、許可されたプロジェクトのチャットでE2Eの開始を依頼します。OLCRは`.olcr/e2e.json`または対応する`dev`／`start`／`serve`スクリプトを使います。ループバックのホストとポートを指定するコマンドや、既存のループバックURLにも接続できます。操作・状態確認・停止はチャットから行います。' : 'When ON, ask in the selected allowed project’s chat to start E2E. OLCR reuses `.olcr/e2e.json` or a supported `dev`/`start`/`serve` script; you can also give chat a one-off command that binds a loopback host and port, or attach to an existing loopback URL. Use chat for actions, status, and stop.'},
  ];
  const help: Record<string, string> = ja ? {
    yarn_enabled: '長い会話や資料を一度に扱うための拡張です。実際に追加する資料の量は、モデル設定のRAG上限でも調整します。',
    context_auto_compress: '長い会話でモデルの上限に近づくと、送信する古い文脈を短縮します。短縮前の内容は会話履歴で確認できます。',
    task_manager_enabled: '実装依頼を計画と作業に分け、進捗や追加指示を管理します。通常の質問や相談は引き続きチャットから行えます。',
    web_scraping_enabled: '指定したページの文章や表を抽出する機能です。ウェブ検索のプロバイダーや検索モードとは別に設定します。',
    computer_use_enabled: '指定したアプリの画面を確認し、クリックや入力などの操作を行う機能です。OSの操作権限も必要です。',
    local_e2e_enabled: 'このMacで動くWebアプリをブラウザーで操作し、一連の動作を確認します。対象は許可されたプロジェクトです。',
  } : {
    yarn_enabled: 'Extend the window for long conversations or references. The RAG allowance separately controls how much retrieved material is added.',
    context_auto_compress: 'Shorten older context sent to a local model as its window fills. The original remains available in conversation history.',
    task_manager_enabled: 'Organize implementation requests into a plan and tasks, with progress and follow-up instructions managed in chat.',
    web_scraping_enabled: 'Extract text or tables from a specific page. This is configured separately from web search providers and search mode.',
    computer_use_enabled: 'Observe a named app and interact through clicks and typing. macOS permissions are also required.',
    local_e2e_enabled: 'Exercise a local web app in a browser to check a complete flow within an authorized project.',
  };
  return <>
    <section className="panel external-tools-settings">
      <h2>{ja ? '外部ツール · 全体' : 'External Tools · Global'}</h2>
      <div className="setting-row capability-setting">
        <span className="setting-label-with-help"><label htmlFor={`${id}-external`}>{ja ? '外部アクセス' : 'External access'}</label><SettingHelp label={ja?'外部アクセス':'External access'}>{ja?'検索や知識APIなどへアクセスする許可です。プロバイダーの接続設定とは別に、外部への通信を許可する必要があります。':'Allow access to external search and knowledge APIs. Configuring a provider alone does not authorize these network requests.'}</SettingHelp></span>
        <input id={`${id}-external`} type="checkbox" role="switch" checked={state?.external_access_enabled ?? false} disabled={!state || !settings || busy}
          onChange={event => void update({external_access_enabled: event.target.checked})}/>
      </div>
      <p className="setting-hint">{ja ? 'プロバイダーの準備完了はネットワーク利用の許可を意味しません。必要最小限の構造化パラメーターのみ送信されます。' : 'Provider readiness does not grant network authorization. Only minimal structured parameters are sent.'}</p>
      {state ? <details className="settings-disclosure">
        <summary>{ja ? 'API・ツールの状態' : 'API and tool status'}<span className="settings-count">{state.tools.length}</span></summary>
        <div className="settings-disclosure-content">
          {state.tools.length ? state.tools.map(tool => <div className="row tool-status-row" key={tool.tool_id}>
            <span>{tool.provider}<small>{tool.tool_id} · {tool.credential}</small></span>
            {tool.credential_setup?<button type="button" className="tool-availability tool-configure" onClick={()=>setCredentialSetup(tool.credential_setup!)}>{ja?'Configure':'Configure'}</button>:<StatusBadge state={tool.availability}>{tool.availability==='READY'?'Ready':tool.availability}</StatusBadge>}
          </div>) : <p>{ja ? '登録済みのツールはありません。' : 'No registered tools.'}</p>}
        </div>
      </details> : <p role="status">{ja ? 'ツールの状態を確認中…' : 'Checking tool status…'}</p>}
      <CredentialAccessNotice error={error} onRestored={load}/>
    {credentialSetup&&<DialogFrame title={ja?`${credentialSetup.provider}を設定`:`Configure ${credentialSetup.provider}`} onClose={()=>setCredentialSetup(null)}>
        <CredentialSetupCard block={credentialSetup} api={api} onSaved={load}/>
    </DialogFrame>}
    </section>
    <RuntimeComponentsSettings api={api}/>
    <section className="panel capability-settings">
      <h2>{ja ? 'チャットで使う機能' : 'Chat capabilities'}</h2>
      <ConvertSetting api={api} enabled={settings?.convert_enabled===true} save={save}/>
      {features.map(feature => <div className="capability-item" key={feature.key}>
        <div className="setting-row capability-setting">
          <span className="setting-label-with-help"><label htmlFor={`${id}-${feature.key}`}>{feature.title}</label><SettingHelp label={feature.title}>{help[feature.key]}</SettingHelp></span>
          <input id={`${id}-${feature.key}`} type="checkbox" role="switch" checked={settings?.[feature.key] === true} disabled={!settings || busy}
            aria-describedby={`hint-${feature.key}`} onChange={event => void update({[feature.key]: event.target.checked})}/>
        </div>
        <p className="setting-hint" id={`hint-${feature.key}`}>{feature.description}</p>
      </div>)}
    </section>
  </>;
}
