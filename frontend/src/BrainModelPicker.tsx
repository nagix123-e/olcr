import {CredentialAccessNotice} from './CredentialAccessNotice';
import { useEffect, useRef, useState } from 'react';
import { brainEffortLabel, brainRoleLabel, BrainRole, BrainState, modelEffortLevels, providerDisplayLabel, signInBrainOAuth } from './brainModels';
import {openExternal} from './native';
import './brain-models.css';
import {useLanguage} from './language';

type ProviderTab = 'local' | 'openai' | 'anthropic' | 'google' | 'nvidia' | 'huggingface' | 'ollama_cloud';
type PickerPhase = 'effort' | 'providers' | 'models';
const ROLES: BrainRole[] = ['coder', 'short_coding', 'general_inference'];
const PROVIDERS: { id: ProviderTab; label: {ja:string;en:string} }[] = [
  { id: 'local', label: {ja:'ローカル',en:'Local'} }, { id: 'openai', label: {ja:'OpenAI',en:'OpenAI'} },
  { id: 'anthropic', label: {ja:'Anthropic',en:'Anthropic'} }, { id: 'google', label: {ja:'Google',en:'Google'} },
  { id: 'nvidia', label: {ja:'NVIDIA',en:'NVIDIA'} },
  { id: 'huggingface', label: {ja:'Hugging Face',en:'Hugging Face'} },
  { id: 'ollama_cloud', label: {ja:'Ollama Cloud',en:'Ollama Cloud'} },
];

function providerTab(model: { id: string; provider: string; local: boolean }): ProviderTab {
  if (model.local || model.id.startsWith('mlx:') || model.id.startsWith('ollama:')) return 'local';
  if (model.id.startsWith('api:ollama_cloud:')) return 'ollama_cloud';
  const identity = (model.id + ' ' + model.provider).toLowerCase();
  if (identity.includes('anthropic')) return 'anthropic';
  if (identity.includes('google') || identity.includes('gemini')) return 'google';
  if (identity.includes('nvidia') || identity.includes('nim')) return 'nvidia';
  if (identity.includes('huggingface') || identity.includes('hugging face')) return 'huggingface';
  return 'openai';
}

