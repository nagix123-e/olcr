"""Resolve the immutable, bundled Serena runtime without host-Python fallback."""
from __future__ import annotations
import os
from pathlib import Path

SERENA_VERSION = "1.7.0"

def runtime_root(resource_root: str | Path | None = None) -> Path | None:
    """Return a completed bundled runtime only when its marker is present.

    Development may explicitly provide ``OLCR_MCP_RUNTIME_ROOT``.  Packaged
    callers provide the resource root; no system Python, pip, or downloads are
    considered valid fallbacks.
    """
    roots = [Path(resource_root)] if resource_root else []
    if os.environ.get("OLCR_MCP_RUNTIME_ROOT"):
        roots.insert(0, Path(os.environ["OLCR_MCP_RUNTIME_ROOT"]))
    for root in roots:
        candidate = root / "mcp-runtime" / "serena"
        marker = candidate / "runtime-manifest.json"
        python = candidate / "python" / "bin" / "python3"
        if marker.is_file() and python.is_file(): return candidate
    return None

def launch_command(resource_root: str | Path | None = None) -> list[str] | None:
    root = runtime_root(resource_root)
    if root is None: return None
    metadata = (root / "runtime-manifest.json").read_text(encoding="utf-8")
    # Serena's entrypoint is recorded by preparation after inspecting the
    # installed distribution.  The resolver never guesses a module name.
    import json
    entrypoint = json.loads(metadata).get("entrypoint")
    if not isinstance(entrypoint, list) or not all(isinstance(item, str) for item in entrypoint): return None
    return [str(root / "python" / "bin" / "python3"), *entrypoint]
