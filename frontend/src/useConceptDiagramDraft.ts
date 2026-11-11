import {useMemo, useState} from 'react';
import {ConceptDiagram, diagramError} from './conceptDiagramData';
export function useConceptDiagramDraft(scope: string) {
  const key = 'olcr-concept-draft-v1:' + scope;
  const restored = useMemo(() => {
    try {const raw=sessionStorage.getItem(key);if(!raw)return null;const value=JSON.parse(raw) as ConceptDiagram;return diagramError(value,true)?null:value;} catch {return null;}
  }, [key]);
  const [drafts, setDrafts] = useState<Record<string, ConceptDiagram | null>>({});
  const value = Object.prototype.hasOwnProperty.call(drafts,key) ? drafts[key] : restored;
  const updateScope = (targetScope:string,next: ConceptDiagram | null) => {const targetKey='olcr-concept-draft-v1:'+targetScope;setDrafts(previous=>({...previous,[targetKey]:next}));try{if(next)sessionStorage.setItem(targetKey,JSON.stringify(next));else sessionStorage.removeItem(targetKey);}catch{/* Draft remains available in memory when storage is full. */}};
  const update = (next: ConceptDiagram | null) => updateScope(scope,next);
  return [value,update,updateScope] as const;
}
