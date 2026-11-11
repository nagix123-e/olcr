"""MCP connection diagnostics independent of coding-task/model tool selection.

Probes use only initialize + initialized + tools/list, then close their child.
They never invoke tools, read a user's project, or grant new execution policy.
"""
from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import threading

from .mcp_manifest import MCP_MANIFEST
from .mcp_runtime import MCPRuntime
from .serena_runtime import launch_command as serena_command
from .optional_runtimes import runtime as components, SetupError
from .node_mcp_runtime import managed_browser_command, resolve_mcp_resources, runtime_root


class MCPConnections:
    def __init__(self):
        self._checks: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._checking: set[str] = set()

    def _configuration(self, name):
        definition = MCP_MANIFEST[name]
        resources = resolve_mcp_resources(name, for_connection=True)
        command = resources.get("command")
        scope = "coding"
        # This adapter is already enabled for managed browser sessions. This
        # does not enable the separately disabled generic coding MCP path.
        if name == "serena":
            command = serena_command()
            resources["reason"] = "READY" if command else "PYTHON_RUNTIME_MISSING"
        if name == "playwright":
            scope = "managed_browser"
            command = managed_browser_command()
        enabled = bool(definition.get("enabled_by_policy") or definition.get("connection_enabled")) or scope == "managed_browser"
        reason = "READY" if command else (("SERVER_RESOURCE_MISSING" if runtime_root() else "NODE_RUNTIME_MISSING")
                                         if scope == "managed_browser" else resources["reason"])
        return definition, command, scope, enabled, reason

    def inventory(self):
        rows = []
        for name in MCP_MANIFEST:
            definition, command, scope, enabled, reason = self._configuration(name)
            row = {"server": name, "display_name": definition["display_name"], "transport": definition["transport"],
                   "scope": scope, "coding_enabled": bool(definition.get("enabled_by_policy")),
                   "resource_available": bool(command), "can_check": enabled,
                   "connection_status": "NOT_CHECKED" if command and enabled else "DISABLED" if not enabled else "UNAVAILABLE",
                   "reason": "NOT_CHECKED" if command and enabled else reason,
                   "checked_at": None, "tool_count": None}
            with self._lock:
                saved = self._checks.get(name)
                if saved and saved["command"] == command and row["can_check"]:
                    row.update(saved["result"])
                if name in self._checking:
                    row["connection_status"] = "CHECKING"
            rows.append(row)
        return rows

    def check(self, name):
        definition, command, scope, enabled, reason = self._configuration(name)
        if not enabled:
            return next(row for row in self.inventory() if row["server"] == name)
        with self._lock:
            if name in self._checking:
                return {"server": name, "connection_status": "CHECKING", "reason": "CHECK_IN_PROGRESS"}
            self._checking.add(name)
        result = {"connection_status": "FAILED", "reason": "CONNECTION_FAILED", "tool_count": None}
        runtime = None
        try:
            if not command:
                components.ensure('serena' if name == 'serena' else 'node-tools')
                definition, command, scope, enabled, reason = self._configuration(name)
                if not command:
                    raise FileNotFoundError(reason)
            with tempfile.TemporaryDirectory(prefix="olcr-mcp-check-") as directory:
                runtime = MCPRuntime(command, [], startup_timeout=definition["startup_timeout"],
                                     timeout=definition["tool_timeout"], shutdown_timeout=definition["shutdown_timeout"], cwd=directory)
                runtime.start()
                initialized = runtime.initialize()
                if initialized.get("status") != "AVAILABLE":
                    result["reason"] = initialized.get("error_code", "INITIALIZE_FAILED")
                else:
                    listing = runtime.tools_list()
                    if listing.get("status") != "AVAILABLE":
                        result["reason"] = listing.get("error_code", "TOOLS_LIST_FAILED")
                    else:
                        tools = listing["response"]["result"]["tools"]
                        names = {tool["name"] for tool in tools}
                        missing = set(definition["allowed_tools"]) - names
                        result.update(tool_count=len(tools),
                                      protocol_version=runtime.last_diagnostic.get("protocol_version"),
                                      connection_status="FAILED" if missing else "CONNECTED",
                                      reason="REQUIRED_TOOL_MISSING" if missing else "READY")
                # Close before removing the subprocess's working directory.
                runtime.close()
        except SetupError:
            result["reason"] = "RUNTIME_SETUP_FAILED"
        except FileNotFoundError:
            result["reason"] = reason
        except (OSError, ValueError):
            result["reason"] = "CONNECTION_FAILED"
        finally:
            if runtime is not None:
                runtime.close()
            result["checked_at"] = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._checks[name] = {"command": command, "result": result}
                self._checking.discard(name)
        return next(row for row in self.inventory() if row["server"] == name)


mcp_connections = MCPConnections()


def task_connection_availability(rows):
    """Historic task evidence, never a substitute for a live connection probe."""
    availability = "NOT_CHECKED"
    initialized = False
    for row in rows:
        tool, status = row.get("tool_name"), row.get("status")
        if tool == "initialize":
            initialized = status == "PASS"
            availability = "NOT_CHECKED" if initialized else "NO"
        elif tool == "tools/list":
            availability = "YES" if initialized and status == "PASS" else "NO" if status != "PASS" else "NOT_CHECKED"
        elif tool and status == "PASS":
            availability = "YES"
        elif not tool and status == "BLOCKED":
            availability = "NO"
    return availability
