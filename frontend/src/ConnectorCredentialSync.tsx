import {useEffect,useState} from 'react';
import {native} from './native';
import {useLanguage} from './language';

export type ConnectorCredentialSyncBlock={type:'service_credential_sync';connector_id:string};
const handles:Record<string,string>={
 'service.google.gmail':'OLCR_CONNECTOR_GOOGLE_GMAIL',
 'service.google.calendar':'OLCR_CONNECTOR_GOOGLE_CALENDAR',
 'service.google.drive':'OLCR_CONNECTOR_GOOGLE_DRIVE',
};

/** Only the connector ID travels in conversation history; OAuth data uses private endpoints. */
export function ConnectorCredentialSync({block,api}:{block:ConnectorCredentialSyncBlock;api:(path:string,init?:RequestInit)=>Promise<any>}){
 const {language}=useLanguage(),ja=language==='ja';
 const [failed,setFailed]=useState(false);
 useEffect(()=>{
  let live=true;setFailed(false);
  const sync=async()=>{
   const handle=handles[block.connector_id];if(!handle)throw Error('UNSUPPORTED_CONNECTOR');
   const path=`/connectors/${encodeURIComponent(block.connector_id)}/credential-sync`;
   const result=await api(path);if(!live||result.bundle===null)return;
   if(typeof result.bundle!=='string'||!result.bundle)throw Error('INVALID_CONNECTOR_BUNDLE');
   const bridge=native();if(!bridge)throw Error('KEYCHAIN_UNAVAILABLE');
   await bridge.core.invoke('set_external_credential',{handle,value:result.bundle});
   if(!live)return;
   await api(path,{method:'POST',body:JSON.stringify({bundle:result.bundle})});
  };
  void sync().catch(()=>{if(live)setFailed(true)});
  return()=>{live=false};
 },[block.connector_id,api]);
 return failed?<small role="alert">{ja?'更新された接続情報を保存できませんでした。設定から接続し直してください。':'Could not save the refreshed connection. Reconnect in Settings.'}</small>:null;
}
