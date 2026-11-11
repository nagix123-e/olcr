import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {ContextExtensionSettings} from './ContextExtensionSettings';
afterEach(cleanup);
it('edits lengths independently and never overwrites the global toggles', async () => {
  let settings = {yarn_enabled: false, yarn_target_tokens: 524288, context_auto_compress: false, local_context_tokens: 32768};
  const api = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/settings') return settings;
    expect(path).toBe('/settings/retrieval');
    expect(init?.method).toBe('PATCH');
    const patch = JSON.parse(String(init?.body));
    expect(patch).toEqual({yarn_target_tokens: 1048576, local_context_tokens: 65536});
    settings = {...settings, ...patch};
    return settings;
  });
  const view = render(<ContextExtensionSettings api={api}/>);
  const target = await screen.findByRole('spinbutton', {name: /延長先|YaRN target/});
  await waitFor(() => expect(target).toBeEnabled());
  expect(screen.queryByRole('checkbox')).toBeNull();
  expect(screen.queryByRole('switch')).toBeNull();
  expect(screen.getByText(/オン／オフ|Turn YaRN/)).toBeInTheDocument();
  fireEvent.change(target, {target: {value: '1048576'}});
  fireEvent.change(screen.getByRole('spinbutton', {name: /通常|Normal/}), {target: {value: '65536'}});
  // A global toggle can change after this numeric form was loaded.
  settings = {...settings, yarn_enabled: true, context_auto_compress: true};
  fireEvent.click(screen.getByRole('button', {name: /設定を保存|Save context/}));
  await screen.findByRole('status');
  expect(settings).toEqual({yarn_enabled: true, yarn_target_tokens: 1048576, context_auto_compress: true, local_context_tokens: 65536});
  view.unmount();
  render(<ContextExtensionSettings api={api}/>);
  await waitFor(() => expect(screen.getByRole('spinbutton', {name: /延長先|YaRN target/})).toHaveValue(1048576));
  expect(screen.getByRole('spinbutton', {name: /通常|Normal/})).toHaveValue(65536);
});
