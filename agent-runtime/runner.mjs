// LibreChat owns graph loops, context, events, checkpoints and subagent delegation.
// The host alone resolves models and executes the selected OLCR tools.
import {createInterface} from 'node:readline';
import {randomUUID} from 'node:crypto';
import {createAgentSession} from '@librechat/agents';
import {registerProvider} from '@librechat/agents/provider-registration';
import {BaseChatModel} from '@langchain/core/language_models/chat_models';
import {AIMessage,AIMessageChunk} from '@langchain/core/messages';
import {ChatGenerationChunk} from '@langchain/core/outputs';
import {DynamicStructuredTool} from '@langchain/core/tools';
import {convertToOpenAIFunction} from '@langchain/core/utils/function_calling';
import {SqliteSaver} from '@langchain/langgraph-checkpoint-sqlite';

const input=createInterface({input:process.stdin});
const pending=new Map();let initialResolve;
const initial=new Promise(resolve=>initialResolve=resolve);
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
// Keep dependency console output off the protocol channel.
console.log=(...args)=>console.error(...args);
input.on('line',line=>{try{const value=JSON.parse(line);if(value.type==='start')initialResolve(value);else if(value.type==='reply'){const waiter=pending.get(value.id);if(waiter){pending.delete(value.id);value.error?waiter.reject(new Error(value.error)):waiter.resolve(value.result);}}}catch{process.exitCode=1;}});
input.on('close',()=>{for(const waiter of pending.values())waiter.reject(new Error('HOST_DISCONNECTED'));});
function host(kind,payload){const id=randomUUID();return new Promise((resolve,reject)=>{pending.set(id,{resolve,reject});send({type:'request',id,kind,payload});});}
function boundSchema(tool){return tool.function || convertToOpenAIFunction(tool);}
class OLCRChat extends BaseChatModel {
  constructor(config){super(config);this.ownerId=config.ownerId;}
  _llmType(){return 'olcr';}
  bindTools(tools,options={}){return this.bind({...options,tools});}
  async answer(messages,options){
    const rows=messages.map(message=>({role:message.getType(),content:typeof message.content==='string'?message.content:JSON.stringify(message.content),...(message.tool_calls?.length?{tool_calls:message.tool_calls}:{}),...(message.tool_call_id?{tool_call_id:message.tool_call_id}:{})}));
    return host('model',{owner_id:this.ownerId,messages:rows,tools:(options.tools||[]).map(boundSchema)});
  }
  async _generate(messages,options){const result=await this.answer(messages,options);return {generations:[{text:result.content,message:new AIMessage({content:result.content,tool_calls:result.tool_calls||[]})}]};}
  async *_streamResponseChunks(messages,options){const result=await this.answer(messages,options);const message=new AIMessageChunk({content:result.content,tool_calls:result.tool_calls||[]});yield new ChatGenerationChunk({text:result.content,message});}
}
registerProvider({provider:'olcr',model:OLCRChat,family:'generic'});

let checkpointer;
try{
  const request=await initial;
  const makeConfig=agent=>({agentId:agent.id,name:agent.name,instructions:agent.instructions,
    llmConfig:{provider:'olcr',model:'OLCR',ownerId:agent.id},provider:'olcr',clientOptions:{model:'OLCR',ownerId:agent.id},
    tools:agent.tool_definitions.map(row=>new DynamicStructuredTool({name:row.name,description:row.description,schema:row.schema,
      func:async args=>JSON.stringify(await host('tool',{owner_id:agent.id,tool_id:row.id,args}))})),
    maxContextTokens:12000,maxToolResultChars:12000,summarizationEnabled:true,
    maxSubagentDepth:1,useLegacyContent:true});
  const root=makeConfig(request.agent);
  root.subagentConfigs=(request.agent.subagents||[]).map(child=>({type:child.id,name:child.name,description:child.description||child.name,agentInputs:makeConfig(child),maxTurns:8,allowNested:false}));
  checkpointer=SqliteSaver.fromConnString(request.checkpoint_path);
  const session=await createAgentSession({sessionId:request.conversation_id,sessionPath:request.session_path,cwd:request.state_dir,
    checkpointing:{checkpointer},runConfig:{runId:request.run_id,graphConfig:{type:'standard',...root,compileOptions:{checkpointer}},returnContent:true}});
  const stream=session.stream(request.message,{runId:request.run_id,config:{recursionLimit:30}});
  for await(const event of stream){
    // Reasoning is private; forward only status events to the OLCR chat.
    if(['tool.started','tool.completed','step.finished'].includes(event.type))send({type:'progress',event:event.type});
  }
  const result=await stream.finalResult();
  if(result.interrupted||result.haltedReason)throw new Error('AGENT_RUN_INCOMPLETE');
  if(!result.text?.trim())throw new Error('AGENT_EMPTY_RESPONSE');
  send({type:'done',text:result.text,usage:result.usage});
}catch(error){send({type:'error',error:error.message||'AGENT_RUNTIME_FAILED'});process.exitCode=1;}
finally{checkpointer?.db.close();input.close();}
