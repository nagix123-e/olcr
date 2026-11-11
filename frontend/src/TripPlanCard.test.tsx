import {render,screen,cleanup} from '@testing-library/react';
import {describe,it,expect,afterEach} from 'vitest';
import {TripPlanCard,type TripPlanBlock} from './TripPlanCard';
afterEach(cleanup);
const block:TripPlanBlock={type:'trip_plan',status:'collecting',updated_at:0,brief:{origin:'東京',destination:'京都',start_date:'2026-12-01',end_date:'2026-12-02',travelers:2,budget:'全員10万円',interests:'庭園',preferences:'歩行を少なめ',pace:'ゆっくり'}};
describe('Travel brief in chat',()=>{
 it('shows saved conditions and how to continue',()=>{
  render(<TripPlanCard block={block} ja/>);
  expect(screen.getByText('全員10万円')).toBeTruthy();
  expect(screen.getByText('歩行を少なめ')).toBeTruthy();
  expect(screen.getByText(/\/trip new/)).toBeTruthy();
  expect(document.querySelector('details')?.open).toBe(true);
 });
 it('keeps completed drafts compact with accessible status',()=>{
  render(<TripPlanCard block={{...block,status:'ready'}} ja={false}/>);
  expect(screen.getByText(/Draft ready/)).toBeTruthy();
  expect(document.querySelector('details')?.open).toBe(false);
 });
});
