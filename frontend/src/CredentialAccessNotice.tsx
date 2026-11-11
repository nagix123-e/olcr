import {useState} from 'react';
import {native} from './native';
import {useLanguage} from './language';

export function CredentialAccessNotice({error,onRestored}:{error:string;onRestored:()=>Promise<void>}) {
  const {language}=useLanguage(),ja=language==='ja';
  const [busy,setBusy]=useState(false),[failure,setFailure]=useState('');
  if(!error)return null;
  const needsAccess=error.includes('credential_bundle_authorization_required')||error.includes('credential_bundle_bridge_timeout');
  if(!needsAccess)return <p role="alert">{error}</p>;
  return <div className="credential-access-notice" role="status">
    <p>{ja?'保存済みの認証情報にアクセスする許可が必要です。画面の移動では認証を要求しません。':'Access to saved credentials needs your permission. Navigation will not request authorization.'}</p>
    <button type="button" disabled={busy} onClick={async()=>{
      const bridge=native();if(!bridge)return;setBusy(true);setFailure('');
      try{await bridge.core.invoke('restore_external_api_credentials',{authorize:true});await onRestored()}
      catch(reason){const message=reason instanceof Error?reason.message:String(reason);setFailure(message.includes('credential_bundle_authorization_required')||message.includes('credential_bundle_bridge_timeout')?(ja?'許可が完了していません。必要なときにもう一度お試しください。':'Authorization is incomplete. Try again when ready.'):message)}
      finally{setBusy(false)}
    }}>{ja?(busy?'許可を確認中…':'認証情報へのアクセスを許可'):(busy?'Authorizing…':'Authorize credential access')}</button>
    <p className="setting-hint">{ja?'macOSの確認で「常に許可」を選ぶと、同じ署名のOLCRヘルパーへの許可を次回以降も保持できます。':'Choose Always Allow in the macOS dialog to retain permission for the same signed OLCR helper.'}</p>
    {failure&&<p role="alert">{failure}</p>}
  </div>;
}
