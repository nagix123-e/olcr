"""Conversation-scoped agent creation. Supplied material never enters generation."""
from __future__ import annotations
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time
import uuid
import zipfile
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from typing import Literal
from .custom_agents import AgentDefinition, AgentKnowledge
from .agent_knowledge import read_file, TEXT_TYPES, BINARY_TYPES
from .agent_packages import AgentPackage, parse_zip, MAX_ARCHIVE_BYTES

LOCK = threading.RLock()

def creation_intent(text: str) -> bool:
    if re.search(r'(?:エージェント|agents?).{0,15}(?:作成|create|build).{0,12}(?:方法|仕組み)|^\s*(?:how\s+(?:to|do|does)|explain\s+how|tell\s+me\s+how)', text, re.I): return False
    # Describing an agent that discusses implementation is a creation request.
    # Exclude requests to implement an agent in a codebase, not its subject matter.
    if not re.search(r'カスタム|custom',text,re.I) and re.search(
        r'リポジトリ|codebase|repository|(?:コード|typescript|python).{0,20}(?:で|に|内|実装|作成|作って)|'
        r'エージェント.{0,8}(?:実装|コーディング)|(?:implement|code)\s+(?:an?\s+)?agent',text,re.I):return False
    return bool(re.search(r'(?:カスタム\s*)?(?:エージェント|agents?).{0,40}(?:作成|作って|作りたい|作る|ほしい|欲しい|create|build)|(?:作成|作って|create|build).{0,60}(?:エージェント|agents?)', text, re.I))

def requirements_intent(text: str) -> bool:
    """Recognize a requested agent's behavior without treating source text as a brief."""
    return creation_intent(text) or bool(re.search(
        r'(?:して|って|いて|めて|えて|けて|立てて)くれる|(?:エージェント|agent).{0,20}(?:にしたい|がほしい|が欲しい)|'
        r'生成条件|条件として|生成して|(?:知識|プロンプト|指示).{0,12}(?:作って|生成)|generate|requirements',text,re.I))

def builder_state(db, cid):
    if not cid: return None
    with db.connect() as connection:
        row = connection.execute('SELECT state_json FROM custom_agent_builders WHERE conversation_id=?', (cid,)).fetchone()
    return json.loads(row[0]) if row else None

def requested(db, value):
    return bool(getattr(value, 'agent_builder', False) or creation_intent(value.message)
                or (builder_state(db, value.conversation_id) or {}).get('status') == 'collecting')

class GeneratedKnowledge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: StrictStr = Field(min_length=1, max_length=200)
    content: StrictStr = Field(max_length=100_000)

class GeneratedDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['ask','create']
    questions: list[StrictStr] = Field(default_factory=list, max_length=3)
    name: StrictStr = Field(default='', max_length=100)
    description: StrictStr = Field(default='', max_length=1000)
    instructions: StrictStr = Field(default='', max_length=32000)
    knowledge: list[GeneratedKnowledge] = Field(default_factory=list, max_length=20)

def generate_decision(runtime, conditions, definition):
    # This function is reached only when there are no supplied prompts/files.
    system = ('Create a custom agent from the user requirements. Ask up to three concise questions in the user language '
              'only when missing information prevents writing useful agent instructions. Infer a suitable name from its role '
              'unless the user supplied a name. The requested agent behavior is the specification: if it should ask its future '
              'users about development details, encode that interview behavior in its instructions; do not ask the creator '
              'to provide a specific development project. Ask across multiple turns as necessary. '
              'When sufficient, create its instructions and useful reference knowledge. Do not invent external facts, '
              'credentials, installed tools, models, or source documents. Label content as generated rather than user-supplied. '
              'Copy the explicitly provided name and description exactly. Return only the requested schema.')
    from .coding_tasks import model_slot
    try:
        with model_slot():
            result = runtime._generate_brain([{'role':'system','content':system},
                {'role':'user','content':json.dumps({'requirements':conditions, 'provided_fields':definition},ensure_ascii=False)}],
                '\n'.join(conditions), structured_schema=GeneratedDecision.model_json_schema(),
                thinking_override=False, max_tokens=4096, model_name='role:normal_chat')
    except Exception as exc:raise ValueError('AGENT_BUILDER_GENERATION_UNAVAILABLE') from exc
    return GeneratedDecision.model_validate_json(result['text'])

