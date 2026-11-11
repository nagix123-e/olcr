import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {MobileConnectionSettings} from './MobileConnectionSettings';
afterEach(cleanup);
it('requires consent and a PC confirmation before granting a phone access',async()=>{
  const api=vi.fn(async(path:string,init?:RequestInit)=>{
    if(init?.method==='POST'&&path==='/mobile/pairing')return {url:'happier:///pair?test',qr_data_url:'data:image/svg+xml;base64,AA==',expires_at:'2099-01-01T00:00:00Z'};
    if(path==='/mobile/pairing/approve')return {state:'approved'};
    return {state:'requested',device_label:'Test phone',confirm_code:'123 456'};
  });
  render(<MobileConnectionSettings api={api} onBack={()=>{}}/>);
  expect(screen.getByRole('button',{name:'接続用QRを表示'})).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button',{name:'接続用QRを表示'}));
  expect(await screen.findByAltText('スマホ接続用の一回限りのQRコード')).toBeVisible();
  await screen.findByText('123 456');
  expect(api.mock.calls.some(([path])=>path==='/mobile/pairing/approve')).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:'このスマホを許可'}));
  await screen.findByText('端末の追加を承認しました。');
  const request=api.mock.calls.find(([path])=>path==='/mobile/pairing/approve');
  expect(JSON.parse(String(request?.[1]?.body))).toEqual({code:'123 456'});
});
