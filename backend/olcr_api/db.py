from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import re
from typing import Any


SCHEMA_VERSION = 39
CONVERSATION_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_memory_summaries(
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    first_ordinal INTEGER NOT NULL,
    last_ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    index_version TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    source_at REAL NOT NULL,
    UNIQUE(conversation_id,model,index_version,first_ordinal)
);
CREATE INDEX IF NOT EXISTS memory_summary_model ON conversation_memory_summaries(model,index_version,dimension);
CREATE INDEX IF NOT EXISTS memory_turn_recency ON conversation_memory_embeddings(model,index_version,dimension,created_at DESC);
CREATE INDEX IF NOT EXISTS memory_message_ordinal ON messages(conversation_id,ordinal);
CREATE INDEX IF NOT EXISTS memory_assistant_recency ON messages(created_at DESC,id) WHERE role='assistant';
CREATE INDEX IF NOT EXISTS memory_project_conversations ON conversations(project_id,id);
CREATE TRIGGER IF NOT EXISTS memory_invalidate_updated_message
AFTER UPDATE OF content,blocks_json,role,ordinal,conversation_id ON messages
BEGIN
    DELETE FROM conversation_memory_summaries WHERE conversation_id=OLD.conversation_id
        AND OLD.ordinal<=last_ordinal+1;
    DELETE FROM conversation_memory_embeddings WHERE conversation_id=OLD.conversation_id
        AND turn_ordinal IN (OLD.ordinal,OLD.ordinal-1);
END;
CREATE TRIGGER IF NOT EXISTS memory_invalidate_deleted_message
AFTER DELETE ON messages
BEGIN
    DELETE FROM conversation_memory_summaries WHERE conversation_id=OLD.conversation_id
        AND OLD.ordinal<=last_ordinal+1;
    DELETE FROM conversation_memory_embeddings WHERE conversation_id=OLD.conversation_id
        AND turn_ordinal IN (OLD.ordinal,OLD.ordinal-1);
END;
CREATE TRIGGER IF NOT EXISTS memory_invalidate_deleted_conversation
AFTER DELETE ON conversations
BEGIN
    DELETE FROM conversation_memory_embeddings WHERE conversation_id=OLD.id;
END;
"""

MEMORY_ELIGIBILITY = """u.role='user' AND a.role='assistant' AND a.content <> ''
  AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.id=a.task_id AND t.state IN ('failed','denied','cancelled'))
  AND NOT EXISTS (SELECT 1 FROM json_each(COALESCE(u.blocks_json,'[]')) b WHERE json_extract(b.value,'$.type')='medication_management')
  AND NOT EXISTS (SELECT 1 FROM json_each(COALESCE(a.blocks_json,'[]')) b WHERE json_extract(b.value,'$.type')='medication_management')
