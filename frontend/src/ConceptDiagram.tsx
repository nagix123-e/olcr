import {useCallback,useEffect,useMemo,useRef,useState,type CSSProperties} from 'react';
import {createPortal} from 'react-dom';
import {UiIcon} from './UiIcon';
import {DIAGRAM_COLORS,diagramColor,type DiagramColor} from './diagramColors';
import {Background,BaseEdge,ConnectionMode,Controls,Handle,MarkerType,NodeResizer,Position,ReactFlow,ReactFlowProvider,applyNodeChanges,applyEdgeChanges,getStraightPath,useReactFlow,type Edge,type EdgeProps,type Node,type NodeProps,type NodeChange} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './concept-diagram.css';
import {boxSize,containsBox,DEFAULT_BOX_SIZE,diagramError,overlapRect,partiallyOverlaps,reconcileGeometry,upgradeDiagram,type ConceptDiagram,type DiagramNode,type DiagramSide} from './conceptDiagramData';
import {useLanguage} from './language';

type Box=Node<{label:string;borderColor?:string;textColor?:string;container?:boolean;venn?:boolean;disabled?:boolean;edit?:(id:string,label:string)=>void;begin?:()=>void;end?:()=>void;minWidth?:number;minHeight?:number;ariaLabel?:string},'concept'>;
type Link=Edge<{textColor?:string;toggle?:(id:string,end:'start'|'end')=>void;ja?:boolean},'conceptLink'>;
const sides:[DiagramSide,Position][]=[['left',Position.Left],['right',Position.Right],['top',Position.Top],['bottom',Position.Bottom]];
function ConceptBox({id,data,selected}:NodeProps<Box>){return <div className={`concept-box${data.container?' container':''}${data.venn?' venn':''}`} style={{'--concept-border':data.borderColor,'--concept-text':data.textColor} as CSSProperties}>
  {data.edit&&<NodeResizer color="#8c8c8c" isVisible={selected&&!data.disabled} minWidth={data.minWidth||100} minHeight={data.minHeight||60} maxWidth={2400} maxHeight={1600} keepAspectRatio={false} onResizeStart={data.begin} onResizeEnd={data.end}/>}
  {sides.map(([side,position])=><Handle key={side} id={side} type="source" position={position}/>)}
  {data.edit?<textarea className="nodrag nopan nowheel" aria-label={data.ariaLabel} value={data.label} maxLength={500} disabled={data.disabled} onKeyDown={e=>e.stopPropagation()} onChange={e=>data.edit?.(id,e.target.value)}/>:<span>{data.label}</span>}
</div>;}
function ConceptLink(props:EdgeProps<Link>){const [path]=getStraightPath(props),{id,sourceX,sourceY,targetX,targetY,data}=props,dx=targetX-sourceX,dy=targetY-sourceY,length=Math.hypot(dx,dy)||1;return <>
  <BaseEdge id={id} path={path} markerStart={props.markerStart} markerEnd={props.markerEnd} style={props.style} interactionWidth={20}/>
  {props.label&&<text x={(sourceX+targetX)/2} y={(sourceY+targetY)/2-7} textAnchor="middle" className="concept-link-label" style={{fill:data?.textColor}} pointerEvents="none">{String(props.label).slice(0,40)}</text>}
  {data?.toggle&&(['start','end'] as const).map(end=>{const x=end==='start'?sourceX+dx/length*14:targetX-dx/length*14,y=end==='start'?sourceY+dy/length*14:targetY-dy/length*14;return <circle key={end} className="concept-line-end" cx={x} cy={y} r={14} tabIndex={0} role="button" aria-label={data.ja?`${end==='start'?'始点':'終点'}の矢印を切り替え`:`Toggle ${end} arrow`} onContextMenu={event=>{event.preventDefault();event.stopPropagation();data.toggle?.(id,end)}} onKeyDown={event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();event.stopPropagation();data.toggle?.(id,end)}}}><title>{data.ja?'右クリックで矢印を切り替え':'Right-click to toggle arrow'}</title></circle>;})}
