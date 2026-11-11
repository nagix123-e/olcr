import './travel-booking.css';

export type TravelBookingReview={
 id:string;provider_id:string;title:string;category:string;price_amount:string;currency:string;fees_amount:string;
 start_date:string;end_date:string;start_time?:string;end_time?:string;party_count:number;cancellation_terms:string;
 price_kind:'live'|'estimate'|'unknown';status:string;expires_at:number;provider_reference?:string|null;
 browser_session_id?:string|null;
};
export type TravelBookingBlock={
 type:'travel_booking';status:'collecting'|'handoff'|'offers'|'review'|'committing'|'confirmed'|'failed'|'uncertain'|'expired'|'cancelled';
 message:string;review?:TravelBookingReview|null;offers?:Array<{id:string;title:string;price_amount:string;currency:string;price_kind:string;start_date:string;end_date:string;start_time?:string;end_time?:string;party_count:number}>;
 provider_reference?:string|null;categories?:string[];
};

const statusText=(status:string,ja:boolean)=>({
 collecting:ja?'予約内容を確認中':'Gathering booking details',handoff:ja?'ブラウザーでの操作が必要':'Browser action needed',
 offers:ja?'予約候補':'Offers',review:ja?'予約内容の最終確認':'Review reservation',committing:ja?'予約先に確認中':'Checking with provider',
 confirmed:ja?'予約先が確定':'Provider confirmed',failed:ja?'予約できませんでした':'Booking failed',
 uncertain:ja?'結果を確認できません':'Outcome uncertain',expired:ja?'確認期限切れ':'Review expired',cancelled:ja?'取り消し済み':'Cancelled'
} as Record<string,string>)[status]||status;

export function TravelBookingCard({block,ja,onConfirm,onSelectOffer}:{block:TravelBookingBlock;ja:boolean;onConfirm?:(reviewId:string)=>void;onSelectOffer?:(offerId:string)=>void}){
 const review=block.review;
 const expired=Boolean(review&&review.expires_at<=Date.now()/1000);
 const feeDisplay=review&&/^\d[\d,.]*$/.test(review.fees_amount)?`${review.fees_amount} ${review.currency}`:(review?.fees_amount||'');
 const canConfirm=Boolean(review&&block.status==='review'&&!expired&&review.price_kind==='live'&&onConfirm);
 return <aside className={`travel-booking-card status-${block.status}`} aria-label={ja?'旅行予約':'Travel booking'}>
  <header><span aria-hidden="true">▣</span><strong>{ja?'予約':'Reservation'}</strong><span className="travel-booking-status" role="status">{statusText(expired?'expired':block.status,ja)}</span></header>
  {review&&<dl className="travel-booking-terms">
   <div><dt>{ja?'予約先':'Provider'}</dt><dd>{review.provider_id}</dd></div>
   <div><dt>{ja?'候補':'Selection'}</dt><dd>{review.title}</dd></div>
   <div><dt>{ja?'合計金額':'Total'}</dt><dd>{review.price_amount} {review.currency} <small>({review.price_kind==='live'?(ja?'最新':'Live'):(ja?'概算・未確定':'Estimate')})</small></dd></div>
   <div><dt>{ja?'追加料金':'Fees'}</dt><dd>{feeDisplay}</dd></div>
   <div><dt>{ja?'日程':'Dates'}</dt><dd>{review.start_date}{review.start_date!==review.end_date?` – ${review.end_date}`:''}{review.start_time?` · ${review.start_time}`:''}{review.end_time?` – ${review.end_time}`:''}</dd></div>
   <div><dt>{ja?'人数':'Party'}</dt><dd>{review.party_count}{ja?'人':' people'}</dd></div>
   <div className="cancellation"><dt>{ja?'取消条件':'Cancellation'}</dt><dd>{review.cancellation_terms}</dd></div>
   <div><dt>{ja?'確認期限':'Expires'}</dt><dd>{new Date(review.expires_at*1000).toLocaleString(ja?'ja-JP':undefined)}</dd></div>
   {review.provider_reference&&<div><dt>{ja?'予約番号':'Reference'}</dt><dd>{review.provider_reference}</dd></div>}
  </dl>}
  {block.message&&<p className="travel-booking-message">{block.message}</p>}
  {block.offers?.map(offer=><article className="travel-booking-offer" key={offer.id}>
   <strong>{offer.title}</strong><span>{offer.price_amount} {offer.currency} · {offer.start_date}{offer.start_date!==offer.end_date?` – ${offer.end_date}`:''}{offer.start_time?` · ${offer.start_time}`:''}{offer.end_time?` – ${offer.end_time}`:''} · {offer.party_count}{ja?'人':' people'}</span>
   {onSelectOffer&&offer.price_kind==='live'&&<button type="button" onClick={()=>onSelectOffer(offer.id)}>{ja?'この候補を確認':'Review this offer'}</button>}
   {offer.price_kind!=='live'&&<small>{ja?'最新価格を確認できないため予約確認に進めません':'A live price is required before review'}</small>}
  </article>)}
  {canConfirm&&<button className="travel-booking-confirm" type="button" onClick={()=>onConfirm?.(review!.id)}>
   {ja?'この内容で予約する':'Confirm this reservation'}
  </button>}
  {block.status==='uncertain'&&<p role="alert">{ja?'予約先で状態を確認してください。重複を避けるため自動再送信はしません。':'Check the provider. OLCR will not retry automatically.'}</p>}
  {block.status==='confirmed'&&<p role="status">{ja?'予約先の確認結果を受け取りました。':'The provider returned a confirmation.'}</p>}
 </aside>;
}
