import {expect,it} from 'vitest';
import {isAgentCreationRequest,builderPending} from './agentBuilder';

it('routes explicit creation but preserves help and ordinary coding requests',()=>{
  expect(isAgentCreationRequest('勉強方法を教えるカスタムエージェントを作って')).toBe(true);
  expect(isAgentCreationRequest('Create a custom agent that teaches how to write essays')).toBe(true);
  expect(isAgentCreationRequest('カスタムエージェントを作成する方法を説明して')).toBe(false);
  expect(isAgentCreationRequest('Explain how to create a custom agent')).toBe(false);
  expect(isAgentCreationRequest('Create an agent in this Python repository')).toBe(false);
  expect(isAgentCreationRequest('開発計画をユーザーに実装詳細について質問しながら立ててくれるエージェントを作りたい')).toBe(true);
  expect(isAgentCreationRequest('Pythonを教えてくれるエージェントを作りたい')).toBe(true);
  expect(isAgentCreationRequest('Pythonでエージェントを作って')).toBe(false);
  expect(isAgentCreationRequest('エージェントを実装して')).toBe(false);
});
it('resumes only the latest unfinished creation in this conversation',()=>{
  expect(builderPending([{blocks:[{type:'agent_builder',status:'collecting'}]}])).toBe(true);
  expect(builderPending([{blocks:[{type:'agent_builder',status:'collecting'}]},{blocks:[{type:'agent_builder',status:'created'}]}])).toBe(false);
  expect(builderPending([])).toBe(false);
});
