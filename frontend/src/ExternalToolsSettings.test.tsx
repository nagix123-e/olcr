import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {ExternalToolsSettings} from './ExternalToolsSettings';
vi.mock('./language', () => ({useLanguage: () => ({language: 'en'})}));
vi.mock('./native', () => ({native: () => null}));
afterEach(cleanup);
const tools = [{tool_id: 'search', provider: 'Search provider', credential: 'Configured', availability: 'READY', external_authorized: false, endpoint_mode: 'remote'}];
it('keeps tool details collapsed while access remains available independently', async () => {
  const api = vi.fn().mockResolvedValue({external_access_enabled: false, tools});
  const save = vi.fn().mockResolvedValue({});
  render(<ExternalToolsSettings api={api} settings={{}} save={save}/>);
  const summary = await screen.findByText('API and tool status');
  const details = summary.closest('details')!;
  expect(details).not.toHaveAttribute('open');
  fireEvent.click(summary);
  expect(details).toHaveAttribute('open');
  expect(screen.getByText('Search provider')).toBeVisible();
  expect(screen.getByText('Ready')).toBeVisible();
  fireEvent.click(summary);
  expect(details).not.toHaveAttribute('open');
  fireEvent.click(screen.getByRole('switch', {name: 'External access'}));
  await waitFor(() => expect(save).toHaveBeenCalledWith({external_access_enabled: true}));
  expect(details).not.toHaveAttribute('open');
});
it('opens the provider credential setup from Configure in tool status', async () => {
  const credential_setup = {
    type: 'credential_setup', tool_id: 'research.openalex', provider: 'OpenAlex',
    setup_url: 'https://example.invalid/register', link_label: 'OpenAlex API key',
    fields: [{id: 'api_key', label: 'OpenAlex API key', input_type: 'password', keychain_handle: 'OLCR_OPENALEX_API_KEY'}],
  };
  const api = vi.fn().mockResolvedValue({external_access_enabled: false, tools: [{...tools[0], tool_id: 'research.openalex', provider: 'OpenAlex', availability: 'CONFIG_REQUIRED', credential: 'required', credential_setup}]});
  render(<ExternalToolsSettings api={api} settings={{}} save={vi.fn()}/>);
  fireEvent.click(await screen.findByText('API and tool status'));
  fireEvent.click(await screen.findByRole('button', {name: 'Configure'}));
  expect(screen.getByRole('dialog', {name: 'Configure OpenAlex'})).toBeVisible();
  expect(screen.getByRole('link', {name: 'OpenAlex API key'})).toHaveAttribute('href', credential_setup.setup_url);
  expect(screen.getByLabelText('OpenAlex API key')).toHaveAttribute('type', 'password');
  fireEvent.keyDown(screen.getByRole('dialog', {name: 'Configure OpenAlex'}), {key: 'Escape'});
  expect(screen.queryByRole('dialog', {name: 'Configure OpenAlex'})).not.toBeInTheDocument();
});
it('saves only the requested capability and prevents repeat changes during a save', async () => {
  const api = vi.fn().mockResolvedValue({external_access_enabled: true, tools: []});
  let finish!: (value: unknown) => void;
  const save = vi.fn(() => new Promise(resolve => {finish = resolve;}));
  render(<ExternalToolsSettings api={api} settings={{task_manager_enabled: false}} save={save}/>);
  await screen.findByText('API and tool status');
  fireEvent.click(screen.getByRole('switch', {name: 'Coding Orchestrator'}));
  expect(save).toHaveBeenCalledWith({task_manager_enabled: true});
  expect(screen.getByRole('switch', {name: 'Computer Use'})).toBeDisabled();
  finish({});
  await waitFor(() => expect(screen.getByRole('switch', {name: 'Computer Use'})).toBeEnabled());
});
it('shows save failures once and retains the previous switch value', async () => {
  const api = vi.fn().mockResolvedValue({external_access_enabled: true, tools: []});
  const save = vi.fn().mockRejectedValue(new Error('Unable to save'));
  render(<ExternalToolsSettings api={api} settings={{web_scraping_enabled: false}} save={save}/>);
  await screen.findByText('API and tool status');
  fireEvent.click(screen.getByRole('switch', {name: 'Web scraping'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('Unable to save');
  expect(screen.getByRole('switch', {name: 'Web scraping'})).not.toBeChecked();
});
it('uses shared global switches for YaRN and compression without changing context lengths', async () => {
  const api = vi.fn().mockResolvedValue({external_access_enabled: false, tools: []});
  const save = vi.fn().mockResolvedValue({});
  const settings = {yarn_enabled: false, context_auto_compress: true, yarn_target_tokens: 524288, local_context_tokens: 32768};
  const view = render(<ExternalToolsSettings api={api} settings={settings} save={save}/>);
  await screen.findByText('API and tool status');
  const yarn = screen.getByRole('switch', {name: 'YaRN'});
  expect(yarn.closest('.setting-row')).toHaveClass('capability-setting');
  fireEvent.click(yarn);
  await waitFor(() => expect(save).toHaveBeenCalledWith({yarn_enabled: true}));
  await waitFor(() => expect(yarn).toBeEnabled());
  const compression = screen.getByRole('switch', {name: 'Automatic context compression'});
  expect(compression).toBeChecked();
  fireEvent.click(compression);
  await waitFor(() => expect(save).toHaveBeenCalledWith({context_auto_compress: false}));
  view.rerender(<ExternalToolsSettings api={api} settings={{...settings, yarn_enabled: true, context_auto_compress: false}} save={save}/>);
  expect(screen.getByRole('switch', {name: 'YaRN'})).toBeChecked();
  expect(screen.getByRole('switch', {name: 'Automatic context compression'})).not.toBeChecked();
});
