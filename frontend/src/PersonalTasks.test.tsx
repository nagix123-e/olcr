import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
vi.mock('./language',()=>({useLanguage:()=>({language:'en'})}));
import {PersonalTasks} from './PersonalTasks';
afterEach(()=>{cleanup();vi.clearAllMocks()});
const profile={display_name:'Nagi',time_zone:'Asia/Tokyo',language:'en',response_style:'balanced',interests:'',instructions:'',dietary_preferences:'',accessibility_preferences:'',personalization_enabled:false,background_enabled:false,mail_enabled:false,web_enabled:false,model_enabled:false};
const fixture=(tasks:any[]=[],notifications:any[]=[])=>vi.fn(async(path:string,init?:RequestInit)=>{
 if(path==='/personal/profile')return init?JSON.parse(String(init.body)):profile;
 if(path==='/personal/goals')return {goals:[]};
 if(path==='/personal/tasks')return init?{id:'new',...JSON.parse(String(init.body)),status:'paused'}:{tasks};
 if(path==='/personal/inbox')return {notifications};
 if(path==='/personal/runs')return {runs:[]};
 return {};
});
it('creates a real scheduled reminder and preserves profile consent on save',async()=>{
 const api=fixture();render(<PersonalTasks api={api}/>);
 await screen.findByDisplayValue('Nagi');
 fireEvent.change(screen.getByLabelText('Task title'),{target:{value:'Read a chapter'}});
 fireEvent.click(screen.getByRole('button',{name:'Save task'}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/personal/tasks',expect.objectContaining({method:'POST'})));
 const call=api.mock.calls.find(([path,init])=>path==='/personal/tasks'&&init?.method==='POST');
 expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({title:'Read a chapter',kind:'reminder',enabled:false});
 fireEvent.click(screen.getByRole('checkbox',{name:'Use my preferences in chat'}));
 fireEvent.click(screen.getByRole('button',{name:'Save preferences'}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/personal/profile',{method:'PUT',body:JSON.stringify({...profile,personalization_enabled:true})}));
});
it('shows read-only results and performs only the requested control',async()=>{
 const task={id:'t1',title:'Inbox review',kind:'mail_review',status:'active',enabled:true,next_run_at:1,interval_seconds:300,query:'',prompt:'',connector_id:'service.google.gmail'};
 const api=fixture([task],[{id:'n1',task_id:'t1',title:'Inbox review',created_at:1,read_at:null,result:{summary:'1 deadline proposals',proposals:[{title:'Report',date:'2026-12-20',message_id:'m1'}]}}]);
 render(<PersonalTasks api={api}/>);
 expect(await screen.findByText('1 deadline proposals')).toBeInTheDocument();
 expect(screen.getByText('Report · 2026-12-20')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'Pause Inbox review'}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/personal/tasks/t1/control',{method:'POST',body:JSON.stringify({action:'pause'})}));
 expect(api.mock.calls.some(([path])=>path.includes('/confirm'))).toBe(false);
});
it('retains errors and never reports a failed preference save as success',async()=>{
 const api=fixture();api.mockImplementation(async(path:string,init?:RequestInit)=>{if(init)throw Error('Save failed');if(path==='/personal/profile')return profile;if(path==='/personal/tasks')return{tasks:[]};if(path==='/personal/goals')return{goals:[]};return{notifications:[],runs:[]};});
 render(<PersonalTasks api={api}/>);await screen.findByDisplayValue('Nagi');
 fireEvent.click(screen.getByRole('button',{name:'Save preferences'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('Save failed');
 expect(screen.queryByText('Preferences saved.')).not.toBeInTheDocument();
});
