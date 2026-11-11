import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {RuntimeComponentsSettings,RuntimeSetupStatus} from './RuntimeComponents';
afterEach(cleanup);
it('checks status without downloading; explicit preparation starts only the selected engine',async()=>{
 const api=vi.fn(async(path:string)=>path==='/components'?{components:[{name:'agents',state:'not_installed'},{name:'browser',state:'not_installed'}]}:{state:'installing'});
 render(<RuntimeComponentsSettings api={api}/>);
 const downloads=await screen.findAllByRole('button',{name:/ダウンロード|Download/});
 expect(downloads).toHaveLength(2);
 expect(api).toHaveBeenCalledTimes(1);
 fireEvent.click(downloads[0]);
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/components/agents/prepare',{method:'POST'}));
 expect(api).not.toHaveBeenCalledWith('/components/browser/prepare',expect.anything());
});
it('shows failed setup with retry and hides completed or unused engines',async()=>{
 const api=vi.fn(async()=>({components:[{name:'laya',state:'ready'},{name:'agents',state:'not_installed'},{name:'browser',state:'error',message:'取得失敗'}]}));
 render(<RuntimeSetupStatus api={api}/>);
 await waitFor(()=>expect(screen.getAllByRole('button')).toHaveLength(1));
 fireEvent.click(screen.getByRole('button'));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/components/browser/prepare',{method:'POST'}));
});
