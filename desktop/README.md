# OLCR desktop development

The Tauri shell owns one Python backend child and passes a fresh per-launch API credential only through its child environment and the in-memory frontend bridge. It does not read SQLite or parse CLI output.

From the OLCR source root:

```sh
npm --prefix frontend install
OLCR_PYTHON="$(pwd)/.venv/bin/python" npm --prefix frontend run tauri:dev
```

`OLCR_PYTHON` is deliberately required; the shell will not silently select an arbitrary system interpreter. The child binds to `127.0.0.1` only. A foreign process on port 8000 is never terminated; startup reports failure instead.