export function BrainModelPicker({ state, onSettings }: { state: BrainState; onSettings?: () => void }) {
  const {language}=useLanguage();const ja=language==="ja";
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<PickerPhase>('effort');
  const [modelRole, setModelRole] = useState<BrainRole | null>(null);
  const [provider, setProvider] = useState<ProviderTab>('local');
  const [query, setQuery] = useState('');
  const [reconnecting,setReconnecting]=useState(false),[connectionError,setConnectionError]=useState('');
  const [effortDrafts,setEffortDrafts]=useState<Partial<Record<BrainRole,string>>>({});
  const pendingEffortSaves=useRef(new Set<BrainRole>());
  const chatgpt=state.catalog.providers.find(provider=>provider.id==='openai_oauth');
  const reconnectChatGPT=async()=>{setReconnecting(true);setConnectionError('');try{await signInBrainOAuth(undefined,language);await state.refresh()}catch(error){setConnectionError(error instanceof Error?error.message:String(error))}finally{setReconnecting(false)}};
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const providerButtons = useRef<Partial<Record<ProviderTab, HTMLButtonElement | null>>>({});

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    if (phase === 'models') search.current?.focus();
    if (phase === 'providers') providerButtons.current[provider]?.focus();
  }, [open, phase, provider]);

  useEffect(() => {
    if (!open) return;
    void state.refreshUsage();
    const timer=window.setInterval(()=>void state.refreshUsage(),15000);
    return ()=>window.clearInterval(timer);
  },[open,state.refreshUsage]);

  const roles: BrainRole[] = state.configuration.mode === 'unified' ? ['brain'] : ROLES;
  const roleLabel=(role:BrainRole)=>brainRoleLabel(role,language);
  const providerLabel=(item:typeof PROVIDERS[number])=>item.label[language];
  const allModels = state.catalog.models.filter(model => model.kind === 'language');
  const currentModel = (role: BrainRole) => allModels.find(model => model.id === state.configuration.selections[role]);
  const modelName = (role: BrainRole) => currentModel(role)?.label || state.configuration.selections[role];
  const tokenCount = (count:number) => new Intl.NumberFormat(language==='ja'?'ja-JP':'en-US').format(count);
  const safeRateCount=(value:string|undefined)=>value&&/^\d+$/.test(value)?tokenCount(Number(value)):null;
  const levelsFor = (role: BrainRole) => modelEffortLevels(currentModel(role));
  const defaultFor = (role: BrainRole) => {
    const levels = levelsFor(role);
    const selected = state.configuration.efforts?.[role];
    const preferred = selected ?? (currentModel(role)?.local ? 'auto' : currentModel(role)?.effort_default) ?? null;
    return preferred && levels.includes(preferred) ? preferred : null;
  };
  const providerModels = allModels.filter(model => providerTab(model) === provider && model.available);
  const models = providerModels.filter(model => model.label.toLowerCase().includes(query.trim().toLowerCase()));
  const closePicker = () => {
    setOpen(false);
    trigger.current?.focus();
  };
  const openPicker = () => {
    if (open) { setOpen(false); return; }
    void state.refresh();
    setPhase('effort');
    setModelRole(null);
    setQuery('');
    setOpen(true);
  };
  const beginModelSelection = (role: BrainRole) => {
    setModelRole(role);
    setProvider(currentModel(role) ? providerTab(currentModel(role)!) : 'local');
    setQuery('');
    setPhase('providers');
  };
  const chooseProvider = (next: ProviderTab) => {
    setProvider(next);
    setQuery('');
    setPhase('models');
  };
  const chooseEffort = async (role: BrainRole, effort: string | null) => {
    try {
      await state.save({ ...state.configuration, efforts: { ...state.configuration.efforts, [role]: effort } });
    } catch { /* The Brain state owns and displays persistence errors. */ }
  };
  const setEffortDraft = (role: BrainRole, effort: string) => {
    setEffortDrafts(previous => ({...previous,[role]:effort}));
  };
  const clearEffortDraft = (role: BrainRole, effort?: string) => {
    setEffortDrafts(previous => {
      if (effort && previous[role] !== effort) return previous;
      const next = {...previous};
      delete next[role];
      return next;
    });
  };
  const commitEffort = async (role: BrainRole, effort: string | undefined, savedEffort: string | null) => {
    if (!effort) return;
    if (effort === savedEffort) {
      clearEffortDraft(role);
      return;
    }
    if (pendingEffortSaves.current.has(role)) return;
    pendingEffortSaves.current.add(role);
    try {
      await chooseEffort(role, effort);
    } finally {
      pendingEffortSaves.current.delete(role);
      clearEffortDraft(role, effort);
    }
  };
  const chooseModel = async (role: BrainRole, id: string) => {
    const chosen = allModels.find(model => model.id === id);
    if (!chosen) return;
    const allowed = modelEffortLevels(chosen);
    const previousEffort = state.configuration.efforts?.[role];
    const effort = previousEffort && allowed.includes(previousEffort)
      ? previousEffort : chosen.local ? 'auto' : chosen.effort_default && allowed.includes(chosen.effort_default)
        ? chosen.effort_default : null;
    try {
      await state.save({
        ...state.configuration,
        selections: { ...state.configuration.selections, [role]: id },
        efforts: { ...state.configuration.efforts, [role]: effort },
      });
      setPhase('effort');
      setModelRole(null);
    } catch { /* Keep the model chooser open so the user can retry. */ }
  };
  const restoreEffortDefault = async (role: BrainRole) => chooseEffort(role, currentModel(role)?.effort_default ?? null);

  return <div className="brain-picker" ref={root} onKeyDown={event => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      if (phase === 'models') setPhase('providers');
      else if (phase === 'providers') setPhase('effort');
      else closePicker();
    }
  }}>
    <button ref={trigger} type="button" className="brain-picker-trigger" aria-haspopup="dialog"
      aria-expanded={open} aria-label={ja?"Brainモデルを選択":"Choose Brain models"} onClick={openPicker}>
      Brain <span>{roles.length === 1 ? modelName('brain') : ja?'3 用途':'3 roles'}</span><span aria-hidden="true">⌃</span>
    </button>
    {open && <div className="brain-picker-popover" role="dialog" aria-label={ja?"Brainモデルの選択":"Brain model selection"}>
      <header>
        <div><strong>{ja?(phase==='effort'?'推論強度':phase==='providers'?'プロバイダーを選択':'モデルを選択'):(phase === 'effort' ? 'Effort' : phase === 'providers' ? 'Choose provider' : 'Choose model')}</strong>
          <small>{ja?(state.configuration.mode==='unified'?'ひとつのモデルを全用途で使用':'用途ごとにモデルを選択'):(state.configuration.mode==='unified'?'Use one model for all tasks':'Choose a model for each task')}</small></div>
        <button type="button" aria-label={ja?"モデル選択を閉じる":"Close model chooser"} onClick={closePicker}>×</button>
      </header>

      {phase === 'effort' && <div className={`brain-effort-columns ${roles.length === 1 ? 'unified' : ''}`}>
        {roles.map(role => {
          const model = currentModel(role);
          const levels = levelsFor(role);
          const savedEffort = defaultFor(role);
          const draftEffort = effortDrafts[role];
          const selected = draftEffort && levels.includes(draftEffort) ? draftEffort : savedEffort;
          const selectedIndex = selected ? levels.indexOf(selected) : -1;
          const rangeMax = levels.length ? levels.length - 1 : 1;
          const rangeValue = levels.length ? Math.max(0, selectedIndex) : 1;
          const progress = levels.length > 1 ? (rangeValue / rangeMax) * 100 : 100;
          return <section className={`brain-effort-card${model?.local?' brain-local-effort':''}`} key={role} aria-label={ja?`${roleLabel(role)}の推論強度`:`${roleLabel(role)} effort`}>
            <div className="brain-effort-topline">{model?.quick_supported?<button type="button" className="brain-quick-toggle" aria-label={ja?`${roleLabel(role)}のQuickモード`:`${roleLabel(role)} Quick mode`} aria-pressed={state.configuration.quick_models?.[model.id]===true} disabled={state.loading} title={ja?'Quickモード：高速処理。利用量・料金が増える場合があります。':'Quick mode: faster processing; usage or cost may increase.'} onClick={()=>void state.save({...state.configuration,quick_models:{...state.configuration.quick_models,[model.id]:!state.configuration.quick_models?.[model.id]}}).catch(()=>{})}>ϟ</button>:<span aria-hidden="true"/>}
              <strong>{selected ? brainEffortLabel(selected,language) : ''}</strong>
              <button type="button" aria-label={ja?`${roleLabel(role)}の推論強度をプロバイダーの既定値に戻す`:`Reset ${roleLabel(role)} effort to provider default`}
                title={ja?"プロバイダーの既定値に戻す":"Reset to provider default"} onClick={() => void restoreEffortDefault(role)}>↺</button>
            </div>
            <button type="button" className="brain-effort-model" aria-label={ja?`${roleLabel(role)}のモデルを選択`:`Choose model for ${roleLabel(role)}`}
              onClick={() => beginModelSelection(role)}>{modelName(role)} <span aria-hidden="true">›</span></button>
            {model && <div className="brain-usage-line" aria-label={ja?`${model.label}の週間利用量`:`${model.label} weekly usage`}>
              {(() => {
                const usage=state.usage.models[model.id] ?? {input_tokens:0,output_tokens:0,total_tokens:0,window_start:state.usage.week_starts_at,window_kind:'weekly' as const,last_used:null,limit_tokens:null,cost_usd:null,provider_rate_limits:null,rate_limits_observed_at:null};
                const calls=usage.request_count;
                const recorded=state.usage.models[model.id]!==undefined && calls!==0;
                const reported=usage.token_reported_requests??(recorded?1:0);
                const missing=usage.unreported_requests??0;
                const hasTokens=reported>0;
                const caption=!recorded?(ja?'今週の利用記録なし':'No usage recorded this week')
                  :!hasTokens?(ja?`週 ${tokenCount(calls||0)} リクエスト`:`Week ${tokenCount(calls||0)} requests`)
                  :ja?`週 ${tokenCount(usage.total_tokens)} tokens${missing?'（取得分）':''}`:`Week ${tokenCount(usage.total_tokens)} tokens${missing?' (reported)':''}`;
                const secondary=usage.limit_tokens !== null
                  ? (ja?`上限 ${tokenCount(usage.limit_tokens)} tokens`:`Limit ${tokenCount(usage.limit_tokens)} tokens`)
                  : usage.cost_usd !== null
                    ? (ja?`利用額 $${usage.cost_usd.toFixed(4)}`:`Cost $${usage.cost_usd.toFixed(4)}`)
                    : '';
                const rate=usage.provider_rate_limits||{};
                const remainingTokens=safeRateCount(rate['x-ratelimit-remaining-tokens']);
                const remainingRequests=safeRateCount(rate['x-ratelimit-remaining-requests']);
                const tokenLimit=safeRateCount(rate['x-ratelimit-limit-tokens']);
                const requestLimit=safeRateCount(rate['x-ratelimit-limit-requests']);
                const rateDetails=[
                  remainingTokens&&`${ja?'残り':'remaining'} ${remainingTokens} tokens`,
                  remainingRequests&&`${ja?'残り':'remaining'} ${remainingRequests} requests`,
                  tokenLimit&&`${ja?'レート上限':'rate limit'} ${tokenLimit} tokens`,
                  requestLimit&&`${ja?'レート上限':'rate limit'} ${requestLimit} requests`,
                  rate['retry-after']&&`${ja?'再試行まで':'retry after'} ${rate['retry-after']}s`,
                ].filter(Boolean).join(' · ');
                const observed=usage.rate_limits_observed_at?new Date(usage.rate_limits_observed_at*1000).toLocaleString(language==='ja'?'ja-JP':'en-US',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
                const modelProvider=model.provider.toLowerCase();
                const usageLinkLabel=modelProvider.includes('google')?'Google AI Studio usage ↗':modelProvider.includes('nvidia')?'NVIDIA account limits ↗':modelProvider.includes('chatgpt')?'ChatGPT Settings → Usage ↗':modelProvider.includes('anthropic')?'Anthropic Console ↗':'OpenAI API usage ↗';
                return <>
                  <span>{caption}</span>
                  {hasTokens&&!missing&&!usage.total_only_requests&&<small>{ja?`入力 ${tokenCount(usage.input_tokens)} · 出力 ${tokenCount(usage.output_tokens)}`:`In ${tokenCount(usage.input_tokens)} · Out ${tokenCount(usage.output_tokens)}`}</small>}
                  {recorded&&calls!==undefined&&hasTokens&&<small>{ja?`${tokenCount(calls)} リクエスト`:`${tokenCount(calls)} requests`}</small>}
                  {recorded&&hasTokens&&missing>0&&<small>{ja?`トークン取得 ${reported}/${calls} 回`:`Tokens reported for ${reported}/${calls} requests`}</small>}
                  {recorded&&!hasTokens&&<small>{ja?'トークン数未取得 · OLCRからの呼び出し回数':'Tokens not reported · calls from OLCR'}</small>}
                  {Boolean(usage.failed_requests)&&<small>{ja?`うち失敗 ${usage.failed_requests} 回`:`${usage.failed_requests} failed`}</small>}
                  {secondary && <small className="brain-usage-secondary">{secondary}</small>}
                  {rateDetails&&<small className="brain-usage-rate" title={ja?'プロバイダー応答の短時間レート制限です。契約全体の残量ではありません。':'Short-window rate limits reported by the provider; these are not your account-wide allowance.'}>{ja?'API応答':'API response'}{observed?` · ${observed}`:''} · {rateDetails}</small>}
                  {model.usage_url&&<a className="brain-usage-link" href={model.usage_url} onClick={event=>{event.preventDefault();void openExternal(model.usage_url!).catch(()=>{});}}>{ja?`${usageLinkLabel}（公式画面）`:usageLinkLabel}</a>}
                  <button type="button" aria-label={ja?`${model.label}の利用量をリセット`:`Reset usage for ${model.label}`}
                    title={ja?`このモデルの週間利用量を今から数え直す`:`Reset this model's usage window now`}
                    onClick={() => void state.resetUsage(model.id)}>↺</button>
                </>;
              })()}
            </div>}
            <div className="brain-effort-role-label">{roleLabel(role)}</div>
            <input className="brain-effort-slider" type="range" min={0} max={rangeMax} step={1}
              value={rangeValue} aria-label={ja?`${roleLabel(role)}の推論強度`:`${roleLabel(role)} effort level`}
              aria-valuetext={selected?brainEffortLabel(selected,language):(ja?'未設定':'Not set')} disabled={state.loading || levels.length === 0}
              style={{ background: `linear-gradient(to right, var(--olcr-accent) 0%, var(--olcr-accent) ${progress}%, #e8e8e8 ${progress}%, #e8e8e8 100%)` }}
              onChange={event => {
                const next = levels[Number(event.currentTarget.value)];
                if (next) setEffortDraft(role, next);
              }}
              onPointerUp={event => void commitEffort(role, levels[Number(event.currentTarget.value)], savedEffort)}
              onPointerCancel={event => void commitEffort(role, levels[Number(event.currentTarget.value)], savedEffort)}
              onKeyUp={event => void commitEffort(role, levels[Number(event.currentTarget.value)], savedEffort)}
              onBlur={event => void commitEffort(role, levels[Number(event.currentTarget.value)], savedEffort)} />
            {levels.length > 0 && <div className="brain-effort-level-labels" aria-hidden="true">
              {levels.map(level => <span key={level}>{brainEffortLabel(level,language)}</span>)}
            </div>}
            {levels.length === 0 && <small className="brain-effort-unsupported">{ja?"このモデルに推論強度の設定はありません":"Effort levels are unavailable for this model"}</small>}
            {(model?.local||levels.join(',')==='off,auto,on') && <small className="brain-local-thinking-hint">{ja?'ゼロ: 思考オフ · 自動: 内容に応じて切り替え · MAX: 計画・実装・検証・JSON生成でも常に思考オン。':'Zero: off · Auto: adapts to the task · MAX: always on, including planning, implementation, verification and JSON.'}</small>}
            {!model && <small role="alert">{ja?"モデル情報を取得できません。":"Could not load model information."}</small>}
          </section>;
        })}
      </div>}

      {phase === 'providers' && <>
        <button type="button" className="brain-stage-back" onClick={() => setPhase('effort')}>{ja?"↩︎ 推論強度":"↩︎ Effort"}</button>
        <nav className="brain-provider-list" aria-label={ja?"モデルプロバイダー":"Model providers"}>
          {PROVIDERS.map(item => {
            const count = allModels.filter(model => providerTab(model) === item.id).length;
            return <button key={item.id} ref={element => { providerButtons.current[item.id] = element; }}
              type="button" className="brain-provider-card" aria-pressed={provider === item.id}
              onClick={() => chooseProvider(item.id)}>
              <span>{providerLabel(item)}</span><small>{item.id==='openai'&&chatgpt?.connection_state==='reauth_required'?(ja?'ChatGPTの再接続が必要':'Reconnect ChatGPT'):count ? (ja?`${count} モデル`:`${count} ${count===1?'model':'models'}`) : item.id==='openai'&&chatgpt?.configured?(ja?'ChatGPT接続済み · 一覧を取得できません':'ChatGPT connected · catalog unavailable'):(ja?'接続済みモデルなし':'No connected models')}</small><b aria-hidden="true">›</b>
            </button>;
          })}
        </nav>
      </>}

      {phase === 'models' && modelRole && <>
        <div className="brain-model-stage-heading"><button type="button" aria-label={ja?"プロバイダー一覧に戻る":"Back to providers"}
          className="brain-back-button" onClick={() => setPhase('providers')}>↩︎</button>
          <strong>{PROVIDERS.find(item => item.id === provider)?.label[language]}</strong>
          <span>{ja?`${roleLabel(modelRole)} · モデルを選択`:`${roleLabel(modelRole)} · Choose model`}</span></div>
        {provider==='openai'&&chatgpt?.connection_state==='reauth_required'&&<div className="brain-connection-recovery"><p>{ja?'ChatGPTの接続期限が切れています。再ログインすると、このアカウントのモデルを読み直します。':'Your ChatGPT connection expired. Sign in again to reload your account models.'}</p><button type="button" disabled={reconnecting} onClick={()=>void reconnectChatGPT()}>{reconnecting?(ja?'接続を確認中…':'Connecting…'):(ja?'ChatGPTに再接続':'Reconnect ChatGPT')}</button>{connectionError&&<p role="alert">{connectionError}</p>}</div>}
        <input ref={search} className="brain-search" aria-label={ja?"モデル名を検索":"Search model names"}
          placeholder={ja?"モデル名を検索…":"Search model names…"} value={query} onChange={event => setQuery(event.target.value)} />
        <CredentialAccessNotice error={state.error} onRestored={state.refresh}/>
        <section className="brain-model-list" aria-label={ja?`${roleLabel(modelRole)}のモデル`:`${roleLabel(modelRole)} models`}>
          {models.map(model => <button type="button" className="brain-option" key={model.id}
            aria-pressed={state.configuration.selections[modelRole] === model.id}
            disabled={state.loading} onClick={() => void chooseModel(modelRole, model.id)}>
            <span>{model.label}</span>
            <small>{providerDisplayLabel(model.provider,language)} · {model.local ? (ja?'このMac':'This Mac') : (ja?'接続済み':'Connected')}</small>
            <b aria-hidden="true">{state.configuration.selections[modelRole] === model.id ? '✓' : ''}</b>
          </button>)}
          {!models.length && !state.loading && <p>{providerModels.length
            ? (ja?'検索条件に一致するモデルがありません。':'No models match your search.')
            : provider==='local'
              ? (ja?'選択できるローカルモデルがありません。「モデル設定」からモデルをダウンロードしてください。':'No local models are available. Download a model in Model settings.')
              : (ja?'このプロバイダーのモデルは未接続です。「モデル設定」から接続してください。':'No models are connected for this provider. Connect one in Model settings.')}</p>}
        </section>
      </>}

      <footer>
        <small>{ja?(state.loading?'読み込み中…':phase==='effort'?'推論強度はモデルごとの設定です。変更は次のリクエストから適用されます。':'変更は次のリクエストから適用されます。'):(state.loading?'Loading…':phase==='effort'?'Effort is configured per model. Changes apply to the next request.':'Changes apply to the next request.')}</small>
        {onSettings && <button type="button" onClick={() => { setOpen(false); onSettings(); }}>{ja?"モデル設定 →":"Model settings →"}</button>}
      </footer>
    </div>}
  </div>;
}
