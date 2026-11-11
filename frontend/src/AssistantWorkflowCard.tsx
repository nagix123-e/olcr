import {BrowserViewport} from './BrowserViewport';
import {useId} from 'react';
import {UiIcon, type UiIconName} from './UiIcon';
import './assistant-workflow.css';

export type AssistantWorkflowBlock = {
  type: 'assistant_workflow';
  id: string;
  conversation_id: string;
  domain: 'personal' | 'tools_forms' | 'memory' | 'commerce' | 'mail';
  status: 'collecting' | 'review' | 'accepted' | 'completed' | 'error';
  title?: string;
  details?: string[];
  actions?: {label: string; message: string}[];
  browser_session_id?:string;
};

const domainIcons: Record<AssistantWorkflowBlock['domain'], UiIconName> = {
  personal: 'calendar', tools_forms: 'tools', memory: 'knowledge', commerce: 'shopping', mail: 'mail',
};

/** Workflow actions travel through the conversation; this card never mutates task data. */
export function AssistantWorkflowCard({block, ja, busy = false, onAction,api,latest=true}: {
  block: AssistantWorkflowBlock;
  ja: boolean;
  busy?: boolean;
  onAction?: (message: string) => void;
  api?:(path:string,init?:RequestInit)=>Promise<any>;
  latest?:boolean;
}) {
  const headingId = useId();
  const domainLabels = ja
    ? {personal: 'タスク・目標', tools_forms: 'フォーム・ツール', memory: '記憶', commerce: 'ショッピング', mail: 'メール'}
    : {personal: 'Tasks and goals', tools_forms: 'Forms and tools', memory: 'Memory', commerce: 'Shopping', mail: 'Mail'};
  const statusLabels = ja
    ? {collecting: '内容を確認中', review: '確認してください', accepted: '送信依頼済み', completed: '完了', error: '確認が必要です'}
    : {collecting: 'Gathering details', review: 'Ready for review', accepted: 'Request accepted', completed: 'Completed', error: 'Needs attention'};
  const domainLabel = domainLabels[block.domain];
  return <section className={`assistant-workflow assistant-workflow--${block.status}`} aria-labelledby={headingId} aria-busy={busy}>
    <div className="assistant-workflow-heading">
      <span className="assistant-workflow-domain"><UiIcon name={domainIcons[block.domain]} size={16}/>{domainLabel}</span>
      <span className="assistant-workflow-status" role="status">{busy ? (ja ? '処理中…' : 'Processing…') : statusLabels[block.status]}</span>
    </div>
    <h3 id={headingId}>{block.title || domainLabel}</h3>
    {Boolean(block.details?.length) && <ul className="assistant-workflow-details">
      {block.details!.map((detail, index) => <li key={index}>{detail}</li>)}
    </ul>}
    {api&&latest&&block.browser_session_id&&<BrowserViewport api={api} sessionId={block.browser_session_id} active={block.status==='collecting'||block.status==='review'}/>}
    {Boolean(block.actions?.length) && <div className="assistant-workflow-actions">
      {block.actions!.map((action, index) => <button key={`${action.message}-${index}`} type="button"
        disabled={busy || !onAction} onClick={() => onAction?.(action.message)}>{action.label}</button>)}
    </div>}
  </section>;
}
