# OLCR

症状相談の試験機能（OpenSymptom参考リンク＋Infermedica接続）は、[設定・利用方法](docs/SYMPTOM_CONSULTATION.md)を参照してください。既定では無効です。

Local-first cognitive workspace for **macOS Apple Silicon (arm64)**. OLCR starts from Terminal, keeps its service on localhost, and uses Ollama models installed on your Mac. Semantic retrieval is experimental.

## Quick Start

### Release installation: macOS Apple Silicon

OLCR v0.4.5 supports macOS Apple Silicon (arm64) only. Download the release archive, then run:

```sh
cd /path/to/downloaded/archive
shasum -a 256 olcr-v0.4.5-macos-arm64.tar.gz
tar -xzf olcr-v0.4.5-macos-arm64.tar.gz
cd olcr-v0.4.5-macos-arm64
./install.sh
olcr
```

The release archive is an executable distribution, not a source checkout: do not run `pip install -e .` inside it. The installer is user-space only. It installs the immutable runtime under `~/Library/Application Support/OLCR/runtime/0.2.3/` and a stable launcher at `~/.local/bin/olcr`; it never edits your shell profile. If the launcher directory is not on `PATH`, the installer prints the exact `export PATH=...` command to use.

### Upgrade to the latest version

Exit any running OLCR REPL, download `olcr-v0.2.8-macos-arm64.tar.gz`, verify its SHA-256 when available, then install it:

```sh
cd /path/to/downloaded/archive
shasum -a 256 olcr-v0.2.3-macos-arm64.tar.gz
tar -xzf olcr-v0.2.3-macos-arm64.tar.gz
cd olcr-v0.2.3-macos-arm64
./install.sh
hash -r 2>/dev/null || true
olcr --version
# expected: 0.4.5
olcr status
```

Re-running the installer preserves existing workspace, settings, and model selections.

OLCR v0.1.x is currently distributed without Apple notarization. Browser and GitHub downloads may carry the macOS `com.apple.quarantine` attribute, which can prevent the bundled runtime from launching. Before running it, `install.sh` prints a warning and removes that attribute only from OLCR's installed runtime and its OLCR-managed launcher. It does not disable Gatekeeper system-wide and does not remove quarantine from Downloads, your home directory, Ollama, or other unrelated files. Running `install.sh` constitutes consent to this documented installation operation.

OLCR bundles its Python runtime, pinned Laya decision runtime/checkpoints, Python dependencies, and frontend assets. It does **not** bundle Ollama or Ollama model blobs. Install Ollama separately, then install the active external models:

```sh
ollama pull qwen3.6:latest
ollama pull embeddinggemma:latest
```

`qwen3.6:latest` is the packaged default main model. `qwen3.8:latest` remains a supported user-selectable alternative; semantic/vector features remain opt-in. Run `olcr status`, `olcr search "prompt allocation policy"`, `/help`, or `/status` after installation. First launch asks for an authorized workspace and an optional explicit core-context snapshot.

Workspace authorization and startup flow:

```sh
mkdir -p "$HOME/Downloads/file to make tetris in"
olcr
# enter the workspace path when prompted, choose core context or skip it,
# verify workspace READY, then enter your implementation request
```

From an existing REPL use `/workspace set "/Users/you/Downloads/file to make tetris in"`. For paths containing spaces, the quoted form is recommended: `/workspace set "/Users/name/Downloads/project folder"`. Shell-escaped paths may show spaces as `/Users/you/Downloads/file\ to\ make\ tetris\ in`; the backslashes are escaping, not directory-name characters.

For ordinary chat and generation, OLCR defaults to `qwen3.6:latest`. A non-empty saved model setting takes precedence over `OLLAMA_MODEL`; a non-empty `OLLAMA_MODEL` takes precedence over the packaged default. Empty saved model values are treated as unset. OLCR uses one settings database at `~/Library/Application Support/OLCR/olcr.db` (or the explicit `OLCR_DB_PATH`) and permits local model requests to run for up to 750 seconds.

