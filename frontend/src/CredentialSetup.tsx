import { useState, type FormEvent } from 'react';
import { native, openExternal } from './native';
import './credential-setup.css';
import {CredentialFields} from './CredentialFields';
import {useLanguage} from './language';

export type CredentialSetupBlock = {
  type: 'credential_setup';
  tool_id: string;
  provider: string;
  credential_kind?: 'external_tool' | 'web_search';
  web_provider?: 'brave' | 'tavily';
  setup_url: string;
  link_label?: string;
  guidance?: string;
  fields: Array<{ id: string; label: string; input_type: 'text' | 'email' | 'password'; keychain_handle?: string }>;
};

export function CredentialSetupCard({ block, api, onSaved }: {
  block: CredentialSetupBlock;
  api: (path: string, init?: RequestInit) => Promise<any>;
  onSaved?: () => void | Promise<void>;
}) {
  const {language}=useLanguage();const ja=language==="ja";
  const [values, setValues] = useState<Record<string, string>>({});
  const [status, setStatus] = useState('');
  const [saving, setSaving] = useState(false);
  const setupUrl = block.setup_url.startsWith('https://') ? block.setup_url : null;

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (saving || block.fields.some(field => !values[field.id]?.trim())) return;
    const bridge = native();
    if (!bridge) { setStatus(ja?'OLCRデスクトップのキーチェーンを利用できません。':'OLCR desktop Keychain is unavailable.'); return; }
    setSaving(true);
    setStatus('');
    try {
      let result: any;
      if (block.credential_kind === 'web_search') {
        const provider = block.web_provider;
        const key = values.api_key?.trim();
        if (!provider || !key) throw new Error(ja?'Web検索プロバイダーまたはAPIキーが不正です。':'Invalid web search provider or API key.');
        await bridge.core.invoke('set_web_credential', { provider, key });
        result = await api('/web/credentials', {
          method: 'PUT', body: JSON.stringify({ provider, key }),
        });
      } else {
        for (const field of block.fields) {
          if (!field.keychain_handle) throw new Error(ja?'Keychainの保存先がありません。':'Keychain handle is missing.');
          await bridge.core.invoke('set_external_credential', {
            handle: field.keychain_handle,
            value: values[field.id].trim(),
          });
        }
        result = await api('/external-tools/credentials', {
          method: 'PUT',
          body: JSON.stringify({ tool_id: block.tool_id, credentials: values }),
        });
      }
      setValues({});
      setStatus(block.credential_kind === 'web_search'
        ? (ja?'APIキーをこのMacのキーチェーンに保存しました。Web Search設定や外部アクセス設定は変更していません。':'The API key was saved to this Mac’s Keychain. Web Search and External Access settings were not changed.')
        : result.external_access_enabled
          ? (ja?'認証情報をこのMacのキーチェーンに保存しました。外部アクセス設定は変更していません。':'Credentials were saved to this Mac’s Keychain. External access was not changed.')
          : (ja?'認証情報をこのMacのキーチェーンに保存しました。外部アクセスはOFFのままです。':'Credentials were saved to this Mac’s Keychain. External access remains OFF.'));
      await onSaved?.();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : ja?'認証情報を保存できませんでした。':'Could not save credentials.');
    } finally {
      setSaving(false);
    }
  };

  return <section className="credential-setup" aria-label={ja?`${block.provider}の認証情報設定`:`${block.provider} credential setup`}>
    <h3>{ja?`${block.provider}の認証情報が必要です`:`Credentials required for ${block.provider}`}</h3>
    <p>{ja?"プロバイダーのページで認証情報を取得し、下の専用欄に入力してください。通常のチャット本文には送信されません。":"Get credentials from the provider and enter them below. They are never sent in regular chat messages."}</p>
    {setupUrl && <p><a href={setupUrl} target="_blank" rel="noopener noreferrer"
      onClick={event => {
        event.preventDefault();
        void openExternal(setupUrl).catch(() => window.open(setupUrl, '_blank', 'noopener,noreferrer'));
      }}
    >{block.link_label || (ja?`${block.provider}のAPI設定・ドキュメント`:`${block.provider} API settings and documentation`)}</a></p>}
    {block.guidance && <p>{block.guidance}</p>}
    <form onSubmit={save}>
      <CredentialFields fields={block.fields} values={values} disabled={saving} onChange={(id,value)=>setValues(current=>({...current,[id]:value}))}/>
      <button type="submit" disabled={saving || block.fields.some(field => !values[field.id]?.trim())}>
        {ja?(saving?'保存中…':'認証情報をキーチェーンに保存'):(saving?'Saving…':'Save credentials to Keychain')}
      </button>
    </form>
    <p className="credential-setup-status" role="status">{status}</p>
    <small>{ja?"保存先はこのMacのキーチェーンです。認証情報を保存するだけでは外部へ接続しません。":"Credentials are saved to this Mac’s Keychain. Saving them does not make an external connection."}</small>
  </section>;
}
