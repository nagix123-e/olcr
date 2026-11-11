import {cleanup,fireEvent,render,screen,within,waitFor} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {BrainModelPicker} from './BrainModelPicker';
import {ModelsPanel,ModelProviderForm} from './ModelsPanel';
import {BrainState,INITIAL_BRAIN} from './brainModels';
const openExternalMock=vi.hoisted(()=>vi.fn().mockResolvedValue(undefined));
vi.mock('./native',async importOriginal=>({...await importOriginal<typeof import('./native')>(),openExternal:openExternalMock}));
afterEach(cleanup);
function state():BrainState{return {configuration:structuredClone(INITIAL_BRAIN),catalog:{providers:[],models:[{id:'local:brain',label:'Local Brain',provider:'Ollama',available:true,kind:'language',local:true},{id:'embed',label:'EmbeddingGemma',provider:'Ollama',available:true,kind:'embedding',local:true},{id:'vision',label:'UI Venus',provider:'MLX',available:true,kind:'vision',local:true},{id:'offline',label:'Offline Brain',provider:'API',available:false,kind:'language',local:false},{id:'api:google:gemini-flash',label:'Gemini Flash',provider:'Google compatible API',available:true,kind:'language',local:false,effort_levels:['low','medium','high'],effort_default:'medium'},{id:'api:openai_oauth:gpt-5',label:'GPT-5',provider:'OpenAI Codex · ChatGPT plan',available:true,kind:'language',local:false,effort_levels:['low','medium','high'],effort_default:'medium'}]},usage:{models:{'api:google:gemini-flash':{input_tokens:1200,output_tokens:300,total_tokens:1500,window_start:0,window_kind:'weekly',last_used:null,limit_tokens:null,cost_usd:null}},week_starts_at:0},loading:false,error:'',save:vi.fn().mockResolvedValue(undefined),refresh:vi.fn().mockResolvedValue(undefined),refreshUsage:vi.fn().mockResolvedValue(undefined),resetUsage:vi.fn().mockResolvedValue(undefined)}}
describe('Brain model UI',()=>{
 it('selects a local model while preserving existing offline fallback choices',async()=>{
  const s=state();s.configuration.fallback_models=['offline','api:disconnected:missing'];
  render(<BrainModelPicker state={s}/>);
  fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));
  fireEvent.click(screen.getByRole('button',{name:'コーディングのモデルを選択'}));
  fireEvent.click(screen.getByRole('button',{name:/ローカル/}));
  fireEvent.click(screen.getByRole('button',{name:/Local Brain/}));
  await waitFor(()=>expect(s.save).toHaveBeenCalledWith(expect.objectContaining({
   selections:{...s.configuration.selections,coder:'local:brain'},
   fallback_models:['offline','api:disconnected:missing'],
  })));
 });
 it('uses Zero Auto MAX for binary API models and saves native on',async()=>{
  const s=state();const model=s.catalog.models.find(m=>m.id==='api:google:gemini-flash')!;
  model.effort_levels=['off','auto','on'];model.effort_default='auto';
  s.configuration.selections.coder=model.id;s.configuration.efforts.coder='auto';
  render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));
  const slider=screen.getByRole('slider',{name:'コーディングの推論強度'});
  expect(slider).toHaveAttribute('aria-valuetext','自動');
  fireEvent.change(slider,{target:{value:'2'}});
  await waitFor(()=>expect(s.save).toHaveBeenCalledWith(expect.objectContaining({efforts:expect.objectContaining({coder:'on'})})));
  expect(screen.getAllByText(/MAX: 計画・実装・検証・JSON生成でも常に思考オン/).length).toBeGreaterThan(0);
 });
 it('registers Ollama Cloud with the shared account form and hosted defaults',async()=>{
  const s=state(),save=vi.fn().mockResolvedValue(undefined);
  render(<ModelsPanel state={s} onBack={vi.fn()} onConfigure={save}/>);
  const row=screen.getByText('Ollama Cloud').closest('.brain-provider-group')! as HTMLElement;
  fireEvent.click(within(row).getByRole('button',{name:'設定'}));
  expect(screen.getByLabelText('APIベースURL')).toHaveValue('https://ollama.com/v1');
  expect(screen.getByLabelText('モデルID')).not.toBeRequired();
  const key=screen.getByLabelText('APIキー');expect(key).toHaveAttribute('type','password');
  fireEvent.change(key,{target:{value:'cloud-fixture'}});
  fireEvent.click(screen.getByRole('button',{name:'接続設定を保存'}));
  await waitFor(()=>expect(save).toHaveBeenCalledWith(expect.objectContaining({id:'ollama_cloud',auth_type:'api_key',account_id:1,values:expect.objectContaining({base_url:'https://ollama.com/v1',api_key:'cloud-fixture',model_ids:''})})));
 });
 it('groups cloud models under Ollama Cloud even if their names contain Gemini',()=>{
  const s=state();s.catalog.models.push({id:'api:ollama_cloud:gemini-cloud:latest',label:'Gemini via Ollama',provider:'Ollama Cloud',local:false,available:true,kind:'language'});
  render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));
  fireEvent.click(screen.getByRole('button',{name:'コーディングのモデルを選択'}));
  fireEvent.click(screen.getByRole('button',{name:/Ollama Cloud/}));
  expect(screen.getByRole('button',{name:/Gemini via Ollama/})).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:/Local Brain/})).toBeNull();
 });
 it('removes one fallback while preserving the order of the remaining models',async()=>{
  const s=state();s.configuration.fallback_models=['local:brain','api:google:gemini-flash','api:openai_oauth:gpt-5'];
  render(<ModelsPanel state={s} onBack={vi.fn()} onConfigure={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'フォールバック 2 を削除'}));
  await waitFor(()=>expect(s.save).toHaveBeenCalledWith({...s.configuration,fallback_models:['local:brain','api:openai_oauth:gpt-5']}));
 });
 it('opens effort controls first, displays one slider per role, and saves selected stages',async()=>{const s=state();s.configuration.selections.coder='api:google:gemini-flash';s.configuration.efforts.coder='medium';const configured={...INITIAL_BRAIN,selections:{...INITIAL_BRAIN.selections,coder:'api:google:gemini-flash'},efforts:{...INITIAL_BRAIN.efforts,coder:'medium'}};render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));expect(screen.getAllByRole('slider')).toHaveLength(3);expect(screen.queryByRole('navigation',{name:'モデルプロバイダー'})).toBeNull();fireEvent.change(screen.getByRole('slider',{name:'コーディングの推論強度'}),{target:{value:'2'}});await waitFor(()=>expect(s.save).toHaveBeenCalledWith({...configured,efforts:{...configured.efforts,coder:'high'}}));
  const unsupported=screen.getByRole('slider',{name:'単純なコーディング、修正の推論強度'});expect(unsupported).toBeDisabled();expect(unsupported).toHaveValue('1');expect(unsupported).toHaveAttribute('aria-valuetext','未設定');
  fireEvent.click(screen.getByRole('button',{name:'コーディングのモデルを選択'}));expect(screen.getByRole('navigation',{name:'モデルプロバイダー'})).toBeInTheDocument();expect(screen.queryByText('EmbeddingGemma')).toBeNull();expect(screen.queryByText('UI Venus')).toBeNull();fireEvent.click(screen.getByRole('button',{name:/ローカル/}));const role=screen.getByRole('region',{name:'コーディングのモデル'});fireEvent.click(within(role).getByRole('button',{name:/Local Brain/}));await waitFor(()=>expect(s.save).toHaveBeenLastCalledWith({...configured,selections:{...configured.selections,coder:'local:brain'},efforts:{...configured.efforts,coder:'auto'}}));expect(within(role).queryByRole('button',{name:/Offline Brain/})).toBeNull()});
 it('lets one role enter a provider, search model names, and return to providers',()=>{const s=state();render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));fireEvent.click(screen.getByRole('button',{name:'コーディングのモデルを選択'}));fireEvent.click(screen.getByRole('button',{name:/Google/}));expect(screen.getByText('コーディング · モデルを選択')).toBeInTheDocument();expect(screen.getAllByRole('button',{name:/Gemini Flash/})).toHaveLength(1);expect(screen.queryByText('Local Brain')).toBeNull();const search=screen.getByRole('textbox',{name:'モデル名を検索'});fireEvent.change(search,{target:{value:'does-not-exist'}});expect(screen.getByText('検索条件に一致するモデルがありません。')).toBeInTheDocument();fireEvent.change(search,{target:{value:'gemini'}});expect(screen.getAllByRole('button',{name:/Gemini Flash/})).toHaveLength(1);fireEvent.click(screen.getByRole('button',{name:'プロバイダー一覧に戻る'}));expect(screen.getByRole('navigation',{name:'モデルプロバイダー'})).toBeInTheDocument();expect(screen.queryByRole('textbox',{name:'モデル名を検索'})).toBeNull()});
 it('groups ChatGPT OAuth models under OpenAI after choosing its provider card',()=>{const s=state();render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));fireEvent.click(screen.getByRole('button',{name:'コーディングのモデルを選択'}));fireEvent.click(screen.getByRole('button',{name:/OpenAI/}));expect(screen.getAllByRole('button',{name:/GPT-5/})).toHaveLength(1)});
 it('honors a provider model with a single effort stage',()=>{const s=state();s.catalog.models.push({id:'api:openai:one-stage',label:'One stage',provider:'OpenAI compatible API',available:true,kind:'language',local:false,effort_levels:['maximum'],effort_default:'maximum'});s.configuration.selections.coder='api:openai:one-stage';render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));const slider=screen.getByRole('slider',{name:'コーディングの推論強度'});expect(slider).toHaveAttribute('max','0');expect(slider).toHaveValue('0');expect(slider).toHaveAttribute('aria-valuetext','最大');expect(screen.getAllByText('最大').length).toBeGreaterThan(0)});
 it('unified mode uses one role card and Escape returns focus',()=>{const s=state();s.configuration.mode='unified';render(<BrainModelPicker state={s}/>);const trigger=screen.getByRole('button',{name:'Brainモデルを選択'});fireEvent.click(trigger);expect(screen.getAllByRole('slider')).toHaveLength(1);expect(screen.getByRole('button',{name:'Brainのモデルを選択'})).toBeInTheDocument();fireEvent.keyDown(screen.getByRole('dialog'),{key:'Escape'});expect(screen.queryByRole('dialog')).toBeNull();expect(trigger).toHaveFocus()});
 it('shows model-specific weekly input/output usage and resets only that model window',async()=>{const s=state();s.configuration.selections.coder='api:google:gemini-flash';render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));expect(screen.getByText('週 1,500 tokens')).toBeInTheDocument();expect(screen.getByText('入力 1,200 · 出力 300')).toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Gemini Flashの利用量をリセット'}));await waitFor(()=>expect(s.resetUsage).toHaveBeenCalledWith('api:google:gemini-flash'))});
 it('falls back to measured requests when the provider does not report tokens',()=>{const s=state();s.configuration.selections.coder='api:google:gemini-flash';Object.assign(s.usage.models['api:google:gemini-flash'],{input_tokens:0,output_tokens:0,total_tokens:0,request_count:7,token_reported_requests:0,unreported_requests:7,failed_requests:1});render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));expect(screen.getByText('週 7 リクエスト')).toBeInTheDocument();expect(screen.getByText('トークン数未取得 · OLCRからの呼び出し回数')).toBeInTheDocument();expect(screen.queryByText('週 0 tokens')).toBeNull();expect(s.refreshUsage).toHaveBeenCalledOnce()});
 it('labels partial token coverage and hides incomplete input/output totals',()=>{const s=state();s.configuration.selections.coder='api:google:gemini-flash';Object.assign(s.usage.models['api:google:gemini-flash'],{request_count:3,token_reported_requests:2,unreported_requests:1});render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));expect(screen.getByText('週 1,500 tokens（取得分）')).toBeInTheDocument();expect(screen.getByText('3 リクエスト')).toBeInTheDocument();expect(screen.getByText('トークン取得 2/3 回')).toBeInTheDocument();expect(screen.queryByText('入力 1,200 · 出力 300')).toBeNull()});
 it('shows no usage separately from a reported zero-token request',()=>{const s=state();s.configuration.mode='unified';s.configuration.selections.brain='api:google:gemini-flash';delete s.usage.models['api:google:gemini-flash'];const view=render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));expect(screen.getByText('今週の利用記録なし')).toBeInTheDocument();s.usage.models['api:google:gemini-flash']={input_tokens:0,output_tokens:0,total_tokens:0,window_start:0,window_kind:'weekly',last_used:null,limit_tokens:null,cost_usd:null,request_count:1,token_reported_requests:1,unreported_requests:0};view.rerender(<BrainModelPicker state={s}/>);expect(screen.getByText('週 0 tokens')).toBeInTheDocument();expect(screen.getByText('1 リクエスト')).toBeInTheDocument()});
 it('opens the selected provider usage page through the desktop external URL bridge',()=>{const s=state();const model=s.catalog.models.find(item=>item.id==='api:google:gemini-flash')!;model.usage_url='https://aistudio.google.com/usage';s.configuration.selections.coder=model.id;render(<BrainModelPicker state={s}/>);fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));const link=screen.getByRole('link',{name:/Google AI Studio usage/});fireEvent.click(link);expect(openExternalMock).toHaveBeenCalledWith(model.usage_url);expect(screen.getByRole('dialog')).toBeInTheDocument()});
 it('mode changes retain separate model selections and settings has a back action',()=>{const s=state(),back=vi.fn();s.configuration.selections.coder='local:brain';render(<ModelsPanel state={s} onBack={back} onConfigure={vi.fn()}/>);fireEvent.click(screen.getByRole('button',{name:/ひとつに統合/}));expect(s.save).toHaveBeenCalledWith({...s.configuration,mode:'unified'});fireEvent.click(screen.getByRole('button',{name:'← 設定'}));expect(back).toHaveBeenCalledOnce()});
 it('shows only ready integrated purpose models and excludes generic Brain models',()=>{const s=state();s.catalog.purpose_models=[
  {id:'embeddinggemma',role:'semantic_embedding',purpose:'Embedding',purpose_en:'Embedding',label:'EmbeddingGemma',provider:'Ollama 0.32.15',deployment:'ollama/embeddinggemma:latest',runtime:'Ollama',local_path:'Ollama model store',ready:true},
  {id:'fastvlm_mlx_bf16',role:'general_image',purpose:'画像理解 · light / fast',purpose_en:'Vision · light / fast',label:'FastVLM · light / fast',provider:'MLX',deployment:'mlx-community/FastVLM-0.5B-bf16',runtime:'MLX',local_path:'models/vision/FastVLM-0.5B-bf16',ready:true},
  {id:'ui_venus_mlx_6bit',role:'gui_grounding',purpose:'画像・画面理解 · heavy / UI',purpose_en:'Vision · heavy / UI',label:'UI-Venus · heavy / UI',provider:'MLX',deployment:'mlx-community/UI-Venus-1.5-2B-6bit',runtime:'MLX',local_path:'models/vision/UI-Venus-1.5-2B-6bit',ready:true},
  {id:'unavailable-role',role:'unused',purpose:'Unused',purpose_en:'Unused',label:'Not integrated',provider:'MLX',deployment:'unused/model',runtime:'MLX',local_path:'models/unused',ready:false},
 ];render(<ModelsPanel state={s} onBack={vi.fn()} onConfigure={vi.fn()}/>);const inventory=screen.getByRole('region',{name:'用途別モデル一覧'});expect(within(inventory).getByRole('heading',{name:'Embedding'})).toBeInTheDocument();expect(within(inventory).getByText('EmbeddingGemma')).toBeInTheDocument();expect(within(inventory).getByRole('heading',{name:'画像理解 · light / fast'})).toBeInTheDocument();expect(within(inventory).getByText('FastVLM · light / fast')).toBeInTheDocument();expect(within(inventory).getByRole('heading',{name:'画像・画面理解 · heavy / UI'})).toBeInTheDocument();expect(within(inventory).getByText('UI-Venus · heavy / UI')).toBeInTheDocument();expect(within(inventory).queryByText('Not integrated')).toBeNull();expect(within(inventory).queryByRole('heading',{name:'汎用生成モデル'})).toBeNull();expect(within(inventory).queryByText('Gemini Flash')).toBeNull()});
 it('uses private credential fields and clears the key after saving',async()=>{const save=vi.fn().mockResolvedValue(undefined);render(<ModelProviderForm provider={{id:'openai',label:'OpenAI',configured:false,auth_type:'api_key'}} onSave={save} onCancel={vi.fn()}/>);expect(screen.getByLabelText('APIキー')).toHaveAttribute('type','password');fireEvent.change(screen.getByLabelText('APIキー'),{target:{value:'ui-test-only'}});fireEvent.change(screen.getByLabelText('モデルID'),{target:{value:'general-llm'}});fireEvent.click(screen.getByRole('button',{name:'接続設定を保存'}));await waitFor(()=>expect(screen.getByLabelText('APIキー')).toHaveValue(''));expect(save).toHaveBeenCalledOnce()});
 it('offers NVIDIA NIM with its hosted API endpoint and automatic model discovery',()=>{const s=state();render(<ModelsPanel state={s} onBack={vi.fn()} onConfigure={vi.fn()}/>);const row=screen.getByText('NVIDIA NIM').closest('.brain-provider-row') as HTMLElement;fireEvent.click(within(row).getByRole('button',{name:'設定'}));expect(screen.getByRole('heading',{name:'NVIDIA NIM'})).toBeInTheDocument();expect(screen.getByLabelText('APIベースURL')).toHaveValue('https://integrate.api.nvidia.com/v1');expect(screen.getByLabelText('モデルID')).not.toBeRequired()});
 it('offers one-click ChatGPT sign-in instead of generic OAuth endpoint fields',()=>{const s=state();render(<ModelsPanel state={s} onBack={vi.fn()} onConfigure={vi.fn()}/>);expect(screen.getByRole('button',{name:'ChatGPTで続行'})).toBeInTheDocument();expect(screen.queryByLabelText('OAuth client ID')).toBeNull();expect(screen.queryByLabelText('Authorization URL')).toBeNull()});
});
it('shows Quick only for supported API models and shares the saved choice across roles using that model',async()=>{
 const s=state(),id='api:openai_oauth:gpt-6-luna';
 s.catalog.models.push({id,label:'GPT-6 Luna',provider:'OpenAI',available:true,local:false,kind:'language',quick_supported:true});
 s.configuration.selections.short_coding=id;s.configuration.selections.general_inference=id;
 const view=render(<BrainModelPicker state={s}/>);
 fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));
 expect(screen.queryByRole('button',{name:'コーディングのQuickモード'})).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'推論のQuickモード'}));
 await waitFor(()=>expect(s.save).toHaveBeenCalledWith({...s.configuration,quick_models:{[id]:true}}));
 s.configuration={...s.configuration,quick_models:{[id]:true}};view.rerender(<BrainModelPicker state={s}/>);
 expect(screen.getByRole('button',{name:'推論のQuickモード'})).toHaveAttribute('aria-pressed','true');
 expect(screen.getByRole('button',{name:'単純なコーディング、修正のQuickモード'})).toHaveAttribute('aria-pressed','true');
 fireEvent.click(screen.getByRole('button',{name:'推論のQuickモード'}));
 await waitFor(()=>expect(s.save).toHaveBeenLastCalledWith({...s.configuration,quick_models:{[id]:false}}));
});
