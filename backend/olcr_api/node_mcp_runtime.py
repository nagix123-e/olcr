"""Resolve immutable, OLCR-owned Node MCP resources.

Release builds resolve the completed bundled runtime only. A source checkout
may opt into that same bundled Node binary while using checked-in Anime.js
server/corpus files until release staging copies them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_RESOURCE_PREFIXES = ("node_modules/", "browser/", "servers/")


def _resource_roots(resource_root: str | Path | None = None) -> list[Path]:
    """Return only OLCR-owned resource roots, in deterministic priority order."""
    roots: list[Path] = []
    if resource_root:
        roots.append(Path(resource_root))
    if override := os.environ.get("OLCR_NODE_MCP_RUNTIME_ROOT"):
        roots.append(Path(override))
    support = Path(os.environ.get("OLCR_APP_SUPPORT", Path.home() / "Library" / "Application Support" / "OLCR"))
    roots.append(support / "runtime" / "current")
    roots.append(Path(__file__).resolve().parents[2])
    unique: list[Path] = []
    for root in roots:
        expanded = root.expanduser()
        if expanded not in unique:
            unique.append(expanded)
    return unique


def _runtime_candidate(root: Path) -> Path | None:
    candidate = root / "mcp-runtime" / "node"
    if (candidate / "runtime-manifest.json").is_file() and (candidate / "node" / "bin" / "node").is_file():
        return candidate
    return None


def runtime_root(resource_root: str | Path | None = None) -> Path | None:
    for root in _resource_roots(resource_root):
        if candidate := _runtime_candidate(root):
            return candidate
    return None


def _source_repo_root(source_root: str | Path | None = None) -> Path | None:
    """Resolve source resources only for an explicit latest-source launch."""
    mode = os.environ.get("MCP_RESOURCE_MODE", "").upper()
    if mode == "BUNDLED_RELEASE":
        return None
    if source_root is not None:
        # Preserve the caller's spelling for deterministic command arguments
        # (notably temporary paths under /var on macOS).
        return Path(source_root).expanduser()
    backend_env = os.environ.get("OLCR_BACKEND_DIR")
    source_backend = Path(__file__).resolve().parents[1]
    if not backend_env or Path(backend_env).expanduser().resolve() != source_backend:
        return None
    if mode not in {"", "DEV_SOURCE"}:
        return None
    return source_backend.parent


def _read_manifest(root: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _bundled_entrypoint(root: Path, server: str, metadata: dict[str, Any] | None) -> tuple[list[str] | None, str]:
    if metadata is None:
        return None, "RESOURCE_RESOLUTION_FAILED"
    entrypoint = (metadata.get("entrypoints") or {}).get(server)
    if not isinstance(entrypoint, list) or not all(isinstance(item, str) for item in entrypoint):
        return None, "SERVER_RESOURCE_MISSING"
    if any(item.startswith(_RESOURCE_PREFIXES) and not (root / item).is_file() for item in entrypoint):
        return None, "SERVER_RESOURCE_MISSING"
    if server == "animejs" and not (root / "servers" / "animejs-reference" / "animejs-v4-reviewed.json").is_file():
        return None, "CORPUS_RESOURCE_MISSING"
    command = [str(root / "node" / "bin" / "node")]
    command.extend(str(root / item) if item.startswith(_RESOURCE_PREFIXES) else item for item in entrypoint)
    return command, "READY"


def resolve_mcp_resources(server: str, resource_root: str | Path | None = None,
                         source_root: str | Path | None = None) -> dict[str, Any]:
    """Return deterministic resource and launch prerequisites for ``server``."""
    try:
        from .mcp_manifest import MCP_MANIFEST
        registered = server in MCP_MANIFEST
        enabled = bool(MCP_MANIFEST.get(server, {}).get("enabled_by_policy"))
    except Exception:
        registered = enabled = False
    configured_mode = os.environ.get("MCP_RESOURCE_MODE", "").upper()
    result: dict[str, Any] = {
        "server": server, "registered": registered, "enabled": enabled,
        "node_runtime_available": False, "server_resource_available": False,
        "corpus_resource_available": False, "resource_mode": configured_mode if configured_mode in {"DEV_SOURCE", "BUNDLED_RELEASE"} else "NONE",
        "command": None, "reason": "MCP_NOT_REGISTERED" if not registered else
        ("POLICY_DISABLED" if not enabled else "NODE_RUNTIME_MISSING"),
    }
    if not registered or not enabled:
        return result
    root = runtime_root(resource_root)
    bundled_reason: str | None = None
    if root is not None:
        result["node_runtime_available"] = True
        if server == "animejs":
            result["server_resource_available"] = (root / "servers" / "animejs-reference" / "server.js").is_file()
            result["corpus_resource_available"] = (root / "servers" / "animejs-reference" / "animejs-v4-reviewed.json").is_file()
        # An explicit DEV_SOURCE request must remain source mode even when a
        # complete bundle happens to be installed on the same machine.
        if configured_mode != "DEV_SOURCE" or server != "animejs":
            command, reason = _bundled_entrypoint(root, server, _read_manifest(root))
            if command is not None:
                result.update(command=command, resource_mode="BUNDLED_RELEASE", reason="READY")
                if server != "animejs":
                    result["server_resource_available"] = True
                return result
            if server != "animejs":
                result["reason"] = reason
                return result
            bundled_reason = reason
    source = _source_repo_root(source_root)
    if server == "animejs" and source is not None:
        result["resource_mode"] = "DEV_SOURCE"
        server_path = source / "packaging" / "node-mcp" / "animejs-reference" / "server.js"
        corpus_path = server_path.with_name("animejs-v4-reviewed.json")
        result["server_resource_available"] = server_path.is_file()
        result["corpus_resource_available"] = corpus_path.is_file()
        if root is None:
            result["reason"] = "NODE_RUNTIME_MISSING"
        elif not server_path.is_file():
            result["reason"] = "SERVER_RESOURCE_MISSING"
        elif not corpus_path.is_file():
            result["reason"] = "CORPUS_RESOURCE_MISSING"
        else:
            result.update(command=[str(root / "node" / "bin" / "node"), str(server_path)],
                          resource_mode="DEV_SOURCE", reason="READY")
            return result
    elif server == "animejs" and root is not None:
        result["reason"] = bundled_reason or ("SERVER_RESOURCE_MISSING" if _read_manifest(root) else "RESOURCE_RESOLUTION_FAILED")
    return result


def launch_command(server: str, resource_root: str | Path | None = None) -> list[str] | None:
    """Return a command using only a bundled Node executable, or ``None``."""
    value = resolve_mcp_resources(server, resource_root)
    command = value.get("command")
    # Keep the low-level resolver backwards-compatible for callers that only
    # need the entrypoint. The orchestration preflight still rejects a bundled
    # Anime.js runtime whose corpus is absent using the diagnostic reason.
    if command is None and server == "animejs" and resource_root is not None:
        root = runtime_root(resource_root)
        if root is not None:
            metadata = _read_manifest(root)
            entrypoint = (metadata or {}).get("entrypoints", {}).get(server) if metadata else None
            if isinstance(entrypoint, list) and all(isinstance(item, str) for item in entrypoint):
                if not any(item.startswith(_RESOURCE_PREFIXES) and not (root / item).is_file() for item in entrypoint):
                    command = [str(root / "node" / "bin" / "node")]
                    command.extend(str(root / item) if item.startswith(_RESOURCE_PREFIXES) else item for item in entrypoint)
    return list(command) if isinstance(command, list) else None


mcp_resource_status = resolve_mcp_resources
