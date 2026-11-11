import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {BrainModelPicker} from './BrainModelPicker';
import {ReasoningLog,appendReasoning,type ReasoningLogBlock} from './ReasoningLog';
import {INITIAL_BRAIN,type BrainState} from './brainModels';
import {LanguageProvider} from './language';

afterEach(cleanup);
const state=():BrainState=>({configuration:structuredClone(INITIAL_BRAIN),catalog:{models:[],providers:[]},
  usage:{models:{},week_starts_at:0},loading:false,error:'',save:vi.fn().mockResolvedValue(undefined),refresh:vi.fn(),refreshUsage:vi.fn(),resetUsage:vi.fn()});
const block:ReasoningLogBlock={type:'reasoning_log',logs:[{text:'<script>must remain text</script>',kind:'trace',complete:true,truncated:false,model:'local'}]};

describe('thinking controls and received logs',()=>{
  it('accumulates bounded deltas without mixing them with an answer',()=>{
    const first=appendReasoning(undefined,{text:'work ',kind:'trace',complete:false,truncated:false},'model');
    const second=appendReasoning(first,{text:'done',kind:'trace',complete:false,truncated:false});
    expect(second.logs[0].text).toBe('work done');expect(second.logs[0].model).toBe('model');
    const bounded=appendReasoning(second,{text:'x'.repeat(64000),kind:'trace',complete:false,truncated:false});
    expect(bounded.logs[0].text).toHaveLength(64000);expect(bounded.logs[0].truncated).toBe(true);
  });
  it('uses the existing local effort slider for zero, auto and MAX',async()=>{
    const s=state();s.configuration.mode='unified';s.catalog.models=[{id:s.configuration.selections.brain,label:'Local model',provider:'MLX',kind:'language',local:true,available:true}];
    const view=render(<LanguageProvider><BrainModelPicker state={s}/></LanguageProvider>);
    fireEvent.click(screen.getByRole('button',{name:'Brainモデルを選択'}));
    const slider=screen.getByRole('slider',{name:'Brainの推論強度'});
    expect(slider).toBeEnabled();expect(slider).toHaveValue('1');expect(slider).toHaveAttribute('aria-valuetext','自動');
    expect(screen.queryByRole('group',{name:'思考モード'})).toBeNull();
    await act(async()=>fireEvent.change(slider,{target:{value:'0'}}));
    expect(s.save).toHaveBeenLastCalledWith({...s.configuration,efforts:{...s.configuration.efforts,brain:'off'}});
    await act(async()=>fireEvent.change(slider,{target:{value:'2'}}));
    expect(s.save).toHaveBeenLastCalledWith({...s.configuration,efforts:{...s.configuration.efforts,brain:'on'}});
    s.configuration.efforts.brain='on';view.rerender(<LanguageProvider><BrainModelPicker state={s}/></LanguageProvider>);
    expect(slider).toHaveAttribute('aria-valuetext','MAX');
    await act(async()=>fireEvent.change(slider,{target:{value:'1'}}));
    expect(s.save).toHaveBeenLastCalledWith({...s.configuration,efforts:{...s.configuration.efforts,brain:'auto'}});
    s.loading=true;view.rerender(<LanguageProvider><BrainModelPicker state={s}/></LanguageProvider>);expect(slider).toBeDisabled();
  });
  it('keeps logs collapsed, literal and separate from an answer without fetching',()=>{
    const fetch=vi.spyOn(globalThis,'fetch');const {container}=render(<LanguageProvider><ReasoningLog block={block}/><p>answer</p></LanguageProvider>);
    expect(container.querySelector('details')).not.toHaveAttribute('open');
    expect(container.querySelector('script')).toBeNull();
    fireEvent.click(screen.getByText('思考ログ'));
    expect(screen.getByText(block.logs[0].text)).toBeInTheDocument();
    expect(screen.getByText('answer')).toBeInTheDocument();expect(fetch).not.toHaveBeenCalled();fetch.mockRestore();
  });
  it('distinguishes summaries, receiving, incomplete and truncated states',()=>{
    const value:ReasoningLogBlock={type:'reasoning_log',logs:[{...block.logs[0],kind:'summary',complete:false,truncated:true}]};
    const view=render(<LanguageProvider><ReasoningLog block={value} streaming/></LanguageProvider>);
    expect(screen.getByText('受信中')).toBeInTheDocument();
    expect(screen.getByText('プロバイダーの思考要約')).toBeInTheDocument();
    expect(screen.getByText(/表示上限/)).toBeInTheDocument();
    view.rerender(<LanguageProvider><ReasoningLog block={value}/></LanguageProvider>);
    expect(screen.getByText('未完了')).toBeInTheDocument();expect(screen.queryByText('受信中')).toBeNull();
  });
  it('does not render an empty disclosure',()=>{
    const {container}=render(<ReasoningLog block={{type:'reasoning_log',logs:[]}}/>);expect(container).toBeEmptyDOMElement();
  });
});
