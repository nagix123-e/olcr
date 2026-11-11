import type { CSSProperties } from 'react';
import { api } from './api';
import { BrowserViewport } from './BrowserViewport';
import {useLanguage} from './language';

export type LocalE2ETaskBlock = {
  type: 'local_e2e_task';
  session_id: string;
  run_id: string;
  origin: string;
  url: string;
  title?: string;
  status: string;
  snapshot?: string;
};

const previewStyle: CSSProperties = {
  maxHeight: 280,
  overflow: 'auto',
  whiteSpace: 'pre-wrap',
  padding: 12,
  borderRadius: 8,
  background: 'rgba(0, 0, 0, 0.08)',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontSize: 12,
};

export function LocalE2ECard({ block }: { block: LocalE2ETaskBlock }) {
  const {language}=useLanguage();const ja=language==="ja";
  return <section className="panel local-e2e-card" aria-label={ja?"ローカルE2Eブラウザー":"Local E2E browser"}>
    <div className="row"><span>{ja?"ローカルE2E":"LOCAL E2E"} · {block.title || block.origin}<small>{block.url}</small></span><b>{block.status}</b></div>
    <BrowserViewport api={api} sessionId={block.session_id} active={block.status === 'READY' || block.status === 'STARTING'} />
    {block.snapshot && <pre style={previewStyle}>{block.snapshot}</pre>}
  </section>;
}
