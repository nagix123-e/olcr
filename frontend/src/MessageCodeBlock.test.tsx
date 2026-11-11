import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import ReactMarkdown from 'react-markdown';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {MessageCodeBlock} from './MessageCodeBlock';

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
const originalExecCommand = Object.getOwnPropertyDescriptor(document, 'execCommand');
const markdown = (text: string) => <div className="message-markdown"><ReactMarkdown components={{pre: MessageCodeBlock}}>{text}</ReactMarkdown></div>;
const clipboard = (value: unknown) => Object.defineProperty(navigator, 'clipboard', {configurable: true, value});
const legacyCopy = (value: unknown) => Object.defineProperty(document, 'execCommand', {configurable: true, value});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard);
  else Reflect.deleteProperty(navigator, 'clipboard');
  if (originalExecCommand) Object.defineProperty(document, 'execCommand', originalExecCommand);
  else Reflect.deleteProperty(document, 'execCommand');
});

describe('MessageCodeBlock', () => {
  it('copies only the selected command, without explanation, fences or another block', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    clipboard({writeText});
    const command = "python3 -m http.server 8000 --bind 127.0.0.1 --directory '/Users/example/昼食 プロジェクト'";
    render(markdown(`起動方法\n\n\`\`\`sh\n${command}\n\`\`\`\n\nテスト方法\n\n\`\`\`\nnode --test\n\`\`\``));
    const buttons = screen.getAllByRole('button', {name: 'コードをコピー'});
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(command));
    expect(buttons[0]).toHaveTextContent('コピーしました');
    expect(buttons[1]).toHaveTextContent('コピー');
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith('node --test'));
    expect(writeText).toHaveBeenCalledTimes(2);
  });

  it('preserves indentation, internal newlines, trailing spaces and literal characters', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    clipboard({writeText});
    const code = '\tconst dish = "🍱";  \n  console.log(dish);\n\n// 日本語のコメント';
    render(markdown(`\`\`\`javascript\n${code}\n\`\`\``));
    fireEvent.click(screen.getByRole('button', {name: 'コードをコピー'}));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(code));
  });

  it('copies the latest rendered content after a streaming response changes', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    clipboard({writeText});
    const view = render(markdown('```sh\nnode\n```'));
    view.rerender(markdown('```sh\nnode --test\n```'));
    fireEvent.click(screen.getByRole('button', {name: 'コードをコピー'}));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('node --test'));
  });

  it('falls back in a WebView without Clipboard API and restores keyboard focus', async () => {
    clipboard(undefined);
    let copied = '';
    const exec = vi.fn(() => {
      copied = document.querySelector<HTMLTextAreaElement>('textarea[aria-hidden="true"]')?.value ?? '';
      return true;
    });
    legacyCopy(exec);
    render(markdown('```\npython3 -m http.server 8000\n```'));
    const button = screen.getByRole('button', {name: 'コードをコピー'});
    button.focus();
    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveTextContent('コピーしました'));
    expect(exec).toHaveBeenCalledWith('copy');
    expect(copied).toBe('python3 -m http.server 8000');
    expect(document.querySelector('textarea[aria-hidden="true"]')).toBeNull();
    expect(button).toHaveFocus();
  });

  it('falls back if Clipboard API rejects the write', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('NotAllowedError'));
    clipboard({writeText});
    const exec = vi.fn().mockReturnValue(true);
    legacyCopy(exec);
    render(markdown('```\nnode --test\n```'));
    fireEvent.click(screen.getByRole('button', {name: 'コードをコピー'}));
    await waitFor(() => expect(screen.getByRole('button')).toHaveTextContent('コピーしました'));
    expect(exec).toHaveBeenCalledWith('copy');
  });

  it('reports a failed copy instead of showing success, and permits retry', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('NotAllowedError'));
    clipboard({writeText});
    legacyCopy(vi.fn().mockReturnValue(false));
    render(markdown('```\nnode --test\n```'));
    const button = screen.getByRole('button', {name: 'コードをコピー'});
    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('コピーできませんでした');
    expect(button).not.toHaveTextContent('コピーしました');
    writeText.mockResolvedValue(undefined);
    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveTextContent('コピーしました'));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('leaves inline code as inline text', () => {
    render(markdown('テストには `node --test` を使います。'));
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByText('node --test').tagName).toBe('CODE');
  });
});