</>;}
function IntersectionBox({data}:{data:{label:string}}){return <div className="concept-intersection"><span>{data.label}</span></div>;}
const nodeTypes={concept:ConceptBox,intersection:IntersectionBox},edgeTypes={conceptLink:ConceptLink};
function useNodeMeasurements(){
  const [measured,setMeasured]=useState<Record<string,{width:number;height:number}>>({});
  const record=useCallback((changes:NodeChange[])=>{const rows=changes.filter(c=>c.type==='dimensions'&&c.dimensions);if(!rows.length)return;setMeasured(previous=>{let next=previous;for(const row of rows){if(row.type!=='dimensions'||!row.dimensions)continue;const size=row.dimensions;if(previous[row.id]?.width!==size.width||previous[row.id]?.height!==size.height){if(next===previous)next={...previous};next[row.id]=size;}}return next;});},[]);
  return [measured,record] as const;
}

function buildNodes(value:ConceptDiagram,decorate:(node:DiagramNode)=>Box['data']):Node[]{
  const byId=new Map(value.nodes.map(n=>[n.id,n]));
  const depth=(node:DiagramNode):number=>node.parent_id&&byId.has(node.parent_id)?1+depth(byId.get(node.parent_id)!):0;
  const nodes:Node[]=value.nodes.slice().sort((a,b)=>depth(a)-depth(b)).map(n=>{const parent=n.parent_id?byId.get(n.parent_id):null,size=boxSize(n);return {id:n.id,type:'concept',position:parent?{x:n.position.x-parent.position.x,y:n.position.y-parent.position.y}:n.position,parentId:parent?.id,width:size.width,height:size.height,style:{width:size.width,height:size.height},data:{...decorate(n),borderColor:diagramColor(n.border_color,'stroke'),textColor:diagramColor(n.text_color,'text')}};});
  for(const i of value.intersections||[]){const a=byId.get(i.a),b=byId.get(i.b),rect=a&&b?overlapRect(a,b):null;let virtualId='intersection_'+i.id;while(byId.has(virtualId))virtualId='_'+virtualId;if(rect)nodes.push({id:virtualId,type:'intersection',position:{x:rect.x,y:rect.y},width:rect.width,height:rect.height,style:{width:rect.width,height:rect.height,pointerEvents:'none'},zIndex:2000,draggable:false,selectable:false,connectable:false,data:{label:i.label||`${a!.label} ∩ ${b!.label}`}});}
  return nodes;
}
function buildEdges(value:ConceptDiagram,toggle?:NonNullable<Link['data']>['toggle'],ja?:boolean):Link[]{return value.edges.map(e=>({...e,type:'conceptLink',sourceHandle:e.source_handle||'right',targetHandle:e.target_handle||'left',markerStart:e.arrow_start?{type:MarkerType.ArrowClosed,color:diagramColor(e.line_color,'stroke'),width:25,height:25}:undefined,markerEnd:e.arrow_end?{type:MarkerType.ArrowClosed,color:diagramColor(e.line_color,'stroke'),width:25,height:25}:undefined,style:{stroke:diagramColor(e.line_color,'stroke'),strokeWidth:2},data:{toggle,ja,textColor:diagramColor(e.text_color,'text')}}));}

