# Default MCP connection repair — 2026-11-09

The settings page previously required manual probes for three servers. Serena and DevDocs were disabled by the coding policy, Serena had no discoverable runtime, and DevDocs had no executable and incorrect expected tool names.

Diagnostic access now has a separate allowlist flag, so these servers can initialize and advertise tools without widening coding execution permissions. Serena uses a separate managed CPython 3.11.16 / serena-agent 1.7.0 component with SHA-256-locked dependencies. DevDocs vendors commit bf45371ef74ff7e96339397d5f057042ff081348, its MIT license and original source; transpiled ESM uses Node 20 native fetch and the already pinned MCP SDK 1.30.0. Node optional/release packaging includes this adapter.

Opening MCP Settings checks unverified/missing-resource entries sequentially. POST checks prepare missing environments; inventory remains read-only. Leaving the page stops queued checks, failed checks remain retryable, and success requires all expected tools. Child processes close after discovery.

Real probes: Serena 29 tools; DevDocs 5; Playwright 24; shadcn 7; Anime.js 4. All five returned CONNECTED/READY. See the adjacent validation JSON. Backend regression and release tests: 56 passed, including actual probes for all five servers. Frontend: 8 tests passed; typecheck and production build passed (existing chunk-size warning).

The running user app still requires a controlled restart to load the changed Python modules and bundled frontend. No user app was terminated during diagnosis. No model calls or MCP tool execution were used for verification.
