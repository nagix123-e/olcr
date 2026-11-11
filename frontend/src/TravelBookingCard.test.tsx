import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {TravelBookingCard,type TravelBookingBlock} from './TravelBookingCard';
afterEach(cleanup);

const base:TravelBookingBlock={type:'travel_booking',status:'review',message:'内容を確認してください。',review:{
 id:'review-1',provider_id:'managed-browser',title:'京都旅行の宿',category:'lodging',price_amount:'48000',currency:'JPY',fees_amount:'3000',
 start_date:'2026-12-12',end_date:'2026-12-15',start_time:'',end_time:'',party_count:2,
 cancellation_terms:'12月10日まで無料、以降は返金不可',price_kind:'live',status:'review',expires_at:Date.now()/1000+120
}};

describe('TravelBookingCard',()=>{
 it('shows exact terms before the explicit confirmation action',()=>{
  const onConfirm=vi.fn();render(<TravelBookingCard block={base} ja onConfirm={onConfirm}/>);
  expect(screen.getByText('48000 JPY')).toBeTruthy();
  expect(screen.getByText('3000 JPY')).toBeTruthy();
  expect(screen.getByText(/2026-12-12 – 2026-12-15/)).toBeTruthy();
  expect(screen.getByText('12月10日まで無料、以降は返金不可')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'この内容で予約する'}));
  expect(onConfirm).toHaveBeenCalledWith('review-1');
 });
 it('does not imply confirmation before provider acceptance',()=>{
  render(<TravelBookingCard block={base} ja/>);
  expect(screen.queryByText(/予約先が確定/)).toBeNull();
 });
 it('shows fees as unitemized instead of claiming zero when the page lacks a fee breakdown',()=>{
  render(<TravelBookingCard block={{...base,review:{...base.review!,fees_amount:'未内訳'}}} ja/>);
  expect(screen.getByText('未内訳')).toBeTruthy();
  expect(screen.queryByText('未内訳 JPY')).toBeNull();
 });
 it('removes the commit action after expiry or an uncertain outcome',()=>{
  const expired={...base,review:{...base.review!,expires_at:Date.now()/1000-1}};
  const {rerender}=render(<TravelBookingCard block={expired} ja onConfirm={()=>{}}/>);
  expect(screen.queryByRole('button',{name:'この内容で予約する'})).toBeNull();
  rerender(<TravelBookingCard block={{...base,status:'uncertain'}} ja onConfirm={()=>{}}/>);
  expect(screen.queryByRole('button',{name:'この内容で予約する'})).toBeNull();
  expect(screen.getByText(/自動再送信はしません/)).toBeTruthy();
 });
 it('shows the handoff without exposing sensitive values',()=>{
  render(<TravelBookingCard block={{type:'travel_booking',status:'handoff',message:'ブラウザー上で決済を完了してください。'}} ja/>);
  expect(screen.getByText(/決済を完了/)).toBeTruthy();
  expect(screen.queryByRole('button')).toBeNull();
 });
 it('lets the user choose a live offer before creating the exact review',()=>{
  const onSelectOffer=vi.fn();render(<TravelBookingCard block={{type:'travel_booking',status:'offers',message:'候補を選んでください。',offers:[
   {id:'offer-1',title:'京都の宿',price_amount:'48000',currency:'JPY',price_kind:'live',start_date:'2026-12-12',end_date:'2026-12-15',party_count:2},
   {id:'offer-2',title:'概算ホテル',price_amount:'35000',currency:'JPY',price_kind:'estimate',start_date:'2026-12-12',end_date:'2026-12-15',party_count:2}
  ]}} ja onSelectOffer={onSelectOffer}/>);
  fireEvent.click(screen.getByRole('button',{name:'この候補を確認'}));
  expect(onSelectOffer).toHaveBeenCalledWith('offer-1');
  expect(screen.queryByRole('button',{name:'概算ホテル'})).toBeNull();
  expect(screen.getByText(/最新価格を確認できないため/)).toBeTruthy();
 });
});
