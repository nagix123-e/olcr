import { useEffect, useState } from 'react';

type Tool = { tool_id: string; provider: string; availability: string; external_authorized: boolean; credential: string; endpoint_mode: string };
type State = { external_access_enabled: boolean; tools: Tool[] };

export function ExternalToolsSettings({ api, settings, save }: { api: (path: string, init?: RequestInit) => Promise<any>; settings: Record<string, any> | null; save: (next: Record<string, any>) => Promise<any> }) {
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState('');
  const load = () => api('/external-tools').then(setState).catch(e => setError(e.message));
  useEffect(() => { void load(); }, []);
  const toggleExternal = () => {
    if (!state || !settings) return;
    void save({ ...settings, external_access_enabled: !state.external_access_enabled }).then(() => load()).catch(e => setError(e.message));
  };
  const toggleTaskManager = (enabled: boolean) => {
    if (!settings) return;
    void save({ ...settings, task_manager_enabled: enabled }).catch(e => setError(e.message));
  };
  return <>
    <section className="panel">
      <h2>Coding Orchestrator</h2>
      <p>Coding Orchestrator: <b>{settings?.task_manager_enabled === true ? 'ON' : 'OFF'}</b></p>
      <div className="toggle">
        <button className={settings?.task_manager_enabled === true ? 'on' : ''} onClick={() => toggleTaskManager(true)}>ON</button>
        <button className={settings?.task_manager_enabled !== true ? 'on' : ''} onClick={() => toggleTaskManager(false)}>OFF</button>
      </div>
      <small>When OFF, coding requests use the normal Brain conversation.</small>
    </section>
    <section className="panel">
      <h2>External Tools · Global</h2>
      {state && <><p>External access: <b>{state.external_access_enabled ? 'AUTHORIZED' : 'OFF'}</b></p><button onClick={toggleExternal}>{state.external_access_enabled ? 'Disable external access' : 'Enable external access'}</button>{state.tools.map(tool => <div className="row" key={tool.tool_id}><span>{tool.provider}<small>{tool.tool_id} · {tool.credential}</small></span><b>{tool.availability}</b></div>)}</>}
      <p className="muted">Provider readiness does not grant network authorization. Only minimal structured parameters are sent.</p>
      {error && <p className="error">{error}</p>}
    </section>
  </>;
}
