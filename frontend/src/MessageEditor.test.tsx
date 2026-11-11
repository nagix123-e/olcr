import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {MessageEditor,editableMessageIds} from './MessageEditor';
afterEach(cleanup);
it('limits editing to the latest two user messages, independent of assistant count',()=>{
  expect([...editableMessageIds([{id:'u1',role:'user'},{id:'a1',role:'assistant'},{id:'u2',role:'user'},{id:'a2',role:'assistant'},{id:'a3',role:'assistant'},{id:'u3',role:'user'}])]).toEqual(['u2','u3']);
});
it('opens editing in the shared composer and leaves the message displayed',()=>{
  const edit=vi.fn();render(<MessageEditor ja={false} disabled={false} onEdit={edit}>Original message</MessageEditor>);
  fireEvent.click(screen.getByRole('button',{name:'Edit message'}));
  expect(edit).toHaveBeenCalledOnce();expect(screen.getByText('Original message')).toBeInTheDocument();expect(screen.queryByRole('textbox')).toBeNull();
});
it('keeps editing unavailable while a message is being sent',()=>{
  const edit=vi.fn();render(<MessageEditor ja={false} disabled onEdit={edit}>Original</MessageEditor>);
  expect(screen.getByRole('button',{name:'Edit message'})).toBeDisabled();fireEvent.click(screen.getByRole('button',{name:'Edit message'}));expect(edit).not.toHaveBeenCalled();
});
