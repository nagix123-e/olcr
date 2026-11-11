import {cleanup,fireEvent,render,screen,within} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ModelsPanel} from './ModelsPanel';
import {BrainState, INITIAL_BRAIN} from './brainModels';
vi.mock('./PurposeModelsPanel',()=>({PurposeModelsPanel:()=>null}));
afterEach(cleanup);
it('shows the active role as the starting point and allows other role selections in the shared chain',()=>{
 const state:BrainState={configuration:structuredClone(INITIAL_BRAIN),catalog:{models:[
  {id:INITIAL_BRAIN.selections.coder,label:'Coder selection',provider:'MLX',local:true,available:true,kind:'language'},
  {id:INITIAL_BRAIN.selections.short_coding,label:'Short coding selection',provider:'MLX',local:true,available:true,kind:'language'}
 ],providers:[]},usage:{models:{},week_starts_at:0},loading:false,error:'',save:vi.fn().mockResolvedValue(undefined),refresh:vi.fn(),refreshUsage:vi.fn(),resetUsage:vi.fn()};
 render(<ModelsPanel state={state} onBack={vi.fn()} onConfigure={vi.fn()}/>);
 expect(screen.getByText('現在利用中のモデル（役割ごとの選択）')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'＋ モデルを追加'}));
 fireEvent.change(screen.getByLabelText('フォールバック 1 のプロバイダ'),{target:{value:'local'}});
 const select=screen.getByLabelText('フォールバック 1 のモデル');
 expect(within(select).getByRole('option',{name:'Coder selection'})).toBeInTheDocument();
 expect(within(select).getByRole('option',{name:'Short coding selection'})).toBeInTheDocument();
 fireEvent.change(select,{target:{value:INITIAL_BRAIN.selections.short_coding}});
 expect(state.save).toHaveBeenCalledWith({...state.configuration,fallback_models:[INITIAL_BRAIN.selections.short_coding]});
});
