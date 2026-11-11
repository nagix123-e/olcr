export type AgentBuilderBlock={type:'agent_builder';status:'collecting'|'created'|'cancelled';mode:'transfer'|'generate';agent_id?:string|null};
export function isAgentCreationRequest(text:string){
  if(/(?:エージェント|agents?).{0,15}(?:作成|create|build).{0,12}(?:方法|仕組み)|^\s*(?:how\s+(?:to|do|does)|explain\s+how|tell\s+me\s+how)/i.test(text))return false;
  if(!/カスタム|custom/i.test(text)&&/リポジトリ|codebase|repository|(?:コード|typescript|python).{0,20}(?:で|に|内|実装|作成|作って)|エージェント.{0,8}(?:実装|コーディング)|(?:implement|code)\s+(?:an?\s+)?agent/i.test(text))return false;
  return /(?:カスタム\s*)?(?:エージェント|agents?).{0,40}(?:作成|作って|作りたい|作る|ほしい|欲しい|create|build)|(?:作成|作って|create|build).{0,60}(?:エージェント|agents?)/i.test(text);
}
export function builderPending(messages:{blocks?:{type:string;[key:string]:any}[]}[]){
  const block=[...messages].reverse().flatMap(message=>message.blocks||[]).find(block=>block.type==='agent_builder');
  return block?.status==='collecting';
}
