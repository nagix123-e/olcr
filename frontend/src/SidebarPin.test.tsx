import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {SidebarPin,pinnedFirst} from './SidebarPin';
afterEach(cleanup);
it('puts pins first without mutating source and preserves recency when unpinned',()=>{
 const values=[{id:'old',pinned:true,updated_at:1},{id:'new',pinned:false,updated_at:4},{id:'pin',pinned:true,updated_at:3}];
 expect(pinnedFirst(values).map(x=>x.id)).toEqual(['pin','old','new']);
 expect(values.map(x=>x.id)).toEqual(['old','new','pin']);
 expect(pinnedFirst(values.map(x=>({...x,pinned:false}))).map(x=>x.id)).toEqual(['new','pin','old']);
});
it('labels the pin state and prevents repeated changes while saving',()=>{
 const toggle=vi.fn();const view=render(<SidebarPin kind="conversation" label="設計" busy={false} onToggle={toggle}/>);
 fireEvent.click(screen.getByRole('button',{name:'会話「設計」のピン留め'}));expect(toggle).toHaveBeenCalledOnce();
 view.rerender(<SidebarPin kind="conversation" label="設計" pinned busy onToggle={toggle}/>);
 const button=screen.getByRole('button',{name:'会話「設計」のピン留めを解除'});expect(button).toHaveAttribute('aria-pressed','true');expect(button).toBeDisabled();fireEvent.click(button);expect(toggle).toHaveBeenCalledOnce();
});
