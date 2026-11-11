"""Persistent, app-wide model traffic history."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import base64
import ipaddress
import json
import math
import re
import sys
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

from fastapi import HTTPException


RETENTION_OPTIONS = (1, 7, 30, 90)
DEFAULT_RETENTION_DAYS = 30
PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 100
_DEFAULT_PROVIDER_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "openai_oauth": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
    "nvidia_nim": "https://integrate.api.nvidia.com/v1",
    "huggingface": "https://router.huggingface.co/v1",
    "ollama_cloud": "https://ollama.com/v1",
}


@dataclass(frozen=True)
class TrafficContext:
    project_id: str | None = None
    project_name: str | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None


_traffic_context: ContextVar[TrafficContext] = ContextVar(
    "olcr_model_traffic_context", default=TrafficContext())
_database_provider = None
_schema_lock = threading.Lock()


@contextmanager
def traffic_scope(project_id: str | None, project_name: str | None,
                  conversation_id: str | None, conversation_title: str | None) -> Iterator[None]:
    """Associate model calls in this request/task with label snapshots."""
    context = TrafficContext(project_id, project_name, conversation_id, conversation_title)
    token = _traffic_context.set(context)
    try:
        yield
    finally:
        _traffic_context.reset(token)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, bytes):
        return {"$binary_base64": base64.b64encode(value).decode("ascii"),
                "size_bytes": len(value)}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=str)]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _json_safe(dump(mode="json"))
        except (TypeError, ValueError):
            pass
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))
    return str(value)


def _json_text(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))


def _error_code(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:80]
    return cleaned or None


def uses_internet(endpoint: str | None) -> bool:
    """Classify a model endpoint without retaining its hostname or URL."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        return False
    try:
        host = urlsplit(endpoint.strip()).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    local_suffixes = (".local", ".localhost", ".lan", ".internal",
                      ".intranet", ".home.arpa", ".localdomain",
                      ".test", ".example", ".invalid")
    return "." in host and not host.endswith(local_suffixes)


def _ensure_tables(connection) -> None:
    with _schema_lock:
        connection.execute("""CREATE TABLE IF NOT EXISTS model_traffic_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            finished_at REAL,
            project_id TEXT,
            project_name TEXT,
            conversation_id TEXT,
            conversation_title TEXT,
            role TEXT NOT NULL,
            model TEXT NOT NULL,
            runtime TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms REAL,
            request_json TEXT NOT NULL,
            response_json TEXT,
            error_code TEXT,
            internet INTEGER NOT NULL DEFAULT 0
        )""")
        columns = {row["name"] for row in connection.execute(
            "PRAGMA table_info(model_traffic_logs)").fetchall()}
        if "internet" not in columns:
            connection.execute(
                "ALTER TABLE model_traffic_logs ADD COLUMN internet INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS model_traffic_logs_created "
            "ON model_traffic_logs(created_at DESC,id DESC)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_traffic_log_migrations(version INTEGER PRIMARY KEY)")
        if not connection.execute(
                "SELECT 1 FROM model_traffic_log_migrations WHERE version=1").fetchone():
            _backfill_brain_internet_flags(connection)
            connection.execute("INSERT INTO model_traffic_log_migrations(version) VALUES(1)")


