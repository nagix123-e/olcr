from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid
import re
from typing import Optional
import threading
import os
from urllib import request as urllib_request

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .artifacts import ArtifactStore
from .db import Database
from .ollama import OllamaProvider
from .web import fetch, search, brave_search, tavily_search, setup_guidance, provider_key, RUNTIME_PROVIDER_KEYS
from .retrieval import DisabledVectorStore, FileRetriever, FTSRetriever, PathGuard, RetrievalRouter
from .semantic import EmbeddingFailure, LocalVectorStore, OllamaEmbeddingProvider, OllamaIntentNormalizer, OllamaSemanticRelationEvaluator, QwenReranker
from .knowledge import CodingKnowledge, EXPECTED_EMBEDDING_MODEL
from .runtime import ContextManager, Runtime
from .conversation_memory import ConversationMemory
from .models import Route, Task, TaskState
from .ollama import ModelFailure
from .commands import catalog, resolve
from .external_tools import (ExternalToolError, REGISTRY, execute as execute_external_tool,
    route as route_external_tool, compile_provider_arguments, normalize_weather_arguments,
    normalize_research_arguments, normalize_wiki_arguments, status as external_tool_status)
from .coding_tasks import (MAX_RETRIES_PER_PHASE, MAX_SUBSTANTIAL_REPLANS_PER_TASK, coding_candidate,
                            classify_coding_request,
    completion_prompt, final_report_prompt, manager_review_prompt, model_slot, new_id, plan_prompt,
    plan_repair_prompt, report_has_authoritative_failure,
    validate_phase_report, validate_plan, extract_plan_json, plan_schema, coding_action_intent,
    normalize_manager_decision, evaluate_phase, classify_waiting_input, classify_execution_mode,
    compact_normal_plan, execution_mode_diagnostics, resumable_continuation_eligible,
    workspace_mutation_count, zero_mutation_retry_instruction, task_profile,
    required_mcp_contract, normalize_task_graph, canonical_coding_requirements, normalize_coding_requirements, animejs_project_version, animejs_version_compatibility,
    phase_has_observable_deliverable, coding_classification_diagnostics, classify_mutation_mode)
from .mcp_manifest import server_definition
from .mcp_runtime import MCPRuntime
from .node_mcp_runtime import launch_command as node_mcp_launch_command, resolve_mcp_resources as node_mcp_resource_status
from .interactive_planning import parse_pending_questions, resolve_short_reply
class RouterUnavailable(RuntimeError): pass


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    conversation_id: Optional[str] = None
    project_id: Optional[str] = None
    approved: bool = False
    core_context: Optional[str] = Field(default=None, max_length=50_000)
    image: Optional[dict] = None
    attachment: Optional[dict] = None
    external: Optional[dict] = None
    message_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=20, ge=1, le=200)


class IndexInput(BaseModel):
    path: str

class WebInput(BaseModel):
    url: str

class ConfirmationInput(BaseModel):
    action_id: str
    approve: bool

class SettingsInput(BaseModel):
    ollama_endpoint: str
    main_model: str = ""
    vision_model: str = "qwen2.5vl:3b"
    router_model: str = "gemma3:1b"
    embedding_model: str = ""
    semantic_judge_model: str = ""
    reranker_enabled: bool = False
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_threshold: float = 0.01
    allowed_roots: list[str]
    vector_enabled: bool = False
    conversation_memory_enabled: bool = True
    context_budget: int = Field(ge=256, le=200000)
    result_limit: int = Field(default=20, ge=1, le=200)
    confirmation_policy: str = "explicit"
    web_mode: str = "off"
    web_provider: str = "none"
    external_access_enabled: bool = False
    task_manager_enabled: bool = False

class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    workspace_path: Optional[str] = None

class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    workspace_path: Optional[str] = None
    archived: Optional[bool] = None

class CodingTaskUpdate(BaseModel):
    pause_requested: Optional[bool] = None
    archived: Optional[bool] = None
    resume: bool = False

class CoreContextInput(BaseModel):
    content: str = Field(default="", max_length=50_000)

class CommandInput(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)
    project_id: Optional[str] = None
    conversation_id: Optional[str] = None

class ConversationTitleInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)

class ContextFileInput(BaseModel):
    path: str

class WebSettingsInput(BaseModel):
    web_mode: str
    web_provider: str

class WebCredentialInput(BaseModel):
    provider: str
    key: str = Field(min_length=1, max_length=500)


environment_settings = Settings.from_env()
settings = environment_settings
db = Database(settings.db_path)
db.initialize()
context_migration = db.migrate_legacy_conversation_project_contexts()
db.recover_interrupted_coding_tasks()
persisted=db.load_application_settings()
print(f"SETTINGS_LOAD_COUNT={len(persisted)} SETTINGS_UNKNOWN_COUNT=0 LEGACY_PROJECT_CONTEXT_ROWS={context_migration['legacy_rows']} PROJECT_CONTEXT_MIGRATED={context_migration['migrated']} PROJECT_CONTEXT_MALFORMED={context_migration['malformed']} PROJECT_CONTEXT_STORE=conversation_project_contexts", file=__import__('sys').stderr, flush=True)
if persisted: settings=settings.with_overrides(persisted)
files = FileRetriever(settings.allowed_roots)
vectors = LocalVectorStore(db,OllamaEmbeddingProvider(settings.ollama_endpoint),settings.embedding_model,settings.allowed_roots) if settings.vector_enabled else DisabledVectorStore()
retrieval = RetrievalRouter(files, FTSRetriever(db), vectors, settings.vector_enabled, OllamaSemanticRelationEvaluator(settings.ollama_endpoint,settings.semantic_judge_model), OllamaIntentNormalizer(settings.ollama_endpoint,settings.semantic_judge_model), QwenReranker(settings.reranker_model,settings.reranker_enabled,settings.reranker_threshold) if settings.reranker_enabled else None, settings.reranker_threshold)
artifacts=ArtifactStore(str(Path(settings.db_path).parent/"artifacts"),db)
runtime = Runtime(settings, db, retrieval, OllamaProvider(settings.ollama_endpoint),artifacts)
conversation_memory = ConversationMemory(db, OllamaEmbeddingProvider(settings.ollama_endpoint), settings.embedding_model)
coding_knowledge = CodingKnowledge()
cancel_events: dict[str,threading.Event]={}
app = FastAPI(title="OLCR", version="0.6.0")
_gui_session_token=os.environ.get("OLCR_GUI_SESSION_TOKEN", "")
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "tauri://localhost"], allow_methods=["GET","POST","PUT","PATCH","DELETE"], allow_headers=["Content-Type","X-OLCR-Session"])

@app.middleware("http")
async def gui_session_guard(request: Request, call_next):
    if _gui_session_token and request.method != "OPTIONS" and request.url.path != "/api/health":
        if request.headers.get("X-OLCR-Session") != _gui_session_token:
            return __import__("fastapi").responses.JSONResponse({"detail":"missing or invalid GUI session credential"}, status_code=401)
    return await call_next(request)

MEMORY_OFF_CONSTRAINT = (
    "[MEMORY_DISABLED]\n"
    "Conversation memory is disabled for this request. Do not claim to remember "
    "or retrieve facts from previous conversations unless they are independently "
    "present in currently available context. If prior-conversation details are "
    "unavailable, say they cannot be verified because conversation memory is "
    "disabled or unavailable. Do not invent replacement facts. Current user "
    "message, core context, and explicitly supplied context remain usable."
)

def conversation_memory_constraint(enabled: bool) -> str:
    return "" if enabled else MEMORY_OFF_CONSTRAINT


def rebuild(candidate: Settings) -> None:
    global settings,files,vectors,retrieval,runtime,coding_knowledge
    settings=candidate; files=FileRetriever(settings.allowed_roots)
    vectors=LocalVectorStore(db,OllamaEmbeddingProvider(settings.ollama_endpoint),settings.embedding_model,settings.allowed_roots) if settings.vector_enabled else DisabledVectorStore()
    retrieval=RetrievalRouter(files,FTSRetriever(db),vectors,settings.vector_enabled,OllamaSemanticRelationEvaluator(settings.ollama_endpoint,settings.semantic_judge_model),OllamaIntentNormalizer(settings.ollama_endpoint,settings.semantic_judge_model),QwenReranker(settings.reranker_model,settings.reranker_enabled,settings.reranker_threshold) if settings.reranker_enabled else None,settings.reranker_threshold)
    runtime=Runtime(settings,db,retrieval,OllamaProvider(settings.ollama_endpoint),artifacts)
    global conversation_memory
    conversation_memory=ConversationMemory(db, OllamaEmbeddingProvider(settings.ollama_endpoint), settings.embedding_model)
    coding_knowledge=CodingKnowledge()


def coding_knowledge_context_for_subtask(request: str, workspace_root: str | None, provider: OllamaEmbeddingProvider | None = None) -> str:
    """Return bounded release knowledge for one phase, or an empty safe fallback.

    This deliberately accepts no project contents or task history.  The
    workspace is consulted only for small dependency manifests when selecting
    stack metadata; no such information is persisted in the knowledge index.
    """
    provider = provider or OllamaEmbeddingProvider(settings.ollama_endpoint)
    # Do not contact Ollama at all when the immutable release artifact is not
    # installed.  This keeps the existing development fallback fast and
    # ensures Coding Tasks remain usable with KNOWLEDGE_STATUS=NOT_AVAILABLE.
    if not coding_knowledge.index.available:
        print("CODING_KNOWLEDGE_STATUS=NOT_AVAILABLE CODING_KNOWLEDGE_REASON=" + coding_knowledge.index.reason + " CODING_KNOWLEDGE_CORE_RULES=0 CODING_KNOWLEDGE_DETAILED=0 CODING_KNOWLEDGE_RERANKER=NOT_USED", file=__import__("sys").stderr, flush=True)
        return ""
    try:
        identity = provider.model_identity(EXPECTED_EMBEDDING_MODEL)
    except Exception as exc:
        identity = None
        identity_reason = getattr(exc, "category", type(exc).__name__)
    else:
        identity_reason = "AVAILABLE"
    result = coding_knowledge.context_for(request, workspace_root, provider, identity)
    context = coding_knowledge.format_context(result)
    print(
        f"CODING_KNOWLEDGE_STATUS={result.status} CODING_KNOWLEDGE_REASON={result.reason or identity_reason} "
        f"CODING_KNOWLEDGE_CORE_RULES={len(result.core_rules)} CODING_KNOWLEDGE_DETAILED={len(result.records)} "
        "CODING_KNOWLEDGE_RERANKER=NOT_USED",
        file=__import__("sys").stderr, flush=True,
    )
    return context


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version, "bind_scope":"loopback_only", "app_support":str(Path(settings.db_path).parent), "db_path":settings.db_path, "model_configuration": "ready" if settings.main_model else "not_ready", "router_model": settings.router_model, "session_auth_required":bool(_gui_session_token)}

@app.get("/api/models/status")
def model_status():
    installed=[]
    try:
        with urllib_request.urlopen(settings.ollama_endpoint.rstrip("/")+"/api/tags", timeout=2) as response:
            payload=json.load(response); installed=[str(x.get("name")) for x in payload.get("models", []) if isinstance(x,dict)]
    except Exception:
        pass
    return {"router_model":settings.router_model, "router_installed":settings.router_model in installed, "installed":installed}

def valid_workspace(path: str | None) -> str | None:
    if path is None or not path.strip(): return None
    candidate=Path(path).expanduser().resolve()
    if not candidate.is_dir(): raise HTTPException(422,"workspace must be an existing directory")
    return str(candidate)

def project_context(project_id: str) -> str:
    return str(db.load_settings().get("project_core_context:"+project_id, ""))

def _conversation_project_context(conversation_id: str, project_id: str, message: str = "") -> dict:
    """Persist only user-established, bounded subject facts per conversation."""
    current=db.load_conversation_project_context(conversation_id)
    project=db.project(project_id) or {}
    value={**current,"project_id":project_id,"project_name":project.get("name",""),"workspace_path":project.get("workspace_path")}
    provenance=dict(current.get("provenance") or {})
    text=(message or "")
    if re.search(r"tetris|テトリス", text, re.I):
        value["current_subject"]="Tetris web game"
        value["project_type"]="web game"
        provenance.update({"current_subject":"USER_CONFIRMED", "project_type":"USER_CONFIRMED"})
    elif re.search(r"(?:別件|別の).*?(?:olcr|webサイト|website)|(?:olcr).*(?:webサイト|website|サイト)", text, re.I):
        value["current_subject"]="OLCR website"
        value["project_type"]="website"
        provenance.update({"current_subject":"USER_CONFIRMED", "project_type":"USER_CONFIRMED"})
    elif not value.get("current_subject") and value.get("project_name"):
        # The project record is authoritative even before a conversation has
        # accumulated enough prose to establish a narrower subject.
        project_name=str(value["project_name"]).strip()
        value["current_subject"]=("Tetris project" if re.search(r"tetris|テトリス", project_name, re.I)
                                  else project_name + " project")
        provenance["current_subject"]="PROJECT_METADATA"
    if re.search(r"react", text, re.I):
        stack=list(value.get("known_stack") or [])
        if "React" not in stack: stack.append("React")
        value["known_stack"]=stack[:8]
        provenance["known_stack"]="USER_CONFIRMED"
    if re.search(r"開始しない|動かない|start.*not|not.*start", text, re.I) and value.get("current_subject"):
        value["recent_observation"]="The current project does not start."
        provenance["recent_observation"]="USER_CONFIRMED"
    if message:
        value["current_goal"]=text[:500]
        provenance["current_goal"]="USER_CONFIRMED"
    value["provenance"]=provenance
    value["revision"]=int(current.get("revision",0))+1 if message else int(current.get("revision",0))
    db.save_conversation_project_context(conversation_id, value, time.time())
    return value

def _project_context_prompt(context: dict) -> str:
    if not context.get("project_id"): return ""
    safe={key:value for key,value in context.items() if key in {"project_id","project_name","workspace_path","project_type","current_subject","known_stack","important_files","current_goal","confirmed_decisions","open_questions","recent_observation","provenance","revision"} and value}
    return "\n[ACTIVE_PROJECT_CONTEXT]\n"+json.dumps(safe,ensure_ascii=False)+"\nTreat this as authoritative project grounding. Resolve ambiguous references against this project first; use generic troubleshooting only after project-consistent explanations, and do not ask for facts already present here. Do not introduce unrelated emulator, ROM, or console hypotheses without evidence.\n[/ACTIVE_PROJECT_CONTEXT]\n"

def _coding_task_context(conversation_id: str) -> str:
    """Expose the one current coding task, without task logs or full plans."""
    task=next((item for item in db.coding_tasks(conversation_id) if not item.get("archived")), None)
    if not task: return ""
    safe={key:task.get(key) for key in ("id", "original_goal", "status", "activity", "current_phase_id", "recovery_action", "recovery_reason", "plan_revision") if task.get(key) not in (None, "", "NONE")}
    return "\n[ACTIVE_CODING_TASK_STATE]\n"+json.dumps(safe, ensure_ascii=False)+"\n[/ACTIVE_CODING_TASK_STATE]\n"

def _planning_context_prompt(session: dict | None) -> str:
    if not session: return ""
    safe={"revision":session.get("planning_revision"), "status":session.get("status"),
          "decisions":session.get("decisions") or {}, "open_questions":[item.get("question") for item in (session.get("pending_questions") or [])][:8],
          "format_state":session.get("format_state") or {}}
    return "\n[INTERACTIVE_PLANNING_STATE]\n"+json.dumps(safe, ensure_ascii=False)+"\n[/INTERACTIVE_PLANNING_STATE]\n"

def _record_planning_decisions(conversation_id: str, project_id: str, session: dict | None) -> None:
    if not session or not session.get("decisions"):
        return
    context=_conversation_project_context(conversation_id, project_id)
    context["confirmed_decisions"]={str(key): str(value)[:200] for key, value in session["decisions"].items()}
    provenance=dict(context.get("provenance") or {})
    provenance["confirmed_decisions"]="PLANNING_DECISION"
    context["provenance"]=provenance
    context["revision"]=int(context.get("revision", 0))+1
    db.save_conversation_project_context(conversation_id, context, time.time())

def active_conversation_context(conversation_id: str, limit: int = 8) -> str:
    record = db.conversation(conversation_id)
    if not record: return ""
    messages = record.get("messages", [])[-limit:]
    if not messages: return ""
    return "\n[ACTIVE_CONVERSATION]\n" + "\n".join(f"{m['role']}: {m['content'][:4000]}" for m in messages)


def _planning_format_state(conversation_id: str) -> dict[str, bool]:
    """Preserve explicit presentation opt-outs independently from choices."""
    transcript = active_conversation_context(conversation_id, limit=20)
    return {"SIL": not bool(re.search(r"\bSIL\s*=\s*(?:false|off|no|0)\b", transcript, re.I)),
            "SUI": not bool(re.search(r"\bSUI\s*=\s*(?:false|off|no|0)\b", transcript, re.I))}


def _render_pending_questions(questions: list[dict]) -> str:
    return "\n".join(
        f"{question['id']}: {question.get('question','')}\n" +
        "\n".join(f"{key}. {label}" for key, label in (question.get("options") or {}).items())
        for question in questions)


def _resolve_interactive_planning_reply(conversation_id: str, message: str) -> tuple[dict | None, str | None]:
    session = db.active_interactive_planning(conversation_id)
    resolution = resolve_short_reply(message, list(session.get("pending_questions") or [])) if session else resolve_short_reply(message, [])
    if not resolution:
        return session, None
    if resolution["kind"] == "NO_PENDING":
        return None, "現在選択できる計画質問がありません。どの計画についての選択か指定してください。"
    if resolution["kind"] == "INCOMPATIBLE":
        return session, "現在の保留質問では、その選択肢を一意に適用できません。残っている質問だけ指定してください。\n" + _render_pending_questions(session.get("pending_questions") or [])
    pending = list(session.get("pending_questions") or [])
    answered = list(session.get("answered_questions") or [])
    assumptions = list(session.get("assumptions") or [])
    decisions = dict(session.get("decisions") or {})
    answer_count = 0
    for question in pending[:]:
        answer = resolution.get("answers", {}).get(str(question.get("id")))
        if not answer:
            continue
        choice, source = answer
        question["state"] = "ANSWERED"
        answered.append({"id": question["id"], "answer": choice, "answer_source": source})
        decisions[question["id"]] = choice
        if source == "ASSUMPTION_RECOMMENDED":
            assumptions.append({"question_id": question["id"], "choice": choice, "source": source})
        pending.remove(question); answer_count += 1
    status = "COMPLETE" if not pending else "ACTIVE"
    session = db.update_interactive_planning(session["id"], status=status, pending_questions=pending,
                                              answered_questions=answered, assumptions=assumptions, decisions=decisions) or session
    print(f"PLANNING_SESSION_ACTIVE=YES PLANNING_REVISION={session.get('planning_revision')} "
          f"PENDING_QUESTION_COUNT={len(pending) + answer_count} SHORT_REPLY_DETECTED=YES "
          f"SHORT_REPLY_RESOLUTION=RESOLVED QUESTIONS_ANSWERED={answer_count} QUESTIONS_REMAINING={len(pending)}",
          file=__import__('sys').stderr, flush=True)
    if pending:
        return session, "選択を反映しました。残りの計画質問に回答してください。\n" + _render_pending_questions(pending)
    summary = "\n".join(f"- {item['id']}: {item['answer']}" for item in answered)
    return session, "選択を反映しました。開発計画の決定事項は次のとおりです。\n" + summary


def _persist_interactive_planning_questions(conversation_id: str, response: str) -> None:
    questions = parse_pending_questions(response)
    if not questions:
        return
    session = db.create_interactive_planning(conversation_id, questions, _planning_format_state(conversation_id))
    print(f"PLANNING_SESSION_ACTIVE=YES PLANNING_REVISION={session.get('planning_revision')} "
          f"PENDING_QUESTION_COUNT={len(questions)} SHORT_REPLY_DETECTED=NO SHORT_REPLY_RESOLUTION=NOT_APPLICABLE "
          f"QUESTIONS_ANSWERED=0 QUESTIONS_REMAINING={len(questions)}", file=__import__('sys').stderr, flush=True)

@app.get("/api/projects")
def list_projects(): return {"projects":db.projects()}

@app.get("/api/conversations/{conversation_id}/coding-tasks")
def list_coding_tasks(conversation_id: str): return {"tasks":db.coding_tasks(conversation_id)}

@app.patch("/api/coding-tasks/{task_id}")
def update_coding_task(task_id: str, value: CodingTaskUpdate):
    task=db.coding_task(task_id)
    if not task: raise HTTPException(404,"coding task not found")
    updates={}
    if value.pause_requested is not None: updates["pause_requested"]=value.pause_requested
    if value.archived is not None: updates["archived"]=value.archived
    if value.resume:
        if not resumable_continuation_eligible(task): raise HTTPException(409,"task is not safely resumable or requires authorization")
        if updates: db.update_coding_task(task_id,**updates)
        replan_epoch_resume = _begin_human_recovery_epoch(task)
        result=db.enqueue_coding_task(task_id); _coding_scheduler_wake.set()
        print(f"TASK_ID={task_id} RESUME_REQUESTED=true RESUME_ACCEPTED=true STATUS_BEFORE_RESUME=RESUMABLE STATUS_AFTER_RESUME=QUEUED RECOVERY_ACTION={task.get('recovery_action','NONE')} RECOVERY_REASON={task.get('recovery_reason','NONE')} RECOVERY_EPOCH={result.get('recovery_epoch',0) if result else task.get('recovery_epoch',0)} REPLAN_COUNT_FOR_NEW_EPOCH={result.get('replan_count_in_epoch','unchanged') if result else 'unchanged'} NEXT_TASK_ACTION=QUEUE_FIFO",file=__import__('sys').stderr,flush=True)
        return result
    if value.pause_requested is True:
        result=db.dequeue_coding_task(task_id)
        return db.update_coding_task(task_id,pause_requested=True,archived=updates.get("archived",task.get("archived")),recovery_action="RECOVERY_REVIEW",recovery_reason="PAUSED") or result
    if value.archived is True and task["status"] == "QUEUED":
        db.dequeue_coding_task(task_id)
    result=db.update_coding_task(task_id,**updates)
    if value.pause_requested is False: _coding_scheduler_wake.set()
    return result

@app.get("/api/coding-tasks/{task_id}/phase-reports")
def coding_task_phase_reports(task_id: str):
    if not db.coding_task(task_id): raise HTTPException(404,"coding task not found")
    return {"reports":db.coding_phase_reports(task_id)}

@app.get("/api/coding-tasks/{task_id}")
def get_coding_task(task_id: str):
    task=db.coding_task(task_id)
    if not task: raise HTTPException(404,"coding task not found")
    task["queue_position"]=db.coding_task_queue_position(task_id)
    return task

def _pause_at_checkpoint(task_id: str) -> bool:
    task=db.coding_task(task_id)
    if not task or task.get("pause_requested"):
        if task: db.update_coding_task(task_id,status="RESUMABLE",activity="NONE",queue_order=None,recovery_action="RECOVERY_REVIEW",recovery_reason="PAUSED")
        print(f"TASK_ID={task_id} PAUSE_REQUESTED=true TASK_STATUS=RESUMABLE",file=__import__('sys').stderr,flush=True)
        return True
    return False

def _transition_resumable(task_id: str, failure_stage: str, failure_class: str, recovery_action: str = "RECOVERY_REVIEW", exc: Exception | None = None, recovery_reason: str | None = None) -> None:
    before=db.coding_task(task_id) or {}
    reason=recovery_reason or failure_stage
    db.update_coding_task(task_id,status="RESUMABLE",activity="NONE",queue_order=None,recovery_action=recovery_action,recovery_reason=reason)
    print(f"TASK_ID={task_id} STATUS_BEFORE={before.get('status','UNKNOWN')} ACTIVITY_BEFORE={before.get('activity','UNKNOWN')} FAILURE_STAGE={failure_stage} FAILURE_CLASS={failure_class} EXCEPTION_TYPE={type(exc).__name__ if exc else 'NONE'} RECOVERY_ACTION={recovery_action} RECOVERY_REASON={reason} STATUS_AFTER=RESUMABLE",file=__import__('sys').stderr,flush=True)

def _begin_human_recovery_epoch(task: dict) -> bool:
    """Grant one fresh automatic-replan budget only for an explicit Resume.

    The task remains the source of truth; callers must enqueue it after this
    transition.  Other resumable reasons retain their existing epoch/budget.
    """
    if task.get("recovery_action") != "REPLAN_CONTINUATION" or task.get("recovery_reason") != "REPLAN_LIMIT":
        return False
    db.update_coding_task(task["id"], recovery_epoch=int(task.get("recovery_epoch") or 0) + 1, replan_count_in_epoch=0)
    return True