FIELDS = {'名前':'name','名称':'name','name':'name','説明':'description','description':'description',
          '指示':'instructions','プロンプト':'instructions','prompt':'instructions','instructions':'instructions',
          '知識':'knowledge','knowledge':'knowledge','アイコン':'icon','icon':'icon',
          'モデル':'preferred_model','model':'preferred_model','用途':'preferred_router','purpose':'preferred_router',
          '条件':'conditions','生成条件':'conditions','conditions':'conditions','requirements':'conditions'}

def supplied_fields(text):
    pattern = r'(?m)^(名前|名称|name|説明|description|指示|プロンプト|prompt|instructions|知識|knowledge|アイコン|icon|モデル|model|用途|purpose|生成条件|条件|conditions|requirements)[：:][ \t]?'
    markers = list(re.finditer(pattern, text, re.I)); result = {}
    for index, marker in enumerate(markers):
        field=FIELDS[marker.group(1).lower()]
        # A supplied prompt/reference body is opaque. A line such as "name:"
        # inside it must never be reinterpreted as another builder field.
        if field in {'instructions','knowledge','conditions'}:
            value=text[marker.end():]
            if value.startswith('\n'):value=value[1:]
            result[field]=value
            break
        end = markers[index+1].start() if index+1 < len(markers) else len(text)
        value = text[marker.end():end]
        if value.startswith('\n'): value = value[1:]
        if index+1 < len(markers) and value.endswith('\n'): value = value[:-1]
        if field in {'name','icon','preferred_model','preferred_router'}:value=value.partition('\n')[0]
        result[field] = value
    if 'name' not in result:
        quoted=re.search(r'(?:名前|名称)(?:は|を)[「『"]([^」』"]+)[」』"]',text)
        if quoted:result['name']=quoted.group(1)
    return result

def attachment_data(value):
    attachment = value.attachment or value.image
    if not attachment: return None
    encoded = attachment.get('bytesBase64') or attachment.get('file_base64')
    if encoded is None and attachment.get('data_url'):
        encoded = str(attachment['data_url']).partition(',')[2]
    if encoded is not None:
        try: raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc: raise ValueError('AGENT_ATTACHMENT_INVALID') from exc
    else:
        raw = str(attachment.get('content') or '').encode('utf-8')
    return str(attachment.get('name') or 'knowledge.txt'), raw

def folder_knowledge(db, cid, value, existing_count):
    attachment=value.attachment
    if not isinstance(attachment,dict) or attachment.get('kind')!='folder':return None
    from .working_folders import WorkingFolders
    from .task_workspace import sensitive_path
    folders=WorkingFolders(db)
    try:
        folder=folders.attach(cid,attachment)
        root=Path(folder['root']).resolve(strict=True)
        paths=[relative for relative in folders.files(root)
               if not sensitive_path(relative) and PurePosixPath(relative).suffix.lower() in TEXT_TYPES|BINARY_TYPES]
        if not paths:raise ValueError('AGENT_KNOWLEDGE_FOLDER_HAS_NO_SUPPORTED_FILES')
        if existing_count+len(paths)>20:raise ValueError('AGENT_KNOWLEDGE_FOLDER_EXCEEDS_20_FILES')
        knowledge=[]
        for relative in paths:
            path=root.joinpath(*PurePosixPath(relative).parts)
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError('AGENT_KNOWLEDGE_FILE_UNAVAILABLE')
                with os.fdopen(fd,'rb') as handle:
                    fd=-1
                    raw=handle.read()
            finally:
                if fd>=0:os.close(fd)
            display_name=relative[-200:]
            knowledge.append(read_file(display_name,raw))
        return folder['name'],knowledge
    except OSError as exc:
        raise ValueError('AGENT_KNOWLEDGE_FOLDER_UNAVAILABLE') from exc

def knowledge_set_zip(raw):
    if len(raw) > MAX_ARCHIVE_BYTES: raise ValueError('AGENT_PACKAGE_TOO_LARGE')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if '.olcr-agent.json' in archive.namelist(): return parse_zip(raw)
            files = [info for info in infos if not info.is_dir() and PurePosixPath(info.filename).name!='.DS_Store'
                     and PurePosixPath(info.filename).parts[0]!='__MACOSX']
            if not files or len(files) > 20 or len(infos)>1000 or len({info.filename for info in infos}) != len(infos):
                raise ValueError('AGENT_KNOWLEDGE_ZIP_INVALID')
            for info in infos:
                path = PurePosixPath(info.filename)
                if (path.is_absolute() or '..' in path.parts or '\\' in info.filename or info.flag_bits & 1
                        or stat.S_ISLNK(info.external_attr >> 16)
                        or info.compress_type not in {zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED}):
                    raise ValueError('AGENT_KNOWLEDGE_ZIP_INVALID')
            return [read_file(info.filename, archive.read(info)) for info in files]
    except (zipfile.BadZipFile, RuntimeError, UnicodeError) as exc:
        raise ValueError('AGENT_KNOWLEDGE_ZIP_INVALID') from exc

