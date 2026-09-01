from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import re
from typing import Any


SCHEMA_VERSION = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, task_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL, ordinal INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, conversation_id TEXT, raw_request TEXT NOT NULL, route TEXT, state TEXT NOT NULL, authorization_state TEXT NOT NULL, reason_category TEXT, selected_context_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, duration_ms REAL, error TEXT);
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, source TEXT UNIQUE NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, metadata_json TEXT NOT NULL, indexed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS document_chunks(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(text, source UNINDEXED, document_id UNINDEXED);
CREATE TABLE IF NOT EXISTS tool_executions(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, tool_name TEXT NOT NULL, version TEXT NOT NULL, risk TEXT NOT NULL, input_json TEXT NOT NULL, output_json TEXT, status TEXT NOT NULL, latency_ms REAL NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS model_calls(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, model TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms REAL NOT NULL, status TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS procedures(id TEXT PRIMARY KEY, version TEXT NOT NULL, name TEXT NOT NULL, input_schema_json TEXT NOT NULL, steps_json TEXT NOT NULL, constraints_json TEXT NOT NULL, validated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS memory_facts(id INTEGER PRIMARY KEY, text TEXT NOT NULL, source TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS application_settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending_actions(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, action_id TEXT UNIQUE NOT NULL, tool_name TEXT NOT NULL, tool_input_json TEXT NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, path TEXT UNIQUE NOT NULL, result_count INTEGER NOT NULL, size_bytes INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS vector_embeddings(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, chunk_ordinal INTEGER NOT NULL, line_start INTEGER NOT NULL, text TEXT NOT NULL, content_hash TEXT NOT NULL, document_hash TEXT NOT NULL, model TEXT NOT NULL, dimension INTEGER NOT NULL, index_version TEXT NOT NULL, vector_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(document_id,chunk_ordinal,model,index_version));
"""


class Database:
    def __init__(self, path: str): self.path = path
    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
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
            elif rows and rows[0][0] != SCHEMA_VERSION: raise RuntimeError(f"incompatible schema version {rows[0][0]}")
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

    def create_conversation(self, title: str, now: float, conversation_id: str) -> str:
        with self.connect() as db: db.execute("INSERT INTO conversations VALUES(?,?,?)", (conversation_id, title[:120] or "New conversation", now))
        return conversation_id
    def add_message(self, conversation_id: str, role: str, content: str, now: float, message_id: str, task_id: str | None = None) -> None:
        with self.connect() as db:
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (message_id, conversation_id, task_id, role, content, ordinal, now))
    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not row: return None
            return {**dict(row), "messages": [dict(x) for x in db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY ordinal", (conversation_id,))]}
    def save_setting(self, key: str, value: Any, now: float) -> None:
        with self.connect() as db: db.execute("INSERT OR REPLACE INTO application_settings VALUES(?,?,?)", (key,json.dumps(value),now))
    def load_settings(self) -> dict[str, Any]:
        with self.connect() as db: return {x["key"]: json.loads(x["value_json"]) for x in db.execute("SELECT * FROM application_settings")}
