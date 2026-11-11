import {describe,it,expect,vi} from 'vitest';
import {openGroundedDisclosure} from './computerUse';

describe('grounded disclosure action',()=>{
 const route={status:'PASS',mechanism:'native_structured',verification_required:true};
 const routeFor=(body:any)=>({...route,observation_id:body.observation.dom.observation_id,session_id:body.observation.dom.session_id});
 it('executes only the selected live summary and verifies a fresh expanded state',async()=>{
  document.body.innerHTML='<main><details><summary>Project Core Context</summary><button>Save</button></details></main>';
  const calls:any[]=[];
  const api=vi.fn(async(path:string,init?:RequestInit)=>{
   const body=JSON.parse(String(init?.body));calls.push({path,body});
   if(path.endsWith('/plan'))return {plan_id:'plan-1',conversation_id:'conversation-1',action_route:routeFor(body),selected_element:{id:'dom-0',label:'Project Core Context'}};
   expect(body.observation.dom.elements[0].expanded).toBe(true);
   return {status:'PASS',verified:true,message:'Opened and verified',conversation_id:'conversation-1'};
  });
  const result=await openGroundedDisclosure('Open Project Core Context','project-1',null,api);
  expect(document.querySelector('details')?.open).toBe(true);
  expect(result.verified).toBe(true);
  expect(calls.map(call=>call.path)).toEqual(['/computer/actions/open-disclosure/plan','/computer/actions/open-disclosure/verify']);
  expect(calls[1].body.execution_receipt).toMatchObject({source:'native_structured',action_kind:'open_disclosure',
   status:'PASS',execution_attempted:true,before_session_id:beforeSession(calls[0].body),
   before_observation_id:calls[0].body.observation.dom.observation_id,
   after_session_id:calls[1].body.observation.dom.session_id,
   after_observation_id:calls[1].body.observation.dom.observation_id});
 });

 it('fails closed if the semantic ID does not resolve to the same closed disclosure',async()=>{
  document.body.innerHTML='<main><details open><summary>Project Core Context</summary></details></main>';
  const api=vi.fn(async(_path:string,init?:RequestInit)=>{const body=JSON.parse(String(init?.body));return {plan_id:'plan-1',action_route:routeFor(body),selected_element:{id:'dom-0',label:'Project Core Context'}}});
  await expect(openGroundedDisclosure('Open context','project-1',null,api)).rejects.toThrow('SEMANTIC_TARGET_STALE_OR_NOT_ALLOWLISTED');
  expect(api).toHaveBeenCalledTimes(1);
 });

 it('fails closed if the target changes while semantic planning is pending',async()=>{
  document.body.innerHTML='<main><details><summary>Project Core Context</summary></details></main>';
  const api=vi.fn(async(_path:string,init?:RequestInit)=>{
   const body=JSON.parse(String(init?.body));
   document.querySelector('summary')!.textContent='Different disclosure';
   return {plan_id:'plan-1',action_route:routeFor(body),selected_element:{id:'dom-0',label:'Project Core Context'}};
  });
  await expect(openGroundedDisclosure('Open context','project-1',null,api)).rejects.toThrow('SEMANTIC_TARGET_STALE_OR_NOT_ALLOWLISTED');
  expect(document.querySelector('details')?.open).toBe(false);
 });

 it('fails closed if the router result belongs to another observation or session',async()=>{
  document.body.innerHTML='<main><details><summary>Project Core Context</summary></details></main>';
  const api=vi.fn(async(_path:string,init?:RequestInit)=>{
   const body=JSON.parse(String(init?.body));
   return {plan_id:'plan-1',action_route:{...routeFor(body),observation_id:'other-observation'},
    selected_element:{id:'dom-0',label:'Project Core Context'}};
  });
  await expect(openGroundedDisclosure('Open context','project-1',null,api)).rejects.toThrow('ACTION_ROUTE_STATE_MISMATCH');
  expect(document.querySelector('details')?.open).toBe(false);
 });

 it('does not claim success when the fresh postcondition is not verified',async()=>{
  document.body.innerHTML='<main><details><summary>Project Core Context</summary></details></main>';
  const api=vi.fn(async(path:string,init?:RequestInit)=>path.endsWith('/plan')
   ?{plan_id:'plan-1',action_route:routeFor(JSON.parse(String(init?.body))),selected_element:{id:'dom-0',label:'Project Core Context'}}
   :{status:'FAILED',verified:false,reason:'EXPECTED_POSTCONDITION_NOT_OBSERVED'});
  await expect(openGroundedDisclosure('Open context','project-1',null,api)).rejects.toThrow('POSTCONDITION_NOT_VERIFIED');
 });
});

function beforeSession(body:any){return body.observation.dom.session_id}
