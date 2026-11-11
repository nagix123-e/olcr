import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {RetrievalSettings} from './RetrievalSettings';

afterEach(cleanup);
it('persists the allowance, relevance and model override through a narrow patch', async () => {
  const settings = {context_budget: 8000, rag_min_relevance: .35, model_context_budgets: {}};
  const api = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/settings') return settings;
    expect(path).toBe('/settings/retrieval');
    expect(init?.method).toBe('PATCH');
    return {...settings, ...JSON.parse(String(init?.body))};
  });
  render(<RetrievalSettings api={api} models={[{id: 'mlx:small', label: 'Small MLX', kind: 'language'}]}/>);
  const budget = await screen.findByRole('spinbutton', {name: /追加コンテクスト|Supplemental context/});
  await waitFor(() => expect((budget as HTMLInputElement).disabled).toBe(false));
  fireEvent.change(budget, {target: {value: '4096'}});
  fireEvent.change(screen.getByRole('slider'), {target: {value: '.7'}});
  fireEvent.change(screen.getByRole('combobox'), {target: {value: 'mlx:small'}});
  fireEvent.change(screen.getByRole('spinbutton', {name: /このモデル|Allowance for this model/}), {target: {value: '2048'}});
  fireEvent.click(screen.getByRole('button', {name: /RAG設定を保存|Save RAG settings/}));
  await screen.findByRole('status');
  const payload = JSON.parse(String(api.mock.calls.at(-1)?.[1]?.body));
  expect(payload).toEqual({context_budget: 4096, rag_min_relevance: .7, model_context_budgets: {'mlx:small': 2048}});
});
