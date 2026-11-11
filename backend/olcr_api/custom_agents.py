"""OLCR-owned definitions and policies; execution belongs to LibreChat Agents."""
from __future__ import annotations
import json
import time
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

class AgentKnowledge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: StrictStr = Field(min_length=1, max_length=200)
    content: StrictStr = Field(max_length=100_000)
    mime_type: StrictStr | None = Field(default=None, max_length=100)
    file_base64: StrictStr | None = None

    @model_validator(mode='after')
    def original_file(self):
        if self.file_base64 is not None:
            from .agent_knowledge import decode_file, validate_file
            mime = validate_file(self.name, decode_file(self.file_base64))
            if self.mime_type != mime: raise ValueError('AGENT_KNOWLEDGE_TYPE_MISMATCH')
        return self

class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: StrictStr = Field(min_length=1, max_length=100)
    description: StrictStr = Field(default='', max_length=1000)
    icon: StrictStr = Field(default='✦', max_length=350_000)
    instructions: StrictStr = Field(default='', max_length=32_000)
    preferred_router: Literal['normal_chat','planner_replanner','short_coding_fix_debug','long_implementation'] = 'normal_chat'
    preferred_model: StrictStr | None = Field(default=None, max_length=250)
    knowledge: list[AgentKnowledge] = Field(default_factory=list, max_length=20)
    tools: list[StrictStr] = Field(default_factory=list, max_length=30)
    skills: list[StrictStr] = Field(default_factory=list, max_length=20)
    subagent_ids: list[StrictStr] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def bounded(self):
        if not self.name.strip(): raise ValueError('AGENT_NAME_REQUIRED')
        value = self.model_dump()
        for item in value['knowledge']:
            item.pop('file_base64', None)
        if len(json.dumps(value,ensure_ascii=False).encode()) > 600_000: raise ValueError('AGENT_DEFINITION_TOO_LARGE')
        for items in (self.tools,self.skills,self.subagent_ids):
            if len(items)!=len(set(items)): raise ValueError('AGENT_DUPLICATE_REFERENCE')
        return self

class AgentStore:
    def __init__(self, db): self.db=db
    def list(self):
        with self.db.connect() as con:
            return [self.decode(row) for row in con.execute('SELECT * FROM custom_agents WHERE archived=0 ORDER BY updated_at DESC')]
    @staticmethod
    def decode(row): return {**json.loads(row['definition_json']),'id':row['id'],'revision':row['revision'],'archived':bool(row['archived'])}
    def get(self, agent_id):
        with self.db.connect() as con:
            row=con.execute('SELECT * FROM custom_agents WHERE id=?',(agent_id,)).fetchone()
            return self.decode(row) if row else None
    def save(self, value: AgentDefinition, agent_id=None):
        agent_id=agent_id or str(uuid.uuid4());now=time.time()
        with self.db.connect() as con:
            if con.execute('SELECT archived FROM custom_agents WHERE id=?',(agent_id,)).fetchone() is not None:
                con.execute('UPDATE custom_agents SET definition_json=?,revision=revision+1,updated_at=? WHERE id=? AND archived=0',(value.model_dump_json(),now,agent_id))
            else:
                con.execute('INSERT INTO custom_agents VALUES(?,?,1,0,?,?)',(agent_id,value.model_dump_json(),now,now))
        return self.get(agent_id)
    def archive(self, agent_id):
        with self.db.connect() as con: con.execute('UPDATE custom_agents SET archived=1,updated_at=? WHERE id=?',(time.time(),agent_id))
    def bound(self, conversation_id):
        with self.db.connect() as con:
            row=con.execute('SELECT definition_json FROM custom_agent_conversations WHERE conversation_id=?',(conversation_id,)).fetchone()
            return json.loads(row[0]) if row else None
    def bind(self, agent, project_id=None):
        cid=str(uuid.uuid4());now=time.time()
        children=[]
        for child_id in agent['subagent_ids']:
            child=self.get(child_id)
            if not child or child['archived']: raise ValueError('AGENT_SUBAGENT_UNAVAILABLE')
            children.append(child)
        snapshot={**agent,'subagents':children}
        with self.db.connect() as con:
            con.execute('INSERT INTO conversations(id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?)',(cid,project_id,agent['name'],now,now))
            con.execute('INSERT INTO custom_agent_conversations VALUES(?,?,?)',(cid,agent['id'],json.dumps(snapshot,ensure_ascii=False)))
        return self.db.conversation(cid)

