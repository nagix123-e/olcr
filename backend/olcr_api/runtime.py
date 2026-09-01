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
            if self._implementation_intent(lower):
                return self._execute_implementation(task, text, core_context, started)
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
    def _implementation_intent(lower: str) -> bool:
        # Treat explicit file-creation requests as implementation work even when
        # the user lists filenames directly (e.g. ``create index.html, style.css``).
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", lower) and re.search(r"(作成|作って|実装|書き込|更新|変更|完成|ファイル).*(workspace|ワークスペース|ファイル|index\.html|style\.css|game\.js|tetris|テトリス)", lower):
            return True
        return bool(re.search(r"\b(implement|create (?:the |.* )?files?|create\s+[^\n]*(?:\.(?:html?|css|js|jsx|ts|py)\b)|modify|fix|refactor|update|write .* (?:into|to) (?:the )?(?:project|workspace)|build)\b", lower))

    def _workspace_files(self) -> list[str]:
        root = self.settings.allowed_roots[0] if self.settings.allowed_roots else None
        if not root: raise PermissionError("an authorized workspace is required for implementation work")
        base = Path(root)
        return sorted(str(item.relative_to(base)) for item in base.rglob("*") if item.is_file())[:200]

    def _execute_implementation(self, task: Task, text: str, core_context: str, started: float) -> tuple[Task, str]:
        """One bounded model→typed-file-tools→inspection loop for workspace mutations."""
        task.route, task.reason_category, task.authorization_state = Route.IMPLEMENTATION, "authorized_workspace_mutation", "authorized"
        task.transition(TaskState.EXECUTING)
        files = self._workspace_files()
        task.tool_executions.append({"tool":"workspace_list","version":"1.0","risk":"SAFE","input":{},"output":{"files":files},"status":"success","latency_ms":0})
        prompt = ("You have bounded filesystem tools inside the authorized workspace only. Inspect the listed files, then return ONLY JSON: "
                  '{"operations":[{"op":"write","path":"relative/path","content":"..."}],"verification":"..."}. '
                  "For implementation requests you must provide one or more write operations; do not return Markdown code blocks or claim files were changed without operations. "
                  f"Workspace files: {files}.\nRequest: {text}")
        if core_context: prompt += "\nDevelopment plan: " + core_context[: self.settings.context_budget // 4]
        messages=[{"role":"system","content":"Use only the supplied workspace tool protocol."},{"role":"user","content":prompt}]
        result = self.model.generate(messages, self.settings.main_model)
        task.model_calls.append({"model":self.settings.main_model, **{k:result.get(k) for k in ("prompt_tokens","completion_tokens","latency_ms")}, "status":"success"})
        raw=result.get("text", "") if isinstance(result,dict) else ""
        # Qwen may wrap a single otherwise-valid JSON object in a Markdown fence.
        if raw.strip().startswith("```") and raw.strip().endswith("```"):
            raw=raw.strip().split("\n",1)[-1].rsplit("```",1)[0].strip()
        try: payload=json.loads(raw); operations=payload.get("operations")
        except (TypeError, ValueError, KeyError) as exc: raise RuntimeError("implementation model did not return a valid file-operation plan") from exc
        if not isinstance(operations,list) or not operations: raise RuntimeError("implementation model returned no file operations; no workspace files were changed")
        if len(operations)>20: raise RuntimeError("implementation plan exceeds the 20-operation safety limit")
        changed=[]
        for operation in operations:
            if not isinstance(operation,dict) or operation.get("op")!="write" or not isinstance(operation.get("path"),str) or not isinstance(operation.get("content"),str):
                raise RuntimeError("implementation plan contains an unsupported file operation")
            if len(operation["content"])>500_000: raise RuntimeError("implementation file content exceeds the safety limit")
            requested=Path(operation["path"])
            if not requested.is_absolute(): requested=Path(self.settings.allowed_roots[0]) / requested
            target=self.retrieval.files.guard.resolve(str(requested))
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(operation["content"])
            changed.append(str(target))
            task.tool_executions.append({"tool":"workspace_write","version":"1.0","risk":"SAFE","input":{"path":str(target),"content_length":len(operation["content"])},"output":{"path":str(target),"bytes":target.stat().st_size},"status":"success","latency_ms":0})
        inspected=[]
        for target in changed:
            value=Path(target).read_text()
            inspected.append({"path":target,"bytes":len(value)})
            task.tool_executions.append({"tool":"workspace_read","version":"1.0","risk":"SAFE","input":{"path":target},"output":{"bytes":len(value)},"status":"success","latency_ms":0})
        task.transition(TaskState.COMPLETED)
        return self._finish(task, "Implemented workspace changes: " + ", ".join(changed) + ". Verified written files: " + ", ".join(x["path"] for x in inspected) + ".", started)
    @staticmethod
    def _retrieval_query(text: str) -> str | None:
        m = re.search(r"(?:search|find|grep)(?:\s+(?:for|files?|paths?))?\s*[:\"]?(.+?)[\"]?$", text.strip(), re.I)
        if not m: return None
        return re.split(r"\s+(?:and\s+)?(?:summarize|explain|synthesize)\b",m.group(1).strip(),maxsplit=1,flags=re.I)[0].strip()
