from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import re
from typing import Any


SCHEMA_VERSION = 22
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace_path TEXT, archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, task_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL, ordinal INTEGER NOT NULL, created_at REAL NOT NULL, blocks_json TEXT);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, conversation_id TEXT, raw_request TEXT NOT NULL, route TEXT, state TEXT NOT NULL, authorization_state TEXT NOT NULL, reason_category TEXT, selected_context_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, duration_ms REAL, error TEXT);
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, source TEXT UNIQUE NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, metadata_json TEXT NOT NULL, indexed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS document_chunks(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(text, source UNINDEXED, document_id UNINDEXED);
CREATE TABLE IF NOT EXISTS tool_executions(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, tool_name TEXT NOT NULL, version TEXT NOT NULL, risk TEXT NOT NULL, input_json TEXT NOT NULL, output_json TEXT, status TEXT NOT NULL, latency_ms REAL NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS model_calls(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, model TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms REAL NOT NULL, status TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS procedures(id TEXT PRIMARY KEY, version TEXT NOT NULL, name TEXT NOT NULL, input_schema_json TEXT NOT NULL, steps_json TEXT NOT NULL, constraints_json TEXT NOT NULL, validated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS memory_facts(id INTEGER PRIMARY KEY, text TEXT NOT NULL, source TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS application_settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_project_contexts(conversation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_path TEXT, active_subject TEXT, context_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending_actions(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, action_id TEXT UNIQUE NOT NULL, tool_name TEXT NOT NULL, tool_input_json TEXT NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, path TEXT UNIQUE NOT NULL, result_count INTEGER NOT NULL, size_bytes INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_memory_embeddings(id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL, user_message TEXT NOT NULL, assistant_message TEXT NOT NULL, turn_ordinal INTEGER NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL, index_version TEXT NOT NULL, vector_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(conversation_id,turn_ordinal,model,index_version));
CREATE TABLE IF NOT EXISTS vector_embeddings(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, chunk_ordinal INTEGER NOT NULL, line_start INTEGER NOT NULL, text TEXT NOT NULL, content_hash TEXT NOT NULL, document_hash TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL, index_version TEXT NOT NULL, vector_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(document_id,chunk_ordinal,model,index_version));
CREATE TABLE IF NOT EXISTS coding_tasks(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, source_message_id TEXT UNIQUE, original_goal TEXT NOT NULL, status TEXT NOT NULL, activity TEXT NOT NULL, plan_json TEXT, active_plan_json TEXT, plan_revision INTEGER NOT NULL DEFAULT 0, pending_plan_json TEXT, subtask_progress_json TEXT, current_phase_id TEXT, retry_count INTEGER NOT NULL DEFAULT 0, max_retries INTEGER NOT NULL DEFAULT 2, replan_count INTEGER NOT NULL DEFAULT 0, replan_count_in_epoch INTEGER NOT NULL DEFAULT 0, recovery_epoch INTEGER NOT NULL DEFAULT 0, approved_scopes_json TEXT NOT NULL DEFAULT '[]', pending_authorization_json TEXT, pending_user_confirmation INTEGER NOT NULL DEFAULT 0, pause_requested INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0, queue_order INTEGER, recovery_action TEXT NOT NULL DEFAULT 'NONE', recovery_reason TEXT NOT NULL DEFAULT 'NONE', final_report_json TEXT, final_report_status TEXT NOT NULL DEFAULT 'NOT_RUN', execution_mode TEXT NOT NULL DEFAULT 'NORMAL', task_profile TEXT NOT NULL DEFAULT 'GENERAL_CODING', requirements_json TEXT NOT NULL DEFAULT '{}', required_mcp_json TEXT NOT NULL DEFAULT '[]', mcp_evidence_json TEXT NOT NULL DEFAULT '[]', batch_cursor INTEGER NOT NULL DEFAULT 0, batch_handoff_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS coding_phase_reports(id TEXT PRIMARY KEY, coding_task_id TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE, phase_id TEXT NOT NULL, attempt INTEGER NOT NULL, structured_report_json TEXT NOT NULL, validation_status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS interactive_planning_sessions(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, planning_mode TEXT NOT NULL, status TEXT NOT NULL, planning_revision INTEGER NOT NULL, pending_questions_json TEXT NOT NULL, answered_questions_json TEXT NOT NULL, assumptions_json TEXT NOT NULL, decisions_json TEXT NOT NULL, format_state_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS coding_task_telemetry(task_id TEXT PRIMARY KEY REFERENCES coding_tasks(id) ON DELETE CASCADE, record_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""


class Database:
    def __init__(self, path: str): self.path = path
    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db
    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            rows = db.execute("SELECT version FROM schema_version").fetchall() if exists else []
            if not rows:
                db.executescript(SCHEMA); db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif rows[0][0] == 1:
                db.execute("ALTER TABLE messages ADD COLUMN task_id TEXT")
                db.execute("ALTER TABLE messages ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0")
                db.execute("CREATE TABLE pending_actions(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, action_id TEXT UNIQUE NOT NULL, tool_name TEXT NOT NULL, tool_input_json TEXT NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL)")
                db.execute("CREATE TABLE artifacts(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, path TEXT UNIQUE NOT NULL, result_count INTEGER NOT NULL, size_bytes INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL)")
                db.execute("UPDATE schema_version SET version=2")
                rows=[(2,)]
            if rows and rows[0][0] == 2:
                db.execute("CREATE TABLE vector_embeddings(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, chunk_ordinal INTEGER NOT NULL, line_start INTEGER NOT NULL, text TEXT NOT NULL, content_hash TEXT NOT NULL, document_hash TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL, index_version TEXT NOT NULL, vector_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(document_id,chunk_ordinal,model,index_version))")
                db.execute("UPDATE schema_version SET version=3")
            if rows and rows[0][0] == 3:
                db.execute("CREATE TABLE IF NOT EXISTS conversation_memory_embeddings(id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL, user_message TEXT NOT NULL, assistant_message TEXT NOT NULL, turn_ordinal INTEGER NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL, vector_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(conversation_id,turn_ordinal,model))")
                db.execute("UPDATE schema_version SET version=4")
                rows=[(4,)]
            if rows and rows[0][0] == 4:
                db.execute("ALTER TABLE conversation_memory_embeddings ADD COLUMN index_version TEXT NOT NULL DEFAULT 'conversation-turn-v1'")
                db.execute("UPDATE schema_version SET version=5")
                rows=[(5,)]
            if rows and rows[0][0] == 5:
                # Legacy conversations deliberately all belong to one imported
                # project.  Their content never provides a reliable project map.
                now = __import__("time").time()
                imported_id = "imported-project-v1"
                db.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace_path TEXT, archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)", (imported_id, "Imported", None, 0, now, now))
                db.execute("ALTER TABLE conversations RENAME TO conversations_legacy")
                db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("INSERT INTO conversations(id,project_id,title,created_at,updated_at) SELECT id,?,title,created_at,created_at FROM conversations_legacy", (imported_id,))
                db.execute("DROP TABLE conversations_legacy")
                db.execute("UPDATE schema_version SET version=6")
                rows=[(6,)]
            if rows and rows[0][0] == 6:
                # SQLite rewrites child foreign keys when a parent table is
                # renamed.  The v5→v6 migration replaced conversations, so
                # rebuild messages to point at the replacement table while
                # retaining every message and its ordering metadata.
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("CREATE TABLE messages_rebuilt(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, task_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL, ordinal INTEGER NOT NULL, created_at REAL NOT NULL)")
                db.execute("INSERT INTO messages_rebuilt(id,conversation_id,task_id,role,content,ordinal,created_at) SELECT id,conversation_id,task_id,role,content,ordinal,created_at FROM messages")
                db.execute("DROP TABLE messages")
                db.execute("ALTER TABLE messages_rebuilt RENAME TO messages")
                db.execute("UPDATE schema_version SET version=7")
                rows=[(7,)]
            if rows and rows[0][0] == 7:
                db.execute("CREATE TABLE coding_tasks(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, original_goal TEXT NOT NULL, status TEXT NOT NULL, activity TEXT NOT NULL, plan_json TEXT, current_phase_id TEXT, retry_count INTEGER NOT NULL DEFAULT 0, max_retries INTEGER NOT NULL DEFAULT 2, approved_scopes_json TEXT NOT NULL DEFAULT '[]', pending_user_confirmation INTEGER NOT NULL DEFAULT 0, pause_requested INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0, queue_order INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("CREATE TABLE coding_phase_reports(id INTEGER PRIMARY KEY, coding_task_id TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE, phase_id TEXT NOT NULL, attempt INTEGER NOT NULL, structured_report_json TEXT NOT NULL, validation_status TEXT NOT NULL, created_at REAL NOT NULL)")
                db.execute("UPDATE schema_version SET version=8")
                rows=[(8,)]
            if rows and rows[0][0] == 8:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN pending_plan_json TEXT")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN replan_count INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN pending_authorization_json TEXT")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN final_report_json TEXT")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN final_report_status TEXT NOT NULL DEFAULT 'NOT_RUN'")
                db.execute("UPDATE schema_version SET version=9")
                rows=[(9,)]
            if rows and rows[0][0] == 9:
                db.execute("CREATE TABLE coding_phase_reports_v10(id TEXT PRIMARY KEY, coding_task_id TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE, phase_id TEXT NOT NULL, attempt INTEGER NOT NULL, structured_report_json TEXT NOT NULL, validation_status TEXT NOT NULL, created_at REAL NOT NULL)")
                db.execute("INSERT INTO coding_phase_reports_v10(id,coding_task_id,phase_id,attempt,structured_report_json,validation_status,created_at) SELECT CAST(id AS TEXT),coding_task_id,phase_id,attempt,structured_report_json,validation_status,created_at FROM coding_phase_reports")
                db.execute("DROP TABLE coding_phase_reports")
                db.execute("ALTER TABLE coding_phase_reports_v10 RENAME TO coding_phase_reports")
                db.execute("UPDATE schema_version SET version=10")
                rows=[(10,)]
            if rows and rows[0][0] == 10:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN recovery_action TEXT NOT NULL DEFAULT 'NONE'")
                db.execute("UPDATE schema_version SET version=11")
                rows=[(11,)]
            if rows and rows[0][0] == 11:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN recovery_reason TEXT NOT NULL DEFAULT 'NONE'")
                db.execute("UPDATE schema_version SET version=12")
                rows=[(12,)]
            if rows and rows[0][0] == 12:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN replan_count_in_epoch INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN recovery_epoch INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE schema_version SET version=13")
                rows=[(13,)]
            if rows and rows[0][0] == 13:
                # Optional typed display blocks keep structured presentation
                # available when conversations are reloaded while preserving
                # every legacy text-only message.
                db.execute("ALTER TABLE messages ADD COLUMN blocks_json TEXT")
                db.execute("UPDATE schema_version SET version=14")
                rows=[(14,)]
            if rows and rows[0][0] == 14:
                # The task graph is stored with a plan, while these compact
                # runtime records are the durable execution cursor.  Legacy
                # plans deliberately retain NULL and use phase-level UI.
                db.execute("ALTER TABLE coding_tasks ADD COLUMN subtask_progress_json TEXT")
                db.execute("UPDATE schema_version SET version=15")
                rows=[(15,)]
            if rows and rows[0][0] == 15:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'NORMAL'")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN batch_cursor INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN batch_handoff_json TEXT")
                db.execute("UPDATE schema_version SET version=16")
                rows=[(16,)]
            if rows and rows[0][0] == 16:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN source_message_id TEXT")
                db.execute("CREATE UNIQUE INDEX coding_tasks_source_message_id ON coding_tasks(source_message_id) WHERE source_message_id IS NOT NULL")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN task_profile TEXT NOT NULL DEFAULT 'GENERAL_CODING'")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN required_mcp_json TEXT NOT NULL DEFAULT '[]'")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN mcp_evidence_json TEXT NOT NULL DEFAULT '[]'")
                db.execute("UPDATE schema_version SET version=17")
                rows=[(17,)]
            if rows and rows[0][0] == 17:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN active_plan_json TEXT")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN plan_revision INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE coding_tasks SET active_plan_json=plan_json WHERE active_plan_json IS NULL")
                db.execute("UPDATE schema_version SET version=18")
                rows=[(18,)]
            if rows and rows[0][0] == 18:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN requirements_json TEXT NOT NULL DEFAULT '{}'")
                db.execute("UPDATE schema_version SET version=19")
                rows=[(19,)]
            if rows and rows[0][0] == 19:
                db.execute("CREATE TABLE interactive_planning_sessions(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, planning_mode TEXT NOT NULL, status TEXT NOT NULL, planning_revision INTEGER NOT NULL, pending_questions_json TEXT NOT NULL, answered_questions_json TEXT NOT NULL, assumptions_json TEXT NOT NULL, decisions_json TEXT NOT NULL, format_state_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("UPDATE schema_version SET version=20")
                rows=[(20,)]
            if rows and rows[0][0] == 20:
                db.execute("CREATE TABLE IF NOT EXISTS conversation_project_contexts(conversation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_path TEXT, active_subject TEXT, context_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("UPDATE schema_version SET version=21")
                rows=[(21,)]
            if rows and rows[0][0] == 21:
                db.execute("CREATE TABLE IF NOT EXISTS coding_task_telemetry(task_id TEXT PRIMARY KEY REFERENCES coding_tasks(id) ON DELETE CASCADE, record_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("UPDATE schema_version SET version=22")
                rows=[(22,)]
            if rows and rows[0][0] != SCHEMA_VERSION: raise RuntimeError(f"incompatible schema version {rows[0][0]}")
            db.commit()
        finally: db.close()
    def save_task(self, task: Any, conversation_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO tasks(id,conversation_id,raw_request,route,state,authorization_state,reason_category,selected_context_json,created_at,updated_at,duration_ms,error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    conversation_id=COALESCE(excluded.conversation_id,tasks.conversation_id),
                    raw_request=excluded.raw_request, route=excluded.route, state=excluded.state,
                    authorization_state=excluded.authorization_state, reason_category=excluded.reason_category,
                    selected_context_json=excluded.selected_context_json, updated_at=excluded.updated_at,
                    duration_ms=excluded.duration_ms, error=excluded.error""", (
                task.id, conversation_id, task.raw_request, task.route.value if task.route else None, task.state.value,
                task.authorization_state, task.reason_category, json.dumps(task.selected_context), task.created_at,
                task.updated_at, (task.updated_at-task.created_at)*1000, task.error))
            for item in task.tool_executions:
                values=(task.id, item.get("tool", "unknown"), item.get("version", "1.0"), item.get("risk", "SAFE"),
                    json.dumps(item.get("input", {})), json.dumps(item.get("output")), item.get("status", "success"), item.get("latency_ms", 0), item.get("error"))
                exists=db.execute("""SELECT 1 FROM tool_executions WHERE task_id=? AND tool_name=? AND version=? AND risk=?
                    AND input_json=? AND output_json IS ? AND status=? AND latency_ms=? AND error IS ?""",values).fetchone()
                if not exists: db.execute("INSERT INTO tool_executions(task_id,tool_name,version,risk,input_json,output_json,status,latency_ms,error) VALUES(?,?,?,?,?,?,?,?,?)", values)
            for item in task.model_calls:
                values=(task.id, item.get("model", ""), item.get("prompt_tokens"), item.get("completion_tokens"), item.get("latency_ms", 0), item.get("status", "unknown"), item.get("error"))
                exists=db.execute("""SELECT 1 FROM model_calls WHERE task_id=? AND model=? AND prompt_tokens IS ?
                    AND completion_tokens IS ? AND latency_ms=? AND status=? AND error IS ?""",values).fetchone()
                if not exists: db.execute("INSERT INTO model_calls(task_id,model,prompt_tokens,completion_tokens,latency_ms,status,error) VALUES(?,?,?,?,?,?,?)", values)
    def search_fts(self, query: str, limit: int, source: str | None = None) -> list[dict[str, Any]]:
        tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if not tokens:
            return []
        # MATCH parameters are still parsed as FTS syntax. Quote each literal
        # token and join them with an operator selected by OLCR, never the user.
        query = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        sql = "SELECT source, snippet(document_fts,0,'[',']','…',18) snippet, bm25(document_fts) rank FROM document_fts WHERE document_fts MATCH ?"
        args: list[Any] = [query]
        if source: sql += " AND source = ?"; args.append(source)
        sql += " ORDER BY rank LIMIT ?"; args.append(limit)
        with self.connect() as db: return [dict(x) for x in db.execute(sql, args)]
    def index_document(self, source: str, title: str, text: str, metadata: dict[str, Any], now: float) -> int:
        with self.connect() as db:
            old = db.execute("SELECT id FROM documents WHERE source=?", (source,)).fetchone()
            if old: db.execute("DELETE FROM document_fts WHERE document_id=?", (old[0],)); db.execute("DELETE FROM documents WHERE id=?", (old[0],))
            cur = db.execute("INSERT INTO documents(source,title,text,metadata_json,indexed_at) VALUES(?,?,?,?,?)", (source,title,text,json.dumps(metadata),now))
            doc_id = int(cur.lastrowid); db.execute("INSERT INTO document_fts(text,source,document_id) VALUES(?,?,?)", (text,source,doc_id)); return doc_id

    IMPORTED_PROJECT_ID = "imported-project-v1"

    def default_project_id(self) -> str:
        with self.connect() as db:
            row=db.execute("SELECT id FROM projects WHERE id=?", (self.IMPORTED_PROJECT_ID,)).fetchone()
            if row: return row[0]
            row=db.execute("SELECT id FROM projects WHERE archived=0 ORDER BY created_at LIMIT 1").fetchone()
            if row: return row[0]
            now=__import__("time").time()
            db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)", (self.IMPORTED_PROJECT_ID,"Imported",None,0,now,now))
            return self.IMPORTED_PROJECT_ID
    def create_coding_task(self, task_id: str, conversation_id: str, goal: str, status: str, activity: str, plan: dict[str, Any] | None, now: float, *, source_message_id: str | None = None) -> dict[str, Any]:
        existing_id: str | None = None
        with self.connect() as db:
            if source_message_id:
                existing=db.execute("SELECT id FROM coding_tasks WHERE source_message_id=?", (source_message_id,)).fetchone()
                if existing:
                    existing_id=str(existing[0])
            if existing_id is None:
                try:
                    serialized_plan=json.dumps(plan) if plan else None
                    db.execute("INSERT INTO coding_tasks(id,conversation_id,source_message_id,original_goal,status,activity,plan_json,active_plan_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(task_id,conversation_id,source_message_id,goal,status,activity,serialized_plan,serialized_plan,now,now))
                except sqlite3.IntegrityError as exc:
                    constraint = "coding_tasks.source_message_id" if "source_message_id" in str(exc) else "UNKNOWN"
                    duplicate_related = constraint != "UNKNOWN"
                    print(f"INTEGRITY_ERROR_CONSTRAINT={constraint} DUPLICATE_REQUEST_RELATED={'YES' if duplicate_related else 'NO'}", file=__import__("sys").stderr, flush=True)
                    if not source_message_id or not duplicate_related:
                        raise
                    existing=db.execute("SELECT id FROM coding_tasks WHERE source_message_id=?", (source_message_id,)).fetchone()
                    if not existing:
                        raise
                    existing_id=str(existing[0])
        return self.coding_task(existing_id or task_id) or {}
    def coding_task_for_source_message(self, source_message_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row=db.execute("SELECT id FROM coding_tasks WHERE source_message_id=?", (source_message_id,)).fetchone()
        return self.coding_task(row[0]) if row else None
    def coding_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row=db.execute("SELECT * FROM coding_tasks WHERE id=?",(task_id,)).fetchone()
            if not row:return None
            value=dict(row); legacy_plan=value.pop("plan_json") or "null"; active_plan=value.pop("active_plan_json") or legacy_plan; value["plan"]=json.loads(active_plan); value["active_plan"]=value["plan"]; value["pending_plan"]=json.loads(value.pop("pending_plan_json") or "null"); value["subtask_progress"]=json.loads(value.pop("subtask_progress_json") or "null"); value["approved_scopes"]=json.loads(value.pop("approved_scopes_json") or "[]"); value["pending_authorization"]=json.loads(value.pop("pending_authorization_json") or "null"); value["final_report"]=json.loads(value.pop("final_report_json") or "null"); value["batch_handoff"]=json.loads(value.pop("batch_handoff_json") or "null"); value["requirements"]=json.loads(value.pop("requirements_json") or "{}"); value["required_mcp"]=json.loads(value.pop("required_mcp_json") or "[]"); value["mcp_evidence"]=json.loads(value.pop("mcp_evidence_json") or "[]")
            return value
    def coding_tasks(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.connect() as db: ids=[x[0] for x in db.execute("SELECT id FROM coding_tasks WHERE conversation_id=? ORDER BY updated_at DESC",(conversation_id,))]
        return [x for task_id in ids if (x:=self.coding_task(task_id))]
    def save_coding_telemetry(self, task_id: str, record: dict[str, Any]) -> None:
        now=__import__("time").time()
        with self.connect() as db:
            db.execute("INSERT INTO coding_task_telemetry(task_id,record_json,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET record_json=excluded.record_json,updated_at=excluded.updated_at", (task_id, json.dumps(record, ensure_ascii=False), now, now))
            db.execute("DELETE FROM coding_task_telemetry WHERE task_id NOT IN (SELECT task_id FROM coding_task_telemetry ORDER BY updated_at DESC LIMIT 1000)")
    def coding_telemetry(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row=db.execute("SELECT record_json FROM coding_task_telemetry WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None
    def update_coding_task(self, task_id: str, **values: Any) -> dict[str, Any] | None:
        current=self.coding_task(task_id)
        if not current:return None
        allowed={"status","activity","plan","active_plan","plan_revision","pending_plan","subtask_progress","current_phase_id","retry_count","max_retries","replan_count","replan_count_in_epoch","recovery_epoch","approved_scopes","pending_authorization","pending_user_confirmation","pause_requested","archived","queue_order","recovery_action","recovery_reason","final_report","final_report_status","execution_mode","task_profile","requirements","required_mcp","mcp_evidence","batch_cursor","batch_handoff"}
        values={k:v for k,v in values.items() if k in allowed}
        if not values:return current
        if "plan" in values:
            serialized=json.dumps(values.pop("plan")); values["plan_json"]=serialized; values["active_plan_json"]=serialized
        if "active_plan" in values: values["active_plan_json"]=json.dumps(values.pop("active_plan"))
        if "pending_plan" in values: values["pending_plan_json"]=json.dumps(values.pop("pending_plan"))
        if "subtask_progress" in values: values["subtask_progress_json"]=json.dumps(values.pop("subtask_progress"))
        if "approved_scopes" in values: values["approved_scopes_json"]=json.dumps(values.pop("approved_scopes"))
        if "pending_authorization" in values: values["pending_authorization_json"]=json.dumps(values.pop("pending_authorization"))
        if "final_report" in values: values["final_report_json"]=json.dumps(values.pop("final_report"))
        if "batch_handoff" in values: values["batch_handoff_json"]=json.dumps(values.pop("batch_handoff"))
        if "requirements" in values: values["requirements_json"]=json.dumps(values.pop("requirements"))
        if "required_mcp" in values: values["required_mcp_json"]=json.dumps(values.pop("required_mcp"))
        if "mcp_evidence" in values: values["mcp_evidence_json"]=json.dumps(values.pop("mcp_evidence"))
        values["updated_at"]=__import__("time").time()
        with self.connect() as db: db.execute("UPDATE coding_tasks SET "+",".join(f"{k}=?" for k in values)+" WHERE id=?",(*values.values(),task_id))
        return self.coding_task(task_id)
    def save_coding_checkpoint(self, task_id: str, plan: dict, handoff: dict, cursor: int,
                               revision: int, blueprint_report: dict | None = None,
                               await_continuation: bool = True,
                               recovery_reason: str | None = None) -> dict:
        """Atomically publish a stable checkpoint with its optional planning report."""
        now = __import__('time').time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM coding_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["plan_revision"] != revision:
                raise RuntimeError("checkpoint task or plan revision changed")
            if json.loads(row["pending_authorization_json"] or "null"):
                raise RuntimeError("checkpoint cannot replace pending authorization")
            if row["pause_requested"] or row["archived"]:
                raise RuntimeError("checkpoint interrupted by user pause or archive")
            progress = json.loads(row["subtask_progress_json"] or "null")
            if blueprint_report is not None:
                connection.execute("INSERT INTO coding_phase_reports(id,coding_task_id,phase_id,attempt,structured_report_json,validation_status,created_at) VALUES(?,?,?,?,?,'PASS',?)",
                                   (str(__import__('uuid').uuid4()), task_id, blueprint_report["phase_id"], 0, json.dumps(blueprint_report), now))
                for entry in progress or []:
                    if entry.get("phase_id") == blueprint_report["phase_id"]:
                        entry.update(status="DONE", verification_status="PASS", finished_at=now, attempt=0)
            serialized = json.dumps(plan)
            connection.execute("UPDATE coding_tasks SET status=?, activity='NONE', queue_order=NULL, plan_json=?, active_plan_json=?, batch_cursor=?, batch_handoff_json=?, current_phase_id=?, subtask_progress_json=?, recovery_action='NONE', recovery_reason=?, updated_at=? WHERE id=?",
                               ("RESUMABLE" if await_continuation else "RUNNING", serialized, serialized, cursor, json.dumps(handoff), handoff["next_phase_id"], json.dumps(progress), recovery_reason or ("RESOURCE_CHECKPOINT" if await_continuation else "NONE"), now, task_id))
        return self.coding_task(task_id)

    def add_coding_phase_report(self, task_id: str, phase_id: str, attempt: int, report: dict[str, Any], validation_status: str, now: float) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO coding_phase_reports(id,coding_task_id,phase_id,attempt,structured_report_json,validation_status,created_at) VALUES(?,?,?,?,?,?,?)",(str(__import__('uuid').uuid4()),task_id,phase_id,attempt,json.dumps(report),validation_status,now))
    def update_coding_phase_report(self, report_id: str, report: dict[str, Any], validation_status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE coding_phase_reports SET structured_report_json=?, validation_status=? WHERE id=?",(json.dumps(report),validation_status,report_id))
    def coding_phase_reports(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows=[dict(x) for x in db.execute("SELECT * FROM coding_phase_reports WHERE coding_task_id=? ORDER BY created_at",(task_id,))]
        for row in rows: row["structured_report"]=json.loads(row.pop("structured_report_json"))
        return rows
    def recover_interrupted_coding_tasks(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE coding_tasks SET status='RESUMABLE', activity='NONE', updated_at=? WHERE status IN ('PLANNING','RUNNING','FINAL_REPORTING','QUEUED')",(__import__('time').time(),))
    def enqueue_coding_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            next_order=db.execute("SELECT COALESCE(MAX(queue_order),0)+1 FROM coding_tasks WHERE status='QUEUED'").fetchone()[0]
            db.execute("UPDATE coding_tasks SET status='QUEUED',activity='NONE',pause_requested=0,queue_order=?,updated_at=? WHERE id=?",(next_order,__import__('time').time(),task_id))
        return self.coding_task(task_id)
    def next_queued_coding_task(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row=db.execute("SELECT id FROM coding_tasks WHERE status='QUEUED' AND archived=0 AND pause_requested=0 ORDER BY queue_order,id LIMIT 1").fetchone()
        return self.coding_task(row[0]) if row else None
    def dequeue_coding_task(self, task_id: str) -> dict[str, Any] | None:
        return self.update_coding_task(task_id,status="RESUMABLE",activity="NONE",queue_order=None)
    def coding_task_queue_position(self, task_id: str) -> int | None:
        task=self.coding_task(task_id)
        if not task or task.get("status") != "QUEUED" or task.get("queue_order") is None: return None
        with self.connect() as db:
            row=db.execute("SELECT COUNT(*) FROM coding_tasks WHERE status='QUEUED' AND archived=0 AND queue_order<?",(task["queue_order"],)).fetchone()
        return int(row[0])+1
    def accept_pending_plan(self, task_id: str) -> dict[str, Any] | None:
        task=self.coding_task(task_id)
        if not task or not task.get("pending_plan"): return task
        scopes=list(task.get("approved_scopes") or [])
        pending=task.get("pending_authorization")
        if pending and pending not in scopes: scopes.append(pending)
        return self.update_coding_task(task_id,plan=task["pending_plan"],pending_plan=None,pending_authorization=None,pending_user_confirmation=0,approved_scopes=scopes)
    def projects(self, include_archived: bool=False) -> list[dict[str, Any]]:
        sql="SELECT * FROM projects" + ("" if include_archived else " WHERE archived=0") + " ORDER BY updated_at DESC, created_at DESC"
        with self.connect() as db:
            rows=[dict(x) for x in db.execute(sql)]
            for row in rows:
                workspace=row.get("workspace_path")
                if workspace and not __import__("pathlib").Path(workspace).is_dir():
                    row["workspace_path"]=None
                    db.execute("UPDATE projects SET workspace_path=NULL, updated_at=? WHERE id=?",(__import__("time").time(),row["id"]))
            return rows
    def project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row=db.execute("SELECT * FROM projects WHERE id=?",(project_id,)).fetchone(); return dict(row) if row else None
    def create_project(self, name: str, workspace_path: str | None, now: float, project_id: str) -> dict[str, Any]:
        with self.connect() as db: db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)",(project_id,name[:120],workspace_path,0,now,now))
        return self.project(project_id) or {}
    def update_project(self, project_id: str, name: str | None, workspace_path: str | None, archived: bool | None, now: float) -> dict[str, Any] | None:
        current=self.project(project_id)
        if not current:return None
        values=(name.strip()[:120] if name is not None else current["name"], workspace_path if workspace_path is not None else current["workspace_path"], int(archived) if archived is not None else current["archived"],now,project_id)
        with self.connect() as db: db.execute("UPDATE projects SET name=?,workspace_path=?,archived=?,updated_at=? WHERE id=?",values)
        return self.project(project_id)
    def delete_project(self, project_id: str) -> bool:
        if not self.project(project_id): return False
        with self.connect() as db:
            ids=[r[0] for r in db.execute("SELECT id FROM conversations WHERE project_id=?",(project_id,))]
            for conversation_id in ids:
                db.execute("DELETE FROM messages WHERE conversation_id=?",(conversation_id,))
            db.execute("DELETE FROM conversations WHERE project_id=?",(project_id,))
            db.execute("DELETE FROM projects WHERE id=?",(project_id,))
        return True
    def create_conversation(self, title: str, now: float, conversation_id: str, project_id: str | None = None) -> str:
        project_id=project_id or self.default_project_id()
        with self.connect() as db: db.execute("INSERT INTO conversations VALUES(?,?,?,?,?)", (conversation_id, project_id, title[:120] or "New conversation", now, now))
        return conversation_id
    def add_message(self, conversation_id: str, role: str, content: str, now: float, message_id: str, task_id: str | None = None, blocks: list[dict[str, Any]] | None = None) -> None:
        with self.connect() as db:
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            db.execute("INSERT INTO messages(id,conversation_id,task_id,role,content,ordinal,created_at,blocks_json) VALUES(?,?,?,?,?,?,?,?)", (message_id, conversation_id, task_id, role, content, ordinal, now, json.dumps(blocks, ensure_ascii=False) if blocks else None))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?",(now,conversation_id))
    def active_interactive_planning(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row=db.execute("SELECT * FROM interactive_planning_sessions WHERE conversation_id=? AND status='ACTIVE' ORDER BY planning_revision DESC, updated_at DESC LIMIT 1", (conversation_id,)).fetchone()
        return self._interactive_planning_row(row)
    def _interactive_planning_row(self, row: Any) -> dict[str, Any] | None:
        if not row: return None
        value=dict(row)
        for key in ("pending_questions", "answered_questions", "assumptions", "decisions", "format_state"):
            value[key]=json.loads(value.pop(key + "_json") or ("[]" if key in {"pending_questions", "answered_questions", "assumptions"} else "{}"))
        return value
    def create_interactive_planning(self, conversation_id: str, pending_questions: list[dict[str, Any]], format_state: dict[str, Any] | None = None) -> dict[str, Any]:
        now=__import__("time").time()
        with self.connect() as db:
            prior=db.execute("SELECT COALESCE(MAX(planning_revision),0) FROM interactive_planning_sessions WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            db.execute("UPDATE interactive_planning_sessions SET status='SUPERSEDED', updated_at=? WHERE conversation_id=? AND status='ACTIVE'", (now, conversation_id))
            session_id=str(__import__("uuid").uuid4())
            db.execute("INSERT INTO interactive_planning_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (session_id,conversation_id,"INTERACTIVE","ACTIVE",int(prior)+1,json.dumps(pending_questions,ensure_ascii=False),"[]","[]","{}",json.dumps(format_state or {},ensure_ascii=False),now,now))
        return self.active_interactive_planning(conversation_id) or {}
    def update_interactive_planning(self, session_id: str, **values: Any) -> dict[str, Any] | None:
        allowed={"status","pending_questions","answered_questions","assumptions","decisions","format_state"}
        values={key:value for key,value in values.items() if key in allowed}
        if not values: return None
        for key in ("pending_questions","answered_questions","assumptions","decisions","format_state"):
            if key in values: values[key + "_json"]=json.dumps(values.pop(key),ensure_ascii=False)
        values["updated_at"]=__import__("time").time()
        with self.connect() as db:
            db.execute("UPDATE interactive_planning_sessions SET "+",".join(f"{key}=?" for key in values)+" WHERE id=?", (*values.values(),session_id))
            row=db.execute("SELECT * FROM interactive_planning_sessions WHERE id=?", (session_id,)).fetchone()
        return self._interactive_planning_row(row)
    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not row: return None
            messages=[]
            for x in db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY ordinal", (conversation_id,)):
                message=dict(x)
                raw=message.pop("blocks_json", None)
                if raw:
                    try: message["blocks"]=json.loads(raw)
                    except (TypeError, ValueError): pass
                messages.append(message)
            return {**dict(row), "messages": messages}
    def conversations(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db: return [dict(x) for x in db.execute("SELECT * FROM conversations WHERE project_id=? ORDER BY updated_at DESC, created_at DESC",(project_id,))]
    def rename_conversation(self, conversation_id: str, title: str, now: float) -> dict[str, Any] | None:
        cleaned = title.strip()[:120]
        if not cleaned: return None
        with self.connect() as db: db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=?", (cleaned, now, conversation_id))
        return self.conversation(conversation_id)
    def completed_turns(self, exclude_conversation: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            sql = """SELECT u.conversation_id,u.ordinal,u.content user_message,a.content assistant_message
                     FROM messages u JOIN messages a ON a.conversation_id=u.conversation_id AND a.ordinal=u.ordinal+1
                     JOIN conversations c ON c.id=u.conversation_id
                     WHERE u.role='user' AND a.role='assistant'"""
            args=[]
            if project_id: sql += " AND c.project_id=?"; args.append(project_id)
            if exclude_conversation: sql += " AND u.conversation_id != ?"; args.append(exclude_conversation)
            sql += " ORDER BY u.created_at"
            return [dict(x) for x in db.execute(sql,args)]
    def save_memory_embedding(self, turn: dict[str, Any], model: str, vector: list[float], now: float, index_version: str) -> None:
        with self.connect() as db: db.execute("INSERT OR REPLACE INTO conversation_memory_embeddings(conversation_id,user_message,assistant_message,turn_ordinal,model,dimension,index_version,vector_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (turn['conversation_id'],turn['user_message'],turn['assistant_message'],turn['ordinal'],model,len(vector),index_version,json.dumps(vector),now))
    def memory_embeddings(self, model: str, dimension: int, index_version: str) -> list[dict[str, Any]]:
        with self.connect() as db: return [dict(x) for x in db.execute("SELECT * FROM conversation_memory_embeddings WHERE model=? AND dimension=? AND index_version=?",(model,dimension,index_version))]
    def delete_memory_for_conversation(self, conversation_id: str) -> None:
        with self.connect() as db: db.execute("DELETE FROM conversation_memory_embeddings WHERE conversation_id=?", (conversation_id,))
    def save_setting(self, key: str, value: Any, now: float) -> None:
        with self.connect() as db: db.execute("INSERT OR REPLACE INTO application_settings VALUES(?,?,?)", (key,json.dumps(value),now))
    def load_settings(self) -> dict[str, Any]:
        with self.connect() as db:
            result = {x["key"]: json.loads(x["value_json"]) for x in db.execute("SELECT * FROM application_settings")}
            # Compatibility view for older callers; startup uses the isolated
            # application loader below and never sees these dynamic keys.
            for row in db.execute("SELECT conversation_id, context_json FROM conversation_project_contexts"):
                result["conversation_project_context:" + row["conversation_id"]] = json.loads(row["context_json"])
            return result
    def load_application_settings(self) -> dict[str, Any]:
        """Load only the application-settings namespace for process startup."""
        with self.connect() as db:
            rows = db.execute("SELECT key,value_json FROM application_settings WHERE key NOT LIKE 'conversation_project_context:%'")
            return {row["key"]: json.loads(row["value_json"]) for row in rows}
    def save_conversation_project_context(self, conversation_id: str, value: dict[str, Any], now: float) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO conversation_project_contexts(conversation_id,project_id,workspace_path,active_subject,context_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET project_id=excluded.project_id,workspace_path=excluded.workspace_path,active_subject=excluded.active_subject,context_json=excluded.context_json,revision=excluded.revision,updated_at=excluded.updated_at", (conversation_id, str(value.get("project_id") or ""), value.get("workspace_path"), value.get("current_subject"), json.dumps(value), int(value.get("revision") or 0), now, now))
    def load_conversation_project_context(self, conversation_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT context_json FROM conversation_project_contexts WHERE conversation_id=?", (conversation_id,)).fetchone()
            return json.loads(row["context_json"]) if row else {}
    def migrate_legacy_conversation_project_contexts(self) -> dict[str, int]:
        """Atomically move legacy dynamic keys, retaining malformed sources."""
        migrated = malformed = 0
        with self.connect() as db:
            rows = db.execute("SELECT key,value_json,updated_at FROM application_settings WHERE key LIKE 'conversation_project_context:%'").fetchall()
            for row in rows:
                conversation_id = row["key"].split(":", 1)[1]
                try:
                    value = json.loads(row["value_json"])
                    if not isinstance(value, dict) or not conversation_id or not value.get("project_id"):
                        raise ValueError("invalid project context")
                    db.execute("INSERT INTO conversation_project_contexts(conversation_id,project_id,workspace_path,active_subject,context_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET project_id=excluded.project_id,workspace_path=excluded.workspace_path,active_subject=excluded.active_subject,context_json=excluded.context_json,revision=excluded.revision,updated_at=excluded.updated_at", (conversation_id, str(value.get("project_id")), value.get("workspace_path"), value.get("current_subject"), json.dumps(value), int(value.get("revision") or 0), row["updated_at"], row["updated_at"]))
                    db.execute("DELETE FROM application_settings WHERE key=?", (row["key"],))
                    migrated += 1
                except (ValueError, TypeError, json.JSONDecodeError):
                    malformed += 1
        return {"legacy_rows": len(rows), "migrated": migrated, "malformed": malformed}
