# Fuzzy routing E2E cumulative record: cases 1–352

This index records the 352 previously unresolved routing prompts executed in three runs. Counts below are execution/task states, not semantic correctness results.

## Runs

| Global cases | Run | Completed | Failed | Waiting for confirmation | Client timeout | State unavailable | HTTP 200 |
|---|---|---:|---:|---:|---:|---:|---:|
| 1–152 | r35 partial | 141 | 2 | 5 | 3 | 1 | 149 |
| 153–252 | r37 | 89 | 8 | 2 | 1 | 0 | 99 |
| 253–352 | r39 | 93 | 1 | 6 | 0 | 0 | 99* |
| **Total** | | **323** | **11** | **13** | **4** | **1** | **347*** |

\* r39 has 99 HTTP 200 results from the backend-submitted cases; its first case was submitted through the GUI and is recorded as a completed GUI task without an HTTP status in the batch log. Across all runs, 348 cases produced HTTP 200 or a completed GUI submission, while four cases timed out. The table's HTTP 200 column sums only explicit HTTP statuses.

The r35 state-unavailable row is within its 149 HTTP 200 rows. The r37 and r39 completion counts include tasks that may have used fallback behavior or produced no data from the expected provider. An HTTP response or task `completed` state is not a semantic pass. No all-352 semantic pass audit is recorded here.

## Evidence retention

- **Cases 1–152 (r35):** The original JSONL, summary, backend log, isolated DB, and app bundle were removed by temporary-directory cleanup. The status counts are reconstructed from the captured run output; `.live/olcr-e2e-r35-partial-152.jsonl` retains recovered row metadata only. Route selection, arguments, provider evidence, and response logs cannot be recovered for this range. See [r35 partial record](R35_PARTIAL_GUI_E2E_152.md).
- **Cases 153–252 (r37):** Source prompts, runner output, evidence JSONL, summary, stdout, launch/backend logs, and isolated DB are retained under `.live/olcr-e2e-r37-20261010-*`. See [r37 run record](R37_GUI_E2E_153-252.md).
- **Cases 253–352 (r39):** Prompt source, raw runner output, consolidated 100-row log, DB-backed evidence, summary, stdout, launch/backend logs, and isolated DB are retained under `.live/olcr-e2e-r38-20261010-*` and `.live/olcr-e2e-r39-20261010-*`. See [r39 run record](R39_GUI_E2E_253-352.md).

## Combined accounting

| Outcome | Count |
|---|---:|
| Task completed | 323 |
| Task failed | 11 |
| Waiting for confirmation | 13 |
| Client timeout | 4 |
| State unavailable in r35 capture | 1 |
| **Total cases** | **352** |

The r35 and r37 outcomes are preserved from their existing execution records. The r39 row-level count is corroborated against its isolated database and batch output. The four timeouts were not retried as part of these runs.
