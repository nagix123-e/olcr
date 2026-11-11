import {useEffect,useRef,useState} from 'react';
import {useLanguage} from './language';
import {SettingsBack} from './SettingsBack';
import './browser-forms.css';
type Api=(path:string,init?:RequestInit)=>Promise<any>;
type Session={session_id:string;url:string};
type Field={label:string;selector:string;tag:string;type:string};
type Review={id:string;session:{url:string;origin:string};fields:(Field&{value:string})[];expires_at:number};

/** User-selected browser text drafts; every apply requires an exact-value review. */
export function BrowserForms({api,onBack}:{api:Api;onBack?:()=>void}){
 const {language}=useLanguage(),ja=language==='ja';const text=(en:string,jp:string)=>ja?jp:en;
 const [sessions,setSessions]=useState<Session[]>([]),[session,setSession]=useState(''),[fields,setFields]=useState<Field[]>([]),[values,setValues]=useState<Record<string,string>>({}),[review,setReview]=useState<Review|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[feedback,setFeedback]=useState('');
 const live=useRef(true),generation=useRef(0);
 const report=(e:unknown)=>{if(live.current)setError(e instanceof Error?e.message:String(e));};
 const load=async()=>{const result=await api('/browser-forms/sessions');if(live.current)setSessions(result.sessions||[]);};
 useEffect(()=>{live.current=true;void load().catch(report);return()=>{live.current=false;generation.current++;};},[api]);
 const inspect=async(sid:string)=>{
  const current=++generation.current;setSession(sid);setReview(null);setFields([]);setValues({});setFeedback('');setError('');if(!sid)return;
  setBusy(true);try{const result=await api(`/browser-forms/sessions/${encodeURIComponent(sid)}/fields`);if(live.current&&current===generation.current)setFields(result.fields||[]);}catch(e){if(current===generation.current)report(e);}finally{if(live.current&&current===generation.current)setBusy(false);}
 };
 const prepare=async()=>{
  if(busy)return;setBusy(true);setError('');setFeedback('');setReview(null);const current=generation.current;
  try{const selected=Object.fromEntries(Object.entries(values).filter(([,value])=>value.length));const result=await api(`/browser-forms/sessions/${encodeURIComponent(session)}/prepare`,{method:'POST',body:JSON.stringify({values:selected})});if(live.current&&current===generation.current)setReview(result);}catch(e){report(e);}finally{if(live.current)setBusy(false);}
 };
 const confirm=async(approve:boolean)=>{
  if(!review||busy)return;setBusy(true);setError('');const selected=review;setReview(null);
  try{const result=await api(`/browser-forms/reviews/${encodeURIComponent(selected.id)}/confirm`,{method:'POST',body:JSON.stringify({approve,review:selected})});if(live.current)setFeedback(result.status==='applied'?text('Values filled. Review and submit the form yourself in the browser.','値を入力しました。ブラウザーで内容を確認し、送信はご自身で行ってください。'):text('Draft cancelled. No values were applied.','下書きを取り消しました。値は入力していません。'));}catch(e){report(e);}finally{if(live.current)setBusy(false);}
 };
 return <section className="browser-forms" aria-label={text('Browser form drafts','ブラウザーのフォーム下書き')}>
  {onBack&&<SettingsBack onBack={onBack}/>}<h2>{text('Prepare a form draft','フォームの下書き')}</h2>
  <p>{text('Choose an existing OLCR browser window, enter public form values, and review them before filling. You complete any submission on the website.','開いているOLCRブラウザーを選び、一般的なフォームの値を入力してから確認します。送信はサイト上でご自身で行います。')}</p>
  <p className="setting-hint">{text('Passwords, login codes, card and bank fields are excluded. Do not enter credentials or payment details. Filling sends no keyboard, input or change events; some sites need you to edit the value yourself before they recognize it.','パスワード、認証コード、カード・銀行情報の欄は対象外です。認証情報や決済情報を入力しないでください。キーボード・入力・変更イベントを送らないため、サイトによってはご自身で値を編集する必要があります。')}</p>
  {error&&<p role="alert">{error}</p>}{feedback&&<p role="status">{feedback}</p>}
  <label className="browser-form-session">{text('Browser session','ブラウザーセッション')}<select disabled={busy} value={session} onChange={e=>void inspect(e.target.value)}><option value="">{text('Choose a browser window','ブラウザーを選択')}</option>{sessions.map(item=><option key={item.session_id} value={item.session_id}>{item.url}</option>)}</select></label>
  {!sessions.length&&<p>{text('Open a public HTTPS page in OLCR’s browser first, then refresh.','先にOLCRブラウザーで公開HTTPSページを開いてから、一覧を更新してください。')}</p>}
  <div className="browser-form-actions"><button type="button" disabled={busy} onClick={()=>{generation.current++;setSession('');setFields([]);setValues({});setReview(null);void load().catch(report);}}>{text('Refresh windows','ウィンドウ一覧を更新')}</button>{session&&<button type="button" disabled={busy} onClick={()=>void inspect(session)}>{text('Refresh fields','入力欄を更新')}</button>}</div>
  {session&&!busy&&!fields.length&&<p>{text('No unique, visible, supported text fields are available on this page.','このページには対応する、重複のない表示中のテキスト入力欄がありません。')}</p>}
  {fields.length>0&&<form onSubmit={e=>{e.preventDefault();void prepare();}}><div className="browser-form-fields">{fields.map(field=><label key={field.selector}>{field.label}<textarea rows={field.tag==='textarea'?3:1} disabled={busy} maxLength={2000} value={values[field.label]||''} onChange={e=>{setValues({...values,[field.label]:e.target.value});setReview(null);setFeedback('');}}/></label>)}</div><p className="setting-hint">{text('Leave a field empty to keep it unchanged. Reviews expire after five minutes.','変更しない欄は空のままにしてください。確認内容の有効期限は5分です。')}</p><button disabled={busy||!Object.values(values).some(Boolean)}>{text('Review values','値を確認')}</button></form>}
  {review&&<section className="browser-form-review" aria-label={text('Exact form review','フォームの最終確認')}><h3>{text('Review these exact values','入力する値を確認してください')}</h3><p className="browser-form-origin">{review.session.url}</p><dl>{review.fields.map(field=><div key={field.selector}><dt>{field.label}</dt><dd>{field.value}</dd></div>)}</dl><p>{text('This only fills the reviewed fields. It does not click or submit the form.','確認した欄への入力のみを行います。クリックやフォームの送信は行いません。')}</p><div className="browser-form-actions"><button type="button" disabled={busy||Date.now()/1000>=review.expires_at} onClick={()=>void confirm(true)}>{text('Fill these values only','この値を入力する')}</button><button type="button" disabled={busy} onClick={()=>void confirm(false)}>{text('Cancel draft','下書きを取り消す')}</button></div></section>}
 </section>;
}