function Editor({value,onChange,onClose,disabled}:{value:ConceptDiagram;onChange:(value:ConceptDiagram|null)=>void;onClose:()=>void;disabled:boolean}){
  const {language}=useLanguage(),ja=language==='ja',flow=useReactFlow();
  const [selection,setSelection]=useState({nodes:[] as string[],edges:[] as string[]}),[error,setError]=useState(''),[confirmDiscard,setConfirmDiscard]=useState(false),[,refreshHistory]=useState(0);
  const [measured,recordMeasurements]=useNodeMeasurements();
  const [menu,setMenu]=useState<{node:string;x:number;y:number}|null>(null);
  const [fullscreen,setFullscreen]=useState(false),[paintTarget,setPaintTarget]=useState<'border'|'line'|'text'>('border');
  const editorRef=useRef<HTMLElement>(null),canvasRef=useRef<HTMLDivElement>(null),fullscreenButton=useRef<HTMLButtonElement>(null),moreRef=useRef<HTMLDetailsElement>(null);
  useEffect(()=>{
    if(!fullscreen)return;const root=document.getElementById('root'),wasInert=root?.inert,overflow=document.body.style.overflow;
    if(root)root.inert=true;document.body.style.overflow='hidden';
    const frame=requestAnimationFrame(()=>fullscreenButton.current?.focus());
    return()=>{cancelAnimationFrame(frame);if(root)root.inert=Boolean(wasInert);document.body.style.overflow=overflow;requestAnimationFrame(()=>fullscreenButton.current?.focus())};
  },[fullscreen]);

  const undo=useRef<ConceptDiagram[]>([]),redo=useRef<ConceptDiagram[]>([]),current=useRef(value);current.current=value;
  const remember=()=>{undo.current=[...undo.current.slice(-49),current.current];redo.current=[];refreshHistory(v=>v+1);};
  const update=(next:ConceptDiagram,save=true)=>{const problem=diagramError(next,true);if(problem){setError(ja?'サイズ・要素数・関係・32KBの上限を確認してください。':'Check size, element count, relationships and the 32KB limit.');return;}if(save)remember();setError('');current.current=next;onChange(next);};
  const finish=()=>update(reconcileGeometry(upgradeDiagram(current.current)),false);
  useEffect(()=>{const frame=requestAnimationFrame(()=>void flow.fitView({padding:.25,maxZoom:1}));return()=>cancelAnimationFrame(frame)},[value.nodes.length,flow,fullscreen]);
  useEffect(()=>{
    const canvas=canvasRef.current;if(!canvas||typeof ResizeObserver==='undefined')return;
    let width=canvas.clientWidth,frame=0;
    const observer=new ResizeObserver(()=>{const next=canvas.clientWidth;if(next===width)return;width=next;cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>void flow.fitView({padding:.25,maxZoom:1}));});
    observer.observe(canvas);return()=>{observer.disconnect();cancelAnimationFrame(frame)};
  },[flow,fullscreen]);
  useEffect(()=>{if(!menu)return;const close=()=>setMenu(null);window.addEventListener('pointerdown',close);const key=(event:KeyboardEvent)=>{if(event.key==='Escape')close()};window.addEventListener('keydown',key);return()=>{window.removeEventListener('pointerdown',close);window.removeEventListener('keydown',key)}},[menu]);
  const select=useCallback(({nodes,edges}:{nodes:{id:string;type?:string}[];edges:{id:string}[]})=>{const next={nodes:nodes.filter(n=>n.type==='concept').map(n=>n.id),edges:edges.map(e=>e.id)};setSelection(old=>old.nodes.join(',')===next.nodes.join(',')&&old.edges.join(',')===next.edges.join(',')?old:next)},[]);
  const toggle=(id:string,end:'start'|'end')=>{if(disabled)return;const key=end==='start'?'arrow_start':'arrow_end',next=upgradeDiagram(current.current);update({...next,edges:next.edges.map(e=>e.id===id?{...e,[key]:!e[key]}:e)});};
  const nodes=buildNodes(value,n=>{const children=value.nodes.filter(c=>c.parent_id===n.id);return {label:n.label,container:children.length>0,venn:value.intersections?.some(i=>i.a===n.id||i.b===n.id),disabled,edit:(id,text)=>{const next=upgradeDiagram(current.current);update({...next,nodes:next.nodes.map(n=>n.id===id?{...n,label:text}:n)})},begin:remember,end:finish,minWidth:Math.max(100,...children.map(c=>c.position.x+boxSize(c).width-n.position.x)),minHeight:Math.max(60,...children.map(c=>c.position.y+boxSize(c).height-n.position.y)),ariaLabel:ja?'ボックスの文字':'Box text'};}).map(n=>({...n,measured:measured[n.id],selected:selection.nodes.includes(n.id)}));
  const edges=buildEdges(value,disabled?undefined:toggle,ja).map(e=>({...e,selected:selection.edges.includes(e.id)}));
  const chosen=value.nodes.find(n=>n.id===selection.nodes[0]);
  const chosenEdge=value.edges.find(edge=>edge.id===selection.edges[0]);
  const hasNodes=selection.nodes.length>0,hasEdges=selection.edges.length>0;
  const colorTarget=paintTarget==='border'&&!hasNodes?'line':paintTarget==='line'&&!hasEdges?'border':paintTarget;
  const targetLabel=colorTarget==='border'?(ja?'枠':'Border'):colorTarget==='line'?(ja?'線':'Line'):(ja?'文字':'Text');
  const selectedColors=[...(colorTarget!=='line'?value.nodes.filter(n=>selection.nodes.includes(n.id)).map(n=>(colorTarget==='text'?n.text_color:n.border_color)||'black'):[]),...(colorTarget!=='border'?value.edges.filter(e=>selection.edges.includes(e.id)).map(e=>(colorTarget==='text'?e.text_color:e.line_color)||'black'):[])];
  const paint=(color:DiagramColor)=>{
    if(disabled||selectedColors.every(value=>value===color))return;const next=upgradeDiagram(current.current);
    update({...next,nodes:next.nodes.map(node=>selection.nodes.includes(node.id)&&colorTarget!=='line'?{...node,[colorTarget==='text'?'text_color':'border_color']:color}:node),edges:next.edges.map(edge=>selection.edges.includes(edge.id)&&colorTarget!=='border'?{...edge,[colorTarget==='text'?'text_color':'line_color']:color}:edge)});
  };
  const closeMore=()=>moreRef.current?.removeAttribute('open');

  const candidates=(node:DiagramNode)=>value.nodes.filter(other=>other.id!==node.id&&partiallyOverlaps(node,other));
  const partner=chosen&&(selection.nodes.length===2?value.nodes.find(n=>n.id===selection.nodes[1]&&partiallyOverlaps(chosen,n)):candidates(chosen)[0]);
  const venn=(a:string,b:string)=>{const next=upgradeDiagram(current.current),exists=next.intersections!.some(i=>[i.a,i.b].sort().join(',')===[a,b].sort().join(','));if(!exists)update({...next,intersections:[...next.intersections!,{id:crypto.randomUUID(),a,b,label:''}]});setMenu(null);};
  const remove=()=>{let next=upgradeDiagram(current.current);next={...next,nodes:next.nodes.filter(n=>!selection.nodes.includes(n.id)).map(n=>({...n,parent_id:n.parent_id&&selection.nodes.includes(n.parent_id)?null:n.parent_id})),edges:next.edges.filter(e=>!selection.edges.includes(e.id)&&!selection.nodes.includes(e.source)&&!selection.nodes.includes(e.target)),intersections:next.intersections!.filter(i=>!selection.nodes.includes(i.a)&&!selection.nodes.includes(i.b))};update(next);setSelection({nodes:[],edges:[]});};
  const travel=(source:React.RefObject<ConceptDiagram[]>,target:React.RefObject<ConceptDiagram[]>)=>{const next=source.current.pop();if(next){target.current.push(current.current);onChange(next);refreshHistory(v=>v+1);setError('');}};
  const add=(inside=false)=>{const next=upgradeDiagram(current.current);let point={x:(next.nodes.length%3)*240,y:Math.floor(next.nodes.length/3)*140};let parent:DiagramNode|undefined;if(inside&&chosen){parent={...chosen,size:{width:Math.max(boxSize(chosen).width,380),height:Math.max(boxSize(chosen).height,240)}};next.nodes=next.nodes.map(n=>n.id===parent!.id?parent!:n);point={x:parent.position.x+24,y:parent.position.y+48};}const id=crypto.randomUUID();update({...next,nodes:[...next.nodes,{id,label:ja?'新しい概念':'New concept',position:point,size:DEFAULT_BOX_SIZE,parent_id:parent?.id||null}]});setSelection({nodes:[id],edges:[]});};
  const panel=<section ref={editorRef} className={`concept-diagram-editor${fullscreen?' is-fullscreen':''}`} role={fullscreen?'dialog':undefined} aria-modal={fullscreen?true:undefined} aria-label={ja?'概念図エディター':'Concept diagram editor'} onKeyDownCapture={event=>{
    if(event.key==='Escape'&&!event.nativeEvent.isComposing){
      if(menu){setMenu(null);event.preventDefault();event.stopPropagation();return}
      const details=editorRef.current?.querySelector<HTMLDetailsElement>('details[open]');if(details){details.open=false;event.preventDefault();event.stopPropagation();return}
      if(fullscreen){setFullscreen(false);event.preventDefault();event.stopPropagation();}
    }
    if(fullscreen&&event.key==='Tab'){
      const items=Array.from(editorRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),summary,[tabindex="0"]')||[]).filter(item=>item.getClientRects().length>0);
      const first=items[0],last=items.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}
    }
  }}>
    <header className="concept-heading"><strong>{ja?'概念図':'Concept diagram'}</strong><div className="concept-window-actions"><button ref={fullscreenButton} type="button" aria-label={fullscreen?(ja?'通常表示に戻す':'Return to inline view'):(ja?'全画面で編集':'Edit full screen')} title={fullscreen?(ja?'通常表示に戻す（Esc）':'Return to inline view (Esc)'):(ja?'全画面で編集':'Edit full screen')} onClick={()=>setFullscreen(value=>!value)}><UiIcon name={fullscreen?'collapse':'expand'} size={18}/>{fullscreen&&<span>{ja?'通常表示に戻す':'Inline view'}</span>}</button><button type="button" className="concept-close" aria-label={ja?'概念図を閉じる':'Close concept diagram'} title={ja?'概念図を閉じる':'Close concept diagram'} onClick={onClose}><UiIcon name="close" size={18}/></button></div></header>
    <div className="concept-toolbar" role="toolbar" aria-label={ja?'概念図の操作':'Diagram actions'}>
      <button type="button" className="concept-add" aria-label={ja?'＋ ボックス':'＋ Box'} disabled={disabled||value.nodes.length>=100} onClick={()=>add()}><UiIcon name="plus" size={16}/>{ja?'ボックス':'Box'}</button>
      <div className="concept-tool-group"><button type="button" aria-label={ja?'元に戻す':'Undo'} title={ja?'元に戻す':'Undo'} disabled={disabled||!undo.current.length} onClick={()=>travel(undo,redo)}><UiIcon name="undo" size={16}/></button><button type="button" aria-label={ja?'やり直す':'Redo'} title={ja?'やり直す':'Redo'} disabled={disabled||!redo.current.length} onClick={()=>travel(redo,undo)}><UiIcon name="redo" size={16}/></button></div>
      <button type="button" aria-label={ja?'選択を削除':'Delete selected'} title={ja?'選択を削除':'Delete selected'} disabled={disabled||!hasNodes&&!hasEdges} onClick={remove}><UiIcon name="trash" size={16}/></button>
      <div className="concept-view-actions"><button type="button" aria-label={ja?'全体表示':'Fit view'} title={ja?'全体表示':'Fit view'} onClick={()=>void flow.fitView({padding:.25,maxZoom:1})}><UiIcon name="fit" size={16}/></button>
      <details ref={moreRef} className="concept-more"><summary aria-label={ja?'その他の概念図操作':'More diagram actions'} title={ja?'その他の操作':'More actions'}><UiIcon name="more" size={18}/></summary><div className="concept-more-menu">
        <button type="button" disabled={disabled||!chosen||value.nodes.length>=100} onClick={()=>{closeMore();add(true)}}>{ja?'＋ 下位概念':'＋ Subconcept'}</button>
        <button type="button" disabled={disabled||!partner} onClick={()=>{closeMore();if(chosen&&partner)venn(chosen.id,partner.id)}}>{ja?'重なりをベン図にする':'Make overlap a Venn relationship'}</button>
        <button type="button" disabled={disabled||!chosen||!value.intersections?.some(i=>i.a===chosen.id||i.b===chosen.id)} onClick={()=>{closeMore();update({...upgradeDiagram(current.current),intersections:current.current.intersections?.filter(i=>i.a!==chosen?.id&&i.b!==chosen?.id)})}}>{ja?'ベン図を解除':'Remove Venn relationship'}</button>
        <p>{ja?'内側に置くと下位概念になります。重なりは任意でベン図にできます。線端の右クリック、または線の選択で矢印を切り替えます。':'Drop inside to nest. Overlaps can become Venn relationships. Right-click a line end or select a line to change arrows.'}</p>
        <button type="button" className="concept-discard" disabled={disabled} onClick={()=>{closeMore();setConfirmDiscard(true)}}>{ja?'破棄':'Discard'}</button>
      </div></details></div>
    </div>
    {(hasNodes||hasEdges)&&<div className="concept-inspector" aria-label={ja?'選択の書式':'Selection formatting'}><label className="concept-color-target">{ja?'色':'Color'}<select aria-label={ja?'色を変える対象':'Color target'} value={colorTarget} disabled={disabled} onChange={event=>setPaintTarget(event.target.value as typeof paintTarget)}>{hasNodes&&<option value="border">{ja?'枠':'Border'}</option>}{hasEdges&&<option value="line">{ja?'線':'Line'}</option>}<option value="text">{ja?'文字':'Text'}</option></select></label><div className="concept-color-swatches" role="group" aria-label={ja?`${targetLabel}の色`:`${targetLabel} color`}>{(Object.keys(DIAGRAM_COLORS) as DiagramColor[]).map(color=><button type="button" key={color} data-color={color} className="concept-color-swatch" style={{'--swatch-color':DIAGRAM_COLORS[color].stroke} as CSSProperties} disabled={disabled} aria-label={ja?`${targetLabel}の色: ${DIAGRAM_COLORS[color].ja}`:`${targetLabel} color: ${DIAGRAM_COLORS[color].en}`} title={ja?DIAGRAM_COLORS[color].ja:DIAGRAM_COLORS[color].en} aria-pressed={selectedColors.length>0&&selectedColors.every(value=>value===color)} onClick={()=>paint(color)}>{selectedColors.length>0&&selectedColors.every(value=>value===color)&&<UiIcon name="check" size={13}/>}</button>)}</div>
      {chosen&&<details className="concept-size-menu"><summary>{ja?'サイズ':'Size'}<UiIcon name="chevron" size={14}/></summary>
    {chosen&&<div className="concept-size-fields"><span>{ja?'選択ボックス':'Selected box'}</span>{(['width','height'] as const).map(axis=><label key={axis}>{ja?(axis==='width'?'幅':'高さ'):axis}<input key={`${chosen.id}-${axis}-${boxSize(chosen)[axis]}`} type="number" min={axis==='width'?100:60} max={axis==='width'?2400:1600} disabled={disabled} defaultValue={boxSize(chosen)[axis]} onKeyDown={e=>{e.stopPropagation();if(e.key==='Enter'){e.preventDefault();e.currentTarget.blur();}}} onBlur={e=>{const number=Number(e.target.value);if(!Number.isFinite(number))return;const next=upgradeDiagram(current.current),resized={...chosen,size:{...boxSize(chosen),[axis]:number}};if(next.nodes.some(n=>n.parent_id===chosen.id&&!containsBox(resized,n))){setError(ja?'下位概念が収まるサイズを指定してください。':'Keep subconcepts inside their parent.');return;}update(reconcileGeometry({...next,nodes:next.nodes.map(n=>n.id===chosen.id?resized:n)}));}}/></label>)}<small>{ja?'枠の点・辺をドラッグして縦横を個別に変更':'Drag frame handles to resize each axis'}</small></div>}
      </details>}
      {chosenEdge&&<div className="concept-edge-options"><label>{ja?'線の文字':'Line label'}<input aria-label={ja?'線の文字':'Line label'} maxLength={500} value={chosenEdge.label} disabled={disabled} onChange={event=>{const next=upgradeDiagram(current.current);update({...next,edges:next.edges.map(edge=>edge.id===chosenEdge.id?{...edge,label:event.target.value}:edge)})}}/></label>{(['start','end'] as const).map(end=><button type="button" key={end} disabled={disabled} aria-pressed={Boolean(end==='start'?chosenEdge.arrow_start:chosenEdge.arrow_end)} onClick={()=>toggle(chosenEdge.id,end)}>{ja?(end==='start'?'始点の矢印':'終点の矢印'):(end==='start'?'Start arrow':'End arrow')}</button>)}</div>}
    </div>}
    {confirmDiscard&&<div className="concept-confirm" role="alert">{ja?'ドラフトを破棄しますか？':'Discard draft?'}<button type="button" onClick={()=>{onChange(null);onClose()}}>{ja?'破棄する':'Discard draft'}</button><button type="button" onClick={()=>setConfirmDiscard(false)}>{ja?'戻る':'Keep draft'}</button></div>}
    <div ref={canvasRef} className="concept-canvas"><ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} connectionMode={ConnectionMode.Loose} fitView minZoom={.1} maxZoom={2} fitViewOptions={{padding:.25,maxZoom:1}} nodesDraggable={!disabled} nodesConnectable={!disabled} elementsSelectable={!disabled} deleteKeyCode={disabled?null:['Backspace','Delete']} onNodeDragStart={remember} onNodeDragStop={finish} onNodeContextMenu={(event,node)=>{if(disabled||node.type!=='concept')return;event.preventDefault();setMenu({node:node.id,x:Math.min(event.clientX,window.innerWidth-250),y:Math.min(event.clientY,window.innerHeight-200)})}} onPaneClick={()=>setMenu(null)} onSelectionChange={select} onNodesChange={changes=>{
      recordMeasurements(changes);if(disabled)return;const selections=changes.filter(c=>c.type==='select');if(selections.length)setSelection(old=>({...old,nodes:selections.reduce((ids,c)=>c.selected?[...ids.filter(id=>id!==c.id),c.id]:ids.filter(id=>id!==c.id),old.nodes)}));const content=changes.filter(c=>c.type==='position'||c.type==='remove'||c.type==='dimensions'&&(c.resizing!==undefined||c.setAttributes));if(!content.length)return;
      const updated=applyNodeChanges(content,nodes),byId=new Map(updated.map(n=>[n.id,n]));
      const absolute=(id:string):{x:number;y:number}=>{const n=byId.get(id)!;if(!n.parentId)return n.position;if(!byId.has(n.parentId))return current.current.nodes.find(row=>row.id===id)?.position||n.position;const p=absolute(n.parentId);return {x:p.x+n.position.x,y:p.y+n.position.y}};
      let next=upgradeDiagram(current.current);next={...next,nodes:next.nodes.filter(n=>byId.has(n.id)).map(n=>{const rf=byId.get(n.id)!;return {...n,position:absolute(n.id),size:{width:rf.width||boxSize(n).width,height:rf.height||boxSize(n).height},parent_id:n.parent_id&&byId.has(n.parent_id)?n.parent_id:null}})};
      const ids=new Set(next.nodes.map(n=>n.id));next.edges=next.edges.filter(e=>ids.has(e.source)&&ids.has(e.target));next.intersections=next.intersections!.filter(i=>ids.has(i.a)&&ids.has(i.b));update(content.some(c=>c.type==='dimensions'&&c.resizing===false)?reconcileGeometry(next):next,content.some(c=>c.type==='remove'));
    }} onEdgesChange={changes=>{if(disabled)return;const selections=changes.filter(c=>c.type==='select');if(selections.length)setSelection(old=>({...old,edges:selections.reduce((ids,c)=>c.selected?[...ids.filter(id=>id!==c.id),c.id]:ids.filter(id=>id!==c.id),old.edges)}));const content=changes.filter(c=>c.type==='remove');if(content.length){const next=upgradeDiagram(current.current),ids=new Set(applyEdgeChanges(content,edges).map(e=>e.id));update({...next,edges:next.edges.filter(e=>ids.has(e.id))});}}} onConnect={c=>{if(disabled||!c.source||!c.target||c.source===c.target)return;const next=upgradeDiagram(current.current);if(!next.edges.some(e=>e.source===c.source&&e.target===c.target&&e.source_handle===c.sourceHandle&&e.target_handle===c.targetHandle))update({...next,edges:[...next.edges,{id:crypto.randomUUID(),source:c.source,target:c.target,label:'',source_handle:c.sourceHandle as DiagramSide,target_handle:c.targetHandle as DiagramSide,arrow_start:false,arrow_end:false}]});}}><Background color="#E9E9E9" gap={20} size={1}/><Controls showInteractive={false}/>{!value.nodes.length&&<div className="concept-empty">{ja?'ボックスを追加し、上下左右の点をつなぐ':'Add boxes and connect their four handles'}</div>}</ReactFlow></div>
    {menu&&value.nodes.some(n=>n.id===menu.node)&&<div className="concept-context-menu" role="menu" style={{left:menu.x,top:menu.y}} onPointerDown={e=>e.stopPropagation()}><strong>{ja?'重なりの扱い':'Overlap options'}</strong>{candidates(value.nodes.find(n=>n.id===menu.node)!).map(other=><button key={other.id} role="menuitem" type="button" onClick={()=>venn(menu.node,other.id)}>{ja?`${other.label} とベン図にする`:`Venn relationship with ${other.label}`}</button>)}<button role="menuitem" type="button" onClick={()=>setMenu(null)}>{ja?'閉じる':'Close'}</button></div>}
    <footer className="concept-footer">{error?<span role="alert">{error}</span>:<span>{ja?'選択すると色・サイズを変更できます':'Select an element to change color or size'}</span>}<small>{value.nodes.length}/100 · {value.edges.length} {ja?'本の線':'connections'}</small></footer>
  </section>;
  return fullscreen?createPortal(<div className="concept-fullscreen-layer">{panel}</div>,document.body):panel;
}

