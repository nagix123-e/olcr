# r44 GUI E2E record: semantic repair cases 1–100

- Date: 2026-10-11 (JST)
- Input: first 100 rows of `SEMANTIC_REPAIRS_201_FAIL_68_BLOCKED_20261011.json` (269 total; original global indices 2–160)
- Build: `olcr-e2e-semantic-r44-20261011`
- Bundle: `/private/tmp/olcr-e2e-r44-20261011-target/debug/bundle/macos/OLCR.app`
- Isolated profile/database: `/private/tmp/olcr-e2e-r44-20261011-profile/olcr.db`
- Conversation: `1b876a91-9383-4021-a145-658761d24b50` (projectless)
- Backend: `127.0.0.1:50094`, PID `14709`

## Results

| Result | Count |
|---|---:|
| Fixed source cases | 100 |
| Submitted through visible GUI | 1 |
| Submitted sequentially through the same isolated backend | 99 |
| API HTTP 200, task completed | 95 |
| API HTTP 500 | 3 |
| API client timeout (HTTP status unavailable) | 1 |
| Client-confirmed completed cases including GUI | 96 |
| Cases with an answer persisted in the isolated DB (includes the timed-out client request) | 97 |
| Semantic pass/fail audited | No |

The GUI case showed “Generating response” and the stop-generation control during processing; the answer then appeared. For the API batch, HTTP 200 and task completion are execution outcomes only, not semantic passes.

## Exceptions

| Full-batch index | Original global index | Case ID | Outcome |
|---:|---:|---|---|
| 46 | 89 | `games.trivia#1` | HTTP 500 `Internal Server Error`; no task or answer persisted |
| 47 | 90 | `games.trivia#2` | HTTP 500 `Internal Server Error`; no task or answer persisted |
| 48 | 92 | `games.trivia#4` | HTTP 500 `Internal Server Error`; no task or answer persisted |
| 83 | 135 | `econ.oecd#3` | Client timed out after 240 seconds; the isolated DB later showed both tasks completed and the answer persisted |

No exception was retried. The timeout remains a client-observed timeout and has no captured HTTP response, even though the server later persisted a completed answer.

## Artifacts

- All 100 rows in source order, including prompts, task state, responses, raw SSE, and global case indices: `.live/olcr-e2e-r44-20261011-semantic-001-100-all100.jsonl`
- Aggregate summary: `.live/olcr-e2e-r44-20261011-semantic-001-100-summary.json`
- Exact 100-case source snapshot: `.live/olcr-e2e-r41-20261011-semantic-001-100-source.jsonl`
- API batch raw SSE logs and its summary: `.live/olcr-e2e-r44-20261011-semantic-001-100-api.jsonl` and `...-api-summary.json`
- GUI case and DB evidence: `.live/olcr-e2e-r44-20261011-semantic-001-100-gui-case.jsonl`
- Launch/build log: `/private/tmp/olcr-e2e-r44-20261011-launch.log`
- Isolated SQLite DB: `/private/tmp/olcr-e2e-r44-20261011-profile/olcr.db`
