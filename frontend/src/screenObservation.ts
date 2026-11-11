export type DomSemanticElement={id:string;role:string;label:string;visible:boolean;level?:number;enabled?:boolean;expanded?:boolean};
export type DomScreenObservation={dom:{elements:DomSemanticElement[];text:string[];confidence:number;observation_id:string;observed_at:number;session_id:string}};

const selectors='main header .ready, main h1, main h2, main h3, main [role], main button, main summary';

function isVisible(node:HTMLElement):boolean{
 for(let current:HTMLElement|null=node;current;current=current.parentElement){
  if(current.hidden||current.getAttribute('aria-hidden')==='true')return false;
  const style=typeof getComputedStyle==='function'?getComputedStyle(current):null;
  if(style?.display==='none'||style?.visibility==='hidden')return false;
  if(current.tagName==='DETAILS'&&!current.hasAttribute('open')&&!current.querySelector(':scope > summary')?.contains(node))return false;
 }
 return true;
}

/** Capture a small semantic summary of visible OLCR WebView content, never input values or coordinates. */
export function collectVisibleDomObservation(root:ParentNode=document):DomScreenObservation{
 const elements=visibleSemanticElements(root);
 return {dom:{elements:elements.map(item=>item.element),text:elements.map(item=>item.element.label),confidence:1,
  observation_id:globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random()}`,observed_at:Date.now()/1000,
  session_id:location.origin}};
}

export function locateVisibleDomElement(id:string,root:ParentNode=document):HTMLElement|undefined{
 return visibleSemanticElements(root).find(item=>item.element.id===id)?.node;
}

function visibleSemanticElements(root:ParentNode):Array<{node:HTMLElement;element:DomSemanticElement}>{
 const nodes=Array.from(root.querySelectorAll<HTMLElement>(selectors));
 const elements:Array<{node:HTMLElement;element:DomSemanticElement}>=[];
 for(const node of nodes){
  if(!isVisible(node))continue;
  const tag=node.tagName.toLowerCase();
  const role=node.getAttribute('role')||({button:'button',summary:'disclosure',h1:'heading',h2:'heading',h3:'heading'} as Record<string,string>)[tag]||(node.classList.contains('ready')?'status':'element');
  const label=(node.getAttribute('aria-label')||node.innerText||node.textContent||node.getAttribute('title')||'').replace(/\s+/g,' ').trim().slice(0,240);
  if(!label)continue;
  const headingLevel=/^h[1-6]$/.test(tag)?Number(tag.slice(1)):undefined;
  const element:DomSemanticElement={id:`dom-${elements.length}`,role,label,visible:true,...(headingLevel?{level:headingLevel}:{}),...(tag==='button'?{enabled:!(node as HTMLButtonElement).disabled}:{}),...(tag==='summary'?{expanded:node.parentElement?.tagName==='DETAILS'&&(node.parentElement as HTMLDetailsElement).open}:{})};
  elements.push({node,element});
  if(elements.length>=80)break;
 }
 return elements;
}