def _put_message(connection, cid, role, content, blocks, now):
    ordinal = connection.execute('SELECT COALESCE(MAX(ordinal),-1)+1 FROM messages WHERE conversation_id=?',(cid,)).fetchone()[0]
    connection.execute('INSERT INTO messages(id,conversation_id,role,content,ordinal,created_at,blocks_json) VALUES(?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()),cid,role,content,ordinal,now,json.dumps(blocks,ensure_ascii=False) if blocks else None))

def _finish(db, cid, message_id, message, state, response, *, definition=None, package=None, attachment_name=None, attachment_image=None):
    if state.get('status')=='collecting':
        bounded={**state.get('definition',{}),'name':state.get('definition',{}).get('name') or 'draft'}
        if state.get('pending_source'):
            bounded['knowledge']=[*bounded.get('knowledge',[]),state['pending_source']]
        AgentDefinition.model_validate(bounded)
    from .attachment_previews import image_preview
    preview=image_preview(db,cid,attachment_image)
    now=time.time()
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        prior=connection.execute('SELECT result_json FROM custom_agent_builder_turns WHERE conversation_id=? AND message_id=?',(cid,message_id)).fetchone()
        if prior: return json.loads(prior[0])
        agent_id=None
        if definition is not None or package is not None:
            if package:
                mapping={entry.id:str(uuid.uuid4()) for entry in package.agents}
                entries=[(mapping[entry.id],entry.definition.model_copy(update={'subagent_ids':[mapping[key] for key in entry.definition.subagent_ids]})) for entry in package.agents]
                agent_id=mapping[package.root_id]
            else:
                agent_id=str(uuid.uuid4());entries=[(agent_id,definition)]
            for key, entry in entries:
                connection.execute('INSERT INTO custom_agents VALUES(?,?,1,0,?,?)',(key,entry.model_dump_json(),now,now))
            state={'status':'created','mode':state['mode'],'agent_id':agent_id}
        block={'type':'agent_builder','status':state['status'],'mode':state['mode'],'agent_id':agent_id,'source_message_id':message_id}
        result={'conversation_id':cid,'response':response,'blocks':[block],'sources':[]}
        connection.execute('INSERT INTO custom_agent_builders VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at',
                           (cid,json.dumps(state,ensure_ascii=False),now))
        connection.execute('INSERT INTO custom_agent_builder_turns VALUES(?,?,?)',(cid,message_id,json.dumps(result,ensure_ascii=False)))
        _put_message(connection,cid,'user',message,[{'type':'attachment','name':attachment_name,**preview}] if attachment_name else None,now)
        _put_message(connection,cid,'assistant',response,[block],now)
        connection.execute('UPDATE conversations SET updated_at=? WHERE id=?',(now,cid))
    return result

def cancel_builder(db, cid):
    state=builder_state(db,cid) or {'mode':'transfer'}
    return _finish(db,cid,str(uuid.uuid4()),'カスタムエージェント作成を中止',
                   {'mode':state.get('mode','transfer'),'status':'cancelled'},'カスタムエージェントの作成を中止しました。')

def handle_builder(context, value, cid, generate=None):
    db=context['db']; message=value.message; message_id=value.message_id or str(uuid.uuid4())
    with LOCK:
        with db.connect() as connection:
            prior=connection.execute('SELECT result_json FROM custom_agent_builder_turns WHERE conversation_id=? AND message_id=?',(cid,message_id)).fetchone()
        if prior:return json.loads(prior[0])
        old=builder_state(db,cid)
        state=old if old and old.get('status')=='collecting' else {'status':'collecting','mode':'generate','definition':{},'conditions':[],'locked':[],'waiting':None}
        if re.fullmatch(r'\s*(?:キャンセル|中止|作成を中止|カスタムエージェント作成を中止|cancel)\s*[。.!！]?\s*',message,re.I):
            state={'status':'cancelled','mode':state['mode']}
            return _finish(db,cid,message_id,message,state,'カスタムエージェントの作成を中止しました。')
        definition=state['definition']; provided=supplied_fields(message)
        # Recover drafts left by the old broad "implementation" exclusion when
        # the user repeats the same clear creation request. Never reinterpret files.
        if (state.get('waiting')=='source_role' and state.get('pending_text')
                and requirements_intent(message)
                and requirements_intent(state.get('pending_source',{}).get('content',''))):
            state.pop('pending_source',None);state.pop('pending_text',None)
            state['waiting']=None;state['mode']='generate'
        if not provided and not value.attachment and not value.image and state.get('waiting')!='name' and re.match(r'\s*(?:あなたは|you are\b|act as\b)',message,re.I):
            provided['instructions']=message
        if not state.get('waiting') and not provided and not value.attachment and not value.image:
            if re.match(r'\s*(?:あなたは|you are\b|act as\b)',message,re.I):
                provided['instructions']=message
            elif '\n' in message and re.search(r'そのまま|転写|転記|一字一句|原文|以下|verbatim',message,re.I):
                state['pending_source']={'name':'provided.txt','content':message.partition('\n')[2]}
                state['waiting']='source_role';state['mode']='transfer'
            elif not requirements_intent(message):
                state['pending_source']={'name':'provided.txt','content':message}
                state['pending_text']=True;state['waiting']='source_role';state['mode']='transfer'
        if state.get('waiting') in {'name','instructions'} and not provided and not value.attachment and not value.image:
            provided[state['waiting']]=message
        if old and old.get('waiting')=='source_role' and state.get('pending_source'):
            if re.search(r'知識|knowledge',message,re.I):
                definition.setdefault('knowledge',[]).append(state.pop('pending_source'));state['waiting']=None
            elif re.search(r'指示|プロンプト|prompt|instructions',message,re.I):
                provided['instructions']=state.pop('pending_source')['content'];state['waiting']=None
            elif state.get('pending_text') and re.search(r'条件|requirements',message,re.I):
                state['conditions'].append(state.pop('pending_source')['content']);state['mode']='generate';state['waiting']='conditions'
            else:
                question=('受け取った文章は「指示」「知識」「生成条件」のどれですか？' if state.get('pending_text') else
                          '受け取ったファイルは「指示」と「知識」のどちらに、そのまま登録しますか？')
                return _finish(db,cid,message_id,message,state,question)
        for key, item in provided.items():
            if key=='conditions':state['conditions'].append(item);continue
            if key=='knowledge': definition.setdefault('knowledge',[]).append({'name':f'知識{len(definition.get("knowledge",[]))+1}.txt','content':item})
            else: definition[key]=item
            if key not in state['locked']:state['locked'].append(key)
            if key in {'instructions','knowledge'}:state['mode']='transfer'
        folder_source=folder_knowledge(db,cid,value,len(definition.get('knowledge',[])))
        source=None if folder_source else attachment_data(value)
        package=None
        if folder_source:
            state['mode']='transfer'
            definition.setdefault('knowledge',[]).extend(folder_source[1])
        if (not source and 'conditions' not in provided and re.search(r'そのまま|転写|転記|一字一句|verbatim',message,re.I)
                and not (old and old.get('mode')=='generate' and old.get('waiting')=='conditions')):
            state['mode']='transfer'
        if source:
            state['mode']='transfer';name,raw=source
            if name.lower().endswith('.zip'):
                loaded=knowledge_set_zip(raw)
                if isinstance(loaded,AgentPackage):package=loaded
                else:definition.setdefault('knowledge',[]).extend(loaded)
            else:
                structured=None
                if name.lower().endswith('.json'):
                    try:structured=json.loads(raw)
                    except (ValueError,UnicodeError):pass
                explicit_knowledge=bool(re.search(r'(?:知識|資料|knowledge|reference)(?:として|に|へ|\s+as)',message,re.I))
                if isinstance(structured,dict) and structured.get('format')=='olcr.agent' and not explicit_knowledge:
                    package=AgentPackage.model_validate(structured)
                elif isinstance(structured,dict) and 'name' in structured and ('instructions' in structured or 'knowledge' in structured) and not explicit_knowledge:
                    definition=AgentDefinition.model_validate(structured).model_dump();state['definition']=definition;state['definition_complete']=True
                else:
                    item=read_file(name,raw)
                    if re.search(r'アイコン|\bicon\b',message,re.I) and item['mime_type'].startswith('image/'):
                        definition['icon']='data:'+item['mime_type']+';base64,'+item['file_base64']
                    elif re.search(r'(?:指示|プロンプト|prompt|instructions)(?:として|に|へ|\s+as)',message,re.I):
                        definition['instructions']=item['content']
                    elif re.search(r'知識|資料|knowledge|reference',message,re.I) or item['mime_type'] in {'application/pdf','image/png','image/jpeg','image/webp'}:
                        definition.setdefault('knowledge',[]).append(item)
                    else:
                        state['pending_source']=item;state['waiting']='source_role'
        if package:
            root=next(entry.definition for entry in package.agents if entry.id==package.root_id)
            return _finish(db,cid,message_id,message,state,f'カスタムエージェント「{root.name}」を転写して作成しました。',package=package,attachment_name=source[0])
        if state.get('pending_source'):
            question=('受け取った文章は「指示」「知識」「生成条件」のどれですか？' if state.get('pending_text') else
                      '受け取ったファイルは「指示」と「知識」のどちらに、そのまま登録しますか？')
            return _finish(db,cid,message_id,message,state,question,attachment_name=source[0] if source else folder_source[0] if folder_source else None,attachment_image=value.image or value.attachment)
        if state['mode']=='transfer':
            if not str(definition.get('name') or '').strip():
                state['waiting']='name';response='元の内容は変更せず登録します。エージェントの名前を教えてください。'
            elif not definition.get('instructions') and not definition.get('knowledge') and not state.get('definition_complete'):
                state['waiting']='instructions';response='そのまま登録する指示・プロンプト、または知識ファイルを送ってください。'
            else:
                created=AgentDefinition.model_validate(definition)
                return _finish(db,cid,message_id,message,state,f'カスタムエージェント「{created.name}」を作成しました。受け取った内容は変更していません。',definition=created,attachment_name=source[0] if source else folder_source[0] if folder_source else None,attachment_image=value.image or value.attachment)
        else:
            if not source and 'conditions' not in provided and re.search(r'さっき|前の(?:資料|添付|ファイル)|渡した|元の(?:プロンプト|指示|知識|資料|エージェント)|既存の(?:知識|資料|設定|エージェント)|この(?:資料|ファイル)|添付|previous(?:ly)?\s+(?:uploaded|provided)',message,re.I):
                state['mode']='transfer';state['waiting']='instructions'
                return _finish(db,cid,message_id,message,state,'元の内容を変更せず登録するため、指示を貼り付けるか、元ファイルをこの作成モードに添付してください。')
            if 'conditions' not in provided:state['conditions'].append(message)
            if sum(len(item) for item in state['conditions'])>100_000:raise ValueError('AGENT_BUILDER_REQUIREMENTS_TOO_LARGE')
            if not provided and re.fullmatch(r'\s*(?:カスタム)?(?:エージェント|agent)(?:を)?(?:作成|作って|作りたい)(?:してください|して|ください|ほしい)?[。.!！]?\s*',message,re.I):
                state['waiting']='conditions';response='どんな目的で、誰が使うエージェントを作りますか？指示や知識の生成条件を教えてください。'
            else:
                decision=(generate or (lambda conditions,fields:generate_decision(context['runtime'],conditions,fields)))(state['conditions'],definition)
                if not isinstance(decision,GeneratedDecision):decision=GeneratedDecision.model_validate(decision)
                if decision.action=='ask':
                    questions=[question.strip() for question in decision.questions if question.strip()]
                    state['waiting']='conditions';response='\n'.join(questions) or '目的・対象者・期待する答え方を教えてください。'
                    # The next answer may simply be "yes" or a short choice.
                    # Retain the question so generation can interpret that answer.
                    state['conditions'].append('作成時の確認質問（アシスタント）:\n'+response)
                else:
                    generated={'name':decision.name,'description':decision.description,'instructions':decision.instructions,
                               'knowledge':[item.model_dump() for item in decision.knowledge]}
                    generated.update(definition)
                    if not str(generated.get('name') or '').strip():
                        state['waiting']='name';response='エージェントの名前を教えてください。'
                    elif not str(generated.get('instructions') or '').strip():
                        state['waiting']='conditions';response='期待する役割・答え方をもう少し具体的に教えてください。'
                    elif not decision.knowledge and re.search(r'知識.{0,20}(?:生成|作って|作成)|(?:生成|作成).{0,20}知識|generate.{0,30}knowledge','\n'.join(state['conditions']),re.I):
                        state['waiting']='conditions';response='生成する参考知識の範囲や、含めたい項目を教えてください。'
                    else:
                        created=AgentDefinition.model_validate(generated)
                        return _finish(db,cid,message_id,message,state,f'条件に基づいて指示・参考知識を生成し、カスタムエージェント「{created.name}」を作成しました。',definition=created)
        return _finish(db,cid,message_id,message,state,response,attachment_name=source[0] if source else folder_source[0] if folder_source else None,attachment_image=value.image or value.attachment)
