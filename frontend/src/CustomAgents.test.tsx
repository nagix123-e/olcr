import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {CustomAgents} from './CustomAgents';
import {LanguageProvider} from './language';
import {saveExportApi} from './api';
vi.mock('./api',()=>({saveExportApi:vi.fn().mockResolvedValue(undefined)}));

afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals()});
const stored={id:'agent-one',revision:3,archived:false,name:'My agent',description:'Helper',icon:'✦',instructions:'Help me.',preferred_router:'normal_chat',preferred_model:'ollama:local-model',knowledge:[],tools:[],skills:[],subagent_ids:[]};
const models=['ollama:local-model','api:openai_oauth:gpt-6-luna'];

describe('agent icon images',()=>{
  const converted='data:image/webp;base64,UklGRg==';
  function imageMocks(reject=false){
    let source='',unreadable=reject;
    vi.stubGlobal('Image',class {
      naturalWidth=1024;naturalHeight=512;
      get src(){return source} set src(value:string){source=value}
      decode(){return unreadable?Promise.reject(Error('invalid image')):Promise.resolve()}
    });
    const drawImage=vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue({drawImage} as any);
    vi.spyOn(HTMLCanvasElement.prototype,'toDataURL').mockReturnValue(converted);
    const objectURL=vi.fn(()=>{throw Error('blob: is not allowed by desktop CSP')});
    vi.stubGlobal('URL',class extends URL{static createObjectURL=objectURL});
    return {source:()=>source,drawImage,objectURL,allowRead:()=>{unreadable=false}};
  }
  async function edit(){
    let saved:any;
    const api=vi.fn(async(path:string,init?:RequestInit):Promise<any>=>{
      if(path==='/agents')return {agents:[saved?{...stored,...saved}:stored]};
      if(path==='/agents/options')return {tools:[],skills:[],models:[],runtime_available:true};
      if(path==='/agents/agent-one'&&init?.method==='PUT'){saved=JSON.parse(String(init.body));return {...stored,...saved}}
      throw Error(path);
    });
    const view=render(<LanguageProvider><CustomAgents api={api} onUse={vi.fn()} onClose={vi.fn()}/></LanguageProvider>);
    fireEvent.click(await screen.findByRole('button',{name:/^(Edit|編集)$/}));
    return {...view,api,saved:()=>saved};
  }
  it.each(['picker','drop'])('loads, previews and saves an icon from %s using CSP-permitted data URLs',async method=>{
    const {source,drawImage,objectURL}=imageMocks();const view=await edit();
    const image=new File([new Uint8Array([137,80,78,71])],'icon.png',{type:'image/png'});
    if(method==='picker')fireEvent.change(screen.getByLabelText(/^(Choose icon image|アイコン画像を選択)$/),{target:{files:[image]}});
    else fireEvent.drop(view.container.querySelector('.agent-icon-preview')!,{dataTransfer:{files:[image]}});
    await waitFor(()=>expect(view.container.querySelector('.agent-icon-preview img')).toHaveAttribute('src',converted));
    expect(source()).toMatch(/^data:image\/png;base64,/);expect(objectURL).not.toHaveBeenCalled();
    expect(drawImage).toHaveBeenCalledWith(expect.anything(),0,0,256,128);
    fireEvent.click(screen.getByRole('button',{name:/^(Save|保存)$/}));
    await waitFor(()=>expect(view.saved()?.icon).toBe(converted));
    expect(await screen.findByRole('button',{name:/^(Edit|編集)$/})).toBeEnabled();
    expect(view.container.querySelector('.agent-card img')).toHaveAttribute('src',converted);
  });
  it('keeps the previous icon and allows retrying after an unreadable image',async()=>{
    const image=imageMocks(true);const view=await edit();
    fireEvent.change(screen.getByLabelText(/^(Choose icon image|アイコン画像を選択)$/),{target:{files:[new File(['broken'],'broken.png',{type:'image/png'})]}});
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not read the image|画像を読み込めませんでした/);
    expect(view.container.querySelector('.agent-icon-preview')).toHaveTextContent(stored.icon);
    expect(screen.getByRole('button',{name:/^(Save|保存)$/})).toBeEnabled();
    expect(view.saved()).toBeUndefined();
    image.allowRead();
    fireEvent.change(screen.getByLabelText(/^(Choose icon image|アイコン画像を選択)$/),{target:{files:[new File(['retry'],'icon.png',{type:'image/png'})]}});
    await waitFor(()=>expect(view.container.querySelector('.agent-icon-preview img')).toHaveAttribute('src',converted));
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

it('downloads the selected card as a single portable agent file',async()=>{
  const api=vi.fn(async(path:string):Promise<any>=>path==='/agents'?{agents:[stored]}:{tools:[],skills:[],models:[],runtime_available:true});
  render(<LanguageProvider><CustomAgents api={api} onUse={vi.fn()} onClose={vi.fn()}/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:/^(Download|ダウンロード)$/}));
  await waitFor(()=>expect(saveExportApi).toHaveBeenCalledWith('/agents/agent-one/export','My agent.olcr-agent.zip'));
});

it('creates a new agent from the package and refreshes the cards',async()=>{
  let imported=false;
  const definition=(({id,revision,archived,...fields})=>fields)(stored);
  const bundle={format:'olcr.agent',version:1,root_id:stored.id,agents:[{id:stored.id,definition}]};
  const api=vi.fn(async(path:string,init?:RequestInit):Promise<any>=>{
    if(path==='/agents')return {agents:imported?[{...stored,id:'copy',name:'Imported agent'}]:[]};
    if(path==='/agents/import-zip'){expect(init?.body).toBeInstanceOf(File);expect(init?.headers).toEqual({'Content-Type':'application/zip'});imported=true;return {agent:{...stored,id:'copy',name:'Imported agent'},imported_count:1}}
    return {tools:[],skills:[],models:[],runtime_available:true};
  });
  render(<LanguageProvider><CustomAgents api={api} onUse={vi.fn()} onClose={vi.fn()}/></LanguageProvider>);
  await screen.findByRole('button',{name:/^(Create from file|ファイルから作成)$/});
  const file=new File([new Uint8Array([0x50,0x4b,3,4])],'agent.olcr-agent.zip',{type:'application/zip'});
  Object.defineProperty(file,'text',{value:async()=>JSON.stringify(bundle)});
  fireEvent.change(screen.getByLabelText(/^(Choose agent file|エージェントファイルを選択)$/),{target:{files:[file]}});
  expect(await screen.findByRole('heading',{name:'Imported agent'})).toBeVisible();
});

it('rejects an invalid file without posting an agent definition',async()=>{
  const api=vi.fn(async(path:string):Promise<any>=>{if(path==='/agents/import-zip')throw Error('AGENT_PACKAGE_INVALID_ZIP');return path==='/agents'?{agents:[]}:{tools:[],skills:[],models:[],runtime_available:true}});
  render(<LanguageProvider><CustomAgents api={api} onUse={vi.fn()} onClose={vi.fn()}/></LanguageProvider>);
  await screen.findByRole('button',{name:/^(Create from file|ファイルから作成)$/});
  const file=new File(['bad'],'bad.json');Object.defineProperty(file,'text',{value:async()=>'not JSON'});
  fireEvent.change(screen.getByLabelText(/^(Choose agent file|エージェントファイルを選択)$/),{target:{files:[file]}});
  await screen.findByRole('alert');
  expect(api.mock.calls.some(([path])=>path==='/agents/import-zip')).toBe(true);
});

describe('custom agent editing and use',()=>{
  it.each(models)('saves %s without sending server metadata',async model=>{
    const api=vi.fn(async(path:string,init?:RequestInit)=>{
      if(path==='/agents')return {agents:[{...stored,server_metadata:'read only'}]};
      if(path==='/agents/options')return {tools:[],skills:[],models:models.map(id=>({id,available:true})),runtime_available:true};
      if(path==='/agents/agent-one'&&init?.method==='PUT')return {...stored,...JSON.parse(String(init.body))};
      throw Error(`Unexpected path ${path}`);
    });
    render(<LanguageProvider><CustomAgents api={api} onUse={vi.fn()} onClose={vi.fn()}/></LanguageProvider>);
    fireEvent.click(await screen.findByRole('button',{name:/^(Edit|編集)$/}));
    fireEvent.change(screen.getByLabelText(/^(Model|モデル)$/),{target:{value:model}});
    fireEvent.click(screen.getByRole('button',{name:/^(Save|保存)$/}));
    await waitFor(()=>expect(api.mock.calls.some(([path,init])=>path==='/agents/agent-one'&&init?.method==='PUT')).toBe(true));
    const body=JSON.parse(String(api.mock.calls.find(([path])=>path==='/agents/agent-one')?.[1]?.body));
    expect(body).toEqual({...((({id,revision,archived,...definition})=>definition)(stored)),preferred_model:model});
    expect(body).not.toHaveProperty('archived');expect(body).not.toHaveProperty('server_metadata');
    await screen.findByRole('button',{name:/^(Edit|編集)$/});
    expect(screen.queryByRole('alert')).toBeNull();
  });
  it('allows opening a saved agent before the execution runtime is ready',async()=>{
    const onUse=vi.fn(async()=>{});
    const api=vi.fn(async(path:string):Promise<any>=>path==='/agents'?{agents:[stored]}:{tools:[],skills:[],models:[],runtime_available:false});
    render(<LanguageProvider><CustomAgents api={api} onUse={onUse} onClose={vi.fn()}/></LanguageProvider>);
    const use=await screen.findByRole('button',{name:/^(Use|使う)$/});
    expect(use).toBeEnabled();fireEvent.click(use);await waitFor(()=>expect(onUse).toHaveBeenCalledWith(stored));
  });
});

it('keeps selections through search and collapsing, including an unavailable preferred model',async()=>{
  let saved:any;
  const api=vi.fn(async(path:string,init?:RequestInit):Promise<any>=>{
    if(path==='/agents')return {agents:[{...stored,tools:['web_search']}]};
    if(path==='/agents/options')return {tools:[{id:'web_search',description:'Search pages'},{id:'read_file',description:'Read local text'}],skills:[],models:[],runtime_available:true};
    if(init?.method==='PUT'){saved=JSON.parse(String(init.body));return saved;}
    throw Error(path);
  });
  render(<LanguageProvider><CustomAgents api={api} onUse={vi.fn()} onClose={vi.fn()}/></LanguageProvider>);
  fireEvent.click(await screen.findByRole('button',{name:/^(Edit|編集)$/}));
  expect(screen.getByLabelText(/^(Model|モデル)$/)).toHaveValue(stored.preferred_model);
  const search=screen.getByLabelText(/^(Search tools|ツールを検索)$/);
  const section=search.closest('details')!;
  expect(section).not.toHaveAttribute('open');
  fireEvent.click(section.querySelector('summary')!);
  fireEvent.change(search,{target:{value:'local'}});
  fireEvent.click(screen.getByRole('checkbox',{name:/read_file/}));
  fireEvent.click(section.querySelector('summary')!);
  fireEvent.click(screen.getByRole('button',{name:/^(Save|保存)$/}));
  await waitFor(()=>expect(saved?.tools).toEqual(['web_search','read_file']));
  expect(saved.preferred_model).toBe(stored.preferred_model);
});

it('shows a retry on initial loading failure without opening an incomplete form',async()=>{
  let fail=true;
  const api=vi.fn(async(path:string):Promise<any>=>{
    if(fail)throw Error('Loading failed');
    return path==='/agents'?{agents:[]}:{tools:[],skills:[],models:[],runtime_available:true};
  });
  render(<LanguageProvider><CustomAgents api={api} onUse={vi.fn()} onClose={vi.fn()}/></LanguageProvider>);
  expect(await screen.findByRole('alert')).toHaveTextContent('Loading failed');
  expect(screen.getByRole('button',{name:/^(Create agent|エージェントを作成)$/})).toBeDisabled();
  fail=false;fireEvent.click(screen.getByRole('button',{name:/Retry|再読み込み/}));
  await waitFor(()=>expect(screen.getByRole('button',{name:/^(Create agent|エージェントを作成)$/})).toBeEnabled());
});
