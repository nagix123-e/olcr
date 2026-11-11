import {useEffect, useId, useLayoutEffect, useRef, useState} from 'react';
import {createPortal} from 'react-dom';
import {UiIcon} from './UiIcon';
import {useLanguage} from './language';
import './setting-help.css';

/** Secondary help stays out of labels and never changes the associated setting. */
export function SettingHelp({label, children}: {label: string; children: string}) {
  const ja = useLanguage().language === 'ja', id = useId();
  const trigger = useRef<HTMLButtonElement>(null), tip = useRef<HTMLDivElement>(null);
  const enterTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [hover, setHover] = useState(false), [focus, setFocus] = useState(false);
  const [pinned, setPinned] = useState(false), [dismissed, setDismissed] = useState(false);
  const [position, setPosition] = useState({left: 0, top: 0, ready: false});
  const open = !dismissed && (hover || focus || pinned);
  const clearTimers = () => {clearTimeout(enterTimer.current); clearTimeout(leaveTimer.current);};
  const dismiss = () => {clearTimers(); setDismissed(true); setPinned(false); setHover(false);};
  const enter = (immediate = false) => {
    clearTimers(); setDismissed(false);
    if (immediate) setHover(true); else enterTimer.current = setTimeout(() => setHover(true), 250);
  };
  const leave = () => {clearTimers(); leaveTimer.current = setTimeout(() => setHover(false), 140);};
  useEffect(() => () => clearTimers(), []);
  useEffect(() => {
    const another = (event: Event) => {if ((event as CustomEvent<string>).detail !== id) dismiss();};
    window.addEventListener('olcr-setting-help', another);
    return () => window.removeEventListener('olcr-setting-help', another);
  }, [id]);
  useEffect(() => {
    if (!open) return;
    window.dispatchEvent(new CustomEvent('olcr-setting-help', {detail: id}));
    const outside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !tip.current?.contains(event.target as Node)) dismiss();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); dismiss();}
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape, true);
    window.addEventListener('blur', dismiss);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape, true);
      window.removeEventListener('blur', dismiss);
    };
  }, [open, id]);
  useLayoutEffect(() => {
    if (!open) {setPosition(previous => ({...previous, ready: false})); return;}
    const place = () => {
      if (!trigger.current || !tip.current) return;
      const anchor = trigger.current.getBoundingClientRect(), box = tip.current.getBoundingClientRect();
      if (anchor.bottom < 0 || anchor.top > window.innerHeight) {dismiss(); return;}
      const margin = 12, gap = 8;
      const left = Math.max(margin, Math.min(anchor.left + anchor.width / 2 - box.width / 2, window.innerWidth - box.width - margin));
      const preferred = anchor.top - box.height - gap >= margin ? anchor.top - box.height - gap : anchor.bottom + gap;
      const top = Math.max(margin, Math.min(preferred, window.innerHeight - box.height - margin));
      setPosition({left, top, ready: true});
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place);
    if (tip.current) observer?.observe(tip.current);
    if (trigger.current) observer?.observe(trigger.current);
    return () => {window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); observer?.disconnect();};
  }, [open, children]);
  return <span className="setting-help"><button ref={trigger} type="button" className="setting-help-trigger"
    aria-label={ja ? `${label}の説明` : `Help for ${label}`} aria-describedby={open ? id : undefined}
    onPointerEnter={event => {if (event.pointerType !== 'touch') enter();}} onPointerLeave={leave}
    onFocus={() => {setFocus(true); setDismissed(false);}}
    onBlur={() => {setFocus(false); setPinned(false);}}
    onClick={event => {event.preventDefault(); event.stopPropagation(); setPinned(!pinned); setDismissed(pinned);}}
  ><UiIcon name="help" size={14}/></button>{open && createPortal(<div ref={tip} id={id} role="tooltip" className="setting-help-tooltip"
    style={{left: position.left, top: position.top, visibility: position.ready ? 'visible' : 'hidden'}}
    onPointerEnter={() => enter(true)} onPointerLeave={leave}>{children}</div>, document.body)}</span>;
}
