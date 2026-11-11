import {expect,it} from 'vitest';
import {responseReasoningBlocks} from './ReasoningLog';
it('includes received local reasoning from task metadata without duplicate logs',()=>{
 const task={model_calls:[{model:'ornith',reasoning:{text:'received work',complete:true,kind:'trace'}}]};
 const blocks=responseReasoningBlocks({task,blocks:[{type:'other'}]});
 expect(blocks[1].logs[0]).toMatchObject({text:'received work',model:'ornith'});
 expect(responseReasoningBlocks({task,blocks})).toBe(blocks);
});
it('does not manufacture a log for an enabled mode with no received channel',()=>{
 expect(responseReasoningBlocks({task:{model_calls:[{thinking_decision:{enabled:true}}]}})).toEqual([]);
});
