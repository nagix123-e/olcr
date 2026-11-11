import {useEffect,useState} from 'react';
import {UiIcon,type UiIconName} from './UiIcon';

type Api=(path:string,init?:RequestInit)=>Promise<any>;

const officialIcons:Record<string,string>={
 'service.google.gmail':'https://www.gstatic.com/marketing-cms/assets/images/60/db/3a25579a4b0d87c2a1bfa95c609d/gmail.webp%3Ds80-fcrop64%3D1%2C00000000ffffffff-rw',
 'service.google.calendar':'https://www.gstatic.com/marketing-cms/assets/images/74/54/3480401a4bfa8d9ba2538f9c5730/calendar.webp%3Ds80-fcrop64%3D1%2C00000000ffffffff-rw',
 'service.google.drive':'https://www.gstatic.com/marketing-cms/assets/images/a1/a6/49e0b7d9453b9b2a56526c0df3ff/drive.webp%3Ds80-fcrop64%3D1%2C00000000ffffffff-rw',
 'service.commerce.checkout':'https://cdn.shopify.com/shopifycloud/brochure/assets/brand-assets/shopify-logo-shopping-bag-full-color-66166b2e55d67988b56b4bd28b63c271e2b9713358cb723070a92bde17ad7d63.svg',
};
const fallback:Record<string,UiIconName>={
 'service.google.gmail':'mail',
 'service.apple.mail':'mail',
 'service.google.calendar':'calendar',
 'service.google.drive':'files',
 'service.commerce.checkout':'shopping',
};

/** Brand marks come from the providers' own asset hosts; local glyphs cover offline/failure. */
export function ServiceConnectorIcon({connectorId,networkAllowed,api}:{connectorId:string;networkAllowed:boolean;api?:Api}){
 const [failed,setFailed]=useState(false),[appleIcon,setAppleIcon]=useState('');
 useEffect(()=>{if(connectorId!=='service.apple.mail'||!api)return;let live=true;void api('/connectors/service.apple.mail/icon').then(result=>{if(live&&typeof result?.png_base64==='string'&&result.png_base64.length<2_000_000)setAppleIcon(result.png_base64)}).catch(()=>{});return()=>{live=false};},[api,connectorId]);
 const src=officialIcons[connectorId];
 return <span className="service-brand-icon" aria-hidden="true">
  {connectorId==='service.apple.mail'&&appleIcon?<img className="apple-mail-brand" src={`data:image/png;base64,${appleIcon}`} alt=""/>:networkAllowed&&src&&!failed?<img src={src} alt="" loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={()=>setFailed(true)}/>:<UiIcon name={fallback[connectorId]||(connectorId.startsWith('service.commerce.')?'shopping':'connections')} size={22}/>}
 </span>;
}
