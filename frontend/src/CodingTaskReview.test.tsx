import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
const request=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:request}));
import {CodingTaskReview} from './CodingTaskReview';
import {CodingWorkControls} from './CodingWorkControls';
import type {CodingTask} from './CodingTaskProgress';

const task:CodingTask={id:'a',status:'RESUMABLE',activity:'NONE',plan_revision:3,retry_count:0,max_retries:2};
const data={available:true,revision:'review-1',workspace:{mode:'worktree',root:'/worktree',source_root:'/source',branch:'olcr/task-a'},
 files:[{path:'src/a.ts',status:'MODIFIED',diff:'--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-before\n+after\n',reviewable:true,revertible:true,phase_ids:['p1']}],steering:[],reports:[]};
beforeEach(()=>{request.mockReset();request.mockImplementation(async path=>String(path).endsWith('/review')?structuredClone(data):{status:'OK'});localStorage.setItem('olcr-language','ja')});
afterEach(()=>{cleanup();delete document.documentElement.dataset.olcrActiveProjectId;vi.useRealTimers()});

describe('Coding task review workflow',()=>{
 it('moves file instructions into the main composer without another textarea',async()=>{
  const compose=vi.fn();window.addEventListener('olcr-compose-coding-instruction',compose);
  render(<CodingTaskReview task={task} ja/>);
  await screen.findByRole('region',{name:'src/a.ts diff'});
  fireEvent.click(screen.getByRole('button',{name:'このファイルについて指示'}));
  expect(compose).toHaveBeenCalledOnce();
  expect((compose.mock.calls[0][0] as CustomEvent).detail).toEqual({taskId:'a',text:'src/a.ts: '});
  expect(screen.queryByRole('textbox')).toBeNull();
  window.removeEventListener('olcr-compose-coding-instruction',compose);
 });
 it('freezes the reviewed revision before a confirmed restore',async()=>{
  render(<CodingTaskReview task={task} ja/>);
  await screen.findByRole('region',{name:'src/a.ts diff'});
  fireEvent.click(screen.getByRole('button',{name:'開始前に戻す'}));
  request.mockImplementation(async path=>String(path).endsWith('/review')?{...data,revision:'review-2'}:{status:'OK'});
  fireEvent.click(screen.getByRole('button',{name:'更新'}));
  await waitFor(()=>expect(request.mock.calls.filter(([path])=>path.endsWith('/review')).length).toBeGreaterThan(1));
  fireEvent.click(screen.getByRole('button',{name:'確認して実行'}));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/coding-tasks/a/changes/revert',expect.objectContaining({body:JSON.stringify({path:'src/a.ts',revision:'review-1'})})));
 });
 it('requires selected files and confirmation before applying to source',async()=>{
  render(<CodingTaskReview task={task} ja/>);
  await screen.findByRole('region',{name:'src/a.ts diff'});
  fireEvent.click(screen.getByLabelText('取り込む src/a.ts'));
  fireEvent.click(screen.getByRole('button',{name:'1ファイルを元のWorkspaceへ取り込む'}));
  expect(request.mock.calls.some(([path])=>path.endsWith('/changes/apply'))).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:'確認して実行'}));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/coding-tasks/a/changes/apply',expect.objectContaining({body:JSON.stringify({paths:['src/a.ts'],revision:'review-1'})})));
 });
 it('keeps subsequent user edits protected while still displaying the diff',async()=>{
  request.mockResolvedValue({...data,files:[{...data.files[0],externally_modified:true,revertible:false}]});
  render(<CodingTaskReview task={task} ja/>);
  await screen.findByText('Taskの実行後にこのファイルが編集されています。内容の復元は行えません。');
  expect((screen.getByRole('button',{name:'開始前に戻す'}) as HTMLButtonElement).disabled).toBe(true);
 });
 it('previews cumulative source changes and retains the confirmed diff across refreshes',async()=>{
  const inherited={...data.files[0],inherited:true,revertible:false,source_reviewable:true,source_diff:'--- source/src/a.ts\n+++ worktree/src/a.ts\n@@ -1 +1 @@\n-original\n+cumulative\n'};
  request.mockResolvedValue({...data,files:[inherited]});
  render(<CodingTaskReview task={task} ja/>);
  await screen.findByText(/前の作業/);
  fireEvent.click(screen.getByLabelText('取り込む src/a.ts'));
  fireEvent.click(screen.getByRole('button',{name:'1ファイルを元のWorkspaceへ取り込む'}));
  const preview=await screen.findByRole('region',{name:'src/a.ts source diff'});
  expect(preview.textContent).toContain('cumulative');
  request.mockResolvedValue({...data,revision:'review-2',files:[{...inherited,source_diff:'+later'}]});
  fireEvent.click(screen.getByRole('button',{name:'更新'}));
  await waitFor(()=>expect(request.mock.calls.filter(([path])=>path.endsWith('/review')).length).toBeGreaterThan(1));
  expect(preview.textContent).toContain('cumulative');
  fireEvent.click(screen.getByRole('button',{name:'確認して実行'}));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/coding-tasks/a/changes/apply',expect.objectContaining({body:JSON.stringify({paths:['src/a.ts'],revision:'review-1'})})));
 });
 it('ignores a delayed review response when another task is selected',async()=>{
  let resolve!:(value:unknown)=>void;
  request.mockImplementation(path=>path==='/coding-tasks/a/review'?new Promise(value=>{resolve=value}):Promise.resolve({...data,files:[{...data.files[0],path:'b.ts'}]}));
  const view=render(<CodingTaskReview task={task} ja/>);
  view.rerender(<CodingTaskReview task={{...task,id:'b'}} ja/>);
  await screen.findByRole('region',{name:'b.ts diff'});
  await act(async()=>resolve(data));
  expect(screen.queryByRole('region',{name:'src/a.ts diff'})).toBeNull();
 });
 it('persists the next-task worktree preference and exposes other project tasks',async()=>{
  document.documentElement.dataset.olcrActiveProjectId='p';
  request.mockImplementation(async path=>path==='/projects/p/coding-workspace'?{mode:'worktree'}:path==='/projects/p/coding-work'?{tasks:[{id:'a',conversation_id:'c',original_goal:'First task',status:'RUNNING'},{id:'b',conversation_id:'d',original_goal:'Second task',status:'COMPLETED'}]}:{tasks:[]});
  const open=vi.fn();window.addEventListener('olcr-open-coding-conversation',open);
  render(<CodingWorkControls conversationId="c" selectedId="a" onSelect={()=>{}} ja/>);
  const select=await screen.findByLabelText('新しいTaskの作業場所');
  await waitFor(()=>expect((select as HTMLSelectElement).value).toBe('worktree'));
  fireEvent.change(select,{target:{value:'direct'}});
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/projects/p/coding-workspace',expect.objectContaining({method:'PUT',body:'{"mode":"direct"}'})));
  fireEvent.click(await screen.findByRole('button',{name:'COMPLETED · Second task'}));
  expect(open).toHaveBeenCalledOnce();
  expect((open.mock.calls[0][0] as CustomEvent).detail).toEqual({conversationId:'d',taskId:'b',title:'Second task'});
  window.removeEventListener('olcr-open-coding-conversation',open);
 });
 it('uses the current project prop while the legacy DOM project marker is stale',async()=>{
  document.documentElement.dataset.olcrActiveProjectId='old';
  request.mockImplementation(async path=>path.endsWith('/coding-workspace')?{mode:'direct'}:{tasks:[]});
  const view=render(<CodingWorkControls projectId="new" selectedId={null} onSelect={()=>{}} ja/>);
  await waitFor(()=>expect(request).toHaveBeenCalledWith('/projects/new/coding-workspace'));
  expect(request.mock.calls.some(([path])=>path.startsWith('/projects/old/'))).toBe(false);
  view.rerender(<CodingWorkControls projectId={null} selectedId={null} onSelect={()=>{}} ja/>);
  expect(screen.queryByLabelText('新しいTaskの作業場所')).toBeNull();
 });
});