def _model_text(task_id: str, status: str, activity: str, model: str, messages: list[dict], structured_schema: dict | None = None) -> str | None:
    if _pause_at_checkpoint(task_id): return None
    db.update_coding_task(task_id,status=status,activity=activity)
    print(f"TASK_ID={task_id} TASK_STATUS={status} TASK_ACTIVITY={activity} MODEL_SLOT_OWNER={task_id} MODEL_NAME={model}",file=__import__('sys').stderr,flush=True)
    with model_slot():
        try:
            raw=runtime.model.generate(messages,model,think=False,format=structured_schema) if structured_schema is not None else runtime.model.generate(messages,model,think=False)
        except TypeError:
            # Deterministic test doubles and older compatible providers may not
            # expose the optional Ollama format argument.
            raw=runtime.model.generate(messages,model,think=False)
    return raw.get("text","") if isinstance(raw,dict) else ""

def _phase_status(plan: dict, phase_id: str, status: str) -> dict:
    revised=json.loads(json.dumps(plan))
    for item in revised.get("phases",[]):
        if item.get("id")==phase_id: item["status"]=status
    return revised

_SUBTASK_STATES={"WAITING","RUNNING","DONE","FAILED","PARTIAL"}
_SUBTASK_VERIFICATION={"PASS","FAILED","NOT_RUN","UNVERIFIED"}

def _subtask_entries(plan: dict, prior: list[dict] | None = None, completed_phase_ids: set[str] | None = None) -> list[dict] | None:
    """Create only plan-explicit graph records; legacy plans remain absent."""
    tasks=plan.get("tasks") if isinstance(plan,dict) else None
    phases={phase.get("id") for phase in plan.get("phases",[]) if isinstance(phase,dict)} if isinstance(plan,dict) else set()
    if not isinstance(tasks,list) or not tasks or any(not isinstance(item,dict) or item.get("phase_id") not in phases for item in tasks):
        return None
    prior_by_id={str(item.get("task_id")):item for item in (prior or []) if isinstance(item,dict)}
    completed_phase_ids=completed_phase_ids or set()
    entries=[]
    for graph_task in tasks:
        task_id=str(graph_task["task_id"]); phase_id=str(graph_task["phase_id"]); old=prior_by_id.get(task_id)
        if old and old.get("phase_id")==phase_id and phase_id in completed_phase_ids and old.get("status")=="DONE":
            entries.append(old)
        else:
            entries.append({"task_id":task_id,"phase_id":phase_id,"status":"WAITING","verification_status":"UNVERIFIED","attempt":0,"started_at":None,"finished_at":None,"failure_summary":None})
    return entries

def _initialize_subtask_progress(task_id: str, plan: dict, *, preserve: bool = False, completed_phase_ids: set[str] | None = None) -> None:
    current=db.coding_task(task_id) or {}
    entries=_subtask_entries(plan,current.get("subtask_progress") if preserve else None,completed_phase_ids)
    db.update_coding_task(task_id,subtask_progress=entries)

def _subtask_verification(report: dict | None, decision: str | None = None) -> str:
    if not isinstance(report,dict): return "UNVERIFIED"
    if report.get("status") in {"FAIL","BLOCKED"} or report.get("errors") or report.get("blockers") or report.get("test_fail") or report.get("build_pass")=="FAIL": return "FAILED"
    if report.get("status")=="NOT_RUN" or report.get("build_executed")=="NOT_RUN" or report.get("build_pass")=="NOT_RUN": return "NOT_RUN"
    return "PASS" if decision=="PASS" else "UNVERIFIED"

def _set_subtask_state(task_id: str, phase_id: str, status: str, *, attempt: int | None = None, report: dict | None = None, decision: str | None = None) -> None:
    """Persist a phase-bound graph state at the existing scheduler boundary."""
    if status not in _SUBTASK_STATES: raise ValueError("invalid subtask status")
    task=db.coding_task(task_id) or {}; entries=task.get("subtask_progress")
    if not isinstance(entries,list): return
    matches=[entry for entry in entries if isinstance(entry,dict) and entry.get("phase_id")==phase_id]
    if len(matches)!=1: return
    if status=="RUNNING" and any(entry.get("status")=="RUNNING" and entry.get("phase_id")!=phase_id for entry in entries if isinstance(entry,dict)):
        raise RuntimeError("multiple running subtasks are not permitted")
    now=time.time(); entry=matches[0]; updated={**entry,"status":status}
    if attempt is not None: updated["attempt"]=attempt
    if status=="RUNNING": updated["started_at"]=entry.get("started_at") or now; updated["finished_at"]=None
    if status in {"DONE","FAILED","PARTIAL"}: updated["finished_at"]=now
    if status=="DONE": updated["verification_status"]=_subtask_verification(report,decision); updated["failure_summary"]=None
    elif status in {"FAILED","PARTIAL"}:
        updated["verification_status"]=_subtask_verification(report,decision)
        details=report or {}
        summary=next((str(values[0]) for key in ("errors","blockers","test_fail") if isinstance((values:=details.get(key)),list) and values),"")
        updated["failure_summary"]=summary[:500] or None
    progress=[updated if item is entry else item for item in entries]
    db.update_coding_task(task_id,subtask_progress=progress)

def _typed_summary(execution: Task) -> dict:
    operations=[]
    for item in execution.tool_executions:
        operations.append({"tool":item.get("tool"),"status":item.get("status"),"output":item.get("output"),"error":item.get("error")})
    return {"state":execution.state.value,"error":execution.error,"operations":operations}

def _report_from_execution(phase: dict, attempt: int, execution: Task, response: str) -> dict:
    typed=_typed_summary(execution)
    write_tools={"workspace_write","workspace_write_normalized","workspace_patch"}
    wrote=any(item.get("tool") in write_tools and item.get("status")=="success" for item in execution.tool_executions)
    web_only_zero_write=(execution.state == TaskState.COMPLETED and execution.route != Route.IMPLEMENTATION and not wrote)
    if web_only_zero_write:
        # A managed implementation phase can never treat a normal-chat/web
        # response as successful implementation evidence.
        print("CODING_IMPLEMENTATION_ZERO_WRITE_WEB_ROUTE=true",file=__import__('sys').stderr,flush=True)
    failed=execution.state in {TaskState.FAILED,TaskState.DENIED} or bool(execution.error) or web_only_zero_write
    changed=[]
    for item in execution.tool_executions:
        output=item.get("output") or {}
        if item.get("tool") in {"workspace_write","workspace_write_normalized"} and output.get("path"):
            changed.append(output["path"])
    return {"phase_id":phase["id"],"attempt":attempt,"status":"FAIL" if failed else "PASS",
            "implemented":[response[:400]] if response else [],"changed_files":changed,
            "test_executed":[],"test_pass":[],"test_fail":[],"build_executed":"NOT_RUN",
            "build_pass":"NOT_RUN","errors":([execution.error] if execution.error else []) + (["CODING_IMPLEMENTATION_ZERO_WRITE_WEB_ROUTE"] if web_only_zero_write else []),"blockers":[],"risks":[],
            "typed_execution_summary":typed}

def _save_report(task_id: str, phase_id: str, attempt: int, report: dict, validation_status: str) -> dict:
    db.add_coding_phase_report(task_id,phase_id,attempt,report,validation_status,time.time())
    return db.coding_phase_reports(task_id)[-1]


def _bounded_review_context(report: dict) -> dict:
    """Return only the small, typed portion needed for a retry prompt."""
    decision = report.get("manager_decision") or {}
    diagnosis = decision.get("diagnosis") if isinstance(decision, dict) else {}
    if not isinstance(diagnosis, dict):
        diagnosis = {}
    typed_summary = report.get("typed_execution_summary") if isinstance(report.get("typed_execution_summary"), dict) else {}
    return {
        "decision": str(decision.get("decision") or "")[:80],
        "reason": str(decision.get("reason") or "")[:500],
        "diagnosis": {
            "phase_id": str(diagnosis.get("phase_id") or report.get("phase_id") or "")[:120],
            "plan_revision": int(diagnosis.get("plan_revision") or report.get("plan_revision") or 0),
            "attempt": int(diagnosis.get("attempt") or report.get("attempt") or 0),
            "unmet_done": [str(x)[:240] for x in (diagnosis.get("unmet_done") or [])[:8]],
            "unmet_verify": [str(x)[:240] for x in (diagnosis.get("unmet_verify") or [])[:8]],
            "evidence": [str(x)[:240] for x in (diagnosis.get("evidence_used") or diagnosis.get("evidence") or [])[:8]],
            "evidence_used": [str(x)[:240] for x in (diagnosis.get("evidence_used") or diagnosis.get("evidence") or [])[:8]],
            "failure_class": str(diagnosis.get("failure_class") or "RECOVERABLE_INTERNAL")[:80],
            "retry_instruction": str(diagnosis.get("retry_instruction") or "")[:500],
            "specific_fix": str(diagnosis.get("specific_fix") or diagnosis.get("retry_instruction") or "")[:500],
            "verification_instruction": str(diagnosis.get("verification_instruction") or "")[:500],
            "replan_instruction": str(diagnosis.get("replan_instruction") or "")[:500],
            "replan_reason": str(diagnosis.get("replan_reason") or diagnosis.get("replan_instruction") or "")[:500],
            "replan_instruction_source": str(diagnosis.get("replan_instruction_source") or "derived")[:16],
            "replan_reason_source": str(diagnosis.get("replan_reason_source") or "derived")[:16],
        },
        "typed_state": str(typed_summary.get("state") or "")[:80],
        "typed_error": str(typed_summary.get("error") or "")[:500],
    }


def _context_fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _evidence_counts(report: dict, typed_summary: dict) -> tuple[int, int]:
    operations=[item for item in (typed_summary.get("operations") or []) if isinstance(item,dict)]
    successful=[item for item in operations if str(item.get("status") or "").lower() in {"success", "completed", "ok"}]
    done=len(report.get("changed_files") or []) + sum(1 for item in successful if item.get("tool") in {"workspace_write", "workspace_write_normalized", "workspace_patch"})
    verify=sum(1 for item in successful if item.get("tool") in {"workspace_read", "workspace_read_normalized"})
    verify += len(report.get("test_pass") or []) + (1 if report.get("build_pass") == "PASS" else 0)
    return done, verify


