# r46 GUI E2E record: reviewed cases 201–269

- Date: 2026-10-11 (JST)
- Input: rows 201–269 from `SEMANTIC_REPAIRS_201_FAIL_68_BLOCKED_20261011.json` (69 reviewed cases; source global indices 270–352)
- Build: `olcr-e2e-semantic-r46-20261011`
- Bundle: `/private/tmp/olcr-e2e-r46-20261011-target/debug/bundle/macos/OLCR.app`
- Isolated profile/database: `/private/tmp/olcr-e2e-r46-20261011-profile/olcr.db`
- Projectless conversation: `903f51ce-3ff4-4731-b821-12d59412ed3f` (different from the previous 100-case conversation `3cced4fb-9494-4196-8d23-3f1dd997cb53`)
- Backend: `127.0.0.1:54396`, PID `44795`; app PID `44153`
- Launcher runtime-bindings diagnostic: `FAIL` (reported by the launcher as diagnostic; launcher health and `READY_FOR_COMPUTER_USE=YES` passed)
- Launcher: `/private/tmp/olcr-e2e-r46-20261011-launch.log`
- API run window: 2026-10-11 07:53:13–09:05:46 UTC (16:53–18:05 JST)

## Results

| Result | Count |
|---|---:|
| Source cases | 69 |
| Submitted through visible GUI | 1 |
| Submitted sequentially through the isolated backend | 68 |
| GUI generation loader visibly shown | Yes |
| API HTTP 200 | 65 of 68 |
| API client timeout (HTTP status unavailable) | 3 |
| API task state completed | 23 |
| API task state failed | 42 |
| API task state unavailable (timeout) | 3 |
| Assistant answers persisted in the isolated DB | 66 of 69 |
| Cases with no persisted assistant answer | 3 |
| Semantic pass/fail audited | No |

The three API timeouts were `politics.br_chamber#3` (batch index 14, source global index 287), `politics.br_chamber#4` (index 15, global index 288), and `econ.us_labor#4` (index 53, global index 332). They were not retried. Each lacked a persisted assistant answer at final DB reconciliation.

HTTP 200 records include both completed and failed tasks; transport success is not a semantic pass. The failed task states were preserved as failures. The GUI-submitted first case `finance.ecb#2` completed and its response was persisted.

## Authentication and execution notes

The first API-runner invocation produced 68 HTTP 401 responses because the session credential was extracted incorrectly. Those attempts were rejected before case execution: the database still had only the GUI message and two GUI-originated task rows total (one task row was not attached to a conversation), with no API user messages from the rejected attempts. The credential was then extracted using the established 48-character format, verified with a read-only `GET /api/projects` preflight (HTTP 200), and all 68 API cases were submitted once in the retry log. The initial 401 log is retained separately and is not counted as a case execution.

The GUI-created projectless conversation contains 69 user messages and 66 assistant messages. Its database contains 91 task rows (27 completed, 64 failed); the failed task rows report `WEB_PROVIDER_NOT_CONFIGURED` 42 times and `CREDENTIAL_SETUP_REQUIRED` 22 times. More than one task row may belong to a prompt because provider recovery creates additional tasks. The per-case record includes the database task rows and persisted-answer status for reconciliation.

## Artifacts

- All 69 cases in source order, including response events, DB task states, and persisted-answer status: `.live/olcr-e2e-r46-20261011-semantic-201-269-all69.jsonl`
- Aggregate counts: `.live/olcr-e2e-r46-20261011-semantic-201-269-summary.json`
- Exact reviewed 69-case input snapshot: `.live/olcr-e2e-r46-20261011-semantic-201-269-source.jsonl`
- GUI case: `.live/olcr-e2e-r46-20261011-semantic-201-269-gui-case.jsonl`
- API retry log and summary: `.live/olcr-e2e-r46-20261011-semantic-201-269-api-retry.jsonl` and `.live/olcr-e2e-r46-20261011-semantic-201-269-api-retry-summary.json`
- Initial unauthorized attempts (not counted as executed cases): `.live/olcr-e2e-r46-20261011-semantic-201-269-api.jsonl`
- Isolated SQLite DB: `/private/tmp/olcr-e2e-r46-20261011-profile/olcr.db`
