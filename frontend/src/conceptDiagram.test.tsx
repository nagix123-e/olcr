import {act, cleanup, fireEvent, render, renderHook, screen} from '@testing-library/react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {ComposerActions} from './ComposerActions';
import {emptyDiagram, diagramError} from './conceptDiagramData';
import {useConceptDiagramDraft} from './useConceptDiagramDraft';

afterEach(()=>{cleanup();sessionStorage.clear();});
describe('concept diagram composition',()=>{
  it('opens keyboard menu, restores trigger focus and invokes the selected action',()=>{
    const file=vi.fn(), diagram=vi.fn();render(<ComposerActions disabled={false} onFile={file} onDiagram={diagram}/>);
    const button=screen.getByRole('button',{name:'操作を追加'});fireEvent.click(button);
    expect(screen.getByRole('menuitem',{name:'ファイル・フォルダを添付'})).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('menu'),{key:'ArrowDown'});
    expect(screen.getByRole('menuitem',{name:'概念図'})).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('menu'),{key:'Escape'});expect(screen.queryByRole('menu')).toBeNull();expect(button).toHaveFocus();
    fireEvent.click(button);fireEvent.click(screen.getByRole('menuitem',{name:'ファイル・フォルダを添付'}));expect(file).toHaveBeenCalledOnce();
    fireEvent.click(button);fireEvent.click(screen.getByRole('menuitem',{name:'概念図'}));expect(diagram).toHaveBeenCalledOnce();
  });
  it('closes the action menu on outside pointer input',()=>{render(<ComposerActions disabled={false} onFile={()=>{}} onDiagram={()=>{}}/>);fireEvent.click(screen.getByRole('button'));fireEvent.pointerDown(document.body);expect(screen.queryByRole('menu')).toBeNull();});
  it('isolates drafts by conversation, restores a draft after remount, and clears only the sent scope',()=>{
    const {result,rerender,unmount}=renderHook(({scope})=>useConceptDiagramDraft(scope),{initialProps:{scope:'a'}});
    const diagram={...emptyDiagram(),nodes:[{id:'n1',label:'設計',position:{x:0,y:0}}]};
    act(()=>result.current[1](diagram));rerender({scope:'b'});expect(result.current[0]).toBeNull();
    rerender({scope:'a'});expect(result.current[0]).toEqual(diagram);unmount();
    const restored=renderHook(()=>useConceptDiagramDraft('a'));expect(restored.result.current[0]).toEqual(diagram);
    act(()=>restored.result.current[1](null));expect(sessionStorage.getItem('olcr-concept-draft-v1:a')).toBeNull();
  });
  it('rejects dangling connections, empty diagrams, and oversized labels before sending',()=>{
    expect(diagramError(null as any)).toBe('DIAGRAM_SCHEMA_INVALID');expect(diagramError({...emptyDiagram(),nodes:[null]} as any)).toBe('DIAGRAM_NODE_INVALID');
    const diagram={...emptyDiagram(),nodes:[{id:'n1',label:'',position:{x:0,y:0}}]};expect(diagramError(diagram)).toBe('DIAGRAM_EMPTY_LABELS');
    diagram.nodes[0].label='設計';expect(diagramError(diagram)).toBe('');
    diagram.edges=[{id:'e1',source:'n1',target:'missing',label:''}];expect(diagramError(diagram)).toBe('DIAGRAM_EDGE_INVALID');
    diagram.edges=[];diagram.nodes[0].label='x'.repeat(501);expect(diagramError(diagram)).toBe('DIAGRAM_NODE_INVALID');
  });
});

it('preserves per-element colors through upgrades and draft restoration and rejects unsupported values',async()=>{
  const {upgradeDiagram,reconcileGeometry}=await import('./conceptDiagramData');
  const diagram:import('./conceptDiagramData').ConceptDiagram={...emptyDiagram(),nodes:[
    {id:'a',label:'Input',position:{x:0,y:0},border_color:'red',text_color:'blue'},
    {id:'b',label:'Output',position:{x:300,y:0},border_color:'yellow'}
  ],edges:[{id:'ab',source:'a',target:'b',label:'Flow',line_color:'blue',text_color:'red'}]};
  expect(diagramError(diagram)).toBe('');expect(reconcileGeometry(upgradeDiagram(diagram)).nodes[0]).toMatchObject({border_color:'red',text_color:'blue'});
  const draft=renderHook(()=>useConceptDiagramDraft('colored'));act(()=>draft.result.current[1](diagram));draft.unmount();
  const restored=renderHook(()=>useConceptDiagramDraft('colored'));expect(restored.result.current[0]).toEqual(diagram);
  expect(diagramError({...diagram,nodes:[{...diagram.nodes[0],border_color:'url(javascript:bad)'}]} as any)).toBe('DIAGRAM_NODE_INVALID');
  expect(diagramError({...diagram,edges:[{...diagram.edges[0],line_color:'green'}]} as any)).toBe('DIAGRAM_EDGE_INVALID');
  expect(diagramError({...diagram,schema_version:1})).toBe('DIAGRAM_SCHEMA_INVALID');
});