def _phase_signature(phase: dict) -> str:
    payload = {key: phase.get(key) for key in ("goal", "done", "verify", "dependencies", "risks")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

def _review_phase(task_id: str, managed: dict, phase: dict, report_row: dict, completed: list[dict], workspace_root: str | None = None) -> str | None:
    report=report_row["structured_report"]
    typed=report.get("typed_execution_summary") if isinstance(report.get("typed_execution_summary"),dict) else {}
    prior_reports=[row["structured_report"] for row in db.coding_phase_reports(task_id)
                   if row.get("id") != report_row.get("id") and row.get("phase_id") == phase.get("id")]
    evaluation=evaluate_phase(phase,report,prior_reports,workspace_root)
    report["phase_evaluation"]={key:value for key,value in evaluation.items() if key != "file_state"}
    done_evidence_count, verify_evidence_count = _evidence_counts(report, typed)
    print(f"TASK_ID={task_id} TASK_ACTIVITY=PHASE_EVALUATION PHASE_REPORT_VALID=true "
          f"DONE_EVIDENCE_COUNT={done_evidence_count} VERIFY_EVIDENCE_COUNT={verify_evidence_count} "
          f"GEMMA_EVIDENCE_COUNT={len((typed.get('operations') or []))}",
          file=__import__('sys').stderr,flush=True)
    print(f"TASK_ID={task_id} PHASE_EVALUATION_COMPLETE={'true' if evaluation['phase_complete'] else 'false'} DONE_TOTAL={evaluation['done_total']} DONE_SATISFIED={evaluation['done_satisfied']} VERIFY_TOTAL={evaluation['verify_total']} VERIFY_SATISFIED={evaluation['verify_satisfied']} AUTHORITATIVE_FAILURE={'true' if evaluation['authoritative_failure_present'] else 'false'} REQUIRED_NOT_RUN={'true' if evaluation['required_not_run_present'] else 'false'} ALLOWED_DECISIONS={json.dumps(evaluation['allowed_decisions'])}",file=__import__('sys').stderr,flush=True)
    # Coding Orchestrator decisions are derived from typed evidence.  The
    # lightweight router model remains available to unrelated external-tool
    # routing, but is never a Coding phase manager.
    if evaluation["phase_complete"]:
        decision = {"decision": "PASS", "reason": "typed phase evidence is complete"}
    elif evaluation["authorization_blocker_present"]:
        decision = {"decision": "NEED_USER", "reason": "authorization is required"}
    elif evaluation["external_blocker_present"]:
        decision = {"decision": "BLOCKED", "reason": "external condition prevents progress"}
    elif int(report.get("attempt", 0)) < MAX_RETRIES_PER_PHASE:
        decision = {"decision": "RETRY", "reason": "recoverable phase evidence is incomplete"}
    else:
        decision = {"decision": "REPLAN_REQUIRED", "reason": "phase retry budget exhausted"}
    report["manager_decision"] = decision
    report["manager_diagnostics"] = {"raw_decision": None, "decision_valid": True,
                                      "effective_decision": decision["decision"], "phase_complete": evaluation["phase_complete"]}
    # Persist the deterministic decision before callers advance the plan.  The
    # completion path reloads reports from storage and treats this decision as
    # the authority for a completed phase.
    db.update_coding_phase_report(report_row["id"], report, "PASS")
    print(f"TASK_ID={task_id} CODING_GEMMA_PHASE_REVIEW=REMOVED MANAGER_DECISION={decision['decision']}", file=__import__('sys').stderr, flush=True)
    return decision["decision"]
    raw=_model_text(task_id,"RUNNING","GEMMA_REVIEW",settings.router_model,[
        {"role":"system","content":"You are OLCR Gemma review mode. Return only the required JSON."},
        {"role":"user","content":manager_review_prompt(managed["original_goal"],phase,report,typed,completed,report.get("attempt",0),managed.get("approved_scopes",[]),managed.get("pending_authorization"))},
    ])
    if raw is None: return None
    try: decision=json.loads(raw)
    except Exception: decision={}
    normalized=normalize_manager_decision(decision,phase,report,typed)
    if normalized is None:
        # A malformed review is recoverable.  Preserve the existing one-shot
        # report path without ever turning an internal parser failure into a
        # false external blocker.
        attempt=int(report.get("attempt",0))
        decision={"decision":"RETRY" if attempt < MAX_RETRIES_PER_PHASE else "REPLAN_REQUIRED",
                  "reason":"manager decision schema invalid"}
        decision=normalize_manager_decision(decision,phase,report,typed) or decision
    else:
        decision=normalized
    raw_decision=str(decision.get("decision") or "")
    invalid_reason=""
    if evaluation["phase_complete"]:
        diagnosis_from_model=decision.get("diagnosis") if isinstance(decision.get("diagnosis"),dict) else {}
        # With no typed completion evidence at all, preserve the existing
        # advisory retry path when Gemma names concrete unmet criteria.  Once
        # authoritative Done/Verify evidence exists, the envelope collapses to
        # PASS and a contradictory retry is rejected deterministically.
        evidence_present=done_evidence_count > 0 or verify_evidence_count > 0
        advisory_retry=(raw_decision == "RETRY" and not evidence_present)
        if raw_decision != "PASS" and not advisory_retry and evidence_present:
            invalid_reason="NO_UNMET_CRITERIA" if raw_decision == "RETRY" else "PHASE_COMPLETE_ONLY_PASS"
            decision=normalize_manager_decision({"decision":"PASS","reason":"deterministic phase evaluation is complete"},phase,report,typed) or decision
    elif raw_decision == "PASS":
        invalid_reason="PHASE_NOT_COMPLETE"
        decision=normalize_manager_decision({"decision":"REPLAN_REQUIRED","reason":"required typed criteria remain unmet"},phase,report,typed) or decision
    elif raw_decision == "RETRY" and not (evaluation["done_unmet"] or evaluation["verify_unmet"] or evaluation["authoritative_failure_present"] or evaluation["required_not_run_present"]):
        invalid_reason="NO_UNMET_CRITERIA"
        decision=normalize_manager_decision({"decision":"PASS","reason":"deterministic phase evaluation is complete"},phase,report,typed) or decision
    elif raw_decision == "REPLAN_REQUIRED":
        diagnosis=decision.get("diagnosis") if isinstance(decision.get("diagnosis"),dict) else {}
        structural=bool(diagnosis.get("replan_instruction") or diagnosis.get("replan_reason") or any(term in str(decision.get("reason") or "").lower() for term in ("structur", "unobservable", "unsatisfiable")))
        unresolved=bool(evaluation["done_unmet"] or evaluation["verify_unmet"] or evaluation["authoritative_failure_present"] or evaluation["required_not_run_present"])
        if not unresolved:
            invalid_reason="NO_UNRESOLVED_FAILURE"
            decision=normalize_manager_decision({"decision":"PASS","reason":"deterministic phase evaluation is complete"},phase,report,typed) or decision
        elif not structural:
            invalid_reason="REPLAN_NOT_JUSTIFIED"
            decision=normalize_manager_decision({"decision":"RETRY","reason":"unmet criteria are recoverable within the phase"},phase,report,typed) or decision
    elif raw_decision == "NEED_USER" and not evaluation["authorization_blocker_present"]:
        invalid_reason="NO_USER_INPUT_REQUIRED"
        decision=normalize_manager_decision({"decision":"RETRY","reason":"no user decision is required for this evidence"},phase,report,typed) or decision
    typed_status=str(typed.get("state") or "")
    external_blocker=typed_status==TaskState.DENIED.value or any(
        isinstance(op,dict) and str(op.get("status","")).upper() in {"FORBIDDEN_BY_USER","PERMISSION_DENIED","SAFETY_DENIED"}
        for op in (typed.get("operations") or []))
    if decision.get("decision")=="BLOCKED" and not external_blocker:
        attempt=int(report.get("attempt",0))
        decision={"decision":"RETRY" if attempt < MAX_RETRIES_PER_PHASE else "REPLAN_REQUIRED","reason":"internal phase evidence is recoverable"}
        decision=normalize_manager_decision(decision,phase,report,typed) or decision
    if external_blocker and decision.get("decision")=="BLOCKED" and isinstance(decision.get("diagnosis"),dict):
        decision["diagnosis"]["failure_class"]="EXTERNAL_BLOCKER"
    print(f"TRUSTED_EXTERNAL_BLOCKER={'true' if external_blocker else 'false'} FAILURE_CLASS={'EXTERNAL_BLOCKER' if external_blocker and decision.get('decision')=='BLOCKED' else 'RECOVERABLE_INTERNAL'}",file=__import__('sys').stderr,flush=True)
    if decision["decision"]=="PASS" and not evaluation["phase_complete"]:
        decision=normalize_manager_decision({"decision":"REPLAN_REQUIRED","reason":"typed evidence does not support PASS"},phase,report,typed) or decision
    report["manager_decision"]=decision
    report["manager_diagnostics"]={"raw_decision":raw_decision,"decision_valid":not bool(invalid_reason),
                                    "invalid_reason":invalid_reason or None,
                                    "effective_decision":decision.get("decision",""),
                                    "phase_complete":evaluation["phase_complete"]}
    print(f"TASK_ID={task_id} MANAGER_RAW_DECISION={raw_decision} MANAGER_DECISION_VALID={'false' if invalid_reason else 'true'} MANAGER_DECISION_INVALID_REASON={invalid_reason or 'NONE'} PHASE_EVALUATION_COMPLETE={'true' if evaluation['phase_complete'] else 'false'} MANAGER_EFFECTIVE_DECISION={decision.get('decision','')}",file=__import__('sys').stderr,flush=True)
    db.update_coding_phase_report(report_row["id"],report,"PASS")
    diagnosis=decision.get("diagnosis") or {}
    print(f"TASK_ID={task_id} TASK_ACTIVITY=GEMMA_REVIEW MANAGER_DECISION={decision['decision']} "
          f"DIAGNOSIS_PHASE_ID={diagnosis.get('phase_id','')} DIAGNOSIS_PLAN_REVISION={diagnosis.get('plan_revision',0)} "
          f"DIAGNOSIS_ATTEMPT={diagnosis.get('attempt',0)} UNMET_DONE_COUNT={len(diagnosis.get('unmet_done') or [])} "
          f"UNMET_VERIFY_COUNT={len(diagnosis.get('unmet_verify') or [])} EVIDENCE_COUNT={len(diagnosis.get('evidence') or [])} "
          f"RETRY_INSTRUCTION_PRESENT={'true' if diagnosis.get('retry_instruction') else 'false'} "
          f"REPLAN_INSTRUCTION_PRESENT={'true' if diagnosis.get('replan_instruction') else 'false'}",
          file=__import__('sys').stderr,flush=True)
    return decision["decision"]

def _generate_plan(task_id: str, goal: str, prompt: str, activity: str) -> tuple[dict | None, str | None]:
    raw=_model_text(task_id,"PLANNING" if activity=="QWEN_PLANNING" else "RUNNING",activity,settings.main_model,[
        {"role":"system","content":"You are OLCR Qwen Planning Mode. Return JSON only; this is read-only planning."},
        {"role":"user","content":prompt},
    ], structured_schema=plan_schema())
    if raw is None: return None,None
    plan,parse_error=extract_plan_json(raw)
    errors=([parse_error] if parse_error else []) + validate_plan(plan,goal)
    # The task goal is persisted host state; a model paraphrase must never
    # consume the single schema-repair attempt or alter that identity.
    if plan is not None and errors == ["original goal mismatch"]:
        print("PLAN_ORIGINAL_GOAL_SOURCE=task PLAN_MODEL_ORIGINAL_GOAL_PRESENT=yes PLAN_MODEL_ORIGINAL_GOAL_MATCH=no",file=__import__('sys').stderr,flush=True)
        plan={**plan,"original_goal":goal}
        errors=validate_plan(plan,goal)
        print("CANONICAL_PLAN_ORIGINAL_GOAL_SOURCE=task",file=__import__('sys').stderr,flush=True)
    # The planner may provide an advisory task graph, but OLCR derives the
    # authoritative graph after profile normalization.  Preserve a valid phase
    # plan when only those advisory task references are malformed; otherwise a
    # preflight-only row can make Qwen spend its single repair on an artifact
    # the orchestrator replaces anyway.
    if errors and isinstance(plan, dict) and "tasks" in plan:
        phase_plan={key:value for key,value in plan.items() if key != "tasks"}
        phase_plan_errors=validate_plan(phase_plan,goal)
        if not phase_plan_errors:
            print("PLAN_ADVISORY_TASK_GRAPH=DISCARDED_INVALID AUTHORITATIVE_TASK_GRAPH=PENDING_NORMALIZATION",file=__import__('sys').stderr,flush=True)
            plan,errors=phase_plan,[]
    print(f"PLAN_INITIAL_PARSE={'PASS' if not parse_error else 'FAIL'} PLAN_INITIAL_SCHEMA={'PASS' if not validate_plan(plan,goal) else 'FAIL'} PLAN_INITIAL_ERRORS={json.dumps(errors,ensure_ascii=False)}",file=__import__('sys').stderr,flush=True)
    if not errors: return plan,raw
    repaired=_model_text(task_id,"PLANNING","QWEN_PLAN_SCHEMA_REPAIR",settings.main_model,[
        {"role":"system","content":"You only repair JSON schema and format. Do not plan new work or execute tools."},
        {"role":"user","content":plan_repair_prompt(goal,raw,errors)},
    ], structured_schema=plan_schema())
    if repaired is None: return None,None
    plan,repair_parse_error=extract_plan_json(repaired)
    if plan is not None and isinstance(plan,dict):
        plan={**plan,"original_goal":goal}
    repair_errors=([repair_parse_error] if repair_parse_error else []) + validate_plan(plan,goal)
    print(f"PLAN_REPAIR_PARSE={'PASS' if not repair_parse_error else 'FAIL'} PLAN_REPAIR_SCHEMA={'PASS' if not validate_plan(plan,goal) else 'FAIL'} PLAN_REPAIR_ERRORS={json.dumps(repair_errors,ensure_ascii=False)}",file=__import__('sys').stderr,flush=True)
    return (plan,repaired) if not repair_errors else (None,repaired)

def _scope_expansion(current: dict, proposed: dict) -> list[str]:
    previous=set((current.get("scope") or {}).get("allowed") or [])
    return [item for item in ((proposed.get("scope") or {}).get("allowed") or []) if item not in previous]

_PLAN_AUTHORIZATION_BOUNDARY = re.compile(
    r"(?:\bgit\s+(?:commit|push)\b|\b(?:publish|deploy|purchase|delete)\b|"
    r"(?:秘密|シークレット|認証情報|外部アップロード|公開|デプロイ|購入|"
    r"コミット|プッシュ|破壊的|削除)|"
    r"(?:commit|push|secret|credential|upload).*(?:execute|operation|change))",
    re.IGNORECASE,
)

def _plan_authorization_boundary(plan: dict) -> list[str]:
    """Return only operations that need authorization beyond a coding request."""
    candidates = list((plan.get("scope") or {}).get("allowed") or [])
    candidates.extend(str(phase.get("goal") or "") for phase in plan.get("phases") or [])
    candidates.extend(str(task.get("goal") or "") for task in plan.get("tasks") or [])
    return [item for item in candidates if _PLAN_AUTHORIZATION_BOUNDARY.search(item)]

def _replan_task(task_id: str, managed: dict, completed_ids: set[str], reason: str, diagnosis: dict | None = None) -> str:
    replan_count_in_epoch=int(managed.get("replan_count_in_epoch") or 0)
    if replan_count_in_epoch >= MAX_SUBSTANTIAL_REPLANS_PER_TASK:
        _transition_resumable(task_id,"REPLAN_LIMIT","RECOVERABLE_INTERNAL","REPLAN_CONTINUATION")
        return "Coding Task could not continue after the replan limit."
    current=managed.get("plan") or {}
    reports=[r["structured_report"] for r in db.coding_phase_reports(task_id)]
    unfinished=[phase for phase in current.get("phases",[]) if phase.get("id") not in completed_ids]
    failure_history=[_bounded_review_context(report) for report in reports[-8:] if report.get("manager_decision")]
    structural_reason = bool(diagnosis and (
        diagnosis.get("unmet_done") or diagnosis.get("unmet_verify") or
        diagnosis.get("replan_instruction_source") == "model" or
        diagnosis.get("replan_reason_source") == "model"))
    structural_reason = structural_reason or any(term in reason.lower() for term in (
        "structur", "verification criteria", "verify criteria", "unobservable", "unsatisfiable"))
    prompt=("Return a replacement read-only coding plan JSON. Preserve original goal, approved scope, completed phase IDs "
            "and verified results. Redesign only unfinished work. Use the bounded failure history and unmet criteria to "
            "make the next phase materially actionable; do not repeat an ineffective phase structure. " + json.dumps({"original_goal":managed["original_goal"],"current_plan":current,
            "current_plan_revision":int(managed.get("plan_revision") or 0),"failing_phase_id":unfinished[0].get("id") if unfinished else None,
            "completed_phase_ids":sorted(completed_ids),"attempt_reports":reports[-12:],"failure_history":failure_history,"reason":reason,
            "approved_scopes":managed.get("approved_scopes",[]),"replan_count":managed.get("replan_count",0)},ensure_ascii=False))
    print(f"TASK_ID={task_id} REPLAN_FAILURE_CONTEXT_PRESENT={'true' if failure_history else 'false'} "
          f"REPLAN_BOUNDED_FAILURE_COUNT={len(failure_history)} REPLAN_CONTEXT_FINGERPRINT={_context_fingerprint({'revision': managed.get('plan_revision',0), 'completed': sorted(completed_ids), 'history': failure_history, 'reason': reason[:500]})}",
          file=__import__('sys').stderr,flush=True)
    plan,_=_generate_plan(task_id,managed["original_goal"],prompt,"QWEN_REPLANNING")
    if not plan:
        _transition_resumable(task_id,"REPLAN_VALIDATION","RECOVERABLE_INTERNAL","REPLAN_CONTINUATION")
        return "Coding Task replan could not be validated."
    # A replan may only redesign unfinished work.  Persisted manager-approved
    # reports are the authority for completed phases, so do not let a model
    # silently omit or reopen one of them.
    old_signatures={phase.get("id"):_phase_signature(phase) for phase in current.get("phases",[])}
    new_signatures={phase.get("id"):_phase_signature(phase) for phase in plan.get("phases",[]) if phase.get("id") in old_signatures}
    # A completed phase may remain PASS only when its acceptance contract is
    # unchanged.  Revisions that alter a completed phase revalidate that phase
    # alone rather than resetting the whole task.
    invalidated_completed={phase_id for phase_id in completed_ids
                           if new_signatures.get(phase_id) != old_signatures.get(phase_id)}
    proposed={phase.get("id"):phase for phase in plan.get("phases",[])}
    # Unchanged completed phases must remain PASS.  A phase whose acceptance
    # contract changed is the sole exception: it is explicitly invalidated
    # below and will be revalidated without resetting unrelated completed work.
    if any(phase_id not in proposed or
           (proposed[phase_id].get("status") != "pass" and phase_id not in invalidated_completed)
           for phase_id in completed_ids):
        _transition_resumable(task_id,"REPLAN_VALIDATION","RECOVERABLE_INTERNAL","REPLAN_CONTINUATION")
        return "Coding Task replan did not preserve completed phases."
    for phase_id in invalidated_completed:
        if phase_id in proposed:
            proposed[phase_id]["status"]="pending"
    completed_ids=set(completed_ids)-invalidated_completed
    changed=[phase_id for phase_id,signature in old_signatures.items()
             if new_signatures.get(phase_id) != signature]
    structure_changed=set(old_signatures) != set(new_signatures) or bool(changed)
    failing_phase_id=unfinished[0].get("id") if unfinished else ""
    print(f"TASK_ID={task_id} REPLAN_OLD_REVISION={int(managed.get('plan_revision') or 0)} "
          f"REPLAN_NEW_REVISION={int(managed.get('plan_revision') or 0)+1} "
          f"OLD_PLAN_REVISION={int(managed.get('plan_revision') or 0)} NEW_PLAN_REVISION={int(managed.get('plan_revision') or 0)+1} "
          f"OLD_PHASE_SIGNATURE={old_signatures.get(failing_phase_id,'')} NEW_PHASE_SIGNATURE={new_signatures.get(failing_phase_id,'')} "
          f"REPLAN_OLD_PHASE_SIGNATURES={json.dumps(old_signatures,sort_keys=True)} "
          f"REPLAN_NEW_PHASE_SIGNATURES={json.dumps(new_signatures,sort_keys=True)} "
          f"FAILING_PHASE_CHANGED={'true' if structure_changed else 'false'} REPLAN_EFFECTIVE={'true' if structure_changed else 'false'}",
          file=__import__('sys').stderr,flush=True)
    if structural_reason and not structure_changed:
        unmet = []
        if diagnosis:
            unmet.extend(diagnosis.get("unmet_done") or [])
            unmet.extend(diagnosis.get("unmet_verify") or [])
        pending = {"type":"REPLAN_INEFFECTIVE", "reason":"Phase replan did not change the failing phase structure.", "unmet_criteria": unmet[:8], "state":"PENDING"}
        db.update_coding_task(task_id,status="WAITING_FOR_USER",activity="NONE",queue_order=None,
                              current_phase_id=failing_phase_id or None,
                              pending_authorization=pending,pending_user_confirmation=1,
                              recovery_action="NONE",recovery_reason="REPLAN_INEFFECTIVE")
        print(f"TASK_ID={task_id} FAILURE_STAGE=REPLAN_INEFFECTIVE NEXT_TASK_ACTION=WAITING_FOR_USER",file=__import__('sys').stderr,flush=True)
        return "Coding Task の再計画では問題を解消できませんでした。修正したい内容を入力してください。"
    expansion=_scope_expansion(current,plan)
    count=int(managed.get("replan_count") or 0)+1
    epoch_count=replan_count_in_epoch+1
    if expansion:
        authorization={"requested_scope":expansion,"reason":reason,"source":"replan","state":"PENDING"}
        db.update_coding_task(task_id,status="WAITING_FOR_USER",activity="SCOPE_AUTHORIZATION",pending_plan=plan,pending_authorization=authorization,pending_user_confirmation=1,replan_count=count,replan_count_in_epoch=epoch_count)
        return "Coding Task requires authorization for the proposed scope expansion."
    next_phase=next((phase for phase in plan.get("phases",[])
                     if phase.get("id") not in completed_ids
                     and all(dep in completed_ids for dep in phase.get("dependencies",[]))), None)
    db.update_coding_task(task_id,plan=plan,plan_revision=int(managed.get("plan_revision") or 0)+1,replan_count=count,replan_count_in_epoch=epoch_count,
                          current_phase_id=next_phase.get("id") if next_phase else None,
                          retry_count=0,recovery_action="NONE",recovery_reason="NONE")
    _initialize_subtask_progress(task_id,plan,preserve=True,completed_phase_ids=completed_ids)
    db.enqueue_coding_task(task_id)
    return "Coding Task was replanned and requeued."

def _finalization_log(task_id: str, *, final_phase_id: str | None, all_phases_pass: bool,
                     completion_check_started: bool, completion_check_raw_decision: str,
                     completion_check_valid: bool, final_report_started: bool,
                     final_report_valid: bool, final_report_persisted: bool,
                     status_before: str, status_after: str, failure_stage: str = "NONE",
                     exception_type: str = "NONE") -> None:
    """Emit bounded finalization diagnostics without logging report bodies."""
    fields=(f"TASK_ID={task_id}", f"FINAL_PHASE_ID={final_phase_id or 'NONE'}",
            f"ALL_PHASES_PASS={'true' if all_phases_pass else 'false'}",
            f"COMPLETION_CHECK_STARTED={'true' if completion_check_started else 'false'}",
            f"COMPLETION_CHECK_RAW_DECISION={completion_check_raw_decision[:80] or 'NONE'}",
            f"COMPLETION_CHECK_VALID={'true' if completion_check_valid else 'false'}",
            f"FINAL_REPORT_STARTED={'true' if final_report_started else 'false'}",
            f"FINAL_REPORT_VALID={'true' if final_report_valid else 'false'}",
            f"FINAL_REPORT_PERSISTED={'true' if final_report_persisted else 'false'}",
            f"TASK_STATUS_BEFORE_COMPLETION={status_before}", f"TASK_STATUS_AFTER_COMPLETION={status_after}",
            f"FINALIZATION_FAILURE_STAGE={failure_stage}", f"FINALIZATION_EXCEPTION_TYPE={exception_type}")
    print(" ".join(fields), file=__import__('sys').stderr, flush=True)


def _report_plan_revision(report: dict) -> int:
    """Read a report revision defensively; legacy reports belong to revision 0."""
    try:
        return int(report.get("plan_revision", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _latest_current_phase_report(task_id: str, phase_id: str, revision: int) -> dict | None:
    """Return the latest report for a phase in this plan revision only."""
    rows=[row for row in db.coding_phase_reports(task_id)
          if row.get("phase_id") == phase_id and _report_plan_revision(row["structured_report"]) == revision]
    if not rows:
        return None
    latest=rows[-1]
    if latest.get("validation_status") != "PASS":
        return None
    if latest["structured_report"].get("manager_decision",{}).get("decision") != "PASS":
        return None
    return latest


def _latest_completed_phase_report(task_id: str, phase_id: str) -> dict | None:
    """Return the latest validated PASS report across plan revisions."""
    rows=[row for row in db.coding_phase_reports(task_id)
          if row.get("phase_id") == phase_id and row.get("validation_status") == "PASS"
          and row.get("structured_report", {}).get("manager_decision", {}).get("decision") == "PASS"]
    return rows[-1] if rows else None


def _artifact_paths_from_reports(reports: list[dict], workspace_root: str | None = None) -> list[str]:
    """Collect deterministic paths produced by successful typed file writes."""
    paths: set[str] = set()
    write_tools = {"workspace_write", "workspace_write_normalized", "workspace_patch", "write_text"}
    for report in reports:
        for value in report.get("changed_files") or []:
            if isinstance(value, str) and value.strip():
                paths.add(value.strip())
        typed = report.get("typed_execution_summary")
        operations = typed.get("operations") if isinstance(typed, dict) else []
        for operation in operations or []:
            if not isinstance(operation, dict) or operation.get("tool") not in write_tools:
                continue
            if str(operation.get("status") or "").lower() not in {"success", "completed", "ok", "normalized"}:
                continue
            output = operation.get("output") if isinstance(operation.get("output"), dict) else {}
            input_value = operation.get("input") if isinstance(operation.get("input"), dict) else {}
            value = output.get("path") or input_value.get("path")
            if isinstance(value, str) and value.strip():
                paths.add(value.strip())
    normalized: set[str] = set()
    for value in paths:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() and workspace_root:
            candidate = Path(workspace_root).expanduser() / candidate
        if candidate.is_absolute():
            normalized.add(str(candidate.resolve()))
        else:
            normalized.add(value)
    return sorted(normalized)


def _append_artifact_paths(final_text: str, reports: list[dict], workspace_root: str | None = None) -> str:
    paths = _artifact_paths_from_reports(reports, workspace_root)
    lines = ["", "成果物ファイル:"]
    if paths:
        lines.extend(f"- {path}" for path in paths)
    else:
        lines.append("- なし")
    return final_text.rstrip() + "\n" + "\n".join(lines)


_MCP_LIFECYCLE_ONLY_TOOLS = {"", "initialize", "tools/list"}


def _required_mcp_names(managed: dict) -> list[str]:
    requirements = managed.get("requirements") if isinstance(managed.get("requirements"), dict) else {}
    names = requirements.get("required_mcps", requirements.get("required_mcp", managed.get("required_mcp") or []))
    return [str(name) for name in names if str(name)]


def _mcp_usage_rows(managed: dict, server_id: str) -> list[dict]:
    return [item for item in (managed.get("mcp_evidence") or [])
            if isinstance(item, dict) and item.get("mcp_name") == server_id
            and item.get("status") == "PASS"
            and str(item.get("tool_name") or "") not in _MCP_LIFECYCLE_ONLY_TOOLS]


def _final_acceptance_gaps(managed: dict, reports: list[dict]) -> list[str]:
    """Compute final gates strictly from persisted typed state, never prose."""
    gaps = [f"required MCP used: {name}" for name in _required_mcp_names(managed)
            if not _mcp_usage_rows(managed, name)]
    requirements = managed.get("requirements") if isinstance(managed.get("requirements"), dict) else {}
    requested = {str(name) for name in (requirements.get("required_verification") or [])}
    if "build" in requested and not any(report.get("build_pass") == "PASS" for report in reports):
        gaps.append("required build verification")
    if "typecheck" in requested:
        typecheck = any("typecheck" in " ".join(map(str, [*(report.get("test_executed") or []), *(report.get("test_pass") or [])])).lower()
                        for report in reports)
        if not typecheck:
            gaps.append("required typecheck verification")
    if "browser" in requested and not _mcp_usage_rows(managed, "playwright"):
        gaps.append("required browser verification")
    return gaps


def _append_mcp_telemetry(final_text: str, managed: dict) -> str:
    required=set(_required_mcp_names(managed))
    requirements = managed.get("requirements") if isinstance(managed.get("requirements"), dict) else {}
    evidence=managed.get("mcp_evidence") or []
    lines=["", "MCP telemetry:"]
    generic_available=[]; generic_used=[]; generic_purpose=[]; generic_skips=[]
    for name in ("shadcn", "playwright", "serena", "animejs"):
        rows=[item for item in evidence if isinstance(item,dict) and item.get("mcp_name") == name]
        available=any(item.get("status") == "PASS" for item in rows)
        usage=_mcp_usage_rows(managed, name)
        calls=[str(item.get("tool_name") or "") for item in usage]
        purpose=next((str(item.get("purpose") or "") for item in usage), "NONE")
        skip_reason=("NOT_REQUIRED" if name not in required else
                     "NONE" if usage else
                     next((str(item.get("error") or "") for item in reversed(rows) if item.get("error")), "UNVERIFIED"))
        lines.append(f"{name.upper()}_MCP_AVAILABLE={'YES' if rows and not all(item.get('status') == 'BLOCKED' for item in rows) else 'NO'}")
        lines.append(f"{name.upper()}_MCP_USED={'YES' if usage else 'NO'}")
        lines.append(f"{name.upper()}_MCP_TOOL_CALLS={','.join(calls) if calls else 'NONE'}")
        lines.append(f"{name.upper()}_MCP_PURPOSE={purpose}")
        if name == "animejs":
            lines.append(f"ANIMEJS_MCP_REQUIRED={'YES' if name in required else 'NO'}")
            lines.append("ANIMEJS_VERSION_CONTEXT=V4" if usage else "ANIMEJS_VERSION_CONTEXT=NONE")
            mode = next((str(item.get("resource_mode")) for item in reversed(rows) if item.get("resource_mode")), "NONE")
            lines.append(f"MCP_RESOURCE_MODE={mode}")
            lines.append(f"ANIMEJS_MCP_INITIALIZE={'PASS' if any(item.get('tool_name') == 'initialize' and item.get('status') == 'PASS' for item in rows) else 'NOT_RUN'}")
            lines.append(f"ANIMEJS_MCP_TOOLS_LIST={'PASS' if any(item.get('tool_name') == 'tools/list' and item.get('status') == 'PASS' for item in rows) else 'NOT_RUN'}")
            lines.append(f"ANIMEJS_MCP_TOOL_CALL={'PASS' if usage else 'NOT_RUN'}")
            project = requirements.get("animejs_project") if isinstance(requirements.get("animejs_project"), dict) else {}
            for key in ("ANIMEJS_PROJECT_DEPENDENCY_PRESENT", "ANIMEJS_PROJECT_VERSION", "ANIMEJS_PROJECT_MAJOR_VERSION", "ANIMEJS_VERSION_COMPATIBILITY"):
                lines.append(f"{key}={project.get(key, 'UNKNOWN')}")
            lines.append(f"ANIMEJS_MIGRATION_PERFORMED={'YES' if project.get('ANIMEJS_VERSION_COMPATIBILITY') == 'MIGRATION_REQUESTED' else 'NO'}")
        lines.append(f"{name.upper()}_MCP_SKIP_REASON={skip_reason}")
        generic_available.append(f"{name}:{'YES' if available else 'NO'}")
        generic_used.append(f"{name}:{'YES' if usage else 'NO'}")
        generic_purpose.append(f"{name}:{purpose}")
        generic_skips.append(f"{name}:{skip_reason}")
    # Stable generic fields let consumers render a single deterministic MCP
    # summary while the per-server fields retain diagnostic detail.
    lines.extend([f"MCP_AVAILABLE={';'.join(generic_available)}",
                  f"MCP_USED={';'.join(generic_used)}",
                  f"MCP_PURPOSE={';'.join(generic_purpose)}",
                  f"MCP_SKIP_REASON={';'.join(generic_skips)}"])
    return final_text.rstrip()+"\n"+"\n".join(lines)


def _final_report_only(task_id: str, managed: dict, plan: dict, reports: list[dict], *,
                       completion_check_started: bool = False,
                       completion_check_raw_decision: str = "NONE",
                       completion_check_valid: bool = False) -> str:
    """Generate and persist only the final report for FINAL_REPORT recovery."""
    before=db.coding_task(task_id) or {}
    final_phase_id=(plan.get("phases") or [{}])[-1].get("id")
    gaps = _final_acceptance_gaps(before or managed, reports)
    if gaps:
        reason = "REQUIRED_MCP_UNVERIFIED" if any(item.startswith("required MCP") or item.startswith("required browser") for item in gaps) else "REQUIRED_VERIFICATION_UNVERIFIED"
        after = db.update_coding_task(task_id, status="RESUMABLE", activity="NONE",
                                      recovery_action="RECOVERY_REVIEW", recovery_reason=reason) or {}
        _finalization_log(task_id, final_phase_id=final_phase_id, all_phases_pass=False,
                          completion_check_started=completion_check_started,
                          completion_check_raw_decision=completion_check_raw_decision,
                          completion_check_valid=completion_check_valid, final_report_started=False,
                          final_report_valid=False, final_report_persisted=False,
                          status_before=before.get("status", "UNKNOWN"),
                          status_after=after.get("status", "RESUMABLE"),
                          failure_stage="REQUIRED_ACCEPTANCE", exception_type="NONE")
        return "Coding Task final verification is incomplete: " + ", ".join(gaps)
    try:
        final=_model_text(task_id,"FINAL_REPORTING","QWEN_FINAL_REPORT",settings.main_model,[
            {"role":"system","content":"You are OLCR Qwen final report mode. Read-only: do not execute tools or edit files."},
            {"role":"user","content":final_report_prompt(managed["original_goal"],plan,reports)},
        ])
    except Exception as exc:
        _transition_resumable(task_id,"FINAL_REPORT","RECOVERABLE_INTERNAL","FINAL_REPORT",exc,"MODEL_CALL")
        _finalization_log(task_id,final_phase_id=final_phase_id,all_phases_pass=True,completion_check_started=completion_check_started,
                          completion_check_raw_decision=completion_check_raw_decision,completion_check_valid=completion_check_valid,final_report_started=True,
                          final_report_valid=False,final_report_persisted=False,status_before=before.get("status","UNKNOWN"),
                          status_after="RESUMABLE",failure_stage="FINAL_REPORT",exception_type=type(exc).__name__)
        return "Coding Task final report could not be generated."
    if final is None:
        after=db.coding_task(task_id) or {}
        _finalization_log(task_id,final_phase_id=final_phase_id,all_phases_pass=True,completion_check_started=completion_check_started,
                          completion_check_raw_decision=completion_check_raw_decision,completion_check_valid=completion_check_valid,final_report_started=True,
                          final_report_valid=False,final_report_persisted=False,status_before=before.get("status","UNKNOWN"),
                          status_after=after.get("status","RESUMABLE"),failure_stage="PAUSED",exception_type="NONE")
        return "Task paused before final report."
    final_text=final.strip() if isinstance(final,str) else ""
    if not final_text:
        _transition_resumable(task_id,"FINAL_REPORT","RECOVERABLE_INTERNAL","FINAL_REPORT",recovery_reason="INVALID_OUTPUT")
        _finalization_log(task_id,final_phase_id=final_phase_id,all_phases_pass=True,completion_check_started=completion_check_started,
                          completion_check_raw_decision=completion_check_raw_decision,completion_check_valid=completion_check_valid,final_report_started=True,
                          final_report_valid=False,final_report_persisted=False,status_before=before.get("status","UNKNOWN"),
                          status_after="RESUMABLE",failure_stage="FINAL_REPORT",exception_type="INVALID_OUTPUT")
        return "Coding Task final report could not be validated."
    conversation = db.conversation(managed.get("conversation_id", "")) or {}
    project = db.project(conversation.get("project_id", "")) or {}
    persisted_reports = [row["structured_report"] for row in db.coding_phase_reports(task_id)
                         if row.get("validation_status") == "PASS"]
    final_text = _append_artifact_paths(final_text, persisted_reports or reports, project.get("workspace_path"))
    final_text = _append_mcp_telemetry(final_text, db.coding_task(task_id) or managed)
    try:
        db.update_coding_task(task_id,status="COMPLETED",activity="NONE",current_phase_id=None,
                              final_report={"text":final_text},final_report_status="PASS",
                              recovery_action="NONE",recovery_reason="NONE")
        persisted=db.coding_task(task_id) or {}
        if persisted.get("status") != "COMPLETED" or persisted.get("final_report_status") != "PASS":
            raise RuntimeError("final report persistence verification failed")
        db.add_message(managed["conversation_id"],"assistant",final_text,time.time(),str(uuid.uuid4()),task_id)
    except Exception as exc:
        _transition_resumable(task_id,"FINAL_REPORT_PERSISTENCE","RECOVERABLE_INTERNAL","FINAL_REPORT",exc,"PERSISTENCE")
        _finalization_log(task_id,final_phase_id=final_phase_id,all_phases_pass=True,completion_check_started=completion_check_started,
                          completion_check_raw_decision=completion_check_raw_decision,completion_check_valid=completion_check_valid,final_report_started=True,
                          final_report_valid=True,final_report_persisted=False,status_before=before.get("status","UNKNOWN"),
                          status_after="RESUMABLE",failure_stage="FINAL_REPORT_PERSISTENCE",exception_type=type(exc).__name__)
        return "Coding Task final report could not be persisted."
    _finalization_log(task_id,final_phase_id=final_phase_id,all_phases_pass=True,completion_check_started=completion_check_started,
                      completion_check_raw_decision=completion_check_raw_decision,completion_check_valid=completion_check_valid,final_report_started=True,
                      final_report_valid=True,final_report_persisted=True,status_before=before.get("status","UNKNOWN"),
                      status_after="COMPLETED")
    return "Coding Task completed."


def _complete_task(task_id: str, managed: dict) -> str:
    before=db.coding_task(task_id) or {}
    if before.get("status") == "COMPLETED":
        return "Coding Task completed."
    plan=managed.get("plan") or {}
    current_revision=int(managed.get("plan_revision") or 0)
    phases=plan.get("phases") or []
    final_phase_id=phases[-1].get("id") if phases else None
    # Reports from prior replans are historical evidence only.  A finalization
    # decision must be based on the currently approved plan revision.
    reports=[]
    for phase in phases:
        latest=_latest_current_phase_report(task_id,phase.get("id"),current_revision)
        if latest is None and phase.get("status") == "pass":
            latest=_latest_completed_phase_report(task_id,phase.get("id"))
        if latest is None:
            after=db.update_coding_task(task_id,status="RESUMABLE",activity="NONE",recovery_action="RECOVERY_REVIEW",recovery_reason="COMPLETION_EVIDENCE") or {}
            _finalization_log(task_id,final_phase_id=final_phase_id,all_phases_pass=False,completion_check_started=False,
                              completion_check_raw_decision="NONE",completion_check_valid=False,final_report_started=False,
                              final_report_valid=False,final_report_persisted=False,status_before=before.get("status","UNKNOWN"),
                              status_after=after.get("status","RESUMABLE"),failure_stage="EVIDENCE",exception_type="NONE")
            return "Coding Task completion evidence is incomplete."
        reports.append(latest["structured_report"])
    all_phases_pass=len(reports)==len(phases) and len(reports)>0
    if managed.get("pending_authorization") or any(report_has_authoritative_failure(r,next((p for p in phases if p.get("id")==r.get("phase_id")),{})) for r in reports):
        after=db.update_coding_task(task_id,status="RESUMABLE",activity="NONE",recovery_action="RECOVERY_REVIEW",recovery_reason="COMPLETION_EVIDENCE") or {}
        _finalization_log(task_id,final_phase_id=final_phase_id,all_phases_pass=all_phases_pass,completion_check_started=False,
                          completion_check_raw_decision="NONE",completion_check_valid=False,final_report_started=False,
                          final_report_valid=False,final_report_persisted=False,status_before=before.get("status","UNKNOWN"),
                          status_after=after.get("status","RESUMABLE"),failure_stage="EVIDENCE",exception_type="NONE")
        return "Coding Task completion evidence is incomplete."
    # Completion is a deterministic consequence of every phase carrying
    # authoritative evidence.  Coding must not call the lightweight Gemma
    # router as a second quality or completion judge.
    print(f"TASK_ID={task_id} CODING_GEMMA_COMPLETION_CHECK=REMOVED COMPLETION_CHECK=DETERMINISTIC_PASS", file=__import__('sys').stderr, flush=True)
    return _final_report_only(task_id, managed, plan, reports, completion_check_started=False,
                              completion_check_raw_decision="DETERMINISTIC_PASS", completion_check_valid=True)

def _heavy_batch_checkpoint(task_id: str, plan: dict, completed: set[str]) -> str | None:
    """Persist a bounded handoff after one heavy batch; never use authorization."""
    remaining=[phase for phase in plan.get("phases",[]) if phase.get("id") not in completed]
    if not remaining:
        return None
    reports=[row["structured_report"] for row in db.coding_phase_reports(task_id)]
    changed=sorted({path for report in reports for path in (report.get("changed_files") or []) if isinstance(path,str)})
    cursor=next((index for index,phase in enumerate(plan.get("phases",[])) if phase.get("id") not in completed),len(plan.get("phases",[])))
    handoff={"changed_files":changed,"created_interfaces":[],"data_contracts":[],"decisions":[],"verification":[],"unresolved":[],"next_constraints":[str(remaining[0].get("goal") or "")[:500]]}
    db.update_coding_task(task_id,status="RESUMABLE",activity="NONE",batch_cursor=cursor,batch_handoff=handoff,
                          pending_authorization=None,pending_user_confirmation=0,
                          recovery_action="NONE",recovery_reason="RESOURCE_CHECKPOINT")
    print(f"TASK_ID={task_id} EXECUTION_MODE=HEAVY_BATCHED RESOURCE_CHECKPOINT=true BATCH_CURSOR={cursor} BATCHES_REMAINING={len(remaining)}",file=__import__('sys').stderr,flush=True)
    return "Heavy batch completed; task is resumable."

def _run_managed_task(task_id: str, workspace_root: str | None) -> str:
    """Run one approved task serially through the existing typed Runtime executor."""
    managed=db.coding_task(task_id)
    if not managed or managed["status"] not in {"QUEUED","RUNNING"} or managed["archived"] or managed["pause_requested"]: return ""
    plan=managed.get("plan") or {}; phases=plan.get("phases",[])
    heavy=managed.get("execution_mode") == "HEAVY_BATCHED"
    current_revision=int(managed.get("plan_revision") or 0)
    completed={phase["id"] for phase in phases
               if phase.get("status") == "pass" or
               _latest_current_phase_report(task_id,phase["id"],current_revision) is not None}
    # A human Resume from REPLAN_LIMIT starts a fresh bounded recovery epoch.
    # The saved terminal decision remains the authority for the first action:
    # perform the required replan before attempting another implementation.
    if managed.get("recovery_action") == "REPLAN_CONTINUATION" and managed.get("recovery_reason") == "REPLAN_LIMIT":
        print(f"TASK_ID={task_id} RECOVERY_EPOCH={managed.get('recovery_epoch',0)} REPLAN_COUNT_IN_EPOCH={managed.get('replan_count_in_epoch',0)} NEXT_TASK_ACTION=QWEN_REPLANNING",file=__import__('sys').stderr,flush=True)
        return _replan_task(task_id,managed,completed,"explicit human resume after REPLAN_LIMIT")
    if managed.get("recovery_action") == "REPLAN_CONTINUATION" and managed.get("recovery_reason") == "USER_REVISION":
        revision=(managed.get("pending_authorization") or {}).get("user_revision") or "user supplied phase revision"
        print(f"TASK_ID={task_id} NEXT_TASK_ACTION=QWEN_REPLANNING USER_REVISION_PRESENT=true",file=__import__('sys').stderr,flush=True)
        return _replan_task(task_id,managed,completed,revision)
    if managed.get("recovery_action") == "FINAL_REPORT":
        current_revision=int(managed.get("plan_revision") or 0)
        plan=managed.get("plan") or {}
        reports=[]
        for phase in plan.get("phases",[]):
            latest=_latest_current_phase_report(task_id,phase.get("id"),current_revision)
            if latest is None:
                return _complete_task(task_id,managed)
            reports.append(latest["structured_report"])
        return _final_report_only(task_id,managed,plan,reports)
    for phase in phases:
        if phase["id"] in completed: continue
        if any(dep not in completed for dep in phase.get("dependencies",[])): continue
        if not phase_has_observable_deliverable(phase):
            print(f"TASK_ID={task_id} CONTROL_PLANE_INVARIANT_FAILURE=NON_ARTIFACT_PHASE_DISPATCH PHASE_ID={phase['id']}",file=__import__('sys').stderr,flush=True)
            db.update_coding_task(task_id,status="BLOCKED",activity="NONE",recovery_action="NONE",recovery_reason="NON_ARTIFACT_PHASE")
            return "Coding Task is blocked: orchestration-only work cannot enter implementation."
        if (db.coding_task(task_id) or {}).get("pause_requested"):
            db.update_coding_task(task_id,status="RESUMABLE",activity="NONE"); return "Task paused."
        plan_revision=current_revision
        reports=[r for r in db.coding_phase_reports(task_id) if r["phase_id"] == phase["id"] and r["validation_status"]=="PASS" and _report_plan_revision(r["structured_report"]) == plan_revision]
        latest=reports[-1] if reports else None
        if latest and not latest["structured_report"].get("manager_decision"):
            decision=_review_phase(task_id,managed,phase,latest,[r["structured_report"] for r in db.coding_phase_reports(task_id) if r["phase_id"] in completed],workspace_root)
            if decision is None: return "Task paused after phase report."
            latest=db.coding_phase_reports(task_id)[-1]
        if latest and latest["structured_report"].get("manager_decision",{}).get("decision") == "PASS":
            _set_subtask_state(task_id,phase["id"],"DONE",attempt=int(latest["attempt"]),report=latest["structured_report"],decision="PASS")
            completed.add(phase["id"]); plan=_phase_status(plan,phase["id"],"pass"); db.update_coding_task(task_id,plan=plan,activity="NONE",retry_count=0,recovery_action="NONE",recovery_reason="NONE"); continue
        attempt=len(reports)
        if latest:
            decision=latest["structured_report"].get("manager_decision",{}).get("decision")
            reason=latest["structured_report"].get("manager_decision",{}).get("reason","manager decision")
            if decision == "BLOCKED":
                _set_subtask_state(task_id,phase["id"],"FAILED",attempt=int(latest["attempt"]),report=latest["structured_report"],decision=decision)
                db.update_coding_task(task_id,status="BLOCKED",activity="NONE")
                print(f"MANAGER_DECISION=BLOCKED NEXT_TASK_ACTION=STOP_BLOCKED TASK_STATUS=BLOCKED TASK_ACTIVITY=NONE",file=__import__('sys').stderr,flush=True)
                return "Coding Task is blocked: "+reason
            if decision == "NEED_USER":
                _set_subtask_state(task_id,phase["id"],"PARTIAL",attempt=int(latest["attempt"]),report=latest["structured_report"],decision=decision)
                db.update_coding_task(task_id,status="WAITING_FOR_USER",activity="NONE")
                return "Coding Task is waiting for user authorization: "+reason
            if decision == "REPLAN_REQUIRED":
                diagnosis=latest["structured_report"].get("manager_decision",{}).get("diagnosis")
                return _replan_task(task_id,managed,completed,reason,diagnosis)
        # A resumed task keeps its persisted attempt count and existing bounds.
        if latest and attempt > MAX_RETRIES_PER_PHASE:
            return _replan_task(task_id,managed,completed,"retry limit reached",latest["structured_report"].get("manager_decision",{}).get("diagnosis"))
        retry_context=_bounded_review_context(latest["structured_report"]) if latest else None
        if retry_context:
            previous=latest["structured_report"]
            instruction=zero_mutation_retry_instruction(phase,previous,[row["structured_report"] for row in reports[:-1]],workspace_root)
            if instruction:
                retry_context["diagnosis"]["retry_instruction"]=instruction
                retry_context["diagnosis"]["specific_fix"]=instruction
            print(f"RETRY_CAUSE={'ZERO_WORKSPACE_MUTATION' if instruction else 'PHASE_EVIDENCE_INCOMPLETE'} RETRY_INSTRUCTION_KIND={'ZERO_MUTATION' if instruction else 'PRIOR_DIAGNOSIS'} PREVIOUS_WORKSPACE_MUTATION_COUNT={workspace_mutation_count(previous)}",file=__import__('sys').stderr,flush=True)
        context_payload={"phase_id":phase["id"],"plan_revision":plan_revision,"attempt":attempt,
                         "prior_review":retry_context} if retry_context else {"phase_id":phase["id"],"plan_revision":plan_revision,"attempt":attempt}
        print(f"TASK_ID={task_id} PHASE_ID={phase['id']} PHASE_ATTEMPT={attempt} "
              f"RETRY_CONTEXT_PRESENT={'true' if retry_context else 'false'} "
              f"RETRY_INSTRUCTION_PRESENT={'true' if retry_context and retry_context['diagnosis'].get('retry_instruction') else 'false'} "
              f"IMPLEMENTATION_CONTEXT_FINGERPRINT={_context_fingerprint(context_payload)}",
              file=__import__('sys').stderr,flush=True)
        db.update_coding_task(task_id,status="RUNNING",activity="QWEN_IMPLEMENTATION",current_phase_id=phase["id"],retry_count=max(0,attempt-1),recovery_action="NONE",recovery_reason="NONE")
        _set_subtask_state(task_id,phase["id"],"RUNNING",attempt=attempt)
        print(f"TASK_ID={task_id} TASK_STATUS=RUNNING TASK_ACTIVITY=QWEN_IMPLEMENTATION MODEL_SLOT_OWNER={task_id} MODEL_NAME={settings.main_model} PHASE_ID={phase['id']} PHASE_ATTEMPT={attempt}",file=__import__('sys').stderr,flush=True)
        phase_mcp=_phase_required_mcp(plan,phase["id"])
        mcp_context=[]
        for server_id in phase_mcp:
            persisted=db.coding_task(task_id) or {}
            evidence=next((item for item in reversed(persisted.get("mcp_evidence") or [])
                           if item.get("mcp_name") == server_id and item.get("status") == "PASS"
                           and str(item.get("tool_name") or "") not in _MCP_LIFECYCLE_ONLY_TOOLS),None)
            if evidence is None:
                preflight=(persisted.get("requirements") or {}).get("preflight") or {}
                if server_id == "shadcn" and preflight.get("shadcn") == "PASS":
                    print(f"TASK_ID={task_id} CONTROL_PLANE_INVARIANT_FAILURE=MCP_EVIDENCE_LOST REQUIRED_MCP=shadcn",file=__import__('sys').stderr,flush=True)
                    db.update_coding_task(task_id,status="BLOCKED",activity="NONE",recovery_action="NONE",recovery_reason="MCP_EVIDENCE_LOST")
                    _set_subtask_state(task_id,phase["id"],"FAILED",attempt=attempt,decision="BLOCKED")
                    return "Coding Task is blocked: persisted shadcn preflight evidence was lost."
                evidence,error=_run_required_mcp(task_id,server_id,workspace_root,
                                                 f"{'required' if server_id in _required_mcp_names(managed) else 'automatic animation reference'} by phase {phase['id']}")
                if error:
                    if server_id not in _required_mcp_names(managed):
                        # An automatic helper is advisory; its failed attempt is
                        # still typed evidence, but must not turn into a false
                        # required-MCP gate.
                        continue
                    db.update_coding_task(task_id,status="BLOCKED",activity="NONE",recovery_action="NONE",
                                          recovery_reason="REQUIRED_MCP_FAILED")
                    _set_subtask_state(task_id,phase["id"],"FAILED",attempt=attempt,decision="BLOCKED")
                    print(f"TASK_ID={task_id} REQUIRED_MCP={server_id} MCP_USAGE=FAILED TASK_STATUS=BLOCKED",file=__import__('sys').stderr,flush=True)
                    return f"Coding Task is blocked: required {server_id} MCP failed."
            mcp_context.append(evidence)
        phase_with_contract={**phase,"required_mcp":phase_mcp}
        animejs_context = ((db.coding_task(task_id) or {}).get("requirements") or {}).get("animejs_project")
        animejs_safety = ("Anime.js version evidence: " + json.dumps(animejs_context, ensure_ascii=False) + "\n"
                          if isinstance(animejs_context, dict) else "")
        phase_request=(f"Implement the approved coding phase only. Original goal: {managed['original_goal']}\n"
                       f"Workspace: {workspace_root or '(project workspace)'}\nPhase: {phase['goal']}\n"
                       f"Done: {phase['done']}\nVerify: {phase['verify']}\n"
                       f"Plan revision: {plan_revision}\nAttempt: {attempt}\nApproved scope: {managed.get('approved_scopes', [])}\n" + animejs_safety
                       + "Use OLCR's typed workspace executor. Do not expand scope or claim unrun verification.\n"
                       + ("Required MCP evidence (use this bounded result): " + json.dumps(mcp_context,ensure_ascii=False) + "\n" if mcp_context else "")
                       + ("Previous bounded review diagnosis (address these criteria before returning): "
                          + json.dumps(retry_context, ensure_ascii=False, sort_keys=True) + "\n" if retry_context else ""))
        phase_request += _frontend_quality_guidance(db.coding_task(task_id) or managed, phase_with_contract, workspace_root)
        if heavy and managed.get("batch_handoff"):
            phase_request += "Persisted handoff from the prior batch (reconcile only what is listed): " + json.dumps(managed["batch_handoff"],ensure_ascii=False,sort_keys=True)[:4000] + "\n"
        knowledge_context = coding_knowledge_context_for_subtask(phase_request, workspace_root)
        with model_slot():
            execution, response=runtime.execute(phase_request,core_context=knowledge_context,workspace_root=workspace_root,managed_context={"managed_coding_task":True,"task_id":task_id,"operation_intent":"IMPLEMENTATION","global_no_write":False})
        report=_report_from_execution(phase_with_contract,attempt,execution,response)
        report["mcp_evidence"]=mcp_context
        report["plan_revision"]=plan_revision
        errors=validate_phase_report(report,phase["id"],attempt)
        report_row=_save_report(task_id,phase["id"],attempt,report,"PASS" if not errors else "INVALID")
        if errors:
            # Exactly one read-only schema repair. Typed facts are copied back after
            # validation so a model cannot fabricate execution success.
            typed=report["typed_execution_summary"]
            repair=_model_text(task_id,"RUNNING","QWEN_REPORT_SCHEMA_REPAIR",settings.main_model,[
                {"role":"system","content":"Repair only the report JSON format. Do not execute tools or alter typed evidence."},
                {"role":"user","content":"Return JSON with the existing report schema. Invalid report: "+json.dumps(report,ensure_ascii=False)},
            ])
            if repair is None: return "Task paused before phase report repair."
            try: repaired=json.loads(repair or "")
            except Exception: repaired={}
            repaired["typed_execution_summary"]=typed
            if execution.state in {TaskState.FAILED,TaskState.DENIED} or execution.error:
                repaired["status"]="FAIL"; repaired["errors"]=[execution.error] if execution.error else repaired.get("errors",[])
            repair_errors=validate_phase_report(repaired,phase["id"],attempt)
            if repair_errors:
                db.update_coding_phase_report(report_row["id"],report,"INVALID")
                _transition_resumable(task_id,"PHASE_REPORT","RECOVERABLE_INTERNAL","RECOVERY_REVIEW")
                return "Coding Task phase report could not be validated after one repair."
            db.update_coding_phase_report(report_row["id"],repaired,"PASS")
            report_row={**report_row,"structured_report":repaired,"validation_status":"PASS"}
        if (db.coding_task(task_id) or {}).get("pause_requested"):
            if report_row["structured_report"].get("changed_files"):
                _set_subtask_state(task_id,phase["id"],"PARTIAL",attempt=attempt,report=report_row["structured_report"])
            db.update_coding_task(task_id,status="RESUMABLE",activity="NONE"); return "Task paused after phase execution."
        decision=_review_phase(task_id,managed,phase_with_contract,report_row,[r["structured_report"] for r in db.coding_phase_reports(task_id) if r["phase_id"] in completed],workspace_root)
        if decision is None: return "Task paused after phase review."
        if decision == "PASS":
            _set_subtask_state(task_id,phase["id"],"DONE",attempt=attempt,report=report_row["structured_report"],decision=decision)
            completed.add(phase["id"]); plan=_phase_status(plan,phase["id"],"pass"); db.update_coding_task(task_id,plan=plan,activity="NONE",retry_count=0,recovery_action="NONE",recovery_reason="NONE")
            if heavy:
                checkpoint=_heavy_batch_checkpoint(task_id,plan,completed)
                if checkpoint:return checkpoint
            continue
        if decision == "RETRY" and attempt < (1 if heavy else MAX_RETRIES_PER_PHASE):
            # Re-enter only this persisted phase. Its immutable plan fields
            # remain unchanged and the existing report makes the next attempt
            # number deterministic (0 → 1 → 2, never 3).
            db.update_coding_task(task_id,status="RUNNING",activity="QWEN_IMPLEMENTATION",retry_count=attempt+1)
            return _run_managed_task(task_id,workspace_root)
        if decision == "REPLAN_REQUIRED" or (decision == "RETRY" and attempt >= MAX_RETRIES_PER_PHASE):
            _set_subtask_state(task_id,phase["id"],"FAILED",attempt=attempt,report=report_row["structured_report"],decision=decision)
            diagnosis=report_row["structured_report"].get("manager_decision",{}).get("diagnosis")
            return _replan_task(task_id,managed,completed,"retry limit reached",diagnosis)
        _transition_resumable(task_id,"MANAGER_DECISION","RECOVERABLE_INTERNAL","RECOVERY_REVIEW")
        return "Coding Task requires recoverable manager decision handling: "+str(decision)
    if len(completed) == len(phases):
        return _complete_task(task_id,db.coding_task(task_id) or managed)
    return _replan_task(task_id,managed,completed,"unmet phase dependencies")

_coding_scheduler_wake=threading.Event()
_runner_guard=threading.Lock()
_active_runners:set[str]=set()

def run_managed_task(task_id: str, workspace_root: str | None) -> str:
    with _runner_guard:
        if task_id in _active_runners:
            return "Coding Task is already running."
        _active_runners.add(task_id)
    try:
        return _run_managed_task(task_id,workspace_root)
    finally:
        with _runner_guard:
            _active_runners.discard(task_id)


def _phase_required_mcp(plan: dict, phase_id: str) -> list[str]:
    for task in plan.get("tasks") or []:
        if isinstance(task, dict) and task.get("phase_id") == phase_id:
            return list(dict.fromkeys([str(name) for name in task.get("required_mcp") or []] + [str(name) for name in task.get("selected_mcp") or []]))
    return []


def _explicit_task_brand_palette(goal: str) -> list[str]:
    """Return only colors the current request labels as brand input."""
    colors: list[str] = []
    for line in (goal or "").splitlines():
        if re.search(r"(?:accent|brand|palette|color|colour|カラー|配色|色)", line, re.I):
            colors.extend(re.findall(r"#[0-9a-fA-F]{3,8}\b", line))
    return list(dict.fromkeys(color.upper() for color in colors))[:8]


def _repository_brand_tokens(workspace_root: str | None) -> list[str]:
    """Read a small, relevant token set without importing unrelated branding."""
    if not workspace_root:
        return []
    root = Path(workspace_root)
    candidates: list[Path] = []
    for name in ("tailwind.config.js", "tailwind.config.ts", "tailwind.config.cjs", "theme.css", "tokens.css"):
        candidate = root / name
        if candidate.is_file():
            candidates.append(candidate)
    try:
        candidates.extend(path for path in root.rglob("*.css")
                          if "node_modules" not in path.parts and ".git" not in path.parts)
    except OSError:
        return []
    tokens: list[str] = []
    for path in candidates[:12]:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")[:80_000]
        except OSError:
            continue
        for name, color in re.findall(r"(--[A-Za-z0-9_-]*(?:color|accent|brand|primary|secondary)[A-Za-z0-9_-]*)\s*:\s*(#[0-9a-fA-F]{3,8})\b", content, re.I):
            tokens.append(f"{name}={color.upper()}")
        if path.suffix in {".js", ".ts", ".cjs"}:
            for name, color in re.findall(r"(?:['\"])?([A-Za-z0-9_-]*(?:color|accent|brand|primary|secondary)[A-Za-z0-9_-]*)(?:['\"])?\s*:\s*['\"]?(#[0-9a-fA-F]{3,8})\b", content, re.I):
                tokens.append(f"{name}={color.upper()}")
    return list(dict.fromkeys(tokens))[:12]


def _task_brand_context(managed: dict, workspace_root: str | None) -> str:
    """Apply task palette precedence: explicit request, repo tokens, neutral."""
    explicit = _explicit_task_brand_palette(str(managed.get("original_goal") or ""))
    if explicit:
        return "[FRONTEND_TASK_BRAND_CONTEXT]\nSOURCE=EXPLICIT_CURRENT_TASK\nPALETTE=" + ", ".join(explicit) + "\n"
    tokens = _repository_brand_tokens(workspace_root)
    if tokens:
        return "[FRONTEND_TASK_BRAND_CONTEXT]\nSOURCE=EXISTING_REPOSITORY_DESIGN_TOKENS\nTOKENS=" + ", ".join(tokens) + "\n"
    return "[FRONTEND_TASK_BRAND_CONTEXT]\nSOURCE=NEUTRAL_DEFAULTS_NO_BRAND_PALETTE\n"


def _frontend_quality_guidance(managed: dict, phase: dict, workspace_root: str | None = None) -> str:
    """Reusable, brand-agnostic guidance limited to marketing frontends."""
    requirements = managed.get("requirements") if isinstance(managed.get("requirements"), dict) else {}
    profile = requirements.get("task_profile", managed.get("task_profile"))
    if profile != "FRONTEND_ONLY_MARKETING_SITE":
        return ""
    browser_phase = "playwright" in (phase.get("required_mcp") or []) or bool(re.search(r"browser|playwright|ブラウザ", str(phase.get("goal") or ""), re.I))
    if browser_phase:
        return (
            "[FRONTEND_UI_QUALITY_SELF_REVIEW]\n"
            "Before final browser verification, check: one clear primary CTA and a visually secondary alternative; no repeated major external-destination CTA pair; distinct roles for hero/features/workflow/tooling/CTA; no more than two consecutive card-grid sections; readable hierarchy and max-width/spacing rhythm; intentional task-brand accent use; and mobile composition that remains intentional. Fix only bounded findings, then report only observed browser results.\n"
            + _task_brand_context(managed, workspace_root))
    return (
        "[FRONTEND_UI_QUALITY_RULES]\n"
        "This is a marketing/product frontend. Create a deliberate landing-page composition: give the hero, features, workflow, development tools, and closing CTA different jobs and visual treatments. Use one clear primary CTA plus a visually secondary alternative. For the same major external destination, use it in the header/hero and closing CTA at most; do not repeat a GitHub/Releases pair in every section or footer. Avoid more than two consecutive card-grid sections; alternate hierarchy, editorial content, product visuals, workflow steps, or split layouts. Keep readable max widths, a consistent spacing rhythm, strong heading/body hierarchy, fewer stronger elements, and a purposeful mobile layout. Use the selected brand accent selectively for emphasis, CTAs, status, or key highlights rather than as a broad wash.\n"
        "When an explicit brand palette or existing design system is available, use its accent colors intentionally and consistently for primary CTAs, active and focus states, badges, small emphasis, status/progress, and selective borders or icons. Avoid flooding large surfaces with an accent unless the requested visual direction calls for it. Preserve repository design tokens unless the current task explicitly requests a redesign; otherwise use restrained neutral defaults and never invent official branding.\n"
        "Before handing off to browser verification, run this compact self-review: CTA hierarchy and deduplication; distinct section roles; varied composition; visual hierarchy/spacing; brand accent discipline; and mobile layout intent.\n"
        + _task_brand_context(managed, workspace_root))


def _mcp_evidence(task_id: str, server_id: str, *, status: str, purpose: str,
                  tool_name: str = "", result: object = None, error: str = "",
                  resource_mode: str = "") -> dict:
    summary = json.dumps(result, ensure_ascii=False, sort_keys=True)[:500] if result is not None else ""
    item = {"evidence_id": str(uuid.uuid4()), "created_at": time.time(), "mcp_name": server_id, "tool_name": tool_name, "status": status,
            "purpose": purpose[:240], "result_summary": summary, "error": error[:300]}
    if resource_mode:
        item["resource_mode"] = resource_mode
    if server_id == "animejs":
        item["server"] = "animejs-reference"
        item["reference_version"] = "V4"
    task = db.coding_task(task_id) or {}
    db.update_coding_task(task_id, mcp_evidence=[*(task.get("mcp_evidence") or []), item])
    return item


def _persisted_requirements(managed: dict) -> dict:
    """Use task-scoped requirements after creation; retain a legacy migration path."""
    requirements=managed.get("requirements") if isinstance(managed.get("requirements"),dict) else {}
    required={"required_capabilities","forbidden_capabilities","task_profile","execution_mode","required_mcp"}
    if required <= set(requirements):
        # Add plural and verification fields to a task made by the preceding
        # schema without changing its user-derived MCP choices.
        if "required_mcps" not in requirements or "required_verification" not in requirements:
            requirements = {**requirements,
                            "required_mcps": list(requirements.get("required_mcp") or []),
                            "required_verification": requirements.get("required_verification") or []}
            db.update_coding_task(managed["id"], requirements=requirements,
                                  required_mcp=requirements["required_mcps"])
        return requirements
    requirements=normalize_coding_requirements(managed["original_goal"])
    db.update_coding_task(managed["id"],requirements=requirements,
                          execution_mode=requirements["execution_mode"],task_profile=requirements["task_profile"],required_mcp=requirements["required_mcps"])
    print(f"TASK_ID={managed['id']} REQUIREMENTS_NORMALIZED=LEGACY_BACKFILL",file=__import__('sys').stderr,flush=True)
    return requirements


def _frontend_benchmark_invariant(requirements: dict) -> bool:
    required=set(requirements.get("required_capabilities") or [])
    forbidden=set(requirements.get("forbidden_capabilities") or [])
    return "frontend" in required and {"backend","database"} <= forbidden


def _planning_preflight_context(task_id: str, workspace_root: str | None) -> dict:
    """Bounded completed orchestration facts supplied to the planner."""
    paths: list[str] = []
    if workspace_root:
        root = Path(workspace_root)
        try:
            paths = sorted(item.name for item in root.iterdir()
                           if item.name not in {".git", "node_modules", ".venv"})[:30]
        except OSError:
            pass
    task = db.coding_task(task_id) or {}
    evidence = [{key: item.get(key) for key in ("mcp_name", "tool_name", "status", "purpose", "result_summary")}
                for item in (task.get("mcp_evidence") or []) if isinstance(item, dict) and item.get("status") == "PASS"]
    return {"repo_summary": {"workspace_entries": paths}, "shadcn_mcp_evidence": [item for item in evidence if item["mcp_name"] == "shadcn"],
            "animejs_mcp_evidence": [item for item in evidence if item["mcp_name"] == "animejs"],
            "required_stack": ["React", "TypeScript", "Vite", "Tailwind CSS", "shadcn/ui"],
            "constraints": ["Required MCP preflight is already complete.", "Do not emit repo inspection, MCP consultation, planning, or final reporting as a task phase."]}


def _animejs_resource_diagnostics(resolution: dict, *, initialize: str = "NOT_RUN",
                                  tools_list: str = "NOT_RUN", available: str = "NO",
                                  tool_call: str = "NOT_RUN", reason: str | None = None) -> None:
    """Emit stable startup diagnostics consumed by the desktop log/QA harness."""
    diagnostic_reason = reason or resolution.get("reason") or "UNKNOWN"
    if diagnostic_reason == "READY":
        diagnostic_reason = "NONE"
    values = {
        "ANIMEJS_MCP_REGISTERED": "YES" if resolution.get("registered") else "NO",
        "ANIMEJS_MCP_ENABLED": "YES" if resolution.get("enabled") else "NO",
        "ANIMEJS_NODE_RUNTIME_AVAILABLE": "YES" if resolution.get("node_runtime_available") else "NO",
        "ANIMEJS_SERVER_RESOURCE_AVAILABLE": "YES" if resolution.get("server_resource_available") else "NO",
        "ANIMEJS_CORPUS_RESOURCE_AVAILABLE": "YES" if resolution.get("corpus_resource_available") else "NO",
        "ANIMEJS_MCP_RESOURCE_MODE": str(resolution.get("resource_mode") or "NONE"),
        "MCP_RESOURCE_MODE": str(resolution.get("resource_mode") or "NONE"),
        "ANIMEJS_MCP_INITIALIZE_AVAILABLE": initialize,
        "ANIMEJS_MCP_TOOLS_LIST_AVAILABLE": tools_list,
        "ANIMEJS_MCP_INITIALIZE": initialize,
        "ANIMEJS_MCP_TOOLS_LIST": tools_list,
        "ANIMEJS_MCP_TOOL_CALL": tool_call,
        "ANIMEJS_MCP_AVAILABLE": available,
        "ANIMEJS_MCP_REASON": str(diagnostic_reason),
        "MCP_UNAVAILABLE_REASON": str(diagnostic_reason),
    }
    print(" ".join(f"{key}={value}" for key, value in values.items()), file=__import__('sys').stderr, flush=True)


def _run_required_mcp(task_id: str, server_id: str, workspace_root: str | None, purpose: str) -> tuple[dict | None, str | None]:
    """Run the mandatory lifecycle and return bounded, persisted telemetry.

    A required MCP that is unavailable is a truthful blocked condition.  This
    never falls back to a prose-only claim or lets Qwen decide to skip it.
    """
    if server_id == "animejs":
        task = db.coding_task(task_id) or {}
        project = animejs_project_version(workspace_root)
        compatibility = animejs_version_compatibility(project, str(task.get("original_goal") or ""))
        project = {**project, "ANIMEJS_REFERENCE_VERSION": "4", "ANIMEJS_VERSION_COMPATIBILITY": compatibility}
        requirements = task.get("requirements") if isinstance(task.get("requirements"), dict) else {}
        db.update_coding_task(task_id, requirements={**requirements, "animejs_project": project})
        if compatibility in {"CONFLICT_V3_V4", "UNKNOWN"}:
            item = _mcp_evidence(task_id, server_id, status="VERSION_CONFLICT", purpose=purpose,
                                 result=project, error="VERSION_CONFLICT" if compatibility == "CONFLICT_V3_V4" else "ANIMEJS_PROJECT_VERSION_UNKNOWN")
            return None, item["error"]
    definition = server_definition(server_id)
    if not definition or not definition.get("enabled_by_policy"):
        item = _mcp_evidence(task_id, server_id, status="BLOCKED", purpose=purpose,
                             error="MCP is not enabled by the packaged policy")
        return None, item["error"]
    resolution = node_mcp_resource_status(server_id)
    command = node_mcp_launch_command(server_id)
    if server_id == "animejs":
        _animejs_resource_diagnostics(resolution)
    if not command:
        item = _mcp_evidence(task_id, server_id, status="BLOCKED", purpose=purpose,
                             error=str(resolution.get("reason") or "bundled MCP runtime is unavailable"),
                             resource_mode=str(resolution.get("resource_mode") or ""))
        return None, item["error"]
    resource_mode = str(resolution.get("resource_mode") or "")
    runtime = MCPRuntime(command, definition.get("allowed_tools") or [],
                         timeout=float(definition.get("tool_timeout") or 20),
                         startup_timeout=float(definition.get("startup_timeout") or 10),
                         shutdown_timeout=float(definition.get("shutdown_timeout") or 2),
                         cwd=workspace_root)
    try:
        runtime.start()
        initialized = runtime.initialize()
        if server_id == "animejs":
            _animejs_resource_diagnostics(resolution,
                                          initialize="YES" if initialized.get("status") == "AVAILABLE" else "NO",
                                          reason=None if initialized.get("status") == "AVAILABLE" else "INITIALIZE_FAILED")
        if initialized.get("status") != "AVAILABLE":
            item = _mcp_evidence(task_id, server_id, status="FAILED", purpose=purpose,
                                 tool_name="initialize", result=initialized,
                                 error=str(initialized.get("error") or "initialize failed"), resource_mode=resource_mode)
            return None, item["error"]
        _mcp_evidence(task_id, server_id, status="PASS", purpose=purpose,
                      tool_name="initialize", result=initialized.get("response"), resource_mode=resource_mode)
        listing = runtime.tools_list()
        if server_id == "animejs":
            _animejs_resource_diagnostics(resolution,
                                          initialize="YES",
                                          tools_list="YES" if listing.get("status") == "AVAILABLE" else "NO",
                                          reason=None if listing.get("status") == "AVAILABLE" else "TOOLS_LIST_FAILED")
        if listing.get("status") != "AVAILABLE":
            item = _mcp_evidence(task_id, server_id, status="FAILED", purpose=purpose,
                                 tool_name="tools/list", result=listing,
                                 error=str(listing.get("error") or "tools/list failed"), resource_mode=resource_mode)
            return None, item["error"]
        tool = next(iter(definition.get("allowed_tools") or []), "")
        listed_tools = {(entry or {}).get("name") for entry in ((listing.get("response") or {}).get("result") or {}).get("tools", [])
                        if isinstance(entry, dict)}
        if tool not in listed_tools:
            if server_id == "animejs":
                _animejs_resource_diagnostics(resolution, initialize="YES", tools_list="YES", reason="TOOLS_LIST_FAILED")
            item = _mcp_evidence(task_id, server_id, status="FAILED", purpose=purpose,
                                 tool_name="tools/list", result=listing,
                                 error=f"required allowed tool is absent: {tool}", resource_mode=resource_mode)
            return None, item["error"]
        _mcp_evidence(task_id, server_id, status="PASS", purpose=purpose,
                      tool_name="tools/list", result=listing.get("response"), resource_mode=resource_mode)
        arguments = ({"query": "card", "registries": ["@shadcn"], "limit": 5} if server_id == "shadcn" else
                     {"query": "scroll timeline stagger React lifecycle scope reduced motion cleanup"} if server_id == "animejs" else
                     {"url": "http://127.0.0.1:5173"} if server_id == "playwright" else {})
        called = runtime.call(tool, arguments)
        if called.get("status") != "AVAILABLE":
            if server_id == "animejs":
                _animejs_resource_diagnostics(resolution, initialize="YES", tools_list="YES", tool_call="NO", reason="TOOL_CALL_FAILED")
            item = _mcp_evidence(task_id, server_id, status="FAILED", purpose=purpose,
                                 tool_name=tool, result=called,
                                 error=str(called.get("error") or "tool call failed"), resource_mode=resource_mode)
            return None, item["error"]
        response = called.get("response") or {}
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(response, dict) and response.get("error"):
            result = response.get("error")
        result_text = json.dumps(result, ensure_ascii=False)[:1000]
        if isinstance(result, dict) and result.get("isError"):
            network_block = server_id == "shadcn" and bool(re.search(r"ENOTFOUND|network|fetch|https?://", result_text, re.I))
            error = "required shadcn registry query blocked by approved network policy" if network_block else "required MCP tool returned an error"
            item = _mcp_evidence(task_id, server_id, status="BLOCKED_NETWORK" if network_block else "FAILED",
                                 purpose=purpose, tool_name=tool, result=result, error=error, resource_mode=resource_mode)
            return None, item["error"]
        item = _mcp_evidence(task_id, server_id, status="PASS", purpose=purpose,
                             tool_name=tool, result=result, resource_mode=resource_mode)
        if server_id == "animejs":
            _animejs_resource_diagnostics(resolution, initialize="YES", tools_list="YES", tool_call="PASS", available="YES")
        return item, None
    finally:
        runtime.close()

def _run_coding_planning(managed: dict) -> None:
    task_id=managed["id"]
    requirements=_persisted_requirements(managed)
    diagnostics={"TASK_PROFILE":requirements["task_profile"],"EXECUTION_MODE":requirements["execution_mode"],
                 "POSITIVE_SIGNALS":requirements["required_capabilities"],"NEGATED_SIGNALS":requirements["forbidden_capabilities"]}
    print(" ".join(f"{key}={json.dumps(value,ensure_ascii=False)}" for key,value in diagnostics.items()),file=__import__("sys").stderr,flush=True)
    execution_mode=diagnostics["EXECUTION_MODE"]
    profile=diagnostics["TASK_PROFILE"]
    required_mcp=list(requirements["required_mcps"])
    if _frontend_benchmark_invariant(requirements) and (profile != "FRONTEND_ONLY_MARKETING_SITE" or execution_mode != "NORMAL"):
        print(f"TASK_ID={task_id} CONTROL_PLANE_INVARIANT_FAILURE=TASK_PROFILE_MISMATCH TASK_PROFILE={profile} EXECUTION_MODE={execution_mode}",file=__import__('sys').stderr,flush=True)
        db.update_coding_task(task_id,status="BLOCKED",activity="NONE",recovery_action="NONE",recovery_reason="TASK_PROFILE_MISMATCH")
        db.add_message(managed["conversation_id"],"assistant","Coding Task は正規化済み要件と実行プロファイルが一致しないため開始しませんでした。",time.time(),str(uuid.uuid4()),task_id)
        return
    db.update_coding_task(task_id, execution_mode=execution_mode, task_profile=profile, required_mcp=required_mcp,requirements=requirements)
    print(f"TASK_ID={task_id} REQUIREMENTS_NORMALIZED=PASS TASK_CREATED=true SHADCN_REQUIRED={'YES' if 'shadcn' in required_mcp else 'NO'}",file=__import__('sys').stderr,flush=True)
    # Required MCP availability is part of the control plane.  Do this before
    # any model planning so an invalid benchmark never spends a Qwen run.
    for server_id in required_mcp:
        # Browser verification is deliberately deferred to its sole final
        # phase.  shadcn consultation, however, must finish before Qwen can
        # implement a benchmark that explicitly requires it.
        if server_id == "playwright":
            continue
        definition=server_definition(server_id)
        resolution = node_mcp_resource_status(server_id)
        command = node_mcp_launch_command(server_id)
        if server_id == "animejs":
            _animejs_resource_diagnostics(resolution)
        if not definition or not definition.get("enabled_by_policy") or not command:
            evidence=_mcp_evidence(task_id, server_id, status="BLOCKED", purpose="required MCP preflight",
                                   error=(str(resolution.get("reason") or "required MCP is unavailable before planning")
                                          if not command else "required MCP is unavailable before planning"),
                                   resource_mode=str(resolution.get("resource_mode") or ""))
            db.update_coding_task(task_id,status="BLOCKED",activity="NONE",recovery_action="NONE",
                                  recovery_reason="REQUIRED_MCP_UNAVAILABLE")
            response=f"Coding Task は開始しませんでした。必須の {server_id} MCP を利用できません。"
            print(f"TASK_ID={task_id} TASK_PROFILE={profile} REQUIRED_MCP={server_id} MCP_PREFLIGHT=BLOCKED ERROR={evidence['error']}",file=__import__('sys').stderr,flush=True)
            db.add_message(managed["conversation_id"],"assistant",response,time.time(),str(uuid.uuid4()),task_id)
            return
        evidence, error = _run_required_mcp(task_id, server_id, (db.project((db.conversation(managed["conversation_id"]) or {}).get("project_id", "")) or {}).get("workspace_path"),
                                            "component/pattern selection for OLCR marketing site")
        if error:
            reason = "REQUIRED_MCP_NETWORK_BLOCKED" if (db.coding_task(task_id) or {}).get("mcp_evidence", [{}])[-1].get("status") == "BLOCKED_NETWORK" else "REQUIRED_MCP_UNAVAILABLE"
            db.update_coding_task(task_id,status="BLOCKED",activity="NONE",recovery_action="NONE",recovery_reason=reason)
            response=f"Coding Task は開始しませんでした。必須の {server_id} MCP の検証に失敗しました。"
            print(f"TASK_ID={task_id} TASK_PROFILE={profile} REQUIRED_MCP={server_id} MCP_PREFLIGHT=BLOCKED ERROR={error}",file=__import__('sys').stderr,flush=True)
            db.add_message(managed["conversation_id"],"assistant",response,time.time(),str(uuid.uuid4()),task_id)
            return
        requirements={**requirements,"preflight":{**(requirements.get("preflight") or {}),server_id:"PASS"}}
        db.update_coding_task(task_id,requirements=requirements)
        print(f"TASK_ID={task_id} SHADCN_PREFLIGHT={'PASS' if server_id == 'shadcn' else 'NOT_APPLICABLE'} SHADCN_EVIDENCE_PERSISTED={'PASS' if server_id == 'shadcn' else 'NOT_APPLICABLE'} ANIMEJS_MCP_PREFLIGHT={'PASS' if server_id == 'animejs' else 'NOT_APPLICABLE'}",file=__import__('sys').stderr,flush=True)
    planning_suffix=(" Do not create verification-only phases; attach focused verification to each implementation batch and consolidated verification to the final batch." if execution_mode == "HEAVY_BATCHED" else "")
    workspace_root=(db.project((db.conversation(managed["conversation_id"]) or {}).get("project_id", "")) or {}).get("workspace_path")
    preflight=_planning_preflight_context(task_id,workspace_root)
    mutation_mode=str(requirements.get("mutation_mode") or "IMPLEMENTATION")
    print(f"TASK_ID={task_id} CODING_MUTATION_MODE={mutation_mode} FIX_REASON={requirements.get('fix_reason','')} "
          f"FIX_SCOPE_EXPANDED=NO FIX_ESCALATED_TO_IMPLEMENTATION=NO FIX_RETRY_COUNT={managed.get('retry_count',0)}", file=__import__('sys').stderr, flush=True)
    planner_input=(plan_prompt(managed["original_goal"],execution_mode,mutation_mode)+planning_suffix
                   + "\n[ORCHESTRATOR_PREFLIGHT_ALREADY_COMPLETED]\n"
                   + json.dumps(preflight, ensure_ascii=False, sort_keys=True))
    print(f"TASK_ID={task_id} QWEN_PLANNING_AFTER_PREFLIGHT=YES",file=__import__('sys').stderr,flush=True)
    plan,_=_generate_plan(task_id,managed["original_goal"],planner_input,"QWEN_PLANNING")
    if not plan:
        _transition_resumable(task_id,"PLANNING","RECOVERABLE_INTERNAL","RECOVERY_REVIEW")
        response="Coding Task の計画を検証できませんでした。実装は開始していません。"
    else:
        original_phase_count=len(plan.get("phases") or [])
        if profile == "FRONTEND_ONLY_MARKETING_SITE":
            plan=normalize_task_graph(plan,profile,required_mcp)
            selected=(requirements.get("selected_mcps") or [])
            if selected and plan.get("tasks"):
                plan["tasks"][0]["selected_mcp"] = selected
            print(f"TASK_GRAPH_NORMALIZATION=PASS TASK_PROFILE={profile} ORIGINAL_PLAN_PHASE_COUNT={original_phase_count} NORMALIZED_PLAN_PHASE_COUNT={len(plan.get('phases') or [])} FINAL_REPORT_PHASE_COUNT=0",file=__import__('sys').stderr,flush=True)
        elif execution_mode == "NORMAL":
            candidate=compact_normal_plan(plan,managed["original_goal"])
            compact_errors=validate_plan(candidate,managed["original_goal"])
            if not compact_errors:
                plan=candidate
            print(f"NORMAL_PLAN_COMPACTION={'PASS' if len(plan.get('phases') or []) < original_phase_count else 'NOT_APPLICABLE'} ORIGINAL_PLAN_PHASE_COUNT={original_phase_count} COMPACTED_PLAN_PHASE_COUNT={len(plan.get('phases') or [])}",file=__import__('sys').stderr,flush=True)
        # Planner output cannot claim work was completed before execution.
        plan={**plan,"phases":[{**phase,"status":"pending"} for phase in plan.get("phases",[])]}
        _initialize_subtask_progress(task_id,plan)
        protected_scope=_plan_authorization_boundary(plan)
        if protected_scope:
            authorization={"requested_scope":protected_scope,"reason":"The plan includes a protected operation outside ordinary repository implementation.","source":"plan","state":"PENDING"}
            db.update_coding_task(task_id,status="WAITING_FOR_USER",activity="SCOPE_AUTHORIZATION",execution_mode=execution_mode,task_profile=profile,required_mcp=required_mcp,
                                  pending_plan=plan,pending_authorization=authorization,
                                  pending_user_confirmation=1)
            response=("計画の作成が完了しました。次の操作には追加の承認が必要です。\n\n"
                      + "\n".join(f"- {item}" for item in protected_scope)
                      + "\n\n内容を確認し、「承認します」と返信してください。")
        else:
            approved_scope={"source":"original_request","requested_scope":list((plan.get("scope") or {}).get("allowed") or [])}
            db.update_coding_task(task_id,status="QUEUED",activity="NONE",plan=plan,plan_revision=0,execution_mode=execution_mode,task_profile=profile,required_mcp=required_mcp,batch_cursor=0,batch_handoff=None,
                                  approved_scopes=[approved_scope],pending_authorization=None,
                                  pending_user_confirmation=0)
            db.enqueue_coding_task(task_id)
            _coding_scheduler_wake.set()
            response=("計画の作成が完了しました。承認済みの依頼範囲で実装と検証を継続します。\n\n実装計画\n"
                      + "\n".join(f"Phase {i} / {len(plan['phases'])}\n{p['goal']}\n確認: " + ", ".join(p['verify']) for i,p in enumerate(plan["phases"],1)))
    db.add_message(managed["conversation_id"],"assistant",response,time.time(),str(uuid.uuid4()),task_id)

def _coding_scheduler() -> None:
    while True:
        _coding_scheduler_wake.wait()
        _coding_scheduler_wake.clear()
        while (managed:=db.next_queued_coding_task()):
            try:
                if managed.get("activity") == "QWEN_PLANNING" or not managed.get("plan"):
                    _run_coding_planning(managed)
                else:
                    workspace=(db.project((db.conversation(managed["conversation_id"]) or {}).get("project_id","")) or {}).get("workspace_path")
                    run_managed_task(managed["id"],workspace)
            except Exception as exc:
                # A worker exception must release the queue and leave a
                # truthful resumable task rather than silently killing the
                # scheduler thread.
                stage="MODEL_CALL" if isinstance(exc,ModelFailure) else "SCHEDULER"
                _transition_resumable(managed["id"],stage,"RECOVERABLE_INTERNAL","RECOVERY_REVIEW",exc)
                print(f"TASK_ID={managed['id']} FAILURE_STAGE={stage} ERROR_TYPE={type(exc).__name__} NEXT_TASK_ACTION=RESUME_REQUIRED TASK_STATUS=RESUMABLE TASK_ACTIVITY=NONE",file=__import__('sys').stderr,flush=True)

threading.Thread(target=_coding_scheduler,name="olcr-coding-scheduler",daemon=True).start()

@app.post("/api/projects")
def create_project(value: ProjectInput):
    return db.create_project(value.name,valid_workspace(value.workspace_path),time.time(),str(uuid.uuid4()))

@app.patch("/api/projects/{project_id}")
def update_project(project_id: str,value:ProjectUpdate):
    result=db.update_project(project_id,value.name,valid_workspace(value.workspace_path) if value.workspace_path is not None else None,value.archived,time.time())
    if not result: raise HTTPException(404,"project not found")
    return result

@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    if not db.delete_project(project_id): raise HTTPException(404,"project not found")
    return {"deleted":True,"project_id":project_id}

@app.get("/api/projects/{project_id}/conversations")
def list_conversations(project_id: str):
    if not db.project(project_id): raise HTTPException(404,"project not found")
    return {"conversations":db.conversations(project_id)}

@app.get("/api/projects/{project_id}/conversations/{conversation_id}")
def project_conversation(project_id: str, conversation_id: str):
    value = db.conversation(conversation_id)
    if not value or value["project_id"] != project_id:
        raise HTTPException(404, "conversation not found")
    return value

@app.post("/api/projects/{project_id}/conversations")
def create_conversation(project_id: str):
    project = db.project(project_id)
    if not project: raise HTTPException(404, "project not found")
    conversation_id = str(uuid.uuid4())
    db.create_conversation("New conversation", time.time(), conversation_id, project_id)
    return db.conversation(conversation_id)

@app.patch("/api/projects/{project_id}/conversations/{conversation_id}")
def rename_project_conversation(project_id: str, conversation_id: str, value: ConversationTitleInput):
    conversation = db.conversation(conversation_id)
    if not conversation or conversation["project_id"] != project_id:
        raise HTTPException(404, "conversation not found")
    return db.rename_conversation(conversation_id, value.title, time.time())

@app.get("/api/projects/{project_id}/core-context")
def get_core_context(project_id: str):
    if not db.project(project_id): raise HTTPException(404,"project not found")
    return {"project_id":project_id,"content":project_context(project_id),
            "source":db.load_settings().get("project_core_source:"+project_id)}

@app.put("/api/projects/{project_id}/core-context")
def set_core_context(project_id: str,value:CoreContextInput):
    if not db.project(project_id): raise HTTPException(404,"project not found")
    db.save_setting("project_core_context:"+project_id,value.content,time.time())
    db.save_setting("project_core_source:"+project_id,None,time.time())
    return {"project_id":project_id,"content":value.content}

@app.post("/api/projects/{project_id}/core-context/load")
def load_project_context(project_id: str, value: ContextFileInput):
    project = db.project(project_id)
    if not project: raise HTTPException(404, "PROJECT_NOT_FOUND")
    # Typed paths are only accepted within the explicitly configured workspace.
    # A picker/drop never grants the parent directory implicitly.
    if not project.get("workspace_path"):
        raise HTTPException(422, "WORKSPACE_REQUIRED: select an authorized workspace first")
    raw = Path(value.path).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise HTTPException(422, "CONTEXT_PATH_INVALID")
    if any(p.is_symlink() for p in (raw, *raw.parents)):
        raise HTTPException(403, "CONTEXT_SYMLINK_DENIED")
    root = Path(project["workspace_path"]).resolve()
    path = raw.resolve()
    if root not in path.parents: raise HTTPException(403, "CONTEXT_OUTSIDE_WORKSPACE")
    try:
        if not path.is_file() or path.stat().st_size > 200_000:
            raise ValueError("CONTEXT_FILE_INVALID_OR_TOO_LARGE")
        content = path.read_text(encoding="utf-8")
        if len(content) > 50_000 or not content.strip(): raise ValueError("CONTEXT_CONTENT_INVALID")
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    with db.connect() as conn:
        for key, item in (("project_core_context:", content), ("project_core_source:", str(path))):
            conn.execute("INSERT OR REPLACE INTO application_settings VALUES(?,?,?)",
                (key+project_id, json.dumps(item), time.time()))
    return get_core_context(project_id)

@app.post("/api/projects/{project_id}/core-context/reload")
def reload_project_context(project_id: str):
    source = db.load_settings().get("project_core_source:"+project_id)
    if not source: raise HTTPException(422, "CONTEXT_NO_SOURCE")
    return load_project_context(project_id, ContextFileInput(path=source))

@app.get("/api/web/settings")
def web_settings():
    brave = bool(provider_key("brave"))
    tavily = bool(provider_key("tavily"))
    return {"web_mode":settings.web_mode, "web_provider":settings.web_provider,
        "providers":["none", "brave", "tavily", "duckduckgo"],
        "credentials":{"none":"Disabled", "duckduckgo":"Not required",
            "brave":"Configured" if brave else "Not configured",
            "tavily":"Configured" if tavily else "Not configured"},
        "setup_guidance": setup_guidance(settings.web_provider)}

@app.get("/api/external-tools")
def external_tools():
    return {"external_access_enabled": settings.external_access_enabled,
            "tools": external_tool_status(settings.external_access_enabled)}

def _world_bank_table_block(result: dict) -> dict | None:
    """Build a provider-neutral, display-only table from normalized data.

    The provider payload remains authoritative and unchanged.  This block is
    optional presentation metadata for the GUI; React renders cell values as
    text, so raw values can never become executable markup.
    """
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return None
    def country_name(item):
        country = item.get("country")
        if isinstance(country, dict):
            country = country.get("value") or country.get("id")
        return country or item.get("countryiso3code") or "Unknown country"
    def format_value(value):
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}".rstrip("0").rstrip(".")
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    countries=[]
    for item in items:
        name=country_name(item)
        if name not in countries: countries.append(name)
    years=sorted({int(item["date"]) for item in items if str(item.get("date", "")).isdigit()}, reverse=True)
    if not countries or not years:
        return None
    values={(country_name(item), int(item["date"])): item.get("value") for item in items if str(item.get("date", "")).isdigit()}
    columns=[{"key":"year", "label":"Year", "align":"right"}]
    columns.extend({"key":f"country_{index}", "label":country, "align":"right"} for index,country in enumerate(countries))
    rows=[]
    for year in years:
        row={"year": year}
        row.update({f"country_{index}": format_value(values.get((country, year))) for index,country in enumerate(countries)})
        rows.append(row)
    return {"type":"table", "caption":"World Bank", "columns":columns, "rows":rows,
            "source_label":"World Bank"}

def external_tool_display_blocks(result: dict) -> list[dict]:
    """Return optional structured display blocks without provider branching in the UI."""
    if result.get("tool_id") == "statistics.world_bank":
        block=_world_bank_table_block(result)
        return [block] if block else []
    return []

def external_tool_summary(result: dict) -> str:
    """Human-readable command copy; the complete normalized data stays typed."""
    tool, data = result["tool_id"], result["data"]
    if tool == "weather.open_meteo":
        place=data.get("location", {}); current=data.get("current", {})
        values=[f"Weather · Open-Meteo", ", ".join(x for x in (place.get("name"),place.get("country")) if x)]
        if current.get("temperature_2m") is not None: values.append(f"{current['temperature_2m']}°C" + (f" · Feels like {current['apparent_temperature']}°C" if current.get("apparent_temperature") is not None else ""))
        if current.get("wind_speed_10m") is not None: values.append(f"Wind {current['wind_speed_10m']} km/h")
        return "\n".join(values)
    if tool == "currency.frankfurter":
        amount=data.get("amount"); converted=data.get("converted_amount"); base=data["base"]; quote=data["quote"]
        values=["Currency · Frankfurter"]
        if amount is not None: values.append(f"{amount} {base} ≈ {converted} {quote}")
        values.append(f"1 {base} = {data['rate']} {quote}")
        values.append(f"Rate date: {data.get('rate_date') or 'not provided'} · Daily reference rate")
        return "\n".join(values)
    if tool == "research.openalex":
        works=data.get("works", []); lines=["Research · OpenAlex"]
        for index, work in enumerate(works[:5], 1):
            detail=" · ".join(x for x in (", ".join(work.get("authors", [])[:2]), str(work.get("publication_year") or "")) if x)
            lines.append(f"{index}. {work.get('title') or 'Untitled'}" + (f"\n   {detail}" if detail else "") + (f"\n   {work['url']}" if work.get("url") else ""))
        return "\n".join(lines)
    if tool == "knowledge.wikimedia":
        return "\n".join(["Wikipedia · Wikimedia", data.get("title", ""), data.get("extract", "")[:900], f"Source: {data.get('page_url','')}"])
    if tool == "chemistry.compound":
        return "\n".join(x for x in [
            str(result.get("provider") or "PubChem"),
            f"Compound: {data.get('compound') or data.get('query','')}",
            f"CID: {data.get('cid')}" if data.get("cid") is not None else "",
            f"Formula: {data.get('molecular_formula')}" if data.get("molecular_formula") else "",
            f"Molecular weight: {data.get('molecular_weight')}" if data.get("molecular_weight") else "",
            f"IUPAC name: {data.get('iupac_name')}" if data.get("iupac_name") else "",
        ] if x)
    if tool == "government.us_federal_register":
        items = [item for item in (data.get("items") or [])[:5] if isinstance(item, dict)]
        if not items:
            return "今回のFederal Register検索では、人工知能に関係する最近のruleまたはnoticeは見つかりませんでした。"
        lines = ["Federal Registerで人工知能に関係する最近の文書が見つかりました。", ""]
        rendered = 0
        for item in items:
            title = item.get("title") or item.get("document_number")
            if not title:
                continue
            rendered += 1
            lines.append(f"{rendered}. {title}")
            document_type = item.get("document_type") or item.get("type")
            document_number = item.get("document_number")
            publication_date = item.get("publication_date") or item.get("publicationDate")
            agency = item.get("agency") or item.get("agencies")
            url = item.get("url") or item.get("html_url")
            if document_type:
                lines.append(f"   種別：{document_type}")
            if document_number:
                lines.append(f"   文書番号：{document_number}")
            if publication_date:
                lines.append(f"   公開日：{publication_date}")
            if agency:
                if isinstance(agency, list):
                    agency = ", ".join(str(value) for value in agency if value)
                if agency:
                    lines.append(f"   機関：{agency}")
            if url:
                lines.append(f"   {url}")
            lines.append("")
        if rendered == 0:
            return "今回のFederal Register検索では、人工知能に関係する最近のruleまたはnoticeは見つかりませんでした。"
        has_provenance = str(result.get("provider") or "").casefold() == "federal register" or any(
            isinstance(source, dict) and str(source.get("provider") or "").casefold() == "federal register"
            for source in (result.get("sources") or [])
        )
        if has_provenance:
            lines.append("出典：Federal Register")
        return "\n".join(lines)
    if tool == "math.symbolic":
        def format_latex(value):
            text = str(value or "").strip()
            # Normalize adapter output into a small, safe LaTeX subset. The
            # delimiters are presentation syntax consumed by the frontend.
            text = re.sub(r"\\(?:\(|\)|\[|\])", "", text)
            text = text.replace("$$", "").replace("$", "").strip()
            text = re.sub(r"\*\*\s*([A-Za-z0-9]+)", r"^{\1}", text)
            text = re.sub(r"\^\s*([0-9]+)", r"^{\1}", text)
            text = re.sub(r"(?<!\*)\s*\*\s*(?!\*)", r"\\,", text)
            return text
        operation = str(data.get("operation") or "symbolic").lower()
        labels = {"factor": "因数分解結果", "differentiate": "微分結果", "simplify": "簡約結果", "integrate": "積分結果"}
        label = labels.get(operation, "計算結果")
        expression = format_latex(data.get("expression"))
        value = format_latex(data.get("result"))
        subject = f"${expression}$ の" if expression else ""
        response = f"{subject}{label}は、\n$$\n{value}\n$$\nです。"
        # The deterministic math tool path carries a matching SymPy source;
        # ordinary Brain arithmetic has no such provider evidence and gets no
        # attribution.
        execution_evidence = result.get("provider") == "SymPy" and any(
            isinstance(source, dict) and source.get("provider") == "SymPy" for source in (result.get("sources") or [])
        )
        if execution_evidence:
            response += "\n\n使用ツール：SymPy"
        return response
    if tool == "statistics.world_bank":
        def country_name(item):
            country = item.get("country")
            if isinstance(country, dict):
                country = country.get("value") or country.get("id")
            return country or item.get("countryiso3code") or "Unknown country"
        def format_value(value):
            if value is None:
                return "—"
            try:
                number = float(value)
            except (TypeError, ValueError):
                return str(value)
            if number.is_integer():
                return f"{int(number):,}"
            return f"{number:,.2f}".rstrip("0").rstrip(".")
        items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        countries = []
        for item in items:
            name = country_name(item)
            if name not in countries:
                countries.append(name)
        years = sorted({int(item["date"]) for item in items if str(item.get("date", "")).isdigit()}, reverse=True)
        # Render every explicitly requested country/year cell. This is bounded
        # by the compiler's entity and year limits and avoids generic first-N
        # truncation that hid Germany and earlier years.
        values = {(country_name(item), int(item["date"])): item.get("value") for item in items if str(item.get("date", "")).isdigit()}
        lines = ["World Bank", "Year | " + " | ".join(countries), "--- | " + " | ".join("---" for _ in countries)]
        for year in years:
            lines.append(str(year) + " | " + " | ".join(format_value(values.get((country, year))) for country in countries))
        lines.append("Unit: provider native units (unscaled)")
        complete = bool(countries and years and data.get("request_complete") and all((country, year) in values for country in countries for year in years))
        data["render_complete"] = complete
        return "\n".join(lines)
    if tool == "earth.natural_event":
        lines = ["現在EONETに登録されている自然災害の例です。", ""]
        rendered = 0
        for item in (data.get("items") or [])[:5]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name") or item.get("id")
            if not title:
                continue
            rendered += 1
            lines.append(f"{rendered}. {title}")
            location = item.get("location")
            if location:
                lines.append(f"   場所：{location}")
            category = item.get("category")
            if isinstance(category, dict):
                category = category.get("title") or category.get("name") or category.get("id")
            if category:
                lines.append(f"   種別：{category}")
            date = item.get("date")
            if date:
                lines.append(f"   日付：{date}")
            lines.append("")
        if rendered == 0:
            return "今回のEONET検索では、表示できる自然災害データが見つかりませんでした。"
        has_provenance = str(result.get("provider") or "").upper().find("EONET") >= 0 or any(
            isinstance(source, dict) and "EONET" in str(source.get("provider") or "").upper()
            for source in (result.get("sources") or [])
        )
        if has_provenance:
            lines.extend(["出典：NASA EONET"])
        return "\n".join(lines)
    if tool == "geo.routing":
        origin, destination = data.get("origin"), data.get("destination")
        route = next((item for item in (data.get("items") or []) if isinstance(item, dict)), {})
        distance = route.get("distance_m", route.get("distance"))
        duration = route.get("duration_s", route.get("duration"))
        profile = str(data.get("profile") or route.get("profile") or "driving")
        transport = {"driving": "車", "walking": "徒歩", "cycling": "自転車"}.get(profile, profile)
        def format_distance(value):
            try:
                meters = float(value)
            except (TypeError, ValueError):
                return "距離不明"
            if meters >= 1000:
                km = f"{meters / 1000:.1f}".rstrip("0").rstrip(".")
                return f"約{km}km"
            return f"約{round(meters):,}m"
        def format_duration(value):
            try:
                seconds = max(0, int(round(float(value))))
            except (TypeError, ValueError):
                return "時間不明"
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            if hours:
                return f"約{hours}時間{minutes}分" if minutes else f"約{hours}時間"
            if minutes:
                return f"約{minutes}分{secs}秒" if secs else f"約{minutes}分"
            return f"約{secs}秒"
        sentence = f"{origin or '出発地'}から{destination or '目的地'}までの道路距離は{format_distance(distance)}で、推定所要時間は{transport}で{format_duration(duration)}です。"
        return sentence + "\n\n経路データ：OSRM"
    if tool == "web.archive_search":
        lines = [str(result.get("provider") or tool)]
        for item in (data.get("items") or [])[:5]:
            if isinstance(item, dict):
                timestamp, original = item.get("timestamp"), item.get("original")
                if timestamp and original:
                    lines.append(f"{timestamp} · {original}")
        return "\n".join(lines)
    # Extended providers return a common bounded envelope.  Keep the fallback
    # readable even when the optional composition model is unavailable; never
    # mislabel these results as Wikipedia or dump the full upstream payload.
    provider = result.get("provider") or tool
    lines = [f"{provider}"]
    items = data.get("items")
    if isinstance(items, list):
        for index, item in enumerate(items[:5], 1):
            if isinstance(item, dict):
                title = item.get("title") or item.get("name") or item.get("full_name") or item.get("id") or "Result"
                detail = item.get("description") or item.get("snippet") or item.get("url") or item.get("html_url")
                lines.append(f"{index}. {title}" + (f"\n   {detail}" if detail else ""))
            else:
                lines.append(f"{index}. {item}")
    elif isinstance(data.get("raw_metadata"), dict):
        response = data["raw_metadata"]
        summary = response.get("title") or response.get("message") or response.get("status")
        if summary is not None:
            lines.append(str(summary))
    elif isinstance(data.get("results"), dict):
        results = data["results"]
        timezone_name = data.get("display_timezone") or "UTC"
        lines.append(f"Timezone: {timezone_name}")
        for key in ("sunrise", "sunset", "civil_twilight_begin", "civil_twilight_end"):
            if results.get(key): lines.append(f"{key}: {results[key]}")
    return "\n".join(lines)

def router_decision(message: str) -> tuple[str, dict] | None:
    """Ask the configured generative Router for a strict, bounded decision.
    A missing/unavailable Router is a no-tool result; it never authorizes a call.
    """
    if not settings.router_model or settings.router_model == "embeddinggemma:latest":
        return None

    allowed_tools = "|".join(sorted(REGISTRY))
    prompt = ('Return exactly one JSON object, no Markdown. Schema: '
              '{"decision":"tool","tool_id":"' + allowed_tools + '","arguments":{}} '
              'or {"decision":"no_tool"} or {"decision":"needs_clarification","missing":["..."]}. '
              'Choose the most specific structured tool first: weather before Web Search, currency before Web Search, research for papers, and Wikimedia only when explicitly requested. Generic Web Search is only for open-web/news requests. '
              'Never output URLs, hosts, methods, credentials, authorization, or other keys.\nUSER: ' + message[:1000])
    try:
        messages=[{"role":"system","content":"You are OLCR Router. Output strict JSON only."},{"role":"user","content":prompt}]
        raw=runtime.model.generate(messages, settings.router_model, think=False)
        try: parsed=json.loads(str(raw.get("text", "")).strip().removeprefix("```json").removesuffix("```").strip())
        except (ValueError, TypeError, json.JSONDecodeError):
            raw=runtime.model.generate(messages+[{"role":"user","content":"Correction: return one valid JSON object only."}], settings.router_model, think=False)
            parsed=json.loads(str(raw.get("text", "")).strip().removeprefix("```json").removesuffix("```").strip())
        if not isinstance(parsed,dict) or parsed.get("decision") not in {"tool","no_tool","needs_clarification"}: return None
        if parsed["decision"] != "tool": return None
        tool_id=parsed.get("tool_id"); arguments=parsed.get("arguments")
        if tool_id not in REGISTRY or not isinstance(arguments,dict): return None
        # Provider adapters perform the final typed bounds and host validation.
        return tool_id, arguments
    except ModelFailure as exc:
        print("ROUTER_MODEL_STATUS=UNAVAILABLE_OR_INVALID", file=__import__('sys').stderr, flush=True)
        raise RouterUnavailable("ROUTER_MODEL_NOT_INSTALLED") from exc
    except (ValueError, TypeError, json.JSONDecodeError):
        print("ROUTER_DECISION_INVALID=YES", file=__import__('sys').stderr, flush=True)
        return None

def compose_external_result(request: str, result: dict) -> tuple[Task, str]:
    """Compose a provider result without re-entering generic retrieval routing."""
    sources = result.get("sources") if isinstance(result, dict) else None
    provider = result.get("provider") if isinstance(result, dict) else None
    # Provider attribution is authoritative only when the adapter returned a
    # matching source in this result. Never let a Brain completion invent it.
    if not isinstance(sources, list) or not sources or not provider or not any(
        isinstance(source, dict) and source.get("provider") == provider for source in sources
    ):
        task = Task(request)
        task.transition(TaskState.ROUTING); task.error = "PROVENANCE_UNAVAILABLE"; task.transition(TaskState.FAILED)
        return task, "構造化データ源から取得できませんでした。"
    # These outputs contain exact provider fields (or executable local-tool
    # results) whose wording must remain evidence-bound.  Render them
    # deterministically instead of allowing a Brain completion to invent
    # snapshot dates, chemical values, or alternate calculator sources.
    if result.get("tool_id") in {"chemistry.compound", "government.us_federal_register", "math.symbolic", "web.archive_search", "statistics.world_bank", "earth.natural_event", "geo.routing", "knowledge.wikimedia"}:
        task = Task(request)
        task.transition(TaskState.ROUTING); task.transition(TaskState.EXECUTING); task.transition(TaskState.COMPLETED)
        if result.get("tool_id") == "math.symbolic":
            print("SYMPY_DETERMINISTIC_RENDERER_USED=true BRAIN_COMPOSER_USED=false", file=__import__('sys').stderr, flush=True)
        if result.get("tool_id") == "earth.natural_event":
            print("CURRENT_DATA_TERMINAL=SUCCESS BRAIN_FALLBACK_BLOCKED=YES", file=__import__('sys').stderr, flush=True)
        owner = "Federal Register deterministic renderer" if result.get("tool_id") == "government.us_federal_register" else "DETERMINISTIC_RENDERER"
        print(f"FINAL_RESPONSE_OWNER={owner} TERMINAL_RESPONSE_READY=true DETERMINISTIC_RENDERER={result.get('tool_id')} BRAIN_COMPOSER_USED=NO GENERIC_COMPOSER_USED=NO", file=__import__('sys').stderr, flush=True)
        return task, external_tool_summary(result)
    payload = {"tool_id": result["tool_id"], "provider": result["provider"], "fetched_at": result["fetched_at"], "data": result["data"], "sources": result["sources"]}
    task, answer = runtime.compose_tool_result(request, payload)
    # A model can still emit a generic failure despite a successful typed
    # provider response. Give it one bounded recomposition with the same
    # evidence, then fall back to deterministic rendering.
    contradiction = bool(re.search(r"(?:取得できません|取得できなかった|アクセスできません|データがありません|情報がありません|could not retrieve|unable to access|no data)", answer or "", re.I))
    if contradiction:
        print("PROVIDER_SUCCESS_CONTRADICTION=YES COMPOSITION_RETRY_COUNT=1", file=__import__('sys').stderr, flush=True)
        retry_task, retry_answer = runtime.compose_tool_result(request, payload)
        if not re.search(r"(?:取得できません|取得できなかった|アクセスできません|データがありません|情報がありません|could not retrieve|unable to access|no data)", retry_answer or "", re.I):
            task, answer = retry_task, retry_answer
        else:
            print("PROVIDER_SUCCESS_CONTRADICTION=UNRESOLVED DETERMINISTIC_RENDERER=USED", file=__import__('sys').stderr, flush=True)
            answer = external_tool_summary(result)
    answer = answer or ""
    # A completion may mention a different well-known source from parametric
    # memory. Replace that draft with deterministic provider prose instead.
    known_sources = {
        "Wikidata", "World Bank", "Statistics Canada", "Eurostat", "OECD", "Open-Meteo", "Wikipedia",
        "Wolfram Alpha", "SymPy", "SciPy", "USGS", "NASA", "EONET", "NOAA", "ClinicalTrials.gov",
        "Open Library", "PubChem", "QuickChart", "MyMemory", "Free Dictionary", "Federal Register",
        "Overpass", "OSRM", "United Nations", "国際連合",
    }
    mentioned = {name for name in known_sources if re.search(re.escape(name), answer, re.I)}
    if any(name.casefold() != str(provider).casefold() and name.casefold() not in str(provider).casefold() for name in mentioned):
        print("PROVENANCE_CLAIM_REJECTED=UNSUPPORTED_SOURCE", file=__import__('sys').stderr, flush=True)
        return task, external_tool_summary(result)
    return task, answer or external_tool_summary(result)


def _external_tool_user_message(code: str, arguments: dict | None = None) -> str:
    """Map internal provider/compiler codes to bounded UI prose."""
    if code in {"ARGUMENT_COMPILATION_FAILED", "INVALID_TOOL_ARGUMENTS", "QUERY_TOO_LONG", "QUERY_COMPLEXITY_LIMIT"}:
        return "外部データの検索条件を解釈できませんでした。場所・対象・期間などを具体的に指定して再試行してください。"
    if code == "LOCATION_AMBIGUOUS":
        location = (arguments or {}).get("location", "指定された場所")
        return f"「{location}」に一致する地域を特定できませんでした。国名や都道府県名を追加してください。"
    if code == "LOCATION_NOT_FOUND":
        location = (arguments or {}).get("location", "指定された場所")
        return f"「{location}」を場所として特定できませんでした。もう少し具体的な地域名を指定してください。"
    if code.startswith(("PROVIDER_", "RATE_")):
        return "外部データを取得できませんでした。しばらくしてから再試行してください。"
    return "外部データを取得できませんでした。検索条件を確認して再試行してください。"

@app.put("/api/web/credentials")
def set_web_credential(value: WebCredentialInput):
    if value.provider not in {"brave", "tavily"}:
        raise HTTPException(422, "CREDENTIAL_PROVIDER_UNSUPPORTED")
    RUNTIME_PROVIDER_KEYS[value.provider] = value.key
    return web_settings()

@app.put("/api/web/settings")
def save_web_settings(value: WebSettingsInput):
    try: candidate = settings.with_overrides(value.model_dump())
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    with db.connect() as conn:
        for key, item in value.model_dump().items():
            conn.execute("INSERT OR REPLACE INTO application_settings VALUES(?,?,?)", (key,json.dumps(item),time.time()))
    rebuild(candidate)
    return web_settings()

@app.get("/api/commands")
def command_catalog(): return {"commands":catalog()}

@app.post("/api/commands")
def execute_command(value: CommandInput):
    try: spec, argument = resolve(value.text.strip())
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
    if not spec.enabled: raise HTTPException(422, spec.reason)
    pid = value.project_id
    if spec.requires_project and (not pid or not db.project(pid)):
        raise HTTPException(422, "PROJECT_REQUIRED")
    conversation_id = value.conversation_id or db.create_conversation(value.text, time.time(), str(uuid.uuid4()), pid)
    if not db.conversation(conversation_id): raise HTTPException(404, "conversation not found")
    db.add_message(conversation_id, "user", value.text, time.time(), str(uuid.uuid4()))
    command = spec.command
    response_kind = "command_result"
    if command == "/help": result = catalog()
    elif command == "/status": result = health()
    elif command in ("/models", "/option show"):
        result = {k:getattr(settings,k) for k in ("main_model","router_model","vision_model")}
    elif command.startswith("/context "):
        action = command.split()[1]
        if action == "show": result = get_core_context(pid)
        elif action == "reload": result = reload_project_context(pid)
        elif action == "load":
            if not argument: return {"kind":"ui_action", "ui_action":"core_context"}
            result = load_project_context(pid, ContextFileInput(path=argument.strip('"')))
        else: result = set_core_context(pid, CoreContextInput(content=argument if action == "set" else ""))
    elif command.startswith("/workspace "):
        if command.endswith("set"):
            if not argument: return {"kind":"ui_action", "ui_action":"workspace"}
            result = update_project(pid, ProjectUpdate(workspace_path=argument.strip('"')))
        else: result = {"workspace":db.project(pid)["workspace_path"]}
    elif command.startswith("/memory "):
        action = command.split()[1]
        if action != "show":
            current = settings.public_dict(); current["conversation_memory_enabled"] = action == "on"
            put_settings(SettingsInput(**current))
        result = {"scope":"global", "conversation_memory_enabled":settings.conversation_memory_enabled}
    elif command in ("/weather", "/currency", "/research", "/wiki"):
        if not settings.external_access_enabled: raise HTTPException(403, "EXTERNAL_ACCESS_REQUIRED")
        if not argument: raise HTTPException(422, "INVALID_TOOL_ARGUMENTS")
        try:
            if command == "/weather": result=execute_external_tool("weather.open_meteo", {"location":argument})
            elif command == "/currency":
                bits=argument.split()
                if len(bits) != 3: raise ExternalToolError("INVALID_TOOL_ARGUMENTS")
                result=execute_external_tool("currency.frankfurter", {"amount":float(bits[0]),"base":bits[1],"quote":bits[2]})
            elif command == "/research": result=execute_external_tool("research.openalex", {"query":argument})
            else: result=execute_external_tool("knowledge.wikimedia", {"query":argument, "language":"ja"})
        except ExternalToolError as exc:
            # Keep stable machine code in server diagnostics while exposing
            # bounded natural-language guidance to the GUI.
            print(f"TOOL_ARGUMENTS_VALID=NO TOOL_ARGUMENT_ERROR={exc}", file=__import__('sys').stderr, flush=True)
            raise HTTPException(422, _external_tool_user_message(str(exc))) from exc
        except ValueError as exc: raise HTTPException(422, _external_tool_user_message("INVALID_TOOL_ARGUMENTS")) from exc
        response_kind = "external_tool_result"
    elif command.startswith("/external "):
        action = command.split()[-1]
        if action in ("on", "off"):
            current=settings.public_dict(); current["external_access_enabled"] = action == "on"
            put_settings(SettingsInput(**current))
        result = {"external_access_enabled": settings.external_access_enabled, "tools": external_tool_status(settings.external_access_enabled)}
    elif command.startswith("/web"):
        action = command.split()[-1]
        if action in ("off","manual","auto"):
            save_web_settings(WebSettingsInput(web_mode=action,web_provider=settings.web_provider))
        elif command.startswith("/web provider") and action != "show":
            save_web_settings(WebSettingsInput(web_mode=settings.web_mode,web_provider="none" if action == "clear" else action))
        result = web_settings()
    else: raise HTTPException(422, "COMMAND_UNAVAILABLE")
    message = compose_external_result(value.text, result)[1] if response_kind == "external_tool_result" else json.dumps(result, ensure_ascii=False, indent=2)
    blocks = external_tool_display_blocks(result) if response_kind == "external_tool_result" else []
    db.add_message(conversation_id, "assistant", message, time.time(), str(uuid.uuid4()), blocks=blocks)
    return {"status":"ok", "conversation_id":conversation_id, "kind":response_kind, "tool_id":result.get("tool_id") if response_kind == "external_tool_result" else None, "provider":result.get("provider") if response_kind == "external_tool_result" else None,
        "data":result, "sources":result.get("sources", []) if response_kind == "external_tool_result" else [],
        "message":message}


@app.post("/api/chat")
def chat(value: ChatInput):
    print("GUI_CHAT_BACKEND_RECEIVED=true", file=__import__("sys").stderr, flush=True)
    project_id=value.project_id or db.default_project_id()
    conversation_id=value.conversation_id or db.create_conversation(value.message,time.time(),str(uuid.uuid4()),project_id)
    existing=db.conversation(conversation_id)
    if not existing: raise HTTPException(404,"conversation not found")
    if existing["project_id"] != project_id: raise HTTPException(403,"conversation does not belong to project")
    if value.message_id:
        prior=db.coding_task_for_source_message(value.message_id)
        if prior:
            response="同じ送信済みメッセージの Coding Task を参照しています。"
            print(f"CODING_TASK_IDEMPOTENT_HIT=true CODING_TASK_ID={prior['id']} NORMAL_RUNTIME_FALLBACK=false",file=__import__('sys').stderr,flush=True)
            return {"conversation_id":conversation_id,"coding_task_id":prior["id"],"response":response,"sources":[]}
    print("GUI_CHAT_VALIDATION=PASS GUI_CHAT_PROJECT_RESOLUTION=PASS GUI_CHAT_CONVERSATION_RESOLUTION=PASS", file=__import__("sys").stderr, flush=True)
    source_message_id=value.message_id or str(uuid.uuid4())
    attachment_meta=value.image if isinstance(value.image,dict) else value.attachment if isinstance(value.attachment,dict) else None
    user_blocks=([{"type":"attachment","name":str(attachment_meta.get("name") or "attachment")[:200],
                   "mime_type":str(attachment_meta.get("mime_type") or attachment_meta.get("mimeType") or "application/octet-stream")[:120]}]
                 if attachment_meta else None)
    db.add_message(conversation_id,"user",value.message,time.time(),source_message_id,blocks=user_blocks)
    conversation_project_state=_conversation_project_context(conversation_id, project_id, value.message)
    print(f"ACTIVE_PROJECT_ID={project_id} ACTIVE_WORKSPACE={conversation_project_state.get('workspace_path') or ''} "
          f"PROJECT_CONTEXT_LOADED=YES PROJECT_CONTEXT_REVISION={conversation_project_state.get('revision',0)} "
          f"ACTIVE_SUBJECT={conversation_project_state.get('current_subject','')} "
          f"ACTIVE_SUBJECT_SOURCE={(conversation_project_state.get('provenance') or {}).get('current_subject','')} "
          f"PROJECT_CONTEXT_FACT_COUNT={len([key for key, item in conversation_project_state.items() if item and key not in {'revision','provenance'}])} "
          f"ATTACHMENT_COUNT={1 if attachment_meta else 0}", file=__import__('sys').stderr, flush=True)
    print("GUI_CHAT_USER_SAVE=PASS GUI_CHAT_RUNTIME_START", file=__import__("sys").stderr, flush=True)
    planning_session, planning_response = _resolve_interactive_planning_reply(conversation_id, value.message)
    if planning_response is not None:
        _record_planning_decisions(conversation_id, project_id, planning_session)
        db.add_message(conversation_id, "assistant", planning_response, time.time(), str(uuid.uuid4()))
        print("CODING_TASK_CREATED=NO CODING_ROUTE=INTERACTIVE_PLANNING", file=__import__("sys").stderr, flush=True)
        return {"conversation_id": conversation_id, "response": planning_response, "sources": []}
    approved_message=bool(re.search(r"^(?:はい、?\s*)?(?:承認します|許可します|承認|この計画を承認します|この計画を実行してください|この計画で進めて|この計画でお願いします|実行してください|進めてください|進めて|この内容でok|お願いします、?実装して)\s*[。！!]*$",value.message.strip(),re.I))
    waiting=next((task for task in db.coding_tasks(conversation_id) if task["status"] == "WAITING_FOR_PLAN_APPROVAL"),None)
    if waiting and approved_message:
        print(f"MANAGED_INPUT_ROUTED=true CODING_TASK_ID={waiting['id']} CODING_ROUTE=PLAN_APPROVAL",file=__import__('sys').stderr,flush=True)
        db.enqueue_coding_task(waiting["id"]); _coding_scheduler_wake.set()
        response="Coding Task を実行キューへ追加しました。"
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),waiting["id"])
        return {"conversation_id":conversation_id,"coding_task_id":waiting["id"],"response":response,"sources":[]}
    if waiting:
        print(f"MANAGED_INPUT_ROUTED=true CODING_TASK_ID={waiting['id']} CODING_ROUTE=PLAN_APPROVAL_WAITING",file=__import__('sys').stderr,flush=True)
        response="計画の承認待ちです。計画を実行する場合は「この計画で進めて」と返信してください。"
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),waiting["id"])
        return {"conversation_id":conversation_id,"coding_task_id":waiting["id"],"response":response,"sources":[]}
    ineffective_waiting=next((task for task in db.coding_tasks(conversation_id)
                              if task["status"] == "WAITING_FOR_USER" and task.get("recovery_reason") == "REPLAN_INEFFECTIVE"),None)
    if ineffective_waiting:
        intent=classify_waiting_input(value.message)
        print(f"TASK_ID={ineffective_waiting['id']} WAITING_INPUT_INTENT={intent} USER_REVISION_PRESENT={'true' if intent == 'REVISION' else 'false'}",file=__import__('sys').stderr,flush=True)
        if intent == "REVISION":
            revision=value.message.strip()
            revision_context={**(ineffective_waiting.get("pending_authorization") or {}), "user_revision": revision, "state": "PENDING"}
            db.update_coding_task(ineffective_waiting["id"], status="QUEUED", activity="QWEN_REPLANNING",
                                  pending_authorization=revision_context, recovery_action="REPLAN_CONTINUATION",
                                  recovery_reason="USER_REVISION", pending_user_confirmation=0)
            db.enqueue_coding_task(ineffective_waiting["id"]); _coding_scheduler_wake.set()
            response="修正指示を受け取り、未完了部分の再計画を開始します。"
            db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),ineffective_waiting["id"])
            return {"conversation_id":conversation_id,"coding_task_id":ineffective_waiting["id"],"response":response,"sources":[]}
        pending=ineffective_waiting.get("pending_authorization") or {}
        unmet=pending.get("unmet_criteria") or []
        details="\n".join(f"- {item}" for item in unmet[:6])
        phase_id=str(ineffective_waiting.get("current_phase_id") or "")
        phase_label=f"Phase {phase_id[1:]}" if re.fullmatch(r"p\d+", phase_id, re.IGNORECASE) else (phase_id or "未完了のPhase")
        response=(f"{phase_label} の再計画では、失敗しているPhaseの構造を改善できなかったため確認が必要です。\n"
                  + ("現在の未達条件:\n" + details + "\n" if details else "")
                  + "内容を変更したい場合は、変更内容をそのまま入力してください。")
        print(f"TASK_ID={ineffective_waiting['id']} CLARIFICATION_NO_MUTATION=true QWEN_REPLANNING_STARTED=false QUEUE_ENTRY_CREATED=false PLAN_REVISION_CHANGED=false CURRENT_PHASE_CHANGED=false STATUS_AFTER=WAITING_FOR_USER",file=__import__('sys').stderr,flush=True)
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),ineffective_waiting["id"])
        return {"conversation_id":conversation_id,"coding_task_id":ineffective_waiting["id"],"response":response,"sources":[]}
    scope_waiting=next((task for task in db.coding_tasks(conversation_id) if task["status"] == "WAITING_FOR_USER" and task.get("pending_authorization")),None)
    if scope_waiting and approved_message:
        db.accept_pending_plan(scope_waiting["id"])
        db.enqueue_coding_task(scope_waiting["id"]); _coding_scheduler_wake.set()
        response="Coding Task の追加スコープを承認し、実行キューへ追加しました。"
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),scope_waiting["id"])
        return {"conversation_id":conversation_id,"coding_task_id":scope_waiting["id"],"response":response,"sources":[]}
    if scope_waiting:
        intent=classify_waiting_input(value.message)
        print(f"TASK_ID={scope_waiting['id']} WAITING_INPUT_INTENT={intent} USER_REVISION_PRESENT=false QWEN_REPLANNING_STARTED=false QUEUE_ENTRY_CREATED=false STATUS_AFTER=WAITING_FOR_USER",file=__import__('sys').stderr,flush=True)
        response="追加スコープの承認待ちです。内容を確認し、「承認します」と返信してください。"
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),scope_waiting["id"])
        return {"conversation_id":conversation_id,"coding_task_id":scope_waiting["id"],"response":response,"sources":[]}
    waiting_task=next((task for task in db.coding_tasks(conversation_id) if task["status"] == "WAITING_FOR_USER"),None)
    if waiting_task:
        intent=classify_waiting_input(value.message)
        print(f"TASK_ID={waiting_task['id']} WAITING_INPUT_INTENT={intent} USER_REVISION_PRESENT=false QWEN_REPLANNING_STARTED=false QUEUE_ENTRY_CREATED=false STATUS_AFTER=WAITING_FOR_USER",file=__import__('sys').stderr,flush=True)
        response="確認が必要です。現在のTaskはユーザー入力を待っています。変更内容を具体的に入力してください。"
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),waiting_task["id"])
        return {"conversation_id":conversation_id,"coding_task_id":waiting_task["id"],"response":response,"sources":[]}
    # Task-control/status requests stay under the managed task authority even
    # after a terminal BLOCKED decision; never let normal Brain prose imply
    # that the task resumed.
    managed_tasks=db.coding_tasks(conversation_id)
    resume_request=bool(re.search(r"(?:再開(?:して)?|続行(?:して)?|続けて|次へ|実行を続けて|このタスクを続けて|continue|resume|この計画を実行してください|実行してください)",value.message,re.I))
    active_candidates=[task for task in managed_tasks if task["status"] in {"QUEUED","RUNNING","PLANNING","FINAL_REPORTING"}]
    resumable_candidates=[task for task in managed_tasks if resumable_continuation_eligible(task)]
    active_task=active_candidates[0] if len(active_candidates) == 1 else None
    resumable_task=resumable_candidates[0] if len(resumable_candidates) == 1 else None
    protected_task=next((task for task in managed_tasks if task["status"] == "RESUMABLE" and task.get("pending_authorization")),None)
    if resume_request and protected_task:
        response="保護された操作の承認待ちです。「続行」だけでは承認されません。"
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),protected_task["id"])
        return {"conversation_id":conversation_id,"coding_task_id":protected_task["id"],"response":response,"sources":[]}
    if resume_request and (len(active_candidates) > 1 or len(resumable_candidates) > 1):
        candidates=active_candidates or resumable_candidates
        response="複数の回復可能な Coding Task があるため、対象を選択してください。"
        print(f"MANAGED_INPUT_ROUTED=true CODING_ROUTE=TASK_SELECTION_REQUIRED CANDIDATE_TASK_IDS={','.join(task['id'] for task in candidates)} NORMAL_RUNTIME_FALLBACK=false",file=__import__('sys').stderr,flush=True)
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),candidates[0]["id"])
        return {"conversation_id":conversation_id,"coding_task_id":candidates[0]["id"],"response":response,"sources":[]}
    if resume_request and active_task:
        response="このCoding Taskはすでに実行中です。"
        print(f"MANAGED_INPUT_ROUTED=true CODING_TASK_ID={active_task['id']} CODING_ROUTE=ALREADY_RUNNING NORMAL_RUNTIME_FALLBACK=false",file=__import__('sys').stderr,flush=True)
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),active_task["id"])
        return {"conversation_id":conversation_id,"coding_task_id":active_task["id"],"response":response,"sources":[]}
    if resume_request and resumable_task:
        replan_epoch_resume = _begin_human_recovery_epoch(resumable_task)
        queued=db.enqueue_coding_task(resumable_task["id"]); _coding_scheduler_wake.set()
        print(f"TASK_ID={resumable_task['id']} RESUME_REQUESTED=true RESUME_ACCEPTED=true STATUS_BEFORE_RESUME=RESUMABLE STATUS_AFTER_RESUME=QUEUED RECOVERY_ACTION={resumable_task.get('recovery_action','NONE')} RECOVERY_REASON={resumable_task.get('recovery_reason','NONE')} RECOVERY_EPOCH={queued.get('recovery_epoch',0) if queued else resumable_task.get('recovery_epoch',0)} REPLAN_COUNT_FOR_NEW_EPOCH={queued.get('replan_count_in_epoch','unchanged') if queued else 'unchanged'} NEXT_TASK_ACTION=QUEUE_FIFO",file=__import__('sys').stderr,flush=True)
        response="Coding Task を再開し、実行キューへ追加しました。" + (" 新しい回復 epoch を開始しました。" if replan_epoch_resume else "")
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),resumable_task["id"])
        return {"conversation_id":conversation_id,"coding_task_id":resumable_task["id"],"response":response,"sources":[]}
    blocked=next((task for task in managed_tasks if task["status"]=="BLOCKED"),None)
    control_message=bool(re.search(r"(?:次に進んで|続けて|このtaskを進めて|どうなってる|状態(?:確認)?|今どこ|進捗)",value.message,re.I))
    if blocked and control_message:
        reason=(blocked.get("pending_authorization") or {}).get("reason") or "計画または実行結果を検証できませんでした"
        response=f"このCoding Taskは現在ブロックされています。{reason}"
        print(f"MANAGED_INPUT_ROUTED=true CODING_TASK_ID={blocked['id']} CODING_ROUTE=BLOCKED_TASK_CONTROL NORMAL_RUNTIME_FALLBACK=false",file=__import__('sys').stderr,flush=True)
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),blocked["id"])
        return {"conversation_id":conversation_id,"coding_task_id":blocked["id"],"response":response,"sources":[]}
    # The persisted toggle controls orchestration only.  When disabled, an
    # otherwise coding-shaped request continues through the normal Brain path.
    classification = coding_classification_diagnostics(
        value.message,
        project_scoped=bool((db.project(project_id) or {}).get("workspace_path")),
        attachment_present=bool(attachment_meta),
    )
    activation_class = str(classification["classification"])
    print(f"CODING_ORCHESTRATOR_CLASSIFICATION={activation_class} "
          f"CODING_CLASSIFICATION={activation_class} "
          f"MUTATION_INTENT={'YES' if classification['mutation_intent'] else 'NO'} "
          f"PLANNING_INTENT={'YES' if classification['planning_intent'] else 'NO'} "
          f"EXPLICIT_NON_CODING={'YES' if classification['explicit_non_coding'] else 'NO'} "
          f"CLASSIFICATION_REASON={classification['reason']}",
          file=__import__('sys').stderr, flush=True)
    candidate=(activation_class == "CODING") and settings.task_manager_enabled
    print(f"CODING_CANDIDATE={'true' if candidate else 'false'}",file=__import__('sys').stderr,flush=True)
    if candidate:
        requested_task_id=new_id(); now=time.time()
        task=db.create_coding_task(requested_task_id,conversation_id,value.message,"QUEUED","QWEN_PLANNING",None,now,source_message_id=source_message_id)
        task_id=task["id"]
        if task_id != requested_task_id:
            response="同じ送信済みメッセージの Coding Task を参照しています。"
            print(f"CODING_TASK_IDEMPOTENT_HIT=true CODING_TASK_ID={task_id} NORMAL_RUNTIME_FALLBACK=false",file=__import__('sys').stderr,flush=True)
            return {"conversation_id":conversation_id,"coding_task_id":task_id,"response":response,"sources":[]}
        # The control plane receives the current user turn as a structured
        # input.  Project context/history is assembled separately for model
        # grounding and is never concatenated into requirement extraction.
        requirements=normalize_coding_requirements({
            "current_user_text": value.message,
            "project_metadata": {"project_id": project_id},
            "selected_workspace": (db.project(project_id) or {}).get("workspace_path"),
            "conversation_id": conversation_id,
        })
        db.update_coding_task(task_id,requirements=requirements,execution_mode=requirements["execution_mode"],task_profile=requirements["task_profile"],required_mcp=requirements["required_mcp"])
        norm_diag=requirements.get("normalization_diagnostics") or {}
        valid=bool(norm_diag.get("canonical_requirements_valid"))
        print(f"TASK_ID={task_id} TASK_CREATED=true REQUIREMENTS_NORMALIZED=PASS CANONICAL_REQUIREMENTS_VALID={'PASS' if valid else 'FAIL'} CURRENT_USER_TEXT_HASH={norm_diag.get('current_user_text_hash','')} NORMALIZER_CONTROL_INPUT_HASH={norm_diag.get('normalizer_control_input_hash','')} MODEL_CONTEXT_HASH={norm_diag.get('model_context_hash','')} NORMALIZER_INPUT_CHAR_COUNT={norm_diag.get('normalizer_input_char_count',0)} CURRENT_USER_TEXT_CHAR_COUNT={norm_diag.get('current_user_text_char_count',0)} PROJECT_CONTEXT_ADDED_BEFORE_NORMALIZATION={'YES' if norm_diag.get('project_context_added_before_normalization') else 'NO'} CONVERSATION_HISTORY_ADDED_BEFORE_NORMALIZATION={'YES' if norm_diag.get('conversation_history_added_before_normalization') else 'NO'} ASSISTANT_HISTORY_ADDED_BEFORE_NORMALIZATION={'YES' if norm_diag.get('assistant_history_added_before_normalization') else 'NO'} RAW_REQUEST_HASH={norm_diag.get('raw_request_hash','')} SCOPED_INPUT_HASH={norm_diag.get('scoped_input_hash','')} CANONICAL_REQUIREMENTS_HASH={norm_diag.get('canonical_requirements_hash','')} CODING_MUTATION_MODE={requirements['mutation_mode']} FIX_REASON={requirements['fix_reason']} FIX_SCOPE_EXPANDED=NO FIX_ESCALATED_TO_IMPLEMENTATION=NO REQUIRED_CAPABILITIES={json.dumps(requirements['required_capabilities'])} FORBIDDEN_CAPABILITIES={json.dumps(requirements['forbidden_capabilities'])} REQUIRED_MCPS={json.dumps(requirements['required_mcps'])} CAPABILITY_PROVENANCE={json.dumps(norm_diag.get('capability_provenance', []), ensure_ascii=False, sort_keys=True)}",file=__import__('sys').stderr,flush=True)
        if not valid:
            db.update_coding_task(task_id, status="BLOCKED", activity="NONE", recovery_action="NONE", recovery_reason="INVALID_REQUIREMENTS")
            print(f"TASK_ID={task_id} CONTROL_PLANE_INVARIANT_FAILURE=CANONICAL_REQUIREMENTS_CONFLICT CODING_TASK_STATUS=BLOCKED QUEUED=NO", file=__import__('sys').stderr, flush=True)
            response="Coding Task の実行要件を確定できませんでした。要求の必須条件と除外条件が矛盾しています。"
            db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task_id)
            return {"conversation_id":conversation_id,"coding_task_id":task_id,"response":response,"sources":[]}
        db.enqueue_coding_task(task_id); db.update_coding_task(task_id,activity="QWEN_PLANNING"); _coding_scheduler_wake.set()
        print(f"CODING_ROUTE=CODING_MANAGED CODING_MANAGED=true CODING_TASK_ID={task_id} NORMAL_RUNTIME_FALLBACK=false CODING_TASK_STATUS=QUEUED",file=__import__('sys').stderr,flush=True)
        response="Coding Task を登録しました。計画を作成中です。"
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task_id)
        return {"conversation_id":conversation_id,"coding_task_id":task_id,"response":response,"sources":[]}
    if activation_class == "AMBIGUOUS" and settings.task_manager_enabled:
        response="実装を行うか、計画・設計のみを行うかが明確ではありません。どちらを希望するか指定してください。"
        print("CODING_TASK_CREATED=NO CODING_ROUTE=AMBIGUOUS_SCOPE NORMAL_RUNTIME_FALLBACK=false", file=__import__("sys").stderr, flush=True)
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()))
        return {"conversation_id":conversation_id,"response":response,"sources":[]}
    if activation_class == "NON_CODING":
        print("CODING_TASK_CREATED=NO CODING_ROUTE=NORMAL_BRAIN_PLANNING", file=__import__("sys").stderr, flush=True)
    # Defense in depth: an actionable coding request that did not enter the
    # managed route must never fall through to the normal Brain or vision path.
    # Model prose is not execution authority, so return a bounded truthful
    # response before any normal runtime dispatch can occur.
    action_intent = coding_action_intent(value.message)
    if action_intent and settings.task_manager_enabled:
        manager_requested = bool(re.search(r"coding task(?: manager)?|コーディングタスク|管理タスク|実GUI E2E", value.message, re.I))
        response = ("Coding Taskとして実装を開始できませんでした。\n"
                    "この応答ではファイル変更や実装は行っていません。")
        if manager_requested:
            response += "\nCoding Task Managerへのルーティングに失敗したため、通常回答で実装内容を生成することはしませんでした。"
        print(f"CODING_ACTION_INTENT=true CODING_CANDIDATE=false MANAGED_ROUTE_SELECTED=false "
              f"AUTHORIZED_EXECUTION_OCCURRED=false CODING_OUTPUT_GUARD_ACTIVE=true "
              f"NORMAL_IMPLEMENTATION_OUTPUT_ALLOWED=false EXECUTION_CLAIM_ALLOWED=false RESPONSE_REPLACED=true",
              file=__import__("sys").stderr, flush=True)
        db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()))
        return {"conversation_id":conversation_id,"response":response,"sources":[]}
    # Explicit Web Search owns the request before the structured-tool Router.
    # This prevents a Router decision or heuristic from sending a fresh
    # current-data request down the normal-chat path.
    requested_wikipedia = bool(re.search(r"(?:wikipedia|wiki|ウィキペディア)", value.message, re.I) and not re.search(r"wikidata", value.message, re.I))
    explicit_web_request = bool(re.search(r"(?:web検索|webで|ネットで|インターネットで|search the web|search web)", value.message, re.I)) and not requested_wikipedia
    print(f"EXPLICIT_SEARCH_DETECTED={'YES' if explicit_web_request or requested_wikipedia else 'NO'} REQUESTED_SOURCE={'WIKIPEDIA' if requested_wikipedia else 'GENERIC_WEB' if explicit_web_request else 'NONE'}", file=__import__('sys').stderr, flush=True)
    structured_data_request = bool(re.search(r"(?:構造化データ|structured data|構造化された|データを使って|確認してください|比較|ランキング|人口\s*\d+万人以上|(?:首都|人口|通貨|主要言語).*(?:まとめ|教えて|調べ))", value.message, re.I))
    route_source = "none"
    try:
        # The deterministic matcher is the single authoritative first pass for
        # strongly recognizable capabilities.  The optional model router may
        # fill gaps, but it must not override a known route (for example a
        # translation sentence that contains the word "weather").
        tool_request = None if explicit_web_request else route_external_tool(value.message)
        if tool_request is not None:
            route_source = "deterministic"
        if tool_request is None and not explicit_web_request:
            tool_request = router_decision(value.message)
            if tool_request is not None:
                route_source = "model"
                # A model suggestion must not turn an otherwise unclassified
                # prompt into a location/geocoding call merely because it
                # contains a country or city token.  Specialized geographic
                # requests are already handled by the deterministic matcher;
                # reject only the unsafe broad fallback here.
                if tool_request[0] in {"weather.open_meteo", "geo.poi_search", "geo.routing", "marine.tides_currents", "astronomy.sun_times"}:
                    if not re.search(r"(?:天気|weather|気温|予報|雨|晴れ|PM\s*2\.?5|大気|半径|病院|薬局|学校|ルート|経路|距離|から.+まで|潮汐|潮位|満潮|干潮|tides?|currents?|日の出|日の入り|sunrise|sunset)", value.message, re.I):
                        print(f"TOOL_MODEL_DECISION_REJECTED=BROAD_GEO_FALLBACK TOOL_MODEL_SELECTED={tool_request[0]}", file=__import__('sys').stderr, flush=True)
                        tool_request = None
                        route_source = "none"
    except RouterUnavailable:
        # Deterministic provider routes remain usable when the optional router
        # model is unavailable; the compiler still validates every field.
        tool_request = route_external_tool(value.message)
        route_source = "deterministic_fallback"
        print(f"ROUTER_MODEL_FALLBACK=DETERMINISTIC TOOL_SELECTED={tool_request[0] if tool_request else 'NONE'}", file=__import__('sys').stderr, flush=True)
    if tool_request:
        tool_id, arguments = tool_request
        print(f"TOOL_ROUTE_DECISION=tool TOOL_ROUTE_SOURCE={route_source} TOOL_CAPABILITY={tool_id} TOOL_PROVIDER={REGISTRY[tool_id].provider} TOOL_ROUTER_SELECTED={tool_id}", file=__import__('sys').stderr, flush=True)
        try:
            arguments = compile_provider_arguments(tool_id, value.message, arguments)
        except ExternalToolError as exc:
            code = str(exc)
            print(f"TOOL_ARGUMENTS_VALID=NO TOOL_ARGUMENT_ERROR={code}", file=__import__('sys').stderr, flush=True)
            task = Task(value.message)
            task.transition(TaskState.ROUTING); task.error = code; task.transition(TaskState.FAILED)
            db.save_task(task, conversation_id)
            response = _external_tool_user_message(code, arguments)
            db.add_message(conversation_id, "assistant", response, time.time(), str(uuid.uuid4()), task.id)
            return {"conversation_id": conversation_id, "task": task.__dict__ | {"route": None, "state": task.state.value}, "response": response, "sources": []}
        if tool_id == "weather.open_meteo":
            print(f"TOOL_INVOCATION_LOCATION={arguments.get('location','')} TOOL_INVOCATION_DATE={arguments.get('date','NONE')}", file=__import__('sys').stderr, flush=True)
            arguments = normalize_weather_arguments(arguments)
            print(f"LOCATION_NORMALIZATION_INPUT={tool_request[1].get('location','')} LOCATION_NORMALIZATION_OUTPUT={arguments.get('location','')}", file=__import__('sys').stderr, flush=True)
        elif tool_id == "research.openalex" and not arguments.get("doi"):
            arguments = normalize_research_arguments(arguments)
        elif tool_id == "knowledge.wikimedia":
            arguments = normalize_wiki_arguments(arguments)
            print(f"WIKIPEDIA_PROVIDER_SELECTED=YES NORMALIZED_SEARCH_QUERY={arguments.get('query','')} LOCAL_RETRIEVAL_ATTEMPTED=NO", file=__import__('sys').stderr, flush=True)
        # Local tools (for example SymPy) do not require network authorization;
        # only providers whose registry entry is external are gated here.
        if REGISTRY[tool_id].external and not settings.external_access_enabled:
            task = Task(value.message)
            task.error = "EXTERNAL_ACCESS_REQUIRED"; task.transition(TaskState.ROUTING); task.transition(TaskState.FAILED)
            db.save_task(task, conversation_id)
            response = "EXTERNAL_ACCESS_REQUIRED: External Tools are disabled. Enable them globally in Settings or with /external on."
            db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task.id)
            return {"conversation_id":conversation_id,"task":task.__dict__ | {"route":None,"state":task.state.value},"response":response,"sources":[]}
        try:
            print(f"PROVIDER_REQUEST_STARTED={tool_id}", file=__import__('sys').stderr, flush=True)
            if tool_id == "knowledge.wikimedia": print("WIKIPEDIA_API_CALL_STARTED=YES", file=__import__('sys').stderr, flush=True)
            result = execute_external_tool(tool_id, arguments)
            data = result.get("data") if isinstance(result, dict) else {}
            print(f"PROVIDER_REQUEST_STATUS=SUCCESS PROVIDER_RESULT_KIND={(data or {}).get('result_kind','unknown')} PROVIDER_HAS_PAYLOAD={(data or {}).get('has_payload','unknown')} PROVIDER_ITEM_COUNT={(data or {}).get('item_count',len((data or {}).get('items') or []))} PROVIDER_EVIDENCE_FIELD_COUNT={(data or {}).get('evidence_field_count','unknown')} PROVIDER_SEMANTIC_STATUS={(data or {}).get('semantic_status','unknown')} PROVIDER_REQUEST_COMPLETE={(data or {}).get('request_complete','unknown')} PROVIDER_PROVENANCE_PRESENT={'YES' if result.get('sources') else 'NO'}", file=__import__('sys').stderr, flush=True)
            if tool_id == "knowledge.wikimedia": print(f"WIKIPEDIA_API_CALL_FINISHED=YES WIKIPEDIA_RESULT_COUNT={1 if (data or {}).get('title') else 0}", file=__import__('sys').stderr, flush=True)
            if tool_id == "government.us_federal_register":
                print(f"FEDERAL_HTTP_STATUS=200 FEDERAL_RAW_DOCUMENT_COUNT={(data or {}).get('raw_document_count',(data or {}).get('item_count',len((data or {}).get('items') or [])))} FEDERAL_NORMALIZED_DOCUMENT_COUNT={(data or {}).get('item_count',len((data or {}).get('items') or []))} FEDERAL_SEMANTIC_STATUS_BEFORE_GENERIC={(data or {}).get('semantic_status','unknown')} FEDERAL_SEMANTIC_STATUS_AFTER_GENERIC={(data or {}).get('semantic_status','unknown')}", file=__import__('sys').stderr, flush=True)
        except ExternalToolError as exc:
            code = str(exc)
            print(f"PROVIDER_REQUEST_STATUS=FAILURE PROVIDER_ERROR={code} PROVIDER_PROVENANCE_PRESENT=NO FALLBACK_USED=NO", file=__import__('sys').stderr, flush=True)
            if tool_id == "knowledge.wikimedia": print("WIKIPEDIA_API_CALL_FINISHED=NO WIKIPEDIA_RESULT_COUNT=0", file=__import__('sys').stderr, flush=True)
            if tool_id == "earth.natural_event":
                print("CURRENT_DATA_TERMINAL=FAILURE BRAIN_FALLBACK_BLOCKED=YES", file=__import__('sys').stderr, flush=True)
            if tool_id == "earth.natural_event":
                response = "EONETから現在の自然災害データを取得できませんでした。しばらくしてから再試行してください。"
            elif tool_id == "government.us_federal_register":
                response = "Federal Registerからデータを取得できませんでした。しばらくしてから再試行してください。"
            else:
                response = _external_tool_user_message(code, arguments)
            # Provider failures are still a completed conversational answer. Keep
            # the machine-readable code on the task, but send the natural-language
            # response through the same assistant-message path as successful turns.
            task = Task(value.message)
            task.transition(TaskState.ROUTING)
            task.error = code
            task.transition(TaskState.FAILED)
            db.save_task(task, conversation_id)
            db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task.id)
            if tool_id in {"earth.natural_event", "government.us_federal_register"}:
                print(f"FINAL_RESPONSE_OWNER=DETERMINISTIC_RENDERER TERMINAL_RESPONSE_READY=true DETERMINISTIC_RENDERER={tool_id} BRAIN_COMPOSER_USED=NO GENERIC_COMPOSER_USED=NO", file=__import__('sys').stderr, flush=True)
            return {"conversation_id":conversation_id,"task":task.__dict__ | {"route":None,"state":task.state.value},"response":response,"sources":[]}
        # HTTP success with no semantically usable payload is a terminal
        # provider outcome.  Do not hand an empty current-data result to Brain,
        # where it could be replaced by model-memory facts.
        if isinstance(result.get("data"), dict) and (
            result["data"].get("semantic_status") == "EMPTY"
            or (tool_id == "earth.natural_event" and result["data"].get("current_turn_evidence") is not True)
        ):
            task = Task(value.message); task.transition(TaskState.ROUTING); task.error = "PROVIDER_EMPTY"; task.transition(TaskState.FAILED)
            if tool_id == "earth.natural_event":
                response = "現在進行中として確認できる自然災害は、今回のEONET検索では見つかりませんでした。"
            elif tool_id == "government.us_federal_register":
                response = "今回のFederal Register検索では、人工知能に関係する最近のruleまたはnoticeは見つかりませんでした。"
            else:
                response = "外部データ源から一致する結果を取得できませんでした。検索条件を確認して再試行してください。"
            db.save_task(task, conversation_id); db.add_message(conversation_id, "assistant", response, time.time(), str(uuid.uuid4()), task.id)
            print(f"PROVIDER_SEMANTIC_STATUS=EMPTY BRAIN_FALLBACK_BLOCKED=YES CURRENT_DATA_TERMINAL=EMPTY TOOL_ID={tool_id}", file=__import__('sys').stderr, flush=True)
            print(f"FINAL_RESPONSE_OWNER=DETERMINISTIC_RENDERER TERMINAL_RESPONSE_READY=true DETERMINISTIC_RENDERER={tool_id} BRAIN_COMPOSER_USED=NO GENERIC_COMPOSER_USED=NO", file=__import__('sys').stderr, flush=True)
            return {"conversation_id":conversation_id,"task":task.__dict__ | {"route":None,"state":task.state.value},"response":response,"sources":[]}
        # Compose the provider result directly; generic runtime retrieval must not
        # interpret words such as "research" as an OLCR internal search request.
        task, response = compose_external_result(value.message, result)
        if tool_id == "knowledge.wikimedia": print("FINAL_RESPONSE_SOURCE=WIKIPEDIA RAW_DIAGNOSTICS_EXPOSED=NO", file=__import__('sys').stderr, flush=True)
        print("FALLBACK_USED=NO FALLBACK_TYPE=PROVIDER_RESULT", file=__import__('sys').stderr, flush=True)
        blocks = external_tool_display_blocks(result)
        db.save_task(task,conversation_id); db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task.id,blocks=blocks)
        return {"conversation_id":conversation_id,"task":task.__dict__ | {"route": task.route.value if task.route else None, "state": task.state.value},"response":response,"sources":result["sources"],"tool":result,"blocks":blocks}
    if structured_data_request:
        # A structured/current-data request without a successful provider route
        # must not fall through to the Brain, which could fabricate provenance.
        print("PROVIDER_REQUEST_STATUS=NOT_SELECTED PROVIDER_PROVENANCE_PRESENT=NO FALLBACK_USED=NO FALLBACK_TYPE=NONE", file=__import__('sys').stderr, flush=True)
        task = Task(value.message); task.transition(TaskState.ROUTING); task.error = "STRUCTURED_PROVIDER_UNAVAILABLE"; task.transition(TaskState.FAILED)
        db.save_task(task, conversation_id)
        response = "構造化データ源から取得できませんでした。検索条件を具体化して再試行してください。"
        db.add_message(conversation_id, "assistant", response, time.time(), str(uuid.uuid4()), task.id)
        return {"conversation_id": conversation_id, "task": task.__dict__ | {"route": None, "state": task.state.value}, "response": response, "sources": []}
    external_context=""
    if value.attachment and isinstance(value.attachment, dict):
        name=str(value.attachment.get("name") or "attachment")[:200]
        content=str(value.attachment.get("content") or "")[:50_000]
        if content: external_context=f"[ATTACHED_FILE name={name}]\n{content}\n[/ATTACHED_FILE]"
    if value.external and value.external.get("read_only") is True:
        root=Path(value.external.get("canonical_path", "")).resolve()
        names=re.findall(r"(?<![/\w])([\w.-]+(?:/[\w.-]+)*)", value.message)
        candidates=[root] if root.is_file() else [(root/n).resolve() for n in names] if root.is_dir() else []
        safe=[f"source: {p.relative_to(root) if p != root else p.name}\ncontent:\n{p.read_text(encoding='utf-8')[:50000]}" for p in candidates if p.is_file() and not p.is_symlink() and (p==root or root in p.parents)]
        if safe: external_context="[EXTERNAL_CONTEXT]\n"+"\n\n".join(safe)
    # Current Web requests are authoritative external-data requests.  Memory is
    # still available for ordinary prompts, but it must never become a fallback
    # source when a fresh search fails or returns no usable articles.
    current_external_data_required = bool(re.search(r"(?:web検索|webで|ネットで|インターネットで|search the web|search web)", value.message, re.I) and re.search(r"(?:今日|本日|最新|最近|リアルタイム|ニュース|today|latest|current|recent|news)", value.message, re.I))
    memory_context = ""
    try:
        memories = conversation_memory.search(value.message, conversation_id, project_id) if settings.conversation_memory_enabled and not current_external_data_required else []
        print(f"MEMORY_USED_AS_CURRENT_WEB_RESULT={'NO' if current_external_data_required else 'NOT_APPLICABLE'}", file=__import__('sys').stderr, flush=True)
        if memories:
            memory_context = "\n[CONVERSATION_MEMORY]\n" + "\n\n".join(x["text"][:3000] for x in memories)
    except Exception:
        memory_context = ""
    constraint=conversation_memory_constraint(settings.conversation_memory_enabled)
    # A fresh/current Web request is isolated from both project context and the
    # active transcript.  Terminal Web outcomes must be authoritative and must
    # never allow prior news to reach a fallback Brain call.
    conversation_context = "" if current_external_data_required else active_conversation_context(conversation_id)
    project_core = "" if current_external_data_required else (value.core_context if value.core_context is not None else project_context(project_id))
    active_planning = db.active_interactive_planning(conversation_id)
    planning_context = "" if current_external_data_required else _planning_context_prompt(active_planning)
    persisted_project_context = "" if current_external_data_required else _project_context_prompt(_conversation_project_context(conversation_id, project_id))
    coding_context = "" if current_external_data_required else _coding_task_context(conversation_id)
    print(f"PLANNING_CONTEXT_LOADED={'YES' if planning_context else 'NO'} CODING_TASK_CONTEXT_LOADED={'YES' if coding_context else 'NO'} "
          f"CONTEXT_SOURCE_COUNT={sum(bool(item) for item in (persisted_project_context, project_core, planning_context, coding_context, conversation_context, external_context, memory_context))}",
          file=__import__('sys').stderr, flush=True)
    combined=persisted_project_context+project_core+planning_context+coding_context+conversation_context+("\n"+external_context if external_context else "")+memory_context+("\n"+constraint if constraint else "")
    if current_external_data_required:
        print("CURRENT_EXTERNAL_DATA_CONTEXT_ISOLATED=YES ACTIVE_TRANSCRIPT_LOADED_AFTER_TERMINAL=NO", file=__import__('sys').stderr, flush=True)
    workspace_root = (db.project(project_id) or {}).get("workspace_path")
    task, response = (runtime.execute_image(value.message, value.image, combined)
                      if value.image else runtime.execute(value.message, value.approved, combined, workspace_root=workspace_root))
    db.save_task(task,conversation_id)
    _persist_interactive_planning_questions(conversation_id, response)
    db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task.id)
    print("GUI_CHAT_ASSISTANT_SAVE=PASS GUI_CHAT_RESPONSE_CREATED", file=__import__("sys").stderr, flush=True)
    try:
        conversation_memory.index_completed_turn(conversation_id)
        conversation_memory.backfill(8)
    except Exception: pass
    return {"conversation_id":conversation_id,"task": task.__dict__ | {"route": task.route.value if task.route else None, "state": task.state.value}, "response": response}


