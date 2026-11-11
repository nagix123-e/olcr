import type {ReactNode} from 'react';
import './ui-icons.css';

// Functional icons share one 24px grid. User-created agent avatars stay separate.
const paths = {
  help: <><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4.3 1.7c-.9.6-1.8 1.1-1.8 2.8"/><path d="M12 17h.01"/></>,
  pin: <><path d="m16 3 5 5-4 1-4 4-1 4-5-5 4-1 4-4Z"/><path d="m7 17-4 4"/></>,
  download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/></>,
  upload: <><path d="M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5"/></>,
  steer: <path d="M4 21V11a5 5 0 0 1 5-5h11m-5-5 5 5-5 5"/>,
  expand: <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M3 3l6 6m12-6-6 6M3 21l6-6m12 6-6-6"/>,
  collapse: <path d="M9 3v6H3m12-6v6h6M3 15h6v6m12-6h-6v6M3 3l6 6m12-6-6 6M3 21l6-6m12 6-6-6"/>,
  fit: <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M8 8h8v8H8Z"/>,
  undo: <path d="m8 4-5 5 5 5M3 9h11a6 6 0 0 1 0 12h-3"/>,
  redo: <path d="m16 4 5 5-5 5m5-5H10a6 6 0 0 0 0 12h3"/>,
  more: <><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></>,
  check: <path d="m5 12 4 4L19 6"/>,
  plus: <path d="M12 5v14M5 12h14"/>,
  close: <path d="m6 6 12 12M18 6 6 18"/>,
  back: <path d="m12 5-7 7 7 7M5 12h14"/>,
  chevron: <path d="m8 10 4 4 4-4"/>,
  settings: <><path d="m9 3-.6 2.4-2 .9-2.2-.7-2 3.4 1.7 1.7v2.6L2.2 15l2 3.4 2.2-.7 2 .9L9 21h4l.6-2.4 2-.9 2.2.7 2-3.4-1.7-1.7v-2.6l1.7-1.7-2-3.4-2.2.7-2-.9L13 3Z"/><circle cx="11" cy="12" r="3"/></>,
  agent: <><rect x="4" y="7" width="16" height="14" rx="4"/><path d="M12 3v4M1 12v5m22-5v5M8 16h8"/><circle cx="8" cy="12" r=".75"/><circle cx="16" cy="12" r=".75"/></>,
  skills: <><path d="M12 6c-3-2-6-2-10-1v15c4-1 7-1 10 1 3-2 6-2 10-1V5c-4-1-7-1-10 1Zm0 0v15"/><path d="M5 9h3m-3 4h3m8-4h3m-3 4h3"/></>,
  tools: <path d="m14.5 6.5 3 3 3-3a6 6 0 0 1-7.7 7.7l-7.1 7.1-3-3 7.1-7.1A6 6 0 0 1 17.5 3l-3 3.5Z"/>,
  paperclip: <path d="m8 13 7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l9-9a2 2 0 0 1 3 3l-9 9a1 1 0 0 0 2 2l8-8"/>,
  map: <><path d="M3 6 9 3l6 3 6-3v15l-6 3-8-3-4 2V6Zm6-3v15m6-12v15"/><path d="M18 20s-4-4.4-4-7a4 4 0 1 1 8 0c0 2.6-4 7-4 7Z"/><circle cx="18" cy="13" r="1"/></>,
  diagram: <><rect x="8" y="2" width="8" height="6" rx="1"/><rect x="2" y="16" width="7" height="6" rx="1"/><rect x="15" y="16" width="7" height="6" rx="1"/><path d="M12 8v4M5.5 16v-4h13v4"/></>,
  scrape: <><circle cx="10" cy="10" r="8"/><path d="M2 10h16M10 2c-4 4-4 12 0 16 2-2 3-4 3-8s-1-6-3-8m10 12v8m-3-3 3 3 3-3"/></>,
  guiTest: <><rect x="2" y="3" width="20" height="14" rx="2"/><path d="m8 10 3 3 5-5m-4 9v4m-4 0h8"/></>,
  trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></>,
  copy: <><rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 4V3H3v13h1"/></>,
  edit: <><path d="m15 4 5 5M4 15 17 2l5 5L9 20l-7 2Z"/></>,
  chat: <path d="M21 11a9 9 0 0 1-9 9H4l-3 3 2-8a9 9 0 1 1 18-4Z"/>,
  project: <><path d="M3 5h6l2 3h10v13H3Z"/><path d="M12 11v7m-3-3.5h6"/></>,
  connections: <g transform="translate(2.76 2.76) scale(.84)"><path d="m8 14 6-6M9 17l-2 2a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2-2 2-2a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/></g>,
  link: <><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></>,
  mail: <><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m2 6 10 7L22 6"/></>,
  calendar: <><rect x="3" y="5" width="18" height="17" rx="2"/><path d="M7 2v6m10-6v6M3 11h18m-13 4h3m3 0h3m-9 4h3"/></>,
  shopping: <><path d="M2 3h3l3 13h11l3-10H6M9 20h.01M18 20h.01"/><circle cx="9" cy="20" r="1"/><circle cx="18" cy="20" r="1"/></>,
  files: <><path d="M8 3h9l4 4v14H8Zm9 0v4h4M4 7H2v14h2m8-10h5m-5 4h5"/></>,
  folder: <path d="M3 6h7l2 3h9v11H3Z"/>,
  refresh: <><path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 5v4h4"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 19v-4h-4"/></>,
  knowledge: <><path d="M5 2h9l5 5v15H5Zm9 0v5h5M8 12h8m-8 4h8"/></>,
} satisfies Record<string, ReactNode>;
export type UiIconName = keyof typeof paths;
export function UiIcon({name, size=18, className=''}:{name:UiIconName;size?:number;className?:string}) {
  return <svg className={`ui-icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{paths[name]}</svg>;
}