export function ConceptDiagramEditor(props:Parameters<typeof Editor>[0]){return <ReactFlowProvider><Editor {...props}/></ReactFlowProvider>;}
export function ConceptDiagramPreview({diagram,onEdit}:{diagram:ConceptDiagram;onEdit?:(diagram:ConceptDiagram)=>void}){
  const {language}=useLanguage(),ja=language==='ja',valid=!diagramError(diagram);
  const [measured,recordMeasurements]=useNodeMeasurements();
  const nodes=useMemo(()=>valid?buildNodes(diagram,n=>({label:n.label,container:diagram.nodes.some(c=>c.parent_id===n.id),venn:diagram.intersections?.some(i=>i.a===n.id||i.b===n.id)})).map(n=>({...n,measured:measured[n.id]})):[],[diagram,valid,measured]);
  const edges=useMemo(()=>valid?buildEdges(diagram):[],[diagram,valid]);
  if(!valid)return <p role="alert">{ja?'概念図のデータを読み込めません。':'Unable to load this diagram.'}</p>;
  const label=(id:string)=>diagram.nodes.find(n=>n.id===id)?.label||'—';
  return <section className="concept-diagram-preview" aria-label={ja?'概念図プレビュー':'Concept diagram preview'}><strong>{diagram.title||(ja?'概念図':'Concept diagram')}{onEdit&&<button type="button" onClick={()=>onEdit(upgradeDiagram(diagram))}>{ja?'編集して送る':'Edit and send'}</button>}</strong><div className="concept-preview-canvas"><ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} connectionMode={ConnectionMode.Loose} fitView minZoom={.1} onNodesChange={recordMeasurements} nodesDraggable={false} nodesConnectable={false} elementsSelectable={false} deleteKeyCode={null}><Background color="#E9E9E9" gap={20} size={1}/><Controls showInteractive={false}/></ReactFlow></div><details><summary>{ja?'概念と関係を読む':'Read concepts and relationships'}</summary><ul>{diagram.nodes.map(n=><li key={n.id}>{n.label}{n.parent_id&&` ⊂ ${label(n.parent_id)}`}</li>)}{diagram.edges.map(e=><li key={e.id}>{label(e.source)} {e.arrow_start?(e.arrow_end?'↔':'←'):(e.arrow_end?'→':'—')} {label(e.target)}{e.label&&` (${e.label})`}</li>)}{diagram.intersections?.map(i=><li key={i.id}>{label(i.a)} ∩ {label(i.b)}{i.label&&` (${i.label})`}</li>)}</ul></details></section>;
}
