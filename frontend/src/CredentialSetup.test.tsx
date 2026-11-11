import { cleanup, createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock('./native', async importOriginal => {
  const actual = await importOriginal<typeof import('./native')>();
  return {
    ...actual,
    native: () => ({ core: { invoke: invokeMock } }),
    openExternal: (url: string) => Promise.resolve(invokeMock('open_external_url', { url })),
  };
});
import { CredentialSetupCard, type CredentialSetupBlock } from './CredentialSetup';

const block: CredentialSetupBlock = {
  type: 'credential_setup',
  tool_id: 'conflict.acled',
  provider: 'ACLED',
  setup_url: 'https://acleddata.com/user/register',
  link_label: 'myACLED アカウント登録・APIアクセス',
  guidance: 'myACLEDに登録しただけではAPI利用権限が付与されない場合があり、ACLED側の承認・権限付与が別途必要なことがあります。',
  fields: [
    { id: 'username', label: 'myACLED メールアドレス', input_type: 'email', keychain_handle: 'OLCR_ACLED_USERNAME' },
    { id: 'password', label: 'myACLED パスワード', input_type: 'password', keychain_handle: 'OLCR_ACLED_PASSWORD' },
  ],
};

afterEach(() => { cleanup(); invokeMock.mockReset(); });

describe('CredentialSetupCard', () => {
  it('saves a dedicated secret field to Keychain and local runtime without enabling or calling the provider', async () => {
    const api = vi.fn().mockResolvedValue({ external_access_enabled: false });
    const { container } = render(<CredentialSetupCard block={block} api={api} />);
    expect(screen.getByRole('link', { name: 'myACLED アカウント登録・APIアクセス' })).toHaveAttribute('href', block.setup_url);
    expect(screen.getByText(block.guidance!)).toBeInTheDocument();
    expect(screen.getByText(block.guidance!)).toHaveTextContent('登録しただけではAPI利用権限が付与されない場合');
    expect(screen.getByLabelText('myACLED メールアドレス')).toHaveAttribute('type', 'email');
    expect(screen.getByLabelText('myACLED パスワード')).toHaveAttribute('type', 'password');
    const secret = 'offline-test-password-not-a-real-credential';
    fireEvent.change(screen.getByLabelText('myACLED メールアドレス'), { target: { value: 'user@example.invalid' } });
    fireEvent.change(screen.getByLabelText('myACLED パスワード'), { target: { value: secret } });
    fireEvent.click(screen.getByRole('button', { name: '認証情報をキーチェーンに保存' }));

    await waitFor(() => expect(api).toHaveBeenCalledOnce());
    expect(invokeMock).toHaveBeenCalledWith('set_external_credential', {
      handle: 'OLCR_ACLED_USERNAME', value: 'user@example.invalid',
    });
    expect(invokeMock).toHaveBeenCalledWith('set_external_credential', { handle: 'OLCR_ACLED_PASSWORD', value: secret });
    expect(api).toHaveBeenCalledWith('/external-tools/credentials', {
      method: 'PUT', body: JSON.stringify({ tool_id: 'conflict.acled', credentials: { username: 'user@example.invalid', password: secret } }),
    });
    expect(container.textContent).not.toContain(secret);
    expect(screen.getByRole('status')).toHaveTextContent('外部アクセスはOFFのまま');
    expect(api.mock.calls.some(([path]) => String(path).includes('/chat'))).toBe(false);
    expect(api.mock.calls.some(([path]) => String(path).includes('search'))).toBe(false);
  });

  it('does not make non-HTTPS setup links clickable', () => {
    const unsafe = { ...block, setup_url: 'javascript:alert(1)' };
    render(<CredentialSetupCard block={unsafe} api={vi.fn()} />);
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('opens an HTTPS setup link through the desktop external-URL bridge', async () => {
    render(<CredentialSetupCard block={block} api={vi.fn()} />);
    const link = screen.getByRole('link', { name: 'myACLED アカウント登録・APIアクセス' });
    const click = createEvent.click(link);

    fireEvent(link, click);

    expect(click.defaultPrevented).toBe(true);
    await waitFor(() => expect(invokeMock).toHaveBeenCalledWith('open_external_url', {
      url: block.setup_url,
    }));
  });

  it('saves a Brave Search key through the web Keychain path without changing web settings', async () => {
    const webBlock: CredentialSetupBlock = {
      type: 'credential_setup', tool_id: 'web.search.brave', provider: 'Brave Search',
      credential_kind: 'web_search', web_provider: 'brave',
      setup_url: 'https://api-dashboard.search.brave.com/',
      fields: [{ id: 'api_key', label: 'Brave Search API key', input_type: 'password' }],
    };
    const api = vi.fn().mockResolvedValue({ web_mode: 'off' });
    render(<CredentialSetupCard block={webBlock} api={api} />);
    const secret = 'fake-brave-key-for-ui-test';
    fireEvent.change(screen.getByLabelText('Brave Search API key'), { target: { value: secret } });
    fireEvent.click(screen.getByRole('button', { name: '認証情報をキーチェーンに保存' }));
    await waitFor(() => expect(api).toHaveBeenCalledOnce());
    expect(invokeMock).toHaveBeenCalledWith('set_web_credential', { provider: 'brave', key: secret });
    expect(api).toHaveBeenCalledWith('/web/credentials', {
      method: 'PUT', body: JSON.stringify({ provider: 'brave', key: secret }),
    });
    expect(screen.getByRole('status')).toHaveTextContent('Web Search設定や外部アクセス設定は変更していません');
    expect(document.body.textContent).not.toContain(secret);
  });
});
