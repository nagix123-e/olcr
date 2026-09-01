# OLCR

Local-first cognitive workspace for **macOS Apple Silicon (arm64)**. OLCR starts from Terminal, keeps its service on localhost, and uses Ollama models installed on your Mac. Semantic retrieval is experimental.

## Quick Start

Open Terminal and run:

```sh
cd /path/to/lightweight-local-llm/olcr
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
olcr
```

On first launch OLCR asks for an existing workspace directory, explains that it may read only files inside that workspace, and optionally records a workspace-specific core-context snapshot. It then opens the `olcr>` prompt.

OLCR stores CLI state and core-context snapshots in `~/Library/Application Support/OLCR/`; it does not add configuration files to your workspace.

## Prerequisites

- macOS Apple Silicon / arm64
- Python 3.9+
- Ollama installed separately, with local models you choose to use
- `embeddinggemma:latest` for experimental semantic retrieval
- `qwen3.8:latest` for local answer generation

OLCR runs locally and has no cloud-inference fallback. The reranker is still experimental and is not yet bundled into a release archive. If Ollama or a model is unavailable, lexical retrieval remains usable where applicable.

## Terminal use

Interactive commands:

```text
/help
/status
/workspace show
/workspace set /absolute/path
/context show
/context set
/context load docs/project-context.txt
/context reload
/context clear
/models
/quit
```

One-shot commands:

```sh
olcr status
olcr workspace show
olcr workspace set /path/to/workspace
olcr context show
olcr context load docs/project-context.txt
olcr models
olcr search "prompt allocation policy"
```

Core context is never discovered automatically. You may enter it directly, load one explicit file inside the authorized workspace, or skip it. File-backed context is snapshotted; use `context reload` when you deliberately want to refresh it.

## Development checks

```sh
cd backend && python3 -m unittest discover -s tests -v
cd ../frontend && npm run typecheck && npm run build
```
