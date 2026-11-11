import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
const call=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:call,subscribeCodingTask:()=>()=>{}}));
import {ChatComposer} from './ChatComposer';
const props={text:'',setText:vi.fn(),disabled:false,loading:false,send:vi.fn(),commands:[],setAttachment:vi.fn()};
afterEach(()=>{cleanup();vi.clearAllMocks()});
describe('backend permission selection',()=>{
 it('loads server selection and persists only permissions',async()=>{
  call.mockImplementation((path:string,init?:RequestInit)=>Promise.resolve(path==='/permissions'?{level:init?'full':'confirm'}:[]));
  render(<ChatComposer {...props}/>);
  await act(async()=>{});
  fireEvent.click(screen.getByRole('button',{name:/権限設定|Permission settings/}));
  await act(async()=>{});
  expect(screen.getByRole('button',{name:/承認を確認|Confirm Approval/})).toHaveAttribute('aria-pressed','true');
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:/フルアクセス|Full Access/}))});
  expect(call).toHaveBeenCalledWith('/permissions',{method:'PUT',body:JSON.stringify({level:'full'})});
  expect(call.mock.calls.some(([path,init])=>path==='/settings'&&init?.method==='PUT')).toBe(false);
  expect(screen.queryByRole('dialog')).toBeNull();
 });
 it('preserves the server selection and shows save failure',async()=>{
  call.mockImplementation((path:string,init?:RequestInit)=>init?Promise.reject(Error('save failed')):Promise.resolve(path==='/permissions'?{level:'automatic'}:[]));
  render(<ChatComposer {...props}/>);
  await act(async()=>{});
  fireEvent.click(screen.getByRole('button',{name:/権限設定|Permission settings/}));
  await act(async()=>{});
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:/フルアクセス|Full Access/}))});
  expect(screen.getByRole('alert')).toHaveTextContent('save failed');
  expect(screen.getByRole('button',{name:/おまかせ承認|Automatic Approval/})).toHaveAttribute('aria-pressed','true');
 });
});
