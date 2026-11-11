import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {PurposeModelsPanel} from './PurposeModelsPanel';
import {api} from './api';
const invoke=vi.hoisted(()=>vi.fn().mockResolvedValue(undefined));
vi.mock('./native',()=>({native:()=>({core:{invoke}})}));
afterEach(()=>{cleanup();vi.clearAllMocks()});
const role={role:'semantic_embedding',purpose:'Embedding',purpose_en:'Embedding',protocols:['ollama','openai'],selected:{protocol:'preset'},preset:{id:'embeddinggemma',label:'EmbeddingGemma',deployment:'ollama/embeddinggemma:latest',runtime:'Ollama'}};
const catalog=()=>({roles:[structuredClone(role)],presets:[{id:'embeddinggemma',repo:'ollama/embeddinggemma:latest',size_bytes:621_000_000,license:'gemma',downloaded:false}],jobs:[],local_models:[{model:'user-embed',base_url:'http://localhost:11434',capabilities:['embedding']}]});
describe('Purpose model settings',()=>{
 it('cancels a prepared switch when Keychain storage fails',async()=>{
  const request=vi.fn(async(path:string)=>path.endsWith('/probe')?{token:'verified-token',credential_handle:'OLCR_MODEL_SEMANTIC_EMBEDDING_B'}:catalog());
  invoke.mockRejectedValueOnce(Error('KEYCHAIN_SAVE_FAILED'));
  render(<PurposeModelsPanel ja brainModels={[]} request={request as typeof api}/>);
  fireEvent.click(await screen.findByRole('button',{name:'変更'}));
  fireEvent.change(screen.getByLabelText('接続方法'),{target:{value:'openai'}});
  fireEvent.change(screen.getByLabelText('接続先URL'),{target:{value:'https://example.com/v1'}});
  fireEvent.change(screen.getByLabelText('モデルID'),{target:{value:'embed'}});
  fireEvent.change(screen.getByLabelText('APIキー（保存済みの場合は空欄）'),{target:{value:'TEST_ONLY_KEY'}});
  fireEvent.click(screen.getByRole('button',{name:'互換性を確認して切り替え'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('KEYCHAIN_SAVE_FAILED');
  expect(request).toHaveBeenCalledWith('/purpose-models/semantic_embedding/cancel',expect.anything());
  expect(request.mock.calls.some(([path])=>path.endsWith('/activate'))).toBe(false);
 });
 it('downloads only after explicit consent and offers the batch action',async()=>{const request=vi.fn().mockResolvedValue(catalog());render(<PurposeModelsPanel ja brainModels={[]} request={request as typeof api}/>);await screen.findByRole('button',{name:'推奨プリセットを一括ダウンロード'});fireEvent.click(screen.getByRole('button',{name:'ダウンロード'}));expect(screen.getByRole('dialog')).toBeInTheDocument();expect(request).toHaveBeenCalledTimes(1);fireEvent.click(screen.getByRole('button',{name:'同意してダウンロード'}));await waitFor(()=>expect(request).toHaveBeenCalledWith('/model-downloads',{method:'POST',body:JSON.stringify({model_ids:['embeddinggemma'],confirm_download:true})}));expect(screen.queryByRole('dialog')).toBeNull()});
 it('probes, saves the key to the returned bundle slot, then activates',async()=>{const events:string[]=[];const request=vi.fn(async(path:string)=>{events.push(path);if(path.endsWith('/probe'))return{token:'verified-token',credential_handle:'OLCR_MODEL_SEMANTIC_EMBEDDING_B'};if(path.endsWith('/activate'))return{applied:true};return catalog()});invoke.mockImplementation(async()=>{events.push('keychain')});render(<PurposeModelsPanel ja brainModels={[]} request={request as typeof api}/>);fireEvent.click(await screen.findByRole('button',{name:'変更'}));fireEvent.change(screen.getByLabelText('接続方法'),{target:{value:'openai'}});fireEvent.change(screen.getByLabelText('接続先URL'),{target:{value:'https://example.com/v1'}});fireEvent.change(screen.getByLabelText('モデルID'),{target:{value:'my-embedding'}});fireEvent.change(screen.getByLabelText('APIキー（保存済みの場合は空欄）'),{target:{value:'TEST_ONLY_KEY'}});fireEvent.click(screen.getByRole('button',{name:'互換性を確認して切り替え'}));await waitFor(()=>expect(request).toHaveBeenCalledWith('/purpose-models/semantic_embedding/activate',expect.anything()));expect(events.indexOf('/purpose-models/semantic_embedding/probe')).toBeLessThan(events.indexOf('keychain'));expect(events.indexOf('keychain')).toBeLessThan(events.indexOf('/purpose-models/semantic_embedding/activate'));expect(invoke).toHaveBeenCalledWith('set_external_credential',{handle:'OLCR_MODEL_SEMANTIC_EMBEDDING_B',value:'TEST_ONLY_KEY'})});
 it('keeps the previous model and editable form when compatibility fails',async()=>{const request=vi.fn(async(path:string)=>{if(path.endsWith('/probe'))throw Error('EMBEDDING_DIMENSION_MISMATCH');return catalog()});render(<PurposeModelsPanel ja brainModels={[]} request={request as typeof api}/>);fireEvent.click(await screen.findByRole('button',{name:'変更'}));fireEvent.change(screen.getByLabelText('接続方法'),{target:{value:'ollama'}});fireEvent.change(screen.getByLabelText('モデルID'),{target:{value:'bad-model'}});fireEvent.click(screen.getByRole('button',{name:'互換性を確認して切り替え'}));expect(await screen.findByRole('alert')).toHaveTextContent('EMBEDDING_DIMENSION_MISMATCH');expect(invoke).not.toHaveBeenCalled();expect(screen.getByLabelText('モデルID')).toHaveValue('bad-model');expect(within(screen.getByRole('region',{name:'Embedding'})).getByText('EmbeddingGemma')).toBeInTheDocument()});
});
