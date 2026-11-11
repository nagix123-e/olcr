import {UiIcon, type UiIconName} from './UiIcon';
import {useEffect, useId, useRef, useState} from 'react';
import {createPortal} from 'react-dom';
import {useLanguage} from './language';

export function ComposerActions({disabled, onFile, onFolder, onDiagram, onMap, onPrompt,onAgentCreate}: {disabled: boolean; onFile: () => void; onFolder?: () => void; onDiagram: () => void; onMap?:()=>void; onPrompt?: (kind: 'scrape' | 'gui-e2e') => void;onAgentCreate?:()=>void}) {
  const {language} = useLanguage(), ja = language === 'ja';
  const [open, setOpen] = useState(false), [position, setPosition] = useState({left: 0, bottom: 0, height: 260});
  const trigger = useRef<HTMLButtonElement>(null), menu = useRef<HTMLDivElement>(null), id = useId();
  const [attachmentChoices, setAttachmentChoices] = useState(false);
  const actions = [
    {id: 'attachment', icon: 'paperclip' as UiIconName, label: ja ? 'ファイル・フォルダを添付' : 'Attach files or a folder', run: () => onFolder ? setAttachmentChoices(true) : onFile()},
    ...(onMap?[{id:'map',icon:'map' as UiIconName,label:ja?'地図・地点を指定':'Map / select places',run:onMap}]:[]),
    {id: 'diagram', icon: 'diagram' as UiIconName, label: ja ? '概念図' : 'Concept diagram', run: onDiagram},
    ...(onAgentCreate?[{id:'agent-create',icon:'agent' as UiIconName,label:ja?'カスタムエージェント作成':'Create custom agent',run:onAgentCreate}]:[]),
    ...(onPrompt ? [
      {id: 'scrape', icon: 'scrape' as UiIconName, label: ja ? 'スクレイピング' : 'Scraping', run: () => onPrompt('scrape')},
      {id: 'gui-e2e', icon: 'guiTest' as UiIconName, label: ja ? '実GUI E2E' : 'Real GUI E2E', run: () => onPrompt('gui-e2e')},
    ] : []),
  ];
  const close = (focus = true) => {setOpen(false);setAttachmentChoices(false); if (focus) trigger.current?.focus();};
  useEffect(() => {
    if (!open) return;
    const place = () => {const rect = trigger.current?.getBoundingClientRect(); if(rect) setPosition({left: Math.max(8, Math.min(rect.left, window.innerWidth - 248)), bottom: window.innerHeight - rect.top + 8, height: Math.max(64, Math.min(260, rect.top - 16))});};
    place(); menu.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const outside = (event: PointerEvent) => {if(!menu.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) close(false);};
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', place); window.addEventListener('scroll', place, true);
    return () => {document.removeEventListener('pointerdown', outside); window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true);};
  }, [open, attachmentChoices]);
  useEffect(() => {if(disabled)close(false);}, [disabled]);
  return <><button ref={trigger} type="button" className="attach-button" aria-label={ja ? '操作を追加' : 'Add action'} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined} title={ja ? '操作を追加' : 'Add action'} disabled={disabled} onClick={() => setOpen(value => !value)}><UiIcon name="plus"/></button>
    {open && createPortal(<div ref={menu} id={id} className="composer-action-menu" role="menu" aria-label={ja ? '追加する操作' : 'Add an action'} style={{left: position.left, bottom: position.bottom, maxHeight: position.height}} onKeyDown={event => {
      const buttons = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('[role=menuitem]') || []), current = buttons.indexOf(document.activeElement as HTMLButtonElement);
      if(event.key === 'Escape'){event.preventDefault(); close();}
      else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault(); buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(current+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus();}
      else if(event.key === 'Tab')close(false);
    }}>{attachmentChoices ? <>
      <button type="button" role="menuitem" aria-label={ja ? '添付メニューに戻る' : 'Back to attachments'} onClick={() => setAttachmentChoices(false)}><UiIcon name="back"/>{ja ? '戻る' : 'Back'}</button>
      <button type="button" role="menuitem" onClick={() => {close(); setAttachmentChoices(false); onFile()}}><UiIcon name="paperclip"/>{ja ? 'ファイルを選ぶ' : 'Choose files'}</button>
      <button type="button" role="menuitem" onClick={() => {close(); setAttachmentChoices(false); onFolder?.()}}><UiIcon name="folder"/>{ja ? 'フォルダを選ぶ' : 'Choose a folder'}</button>
    </> : actions.map(action => <button type="button" role="menuitem" key={action.id} onClick={() => {if(action.id==='attachment'&&onFolder){action.run();return;}close(); action.run();}}><UiIcon name={action.icon}/>{action.label}</button>)}</div>, document.body)}</>;
}
