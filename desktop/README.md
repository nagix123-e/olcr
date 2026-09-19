# OLCR desktop development

Node MCP development resources use the existing repository layout
`<OLCR root>/mcp-runtime/node`. Assemble that destination with
`scripts/prepare_node_mcp_runtime.py NODE_ARCHIVE LOCKED_NODE_MODULES BROWSER_ROOT mcp-runtime/node`.
The generated directory is ignored by Git and persists across Desktop restarts.
The desktop passes the normalized repository root as `OLCR_NODE_MCP_RUNTIME_ROOT`;
an explicit override must name a root containing `mcp-runtime/node`, not the
`node` directory itself. Invalid overrides fail closed. `MCP_RESOURCE_MODE=DEV_SOURCE`
selects the source-owned Anime.js resources with the prepared Node executable.

The Tauri shell owns one Python backend child and passes a fresh per-launch API credential only through its child environment and the in-memory frontend bridge. It does not read SQLite or parse CLI output.

From the OLCR source root:

```sh
npm --prefix frontend install
OLCR_PYTHON="$(pwd)/.venv/bin/python" npm --prefix frontend run tauri:dev
```

`OLCR_PYTHON` is deliberately required; the shell will not silently select an arbitrary system interpreter. The child binds to `127.0.0.1` only. A foreign process on port 8000 is never terminated; startup reports failure instead.
