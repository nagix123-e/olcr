import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {App} from './main';
import {LanguageProvider} from './language';
import {configureApi} from './api';

vi.mock('./ScreenCapture',()=>({useScreenCapture:()=>({status:{running:false},targetApplication:null}),ScreenCapturePanel:()=>null}));
vi.mock('./ModelUiPreview',()=>({ModelUiPreview:()=>null}));

afterEach(()=>{cleanup();vi.unstubAllGlobals();sessionStorage.clear()});
const json=(value:unknown)=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
const task=(id:string,goal:string)=>({id,conversation_id:'B',original_goal:goal,status:'RESUMABLE',activity:'NONE',
  retry_count:0,max_retries:2,current_phase_id:'p',plan:{phases:[{id:'p',goal,status:'pending'}]}});

it('opens the clicked older task in another conversation instead of its newest task',async()=>{
  const older=task('older','Older selected phase'),newest=task('newest','Newest unrelated phase');
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL)=>{
    const path=String(input).replace('http://fixture','');
    if(path==='/health'||path==='/settings')return json({});
    if(path==='/commands')return json({commands:[]});
    if(path==='/projects')return json({projects:[{id:'p',name:'Project fixture',workspace_path:'/fixture'}]});
    if(path==='/conversations')return json({conversations:[]});
    if(path==='/projects/p/conversations')return json({conversations:[{id:'B',title:'Conversation B'}]});
    if(path==='/projects/p/core-context')return json({content:''});
    if(path==='/projects/p/conversations/B')return json({id:'B',messages:[]});
    if(path==='/conversations/B/coding-tasks')return json({tasks:[newest,older]});
    if(path==='/coding-tasks/older')return json(older);
    if(path==='/coding-tasks/newest')return json(newest);
    if(path.endsWith('/phase-reports'))return json({reports:[]});
    if(path.endsWith('/review'))return json({available:false});
    if(path.endsWith('/steering'))return json({instructions:[]});
    if(path==='/permissions')return json({level:'automatic'});
    if(path==='/brain/models')return json({providers:[],models:[]});
    if(path.startsWith('/brain/configuration'))return json({mode:'unified',
      selections:{brain:'fixture',coder:'fixture',short_coding:'fixture',general_inference:'fixture'},
      efforts:{brain:null,coder:null,short_coding:null,general_inference:null},fallback_models:[]});
    if(path==='/brain/usage')return json({models:{},week_starts_at:0});
    return json({});
  }));
  configureApi('http://fixture','fixture');
  render(<LanguageProvider><App/></LanguageProvider>);
  fireEvent.click((await screen.findByText('Project fixture')).closest('button')!);
  await screen.findByRole('button',{name:'Conversation B'});
  await act(async()=>{window.dispatchEvent(new CustomEvent('olcr-open-coding-conversation',
    {detail:{conversationId:'B',taskId:'older',title:'Conversation B'}}))});
  await waitFor(()=>expect(screen.getByLabelText(/Current task|現在のタスク/)).toHaveTextContent('Older selected phase'));
  expect(screen.queryByText('Newest unrelated phase')).toBeNull();
});
