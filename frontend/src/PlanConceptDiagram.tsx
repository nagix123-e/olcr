import {lazy,Suspense,useMemo,useState} from 'react';
import {diagramError,type ConceptDiagram} from './conceptDiagramData';
import type {CodingPhase,CodingGraphTask} from './CodingTaskProgress';
import {useLanguage} from './language';
const Preview=lazy(()=>import('./ConceptDiagram').then(module=>({default:module.ConceptDiagramPreview})));
// Render saved system architecture, or derive a diagram only from persisted execution steps.
export function PlanConceptDiagram({phases,tasks,systemDiagram}:{phases:CodingPhase[];tasks:CodingGraphTask[];systemDiagram?:ConceptDiagram}){
  const {language}=useLanguage(),ja=language==='ja',[open,setOpen]=useState(false);
  const planDiagram=useMemo(()=>{
    const selectedPhases=phases.slice(0,20),selectedTasks=tasks.filter(t=>!t.phase_id||selectedPhases.some(p=>p.id===t.phase_id)).slice(0,56);
    const phaseIds=new Map(selectedPhases.map((p,i)=>[p.id,`phase_${i}`])),taskIds=new Map(selectedTasks.map((t,i)=>[t.task_id,`task_${i}`]));
    const partial=selectedPhases.length<phases.length||selectedTasks.length<tasks.length;
    const value:ConceptDiagram={type:'concept_diagram',schema_version:2,title:ja?(partial?'実装計画の概念図（一部）':'実装計画の概念図'):(partial?'Implementation plan (partial)':'Implementation plan'),nodes:[],edges:[],intersections:[]};
    selectedPhases.forEach((phase,i)=>{const members=selectedTasks.filter(t=>t.phase_id===phase.id),position={x:(i%2)*960,y:Math.floor(i/2)*1540};value.nodes.push({id:phaseIds.get(phase.id)!,label:phase.goal.slice(0,140),position,size:{width:920,height:Math.max(160,48+Math.ceil(members.length/4)*100)}});members.forEach((task,j)=>value.nodes.push({id:taskIds.get(task.task_id)!,label:task.goal.slice(0,140),position:{x:position.x+20+(j%4)*220,y:position.y+48+Math.floor(j/4)*100},size:{width:200,height:90},parent_id:phaseIds.get(phase.id)}));});
    selectedTasks.filter(t=>!t.phase_id).forEach((task,i)=>value.nodes.push({id:taskIds.get(task.task_id)!,label:task.goal.slice(0,140),position:{x:(i%4)*220,y:Math.ceil(selectedPhases.length/2)*1540+Math.floor(i/4)*110},size:{width:200,height:90}}));
    const link=(source:string|undefined,target:string|undefined)=>{if(source&&target&&value.edges.length<200)value.edges.push({id:`link_${value.edges.length}`,source,target,label:'',arrow_end:true,source_handle:'right',target_handle:'left'});};
    selectedPhases.forEach(p=>p.dependencies?.forEach(d=>link(phaseIds.get(d),phaseIds.get(p.id))));selectedTasks.forEach(t=>t.depends_on?.forEach(d=>link(taskIds.get(d),taskIds.get(t.task_id))));
    return diagramError(value)?null:value;
  },[phases,tasks,ja]);
  const validSystemDiagram=systemDiagram&&!diagramError(systemDiagram)?systemDiagram:null;
  const diagram=validSystemDiagram??planDiagram;
  if(!diagram)return null;
  return <details className="coding-concept-diagram" onToggle={event=>setOpen(event.currentTarget.open)}><summary>{validSystemDiagram?(ja?'システム設計の概念図':'System design diagram'):(ja?'実装計画を概念図で見る':'View implementation plan as a diagram')}</summary>{open&&<Suspense fallback={<p>{ja?'図を読み込み中…':'Loading diagram…'}</p>}><Preview diagram={diagram} onEdit={diagram=>window.dispatchEvent(new CustomEvent('olcr-edit-concept-diagram',{detail:diagram}))}/></Suspense>}<small>{validSystemDiagram?(ja?'計画に保存されたシステム設計です。実装・検証済みとは限りません。':'System design saved in the plan; this is not evidence of implementation or verification.'):(ja?'保存済みフェーズ・タスク・依存関係を図示しています。':'Depicts saved phases, tasks and dependencies.')}</small></details>;
}
