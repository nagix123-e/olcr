# OLCR

Local-first cognitive workspace for **macOS Apple Silicon (arm64)**. OLCR starts from Terminal, keeps its service on localhost, and uses Ollama models installed on your Mac. Semantic retrieval is experimental.

## Quick Start

### Release installation: macOS Apple Silicon

OLCR v0.2.2 supports macOS Apple Silicon (arm64) only. Download the release archive, then run:

```sh
cd /path/to/downloaded/archive
shasum -a 256 olcr-v0.2.2-macos-arm64.tar.gz
tar -xzf olcr-v0.2.2-macos-arm64.tar.gz
cd olcr-v0.2.2-macos-arm64
./install.sh
olcr
```

The release archive is an executable distribution, not a source checkout: do not run `pip install -e .` inside it. The installer is user-space only. It installs the immutable runtime under `~/Library/Application Support/OLCR/runtime/0.2.1/` and a stable launcher at `~/.local/bin/olcr`; it never edits your shell profile. If the launcher directory is not on `PATH`, the installer prints the exact `export PATH=...` command to use.

### Upgrade to the latest version

Exit any running OLCR REPL, download `olcr-v0.2.2-macos-arm64.tar.gz`, verify its SHA-256 when available, then install it:

```sh
cd /path/to/downloaded/archive
shasum -a 256 olcr-v0.2.2-macos-arm64.tar.gz
tar -xzf olcr-v0.2.2-macos-arm64.tar.gz
cd olcr-v0.2.2-macos-arm64
./install.sh
hash -r 2>/dev/null || true
olcr --version
# expected: 0.2.2
olcr status
```

Re-running the installer preserves existing workspace, settings, and model selections.

OLCR v0.1.x is currently distributed without Apple notarization. Browser and GitHub downloads may carry the macOS `com.apple.quarantine` attribute, which can prevent the bundled runtime from launching. Before running it, `install.sh` prints a warning and removes that attribute only from OLCR's installed runtime and its OLCR-managed launcher. It does not disable Gatekeeper system-wide and does not remove quarantine from Downloads, your home directory, Ollama, or other unrelated files. Running `install.sh` constitutes consent to this documented installation operation.

OLCR bundles its Python runtime, Python dependencies, frontend assets, and the experimental Qwen reranker. It does **not** bundle Ollama or Ollama model blobs. Install Ollama separately, then install the active external models:

```sh
ollama pull qwen3.6:latest
ollama pull embeddinggemma:latest
```

`qwen3.6:latest` is the packaged default main model. `qwen3.8:latest` remains a supported user-selectable alternative; semantic/vector features remain opt-in. Run `olcr status`, `olcr search "prompt allocation policy"`, `/help`, or `/status` after installation. First launch asks for an authorized workspace and an optional explicit core-context snapshot.

For ordinary chat and generation, OLCR defaults to `qwen3.6:latest`. A non-empty saved model setting takes precedence over `OLLAMA_MODEL`; a non-empty `OLLAMA_MODEL` takes precedence over the packaged default. Empty saved model values are treated as unset. OLCR uses one settings database at `~/Library/Application Support/OLCR/olcr.db` (or the explicit `OLCR_DB_PATH`) and permits local model requests to run for up to 750 seconds.

Re-running `install.sh` is safe and preserves workspace/core-context user data. To remove the executable/runtime, remove `~/.local/bin/olcr` and `~/Library/Application Support/OLCR/runtime/`; leave `~/Library/Application Support/OLCR/workspaces/` intact unless you explicitly want to remove your user data.

### Development/source installation

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

OLCR runs locally and has no cloud-inference fallback. The reranker remains experimental, but the macOS arm64 release archive bundles Qwen3-Reranker-0.6B for offline local loading. If Ollama or an external Ollama model is unavailable, lexical retrieval remains usable where applicable.

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

## License

OLCR source is licensed under the Apache License 2.0. Bundled third-party components and the experimental reranker remain subject to their own licenses; release artifacts include their collected notices and license materials.
