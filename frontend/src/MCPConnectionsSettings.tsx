import {StatusBadge,Feedback} from './InterfacePrimitives';
import {useCallback, useEffect, useState} from 'react';
import {useLanguage} from './language';
import {SettingHelp} from './SettingHelp';
import './skills-library.css';
import './mcp-connections.css';

type Server = {
  server: string; display_name: string; scope: string; can_check: boolean;
  connection_status: string; reason: string; checked_at?: string | null; tool_count?: number | null;
};

export function MCPConnectionsSettings({api}: {api: (path: string, init?: RequestInit) => Promise<any>}) {
  const ja = useLanguage().language === 'ja';
  const [servers, setServers] = useState<Server[] | null>(null);
  const [checking, setChecking] = useState('');
  const [error, setError] = useState('');
  const [reload,setReload]=useState(0);
  const probe = useCallback(async (name: string, active: () => boolean) => {
    if (!active()) return;
    setChecking(name);
    try {
      const result: Server = await api(`/mcp/connections/${encodeURIComponent(name)}/check`, {method: 'POST'});
      if (active()) setServers(previous => previous?.map(row => row.server === name ? {...row, ...result} : row) ?? null);
    } catch (reason) {
      if (active()) {
        setError(String(reason));
        setServers(previous => previous?.map(row => row.server === name
          ? {...row, connection_status:'FAILED', reason:'CONNECTION_REQUEST_FAILED', checked_at:null, tool_count:null} : row) ?? null);
      }
    } finally {if (active()) setChecking('');}
  }, [api]);
  useEffect(() => {
    let active = true;
    setError('');
    void (async () => {
      try {
        const result = await api('/mcp/connections');
        if (!active) return;
        setServers(result.servers);
        for (const row of result.servers as Server[]) {
          if (!active) break;
          if (row.can_check && ['NOT_CHECKED', 'UNAVAILABLE'].includes(row.connection_status)) {
            await probe(row.server, () => active);
          }
        }
      } catch (reason) {if (active) setError(String(reason));}
    })();
    return () => {active = false;};
  }, [api, probe, reload]);
  const statuses: Record<string, string> = ja ? {
    NOT_CHECKED: '未確認', CHECKING: '接続確認中…', CONNECTED: '接続確認済み',
    DISABLED: '無効', UNAVAILABLE: 'リソース不足', FAILED: '接続失敗',
  } : {
    NOT_CHECKED: 'Not checked', CHECKING: 'Checking…', CONNECTED: 'Connection verified',
    DISABLED: 'Disabled', UNAVAILABLE: 'Resources missing', FAILED: 'Connection failed',
  };
  const reasons: Record<string, string> = ja ? {
    POLICY_DISABLED: '現在の配布ポリシーで無効になっています。',
    NODE_RUNTIME_MISSING: '接続時に専用Node環境を準備します。',
    PYTHON_RUNTIME_MISSING: '接続時にSerenaの専用環境を準備します。',
    RUNTIME_SETUP_FAILED: '専用環境を準備できませんでした。接続を再確認してください。',
    CONNECTION_REQUEST_FAILED: '接続確認を完了できませんでした。再確認できます。',
    SERVER_RESOURCE_MISSING: '同梱サーバーが見つかりません。',
    CORPUS_RESOURCE_MISSING: '参照データが見つかりません。',
    REQUEST_TIMEOUT: 'サーバーからの応答が時間内に届きませんでした。再確認できます。',
    PROCESS_START_FAILED: '同梱サーバーを起動できませんでした。',
    PROCESS_EXITED: 'サーバーが接続中に終了しました。',
    REQUIRED_TOOL_MISSING: '接続できましたが、必要なツールが公開されていません。',
  } : {
    POLICY_DISABLED: 'Disabled by the current packaged policy.',
    NODE_RUNTIME_MISSING: 'The dedicated Node environment will be prepared when connecting.',
    PYTHON_RUNTIME_MISSING: 'The dedicated Serena environment will be prepared when connecting.',
    RUNTIME_SETUP_FAILED: 'Could not prepare the environment. Check the connection again.',
    CONNECTION_REQUEST_FAILED: 'The connection check could not complete. You can try again.',
    SERVER_RESOURCE_MISSING: 'The bundled server is missing.',
    CORPUS_RESOURCE_MISSING: 'The reference data is missing.',
    REQUEST_TIMEOUT: 'The server did not respond in time. You can check again.',
    PROCESS_START_FAILED: 'The bundled server could not start.',
    PROCESS_EXITED: 'The server exited during the connection.',
    REQUIRED_TOOL_MISSING: 'Connected, but required tools are not advertised.',
  };
  const check = async (name: string) => {
    if (checking) return;
    setError('');
    await probe(name, () => true);
  };
  return <section className="skills-library mcp-connections" aria-label={ja?'MCP一覧':'MCP library'}>
    <div className="setting-help-heading"><h2>{ja ? 'MCP接続' : 'MCP connections'}</h2><SettingHelp label={ja?'MCP接続':'MCP connections'}>{ja?'MCPはツールを接続するための共通の仕組みです。「接続確認」は応答できるかを調べるだけで、機能を実行しません。':'MCP is a shared protocol for connecting tools. A connection check tests availability without executing a tool.'}</SettingHelp></div>
    <p className="setting-hint">{ja
      ? 'この画面を開くと、未確認のMCPへの接続を確認します。初回は必要な専用環境を準備します。確認後は接続を閉じます。ツールの実行や自動利用の許可は行いません。'
      : 'Opening this page checks unverified MCP connections and prepares missing environments on first use, then closes each connection. This does not invoke tools or authorize automatic use.'}</p>
    <section className="skills-library-group"><h2>{ja?'登録済みMCP':'Registered MCP servers'} <span>{servers?.length ?? 0}</span></h2>
      <div className="skills-library-grid">
        {servers?.map(row => <article className="skill-library-card" key={row.server}>
          <div className="mcp-connection-heading">
            <h3 className="mcp-server-name">{row.display_name}</h3>
            <StatusBadge state={checking===row.server?'checking':row.connection_status}>{statuses[checking === row.server ? 'CHECKING' : row.connection_status] ?? row.connection_status}</StatusBadge>
            {row.can_check && <button disabled={!!checking} onClick={() => void check(row.server)}
              aria-label={`${row.display_name} ${ja ? 'の接続を確認' : 'check connection'}`}>{ja ? '接続確認' : 'Check connection'}</button>}
          </div>
          {row.scope === 'managed_browser' && <p className="setting-hint">{ja ? '管理ブラウザー用。コーディングTaskへの直接公開は無効です。' : 'Managed browser adapter. Direct exposure to coding tasks is disabled.'}</p>}
          {row.reason !== 'READY' && row.reason !== 'NOT_CHECKED' && <p className="setting-hint">{reasons[row.reason] ?? `${ja ? '接続エラー' : 'Connection error'}: ${row.reason}`}</p>}
          {row.checked_at && <p className="setting-hint">{ja ? '最終確認' : 'Last checked'}: {new Date(row.checked_at).toLocaleString()}
            {row.tool_count != null ? ` · ${row.tool_count} ${ja ? 'ツール' : 'tools'}` : ''}</p>}
        </article>)}
        {servers?.length === 0 && <Feedback kind="empty">{ja?'登録されたMCPはありません。':'No registered MCP servers.'}</Feedback>}
        {!servers && !error && <Feedback>{ja ? '読み込み中…' : 'Loading…'}</Feedback>}
      </div>
    </section>
    {error && <Feedback kind="error" onRetry={()=>setReload(value=>value+1)}>{error}</Feedback>}
  </section>;
}
