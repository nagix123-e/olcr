# OLCR custom agent execution runtime

This isolated Node 24 runtime adopts `@librechat/agents` for graph execution,
tool calling, context handling, session streaming, durable LangGraph SQLite
checkpoints, JSONL sessions and selected subagents. Python owns definitions,
model role resolution, credentials, tool permissions and telemetry.

From the OLCR checkout:

```sh
npm ci --prefix agent-runtime
python3 scripts/prepare_agent_runtime.py
npm --prefix frontend run build
```

Only Node's binary setup and better-sqlite3 native installation scripts are
allowed by package.json. Provider setup scripts are not required. The prepared
runtime includes its own pinned Node binary; it does not modify the Node 20
runtime used by existing MCP servers. Install/build on the release target OS
and CPU architecture; native SQLite and Node files are not portable binaries.
The first preparation downloads the exact upstream Node license notice.

`OLCR_AGENT_RUNTIME` can select a prepared runtime directory. Otherwise the
backend resolves the installed OLCR runtime and then the source checkout.
The release builder includes `agent-runtime`, its manifest and license inventory.
The existing installed app is not replaced by these source preparation steps.

`runner.mjs` uses a private JSONL stdin/stdout channel. Its environment excludes
provider credentials, tracing keys and other application secrets. The child
requests model and tool execution from Python; it never selects model servers,
reads OLCR credentials or receives an externally accessible callback endpoint.

Conversation state lives under the selected profile's `agent-sessions/` directory.
Definition snapshots and selected child snapshots are retained in OLCR SQLite.
Existing conversations keep the definition revision with which they started.
Cancellation is cooperative at model/tool boundaries; a running host model call
finishes before the next graph step is prevented.