@app.post("/api/chat/stream")
def stream_chat(value: ChatInput):
    project_id=value.project_id or db.default_project_id()
    conversation_id=value.conversation_id or db.create_conversation(value.message,time.time(),str(uuid.uuid4()),project_id)
    existing=db.conversation(conversation_id)
    if not existing: raise HTTPException(404,"conversation not found")
    if existing["project_id"] != project_id: raise HTTPException(403,"conversation does not belong to project")
    db.add_message(conversation_id,"user",value.message,time.time(),str(uuid.uuid4()))
    _conversation_project_context(conversation_id, project_id, value.message)
    # The streaming endpoint used to check local retrieval before source-
    # specific providers, which made "search on wikipedia ..." serialize a
    # retrieval envelope as assistant prose. Keep this terminal provider path
    # aligned with /api/chat.
    wiki_request = route_external_tool(value.message)
    if wiki_request and wiki_request[0] == "knowledge.wikimedia":
        _, wiki_arguments = wiki_request
        wiki_arguments = normalize_wiki_arguments(wiki_arguments)
        print(f"EXPLICIT_SEARCH_DETECTED=YES SEARCH_ACTION=SEARCH REQUESTED_SOURCE=WIKIPEDIA NORMALIZED_SEARCH_QUERY={wiki_arguments.get('query','')} SELECTED_SEARCH_PROVIDER=knowledge.wikimedia WIKIPEDIA_PROVIDER_SELECTED=YES LOCAL_RETRIEVAL_ATTEMPTED=NO", file=__import__('sys').stderr, flush=True)
        task = Task(value.message); task.transition(TaskState.ROUTING)
        if not settings.external_access_enabled:
            task.error="EXTERNAL_ACCESS_REQUIRED"; task.transition(TaskState.FAILED); db.save_task(task, conversation_id)
            response=_external_tool_user_message("EXTERNAL_ACCESS_REQUIRED", wiki_arguments)
        else:
            try:
                print("WIKIPEDIA_API_CALL_STARTED=YES", file=__import__('sys').stderr, flush=True)
                result=execute_external_tool("knowledge.wikimedia", wiki_arguments)
                task,response=compose_external_result(value.message,result)
                print(f"WIKIPEDIA_API_CALL_FINISHED=YES WIKIPEDIA_RESULT_COUNT={1 if (result.get('data') or {}).get('title') else 0} FINAL_RESPONSE_SOURCE=WIKIPEDIA RAW_DIAGNOSTICS_EXPOSED=NO", file=__import__('sys').stderr, flush=True)
            except ExternalToolError as exc:
                task.error=str(exc); task.transition(TaskState.FAILED)
                response=_external_tool_user_message(str(exc), wiki_arguments)
                print(f"WIKIPEDIA_API_CALL_FINISHED=NO WIKIPEDIA_RESULT_COUNT=0 FINAL_RESPONSE_SOURCE=WIKIPEDIA_ERROR RAW_DIAGNOSTICS_EXPOSED=NO", file=__import__('sys').stderr, flush=True)
            db.save_task(task,conversation_id)
        def wikipedia_events():
            yield "data: "+json.dumps({"type":"meta","task_id":task.id,"conversation_id":conversation_id})+"\n\n"
            yield "data: "+json.dumps({"type":"chunk","text":response})+"\n\n"
            db.add_message(conversation_id,"assistant",response,time.time(),str(uuid.uuid4()),task.id)
            yield "data: "+json.dumps({"type":"done","task":serialize_task(task)})+"\n\n"
        return StreamingResponse(wikipedia_events(),media_type="text/event-stream")
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
        grounding=_project_context_prompt(_conversation_project_context(conversation_id, project_id))
        grounding+=_planning_context_prompt(db.active_interactive_planning(conversation_id))
        grounding+=_coding_task_context(conversation_id)
        grounding+=active_conversation_context(conversation_id)
        messages=[{"role":"system","content":"Be concise. Do not claim unobserved actions."+grounding},{"role":"user","content":value.message}]
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
    db.delete_memory_for_conversation(conversation_id)
    return {"deleted":conversation_id}

