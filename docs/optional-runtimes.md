# Optional local engines

OLCR ships small download recipes in `backend/olcr_api/runtime_recipes`, not
agent Node dependencies, Node MCP binaries, browser binaries or Laya weights.
The main Python runtime and its Python dependencies remain part of the core.

Only the multilingual Laya checkpoint is used. English input uses that same
loaded checkpoint; even an existing English checkpoint is ignored. On initial
API startup, missing Laya weights are prepared in a background setup job, then
loaded into the decision provider. Chat and Settings remain available during
setup; decision-dependent operations retain their existing readiness checks.

The agent engine is acquired on the first message to a bound custom agent.
Creating/importing/exporting definitions does not acquire the engine. Reference
tools acquire the Node tool engine on their first authorized invocation.
Starting a managed browser acquires both that engine and a separate browser
component. Read-only inventories and diagnostics never initiate downloads.

Settings → External tools shows component status and allows explicit preparation
or retry. Active downloads and failures also appear in the chat's existing status
area. Convert retains its separate OFF-by-default setting and VM lifecycle.

Components live under the current app profile's `components/`, versioned by the
recipe identity. `OLCR_COMPONENTS_ROOT` is an optional explicit deployment/test
override. Existing compatible local resources may still be resolved, but release
staging no longer requires or includes these optional engines. Components remain
installed across core updates and are reused offline.

Sources are pinned Node distributions, integrity-pinned npm lockfiles, the fixed
Laya Hugging Face revision, the fixed Playwright browser archive, and the fixed
better-sqlite3 prebuilt binding. SHA256 checks precede extraction/execution; npm
install scripts are disabled. Agent SQLite needs no host compiler. Internal Node
launch paths avoid global Node/npm and developer tooling. Downloaded package
licenses and Node/browser notices remain with each component. Archives reject
path and link escapes. Interrupted/failed staging is never promoted; successful
installs are atomically renamed and process-locked. Retry reuses verified downloads.

The current native recipes target macOS arm64, matching the release builder.
Other native architectures receive an explicit unsupported-platform status.

Verification includes clean first-use downloads of all four components, a real
LibreChat agent run with a fake local model callback and SQLite checkpoint, real
Japanese/English Laya inference with exactly one loaded checkpoint, and a headless
browser rendering a local in-memory page. No external model API is used.
