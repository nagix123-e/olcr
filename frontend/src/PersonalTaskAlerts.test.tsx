import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {PersonalTaskAlerts} from './PersonalTaskAlerts';
afterEach(cleanup);
it('surfaces unread in-app completions and opens the conversation',async()=>{
 const onOpen=vi.fn();render(<PersonalTaskAlerts api={async()=>({notifications:[{id:'a',read_at:null},{id:'b',read_at:10}]})} ja={false} onOpen={onOpen}/>);
 fireEvent.click(await screen.findByText(/1 task updates/));expect(onOpen).toHaveBeenCalledOnce();
});
