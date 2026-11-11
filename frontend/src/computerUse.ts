import {collectVisibleDomObservation,locateVisibleDomElement} from './screenObservation';
type Api=(path:string,init?:RequestInit)=>Promise<any>;

/** Execute the single reversible DOM action exposed by the current T prototype. */
export async function openGroundedDisclosure(question:string,projectId:string,conversationId:string|null,api:Api,root:ParentNode=document){
 const before=collectVisibleDomObservation(root);
 const plan=await api('/computer/actions/open-disclosure/plan',{method:'POST',body:JSON.stringify({
  question,project_id:projectId,conversation_id:conversationId,observation:before,
 })});
 if(plan.action_route?.status!=='PASS'||plan.action_route.mechanism!=='native_structured'||plan.action_route.verification_required!==true)
  throw Error('ACTION_ROUTE_NOT_EXECUTABLE');
 if(plan.action_route.observation_id!==before.dom.observation_id||plan.action_route.session_id!==before.dom.session_id)
  throw Error('ACTION_ROUTE_STATE_MISMATCH');
 const current=collectVisibleDomObservation(root);
 const target=before.dom.elements.find(item=>item.id===plan.selected_element?.id);
 const liveTarget=current.dom.elements.find(item=>item.id===plan.selected_element?.id);
 const node=locateVisibleDomElement(String(plan.selected_element?.id||''),root);
 if(!target||target.role!=='disclosure'||target.label!==plan.selected_element.label||target.expanded!==false
    ||!liveTarget||liveTarget.role!=='disclosure'||liveTarget.label!==target.label||liveTarget.expanded!==false
    ||current.dom.session_id!==before.dom.session_id
    ||!node||node.tagName!=='SUMMARY'||node.parentElement?.tagName!=='DETAILS'||(node.parentElement as HTMLDetailsElement).open){
  throw Error('SEMANTIC_TARGET_STALE_OR_NOT_ALLOWLISTED');
 }
 // Native summary click toggles only this in-app disclosure; no coordinates or arbitrary selectors are executed.
 node.click();
 const after=collectVisibleDomObservation(root);
 const verification=await api('/computer/actions/open-disclosure/verify',{method:'POST',body:JSON.stringify({
  plan_id:plan.plan_id,observation:after,
  execution_receipt:{source:'native_structured',action_kind:'open_disclosure',status:'PASS',execution_attempted:true,
   before_observation_id:before.dom.observation_id,before_session_id:before.dom.session_id,
   after_observation_id:after.dom.observation_id,after_session_id:after.dom.session_id},
 })});
 if(!verification.verified)throw Error(`POSTCONDITION_NOT_VERIFIED: ${verification.reason||verification.status}`);
 return verification as {status:string;verified:boolean;message:string;conversation_id:string};
}
