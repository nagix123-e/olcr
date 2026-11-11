# Local Thinking policy and MAX verification — 2026-10-30

MAX (`on`) now takes precedence over internal `requested=False` hints and structured output. Zero (`off`) remains off. Auto uses task role (planning and long implementation), explicit caller hints, request complexity cues including short Japanese checks/proofs/debugging requests, and request length. Runtime passes the original request separately so appended reference material alone does not inflate that length.

Coding planning/replanning/check authoring no longer hard-code `think=False`. The model's real role determines the effort slot, independently of the telemetry label. Fallback preserves the original request and caller hint. Local structured Thinking requests reserve at least 4096 output tokens when an explicit smaller budget was supplied, subject to the existing context-window validation.

MLX now runs free reasoning followed by a JSON Schema constraint in the same generation. The constraint skips the prompt and reasoning prelude, activates after the thinking delimiter, and retains a JSON prefix attached to the delimiter's token. Returned reasoning is kept when an early complete-JSON boundary stops generation. Both manual Bonsai and mlx_lm Ornith use the same gate. The live Bonsai test also revealed a terminal EOS marker in the visible answer; only the terminal stop token is removed, preserving literal markers inside generated source strings.

## Live checks

Installed models, local requests only; no settings were saved.

- Ornith Ollama: scoped MAX plus internal `think=False` and a JSON schema sent `think=true`, returned `{ "value": 391 }` for 17 × 23, with 230 reasoning characters.
- Ornith MLX: same calculation and JSON schema returned value 391, 13 reasoning characters, `json_complete`, about 7.3 seconds including loading.
- Bonsai MLX: same calculation and JSON schema returned value 391, 258 reasoning characters, about 12.8 seconds including loading.

The backend regression suite collected 308 tests: 303 passed and 5 obsolete lifecycle tests were skipped. UI tests passed 39 checks; TypeScript passed. Coverage includes mode/role/format priority, original-request isolation, real-token boundary handling, retained reasoning, fallback and coding execution contracts. These short live checks do not establish reliability or latency for long coding tasks.

Logs: `/private/tmp/olcr-thinking-max-final.log`, `/private/tmp/olcr-thinking-max-ui.log`, `/private/tmp/olcr-thinking-max-ollama-real.log`, `/private/tmp/olcr-thinking-max-mlx-real.log`, `/private/tmp/olcr-thinking-max-bonsai-real.log`.