# Reuse existing validated tool executors; no shell/file-write tool is exposed.
BUILTIN_SCHEMAS={
    'calculator': {'expression':{'type':'string'}},
    'json_validate': {'text':{'type':'string'}},
    'lowercase': {'text':{'type':'string'}},
    'sort_ascending': {'items':{'type':'array','items':{'type':'number'}}},
    'list_filter': {'items':{'type':'array','items':{}},'equals':{}},
}
EMAIL_SEND_SCHEMA={'type':'object','properties':{
    'provider':{'type':'string','enum':['service.google.gmail','service.apple.mail']},
    'to':{'oneOf':[{'type':'string','format':'email','minLength':3,'maxLength':500},
                   {'type':'array','minItems':1,'maxItems':50,'items':{'type':'string','format':'email','maxLength':500}}]},
    'cc':{'oneOf':[{'type':'string','format':'email','maxLength':500},
                   {'type':'array','maxItems':50,'items':{'type':'string','format':'email','maxLength':500}}]},
    'bcc':{'oneOf':[{'type':'string','format':'email','maxLength':500},
                    {'type':'array','maxItems':50,'items':{'type':'string','format':'email','maxLength':500}}]},
    'subject':{'type':'string','minLength':1,'maxLength':998},
    'body':{'type':'string','maxLength':200000},
},'required':['provider','to','subject','body'],'additionalProperties':False}
def tool_catalog(db=None):
    from . import external_tools
    from .response_language import response_language
    rows=[{'id':name,'name':name,'description':name,'schema':{'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}} for name,properties in BUILTIN_SCHEMAS.items()]
    rows.append({'id':'email_send','name':'email_send',
        'description':('ユーザーが明示的に依頼したメールをGmailまたはApple Mailから送信します。宛先を推測せず、不明なら確認してください。OLCRがAIエージェントによる送信注記を本文末尾に必ず追加します。' if response_language()=='ja' else
                       'Send an email explicitly requested by the user through Gmail or Apple Mail. Never guess recipients; ask if unclear. OLCR always appends its AI-agent disclosure to the end of the message body.'),
        'schema':EMAIL_SEND_SCHEMA})
    rows.append({'id':'youtube_search','name':'youtube_search',
        'description':('YouTubeで動画・解説・音楽を検索し、タイトル・チャンネル・動画URLを返します。APIキー不要。自動再生はしません。' if response_language() == 'ja' else
                       'Search YouTube for videos, tutorials or music. No API key required. Returns actual titles, channels and video URLs; does not start playback.'),
        'schema':{'type':'object','properties':{'query':{'type':'string','minLength':1,'maxLength':300}},
                  'required':['query'],'additionalProperties':False}})
    rows += [{'id':key,'name':'api_'+key.replace('.','_').replace('-','_'),'description':item.description,
              'schema':{'type':'object','properties':{'arguments':{'type':'string','description':'JSON object of arguments accepted by this OLCR API tool.'}},'required':['arguments'],'additionalProperties':False}}
             for key,item in external_tools.RUNTIME_REGISTRY.items() if item.implemented]
    if db is not None:
        from .agent_tools import ToolStore
        rows += ToolStore(db).catalog()
    return rows

def validate_definition(value, store, brain_models, skills, agent_id=None):
    if any(tool not in {row['id'] for row in tool_catalog(store.db)} for tool in value.tools): raise ValueError('AGENT_UNKNOWN_TOOL')
    for skill in value.skills: skills.load(skill)  # existing enabled/trusted policy
    if value.preferred_model:
        if not any(m['id']==value.preferred_model and m.get('available') for m in brain_models.catalog()['models']): raise ValueError('AGENT_MODEL_UNAVAILABLE')
    for child_id in value.subagent_ids:
        child=store.get(child_id)
        if child_id==agent_id or not child or child['archived']: raise ValueError('AGENT_SUBAGENT_UNAVAILABLE')
