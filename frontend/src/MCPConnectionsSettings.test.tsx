import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {MCPConnectionsSettings} from './MCPConnectionsSettings';
vi.mock('./language', () => ({useLanguage: () => ({language: 'en'})}));
afterEach(cleanup);
const server = {server: 'shadcn', display_name: 'shadcn MCP', scope: 'coding', can_check: true,
  connection_status: 'NOT_CHECKED', reason: 'NOT_CHECKED', checked_at: null, tool_count: null};

it('checks unchecked servers on opening and distinguishes verification from tool use', async () => {
  let finish!: (value: unknown) => void;
  const api = vi.fn().mockResolvedValueOnce({servers: [server]})
    .mockImplementationOnce(() => new Promise(resolve => {finish = resolve;}));
  render(<MCPConnectionsSettings api={api}/>);
  expect(await screen.findByText('Checking…')).toBeVisible();
  expect(screen.getByRole('region', {name:'MCP library'})).toBeVisible();
  expect(screen.getByRole('heading', {name:'shadcn MCP'})).toBeVisible();
  expect(api).toHaveBeenCalledTimes(2);
  const button = screen.getByRole('button', {name: 'shadcn MCP check connection'});
  expect(button).toBeDisabled();
  expect(screen.getByText('Checking…')).toBeVisible();
  expect(api).toHaveBeenLastCalledWith('/mcp/connections/shadcn/check', {method: 'POST'});
  finish({...server, connection_status:'CONNECTED', reason:'READY', checked_at:'2026-10-25T01:00:00Z', tool_count:7});
  expect(await screen.findByText('Connection verified')).toBeVisible();
  expect(screen.getByText(/7 tools/)).toBeVisible();
  expect(screen.getByText(/does not invoke tools or authorize automatic use/)).toBeVisible();
});

it('explains disabled servers and keeps managed-browser scope explicit', async () => {
  const api = vi.fn().mockResolvedValue({servers:[
    {...server, server:'serena', display_name:'Serena', can_check:false, connection_status:'DISABLED', reason:'POLICY_DISABLED'},
    {...server, server:'playwright', display_name:'Playwright MCP', scope:'managed_browser'},
  ]});
  render(<MCPConnectionsSettings api={api}/>);
  expect(await screen.findByText('Disabled')).toBeVisible();
  expect(screen.getByText('Disabled by the current packaged policy.')).toBeVisible();
  expect(screen.queryByRole('button', {name:'Serena check connection'})).not.toBeInTheDocument();
  expect(screen.getByText(/Direct exposure to coding tasks is disabled/)).toBeVisible();
});

it('shows failure cause and allows another check', async () => {
  const api = vi.fn().mockResolvedValueOnce({servers:[server]})
    .mockResolvedValueOnce({...server, connection_status:'FAILED', reason:'REQUEST_TIMEOUT'});
  render(<MCPConnectionsSettings api={api}/>);
  const button = await screen.findByRole('button', {name:'shadcn MCP check connection'});
  expect(await screen.findByText('Connection failed')).toBeVisible();
  expect(screen.getByText(/did not respond in time/)).toBeVisible();
  await waitFor(() => expect(button).toBeEnabled());
});

it('reports API errors without turning unchecked into connection success', async () => {
  const api = vi.fn().mockResolvedValueOnce({servers:[server]}).mockRejectedValueOnce(new Error('Backend unavailable'));
  render(<MCPConnectionsSettings api={api}/>);
  await screen.findByRole('button', {name:'shadcn MCP check connection'});
  expect(await screen.findByRole('alert')).toHaveTextContent('Backend unavailable');
  expect(screen.queryByText('Connection verified')).not.toBeInTheDocument();
});

it('checks each eligible server once, preserves verified entries, and continues after a failure', async () => {
  const other = {...server, server:'animejs', display_name:'Anime.js MCP'};
  const api = vi.fn(async (path: string) => {
    if (path === '/mcp/connections') return {servers:[server,other,{...server,server:'playwright',display_name:'Playwright MCP',connection_status:'CONNECTED',reason:'READY'}]};
    if (path.includes('shadcn')) throw new Error('Server unavailable');
    return {...other,connection_status:'CONNECTED',reason:'READY',tool_count:4};
  });
  render(<MCPConnectionsSettings api={api}/>);
  await waitFor(() => expect(screen.getAllByText('Connection verified')).toHaveLength(2));
  expect(api.mock.calls.map(call => call[0])).toEqual(['/mcp/connections','/mcp/connections/shadcn/check','/mcp/connections/animejs/check']);
  fireEvent.click(screen.getByRole('button',{name:'shadcn MCP check connection'}));
  await waitFor(() => expect(api).toHaveBeenCalledTimes(4));
});

it('does not start queued checks after leaving Settings', async () => {
  let finish!: (value: unknown) => void;
  const api = vi.fn().mockResolvedValueOnce({servers:[server,{...server,server:'animejs'}]})
    .mockImplementationOnce(() => new Promise(resolve => {finish=resolve;}));
  const view=render(<MCPConnectionsSettings api={api}/>);
  await screen.findByText('Checking…');
  view.unmount();
  finish({...server,connection_status:'CONNECTED',reason:'READY'});
  await new Promise(resolve => setTimeout(resolve,0));
  expect(api).toHaveBeenCalledTimes(2);
});
