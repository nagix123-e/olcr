from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
import uuid
from typing import Optional
import threading

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .artifacts import ArtifactStore
from .db import Database
from .ollama import OllamaProvider
from .retrieval import DisabledVectorStore, FileRetriever, FTSRetriever, PathGuard, RetrievalRouter
from .semantic import EmbeddingFailure, LocalVectorStore, OllamaEmbeddingProvider, OllamaIntentNormalizer, OllamaSemanticRelationEvaluator, QwenReranker
from .runtime import ContextManager, Runtime
from .models import Route, Task, TaskState
from .ollama import ModelFailure


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    conversation_id: Optional[str] = None
    approved: bool = False
    core_context: Optional[str] = Field(default=None, max_length=50_000)


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=20, ge=1, le=200)


class IndexInput(BaseModel):
    path: str

class ConfirmationInput(BaseModel):
    action_id: str
    approve: bool

class SettingsInput(BaseModel):
    ollama_endpoint: str
    main_model: str = ""
    router_model: str = ""
    embedding_model: str = ""
    semantic_judge_model: str = ""
    reranker_enabled: bool = False
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_threshold: float = 0.01
    allowed_roots: list[str]
    vector_enabled: bool = False
    context_budget: int = Field(ge=256, le=200000)
    result_limit: int = Field(default=20, ge=1, le=200)
    confirmation_policy: str = "explicit"


environment_settings = Settings.from_env()
settings = environment_settings
db = Database(settings.db_path)
db.initialize()
persisted=db.load_settings()
if persisted: settings=settings.with_overrides(persisted)
files = FileRetriever(settings.allowed_roots)
vectors = LocalVectorStore(db,OllamaEmbeddingProvider(settings.ollama_endpoint),settings.embedding_model,settings.allowed_roots) if settings.vector_enabled else DisabledVectorStore()
retrieval = RetrievalRouter(files, FTSRetriever(db), vectors, settings.vector_enabled, OllamaSemanticRelationEvaluator(settings.ollama_endpoint,settings.semantic_judge_model), OllamaIntentNormalizer(settings.ollama_endpoint,settings.semantic_judge_model), QwenReranker(settings.reranker_model,settings.reranker_enabled,settings.reranker_threshold) if settings.reranker_enabled else None, settings.reranker_threshold)
artifacts=ArtifactStore(str(Path(settings.db_path).parent/"artifacts"),db)
runtime = Runtime(settings, db, retrieval, OllamaProvider(settings.ollama_endpoint),artifacts)
cancel_events: dict[str,threading.Event]={}
app = FastAPI(title="OLCR", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


def rebuild(candidate: Settings) -> None:
    global settings,files,vectors,retrieval,runtime
    settings=candidate; files=FileRetriever(settings.allowed_roots)
    vectors=LocalVectorStore(db,OllamaEmbeddingProvider(settings.ollama_endpoint),settings.embedding_model,settings.allowed_roots) if settings.vector_enabled else DisabledVectorStore()
    retrieval=RetrievalRouter(files,FTSRetriever(db),vectors,settings.vector_enabled,OllamaSemanticRelationEvaluator(settings.ollama_endpoint,settings.semantic_judge_model),OllamaIntentNormalizer(settings.ollama_endpoint,settings.semantic_judge_model),QwenReranker(settings.reranker_model,settings.reranker_enabled,settings.reranker_threshold) if settings.reranker_enabled else None,settings.reranker_threshold)
    runtime=Runtime(settings,db,retrieval,OllamaProvider(settings.ollama_endpoint),artifacts)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.4", "runtime_root": str(Path(__file__).resolve().parents[2]), "app_support": str(Path(settings.db_path).parent), "vector_enabled": settings.vector_enabled, "roots": settings.allowed_roots, "db_path": settings.db_path, "main_model": settings.main_model, "model_configuration": "ready" if settings.main_model else "not_ready"}


@app.post("/api/chat")
def chat(value: ChatInput):
    conversation_id=value.conversation_id or db.create_conversation(value.message,time.time(),str(uuid.uuid4()))
    if not db.conversation(conversation_id): raise HTTPException(404,"conversation not found")
    db.add_message(conversation_id,"user",value.message,time.time(),str(uuid.uuid4()))
    task, response = runtime.execute(value.message, value.approved, value.core_context or "")
    db.save_task(task,conversation_id)
    db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task.id)
    return {"conversation_id":conversation_id,"task": task.__dict__ | {"route": task.route.value if task.route else None, "state": task.state.value}, "response": response}


