# MCP connection boundary and independent checks

## Findings

The prior answer described `MCP telemetry` as internal diagnostics without
checking connectivity. Its `AVAILABLE=NO` was not evidence that a connection
had failed: no task evidence also produced NO, and a FAILED lifecycle row could
produce YES in the per-server field while producing NO in the generic field.

Baseline direct stdio initialize / initialized / tools/list succeeded for the
prepared shadcn, Anime.js and managed-browser Playwright servers. There were
nevertheless genuine transport defects: first stdout line accepted regardless
of ID or message type; reusable/colliding request IDs; one outstanding reader
left behind per timeout; JSON-RPC errors counted as transport success; no
protocol-version validation or paginated discovery.

## Changes

- One stdout reader per child, serialized requests, monotonically increasing
  IDs and exact response matching. Notifications are counted without retaining
  their content. Ping is handled; unadvertised client methods are rejected.
- Supported protocol version negotiation and validated initialize/discovery
  results. JSON-RPC errors are failures and remain inspectable in the response.
- Timeout, invalid messages and process exit close the owned process/readers
  before an explicit reconnection. Paginated tools/list shares one time budget.
  Tool and environment allowlists remain enforced; stderr is drained without
  retaining potentially sensitive output.
- GET `/api/mcp/connections` inventories without launching. POST
  `/api/mcp/connections/{server}/check` launches only a registered bundled server,
  performs handshake/discovery in a temporary working directory, and closes it.
  It invokes no tool and needs neither a model nor a coding task.
- Settings → MCP connections exposes these checks, timestamp, tool count,
  failure cause and retry. Results distinguish NOT_CHECKED, CHECKING, CONNECTED
  (a successful completed check, not a persistent session), FAILED, DISABLED
  and UNAVAILABLE. Resource/command changes invalidate previous results.
- Historical task telemetry separates availability from usage and no longer
  equates unattempted with unavailable, nor reuses discovery from an older
  initialization attempt.

Playwright's check uses the existing managed-browser adapter; its generic
coding MCP policy remains separate and disabled. Serena and DevDocs are
policy-disabled and were not launched or advertised as connected. This source
checkout has no prepared Serena bundle. No missing servers were downloaded,
no models were changed, and no user project or user DB was modified.

## Validation

- 135 backend tests pass: MCP transport/connection/API, Node and Serena
  resolution, Anime.js capability, coding control plane/finalization and
  browser network regression. Tests use an isolated DB/app support directory.
- Includes real initialize/discovery against all three available bundled Node
  servers, interleaved notification/ping/response IDs, concurrent callers,
  process death, explicit reconnect after timeout, JSON-RPC errors, pagination
  and total discovery deadline. Real managed Playwright/Chromium tests exercise
  local JSON retrieval and redirect/subresource forwarding through the policy
  proxy; no public website is contacted by those tests.
- Seven frontend tests pass; TypeScript/Vite production build passes.
- Loopback/browser regression initially hit sandbox socket restrictions; the
  complete suite passed with the necessary execution permission. No skipped
  or failed cases remain in that final run.
- No native OLCR GUI acceptance run or model-driven autonomous MCP selection
  is claimed. See the adjacent validation JSON for actual connection results.

Protocol references: [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
and [stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
