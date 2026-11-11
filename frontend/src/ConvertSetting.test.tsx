import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ConvertSetting} from './ConvertSetting';
vi.mock('./language',()=>({useLanguage:()=>({language:'en'})}));
afterEach(cleanup);

it('does not request runtime setup or status while Convert is OFF',()=>{
  const api=vi.fn(),save=vi.fn();
  render(<ConvertSetting api={api} enabled={false} save={save}/>);
  expect(screen.getByRole('switch',{name:'Convert'})).not.toBeChecked();
  expect(api).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('switch',{name:'Convert'}));
  expect(save).toHaveBeenCalledWith({convert_enabled:true});
  expect(api).not.toHaveBeenCalled();
});

it('shows setup progress using the existing switch row and permits turning OFF',async()=>{
  const api=vi.fn().mockResolvedValue({state:'preparing',message:'Downloading'}),save=vi.fn().mockResolvedValue({});
  render(<ConvertSetting api={api} enabled={true} save={save}/>);
  await waitFor(()=>expect(api).toHaveBeenCalledWith('/file-conversion/runtime'));
  expect(screen.getByRole('status')).toHaveTextContent('Preparing conversion tools');
  fireEvent.click(screen.getByRole('switch',{name:'Convert'}));
  expect(save).toHaveBeenCalledWith({convert_enabled:false});
});

it('retries failed setup only on an explicit retry click',async()=>{
  const api=vi.fn().mockResolvedValue({state:'failed'}),save=vi.fn();
  render(<ConvertSetting api={api} enabled={true} save={save}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Retry'}));
  await waitFor(()=>expect(api).toHaveBeenCalledWith('/file-conversion/runtime/retry',{method:'POST'}));
});

it('ignores a late progress response after Convert is turned OFF',async()=>{
  let resolve!:(value:unknown)=>void;
  const api=vi.fn(()=>new Promise(done=>{resolve=done})),save=vi.fn();
  const view=render(<ConvertSetting api={api} enabled={true} save={save}/>);
  view.rerender(<ConvertSetting api={api} enabled={false} save={save}/>);
  await act(async()=>resolve({state:'ready'}));
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  expect(screen.getByRole('switch',{name:'Convert'})).not.toBeChecked();
});
