import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ReusableTools} from './ReusableTools';
vi.mock('./language',()=>({useLanguage:()=>({language:'en'})}));
afterEach(cleanup);
it('generates an editable recipe and persists it only on Save',async()=>{
 const definition={name:'Normalize',description:'Lowercase',parameters:[{name:'text',type:'string',description:'Text'}],steps:[{tool_id:'lowercase',arguments_json:'{"text":"${inputs.text}"}'}]};
 const api=vi.fn(async(path:string,init?:RequestInit)=>path==='/agent-tools/draft'?definition:path==='/agent-tools'&&init?.method==='POST'?{id:'recipe.a',...definition}:{tools:[]});
 render(<ReusableTools api={api} onBack={()=>{}}/>);
 fireEvent.change(screen.getByLabelText('Describe your tool'),{target:{value:'Normalize text to lowercase'}});
 fireEvent.click(screen.getByText('Generate draft'));
 await screen.findByLabelText('Review and edit tool definition');
 expect(api.mock.calls.filter(([,init])=>init?.method==='POST'&&init?.body===JSON.stringify(definition))).toHaveLength(0);
 fireEvent.click(screen.getByText('Save'));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/agent-tools',{method:'POST',body:JSON.stringify(definition)}));
});
