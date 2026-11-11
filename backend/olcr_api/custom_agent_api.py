"""CRUD routes and shared chat ingestion for custom agents."""
import json
import time
import uuid
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictStr
from .custom_agents import AgentDefinition,AgentStore,tool_catalog,validate_definition
from .custom_agent_runtime import AgentRuntime,runtime_root
from .concept_diagrams import diagram_context
from .generation_control import GenerationCancelled
from .agent_packages import AgentPackage, export_zip, import_zip, import_package, MAX_ARCHIVE_BYTES

class KnowledgeFileInput(BaseModel):
    name: StrictStr = Field(min_length=1, max_length=200)
    data_base64: StrictStr

class AgentUse(BaseModel):
    project_id: str | None = None

def register_agent_routes(app, host):
    @app.post('/api/agents/builder/{conversation_id}/cancel')
    def cancel_creation(conversation_id: str):
        from .agent_builder import cancel_builder
        db=host()['db']
        if not db.conversation(conversation_id): raise HTTPException(404,'CONVERSATION_NOT_FOUND')
        return cancel_builder(db,conversation_id)
    @app.post('/api/agents/knowledge/read')
    def read_knowledge_file(value: KnowledgeFileInput):
        from .agent_knowledge import read_file, decode_file
        try: return read_file(value.name, decode_file(value.data_base64))
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc

    @app.post('/api/agents/import-zip')
    async def import_archive(request: Request):
        from starlette.concurrency import run_in_threadpool
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_ARCHIVE_BYTES: raise HTTPException(413, 'AGENT_PACKAGE_TOO_LARGE')
        try: return await run_in_threadpool(import_zip, AgentStore(host()['db']), bytes(data))
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    @app.post('/api/agents/import')
    def import_file(value: AgentPackage):
        return import_package(AgentStore(host()['db']), value)

    @app.get('/api/agents/{agent_id}/export')
    def export_file(agent_id: str):
        from fastapi.responses import Response
        from urllib.parse import quote
        store = AgentStore(host()['db'])
        agent = store.get(agent_id)
        if not agent or agent['archived']: raise HTTPException(404, 'AGENT_NOT_FOUND')
        try: data = export_zip(store, agent_id)
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc
        name = ''.join(c for c in agent['name'] if c not in '/\\' and ord(c) >= 32)[:100] or 'agent'
        return Response(data, media_type='application/zip',
                        headers={'Content-Disposition': "attachment; filename=agent.olcr-agent.zip; filename*=UTF-8''" + quote(name + '.olcr-agent.zip', safe='')})

    @app.get('/api/agents/options')
    def options():
        context=host()
        active=[s for s in context['skills'].list() if s.get('state')=='ACTIVE' and s.get('enabled') and not s.get('blocked')]
        return {'tools':tool_catalog(context['db']),'skills':[{'name':s['name'],'description':s.get('description','')} for s in active],
                'models':context['brain_models'].catalog()['models'],'runtime_available':runtime_root() is not None}
    @app.get('/api/agents')
    def listing(): return {'agents':AgentStore(host()['db']).list()}
    @app.post('/api/agents')
    def create(value: AgentDefinition):
        context=host();store=AgentStore(context['db'])
        try: validate_definition(value,store,context['brain_models'],context['skills'])
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        return store.save(value)
    @app.put('/api/agents/{agent_id}')
    def update(agent_id: str, value: AgentDefinition):
        context=host();store=AgentStore(context['db']);old=store.get(agent_id)
        if not old or old['archived']: raise HTTPException(404,'AGENT_NOT_FOUND')
        try: validate_definition(value,store,context['brain_models'],context['skills'],agent_id)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        return store.save(value,agent_id)
    @app.delete('/api/agents/{agent_id}')
    def archive(agent_id: str):
        store=AgentStore(host()['db'])
        if not store.get(agent_id): raise HTTPException(404,'AGENT_NOT_FOUND')
        store.archive(agent_id);return {'archived':True}
    @app.post('/api/agents/{agent_id}/conversations')
    def use(agent_id: str, value: AgentUse):
        context=host();store=AgentStore(context['db']);agent=store.get(agent_id)
        if not agent or agent['archived']: raise HTTPException(404,'AGENT_NOT_FOUND')
        if value.project_id:
            project=context['db'].project(value.project_id)
            if not project or project.get('archived'): raise HTTPException(404,'PROJECT_NOT_FOUND')
        try:
            # Opening a saved agent chat only creates its conversation snapshot.
            # Load skills and prepare the execution engine when a message is sent.
            return store.bind(agent,value.project_id)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc

