import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
const request=vi.hoisted(()=>vi.fn());vi.mock('./api',()=>({api:request}));
import {PendingInstructions} from './PendingInstructions';
const task={id:'task',status:'RUNNING',activity:'IMPLEMENTATION',retry_count:0,max_retries:2};
const item={id:'pending',message:'Original instruction',status:'WAITING',received_revision:0};
beforeEach(()=>request.mockReset());afterEach(cleanup);
it('opens the shared composer instead of an inline instruction form',async()=>{
  request.mockResolvedValue({steering:[item]});const edit=vi.fn();render(<PendingInstructions task={task} ja={false} onEdit={edit}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Edit'}));expect(edit).toHaveBeenCalledWith(item);expect(screen.queryByRole('textbox')).toBeNull();
});
it('promotes only the selected pending instruction',async()=>{
  let row={...item};request.mockImplementation(async(_path:string,init?:RequestInit)=>{if(init)row={...row,status:'QUEUED'};return init?row:{steering:[row]}});
  render(<PendingInstructions task={task} ja={false} onEdit={vi.fn()}/>);fireEvent.click(await screen.findByRole('button',{name:'Send to current work'}));
  await screen.findByText('Next boundary');expect(row.status).toBe('QUEUED');
});
it('cancels a pending instruction with a guarded request',async()=>{
  let cancelled=false;request.mockImplementation(async(_path:string,init?:RequestInit)=>{if(init)cancelled=true;const row={...item,status:cancelled?'CANCELLED':'WAITING'};return init?row:{steering:[row]}});
  render(<PendingInstructions task={task} ja={false} onEdit={vi.fn()}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Cancel instruction'}));
  await screen.findByText('Instruction cancelled.');
  expect(request).toHaveBeenCalledWith('/coding-tasks/task/instructions/pending',{method:'PATCH',body:JSON.stringify({action:'cancel',expected_message:item.message,expected_status:'WAITING'})});
  expect(screen.queryByText(item.message)).toBeNull();
});
it('offers an explicit retry for a failed instruction and queues only that item',async()=>{
  let row={...item,status:'NEEDS_REVIEW'};
  request.mockImplementation(async(_path:string,init?:RequestInit)=>{if(init)row={...row,status:'QUEUED'};return init?row:{steering:[row]}});
  render(<PendingInstructions task={task} ja={false} onEdit={vi.fn()}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Review and retry'}));
  await screen.findByText('Next boundary');
  expect(request).toHaveBeenCalledWith('/coding-tasks/task/instructions/pending',{method:'PATCH',body:JSON.stringify({action:'retry',expected_message:item.message,expected_status:'NEEDS_REVIEW'})});
  expect(row.status).toBe('QUEUED');
});
it('never offers editing or cancellation for claimed instructions',async()=>{
  request.mockResolvedValue({steering:[{...item,status:'APPLYING'}]});render(<PendingInstructions task={task} ja={false} onEdit={vi.fn()}/>);
  await waitFor(()=>expect(request).toHaveBeenCalled());expect(screen.queryByRole('button')).toBeNull();
});
