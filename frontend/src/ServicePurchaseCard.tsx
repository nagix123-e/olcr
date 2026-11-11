import {useState} from 'react';
import {openExternal} from './native';
import {useLanguage} from './language';
import './credential-setup.css';

export type ServicePurchaseBlock={type:'service_purchase';id:string;status:'pending';order:{merchant:string;url:string;items:Array<{name:string;quantity:number;unit_price:number|string}>;subtotal:number|string|null;shipping:number|string|null;tax:number|string|null;total:number|string;currency:string;estimated?:boolean}};

/** Approval opens an exact merchant checkout; the card never reports payment success. */
export function ServicePurchaseCard({block,api}:{block:ServicePurchaseBlock;api:(path:string,init?:RequestInit)=>Promise<any>}){
 const {language}=useLanguage(),ja=language==='ja';
 const [busy,setBusy]=useState(false),[state,setState]=useState<'pending'|'checkout_ready'|'denied'>(block.status),[url,setUrl]=useState(''),[error,setError]=useState('');
 const amount=(value:number|string|null)=>value===null?(ja?'店舗で確定':'Determined at checkout'):`${block.order.currency} ${String(value)}`;
 const confirm=async(approve:boolean)=>{
  if(busy||state!=='pending')return;setBusy(true);setError('');
  try{
   const result=await api(`/connectors/purchases/${encodeURIComponent(block.id)}/confirm`,{method:'POST',body:JSON.stringify({approve,order:block.order})});
   if(!approve&&result.status==='denied'){setState('denied');return;}
   if(result.status!=='checkout_ready'||result.purchased!==false)throw Error('PURCHASE_CHECKOUT_RESPONSE_INVALID');
   const checkout=new URL(result.url),original=new URL(block.order.url);
   if(checkout.protocol!=='https:'||checkout.username||checkout.password||checkout.href!==original.href)throw Error('PURCHASE_CHECKOUT_URL_CHANGED');
   setUrl(checkout.href);setState('checkout_ready');
  }catch(reason){
   const message=reason instanceof Error?reason.message:String(reason);
   setError(/EXPIRED|NOT_FOUND/.test(message)?(ja?'この確認は期限切れです。注文内容を準備し直してください。':'This review has expired. Prepare the order again.'):message);
  }finally{setBusy(false)}
 };
 return <section className="credential-setup service-purchase-card" aria-label={ja?'購入内容の確認':'Purchase review'}>
  <h3>{ja?'注文内容を確認':'Review order details'}</h3><strong>{block.order.merchant}</strong>
  <p>{ja?'注文はまだ確定していません。確認後、店舗のサイトで支払いを完了してください。':'Not purchased. Complete payment on the merchant website.'}</p>
  <ul>{block.order.items.map((item,index)=><li key={index}>{item.name} × {item.quantity} · {amount(item.unit_price)}</li>)}</ul>
  <dl><dt>{ja?'小計':'Subtotal'}</dt><dd>{amount(block.order.subtotal)}</dd><dt>{ja?'送料':'Shipping'}</dt><dd>{amount(block.order.shipping)}</dd><dt>{ja?'税':'Tax'}</dt><dd>{amount(block.order.tax)}</dd><dt>{block.order.estimated?(ja?'見積合計':'Estimated total'):(ja?'合計':'Total')}</dt><dd><strong>{amount(block.order.total)}</strong></dd></dl>
  {block.order.estimated&&<p>{ja?'送料・税を含む最終金額は店舗の購入画面で確認してください。':'Confirm the final amount, including shipping and tax, on the merchant website.'}</p>}
  {state==='pending'&&<div className="brain-form-actions"><button type="button" disabled={busy} onClick={()=>void confirm(false)}>{ja?'購入をキャンセル':'Cancel purchase'}</button><button type="button" className="primary" disabled={busy} onClick={()=>void confirm(true)}>{busy?(ja?'確認中…':'Confirming…'):(ja?'注文内容を確認して進む':'Confirm order details')}</button></div>}
  {state==='denied'&&<p role="status">{ja?'購入をキャンセルしました。':'Purchase cancelled.'}</p>}
  {state==='checkout_ready'&&<button type="button" className="primary" onClick={()=>{void openExternal(url).catch(()=>setError(ja?'店舗のページを開けませんでした。':'Could not open the merchant checkout.'))}}>{ja?'店舗の購入手続きを開く':'Open merchant checkout'}</button>}
  {error&&<p role="alert">{error}</p>}
 </section>;
}
