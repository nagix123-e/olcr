import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {AssistantWorkflowCard, type AssistantWorkflowBlock} from './AssistantWorkflowCard';

afterEach(cleanup);

it('shows a mail draft and sends its bound confirmation through chat', () => {
  const onAction=vi.fn();
  render(<AssistantWorkflowCard block={{type:'assistant_workflow',id:'mail-draft',conversation_id:'c',domain:'mail',status:'review',title:'送信内容を確認',details:['宛先：friend@example.org','件名：旅行の相談','本文：\n旅行の予定を教えてください。'],actions:[{label:'この内容で送信',message:'/workflow mail-draft confirm'}]}} ja onAction={onAction}/>);
  expect(screen.getByText('メール')).toBeInTheDocument();
  expect(screen.getByText('宛先：friend@example.org')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'この内容で送信'}));
  expect(onAction).toHaveBeenCalledWith('/workflow mail-draft confirm');
});

const block: AssistantWorkflowBlock = {
  type: 'assistant_workflow', id: 'draft-1', conversation_id: 'conversation-1',
  domain: 'tools_forms', status: 'review', title: 'Review the form draft',
  details: ['Name: Example Person', 'Destination: https://example.com/form'],
  actions: [
    {label: 'Confirm', message: '/workflow draft-1 confirm'},
    {label: 'Cancel', message: '/workflow draft-1 cancel'},
  ],
};

describe('AssistantWorkflowCard', () => {
  it('shows review details and sends confirm and cancel messages through the chat callback', () => {
    const onAction = vi.fn();
    render(<AssistantWorkflowCard block={block} ja={false} onAction={onAction}/>);
    expect(screen.getByRole('region', {name: 'Review the form draft'})).toBeInTheDocument();
    expect(screen.getByText('Name: Example Person')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Ready for review');
    fireEvent.click(screen.getByRole('button', {name: 'Confirm'}));
    fireEvent.click(screen.getByRole('button', {name: 'Cancel'}));
    expect(onAction.mock.calls).toEqual([
      ['/workflow draft-1 confirm'], ['/workflow draft-1 cancel'],
    ]);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('disables all actions while the chat is busy and announces processing', () => {
    const onAction = vi.fn();
    render(<AssistantWorkflowCard block={block} ja busy onAction={onAction}/>);
    screen.getAllByRole('button').forEach(button => expect(button).toBeDisabled());
    expect(screen.getByRole('region')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('処理中');
    fireEvent.click(screen.getByRole('button', {name: 'Confirm'}));
    expect(onAction).not.toHaveBeenCalled();
  });

  it('keeps a historical card read-only when no action callback is provided', () => {
    render(<AssistantWorkflowCard block={block} ja={false}/>);
    screen.getAllByRole('button').forEach(button => expect(button).toBeDisabled());
  });

  it('renders completed summaries without action controls or empty detail lists', () => {
    render(<AssistantWorkflowCard block={{...block, domain: 'personal', status: 'completed', title: undefined, details: undefined, actions: undefined}} ja/>);
    expect(screen.getByRole('region', {name: 'タスク・目標'})).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('完了');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('labels an Apple Mail handoff as accepted rather than completed', () => {
    render(<AssistantWorkflowCard block={{...block, domain:'mail', status:'accepted', title:'送信依頼済み', details:['Apple Mailが送信依頼を受け付けました。'], actions:[]}} ja/>);
    expect(screen.getByRole('status')).toHaveTextContent('送信依頼済み');
    expect(screen.getByRole('status')).not.toHaveTextContent('完了');
  });

  it('renders titles, details and action labels as text, never HTML', () => {
    const html = '<img src=x onerror=alert(1)>';
    const {container} = render(<AssistantWorkflowCard block={{...block, title: html, details: [html], actions: [{label: html, message: '/workflow draft-1 cancel'}]}} ja={false} onAction={vi.fn()}/>);
    expect(screen.getByRole('heading', {name: html})).toBeInTheDocument();
    expect(screen.getByRole('listitem')).toHaveTextContent(html);
    expect(screen.getByRole('button', {name: html})).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });

  it.each([
    ['collecting', 'Gathering details'], ['error', 'Needs attention'],
  ] as const)('shows the %s state without adding an action', (status, label) => {
    render(<AssistantWorkflowCard block={{...block, status, actions: []}} ja={false}/>);
    expect(screen.getByRole('status')).toHaveTextContent(label);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
