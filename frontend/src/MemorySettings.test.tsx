import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {MemorySettings} from './MemorySettings';

afterEach(cleanup);
it('saves only memory settings and explains that turning off sharing retains memories', async () => {
  const settings = {conversation_memory_enabled: true, conversation_memory_cross_project_enabled: true, conversation_memory_min_relevance: .55};
  const save = vi.fn(async (updates: Record<string, unknown>) => ({...settings, ...updates}));
  render(<MemorySettings settings={settings} save={save}/>);
  expect(screen.getByText(/保存済みの記憶は削除|stored memories are retained/i)).toBeTruthy();
  fireEvent.click(screen.getByRole('checkbox', {name: /プロジェクトをまたいだ|Across-project memory/i}));
  fireEvent.change(screen.getByRole('slider'), {target: {value: '.8'}});
  fireEvent.click(screen.getByRole('button', {name: /記憶設定を保存|Save memory settings/i}));
  await screen.findByText(/保存しました|Saved. Applies/);
  expect(save).toHaveBeenCalledWith({...settings, conversation_memory_cross_project_enabled: false, conversation_memory_min_relevance: .8});
});
it('shows a failed save and allows retry without claiming success', async () => {
  const save = vi.fn().mockRejectedValueOnce(new Error('Could not save')).mockResolvedValue({});
  render(<MemorySettings settings={{conversation_memory_enabled: true}} save={save}/>);
  fireEvent.click(screen.getByRole('button', {name: /記憶設定を保存|Save memory settings/i}));
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not save');
  expect(screen.queryByText(/保存しました|Saved. Applies/)).toBeNull();
  await waitFor(() => expect(screen.getByRole('button')).not.toBeDisabled());
  fireEvent.click(screen.getByRole('button'));
  await screen.findByText(/保存しました|Saved. Applies/);
});
