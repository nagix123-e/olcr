# r45 GUI E2E record: reviewed cases 101–200

- Date: 2026-10-11 (JST)
- Input: rows 101–200 from `SEMANTIC_REPAIRS_201_FAIL_68_BLOCKED_20261011.json` (269 reviewed cases; source global indices 161–269)
- Build: `olcr-e2e-semantic-r45-20261011`
- Bundle: `/private/tmp/olcr-e2e-r45-20261011-target/debug/bundle/macos/OLCR.app`
- Isolated profile/database: `/private/tmp/olcr-e2e-r45-20261011-profile/olcr.db`
- Projectless conversation: `3cced4fb-9494-4196-8d23-3f1dd997cb53` (created separately from the prior 100-case conversation)
- Backend: `127.0.0.1:52824`, PID `29781`; app PID `29177`
- Run window: 2026-10-11 13:19–15:17 JST

## Results

| Result | Count |
|---|---:|
| Source cases | 100 |
| Submitted through visible GUI | 1 |
| Submitted sequentially through the isolated backend | 99 |
| GUI loader visibly shown while generating | Yes |
| API HTTP 200 | 96 |
| API client timeout (HTTP status unavailable) | 3 |
| API responses whose task state was completed | 50 |
| API responses whose task state was failed | 46 |
| Assistant answers persisted in the isolated DB | 97 of 100 |
| Cases with no persisted assistant answer | 3 |
| Semantic pass/fail audited | No |

The three client timeouts were `health.clinical_trials#4` (source global index 168), `policy.us_federal_register#2` (206), and `econ.cepalstat#4` (260). In all three cases, both server-side task rows later showed `completed`, but no assistant answer was persisted in the conversation. They were not retried.

HTTP 200 is a transport result, not a semantic pass. The 46 HTTP 200 responses with task state `failed` were preserved as failures.

## Isolated database record

- User messages: 100; assistant messages persisted: 97.
- Task rows for this conversation: 108 (54 completed, 54 failed).
- Failed task reasons: `WEB_PROVIDER_NOT_CONFIGURED` (45), `CREDENTIAL_SETUP_REQUIRED` (8), and `ARGUMENT_COMPILATION_FAILED` (1).

## Artifacts

- All 100 cases in source order, including prompts, API SSE events, per-case database tasks, and persisted-answer status: `.live/olcr-e2e-r45-20261011-semantic-101-200-all100.jsonl`
- Aggregate counts and the three missing-answer cases: `.live/olcr-e2e-r45-20261011-semantic-101-200-summary.json`
- Exact 100-case source snapshot: `.live/olcr-e2e-r45-20261011-semantic-101-200-source.jsonl`
- API source, raw SSE log, and runner summary: `.live/olcr-e2e-r45-20261011-semantic-101-200-api-source.jsonl`, `.live/olcr-e2e-r45-20261011-semantic-101-200-api.jsonl`, and `.live/olcr-e2e-r45-20261011-semantic-101-200-api-summary.json`
- GUI case record: `.live/olcr-e2e-r45-20261011-semantic-101-200-gui-case.jsonl`
- Launch/build log: `/private/tmp/olcr-e2e-r45-20261011-launch.log`
- Isolated SQLite DB: `/private/tmp/olcr-e2e-r45-20261011-profile/olcr.db`
