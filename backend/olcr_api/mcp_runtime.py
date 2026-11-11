"""Allowlisted MCP stdio connection with one reader per child process.

Notifications cannot complete requests. Timed-out connections are closed before
reuse. No shell, model, downloads, or inherited credentials are involved.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Iterable

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE_CHARS = 8_000_000


class MCPRuntime:
    def __init__(self, command: Iterable[str], allowed_tools: Iterable[str], timeout: float = 10.0,
                 startup_timeout: float | None = None, shutdown_timeout: float = 2.0,
                 env_allowlist: Iterable[str] = (), cwd: str | None = None):
        self.command = list(command)
        self.allowed = set(allowed_tools)
        self.timeout = timeout
        self.startup_timeout = startup_timeout if startup_timeout is not None else timeout
        self.shutdown_timeout = shutdown_timeout
        self.env_allowlist = set(env_allowlist)
        self.cwd = cwd
        self.process = None
        self.state = "NOT_STARTED"
        self.last_diagnostic: dict[str, Any] = {}
        self._stderr_thread = self._stdout_thread = None
        self._responses: queue.Queue = queue.Queue(maxsize=64)
        self._requests = itertools.count(1)
        self._operation_lock = threading.RLock()
        self._write_lock = threading.Lock()

    def start(self):
        with self._operation_lock:
            if self.process is not None:
                if self.process.poll() is not None:
                    self._fail("PROCESS_EXITED", "MCP process exited")
                return self.state
            if not self.command:
                self.state = "FAILED"
                self.last_diagnostic = {"initialized": False, "error_code": "COMMAND_MISSING"}
                return self.state
            self.state = "STARTING"
            self._responses = queue.Queue(maxsize=64)
            env = {key: os.environ[key] for key in self.env_allowlist if key in os.environ}
            try:
                process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, cwd=self.cwd)
                self.process = process
                self.last_diagnostic = {"pid": process.pid, "initialized": False, "notifications": 0}
                self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(process.stderr,), daemon=True)
                self._stdout_thread = threading.Thread(target=self._read_messages, args=(process, self._responses), daemon=True)
                self._stderr_thread.start()
                self._stdout_thread.start()
            except OSError:
                self.state = "FAILED"
                self.last_diagnostic = {"initialized": False, "error_code": "PROCESS_START_FAILED"}
            return self.state

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._operation_lock:
            if tool not in self.allowed:
                return {"status": "BLOCKED", "error": "tool not allowed"}
            if self.state != "READY":
                return {"status": "FAILED", "error": "server not ready"}
            return self._request("tools/call", {"name": tool, "arguments": arguments or {}})

    def initialize(self, protocol_version: str = SUPPORTED_PROTOCOL_VERSIONS[0]) -> dict[str, Any]:
        with self._operation_lock:
            if self.state == "READY":
                if self.process and self.process.poll() is None:
                    return {"status": "AVAILABLE", "response": self._initialize_response}
                return self._fail("PROCESS_EXITED", "MCP process exited")
            if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                return {"status": "BLOCKED", "error": "unsupported protocol version"}
            if self.state == "NOT_STARTED":
                self.start()
            result = self._request("initialize", {"protocolVersion": protocol_version, "capabilities": {},
                                                  "clientInfo": {"name": "olcr", "version": "0.1"}})
            if result.get("status") != "AVAILABLE":
                if self.process is not None:
                    self._fail(result.get("error_code", "INITIALIZE_FAILED"), result.get("error", "initialize failed"))
                return result
            payload = result["response"].get("result")
            if (not isinstance(payload, dict) or not isinstance(payload.get("capabilities"), dict)
                    or not isinstance(payload.get("serverInfo"), dict)
                    or not all(isinstance(payload["serverInfo"].get(key), str) and payload["serverInfo"][key]
                               for key in ("name", "version"))):
                return self._fail("INVALID_INITIALIZE_RESPONSE", "invalid initialize response")
            version = payload.get("protocolVersion")
            if version not in SUPPORTED_PROTOCOL_VERSIONS:
                return self._fail("UNSUPPORTED_PROTOCOL_VERSION", "unsupported server protocol version")
            try:
                self._notify("notifications/initialized", {})
            except (OSError, ValueError):
                return self._fail("CONNECTION_CLOSED", "MCP connection closed during initialize")
            self._initialize_response = result["response"]
            self.last_diagnostic.update(initialized=True, protocol_version=version,
                                        server_info=payload["serverInfo"], capabilities=payload["capabilities"])
            self.state = "READY"
            return result

    def tools_list(self) -> dict[str, Any]:
        with self._operation_lock:
            if self.state != "READY":
                return {"status": "BLOCKED", "error": "initialize required"}
            tools, seen = [], set()
            cursor = None
            deadline = time.monotonic() + self.timeout
            for _ in range(100):
                result = self._request("tools/list", {"cursor": cursor} if cursor else {}, deadline=deadline)
                if result.get("status") != "AVAILABLE":
                    return result
                payload = result["response"].get("result")
                if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list) or any(
                        not isinstance(tool, dict) or not isinstance(tool.get("name"), str) for tool in payload["tools"]):
                    return self._fail("INVALID_TOOLS_RESPONSE", "invalid tools/list response")
                tools.extend(payload["tools"])
                cursor = payload.get("nextCursor")
                if cursor is None:
                    return {**result, "response": {**result["response"], "result": {**payload, "tools": tools}}}
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    break
                seen.add(cursor)
            return self._fail("INVALID_TOOLS_PAGINATION", "tools/list pagination did not finish")

    def _request(self, method: str, params: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        process = self.process
        if not process or self.state not in {"STARTING", "READY"}:
            return {"status": "FAILED", "error": "server not ready",
                    "error_code": self.last_diagnostic.get("error_code", "NOT_CONNECTED")}
        if process.poll() is not None:
            return self._fail("PROCESS_EXITED", "MCP process exited")
        request_id = next(self._requests)
        timeout = self.startup_timeout if method == "initialize" else self.timeout
        deadline = deadline if deadline is not None else time.monotonic() + timeout
        try:
            if deadline <= time.monotonic():
                raise queue.Empty
            self._write(process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                message = self._responses.get(timeout=remaining)
                if "_transport_error" in message:
                    return self._fail(message["_transport_error"], "MCP connection closed or sent an invalid message")
                if type(message.get("id")) is not int or message["id"] != request_id:
                    continue
                if "error" in message:
                    return {"status": "FAILED", "error": "MCP server returned a JSON-RPC error",
                            "error_code": "JSON_RPC_ERROR", "response": message}
                return {"status": "AVAILABLE", "response": message}
        except queue.Empty:
            return self._fail("REQUEST_TIMEOUT", f"MCP {method} request timeout")
        except (OSError, ValueError):
            return self._fail("CONNECTION_CLOSED", "MCP connection closed while sending request")

    def _write(self, process, message):
        with self._write_lock:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write(self.process, {"jsonrpc": "2.0", "method": method, "params": params})

    def _read_messages(self, process, responses):
        def put(message):
            try:
                responses.put_nowait(message)
                return True
            except queue.Full:
                while not responses.empty():
                    try: responses.get_nowait()
                    except queue.Empty: break
                responses.put_nowait({"_transport_error": "RESPONSE_QUEUE_OVERFLOW"})
                return False
        try:
            while True:
                line = process.stdout.readline(MAX_MESSAGE_CHARS + 1)
                if not line:
                    put({"_transport_error": "PROCESS_EXITED"})
                    return
                if len(line) > MAX_MESSAGE_CHARS or not line.endswith("\n"):
                    raise ValueError
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError
                if "method" in message:
                    if not isinstance(message["method"], str):
                        raise ValueError
                    if "id" in message:
                        # No sampling, elicitation or filesystem roots are advertised.
                        reply = {"result": {}} if message["method"] == "ping" else {
                            "error": {"code": -32601, "message": "Client method not supported"}}
                        self._write(process, {"jsonrpc": "2.0", "id": message["id"], **reply})
                    else:
                        self.last_diagnostic["notifications"] = self.last_diagnostic.get("notifications", 0) + 1
                    continue
                if "id" not in message or ("result" in message) == ("error" in message):
                    raise ValueError
                if not put(message):
                    return
        except (OSError, ValueError, TypeError):
            put({"_transport_error": "INVALID_MESSAGE"})

    @staticmethod
    def _drain_stderr(stream) -> None:
        """Discard diagnostics without buffering unbounded lines or secrets."""
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def _fail(self, code, error):
        self.close()
        self.state = "FAILED"
        self.last_diagnostic.update(initialized=False, error_code=code)
        return {"status": "FAILED", "error": error, "error_code": code}

    def close(self):
        with self._operation_lock:
            process = self.process
            if process is not None:
                self.state = "STOPPING"
                try:
                    process.stdin.close()
                    process.wait(timeout=self.shutdown_timeout)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        process.terminate()
                        try: process.wait(timeout=self.shutdown_timeout)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                for thread in (self._stdout_thread, self._stderr_thread):
                    if thread is not None:
                        thread.join(timeout=1)
                for stream in (process.stdout, process.stderr):
                    if stream:
                        stream.close()
                self.process = None
                self._stdout_thread = self._stderr_thread = None
            self.state = "STOPPED"
            self.last_diagnostic.update(initialized=False, shutdown=True)