@app.post("/api/chat/stream")
def stream_chat(value: ChatInput):
    conversation_id=value.conversation_id or db.create_conversation(value.message,time.time(),str(uuid.uuid4()))
    if not db.conversation(conversation_id): raise HTTPException(404,"conversation not found")
    db.add_message(conversation_id,"user",value.message,time.time(),str(uuid.uuid4()))
    direct=runtime._direct(value.message); retrieval_query=runtime._retrieval_query(value.message); synthesis=any(x in value.message.lower() for x in ("summarize","explain","synthesize"))
    if direct or (retrieval_query and not synthesis) or any(x in value.message.lower() for x in ("sudo ","rm -rf","write file")):
        task,response=runtime.execute(value.message,value.approved)
        db.save_task(task,conversation_id)
        def immediate():
            yield "data: "+json.dumps({"type":"meta","task_id":task.id,"conversation_id":conversation_id})+"\n\n"
            yield "data: "+json.dumps({"type":"chunk","text":response})+"\n\n"
            db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task.id)
            yield "data: "+json.dumps({"type":"done","task":serialize_task(task)})+"\n\n"
        return StreamingResponse(immediate(),media_type="text/event-stream")
    task=Task(value.message); task.transition(TaskState.ROUTING); db.save_task(task,conversation_id)
    if retrieval_query:
        task.route=Route.RETRIEVAL; task.reason_category="explicit_search_intent"; task.transition(TaskState.SEARCHING)
        db.save_task(task,conversation_id)
        try: evidence,method=retrieval.retrieve(retrieval_query,settings.result_limit)
        except Exception as exc:
            task.selected_context=[{"retrieval_failures":retrieval.last_failures}]
            task.error="retrieval_failed:"+type(exc).__name__; task.transition(TaskState.FAILED); db.save_task(task,conversation_id)
            response="Retrieval failed safely. See the task trace for the failing retrieval layer."
            db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task.id)
            def failed():
                yield "data: "+json.dumps({"type":"meta","task_id":task.id,"conversation_id":conversation_id})+"\n\n"
                yield "data: "+json.dumps({"type":"error","message":response,"task":serialize_task(task)})+"\n\n"
            return StreamingResponse(failed(),media_type="text/event-stream")
        messages,selected=ContextManager(settings.context_budget).build(value.message,evidence); task.selected_context=selected
        if retrieval.last_failures: task.selected_context.append({"retrieval_failures":retrieval.last_failures})
        task.transition(TaskState.GENERATING)
    else:
        task.route=Route.NEURAL; task.reason_category="open_ended_generation"; task.transition(TaskState.GENERATING)
        messages=[{"role":"system","content":"Be concise. Do not claim unobserved actions."},{"role":"user","content":value.message}]
    db.save_task(task,conversation_id)
    event=threading.Event(); cancel_events[task.id]=event
    def events():
        full=""; started=time.perf_counter(); prompt_tokens=None; completion_tokens=None
        yield "data: "+json.dumps({"type":"meta","task_id":task.id,"conversation_id":conversation_id})+"\n\n"
        try:
            stream=runtime.model.generate(messages,settings.main_model,stream=True)
            for part in stream:
                if event.is_set():
                    if hasattr(stream,"close"): stream.close()
                    task.transition(TaskState.CANCELLED); task.error="cancelled by user"; break
                text=part.get("text",""); full+=text
                if part.get("prompt_tokens") is not None: prompt_tokens=part["prompt_tokens"]
                if part.get("completion_tokens") is not None: completion_tokens=part["completion_tokens"]
                if text: yield "data: "+json.dumps({"type":"chunk","text":text})+"\n\n"
            if task.state is TaskState.GENERATING: task.transition(TaskState.COMPLETED)
            task.model_calls.append({"model":settings.main_model,"prompt_tokens":prompt_tokens,"completion_tokens":completion_tokens,"latency_ms":(time.perf_counter()-started)*1000,"status":"cancelled" if task.state is TaskState.CANCELLED else "success"})
            db.save_task(task)
            if full: db.add_message(conversation_id,"assistant",full,time.time(),str(uuid.uuid4()),task.id)
            yield "data: "+json.dumps({"type":"cancelled" if task.state is TaskState.CANCELLED else "done","task":serialize_task(task)})+"\n\n"
        except (ModelFailure,GeneratorExit) as exc:
            if task.state is TaskState.GENERATING: task.transition(TaskState.CANCELLED if isinstance(exc,GeneratorExit) else TaskState.FAILED)
            task.error=str(exc); task.model_calls.append({"model":settings.main_model,"latency_ms":(time.perf_counter()-started)*1000,"status":task.state.value,"error":getattr(exc,"category",None)}); db.save_task(task)
            if not isinstance(exc,GeneratorExit): yield "data: "+json.dumps({"type":"error","message":str(exc),"task":serialize_task(task)})+"\n\n"
        finally: cancel_events.pop(task.id,None)
    return StreamingResponse(events(), media_type="text/event-stream")

def serialize_task(task: Task): return task.__dict__|{"route":task.route.value if task.route else None,"state":task.state.value}

@app.post("/api/tasks/{task_id}/cancel")
def cancel(task_id:str):
    event=cancel_events.get(task_id)
    if not event: raise HTTPException(409,"task is not actively generating")
    event.set(); return {"task_id":task_id,"cancellation_requested":True,"scope":"provider response consumption and HTTP connection"}

