import {act,cleanup,renderHook,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {useAssistantBridge} from './assistantBridge';
const mocks=vi.hoisted(()=>({api:vi.fn(),invoke:vi.fn()}));
vi.mock('./api',()=>({api:mocks.api}));
vi.mock('./native',()=>({native:()=>({core:{invoke:mocks.invoke}})}));
afterEach(()=>{cleanup();vi.resetAllMocks()});
it('schedules persisted alarms and timers with macOS and cancels stopped requests',async()=>{
 const deadline=Date.now()/1000+600;
 mocks.api.mockImplementation(async(path:string)=>path==='/assistant/timers'?{timers:[
  {id:'timer',name:'Tea',deadline,status:'running',notification_status:'pending'},
  {id:'alarm',kind:'alarm',name:'Wake up',deadline,status:'running',notification_status:'pending'},
  {id:'cancelled',deadline:null,status:'cancelled'}
 ]}:{time_zone_configured:true});
 mocks.invoke.mockResolvedValue({status:'scheduled',scheduled_ids:['timer','alarm'],failed_ids:[]});
 renderHook(()=>useAssistantBridge(true,'ja'));
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith('/assistant/notifications',{method:'POST',body:JSON.stringify({ids:['timer','alarm'],status:'scheduled'})}));
 expect(mocks.invoke).toHaveBeenCalledWith('assistant_sync_notifications',{timers:[{id:'timer',name:'Tea',deadline,kind:'timer'},{id:'alarm',name:'Wake up',deadline,kind:'alarm'}],cancelledIds:['cancelled'],language:'ja'});
});
it('retries notification permission on focus after the user changes macOS Settings',async()=>{
 const deadline=Date.now()/1000+600;
 mocks.api.mockImplementation(async(path:string)=>path==='/assistant/timers'?{timers:[{id:'alarm',kind:'alarm',name:'Alarm',deadline,status:'running',notification_status:'denied'}]}:{time_zone_configured:true});
 mocks.invoke.mockResolvedValueOnce({status:'denied',scheduled_ids:[],failed_ids:['alarm']}).mockResolvedValue({status:'scheduled',scheduled_ids:['alarm'],failed_ids:[]});
 renderHook(()=>useAssistantBridge(true,'en'));
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith('/assistant/notifications',{method:'POST',body:JSON.stringify({ids:['alarm'],status:'denied'})}));
 await act(async()=>{window.dispatchEvent(new Event('focus'))});
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith('/assistant/notifications',{method:'POST',body:JSON.stringify({ids:['alarm'],status:'scheduled'})}));
});
