# r35 GUI E2E: partial 152-case record

- Build: `olcr-e2e-352-20261010-r35` (isolated GUI/backend).
- Scope: source cases 1–152 of 352, one projectless conversation.
- Statuses below are reconstructed from the live execution output visible in the task transcript. This is **not** a semantic pass/fail audit.
- The temporary directory was externally cleaned after interruption; the r35 JSONL, summary, backend log, isolated database, and app bundle are no longer available. Consequently route, typed arguments, provider evidence, and final-response logs cannot be recovered for these 152 rows. One case state was truncated in the console capture.
- Durable recovered row metadata: `../.live/olcr-e2e-r35-partial-152.jsonl` (ignored by Git).

## Recovered accounting

| Recorded state | Count | Cases |
|---|---:|---|
| Completed task (not semantically audited) | 141 | Remaining cases in rows 1–152 |
| Task failed | 2 | `calendar.public_holidays#4`, `security.exploit_probability#4` |
| Waiting for confirmation | 5 | `space.iss_position#3`, `earth.streamflow#4`, `biology.occurrence#2`, `crypto.bitcoin_network#3`, `media.tv#2` |
| Client timeout (240 s; not retried) | 3 | `art.met_collection#2`, `econ.oecd#3`, `ocean.tides#4` |
| State unavailable from truncated output | 1 | `security.exploit_probability#3` |
| **Total** | **152** | |

HTTP status was 200 for 149 rows and timed out for 3. These counts reflect only recorded task/transport state; they do not establish correct API selection, arguments, evidence, or visible-answer quality.
