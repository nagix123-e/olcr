from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .auth import AuthorizationPolicy
from .config import Settings
from .db import Database
from .models import Risk, Route, Task, TaskState
from .ollama import ModelFailure, ModelProvider
from .procedures import LOWERCASE_PROCEDURE, ProcedureRunner
from .retrieval import RetrievalRouter
from .tools import ToolValidationError, registry


class ContextManager:
    def __init__(self, budget: int): self.budget = budget
    def build(self, request: str, evidence: list[Any], core_context: str = "") -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        core_context=core_context[:max(0,self.budget//4)]
        remaining = max(0, self.budget - len(request) - len(core_context))
        selected, used = [], 0
        for item in evidence:
            text = item.snippet[:1000]
            if used + len(text) > remaining: break
            selected.append({"source": item.source, "line": item.line, "text": text,"method":item.method,"score":item.score}); used += len(text)
        content = "\n\n".join(f"Source: {x['source']}:{x['line'] or ''}\n{x['text']}" for x in selected)
        messages = [{"role": "system", "content": "Use only relevant supplied evidence. Cite source paths. Never claim to have executed unobserved actions."}]
        if core_context: messages.append({"role":"system","content":"Workspace core context (explicit saved snapshot):\n"+core_context})
        if content: messages.append({"role": "system", "content": "Selected evidence:\n" + content})
        messages.append({"role": "user", "content": request})
        return messages, selected


class Runtime:
    def __init__(self, settings: Settings, db: Database, retrieval: RetrievalRouter, model: ModelProvider, artifacts: Any = None):
        self.settings, self.db, self.retrieval, self.model = settings, db, retrieval, model
        self.tools, self.policy = registry(), AuthorizationPolicy()
        self.procedures = ProcedureRunner(self.tools, self.policy)
        self.artifacts = artifacts
    def execute(self, text: str, approved: bool = False, core_context: str = "") -> tuple[Task, str]:
        task = Task(text); task.transition(TaskState.ROUTING); started = time.perf_counter()
        try:
            lower = text.strip().lower()
            if any(x in lower for x in ("sudo ", "recursive delete", "rm -rf", "credentials", "system configuration")):
                task.route, task.authorization_state, task.reason_category = Route.DIRECT, "blocked", "deny_default_operation"
                task.transition(TaskState.DENIED)
                return self._finish(task, "Blocked by deny-default authorization policy.", started)
            write_match = re.fullmatch(r"write file (.+?):\s*(.*)", text.strip(), re.I | re.S)
            if write_match:
                task.route, task.reason_category = Route.DIRECT, "confirm_operation"
                try: target = str(self.retrieval.files.guard.resolve(write_match.group(1).strip()))
                except PermissionError as exc: task.error=str(exc); task.transition(TaskState.FAILED); return self._finish(task,f"Error: {exc}",started)
                action_id=str(uuid.uuid4()); now=time.time(); task.authorization_state="waiting_for_confirmation"; task.transition(TaskState.WAITING)
                self.db.save_task(task)
                with self.db.connect() as conn: conn.execute("INSERT INTO pending_actions VALUES(?,?,?,?,?,?,?)",(task.id,action_id,"write_text",json.dumps({"path":target,"content":write_match.group(2)}),now+900,"pending",now))
                return task, json.dumps({"confirmation_required":True,"task_id":task.id,"action_id":action_id,"summary":f"Write {len(write_match.group(2))} characters to {target}"})
            if re.search(r"\b(rename (?:file|path|this)|move (?:file|path|this)|run (?:a )?(?:local )?script)\b", lower):
                task.route, task.reason_category, task.authorization_state = Route.DIRECT, "unsupported_confirm_operation", "waiting_for_confirmation"
                task.transition(TaskState.WAITING); return self._finish(task, "This operation has no supported typed executor.", started)
            direct = self._direct(text)
            if direct:
                name, inputs = direct; task.route, task.reason_category = Route.DIRECT, "deterministic_match"; task.transition(TaskState.EXECUTING)
                tool = self.tools[name]; output, latency = tool.run(inputs)
                task.tool_executions.append({"tool": name, "version": tool.version, "risk": tool.risk.value, "input": inputs, "output": output, "latency_ms": latency, "status": "success"})
                task.transition(TaskState.COMPLETED); return self._finish(task, json.dumps(output, ensure_ascii=False), started)
            if lower.startswith("procedure lowercase: "):
                task.route, task.reason_category = Route.PROCEDURE, "validated_procedure_match"; task.transition(TaskState.EXECUTING)
                task.tool_executions = self.procedures.run(LOWERCASE_PROCEDURE, {"text": text.split(":",1)[1].strip()})
                task.transition(TaskState.COMPLETED); return self._finish(task, json.dumps(task.tool_executions[-1]["output"]), started)
            retrieval_query = self._retrieval_query(text)
            evidence = []
            if retrieval_query:
                task.route, task.reason_category = Route.RETRIEVAL, "explicit_search_intent"; task.transition(TaskState.SEARCHING)
                evidence, method = self.retrieval.retrieve(retrieval_query, self.settings.result_limit, "file" in lower or "path" in lower)
                task.selected_context = [{"source": x.source, "line": x.line, "snippet": x.snippet, "method": x.method} for x in evidence]
                if self.retrieval.last_failures: task.selected_context.append({"retrieval_failures":self.retrieval.last_failures})
                if self.settings.vector_enabled: task.selected_context.append({"semantic":self.retrieval.semantic_telemetry})
                if not any(x in lower for x in ("summarize", "explain", "synthesize")):
                    artifact = self.artifacts.create(task.id, evidence) if self.artifacts and len(evidence) > 10 else None
                    if artifact: task.selected_context=[{"artifact":artifact,"retrieval_method":method,"inline_count":0}]
                    task.transition(TaskState.COMPLETED)
                    return self._finish(task, json.dumps({"retrieval_method": method, "results": task.selected_context, "artifact":artifact}, ensure_ascii=False), started)
            task.route = task.route or Route.NEURAL; task.reason_category = task.reason_category or "open_ended_generation"; task.transition(TaskState.GENERATING)
            messages, selected = ContextManager(self.settings.context_budget).build(text, evidence, core_context); task.selected_context = selected
            result = self.model.generate(messages, self.settings.main_model)
            task.model_calls.append({"model": self.settings.main_model, **{k: result.get(k) for k in ("prompt_tokens","completion_tokens","latency_ms")}, "status": "success"})
            task.transition(TaskState.COMPLETED); return self._finish(task, result["text"], started)
        except (ToolValidationError, ValueError, PermissionError, ModelFailure, RuntimeError) as exc:
            task.error = str(exc)
            if task.state not in (TaskState.FAILED, TaskState.DENIED): task.transition(TaskState.FAILED)
            if isinstance(exc, ModelFailure): task.model_calls.append({"model": self.settings.main_model, "status": "error", "error": exc.category})
            return self._finish(task, f"Error: {exc}", started)
    def _finish(self, task: Task, response: str, started: float) -> tuple[Task, str]:
        task.updated_at = task.created_at + (time.perf_counter()-started); self.db.save_task(task); return task, response

    def resolve_confirmation(self, task_id: str, action_id: str, approve: bool) -> tuple[Task, str]:
        with self.db.connect() as conn:
            row=conn.execute("SELECT t.*,p.* FROM tasks t JOIN pending_actions p ON p.task_id=t.id WHERE t.id=? AND p.action_id=?",(task_id,action_id)).fetchone()
        if not row: raise PermissionError("confirmation mismatch")
        if row["status"]!="pending": raise PermissionError("confirmation already resolved")
        if row["expires_at"] < time.time():
            with self.db.connect() as conn: conn.execute("UPDATE pending_actions SET status='expired' WHERE action_id=?",(action_id,))
            raise PermissionError("confirmation expired")
        task=Task(row["raw_request"],id=task_id,route=Route(row["route"]),state=TaskState.WAITING,authorization_state=row["authorization_state"],created_at=row["created_at"],updated_at=row["updated_at"],reason_category=row["reason_category"])
        if not approve:
            task.authorization_state="rejected"; task.transition(TaskState.CANCELLED)
            with self.db.connect() as conn: conn.execute("UPDATE pending_actions SET status='rejected' WHERE action_id=?",(action_id,))
            self.db.save_task(task); return task,"Action rejected; nothing was executed."
        values=json.loads(row["tool_input_json"]); target=self.retrieval.files.guard.resolve(values["path"])
        task.authorization_state="authorized"; task.transition(TaskState.EXECUTING); target.parent.mkdir(parents=True,exist_ok=True); target.write_text(values["content"])
        task.tool_executions.append({"tool":"write_text","version":"1.0","risk":"CONFIRM","input":{"path":str(target),"content_length":len(values["content"])},"output":{"path":str(target),"bytes":target.stat().st_size},"status":"success","latency_ms":0})
        task.transition(TaskState.COMPLETED)
        with self.db.connect() as conn: conn.execute("UPDATE pending_actions SET status='approved' WHERE action_id=?",(action_id,))
        self.db.save_task(task); return task,f"Wrote {target.stat().st_size} bytes to {target}."
    @staticmethod
    def _direct(text: str) -> tuple[str, dict[str, Any]] | None:
        value = text.strip(); lower = value.lower()
        m = re.fullmatch(r"(?:calculate|calc)\s+(.+)", value, re.I)
        if m: return "calculator", {"expression": m.group(1)}
        if lower.startswith("lowercase: "): return "lowercase", {"text": value.split(":",1)[1].strip()}
        if lower.startswith("validate json: "): return "json_validate", {"text": value.split(":",1)[1].strip()}
        m = re.fullmatch(r"sort\s+(-?[\d.]+(?:\s*,\s*-?[\d.]+)*)", value, re.I)
        if m: return "sort_ascending", {"items": [float(x) for x in m.group(1).split(",")]}
        return None
    @staticmethod
    def _retrieval_query(text: str) -> str | None:
        m = re.search(r"(?:search|find|grep)(?:\s+(?:for|files?|paths?))?\s*[:\"]?(.+?)[\"]?$", text.strip(), re.I)
        if not m: return None
        return re.split(r"\s+(?:and\s+)?(?:summarize|explain|synthesize)\b",m.group(1).strip(),maxsplit=1,flags=re.I)[0].strip()
