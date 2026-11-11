import {useEffect, useId, useState} from 'react';
import {useLanguage} from './language';
import {SettingHelp} from './SettingHelp';

type Api = (path: string, init?: RequestInit) => Promise<any>;
type Model = {id: string; label: string; kind: string};

export function RetrievalSettings({api, models}: {api: Api; models: Model[]}) {
  const {language} = useLanguage();
  const ja = language === 'ja';
  const id = useId();
  const [loaded, setLoaded] = useState(false);
  const [budget, setBudget] = useState(8000);
  const [threshold, setThreshold] = useState(.55);
  const [overrides, setOverrides] = useState<Record<string, number>>({});
  const [model, setModel] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    api('/settings').then(value => {
      if (!live) return;
      setBudget(value.context_budget ?? 8000);
      setThreshold(value.rag_min_relevance ?? .55);
      setOverrides(value.model_context_budgets ?? {});
      setLoaded(true);
    }).catch(reason => {if (live) setError(String(reason?.message || reason));});
    return () => {live = false;};
  }, [api]);
  const choices = new Map(models.filter(item => item.kind === 'language').map(item => [item.id, item.label]));
  Object.keys(overrides).forEach(id => {if (!choices.has(id)) choices.set(id, id);});
  return <section className="brain-setting-section rag-context-settings" aria-label={ja ? 'RAG・コンテクスト' : 'RAG and context'}>
    <div className="setting-help-heading"><h3>{ja ? 'RAG・コンテクスト' : 'RAG and context'}</h3><SettingHelp label={ja?'RAG':'RAG'}>{ja?'参考資料や過去の会話から、今の質問に関係する部分を探してモデルに渡す仕組みです。':'Retrieve relevant passages from references and past conversations to inform the current answer.'}</SettingHelp></div>
    <p>{ja ? 'Graphify・検索結果・会話履歴から、今の依頼に関連する情報を選びます。直近の内容も、関連性が低ければ含めません。' : 'Select information relevant to the current request from Graphify, search results, and conversation history. Recent content must also meet the relevance threshold.'}</p>
    <form onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError(''); setStatus('');
      try {
        await api('/settings/retrieval', {method: 'PATCH', body: JSON.stringify({context_budget: budget, rag_min_relevance: threshold, model_context_budgets: overrides})});
        setStatus(ja ? '保存しました。次のリクエストから適用されます。' : 'Saved. Applies to the next request.');
      } catch (reason) {setError(reason instanceof Error ? reason.message : String(reason));}
      finally {setBusy(false);}
    }}>
      <fieldset disabled={!loaded || busy}>
        <div className="setting-field"><span className="setting-label-with-help"><label htmlFor={`${id}-budget`}>{ja ? '追加コンテクストの上限目安（文字）' : 'Supplemental context allowance (characters)'}</label><SettingHelp label={ja?'追加コンテクストの上限':'Supplemental context allowance'}>{ja?'検索結果や会話履歴を追加する量です。増やすと参考情報が増えますが、処理時間やモデルの使用メモリも増える場合があります。':'Controls how much retrieved context is added. More context can increase processing time and model memory use.'}</SettingHelp></span><input id={`${id}-budget`} type="number" min={256} max={200000} required value={budget} onChange={event => setBudget(Number(event.target.value))}/></div>
        <p>{ja ? '入力文と必須の指示は別です。モデルが扱える量に合わせて調整してください。トークン数とは異なります。' : 'User input and required instructions are separate. Adjust to your model’s capacity. Characters are not tokens.'}</p>
        <div className="setting-field"><span className="setting-label-with-help"><label htmlFor={`${id}-threshold`}>{ja ? '関連性のしきい値' : 'Relevance threshold'}</label><SettingHelp label={ja?'関連性のしきい値':'Relevance threshold'}>{ja?'関係の薄い資料が混ざるときは上げ、必要な資料が拾われないときは下げて調整します。回答の正しさを表す数値ではありません。':'Raise it to exclude unrelated material; lower it if useful references are missed. This is not an answer confidence score.'}</SettingHelp></span><input id={`${id}-threshold`} type="range" min={.05} max={1} step={.05} value={threshold} onChange={event => setThreshold(Number(event.target.value))}/><output htmlFor={`${id}-threshold`}>{threshold.toFixed(2)}</output></div>
        <p>{ja ? '高いほど厳選し、関連性が低い情報を除外します。低くすると幅広く参照します。' : 'Higher values select more strictly. Lower values include a broader range of relevant material.'}</p>
        <div className="setting-field"><span className="setting-label-with-help"><label htmlFor={`${id}-model`}>{ja ? 'モデル別の設定' : 'Per-model allowance'}</label><SettingHelp label={ja?'モデル別の設定':'Per-model allowance'}>{ja?'扱える文脈の量が異なるモデルごとに上限を調整できます。個別の値を空欄にすると共通設定を使います。':'Set a different allowance for each model’s capacity. Leave its value blank to use the shared allowance.'}</SettingHelp></span><select id={`${id}-model`} value={model} onChange={event => setModel(event.target.value)}><option value="">{ja ? 'モデルを選択' : 'Choose model'}</option>{[...choices].map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></div>
        {model && <label>{ja ? 'このモデルの上限目安（空欄で共通設定）' : 'Allowance for this model (blank to inherit)'}<input type="number" min={256} max={200000} value={overrides[model] ?? ''} placeholder={String(budget)} onChange={event => setOverrides(previous => {
          const next = {...previous};
          if (event.target.value === '') delete next[model]; else next[model] = Number(event.target.value);
          return next;
        })}/></label>}
        <p>{ja ? '役割ごとに選ばれたモデルや、フォールバック先にも、そのモデルの設定を適用します。' : 'The selected model’s allowance applies to every role, including fallback attempts.'}</p>
        <button type="submit">{busy ? (ja ? '保存中…' : 'Saving…') : (ja ? 'RAG設定を保存' : 'Save RAG settings')}</button>
      </fieldset>
    </form>
    {status && <p role="status">{status}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
