import {useEffect, useId, useState} from 'react';
import {useLanguage} from './language';
import {SettingHelp} from './SettingHelp';

type Api = (path: string, init?: RequestInit) => Promise<any>;
type Controls = {yarn_target_tokens: number; local_context_tokens: number};
const defaults: Controls = {yarn_target_tokens: 524288, local_context_tokens: 32768};

export function ContextExtensionSettings({api}: {api: Api}) {
  const {language} = useLanguage();
  const ja = language === 'ja';
  const id = useId();
  const [value, setValue] = useState(defaults);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let live = true;
    api('/settings').then(data => {
      if (live) {setValue(Object.fromEntries(Object.entries(defaults).map(([key, fallback]) => [key, data[key] ?? fallback])) as Controls); setLoaded(true);}
    }).catch(reason => {if (live) setError(String(reason?.message || reason));});
    return () => {live = false;};
  }, [api]);
  function update(patch: Partial<Controls>) {setValue(previous => ({...previous, ...patch})); setSaved(false);}
  return <section className="brain-setting-section" aria-label={ja ? 'ローカルモデルのコンテキスト' : 'Local model context'}>
    <h3>{ja ? 'ローカルモデルのコンテキスト' : 'Local model context'}</h3>
    <p>{ja ? 'YaRN・コンテキスト自動圧縮のオン／オフは、設定の「チャットで使う機能」で切り替えます。ここではコンテキスト長を調整できます。' : 'Turn YaRN and automatic context compression on or off in Settings → Chat capabilities. Adjust context lengths here.'}</p>
    <form onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError(''); setSaved(false);
      try {await api('/settings/retrieval', {method: 'PATCH', body: JSON.stringify(value)}); setSaved(true);}
      catch (reason) {setError(reason instanceof Error ? reason.message : String(reason));}
      finally {setBusy(false);}
    }}>
      <fieldset disabled={!loaded || busy}>
        <p>{ja ? 'MLX版Ornith・Bonsaiのテキスト推論に適用します。モデル本来の長さを超えた場合にYaRNを適用します。Ollamaを選ぶ場合はオフにしてください。OllamaのAPIからはYaRNを変更できません。' : 'Applies to Ornith and Bonsai text inference in MLX when the target exceeds the native window. Turn this off for Ollama: its API cannot change YaRN.'}</p>
        <div className="setting-field"><span className="setting-label-with-help"><label htmlFor={`${id}-yarn`}>{ja ? 'YaRNの延長先（トークン）' : 'YaRN target (tokens)'}</label><SettingHelp label={ja?'YaRNの延長先':'YaRN target'}>{ja?'一度に扱える文脈の長さを延ばす目標値です。トークンはモデルが文章を処理する単位で、文字数とは一致しません。':'The target context length when extending the model’s window. Tokens are model processing units, not character counts.'}</SettingHelp></span><input id={`${id}-yarn`} type="number" required min={2048} max={2097152} step={1} value={value.yarn_target_tokens} onChange={e => update({yarn_target_tokens: Number(e.target.value)})}/></div>
        <p>{ja ? '拡張倍率はモデル本来の長さから自動計算します。延長するとメモリ使用量が増え、応答品質が変わる場合があります。' : 'The scaling factor is calculated from the native window. Longer contexts use more memory and may affect response quality.'}</p>
        <div className="setting-field"><span className="setting-label-with-help"><label htmlFor={`${id}-context`}>{ja ? '通常のコンテキスト長（トークン）' : 'Normal context window (tokens)'}</label><SettingHelp label={ja?'通常のコンテキスト長':'Normal context window'}>{ja?'会話履歴・参考資料・回答を合わせて扱う長さです。自動圧縮を有効にすると、この長さを目安に古い文脈を短縮します。':'The window shared by history, references and the answer. Automatic compression uses it to decide when to shorten older context.'}</SettingHelp></span><input id={`${id}-context`} type="number" required min={2048} max={2097152} step={1} value={value.local_context_tokens} onChange={e => update({local_context_tokens: Number(e.target.value)})}/></div>
        <p>{ja ? '推定使用量が応答分を含めて80%に達すると、過去の会話や参考資料を抜粋して短縮します。元の会話は残り、現在の入力と必須の指示は保持します。YaRN有効時のMLXでは延長先を基準にします。Ollamaでは通常の長さを送信します。外部APIには適用しません。' : 'At 80% estimated usage, including response space, excerpt older conversation and supporting evidence. Original history, current input and required instructions are preserved. MLX uses the YaRN target when enabled. Ollama receives the normal window. External APIs are unaffected.'}</p>
        <button type="submit">{busy ? (ja ? '保存中…' : 'Saving…') : (ja ? 'コンテキスト設定を保存' : 'Save context settings')}</button>
      </fieldset>
    </form>
    {saved && <p role="status">{ja ? '保存しました。次のリクエストから適用されます。' : 'Saved. Applies to the next request.'}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