@app.post("/api/conversation-memory/maintain")
def maintain_conversation_memory(limit: int = Query(default=8, ge=1, le=8)):
    try:
        return {"indexed": conversation_memory.backfill(limit), "limit": limit, "index_version": conversation_memory.INDEX_VERSION}
    except Exception as exc:
        return {"indexed": 0, "limit": limit, "status": "degraded", "error": type(exc).__name__}


@app.get("/api/settings")
def get_settings(): return settings.public_dict()

@app.post("/api/settings/reload")
def reload_settings():
    """Refresh an existing OLCR backend from its authoritative settings DB."""
    rebuild(environment_settings.with_overrides(db.load_application_settings()))
    return settings.public_dict()

@app.get("/api/semantic/status")
def semantic_status(): return retrieval.semantic_telemetry

@app.post("/api/web/fetch")
def web_fetch(value: WebInput):
    try: return fetch(value.url)
    except Exception as exc: raise HTTPException(400,str(exc)) from exc

@app.post("/api/web/search")
def web_search(value: SearchInput):
    try:
        provider=settings.web_provider
        if provider == "none": raise HTTPException(409, "WEB_PROVIDER_DISABLED")
        fn=brave_search if provider == "brave" else tavily_search if provider == "tavily" else search
        return {"provider":provider,"results": fn(value.query, min(value.limit, 5))}
    except HTTPException: raise
    except RuntimeError as exc: raise HTTPException(503,str(exc)) from exc
    except Exception as exc: raise HTTPException(502,str(exc)) from exc

@app.put("/api/settings")
def put_settings(value:SettingsInput):
    try: candidate=settings.with_overrides(value.model_dump())
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    for key,item in value.model_dump().items(): db.save_setting(key,item,time.time())
    rebuild(candidate)
    return settings.public_dict()
