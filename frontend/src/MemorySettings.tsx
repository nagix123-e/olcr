import {useEffect, useId, useState} from 'react';
import {useLanguage} from './language';
import './memory-settings.css';

type MemoryValues = {
  conversation_memory_enabled: boolean;
  conversation_memory_cross_project_enabled: boolean;
  conversation_memory_min_relevance: number;
};
const valuesFrom = (settings: Record<string, any>): MemoryValues => ({
  conversation_memory_enabled: settings.conversation_memory_enabled ?? true,
  conversation_memory_cross_project_enabled: settings.conversation_memory_cross_project_enabled ?? false,
  conversation_memory_min_relevance: settings.conversation_memory_min_relevance ?? .55,
});

export function MemorySettings({settings, save}: {
  settings: Record<string, any>;
  save: (updates: MemoryValues) => Promise<unknown>;
}) {
  const {language} = useLanguage();
  const ja = language === 'ja';
  const id = useId();
  const [values, setValues] = useState(() => valuesFrom(settings));
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {setValues(valuesFrom(settings));}, [settings.conversation_memory_enabled,
    settings.conversation_memory_cross_project_enabled, settings.conversation_memory_min_relevance]);
  return <form className="memory-settings" aria-label={ja ? '会話の記憶設定' : 'Conversation memory settings'} onSubmit={async event => {
    event.preventDefault(); setBusy(true); setStatus(''); setError('');
    try {
      await save(values);
      setStatus(ja ? '保存しました。次のリクエストから適用されます。' : 'Saved. Applies to the next request.');
    } catch (reason) {setError(reason instanceof Error ? reason.message : String(reason));}
    finally {setBusy(false);}
  }}>
    <fieldset disabled={busy}>
      <div className="setting-row capability-setting memory-toggle-row">
        <div className="memory-toggle-copy">
          <label htmlFor={`${id}-enabled`}>{ja ? '過去の会話を参照' : 'Use past conversations'}</label>
          <p id={`${id}-enabled-hint`} className="setting-hint">{ja ? 'オフでも記憶の保存と索引作成は続きます。再びオンにすると参照できます。' : 'Turning this off keeps saving and indexing memories. Turn it on again to use them.'}</p>
        </div>
        <input id={`${id}-enabled`} type="checkbox" role="switch" aria-describedby={`${id}-enabled-hint`} checked={values.conversation_memory_enabled} onChange={event => {setStatus(''); setValues({...values, conversation_memory_enabled: event.target.checked});}}/>
      </div>
      <div className="setting-row capability-setting memory-toggle-row">
        <div className="memory-toggle-copy">
          <label htmlFor={`${id}-shared`}>{ja ? 'プロジェクトをまたいだ記憶' : 'Across-project memory'}</label>
          <p id={`${id}-shared-hint`} className="setting-hint">{ja ? 'オンでは他のプロジェクトや未設定の会話も参照します。オフでは同じプロジェクト内に限り、プロジェクト未設定の会話は未設定の会話だけを参照します。' : 'When on, memories can come from any project or conversations without a project. When off, retrieval stays within the current project; conversations without a project use only other conversations without a project.'}</p>
        </div>
        <input id={`${id}-shared`} type="checkbox" role="switch" aria-describedby={`${id}-shared-hint`} checked={values.conversation_memory_cross_project_enabled} onChange={event => {setStatus(''); setValues({...values, conversation_memory_cross_project_enabled: event.target.checked});}}/>
      </div>
      <div className="setting-field memory-threshold">
        <div className="memory-threshold-heading">
          <label htmlFor={`${id}-threshold`}>{ja ? '会話の記憶の関連性しきい値' : 'Conversation memory relevance threshold'}</label>
          <output htmlFor={`${id}-threshold`}>{values.conversation_memory_min_relevance.toFixed(2)}</output>
        </div>
        <input id={`${id}-threshold`} type="range" min={0} max={1} step={.01} value={values.conversation_memory_min_relevance} onChange={event => {setStatus(''); setValues({...values, conversation_memory_min_relevance: Number(event.target.value)});}}/>
      </div>
      <p className="setting-hint memory-threshold-hint">{ja ? '初期値は0.55です。値を上げるほど関連性が高い記憶だけを参照します。意味検索が使えないときの単語一致検索にも適用されます。' : 'Default: 0.55. Higher values require a closer match. The same threshold applies to keyword search when embeddings are unavailable.'}</p>
      <button type="submit" className="primary">{busy ? (ja ? '保存中…' : 'Saving…') : (ja ? '記憶設定を保存' : 'Save memory settings')}</button>
    </fieldset>
    {status && <p role="status">{status}</p>}
    {error && <p role="alert">{error}</p>}
  </form>;
}