def agent_chat(context, value, stream=False):
    # Keep expected provider failures inside FastAPI/CORS instead of dropping the
    # browser request with an unhandled 500 that looks like a network failure.
    from .generation_control import GenerationCancelled
    try:
        return _agent_chat(context,value,stream)
    except (HTTPException,GenerationCancelled):
        raise
    except Exception as exc:
        import logging
        import traceback
        from .models import Task,TaskState
        task=Task('Custom agent request failed')
        task.transition(TaskState.ROUTING);task.transition(TaskState.FAILED)
        task.error='AGENT_REQUEST_FAILED: '+type(exc).__name__
        try: context['db'].save_task(task,value.conversation_id)
        except Exception: pass
        # No prompts, image bytes, knowledge or provider credentials in diagnostics.
        frames=[f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}' for frame in traceback.extract_tb(exc.__traceback__)]
        logging.getLogger(__name__).error('AGENT_REQUEST_FAILED task=%s exception=%s stack=%s',task.id,type(exc).__name__,frames)
        from .response_language import response_language
        message=('エージェントの処理に失敗しました。設定したモデルの接続を確認して再試行してください。' if response_language()=='ja' else
                 'The agent request failed. Check the configured model connection and try again.')
        raise HTTPException(502,{'code':'AGENT_REQUEST_FAILED','message':message,'task_id':task.id}) from exc


def _agent_chat(context, value, stream=False):
    db=context['db'];record=db.conversation(value.conversation_id)
    if not record: raise HTTPException(404,'CONVERSATION_NOT_FOUND')
    if 'project_id' in value.model_fields_set and value.project_id!=record['project_id']: raise HTTPException(403,'CONVERSATION_PROJECT_MISMATCH')
    agent=AgentStore(db).bound(value.conversation_id)
    if not agent: raise HTTPException(404,'AGENT_CONVERSATION_NOT_FOUND')
    runner=AgentRuntime(**context)
    try:
        runner.prepare(agent)
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    if value.computer_use_event or value.resume_control or value.resume_task_id: raise HTTPException(422,'AGENT_CONTROL_EVENT_UNSUPPORTED')
    blocks=[];message=value.message
    if value.attachment:
        name=str(value.attachment.get('name','Attachment'))[:200];content=str(value.attachment.get('content',''))
        if len(content.encode())>300_000: raise HTTPException(422,'AGENT_ATTACHMENT_TOO_LARGE')
        from .attachment_previews import image_preview
        blocks.append({'type':'attachment','name':name,**image_preview(db,value.conversation_id,value.attachment)});message+='\nAttached reference data:\n'+json.dumps({'name':name,'content':content},ensure_ascii=False)
    if value.image:
        # Keep vision in OLCR, never let the JS execution library pick a vision model.
        try:
            if not isinstance(value.image.get('data_url'),str): raise ValueError('AGENT_IMAGE_DATA_REQUIRED')
            observation=runner.observe_image(value.image,value.message,agent,value.conversation_id)
            message+='\nOLCR vision observation (reference data):\n'+observation
            from .attachment_previews import image_preview
            blocks.append({'type':'attachment','name':str(value.image.get('name','Image'))[:200],**image_preview(db,value.conversation_id,value.image)})
        except (ValueError,TypeError) as exc: raise HTTPException(422,'AGENT_IMAGE_INVALID') from exc
    if location_map := getattr(value, "location_map", None):
        from .chat_maps import map_context
        blocks.append(location_map.model_dump());message+=map_context(location_map)
    if value.concept_diagram:
        blocks.append(value.concept_diagram.model_dump());message+=diagram_context(blocks[-1])
    if value.core_context: message+='\nUser context (reference):\n'+value.core_context
    db.add_message(value.conversation_id,'user',value.message,time.time(),str(uuid.uuid4()),blocks=blocks or None)
    def events():
        from .response_language import response_language
        import logging
        import traceback
        iterator=runner.events(agent,value.conversation_id,message)
        saving=False;task_id=None
        try:
            for event in iterator:
                task_id=event.get('task_id') or (event.get('task') or {}).get('id') or task_id
                if event['type']=='done':
                    saving=True
                    db.add_message(value.conversation_id,'assistant',event['response'],time.time(),str(uuid.uuid4()),event['task']['id'])
                    saving=False
                yield event
        except GenerationCancelled:
            raise
        except Exception as exc:
            code='AGENT_RESPONSE_SAVE_FAILED' if saving else 'AGENT_EXECUTION_FAILED'
            frames=[f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}' for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).error('%s task=%s exception=%s stack=%s',code,task_id,type(exc).__name__,frames)
            if response_language()=='ja':
                error_message=('回答は生成されましたが、会話への保存に失敗しました。' if saving else 'エージェントの実行に失敗しました。')
            else:
                error_message=('The answer was generated, but could not be saved to the conversation.' if saving else 'The agent execution failed.')
            yield {'type':'error','message':code+': '+error_message,'conversation_id':value.conversation_id,'task_id':task_id}
        finally:
            close=getattr(iterator,'close',None)
            if close:
                try: close()
                except Exception as exc:
                    logging.getLogger(__name__).error('AGENT_CLEANUP_FAILED task=%s exception=%s',task_id,type(exc).__name__)
    if stream:
        return StreamingResponse(('data: '+json.dumps(event,ensure_ascii=False)+'\n\n' for event in events()),media_type='text/event-stream')
    result=None
    for event in events():
        if event['type']=='error': raise HTTPException(502,event['message'])
        if event['type']=='done': result=event
    if not result: raise HTTPException(502,'AGENT_RESPONSE_MISSING')
    return {'conversation_id':value.conversation_id,'response':result['response'],'task':result['task'],'blocks':result.get('blocks',[])}
