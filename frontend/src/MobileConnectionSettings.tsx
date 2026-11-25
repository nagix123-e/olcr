import {useEffect, useState} from 'react';
import {useLanguage} from './language';
import {SettingsBack} from './SettingsBack';
import './mobile-connection.css';

type Api = (path: string, init?: RequestInit) => Promise<any>;
type Pair = {url: string; qr_data_url: string; expires_at: string};
type Status = {state: string; device_label?: string; confirm_code?: string};

export function MobileConnectionSettings({api, onBack}: {api: Api; onBack: () => void}) {
  const {language} = useLanguage();
  const ja = language === 'ja';
  const [pair, setPair] = useState<Pair|null>(null);
  const [status, setStatus] = useState<Status|null>(null);
  const [relay, setRelay] = useState('https://api.happier.dev');
  const [consent, setConsent] = useState(false);
  const [host, setHost] = useState<{workspaces:string[];happier_cli_available:boolean}|null>(null);
  const [workspace, setWorkspace] = useState('');
  useEffect(() => {
    let active = true;
    void api('/mobile/status').then(value => {
      if (!active || !Array.isArray(value.workspaces)) return;
      setHost(value);
      setWorkspace(current => value.workspaces.includes(current) ? current : value.workspaces[0] || '');
    }).catch(() => {});
    return () => {active = false;};
  }, [api]);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!pair) return;
    let live = true;
    const poll = async () => {
      if (Date.now() >= Date.parse(pair.expires_at)) {setPair(null); setError(ja?'QRの有効期限が切れました。再発行してください。':'QR expired. Create a new one.'); return;}
      try {const next = await api('/mobile/pairing'); if (live) setStatus(next);}
      catch {if (live) setError(ja?'接続要求を取得できません。通信を確認してください。':'Could not check pairing. Check the connection.');}
    };
    void poll();
    const timer = window.setInterval(poll, 2500);
    return () => {live = false; window.clearInterval(timer);};
  }, [api, ja, pair]);
  const perform = async (work: () => Promise<void>) => {
    setBusy(true); setError('');
    try {await work();} catch (reason) {
      const code = reason instanceof Error ? reason.message : String(reason);
      setError((ja?'接続を準備できませんでした。':'Could not prepare pairing. ') + code);
    } finally {setBusy(false);}
  };
  return <section className="mobile-connection">
    <SettingsBack onBack={onBack}/>
    <header><span className="mobile-connection-mark">OLCR <small>MOBILE</small></span>
      <h2>{ja?'スマホを接続':'Connect your phone'}</h2>
      <p>{ja?'このPCで動くOLCRを、スマホから操作します。':'Use OLCR running on this PC from your phone.'}</p></header>
    <ol><li>{ja?'PCでQRを表示':'Show the QR on your PC'}</li><li>{ja?'スマホのOLCRで読み取る':'Scan it in OLCR on your phone'}</li><li>{ja?'確認コードを照合して許可':'Match the code and approve'}</li></ol>
    {status?.state==='approved'?<div role="status" className="mobile-pair-success">{ja?'端末の追加を承認しました。':'Phone linking approved.'}</div>:pair?<div className="mobile-pair-card">
      <img src={pair.qr_data_url} width="240" height="240" alt={ja?'スマホ接続用の一回限りのQRコード':'One-time phone pairing QR'}/>
      <p>{ja?'スマホでこのQRを読み取ってください。':'Scan this QR with your phone.'}</p>
      <small>{ja?'有効期限':'Expires'} {new Date(pair.expires_at).toLocaleTimeString()}</small>
      {status?.state==='requested'&&<div className="mobile-pair-confirm"><strong>{status.device_label|| (ja?'スマホ':'Phone')}</strong>
        <output>{status.confirm_code}</output><p>{ja?'スマホにも同じコードが表示されていることを確認してください。':'Check that your phone shows the same code.'}</p>
        <button disabled={busy} className="mobile-pair-primary" onClick={()=>void perform(async()=>{await api('/mobile/pairing/approve',{method:'POST',body:JSON.stringify({code:status.confirm_code})});setPair(null);setStatus({state:'approved'});})}>{ja?'このスマホを許可':'Allow this phone'}</button></div>}
      <button onClick={()=>void navigator.clipboard.writeText(pair.url)}>{ja?'接続リンクをコピー':'Copy pairing link'}</button>
      <button disabled={busy} onClick={()=>void perform(async()=>{await api('/mobile/pairing',{method:'DELETE'});setPair(null);setStatus(null);})}>{ja?'QRを無効にする':'Cancel pairing'}</button>
    </div>:<div className="mobile-pair-start">
      {host&&!host.happier_cli_available&&<p role="status">{ja?'PCに対応するHappier CLIをインストールしてください。':'Install the compatible Happier CLI on this PC.'}</p>}
      {host&&host.workspaces.length===0&&<p role="status">{ja?'QRはこのPCのOLCRに接続します。許可済みフォルダがない場合、スマホ会話はファイルにアクセスしない一般チャットとして開始します。':'The QR connects to OLCR on this PC. Without an authorized folder, mobile conversations start without file access.'}</p>}
      {host&&host.workspaces.length>0&&<label>{ja?'スマホで使う作業フォルダ':'Workspace for your phone'}<select aria-label={ja?'作業フォルダ':'Workspace'} value={workspace} onChange={event=>setWorkspace(event.target.value)}>{host.workspaces.map(root=><option key={root} value={root}>{root}</option>)}</select></label>}
      <label><input type="checkbox" checked={consent} onChange={event=>setConsent(event.target.checked)}/><span>{ja?'このPCのOLCRと暗号化接続します。メールやパスワードの登録は不要です。':'Connect securely to OLCR on this PC. No email or password registration.'}</span></label>
      <button disabled={!consent||busy||!host||!host.happier_cli_available} className="mobile-pair-primary" onClick={()=>void perform(async()=>{setStatus(null);setPair(await api('/mobile/pairing',{method:'POST',body:JSON.stringify({relay_url:relay,consent:true,...(workspace?{workspace}:{})})}));})}>{busy?(ja?'準備中…':'Preparing…'):(ja?'接続用QRを表示':'Show pairing QR')}</button>
      <button aria-expanded={advanced} onClick={()=>setAdvanced(!advanced)}>{ja?'接続先の詳細':'Connection details'}</button>
      {advanced&&<label>Relay URL<input type="url" value={relay} onChange={event=>setRelay(event.target.value)}/></label>}
    </div>}
    {error&&<p role="alert">{error}</p>}
    <aside><strong>{ja?'現在の対応範囲':'Current support'}</strong><p>{ja?'接続を準備するとこのPCでHappier daemonが起動します。信頼する端末のみを承認してください。端末単位のアクセス失効は未対応です。':'Preparing a connection starts the Happier daemon on this PC. Approve only trusted devices. Individual device revocation is not available.'}</p></aside>
  </section>;
}
