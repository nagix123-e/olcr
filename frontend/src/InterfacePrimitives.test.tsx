import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {DialogFrame} from './InterfacePrimitives';
vi.mock('./language',()=>({useLanguage:()=>({language:'en'})}));
afterEach(()=>{cleanup();vi.restoreAllMocks()});
it('retains edited input when dismissal is declined, then allows dismissal',()=>{
 const close=vi.fn(),confirm=vi.spyOn(window,'confirm').mockReturnValue(false);
 render(<DialogFrame title="Connect service" onClose={close}><input aria-label="API key"/></DialogFrame>);
 const field=screen.getByLabelText('API key');fireEvent.input(field,{target:{value:'test-only'}});
 fireEvent.keyDown(field,{key:'Escape'});
 expect(confirm).toHaveBeenCalledOnce();expect(close).not.toHaveBeenCalled();expect(field).toHaveValue('test-only');
 confirm.mockReturnValue(true);fireEvent.click(screen.getByRole('button',{name:'Close'}));expect(close).toHaveBeenCalledOnce();
});
it('keeps a running download dialog open and restores the prior focus on unmount',()=>{
 const trigger=document.createElement('button');document.body.append(trigger);trigger.focus();
 const close=vi.fn();const view=render(<DialogFrame title="Download model" onClose={close} closeDisabled><button>Download</button></DialogFrame>);
 const dialog=screen.getByRole('dialog',{name:'Download model'});
 fireEvent.keyDown(dialog,{key:'Escape'});expect(close).not.toHaveBeenCalled();expect(screen.getByRole('button',{name:'Close'})).toBeDisabled();
 view.unmount();expect(document.activeElement).toBe(trigger);trigger.remove();
});