@app.post("/api/tasks/{task_id}/confirmation")
def confirm(task_id:str,value:ConfirmationInput):
    try: task,response=runtime.resolve_confirmation(task_id,value.action_id,value.approve)
    except PermissionError as exc: raise HTTPException(409,str(exc)) from exc
    with db.connect() as conn: row=conn.execute("SELECT conversation_id FROM tasks WHERE id=?",(task_id,)).fetchone()
    if row and row[0]: db.add_message(row[0],"assistant",response,time.time(),str(uuid.uuid4()),task_id)
    return {"task":serialize_task(task),"response":response}


@app.post("/api/files/search")
def search(value: SearchInput):
    rows, method = retrieval.retrieve(value.query, value.limit)
    return {"method": method, "results": [x.__dict__ for x in rows]}


@app.post("/api/files/index")
def index(value: IndexInput):
    try: path = PathGuard(settings.allowed_roots).resolve(value.path)
    except PermissionError as exc: raise HTTPException(403, str(exc)) from exc
    if not path.is_file(): raise HTTPException(400, "path must be a file")
    try: text = path.read_text(errors="strict")
    except (OSError, UnicodeError) as exc: raise HTTPException(400, f"cannot read text file: {exc}") from exc
    if len(text) > 5_000_000: raise HTTPException(413, "file exceeds indexing limit")
    doc_id = db.index_document(str(path), path.name, text, {"size": path.stat().st_size}, time.time())
    semantic={"state":getattr(vectors,"state","disabled")}
    if settings.vector_enabled and isinstance(vectors,LocalVectorStore):
        try: semantic=vectors.index_document(doc_id,str(path),text)
        except (EmbeddingFailure,ValueError,PermissionError) as exc: semantic={"state":"error" if settings.embedding_model else "model_unavailable","error_category":getattr(exc,"category",type(exc).__name__)}
    return {"id": doc_id, "source": str(path),"semantic":semantic}


@app.get("/api/files")
def indexed_files():
    with db.connect() as conn: return {"roots": settings.allowed_roots, "documents": [dict(x) for x in conn.execute("SELECT id,source,title,indexed_at FROM documents ORDER BY indexed_at DESC")]}

@app.get("/api/files/{document_id}")
def file_detail(document_id:int):
    with db.connect() as conn: row=conn.execute("SELECT id,source,title,metadata_json,indexed_at,length(text) text_length FROM documents WHERE id=?",(document_id,)).fetchone()
    if not row: raise HTTPException(404,"document not found")
    return dict(row)

@app.get("/api/artifacts/{artifact_id}")
def read_artifact(artifact_id:str,offset:int=0,limit:int=Query(default=20,ge=1,le=100)):
    try:return artifacts.read(artifact_id,max(0,offset),min(100,limit))
    except KeyError as exc:raise HTTPException(404,str(exc)) from exc


@app.get("/api/tasks")
def tasks():
    with db.connect() as conn: return [dict(x) for x in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100")]


@app.get("/api/memory")
def memory():
    with db.connect() as conn:
        return {"conversations": [dict(x) for x in conn.execute("SELECT * FROM conversations ORDER BY created_at DESC")], "facts": [dict(x) for x in conn.execute("SELECT * FROM memory_facts ORDER BY created_at DESC")], "documents": [dict(x) for x in conn.execute("SELECT id,source,title,indexed_at FROM documents ORDER BY indexed_at DESC")]}


@app.delete("/api/memory/facts/{fact_id}")
def delete_fact(fact_id: int):
    with db.connect() as conn: deleted = conn.execute("DELETE FROM memory_facts WHERE id=?", (fact_id,)).rowcount
    if not deleted: raise HTTPException(404, "fact not found")
    return {"deleted": fact_id}

@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id:str):
    value=db.conversation(conversation_id)
    if not value: raise HTTPException(404,"conversation not found")
    return value

@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id:str):
    with db.connect() as conn: deleted=conn.execute("DELETE FROM conversations WHERE id=?",(conversation_id,)).rowcount
    if not deleted: raise HTTPException(404,"conversation not found")
    return {"deleted":conversation_id}


@app.get("/api/settings")
def get_settings(): return settings.public_dict()

@app.post("/api/settings/reload")
def reload_settings():
    """Refresh an existing OLCR backend from its authoritative settings DB."""
    rebuild(environment_settings.with_overrides(db.load_settings()))
    return settings.public_dict()

@app.get("/api/semantic/status")
def semantic_status(): return retrieval.semantic_telemetry

@app.put("/api/settings")
def put_settings(value:SettingsInput):
    try: candidate=settings.with_overrides(value.model_dump())
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    for key,item in value.model_dump().items(): db.save_setting(key,item,time.time())
    rebuild(candidate)
    return settings.public_dict()