Re-running `install.sh` is safe and preserves workspace/core-context user data. To remove the executable/runtime, remove `~/.local/bin/olcr` and `~/Library/Application Support/OLCR/runtime/`; leave `~/Library/Application Support/OLCR/workspaces/` intact unless you explicitly want to remove your user data.

### Development/source installation

For the desktop development build, use `scripts/start-dev-independent.sh` when it should keep running after the terminal or ChatGPT/Codex session closes. It starts the dev server under the current user's `launchd` session and writes logs to `/tmp/olcr-dev-independent.*.log`. Stop that managed run with `launchctl remove local.olcr.dev-independent`. The regular `npm --prefix frontend run tauri:dev` command remains attached to its invoking terminal.

Use Python 3.12 or newer. The local LFM audio runtime requires Python 3.12+.

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

Native MLX models use a separate Python environment (`OLCR_MLX_PYTHON`, or `.venv-mlx/bin/python`). Install `backend/requirements-mlx.txt` in that environment for JSON Schema constrained generation and local PLaMo translation. Missing grammar support fails closed for coding operations; missing PLaMo support is surfaced as a translation runtime error. Dependency-free HTML/JavaScript coding tasks use the prepared Node.js/Chromium runtime for executable acceptance checks.

## Prerequisites

- macOS Apple Silicon / arm64
- Python 3.12+
- Ollama installed separately, with local models you choose to use
- `embeddinggemma:latest` for experimental semantic retrieval
- `qwen3.8:latest` for local answer generation

OLCR runs locally and has no cloud-inference fallback. API routing uses EmbeddingGemma candidate retrieval followed by the pinned in-process Laya Choice provider; if either local runtime is unavailable, health is explicitly degraded and the API gate cannot pass. If Ollama or an external Ollama model is unavailable, lexical retrieval remains usable where applicable.

## Personal assistant features

[タスク・目標・プロフィール・フォーム入力・ツール作成はチャットから操作できます](docs/chat-personal-workflows.md)。会話内で必要事項を聞き取り、内容の確認後に既存サービスを実行します。[会話の記憶](docs/conversation-memory-settings.md)の参照範囲・関連度を会話から変更し、[主要通販の接続](docs/service-connectors.md)もチャットから開始できます。プロジェクトをまたぐ記憶は初期状態ではOFFで、OFFでも保存済みの記憶は残ります。定期タスクはOLCRバックエンドの起動中に実行されます。通販接続はサービスごとのAPI対応範囲に従い、専用ブラウザーでのログインと購入確定はユーザーが行います。

## Travel itinerary planning

チャットで「旅行計画を立てて」と依頼すると、条件を保存し、出典付きの日別旅程案を作成・修正できます。使い方と対応範囲は [旅行計画](docs/TRAVEL_PLANNING.md) を参照してください。

## Coding Task review and GitHub

Coding Tasks now support file diffs, safe restore, durable follow-up instructions, isolated local worktrees and reviewed GitHub pull requests. See [Local coding workflow](docs/LOCAL_CODING_WORKFLOW.md) for usage, APIs and workspace behavior.

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

Core context is never discovered automatically. You may enter it directly, load one explicit file inside the authorized workspace, or skip it. `/context load` also accepts a ZIP inside the authorized workspace and reads eligible text members directly without extraction or execution; unsafe paths and binary members are skipped. File-backed context is snapshotted; use `context reload` when you deliberately want to refresh it.

## Attachment format conversion

Turn **Convert** ON in Settings → Chat capabilities. OLCR downloads and starts
its private conversion environment automatically (several GB on first enable).
Convert is OFF by default; Docker installation is not required. Attach a file and ask `pdfに変えて` or `Convert this to png`.
OLCR converts the original bytes without a language model and saves results in
the project's configured workspace, or `~/Downloads` when none is configured.
Existing files are preserved with numbered filenames. See
[file-conversion.md](docs/file-conversion.md) for limits, setup, and service details.

## Development checks

```sh
cd backend && python3 -m unittest discover -s tests -v
cd ../frontend && npm run typecheck && npm run build
```

## License

OLCR source is licensed under the Apache License 2.0. Bundled third-party components and the Laya checkpoints remain subject to their own licenses; release artifacts include their collected notices and license materials.