def _backfill_brain_internet_flags(connection) -> None:
    """Recover public-provider classification for rows written before the flag existed."""
    provider_rows = []
    if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_providers'").fetchone():
        provider_rows = connection.execute(
            "SELECT id,metadata_json FROM brain_providers").fetchall()
    metadata_by_id = {}
    for row in provider_rows:
        try:
            metadata_by_id[row["id"]] = json.loads(row["metadata_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    rows = connection.execute(
        "SELECT id,runtime,request_json FROM model_traffic_logs "
        "WHERE internet=0 AND runtime LIKE 'brain:%'").fetchall()
    for row in rows:
        try:
            request_body = json.loads(row["request_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(request_body, dict):
            continue
        provider = str(request_body.get("provider") or row["runtime"][6:])
        account_id = request_body.get("provider_account_id", 1)
        if type(account_id) is not int or not 1 <= account_id <= 3:
            account_id = 1
        account_key = provider if account_id == 1 else f"{provider}:account:{account_id}"
        metadata = metadata_by_id.get(account_key)
        endpoint = metadata.get("base_url") if isinstance(metadata, dict) else None
        endpoint = endpoint or _DEFAULT_PROVIDER_ENDPOINTS.get(provider)
        if uses_internet(endpoint):
            connection.execute(
                "UPDATE model_traffic_logs SET internet=1 WHERE id=?", (row["id"],))


def _retention_days(connection) -> int | None:
    row = connection.execute(
        "SELECT value_json FROM application_settings WHERE key='model_traffic_retention_days'").fetchone()
    if not row:
        return DEFAULT_RETENTION_DAYS
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return DEFAULT_RETENTION_DAYS
    return value if value is None or (type(value) is int and value in RETENTION_OPTIONS) else DEFAULT_RETENTION_DAYS


def _purge_expired(connection, now: float) -> None:
    days = _retention_days(connection)
    if days is not None:
        connection.execute("DELETE FROM model_traffic_logs WHERE created_at < ?", (now - days * 86400,))


class ModelTrafficCall:
    """One row per model attempt; finish is safe to call at most once."""
    def __init__(self, database, entry_id: int | None, started: float):
        self._database = database
        self.id = entry_id
        self._started = started
        self._finished = False
        self._lock = threading.Lock()

    def finish(self, *, status: str, response_body: Any = None,
               error_code: str | None = None) -> None:
        if self.id is None or status not in {"success", "error", "cancelled"}:
            return
        with self._lock:
            if self._finished:
                return
            self._finished = True
        finished_at = time.time()
        duration_ms = max(0.0, (time.perf_counter() - self._started) * 1000.0)
        try:
            database = self._database()
            with database.connect() as connection:
                _ensure_tables(connection)
                connection.execute("""UPDATE model_traffic_logs
                    SET finished_at=?,status=?,duration_ms=?,response_json=?,error_code=?
                    WHERE id=? AND status='pending'""",
                    (finished_at, status, duration_ms,
                     _json_text(response_body) if response_body is not None else None,
                     _error_code(error_code), self.id))
        except Exception as exc:
            print(f"MODEL_TRAFFIC_LOG_WRITE_FAILED operation=finish error={type(exc).__name__}",
                  file=sys.stderr, flush=True)


def begin_model_traffic(*, role: str, model: str, runtime: str,
                        request_body: Any, record: bool = True,
                        internet: bool = False) -> ModelTrafficCall:
    """Persist request input before inference; headers and credentials are never accepted."""
    started = time.perf_counter()
    if not record:
        return ModelTrafficCall(lambda: None, None, started)
    database = _database_provider
    if not callable(database):
        return ModelTrafficCall(lambda: None, None, started)
    context = _traffic_context.get()
    now = time.time()
    try:
        db = database()
        with db.connect() as connection:
            _ensure_tables(connection)
            _purge_expired(connection, now)
            cursor = connection.execute("""INSERT INTO model_traffic_logs(
                created_at,project_id,project_name,conversation_id,conversation_title,
                role,model,runtime,status,request_json,internet)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (now, context.project_id, context.project_name, context.conversation_id,
                 context.conversation_title, str(role)[:120], str(model)[:240],
                 str(runtime)[:120], "pending", _json_text(request_body), int(bool(internet))))
            entry_id = int(cursor.lastrowid)
    except Exception as exc:
        print(f"MODEL_TRAFFIC_LOG_WRITE_FAILED operation=begin error={type(exc).__name__}",
              file=sys.stderr, flush=True)
        return ModelTrafficCall(lambda: None, None, started)
    return ModelTrafficCall(database, entry_id, started)


def _public_row(row) -> dict[str, Any]:
    result = {key: row[key] for key in (
        "id", "created_at", "finished_at", "project_name", "conversation_title",
        "role", "model", "runtime", "status", "duration_ms", "error_code")}
    result["internet"] = bool(row["internet"])
    return result


def register_model_traffic_routes(app, database) -> None:
    global _database_provider
    _database_provider = database

    def db():
        return database()

    @app.get("/api/model-traffic/settings")
    def get_model_traffic_settings():
        with db().connect() as connection:
            _ensure_tables(connection)
            return {"retention_days": _retention_days(connection)}

    @app.put("/api/model-traffic/settings")
    def put_model_traffic_settings(payload: dict[str, Any]):
        value = payload.get("retention_days", DEFAULT_RETENTION_DAYS)
        if value is not None and (type(value) is not int or value not in RETENTION_OPTIONS):
            raise HTTPException(422, "RETENTION_DAYS_INVALID")
        now = time.time()
        with db().connect() as connection:
            _ensure_tables(connection)
            connection.execute("""INSERT INTO application_settings(key,value_json,updated_at)
                VALUES('model_traffic_retention_days',?,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (json.dumps(value), now))
            _purge_expired(connection, now)
        return {"retention_days": value}

    @app.get("/api/model-traffic")
    def list_model_traffic(before_id: int | None = None, before_created_at: float | None = None,
                           limit: int = PAGE_SIZE_DEFAULT):
        if before_id is not None and before_id < 1:
            raise HTTPException(422, "BEFORE_ID_INVALID")
        if before_created_at is not None and (not math.isfinite(before_created_at) or before_id is None):
            raise HTTPException(422, "BEFORE_CURSOR_INVALID")
        if type(limit) is not int or not 1 <= limit <= PAGE_SIZE_MAX:
            raise HTTPException(422, "LIMIT_INVALID")
        now = time.time()
        with db().connect() as connection:
            _ensure_tables(connection)
            _purge_expired(connection, now)
            sql = "SELECT id,created_at,finished_at,project_name,conversation_title,role,model,runtime,status,duration_ms,error_code,internet FROM model_traffic_logs"
            params: tuple[Any, ...] = ()
            if before_created_at is not None and before_id is not None:
                sql += " WHERE created_at < ? OR (created_at = ? AND id < ?)"
                params = (before_created_at, before_created_at, before_id)
            elif before_id is not None:
                sql += " WHERE id < ?"
                params = (before_id,)
            rows = connection.execute(sql + " ORDER BY created_at DESC,id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        has_more = len(rows) > limit
        items = rows[:limit]
        return {"entries": [_public_row(row) for row in items],
                "next_cursor": ({"created_at": float(items[-1]["created_at"]),
                                 "id": int(items[-1]["id"])} if has_more and items else None)}

    @app.get("/api/model-traffic/{entry_id}")
    def get_model_traffic_detail(entry_id: int):
        if entry_id < 1:
            raise HTTPException(422, "ENTRY_ID_INVALID")
        with db().connect() as connection:
            _ensure_tables(connection)
            _purge_expired(connection, time.time())
            row = connection.execute("SELECT * FROM model_traffic_logs WHERE id=?", (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, "MODEL_TRAFFIC_NOT_FOUND")
        result = _public_row(row)
        for field in ("request_json", "response_json"):
            try:
                result[field.removesuffix("_json")] = json.loads(row[field]) if row[field] is not None else None
            except (TypeError, ValueError, json.JSONDecodeError):
                result[field.removesuffix("_json")] = row[field]
        return result
