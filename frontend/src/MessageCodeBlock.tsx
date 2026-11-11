import {useEffect, useRef, useState, type ReactNode} from 'react';
import {useLanguage} from './language';
import {UiIcon} from './UiIcon';
import './message-code.css';

async function copyText(text: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // Some desktop WebViews expose Clipboard but deny writes. Try the
    // selection-based API before asking the reader to copy manually.
  }
  const active = document.activeElement;
  const selection = document.getSelection();
  const ranges = selection ? Array.from({length: selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
  const field = document.createElement('textarea');
  field.value = text;
  field.readOnly = true;
  field.tabIndex = -1;
  field.setAttribute('aria-hidden', 'true');
  Object.assign(field.style, {position: 'fixed', left: '-9999px', opacity: '0'});
  document.body.appendChild(field);
  try {
    field.select();
    field.setSelectionRange(0, text.length);
    if (!document.execCommand?.('copy')) throw new Error('Clipboard unavailable');
  } finally {
    field.remove();
    if (active instanceof HTMLElement && active.isConnected) active.focus({preventScroll: true});
    selection?.removeAllRanges();
    ranges.filter(range => range.commonAncestorContainer.isConnected).forEach(range => selection?.addRange(range));
  }
}

export function MessageCodeBlock({children}: {children?: ReactNode}) {
  const {language} = useLanguage();
  const ja = language === 'ja';
  const code = useRef<HTMLPreElement>(null);
  const [state, setState] = useState<'idle' | 'copying' | 'copied' | 'error'>('idle');
  useEffect(() => {
    if (state !== 'copied') return;
    const timer = window.setTimeout(() => setState('idle'), 2000);
    return () => window.clearTimeout(timer);
  }, [state]);
  const copy = async () => {
    // Read only the code, excluding the toolbar and surrounding answer.
    // react-markdown adds one final newline when rendering a code fence.
    const text = code.current?.textContent?.replace(/\n$/, '') ?? '';
    setState('copying');
    try {
      await copyText(text);
      setState('copied');
    } catch {
      setState('error');
    }
  };
  const label = ja ? 'コードをコピー' : 'Copy code';
  return <div className="message-code-block">
    <div className="message-code-toolbar">
      <button type="button" className="copy-code" aria-label={label} disabled={state === 'copying'} onClick={() => void copy()}>
        <UiIcon name="copy" size={15}/>
        <span role="status">{state === 'copied' ? (ja ? 'コピーしました' : 'Copied') : state === 'copying' ? (ja ? 'コピー中…' : 'Copying…') : (ja ? 'コピー' : 'Copy')}</span>
      </button>
    </div>
    <pre ref={code} className="message-code" tabIndex={0} aria-label={ja ? 'コード' : 'Code'}>{children}</pre>
    {state === 'error' && <p className="copy-code-error" role="alert">{ja ? 'コピーできませんでした。コードを選択してコピーしてください。' : 'Could not copy. Select the code and copy it manually.'}</p>}
  </div>;
}
