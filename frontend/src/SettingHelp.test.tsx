import {act, cleanup, fireEvent, render, screen} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {SettingHelp} from './SettingHelp';

afterEach(() => {cleanup(); vi.useRealTimers();});

it('lets a pointer cross onto the tooltip to read it, and dismisses with Escape', () => {
  vi.useFakeTimers();
  render(<SettingHelp label="RAG">参考資料の関連する部分を探します。</SettingHelp>);
  const button = screen.getByRole('button', {name: 'RAGの説明'});
  fireEvent.pointerEnter(button);
  expect(screen.queryByRole('tooltip')).toBeNull();
  act(() => vi.advanceTimersByTime(250));
  const tooltip = screen.getByRole('tooltip');
  expect(button).toHaveAttribute('aria-describedby', tooltip.id);
  fireEvent.pointerLeave(button);
  act(() => vi.advanceTimersByTime(80));
  fireEvent.pointerEnter(tooltip);
  act(() => vi.advanceTimersByTime(300));
  expect(tooltip).toBeInTheDocument();
  fireEvent.keyDown(document, {key: 'Escape'});
  expect(screen.queryByRole('tooltip')).toBeNull();
  expect(button).not.toHaveAttribute('aria-describedby');
});

it('supports focus and tap without toggling a setting or submitting its form', () => {
  const submit = vi.fn(event => event.preventDefault()), change = vi.fn();
  render(<form onSubmit={submit}><span className="setting-label-with-help"><label htmlFor="enabled">機能</label><SettingHelp label="機能">この機能の補足です。</SettingHelp></span><input id="enabled" type="checkbox" onChange={change}/><button type="submit">保存</button></form>);
  const button = screen.getByRole('button', {name: '機能の説明'});
  act(() => button.focus());
  expect(screen.getByRole('tooltip')).toBeInTheDocument();
  fireEvent.click(button);
  expect(screen.getByRole('tooltip')).toBeInTheDocument();
  expect(submit).not.toHaveBeenCalled();
  expect(change).not.toHaveBeenCalled();
  expect(screen.getByRole('checkbox', {name: '機能'})).not.toBeChecked();
  fireEvent.click(button);
  expect(screen.queryByRole('tooltip')).toBeNull();
  expect(button).toHaveFocus();
  act(() => screen.getByRole('button', {name: '保存'}).focus());
  act(() => button.focus());
  expect(screen.getByRole('tooltip')).toBeInTheDocument();
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole('tooltip')).toBeNull();
});

it('shows only the requested explanation and removes portals on unmount', () => {
  const view = render(<><SettingHelp label="設定A">説明A</SettingHelp><SettingHelp label="設定B">説明B</SettingHelp></>);
  act(() => screen.getByRole('button', {name: '設定Aの説明'}).focus());
  expect(screen.getByRole('tooltip')).toHaveTextContent('説明A');
  act(() => screen.getByRole('button', {name: '設定Bの説明'}).focus());
  expect(screen.getAllByRole('tooltip')).toHaveLength(1);
  expect(screen.getByRole('tooltip')).toHaveTextContent('説明B');
  view.unmount();
  expect(screen.queryByRole('tooltip')).toBeNull();
});
