"""Small task-scoped MCP process boundary.

The orchestrator uses this adapter only when a task explicitly requests MCP;
unknown tools are rejected by the allowlist and processes are always closed.
"""
from __future__ import annotations
import os, subprocess, json, time, threading
from typing import Any, Iterable

class MCPRuntime:
    def __init__(self, command: Iterable[str], allowed_tools: Iterable[str], timeout: float = 10.0, startup_timeout: float | None = None, shutdown_timeout: float = 2.0, env_allowlist: Iterable[str] = (), cwd: str | None = None):
        self.command=list(command); self.allowed=set(allowed_tools); self.timeout=timeout; self.startup_timeout=startup_timeout or timeout; self.shutdown_timeout=shutdown_timeout; self.env_allowlist=set(env_allowlist); self.cwd=cwd; self.process=None; self.state="NOT_STARTED"; self.last_diagnostic={}
    def start(self):
        if self.process is not None: return self.state
        if not self.command: self.state="FAILED"; return self.state
        self.state="STARTING"
        env={k:os.environ[k] for k in self.env_allowlist if k in os.environ}
        try:
            self.process=subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=self.cwd)
            self.last_diagnostic={"pid":self.process.pid,"initialized":False}; return self.state
        except OSError as exc:
            self.state="FAILED"; self.last_diagnostic={"error":str(exc)}; return self.state
    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if tool not in self.allowed: return {"status":"BLOCKED", "error":"tool not allowed"}
        if not self.process: self.start()
        if self.state != "READY": return {"status":self.state, "error":"server not ready"}
        if not self.process or not self.process.stdin: return {"status":"FAILED", "error":"process unavailable"}
        request={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":tool,"arguments":arguments or {}}}
        try:
            self.process.stdin.write(json.dumps(request)+"\n"); self.process.stdin.flush()
            line=""
            def read():
                nonlocal line
                line=self.process.stdout.readline() if self.process and self.process.stdout else ""
            thread=threading.Thread(target=read,daemon=True); thread.start(); thread.join(self.timeout)
            if thread.is_alive(): return {"status":"FAILED","error":"tool timeout"}
            return {"status":"AVAILABLE", "response":json.loads(line)} if line else {"status":"FAILED","error":"empty response"}
        except Exception as exc: return {"status":"FAILED", "error":str(exc)}

    def initialize(self, protocol_version: str = "2024-11-05") -> dict[str, Any]:
        """Complete MCP initialize handshake before tools can be called."""
        result = self._request("initialize", {"protocolVersion": protocol_version, "capabilities": {}, "clientInfo": {"name": "olcr", "version": "0.1"}})
        response = result.get("response")
        if result.get("status") == "AVAILABLE" and isinstance(response, dict) and "result" in response and "error" not in response:
            self._notify("notifications/initialized", {})
            self.last_diagnostic["initialized"] = True
            self.state = "READY"
            return result
        return {"status":"FAILED", "error":"invalid initialize response"}

    def tools_list(self) -> dict[str, Any]:
        if not self.last_diagnostic.get("initialized"): return {"status":"BLOCKED", "error":"initialize required"}
        return self._request("tools/list", {})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.process or self.state not in {"STARTING", "READY"} or not self.process.stdin: return {"status":"FAILED", "error":"server not ready"}
        try:
            self.process.stdin.write(json.dumps({"jsonrpc":"2.0","id":int(time.time()*1000)%1000000,"method":method,"params":params})+"\n"); self.process.stdin.flush()
            line=""
            def read():
                nonlocal line
                line=self.process.stdout.readline() if self.process and self.process.stdout else ""
            thread=threading.Thread(target=read,daemon=True); thread.start(); thread.join(self.startup_timeout if method == "initialize" else self.timeout)
            if thread.is_alive(): return {"status":"FAILED", "error":"request timeout"}
            return {"status":"AVAILABLE", "response":json.loads(line)} if line else {"status":"FAILED","error":"empty response"}
        except Exception as exc: return {"status":"FAILED","error":str(exc)}
    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self.process and self.process.stdin:
            self.process.stdin.write(json.dumps({"jsonrpc":"2.0","method":method,"params":params})+"\n"); self.process.stdin.flush()
    def close(self):
        if self.process:
            self.state="STOPPING"; self.process.terminate()
            try: self.process.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired: self.process.kill()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    if stream: stream.close()
                except OSError: pass
            self.process=None; self.state="STOPPED"; self.last_diagnostic["shutdown"]=True