"""
MEMORY_PAIRS = """
FROM messages u JOIN messages a ON a.conversation_id=u.conversation_id AND a.ordinal=u.ordinal+1
JOIN conversations c ON c.id=u.conversation_id
WHERE """ + MEMORY_ELIGIBILITY
MEMORY_TURN_COLUMNS = """u.conversation_id,u.ordinal,u.id user_id,a.id assistant_id,
                         u.content user_message,a.content assistant_message,a.created_at source_at"""
CUSTOM_AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_agents(id TEXT PRIMARY KEY, definition_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS custom_agent_conversations(conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE, agent_id TEXT NOT NULL REFERENCES custom_agents(id), definition_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custom_agent_builders(conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE, state_json TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS custom_agent_builder_turns(conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, message_id TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(conversation_id,message_id));
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace_path TEXT, archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id), title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, task_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL, ordinal INTEGER NOT NULL, created_at REAL NOT NULL, blocks_json TEXT);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, conversation_id TEXT, raw_request TEXT NOT NULL, route TEXT, state TEXT NOT NULL, authorization_state TEXT NOT NULL, reason_category TEXT, selected_context_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, duration_ms REAL, error TEXT);
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, source TEXT UNIQUE NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, metadata_json TEXT NOT NULL, indexed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS document_chunks(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(text, source UNINDEXED, document_id UNINDEXED);
CREATE TABLE IF NOT EXISTS tool_executions(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, tool_name TEXT NOT NULL, version TEXT NOT NULL, risk TEXT NOT NULL, input_json TEXT NOT NULL, output_json TEXT, status TEXT NOT NULL, latency_ms REAL NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS model_calls(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, model TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms REAL NOT NULL, status TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS task_reasoning(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, block_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS procedures(id TEXT PRIMARY KEY, version TEXT NOT NULL, name TEXT NOT NULL, input_schema_json TEXT NOT NULL, steps_json TEXT NOT NULL, constraints_json TEXT NOT NULL, validated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_recipes(id TEXT PRIMARY KEY, version INTEGER NOT NULL, name TEXT NOT NULL, recipe_json TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS web_scrape_runs(id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL REFERENCES web_recipes(id) ON DELETE RESTRICT, recipe_json TEXT NOT NULL, status TEXT NOT NULL, page_cursor INTEGER NOT NULL DEFAULT 0, pages_completed INTEGER NOT NULL DEFAULT 0, record_count INTEGER NOT NULL DEFAULT 0, last_page_hash TEXT, stop_reason TEXT, next_url TEXT, error TEXT, recovery_json TEXT NOT NULL DEFAULT '{}', metrics_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS web_scrape_rows(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES web_scrape_runs(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, page_number INTEGER NOT NULL, dedupe_key TEXT, row_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(run_id,ordinal), UNIQUE(run_id,dedupe_key));
CREATE TABLE IF NOT EXISTS desktop_world_states(session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS desktop_world_states_expiry ON desktop_world_states(expires_at);
CREATE TABLE IF NOT EXISTS memory_facts(id INTEGER PRIMARY KEY, text TEXT NOT NULL, source TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS application_settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_project_contexts(conversation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_path TEXT, active_subject TEXT, context_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending_actions(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, action_id TEXT UNIQUE NOT NULL, tool_name TEXT NOT NULL, tool_input_json TEXT NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, path TEXT UNIQUE NOT NULL, result_count INTEGER NOT NULL, size_bytes INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_memory_embeddings(id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL, user_message TEXT NOT NULL, assistant_message TEXT NOT NULL, turn_ordinal INTEGER NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL, index_version TEXT NOT NULL, vector_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(conversation_id,turn_ordinal,model,index_version));
CREATE TABLE IF NOT EXISTS vector_embeddings(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, chunk_ordinal INTEGER NOT NULL, line_start INTEGER NOT NULL, text TEXT NOT NULL, content_hash TEXT NOT NULL, document_hash TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL, index_version TEXT NOT NULL, vector_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(document_id,chunk_ordinal,model,index_version));
CREATE TABLE IF NOT EXISTS coding_tasks(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, source_message_id TEXT UNIQUE, response_language TEXT NOT NULL DEFAULT 'ja', original_goal TEXT NOT NULL, status TEXT NOT NULL, activity TEXT NOT NULL, plan_json TEXT, active_plan_json TEXT, plan_revision INTEGER NOT NULL DEFAULT 0, pending_plan_json TEXT, subtask_progress_json TEXT, current_phase_id TEXT, retry_count INTEGER NOT NULL DEFAULT 0, max_retries INTEGER NOT NULL DEFAULT 2, replan_count INTEGER NOT NULL DEFAULT 0, replan_count_in_epoch INTEGER NOT NULL DEFAULT 0, recovery_epoch INTEGER NOT NULL DEFAULT 0, human_recovery_epochs INTEGER NOT NULL DEFAULT 0, task_total_plan_repair_count INTEGER NOT NULL DEFAULT 0, task_total_recovery_model_calls INTEGER NOT NULL DEFAULT 0, approved_scopes_json TEXT NOT NULL DEFAULT '[]', pending_authorization_json TEXT, pending_user_confirmation INTEGER NOT NULL DEFAULT 0, pause_requested INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0, console_hidden INTEGER NOT NULL DEFAULT 0, queue_order INTEGER, recovery_action TEXT NOT NULL DEFAULT 'NONE', recovery_reason TEXT NOT NULL DEFAULT 'NONE', final_report_json TEXT, final_report_status TEXT NOT NULL DEFAULT 'NOT_RUN', execution_mode TEXT NOT NULL DEFAULT 'NORMAL', task_profile TEXT NOT NULL DEFAULT 'GENERAL_CODING', requirements_json TEXT NOT NULL DEFAULT '{}', required_mcp_json TEXT NOT NULL DEFAULT '[]', mcp_evidence_json TEXT NOT NULL DEFAULT '[]', batch_cursor INTEGER NOT NULL DEFAULT 0, batch_handoff_json TEXT, resume_state_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS coding_phase_reports(id TEXT PRIMARY KEY, coding_task_id TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE, phase_id TEXT NOT NULL, attempt INTEGER NOT NULL, structured_report_json TEXT NOT NULL, validation_status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS interactive_planning_sessions(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, planning_mode TEXT NOT NULL, status TEXT NOT NULL, planning_revision INTEGER NOT NULL, pending_questions_json TEXT NOT NULL, answered_questions_json TEXT NOT NULL, assumptions_json TEXT NOT NULL, decisions_json TEXT NOT NULL, format_state_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS coding_task_telemetry(task_id TEXT PRIMARY KEY REFERENCES coding_tasks(id) ON DELETE CASCADE, record_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _ensure_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    """Apply an additive migration safely when a version marker outlives a partial migration."""
    column = definition.split(None, 1)[0].strip('"`[]')
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
        raise ValueError("invalid schema identifier")
    if not _table_exists(db, table):
        return
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


class Database:
    def __init__(self, path: str): self.path = path
    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def save_procedure(self, record: dict[str, Any]) -> None:
        """Persist a bounded procedure and its applicability metadata in the existing table."""
        procedure_id=str(record.get("id") or "")
        if not procedure_id:raise ValueError("procedure id is required")
        with self.connect() as db:
            old=db.execute("SELECT version,constraints_json FROM procedures WHERE id=?",(procedure_id,)).fetchone()
            previous=json.loads(old[1]) if old else {}
            same_version=bool(old and old[0]==str(record.get("version") or ""))
            metadata={
                "applicability":str(record.get("applicability") or ""),
                "environment_signature":str(record.get("environment_signature") or ""),
                "preconditions":record.get("preconditions") or [],
                "status":str(record.get("status") or "ACTIVE"),
                "run_stats":previous.get("run_stats",{"runs":0,"successes":0,"verified_successes":0,"failures":0,"consecutive_failures":0,"last_verified":None}) if same_version else {"runs":0,"successes":0,"verified_successes":0,"failures":0,"consecutive_failures":0,"last_verified":None},
            }
            captured_trace=record.get("captured_trace")
            if isinstance(captured_trace,dict):
                metadata["captured_trace"]=captured_trace
            elif isinstance(previous.get("captured_trace"),dict):
                metadata["captured_trace"]=previous["captured_trace"]
            review=record.get("review")
            if isinstance(review,dict):
                metadata["review"]=review
            elif isinstance(previous.get("review"),dict):
                metadata["review"]=previous["review"]
            review_history=record.get("review_history")
            if isinstance(review_history,list):
                metadata["review_history"]=[item for item in review_history if isinstance(item,dict)][-16:]
            elif isinstance(previous.get("review_history"),list):
                metadata["review_history"]=previous["review_history"][-16:]
            if same_version and previous.get("status")=="SUSPECT" and metadata["status"]=="ACTIVE":
                metadata["run_stats"]["consecutive_failures"]=0
            db.execute("INSERT INTO procedures(id,version,name,input_schema_json,steps_json,constraints_json,validated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,name=excluded.name,input_schema_json=excluded.input_schema_json,steps_json=excluded.steps_json,constraints_json=excluded.constraints_json,validated=excluded.validated",
                (procedure_id,str(record.get("version") or "1"),str(record.get("name") or procedure_id),
                 json.dumps(record.get("required_inputs") or []),json.dumps(record.get("steps") or []),
                 json.dumps(metadata),int(bool(record.get("validated")))))

    def procedure(self, procedure_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row=db.execute("SELECT * FROM procedures WHERE id=?",(procedure_id,)).fetchone()
        if not row:return None
        value=dict(row)
        try:
            constraints=json.loads(value.pop("constraints_json") or "{}")
            value["required_inputs"]=json.loads(value.pop("input_schema_json") or "[]")
            value["steps"]=json.loads(value.pop("steps_json") or "[]")
        except (TypeError,ValueError,json.JSONDecodeError):
            return None
        value.update(constraints)
        value["validated"]=bool(value["validated"])
        return value

    def procedures(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows=db.execute("SELECT id FROM procedures ORDER BY name,id").fetchall()
        results=[value for row in rows if (value:=self.procedure(str(row[0]))) is not None]
        return [item for item in results if item.get("status")=="ACTIVE" and item.get("validated")] if active_only else results

    def record_procedure_run(self, procedure_id: str, success: bool, observations: list[dict[str, Any]]) -> None:
        now=__import__("time").time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT constraints_json,validated FROM procedures WHERE id=?",(procedure_id,)).fetchone()
            if not row:return
            try:metadata=json.loads(row[0] or "{}")
            except (TypeError,ValueError):metadata={}
            non_runnable_candidate = metadata.get("status") in {"CANDIDATE", "SUSPECT"} and not bool(row[1])
            stats=metadata.setdefault("run_stats",{"runs":0,"successes":0,"verified_successes":0,"failures":0,"consecutive_failures":0,"last_verified":None})
            stats.setdefault("verified_successes",0)
            stats["runs"]=int(stats.get("runs",0))+1
            verified=bool(success and observations and all(
                isinstance(item.get("verification"),dict)
                and (item["verification"].get("result")=="CONFIRMED" or item["verification"].get("verified") is True)
                for item in observations))
            if success:
                stats["successes"]=int(stats.get("successes",0))+1
                if verified:
                    stats["verified_successes"]=int(stats.get("verified_successes",0))+1
                    stats["consecutive_failures"]=0
                    stats["last_verified"]=now
                    if not non_runnable_candidate:
                        metadata["status"]="ACTIVE"
            else:
                stats["failures"]=int(stats.get("failures",0))+1
                stats["consecutive_failures"]=int(stats.get("consecutive_failures",0))+1
                if stats["consecutive_failures"]>=2:
                    metadata["status"]="SUSPECT"
            stats["last_run_at"]=now
            stats["last_result"]="CONFIRMED" if verified else "SUCCEEDED_UNVERIFIED" if success else "NOT_CONFIRMED"
            stats["last_step_count"]=len(observations)
            db.execute("UPDATE procedures SET constraints_json=? WHERE id=?",(json.dumps(metadata),procedure_id))
    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            rows = db.execute("SELECT version FROM schema_version").fetchall() if exists else []
            if not rows:
                db.executescript(SCHEMA); db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif rows[0][0] >= 25:
                # From v25 onward the database may contain a version marker
                # without optional feature tables (or after an interrupted
                # setup). Reapply the CREATE-IF-MISSING baseline first; data
                # and already-existing legacy table shapes remain untouched.
                db.executescript(SCHEMA)
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
                db.execute("INSERT INTO projects(id,name,workspace_path,archived,created_at,updated_at) VALUES(?,?,?,?,?,?)", (imported_id, "Imported", None, 0, now, now))
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
            if rows and rows[0][0] == 22:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN task_total_plan_repair_count INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE coding_tasks ADD COLUMN task_total_recovery_model_calls INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE schema_version SET version=23")
                rows=[(23,)]
            if rows and rows[0][0] == 23:
                # Durable execution facts used when a Coding Task crosses a
                # process restart. This is separate from the planner graph
                # and from user authorization state.
                db.execute("ALTER TABLE coding_tasks ADD COLUMN resume_state_json TEXT")
                db.execute("UPDATE schema_version SET version=24")
                rows=[(24,)]
            if rows and rows[0][0] == 24:
                db.execute("ALTER TABLE coding_tasks ADD COLUMN human_recovery_epochs INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE schema_version SET version=25")
                rows=[(25,)]
            if rows and rows[0][0] == 25:
                db.execute("CREATE TABLE IF NOT EXISTS web_recipes(id TEXT PRIMARY KEY, version INTEGER NOT NULL, name TEXT NOT NULL, recipe_json TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("UPDATE schema_version SET version=26")
                rows=[(26,)]
            if rows and rows[0][0] == 26:
                db.execute("CREATE TABLE IF NOT EXISTS web_scrape_runs(id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL REFERENCES web_recipes(id) ON DELETE RESTRICT, recipe_json TEXT NOT NULL, status TEXT NOT NULL, page_cursor INTEGER NOT NULL DEFAULT 0, pages_completed INTEGER NOT NULL DEFAULT 0, record_count INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS web_scrape_rows(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES web_scrape_runs(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, page_number INTEGER NOT NULL, dedupe_key TEXT, row_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(run_id,ordinal), UNIQUE(run_id,dedupe_key))")
                db.execute("UPDATE schema_version SET version=27")
                rows=[(27,)]
            if rows and rows[0][0] == 27:
                _ensure_column(db, "web_scrape_runs", "last_page_hash TEXT")
                db.execute("UPDATE schema_version SET version=28")
                rows=[(28,)]
            if rows and rows[0][0] == 28:
                _ensure_column(db, "web_scrape_runs", "stop_reason TEXT")
                db.execute("UPDATE schema_version SET version=29")
                rows=[(29,)]
            if rows and rows[0][0] == 29:
                _ensure_column(db, "web_scrape_runs", "next_url TEXT")
                db.execute("UPDATE schema_version SET version=30")
                rows=[(30,)]
            if rows and rows[0][0] == 30:
                db.execute("CREATE TABLE IF NOT EXISTS desktop_world_states(session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS desktop_world_states_expiry ON desktop_world_states(expires_at)")
                db.execute("UPDATE schema_version SET version=31")
                rows=[(31,)]
            if rows and rows[0][0] == 31:
                # A version marker can survive while optional feature tables
                # are absent (for example in a partial v30 database). Bring
                # the scraper tables up to the current base schema before
                # applying the additive recovery column.
                db.execute("CREATE TABLE IF NOT EXISTS web_recipes(id TEXT PRIMARY KEY, version INTEGER NOT NULL, name TEXT NOT NULL, recipe_json TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS web_scrape_runs(id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL REFERENCES web_recipes(id) ON DELETE RESTRICT, recipe_json TEXT NOT NULL, status TEXT NOT NULL, page_cursor INTEGER NOT NULL DEFAULT 0, pages_completed INTEGER NOT NULL DEFAULT 0, record_count INTEGER NOT NULL DEFAULT 0, last_page_hash TEXT, stop_reason TEXT, next_url TEXT, error TEXT, recovery_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS web_scrape_rows(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES web_scrape_runs(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, page_number INTEGER NOT NULL, dedupe_key TEXT, row_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(run_id,ordinal), UNIQUE(run_id,dedupe_key))")
                run_columns = {row[1] for row in db.execute("PRAGMA table_info(web_scrape_runs)")}
                if "recovery_json" not in run_columns:
                    db.execute("ALTER TABLE web_scrape_runs ADD COLUMN recovery_json TEXT NOT NULL DEFAULT '{}'")
                db.execute("UPDATE schema_version SET version=32")
                rows=[(32,)]
            if rows and rows[0][0] == 32:
                run_columns = {row[1] for row in db.execute("PRAGMA table_info(web_scrape_runs)")}
                if "metrics_json" not in run_columns:
                    db.execute("ALTER TABLE web_scrape_runs ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'")
                db.execute("UPDATE schema_version SET version=33")
                rows=[(33,)]
            if rows and rows[0][0] == 33:
                # Project association is optional for ordinary conversations.
                # Keep every existing project assignment and all child rows;
                # only relax the nullable association on the parent table.
                # Earlier migrations may already have opened a transaction;
                # SQLite ignores foreign_keys=OFF inside one. Commit that work
                # before switching FK enforcement, then transact this rebuild.
                db.commit()
                db.execute("PRAGMA foreign_keys=OFF")
                try:
                    db.execute("BEGIN IMMEDIATE")
                    has_conversations = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()
                    if has_conversations:
                        db.execute("CREATE TABLE conversations_new(id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id), title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                        db.execute("INSERT INTO conversations_new(id,project_id,title,created_at,updated_at) SELECT id,project_id,title,created_at,updated_at FROM conversations")
                        db.execute("DROP TABLE conversations")
                        db.execute("ALTER TABLE conversations_new RENAME TO conversations")
                    else:
                        db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id), title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                    db.execute("UPDATE schema_version SET version=34")
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.execute("PRAGMA foreign_keys=ON")
                rows=[(34,)]
            if rows and rows[0][0] == 34:
                _ensure_column(db, "coding_tasks", "response_language TEXT NOT NULL DEFAULT 'ja'")
                db.execute("UPDATE schema_version SET version=35")
                rows=[(35,)]
            if rows and rows[0][0] == 35:
                for table in ('projects','conversations'):
                    _ensure_column(db, table, "pinned INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE schema_version SET version=36")
                rows=[(36,)]
            if rows and rows[0][0] == 36:
                _ensure_column(db, "coding_tasks", "console_hidden INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE schema_version SET version=37")
                rows=[(37,)]
            if rows and rows[0][0] == 37:
                # Received reasoning was added to the fresh schema without a
                # migration, leaving existing profiles unable to save answers.
                db.execute("CREATE TABLE IF NOT EXISTS task_reasoning(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, block_json TEXT NOT NULL)")
                db.execute("UPDATE schema_version SET version=38")
                rows=[(38,)]
            if not rows or rows[0][0] in (38, 39):
                db.executescript(CONVERSATION_MEMORY_SCHEMA)
                if rows and rows[0][0] == 38:
                    # Older indexes were timestamped when embedded and could
                    # push fresh turns out of the recency window after backfill.
                    db.execute("""UPDATE conversation_memory_embeddings AS e
                        SET created_at=COALESCE((SELECT a.created_at FROM messages a
                            WHERE a.conversation_id=e.conversation_id AND a.ordinal=e.turn_ordinal+1
                              AND a.role='assistant' LIMIT 1),e.created_at)""")
                    db.execute("UPDATE schema_version SET version=39")
                    rows=[(39,)]
            if rows and rows[0][0] != SCHEMA_VERSION: raise RuntimeError(f"incompatible schema version {rows[0][0]}")
            db.executescript(CUSTOM_AGENT_SCHEMA)
            db.executescript('''CREATE TABLE IF NOT EXISTS coding_task_events(
                id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,data_json TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS coding_task_events_task ON coding_task_events(task_id,created_at);''')
            db.commit()
        finally: db.close()

    def save_desktop_world_state(self, session_id: str, state: dict[str, Any], now: float, *, ttl_seconds: int = 600) -> dict[str, Any]:
        """Persist one bounded current state per target app session, never frame history."""
        serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 256 * 1024:
            raise ValueError("desktop world state exceeds 256 KiB")
        expires_at = now + max(1, min(int(ttl_seconds), 3600))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM desktop_world_states WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO desktop_world_states(session_id,state_json,updated_at,expires_at) VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at,expires_at=excluded.expires_at",
                       (session_id, serialized, now, expires_at))
            db.execute("DELETE FROM desktop_world_states WHERE session_id NOT IN (SELECT session_id FROM desktop_world_states ORDER BY updated_at DESC LIMIT 32)")
        return {"session_id": session_id, "state": state, "updated_at": now, "expires_at": expires_at}

    @staticmethod
    def _decode_desktop_world_state(row) -> dict[str, Any] | None:
        if not row:
            return None
        try:
            state = json.loads(row["state_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(state, dict):
            return None
        return {"session_id": row["session_id"], "state": state,
                "updated_at": row["updated_at"], "expires_at": row["expires_at"]}

    def load_desktop_world_state(self, session_id: str, now: float) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("DELETE FROM desktop_world_states WHERE expires_at<=?", (now,))
            row = db.execute("SELECT session_id,state_json,updated_at,expires_at FROM desktop_world_states WHERE session_id=?", (session_id,)).fetchone()
        return self._decode_desktop_world_state(row)

    def load_latest_desktop_world_state(self, now: float) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("DELETE FROM desktop_world_states WHERE expires_at<=?", (now,))
            row = db.execute("SELECT session_id,state_json,updated_at,expires_at FROM desktop_world_states ORDER BY updated_at DESC LIMIT 1").fetchone()
        return self._decode_desktop_world_state(row)

    def update_desktop_world_state_perception(self, session_id: str, observation_id: str,
                                              policy: dict[str, Any], now: float) -> dict[str, Any] | None:
        """Attach advisory APC state only to the exact persisted observation it describes."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM desktop_world_states WHERE expires_at<=?", (now,))
            row = db.execute("SELECT state_json,updated_at,expires_at FROM desktop_world_states WHERE session_id=?", (session_id,)).fetchone()
            if not row:
                return None
            try:
                state = json.loads(row["state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(state, dict) or state.get("observation_id") != observation_id:
                return None
            previous = state.get("perception_policy")
            if isinstance(previous, dict) and previous.get("controller_id") == policy.get("controller_id"):
                try:
                    if int(policy.get("revision", -1)) < int(previous.get("revision", -1)):
                        return None
                except (TypeError, ValueError):
                    return None
            state["perception_state"] = policy["state"]
            state["perception_policy"] = policy
            state["updated_at"] = now
            serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            if len(serialized.encode("utf-8")) > 256 * 1024:
                raise ValueError("desktop world state exceeds 256 KiB")
            db.execute("UPDATE desktop_world_states SET state_json=?,updated_at=? WHERE session_id=?",
                       (serialized, now, session_id))
            return {"session_id": session_id, "state": state, "updated_at": now, "expires_at": row["expires_at"]}

    def save_web_recipe(self, record: dict[str, Any]) -> dict[str, Any]:
        recipe_id = str(record.get("id") or "")
        if not recipe_id: raise ValueError("recipe id is required")
        now = __import__("time").time()
        payload = {key: value for key, value in record.items() if key not in {"id", "created_at", "updated_at", "status"}}
        with self.connect() as db:
            old = db.execute("SELECT created_at FROM web_recipes WHERE id=?", (recipe_id,)).fetchone()
            created_at = float(old[0]) if old else now
            db.execute("INSERT INTO web_recipes(id,version,name,recipe_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,name=excluded.name,recipe_json=excluded.recipe_json,status=excluded.status,updated_at=excluded.updated_at",
                       (recipe_id, int(record.get("version") or 1), str(record.get("name") or recipe_id),
                        json.dumps(payload, ensure_ascii=False), str(record.get("status") or "DRAFT"), created_at, now))
        return {**payload, "id": recipe_id, "status": str(record.get("status") or "DRAFT"),
                "created_at": created_at, "updated_at": now}

    def web_recipe(self, recipe_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM web_recipes WHERE id=?", (recipe_id,)).fetchone()
        if not row: return None
        try: payload = json.loads(row["recipe_json"])
        except (TypeError, ValueError): return None
        if not isinstance(payload, dict): return None
        return {**payload, "id": row["id"], "version": int(row["version"]), "name": row["name"],
                "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def web_recipes(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            ids = [str(row[0]) for row in db.execute("SELECT id FROM web_recipes ORDER BY updated_at DESC,id")]
        return [value for recipe_id in ids if (value := self.web_recipe(recipe_id)) is not None]

    def delete_web_recipe(self, recipe_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM web_recipes WHERE id=?", (recipe_id,))
            return cursor.rowcount > 0

    def set_web_recipe_status(self, recipe_id: str, status: str, validation: dict[str, Any] | None = None) -> dict[str, Any] | None:
        allowed = {"DRAFT", "VALIDATED"}
        if status not in allowed: raise ValueError("invalid Recipe status")
        now = __import__("time").time()
        with self.connect() as db:
            row = db.execute("SELECT recipe_json FROM web_recipes WHERE id=?", (recipe_id,)).fetchone()
            if not row: return None
            try: payload = json.loads(row[0])
            except (TypeError, ValueError): return None
            if validation is not None:
                # Store only bounded structural validation facts, never page contents.
                payload["validation"] = {
                    "record_count": max(0, min(int(validation.get("record_count") or 0), 100_000)),
                    "field_match_counts": {str(key)[:120]: max(0, min(int(value or 0), 100_000))
                                           for key, value in list((validation.get("field_match_counts") or {}).items())[:50]},
                    "validated_at": now,
                }
            db.execute("UPDATE web_recipes SET recipe_json=?,status=?,updated_at=? WHERE id=?",
                       (json.dumps(payload, ensure_ascii=False), status, now, recipe_id))
        return self.web_recipe(recipe_id)

    def create_web_scrape_run(self, run_id: str, recipe: dict[str, Any]) -> dict[str, Any]:
        now = __import__("time").time()
        with self.connect() as db:
            db.execute("INSERT INTO web_scrape_runs(id,recipe_id,recipe_json,status,created_at,updated_at) VALUES(?,?,?,'QUEUED',?,?)",
                       (run_id, str(recipe["id"]), json.dumps(recipe, ensure_ascii=False), now, now))
        return self.web_scrape_run(run_id)

    def web_scrape_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM web_scrape_runs WHERE id=?", (run_id,)).fetchone()
        if not row: return None
        value = dict(row)
        try: value["recipe"] = json.loads(value.pop("recipe_json"))
        except (TypeError, ValueError): return None
        try: value["recovery"] = json.loads(value.pop("recovery_json", "{}"))
        except (TypeError, ValueError): value["recovery"] = {}
        try:
            metrics = json.loads(value.pop("metrics_json", "{}"))
        except (TypeError, ValueError):
            metrics = {}
        if not isinstance(metrics, dict):
            metrics = {}
        def bounded_counter(name: str) -> int:
            raw = metrics.get(name, 0)
            return max(0, min(raw, 100_000)) if isinstance(raw, int) and not isinstance(raw, bool) else 0

        metrics = {name: bounded_counter(name) for name in (
            "pages_visited", "records_discovered", "duplicates_removed",
            "dom_extractions", "network_extractions", "detail_pages_visited",
            "detail_extractions", "selector_recoveries",
        )}
        now = __import__("time").time()
        elapsed_end = float(value.get("updated_at") or now)
        if value.get("status") in {"QUEUED", "RUNNING", "PAUSE_REQUESTED", "STOP_REQUESTED"}:
            elapsed_end = now
        elapsed_ms = max(0.0, min((elapsed_end - float(value.get("created_at") or elapsed_end)) * 1000.0, 86_400_000.0))
        records_written = max(0, min(int(value.get("record_count") or 0), 100_000))
        metrics["records_written"] = records_written
        metrics["elapsed_ms"] = round(elapsed_ms, 3)
        metrics["records_per_second"] = round(records_written / (elapsed_ms / 1000.0), 3) if elapsed_ms > 0 else 0.0
        value["metrics"] = metrics
        return value

    def add_web_scrape_metrics(self, run_id: str, increments: dict[str, int]) -> dict[str, Any] | None:
        """Update bounded per-run counters, never append a high-frequency event row."""
        allowed = {"pages_visited", "records_discovered", "duplicates_removed",
                   "dom_extractions", "network_extractions", "detail_pages_visited",
                   "detail_extractions", "selector_recoveries"}
        clean: dict[str, int] = {}
        for name, amount in increments.items():
            if name not in allowed or not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError("invalid scrape metric delta")
            clean[name] = min(amount, 100_000)
        if not clean:
            return self.web_scrape_run(run_id)
        now = __import__("time").time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metrics_json FROM web_scrape_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                db.rollback()
                return None
            try:
                metrics = json.loads(row[0] or "{}")
            except (TypeError, ValueError):
                metrics = {}
            if not isinstance(metrics, dict):
                metrics = {}
            for name, amount in clean.items():
                previous = metrics.get(name, 0)
                previous = previous if isinstance(previous, int) and not isinstance(previous, bool) else 0
                metrics[name] = min(max(0, previous) + amount, 100_000)
            serialized = json.dumps(metrics, separators=(",", ":"))
            db.execute("UPDATE web_scrape_runs SET metrics_json=?,updated_at=? WHERE id=?",
                       (serialized, now, run_id))
        return self.web_scrape_run(run_id)

    def set_web_scrape_recovery(self, run_id: str, recovery: dict[str, Any]) -> dict[str, Any] | None:
        now = __import__("time").time()
        with self.connect() as db:
            row = db.execute(
                "SELECT recipe_json,metrics_json FROM web_scrape_runs WHERE id=? AND status='RUNNING'", (run_id,)
            ).fetchone()
            if row is None:
                return self.web_scrape_run(run_id)
            try:
                recipe = json.loads(row[0])
                original_updated_at = float(recipe.get("updated_at") or 0)
            except (TypeError, ValueError, AttributeError):
                original_updated_at = 0
            payload = {**recovery, "recipe_updated_at": original_updated_at}
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(serialized.encode("utf-8")) > 16 * 1024:
                raise ValueError("scrape recovery evidence exceeds 16 KiB")
            try:
                metrics = json.loads(row[1] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metrics = {}
            if not isinstance(metrics, dict):
                metrics = {}
            previous = metrics.get("selector_recoveries", 0)
            previous = previous if isinstance(previous, int) and not isinstance(previous, bool) else 0
            metrics["selector_recoveries"] = min(max(0, previous) + 1, 100_000)
            db.execute(
                "UPDATE web_scrape_runs SET status='FAILED',error='RECIPE_SELECTOR_RECOVERY_REQUIRED',stop_reason='SELECTOR_RECOVERY',recovery_json=?,metrics_json=?,updated_at=? WHERE id=? AND status='RUNNING'",
                (serialized, json.dumps(metrics, separators=(",", ":")), now, run_id),
            )
        return self.web_scrape_run(run_id)

    def resume_web_scrape_after_selector_review(self, run_id: str, recipe: dict[str, Any]) -> dict[str, Any] | None:
        now = __import__("time").time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT recipe_id,status,stop_reason,recovery_json,recipe_json FROM web_scrape_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if not run or run[1] != "FAILED" or run[2] != "SELECTOR_RECOVERY":
                db.rollback()
                return None
            if str(recipe.get("id") or "") != str(run[0]) or recipe.get("status") != "VALIDATED":
                db.rollback()
                raise ValueError("RECIPE_RECOVERY_REVALIDATION_REQUIRED")
            try:
                previous_recipe = json.loads(run[4])
                previous_updated_at = float(previous_recipe.get("updated_at") or 0)
                reviewed_updated_at = float(recipe.get("updated_at") or 0)
            except (TypeError, ValueError, AttributeError):
                db.rollback()
                raise ValueError("RECIPE_RECOVERY_STATE_INVALID")
            if reviewed_updated_at <= previous_updated_at:
                db.rollback()
                raise ValueError("RECIPE_RECOVERY_REVALIDATION_REQUIRED")
            try:
                recovery = json.loads(run[3] or "{}")
                page_number = int(recovery.get("page_number"))
            except (TypeError, ValueError, AttributeError):
                db.rollback()
                raise ValueError("RECIPE_RECOVERY_STATE_INVALID")
            if page_number < 1:
                db.rollback()
                raise ValueError("RECIPE_RECOVERY_STATE_INVALID")
            cursor = db.execute(
                "UPDATE web_scrape_runs SET recipe_json=?,status='QUEUED',page_cursor=?,error=NULL,stop_reason=NULL,recovery_json='{}',updated_at=? WHERE id=? AND status='FAILED' AND stop_reason='SELECTOR_RECOVERY'",
                (json.dumps(recipe, ensure_ascii=False), page_number - 1, now, run_id),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return None
        return self.web_scrape_run(run_id)

    def update_web_scrape_run(self, run_id: str, status: str, *, error: str | None = None,
                              stop_reason: str | None = None) -> dict[str, Any] | None:
        now = __import__("time").time()
        with self.connect() as db:
            db.execute("UPDATE web_scrape_runs SET status=?,error=?,stop_reason=?,updated_at=? WHERE id=?",
                       (status, error[:500] if error else None, stop_reason, now, run_id))
        return self.web_scrape_run(run_id)

    def checkpoint_web_scrape_page(self, run_id: str, page_number: int, rows: list[tuple[str | None, dict[str, Any]]], page_hash: str, next_url: str | None = None, *, discovered_count: int | None = None) -> dict[str, Any] | None:
        now = __import__("time").time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status FROM web_scrape_runs WHERE id=?", (run_id,)).fetchone()
            if not run: return None
            if run[0] != "RUNNING": return self.web_scrape_run(run_id)
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM web_scrape_rows WHERE run_id=?", (run_id,)).fetchone()[0]
            inserted_count = 0
            for dedupe_key, row in rows:
                cursor = db.execute("INSERT OR IGNORE INTO web_scrape_rows(run_id,ordinal,page_number,dedupe_key,row_json,created_at) VALUES(?,?,?,?,?,?)",
                                    (run_id, ordinal, page_number, dedupe_key, json.dumps(row, ensure_ascii=False), now))
                if cursor.rowcount:
                    ordinal += 1
                    inserted_count += 1
            try:
                metrics = json.loads(db.execute("SELECT metrics_json FROM web_scrape_runs WHERE id=?", (run_id,)).fetchone()[0] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metrics = {}
            if not isinstance(metrics, dict):
                metrics = {}
            candidate_count = len(rows) if discovered_count is None else max(0, min(int(discovered_count), 100_000))
            duplicates = max(0, candidate_count - inserted_count)
            previous_duplicates = metrics.get("duplicates_removed", 0)
            previous_duplicates = previous_duplicates if isinstance(previous_duplicates, int) and not isinstance(previous_duplicates, bool) else 0
            metrics["duplicates_removed"] = min(max(0, previous_duplicates) + duplicates, 100_000)
            db.execute("UPDATE web_scrape_runs SET page_cursor=?,pages_completed=pages_completed+1,record_count=(SELECT COUNT(*) FROM web_scrape_rows WHERE run_id=?),last_page_hash=?,next_url=?,metrics_json=?,updated_at=? WHERE id=?",
                       (page_number, run_id, page_hash, next_url, json.dumps(metrics, separators=(",", ":")), now, run_id))
        return self.web_scrape_run(run_id)

    def active_web_scrape_runs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            ids = [str(row[0]) for row in db.execute("SELECT id FROM web_scrape_runs WHERE status IN ('QUEUED','RUNNING','PAUSE_REQUESTED','STOP_REQUESTED')")]
        return [run for run_id in ids if (run := self.web_scrape_run(run_id)) is not None]

    def interrupt_active_web_scrape_runs(self) -> int:
        now = __import__("time").time()
        with self.connect() as db:
            cursor = db.execute("UPDATE web_scrape_runs SET status='INTERRUPTED',error='PROCESS_RESTARTED',updated_at=? WHERE status IN ('QUEUED','RUNNING','PAUSE_REQUESTED','STOP_REQUESTED')", (now,))
            return cursor.rowcount

    def scrape_table_page(self, run_id: str, offset: int = 0, limit: int = 50, search: str = "") -> dict:
        with self.connect() as db:
            records = db.execute("SELECT id,row_json FROM web_scrape_rows WHERE run_id=? ORDER BY ordinal", (run_id,))
            columns, page, total = {}, [], 0
            for record in records:
                row = json.loads(record["row_json"])
                columns.update(dict.fromkeys(row))
                if search and search.casefold() not in json.dumps(row, ensure_ascii=False).casefold(): continue
                if offset <= total < offset + limit: page.append({"id": record["id"], "values": row})
                total += 1
        return {"columns": list(columns), "rows": page, "total": total, "offset": offset}

    def edit_scrape_table(self, run_id: str, operation: str, column: str = "", target: str = "", value: str = "", row_id: int | None = None) -> None:
        from .scrape_table import transform_rows
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status FROM web_scrape_runs WHERE id=?", (run_id,)).fetchone()
            if not run: raise KeyError("SCRAPE_RUN_NOT_FOUND")
            if run["status"] not in {"COMPLETED", "STOPPED", "FAILED", "INTERRUPTED"}:
                raise ValueError("STOP_RUN_BEFORE_EDITING")
            if run["status"] in {"FAILED", "INTERRUPTED"}:
                # Editing finalizes partial data: it must not later mix with resumed raw extraction.
                db.execute("UPDATE web_scrape_runs SET status='STOPPED',stop_reason='TABLE_EDIT_FINALIZED' WHERE id=?", (run_id,))
            if operation == "cell":
                record = db.execute("SELECT row_json FROM web_scrape_rows WHERE run_id=? AND id=?", (run_id, row_id)).fetchone()
                if not record: raise ValueError("ROW_NOT_FOUND")
                row = json.loads(record[0])
                if column not in row: raise ValueError("COLUMN_NOT_FOUND")
                row[column] = value
                db.execute("UPDATE web_scrape_rows SET row_json=? WHERE run_id=? AND id=?", (json.dumps(row, ensure_ascii=False), run_id, row_id))
            else:
                records = db.execute("SELECT id,row_json,page_number FROM web_scrape_rows WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
                originals = [json.loads(r["row_json"]) for r in records]
                rows = transform_rows(originals, operation, column, target, value)
                if operation in {"deduplicate", "drop_empty"}:
                    kept = iter(rows)
                    wanted = next(kept, None)
                    for record, original in zip(records, originals):
                        if wanted is not None and original == wanted:
                            wanted = next(kept, None)
                        else:
                            db.execute("DELETE FROM web_scrape_rows WHERE run_id=? AND id=?", (run_id, record["id"]))
                else:
                    for record, row in zip(records, rows):
                        db.execute("UPDATE web_scrape_rows SET row_json=? WHERE run_id=? AND id=?", (json.dumps(row, ensure_ascii=False), run_id, record["id"]))
            db.execute("UPDATE web_scrape_runs SET record_count=(SELECT COUNT(*) FROM web_scrape_rows WHERE run_id=?),updated_at=? WHERE id=?", (run_id, __import__("time").time(), run_id))

    def stream_web_scrape_rows(self, run_id: str, *, batch_size: int = 256):
        with self.connect() as db:
            cursor = db.execute("SELECT row_json FROM web_scrape_rows WHERE run_id=? ORDER BY ordinal", (run_id,))
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    yield json.loads(row[0])
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
            from .thinking import reasoning_block
            block = reasoning_block(task.model_calls)
            if block:
                db.execute('INSERT OR REPLACE INTO task_reasoning VALUES(?,?)',
                           (task.id, json.dumps(block, ensure_ascii=False)))
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
            db.execute("INSERT INTO projects(id,name,workspace_path,archived,created_at,updated_at) VALUES(?,?,?,?,?,?)", (self.IMPORTED_PROJECT_ID,"Imported",None,0,now,now))
            return self.IMPORTED_PROJECT_ID
    def create_coding_task(self, task_id: str, conversation_id: str, goal: str, status: str, activity: str, plan: dict[str, Any] | None, now: float, *, source_message_id: str | None = None, initial_requirements: dict[str, Any] | None = None) -> dict[str, Any]:
        from .response_language import response_language
        existing_id: str | None = None
        with self.connect() as db:
            if source_message_id:
                existing=db.execute("SELECT id FROM coding_tasks WHERE source_message_id=?", (source_message_id,)).fetchone()
                if existing:
                    existing_id=str(existing[0])
            if existing_id is None:
                try:
                    serialized_plan=json.dumps(plan) if plan else None
                    db.execute("INSERT INTO coding_tasks(id,conversation_id,source_message_id,response_language,original_goal,status,activity,plan_json,active_plan_json,requirements_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(task_id,conversation_id,source_message_id,response_language(),goal,status,activity,serialized_plan,serialized_plan,json.dumps(initial_requirements or {}),now,now))
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
            value=dict(row); legacy_plan=value.pop("plan_json") or "null"; active_plan=value.pop("active_plan_json") or legacy_plan; value["plan"]=json.loads(active_plan); value["active_plan"]=value["plan"]; value["pending_plan"]=json.loads(value.pop("pending_plan_json") or "null"); value["subtask_progress"]=json.loads(value.pop("subtask_progress_json") or "null"); value["approved_scopes"]=json.loads(value.pop("approved_scopes_json") or "[]"); value["pending_authorization"]=json.loads(value.pop("pending_authorization_json") or "null"); value["final_report"]=json.loads(value.pop("final_report_json") or "null"); value["batch_handoff"]=json.loads(value.pop("batch_handoff_json") or "null"); value["resume_state"]=json.loads(value.pop("resume_state_json") or "null"); value["requirements"]=json.loads(value.pop("requirements_json") or "{}"); value["required_mcp"]=json.loads(value.pop("required_mcp_json") or "[]"); value["mcp_evidence"]=json.loads(value.pop("mcp_evidence_json") or "[]")
            value['console_hidden']=bool(value.get('console_hidden'))
            invocation=db.execute("SELECT data_json,created_at FROM coding_task_events WHERE task_id=? AND kind='MODEL_INVOCATION' ORDER BY created_at DESC,rowid DESC LIMIT 1",(task_id,)).fetchone()
            if invocation: value['model_invocation']={**json.loads(invocation[0]),'created_at':invocation[1]}
            progress=db.execute("SELECT data_json,created_at FROM coding_task_events WHERE task_id=? AND kind='MODEL_PROGRESS' ORDER BY created_at DESC,rowid DESC LIMIT 1",(task_id,)).fetchone()
            if progress:
                latest={**json.loads(progress[0]),'created_at':progress[1]}
                current=value.get('model_invocation',{})
                # Never label a new fallback with the previous model's counters.
                if (latest['created_at'] >= current.get('created_at',0)
                        and (latest.get('invocation_id') == current.get('invocation_id')
                             or latest.get('state') in {'slot_waiting','slot_acquired'})):
                    value['model_progress']=latest
            return value
    def coding_tasks(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.connect() as db: ids=[x[0] for x in db.execute("SELECT id FROM coding_tasks WHERE conversation_id=? ORDER BY updated_at DESC",(conversation_id,))]
        return [x for task_id in ids if (x:=self.coding_task(task_id))]
    def record_coding_event(self, task_id: str, kind: str, data: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute('INSERT INTO coding_task_events VALUES(?,?,?,?,?)',
                       (str(__import__('uuid').uuid4()),task_id,kind,json.dumps(data,ensure_ascii=False),__import__('time').time()))
    def coding_task_events(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows=[dict(row) for row in db.execute('SELECT * FROM coding_task_events WHERE task_id=? ORDER BY created_at,rowid',(task_id,))]
        for row in rows:
            row['data']=json.loads(row.pop('data_json'))
        return rows
    def set_coding_console(self, task_id: str, hidden: bool, snapshot: dict[str, Any], *, restore_archived: bool=False) -> dict[str, Any] | None:
        """Persist display state and its checkpoint without changing execution."""
        now=__import__('time').time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM coding_tasks WHERE id=?',(task_id,)).fetchone()
            if row is None:return None
            db.execute('UPDATE coding_tasks SET console_hidden=?,archived=?,updated_at=? WHERE id=?',
                       (int(hidden),0 if restore_archived else row['archived'],now,task_id))
            plan=json.loads(row['active_plan_json'] or row['plan_json'] or '{}') or {}
            data={**snapshot,'status':row['status'],'current_phase_id':row['current_phase_id'],
                  'plan_revision':row['plan_revision'],'was_archived':bool(row['archived']),
                  'original_goal':row['original_goal'],'retry_count':row['retry_count'],
                  'recovery_action':row['recovery_action'],'recovery_reason':row['recovery_reason'],
                  'phases':[{'id':phase['id'],'goal':phase.get('goal'),'status':phase.get('status')}
                            for phase in plan.get('phases') or []],
                  'report_ids':[report[0] for report in db.execute('SELECT id FROM coding_phase_reports WHERE coding_task_id=? ORDER BY created_at,rowid',(task_id,))]}
            db.execute('INSERT INTO coding_task_events VALUES(?,?,?,?,?)',
                       (str(__import__('uuid').uuid4()),task_id,'CONSOLE_HIDDEN' if hidden else 'CONSOLE_RESTORED',json.dumps(data,ensure_ascii=False),now))
        return self.coding_task(task_id)
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
        allowed={"status","activity","plan","active_plan","plan_revision","pending_plan","subtask_progress","current_phase_id","retry_count","max_retries","replan_count","replan_count_in_epoch","recovery_epoch","human_recovery_epochs","task_total_plan_repair_count","task_total_recovery_model_calls","approved_scopes","pending_authorization","pending_user_confirmation","pause_requested","archived","queue_order","recovery_action","recovery_reason","final_report","final_report_status","execution_mode","task_profile","requirements","required_mcp","mcp_evidence","batch_cursor","batch_handoff","resume_state"}
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
        if "resume_state" in values: values["resume_state_json"]=json.dumps(values.pop("resume_state"), ensure_ascii=False)
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

    def save_coding_completion(self, task_id: str, revision: int, text: str) -> None:
        """Publish the terminal state and its chat report in one transaction."""
        now = __import__('time').time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM coding_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row['plan_revision'] != revision:
                raise RuntimeError('COMPLETION_PLAN_CHANGED')
            if row['status'] == 'COMPLETED':
                return
            if row['pause_requested'] or row['archived']:
                raise RuntimeError('COMPLETION_INTERRUPTED')
            if (json.loads(row['pending_authorization_json'] or 'null')
                    or row['pending_user_confirmation'] or json.loads(row['pending_plan_json'] or 'null')):
                raise RuntimeError('COMPLETION_PENDING_APPROVAL')
            steering_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='coding_steering'").fetchone()
            if steering_exists and conn.execute("SELECT 1 FROM coding_steering WHERE task_id=? AND status IN ('QUEUED','APPLYING','NEEDS_REVIEW')", (task_id,)).fetchone():
                raise RuntimeError('COMPLETION_PENDING_INSTRUCTIONS')
            conn.execute("UPDATE coding_tasks SET status='COMPLETED',activity='NONE',queue_order=NULL,current_phase_id=NULL,final_report_json=?,final_report_status='PASS',recovery_action='NONE',recovery_reason='NONE',updated_at=? WHERE id=?",
                         (json.dumps({'text': text}, ensure_ascii=False), now, task_id))
            ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM messages WHERE conversation_id=?", (row['conversation_id'],)).fetchone()[0]
            conn.execute("INSERT INTO messages(id,conversation_id,task_id,role,content,ordinal,created_at) VALUES(?,?,?,'assistant',?,?,?)",
                         (str(__import__('uuid').uuid4()), row['conversation_id'], task_id, text, ordinal, now))
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, row['conversation_id']))
    def approve_coding_plan(self, task_id: str, revision: int) -> dict[str, Any] | None:
        """Approve the reviewed revision and queue it under one write lock."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM coding_tasks WHERE id=?', (task_id,)).fetchone()
            if (row is None or row['plan_revision'] != revision or row['status'] != 'WAITING_FOR_PLAN_APPROVAL'
                    or row['archived'] or json.loads(row['pending_authorization_json'] or 'null')):
                raise RuntimeError('PLAN_CHANGED_REFRESH_REQUIRED')
            plan = json.loads(row['active_plan_json'] or row['plan_json'] or '{}')
            requirements = {**json.loads(row['requirements_json'] or '{}'), 'planning_only': False}
            scopes = json.loads(row['approved_scopes_json'] or '[]')
            scopes.append({'source': 'explicit_plan_approval',
                           'requested_scope': (plan.get('scope') or {}).get('allowed') or []})
            order = db.execute("SELECT COALESCE(MAX(queue_order),0)+1 FROM coding_tasks WHERE status='QUEUED'").fetchone()[0]
            db.execute("UPDATE coding_tasks SET requirements_json=?,approved_scopes_json=?,status='QUEUED',activity='NONE',pause_requested=0,queue_order=?,updated_at=? WHERE id=?",
                       (json.dumps(requirements), json.dumps(scopes), order, __import__('time').time(), task_id))
        return self.coding_task(task_id)

    def enqueue_coding_task(self, task_id: str, *, clear_pause: bool = True) -> dict[str, Any] | None:
        """Queue an explicit resume, or preserve pauses during automatic continuation."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,queue_order,pause_requested,archived FROM coding_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if row['archived']:
                return self.coding_task(task_id)
            if row['pause_requested'] and not clear_pause:
                return self.coding_task(task_id)
            if row['status'] == 'QUEUED' and row['queue_order'] is not None and not row['pause_requested']:
                return self.coding_task(task_id)
            next_order=db.execute("SELECT COALESCE(MAX(queue_order),0)+1 FROM coding_tasks WHERE status='QUEUED'").fetchone()[0]
            db.execute("UPDATE coding_tasks SET status='QUEUED',activity='NONE',pause_requested=0,queue_order=?,updated_at=? WHERE id=?",(next_order,__import__('time').time(),task_id))
        return self.coding_task(task_id)
    def next_queued_coding_task(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row=db.execute("SELECT id FROM coding_tasks WHERE status='QUEUED' AND archived=0 AND pause_requested=0 ORDER BY queue_order,id LIMIT 1").fetchone()
        return self.coding_task(row[0]) if row else None

    def persist_coding_stop(self, task_id: str, snapshot: dict[str, Any], report_count: int) -> str:
        """Consolidate one stop episode as its final execution evidence arrives."""
        now=__import__('time').time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT conversation_id,resume_state_json,recovery_reason FROM coding_tasks WHERE id=?',(task_id,)).fetchone()
            if row is None or row['recovery_reason']=='MESSAGE_REPLACED':return ''
            state=json.loads(row['resume_state_json'] or '{}')
            previous=state.get('stop_progress') or {}
            same_stop=all(previous.get(key)==snapshot.get(key) for key in ('reason','intended','recovery_epoch'))
            if previous.get('signature')==snapshot.get('signature'):
                if report_count>int(previous.get('report_count') or 0):
                    state['stop_progress']={**previous,'report_count':report_count}
                    db.execute('UPDATE coding_tasks SET resume_state_json=? WHERE id=?',
                               (json.dumps(state,ensure_ascii=False),task_id))
                return str(previous.get('text') or '')
            previous_order=(int(previous.get('plan_revision') or 0),int(previous.get('report_count') or 0))
            incoming_order=(int(snapshot.get('plan_revision') or 0),report_count)
            if same_stop and previous_order>incoming_order:
                return str(previous.get('text') or '')
            identifier=previous.get('message_id') if same_stop else None
            updated=db.execute("UPDATE messages SET content=? WHERE id=? AND task_id=? AND role='assistant'",
                               (snapshot['text'],identifier,task_id)).rowcount if identifier else 0
            if not updated:
                identifier=str(__import__('uuid').uuid4())
                ordinal=db.execute('SELECT COALESCE(MAX(ordinal),-1)+1 FROM messages WHERE conversation_id=?',(row['conversation_id'],)).fetchone()[0]
                db.execute("INSERT INTO messages(id,conversation_id,task_id,role,content,ordinal,created_at) VALUES(?,?,?,'assistant',?,?,?)",
                           (identifier,row['conversation_id'],task_id,snapshot['text'],ordinal,now))
            state['stop_progress']={**snapshot,'message_id':identifier,'report_count':report_count}
            db.execute('UPDATE coding_tasks SET resume_state_json=?,updated_at=? WHERE id=?',
                       (json.dumps(state,ensure_ascii=False),now,task_id))
            db.execute('UPDATE conversations SET updated_at=? WHERE id=?',(now,row['conversation_id']))
        return snapshot['text']
    def dequeue_coding_task(self, task_id: str) -> dict[str, Any] | None:
        return self.update_coding_task(task_id,status="RESUMABLE",activity="NONE",queue_order=None)
    def coding_task_queue_position(self, task_id: str) -> int | None:
        task=self.coding_task(task_id)
        if not task or task.get("status") != "QUEUED" or task.get("queue_order") is None: return None
        with self.connect() as db:
            row=db.execute("SELECT COUNT(*) FROM coding_tasks WHERE status='QUEUED' AND archived=0 AND queue_order<?",(task["queue_order"],)).fetchone()
        return int(row[0])+1
    def accept_pending_plan(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM coding_tasks WHERE id=?', (task_id,)).fetchone()
            plan = json.loads(row['pending_plan_json'] or 'null') if row else None
            if not plan:
                return self.coding_task(task_id)
            if row['archived']:
                return self.coding_task(task_id)
            scopes = json.loads(row['approved_scopes_json'] or '[]')
            authorization = json.loads(row['pending_authorization_json'] or 'null') or {}
            # Approval authorizes exact reviewed manifest paths, not prose.
            manifest = plan.get('file_manifest') if isinstance(plan.get('file_manifest'), list) else []
            allowed = [entry['path'] for entry in manifest if isinstance(entry, dict)
                       and isinstance(entry.get('path'), str) and entry['path'].strip()]
            if allowed:
                approved = {'source': 'explicit_plan_approval', 'requested_scope': allowed,
                            'plan_revision': row['plan_revision']}
                if approved not in scopes: scopes.append(approved)
            serialized = json.dumps(plan)
            conn.execute("UPDATE coding_tasks SET plan_json=?,active_plan_json=?,pending_plan_json='null',pending_authorization_json='null',pending_user_confirmation=0,approved_scopes_json=?,updated_at=? WHERE id=?",
                         (serialized, serialized, json.dumps(scopes), __import__('time').time(), task_id))
            if authorization.get('steering_ids'):
                conn.executemany("UPDATE coding_steering SET status='APPLIED',applied_revision=? WHERE id=? AND task_id=? AND received_revision=? AND status IN ('APPLYING','NEEDS_REVIEW')",
                    [(row['plan_revision'], identifier, task_id, authorization.get('steering_revision'))
                     for identifier in authorization['steering_ids'] if isinstance(identifier, str)])
        return self.coding_task(task_id)
    def projects(self, include_archived: bool=False) -> list[dict[str, Any]]:
        sql="SELECT * FROM projects" + ("" if include_archived else " WHERE archived=0") + " ORDER BY pinned DESC, updated_at DESC, created_at DESC"
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
        with self.connect() as db: db.execute("INSERT INTO projects(id,name,workspace_path,archived,created_at,updated_at) VALUES(?,?,?,?,?,?)",(project_id,name[:120],workspace_path,0,now,now))
        return self.project(project_id) or {}
    def update_project(self, project_id: str, name: str | None, workspace_path: str | None, archived: bool | None, now: float) -> dict[str, Any] | None:
        current=self.project(project_id)
        if not current:return None
        values=(name.strip()[:120] if name is not None else current["name"], workspace_path if workspace_path is not None else current["workspace_path"], int(archived) if archived is not None else current["archived"],now,project_id)
        with self.connect() as db: db.execute("UPDATE projects SET name=?,workspace_path=?,archived=?,updated_at=? WHERE id=?",values)
        return self.project(project_id)
    def pin_project(self, project_id: str, pinned: bool) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("UPDATE projects SET pinned=? WHERE id=?",(int(pinned),project_id))
            row=db.execute("SELECT * FROM projects WHERE id=?",(project_id,)).fetchone()
        return dict(row) if row else None
    def pin_conversation(self, conversation_id: str, pinned: bool, project_id: str | None) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("UPDATE conversations SET pinned=? WHERE id=? AND project_id IS ?",(int(pinned),conversation_id,project_id))
            row=db.execute("SELECT * FROM conversations WHERE id=? AND project_id IS ?",(conversation_id,project_id)).fetchone()
        return dict(row) if row else None
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
        with self.connect() as db: db.execute("INSERT INTO conversations(id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?)", (conversation_id, project_id, title[:120] or "New conversation", now, now))
        return conversation_id
    def add_message(self, conversation_id: str, role: str, content: str, now: float, message_id: str, task_id: str | None = None, blocks: list[dict[str, Any]] | None = None) -> None:
        if role=="assistant":
            from .concept_diagrams import extract_generated_diagrams
            content,diagrams=extract_generated_diagrams(content)
            if diagrams:
                blocks=list(blocks or [])
                for diagram in diagrams:
                    if diagram not in blocks: blocks.append(diagram)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if role == "assistant" and task_id and db.execute("SELECT 1 FROM coding_tasks WHERE id=? AND archived=1 AND recovery_reason='MESSAGE_REPLACED'", (task_id,)).fetchone():
                return
            if role == 'assistant' and task_id:
                log = db.execute('SELECT block_json FROM task_reasoning WHERE task_id=?', (task_id,)).fetchone()
                if log:
                    blocks = [block for block in (blocks or []) if block.get('type') != 'reasoning_log']
                    blocks.append(json.loads(log[0]))
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            db.execute("INSERT INTO messages(id,conversation_id,task_id,role,content,ordinal,created_at,blocks_json) VALUES(?,?,?,?,?,?,?,?)", (message_id, conversation_id, task_id, role, content, ordinal, now, json.dumps(blocks, ensure_ascii=False) if blocks else None))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?",(now,conversation_id))
    def rewind_conversation(self, conversation_id: str, message_id: str, expected_last_message_id: str,
                            project_id: str | None, now: float) -> dict[str, Any]:
        """Drop one of the latest two user turns and its suffix, atomically.

        The caller then submits a fresh request through the normal chat route.
        Task audit records and external side effects are deliberately retained.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conversation = conn.execute("SELECT project_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not conversation:
                raise ValueError("CONVERSATION_NOT_FOUND")
            if conversation["project_id"] != project_id:
                raise ValueError("CONVERSATION_SCOPE_MISMATCH")
            messages = list(conn.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY ordinal", (conversation_id,)))
            if not messages or messages[-1]["id"] != expected_last_message_id:
                raise ValueError("CONVERSATION_CHANGED")
            editable = [row for row in messages if row["role"] == "user"][-2:]
            target = next((row for row in editable if row["id"] == message_id), None)
            if target is None:
                raise ValueError("MESSAGE_NOT_EDITABLE")
            active = conn.execute("SELECT 1 FROM tasks WHERE conversation_id=? AND state NOT IN ('completed','failed','denied','cancelled') LIMIT 1", (conversation_id,)).fetchone()
            coding_active = conn.execute("SELECT 1 FROM coding_tasks WHERE conversation_id=? AND status IN ('QUEUED','PLANNING','RUNNING','FINAL_REPORTING','QWEN_REPLANNING') LIMIT 1", (conversation_id,)).fetchone()
            if active or coding_active:
                raise ValueError("CONVERSATION_BUSY")
            # Medication side effects survive transcript rewinds. Require an explicit
            # correction instead of treating an edited report as a second ingestion.
            if _table_exists(conn, 'medication_chat_turns'):
                removed_users = [row for row in messages if row['role'] == 'user' and row['ordinal'] >= target['ordinal']]
                if any(conn.execute('SELECT 1 FROM medication_chat_turns WHERE source_id=?',
                                    (conversation_id + ':' + row['id'],)).fetchone() for row in removed_users):
                    conn.execute('INSERT OR REPLACE INTO medication_chat_states VALUES(?,?,?)',
                                 (conversation_id, json.dumps({'stage':'rewound'}), now + 1800))
            # Prevent superseded coding tasks from being resumed or polled as
            # the current task, without deleting their execution audit trail.
            conn.execute("UPDATE coding_tasks SET archived=1,pause_requested=1,queue_order=NULL,recovery_reason='MESSAGE_REPLACED',updated_at=? WHERE conversation_id=? AND (updated_at>=? OR source_message_id IN (SELECT id FROM messages WHERE conversation_id=? AND ordinal>=?) OR id IN (SELECT task_id FROM messages WHERE conversation_id=? AND ordinal>=?))",
                         (now, conversation_id, target["created_at"], conversation_id, target["ordinal"], conversation_id, target["ordinal"]))
            conn.execute("DELETE FROM conversation_memory_embeddings WHERE conversation_id=?", (conversation_id,))
            conn.execute("DELETE FROM conversation_project_contexts WHERE conversation_id=?", (conversation_id,))
            conn.execute("DELETE FROM application_settings WHERE key=?", ("conversation_project_context:" + conversation_id,))
            conn.execute("UPDATE interactive_planning_sessions SET status='SUPERSEDED',updated_at=? WHERE conversation_id=? AND status='ACTIVE'", (now, conversation_id))
            conn.execute("DELETE FROM custom_agent_builders WHERE conversation_id=?", (conversation_id,))
            conn.execute("DELETE FROM custom_agent_builder_turns WHERE conversation_id=?", (conversation_id,))
            conn.execute("DELETE FROM messages WHERE conversation_id=? AND ordinal>=?", (conversation_id, target["ordinal"]))
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        return self.conversation(conversation_id) or {}

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
            agent=db.execute("SELECT definition_json FROM custom_agent_conversations WHERE conversation_id=?",(conversation_id,)).fetchone()
            agent_value=json.loads(agent[0]) if agent else None
            return {**dict(row), "messages": messages, "custom_agent": ({"id":agent_value["id"],"name":agent_value["name"],"icon":agent_value["icon"],"revision":agent_value["revision"]} if agent_value else None)}
    def conversations(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db: return [dict(x) for x in db.execute("SELECT * FROM conversations WHERE project_id=? ORDER BY pinned DESC, updated_at DESC, created_at DESC",(project_id,))]
    def projectless_conversations(self) -> list[dict[str, Any]]:
        with self.connect() as db: return [dict(x) for x in db.execute("SELECT * FROM conversations WHERE project_id IS NULL ORDER BY pinned DESC, updated_at DESC, created_at DESC")]
    def rename_conversation(self, conversation_id: str, title: str, now: float) -> dict[str, Any] | None:
        cleaned = title.strip()[:120]
        if not cleaned: return None
        with self.connect() as db: db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=?", (cleaned, now, conversation_id))
        return self.conversation(conversation_id)
    def completed_turns(self, exclude_conversation: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            sql = "SELECT " + MEMORY_TURN_COLUMNS + MEMORY_PAIRS
            args=[]
            if project_id: sql += " AND c.project_id=?"; args.append(project_id)
            if exclude_conversation: sql += " AND u.conversation_id != ?"; args.append(exclude_conversation)
            sql += " ORDER BY u.created_at"
            return [dict(x) for x in db.execute(sql,args)]
    def save_memory_embedding(self, turn: dict[str, Any], model: str, vector: list[float], now: float,
                              index_version: str, summary_version: str | None = None) -> bool:
        with self.connect() as db:
            # A background embedding may finish after an edit. Index only the
            # still-current turn, using one guarded write under SQLite's lock.
            cursor = db.execute("""INSERT OR REPLACE INTO conversation_memory_embeddings(conversation_id,user_message,assistant_message,turn_ordinal,model,dimension,index_version,vector_json,created_at)
                SELECT ?,?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 """ + MEMORY_PAIRS + """
                    AND u.conversation_id=? AND u.ordinal=? AND u.content=? AND a.content=?
                    AND NOT EXISTS (SELECT 1 FROM conversation_memory_summaries s
                        WHERE s.conversation_id=u.conversation_id AND s.model=? AND s.index_version=?
                          AND s.dimension=? AND u.ordinal BETWEEN s.first_ordinal AND s.last_ordinal))""",
                (turn['conversation_id'],turn['user_message'],turn['assistant_message'],turn['ordinal'],model,len(vector),index_version,json.dumps(vector),now,
                 turn['conversation_id'],turn['ordinal'],turn['user_message'],turn['assistant_message'],
                 model,summary_version,len(vector)))
            return cursor.rowcount > 0

    def memory_embeddings(self, model: str, dimension: int, index_version: str) -> list[dict[str, Any]]:
        with self.connect() as db: return [dict(x) for x in db.execute("SELECT * FROM conversation_memory_embeddings WHERE model=? AND dimension=? AND index_version=?",(model,dimension,index_version))]
    def delete_memory_for_conversation(self, conversation_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM conversation_memory_embeddings WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM conversation_memory_summaries WHERE conversation_id=?", (conversation_id,))

    @staticmethod
    def _memory_scope(project_id, cross_project, exclude_conversation):
        clause, args = "", []
        if not cross_project:
            clause += " AND c.project_id IS ?"
            args.append(project_id or None)
        if exclude_conversation is not None:
            clause += " AND c.id <> ?"
            args.append(exclude_conversation)
        return clause, args

    def unindexed_memory_turns(self, model, turn_version, summary_version, limit,
                               *, conversation_id=None, project_id=None):
        sql = "SELECT " + MEMORY_TURN_COLUMNS + MEMORY_PAIRS + """
            AND NOT EXISTS (SELECT 1 FROM conversation_memory_embeddings e
                WHERE e.conversation_id=u.conversation_id AND e.turn_ordinal=u.ordinal
                  AND e.model=? AND e.index_version=?
                  AND e.user_message=u.content AND e.assistant_message=a.content)
            AND NOT EXISTS (SELECT 1 FROM conversation_memory_summaries s
                WHERE s.conversation_id=u.conversation_id AND s.model=? AND s.index_version=?
                  AND u.ordinal BETWEEN s.first_ordinal AND s.last_ordinal)
        """
        args = [model, turn_version, model, summary_version]
        if project_id:
            sql += " AND c.project_id=?"
            args.append(project_id)
        if conversation_id is not None:
            sql += " AND c.id=? ORDER BY u.ordinal DESC"
            args.append(conversation_id)
        else:
            sql += " ORDER BY u.created_at,u.conversation_id,u.ordinal"
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " LIMIT ?", (*args, limit))]

    def memory_summary_batch(self, model, version, size, idle_before, conversation_id=None):
        uncovered = """ AND NOT EXISTS (SELECT 1 FROM conversation_memory_summaries s
            WHERE s.conversation_id=u.conversation_id AND s.model=? AND s.index_version=?
            AND u.ordinal BETWEEN s.first_ordinal AND s.last_ordinal)"""
        args = (model, version)
        with self.connect() as db:
            if conversation_id is None:
                row = db.execute("SELECT u.conversation_id " + MEMORY_PAIRS + uncovered +
                                 " GROUP BY u.conversation_id HAVING COUNT(*) >= ? OR c.updated_at <= ?"
                                 " ORDER BY MIN(u.created_at),u.conversation_id LIMIT 1",
                                 (*args, size, idle_before)).fetchone()
                if row is None:
                    return []
                conversation_id = row["conversation_id"]
            return [dict(row) for row in db.execute(
                "SELECT " + MEMORY_TURN_COLUMNS + MEMORY_PAIRS + uncovered +
                " AND u.conversation_id=? ORDER BY u.ordinal LIMIT ?", (*args, conversation_id, size))]

    def save_memory_summary(self, turns, text, model, version, vector, turn_version):
        conversation_id = turns[0]["conversation_id"]
        first, last = turns[0]["ordinal"], turns[-1]["ordinal"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Recheck the exact snapshot after embedding, in the same write transaction.
            live = self._memory_range(db, conversation_id, first, last, len(turns) + 1)
            if live != turns or db.execute("""SELECT 1 FROM conversation_memory_summaries
                WHERE conversation_id=? AND model=? AND index_version=?
                AND first_ordinal<=? AND last_ordinal>=?""",
                (conversation_id, model, version, last, first)).fetchone():
                return False
            db.execute("""INSERT INTO conversation_memory_summaries
                (conversation_id,first_ordinal,last_ordinal,text,model,dimension,index_version,vector_json,source_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (conversation_id, first, last, text, model, len(vector), version,
                 json.dumps(vector), max(turn["source_at"] for turn in turns)))
            db.execute("""DELETE FROM conversation_memory_embeddings WHERE conversation_id=?
                AND model=? AND index_version=? AND dimension=? AND turn_ordinal BETWEEN ? AND ?""",
                (conversation_id, model, turn_version, len(vector), first, last))
            return True

    def memory_summary_rows(self, model, version, dimension, project_id, cross_project, exclude):
        scope, args = self._memory_scope(project_id, cross_project, exclude)
        sql = """SELECT s.* FROM conversation_memory_summaries s JOIN conversations c ON c.id=s.conversation_id
                 WHERE s.model=? AND s.index_version=? AND (? IS NULL OR s.dimension=?)"""
        with self.connect() as db:
            for row in db.execute(sql + scope, (model, version, dimension, dimension, *args)):
                yield dict(row)

    def recent_memory_embeddings(self, model, version, summary_version, dimension, project_id, cross_project, exclude, limit):
        scope, args = self._memory_scope(project_id, cross_project, exclude)
        sql = """SELECT e.*,a.created_at source_at FROM conversation_memory_embeddings e
            JOIN conversations c ON c.id=e.conversation_id
            JOIN messages u ON u.conversation_id=e.conversation_id AND u.ordinal=e.turn_ordinal
            JOIN messages a ON a.conversation_id=e.conversation_id AND a.ordinal=e.turn_ordinal+1
            WHERE e.model=? AND e.index_version=? AND e.dimension=?
              AND u.content=e.user_message AND a.content=e.assistant_message AND """ + MEMORY_ELIGIBILITY + """
              AND NOT EXISTS (SELECT 1 FROM conversation_memory_summaries s WHERE s.conversation_id=c.id
                  AND s.model=e.model AND s.index_version=? AND s.dimension=e.dimension
                  AND e.turn_ordinal BETWEEN s.first_ordinal AND s.last_ordinal)"""
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + scope + " ORDER BY e.created_at DESC,e.id DESC LIMIT ?",
                (model, version, dimension, summary_version, *args, limit))]

    def recent_memory_turns(self, model, summary_version, project_id, cross_project, exclude, limit):
        scope, args = self._memory_scope(project_id, cross_project, exclude)
        sql = "SELECT " + MEMORY_TURN_COLUMNS + MEMORY_PAIRS + """
            AND NOT EXISTS (SELECT 1 FROM conversation_memory_summaries s WHERE s.conversation_id=c.id
                AND s.model=? AND s.index_version=? AND u.ordinal BETWEEN s.first_ordinal AND s.last_ordinal)
        """
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + scope + " ORDER BY a.created_at DESC,a.id LIMIT ?",
                (model, summary_version, *args, limit))]

    @staticmethod
    def _memory_range(db, conversation_id, first, last, limit, scope="", args=()):
        sql = "SELECT " + MEMORY_TURN_COLUMNS + MEMORY_PAIRS + """
            AND u.conversation_id=? AND u.ordinal BETWEEN ? AND ?"""
        return [dict(row) for row in db.execute(sql + scope + " ORDER BY u.ordinal LIMIT ?",
                                               (conversation_id, first, last, *args, limit))]

    def memory_detail_turns(self, conversation_id, first, last, limit, project_id, cross_project, exclude):
        scope, args = self._memory_scope(project_id, cross_project, exclude)
        with self.connect() as db:
            return self._memory_range(db, conversation_id, first, last, limit, scope, args)
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
