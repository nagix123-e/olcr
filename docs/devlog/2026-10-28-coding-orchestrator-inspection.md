# Coding orchestrator inspection and repair

Inspected the current planner/implementation boundary, typed phase evidence,
verification ownership, retry/replan budgets, persisted recovery, checkpoints,
scheduler teardown, finalization and frontend task progress controls. Graphify
was used to locate related components; current source and isolated executions
were used to assess behavior.

## Findings and changes

| Area | Failure | Repair |
| --- | --- | --- |
| Build evidence | The Vite executor performed a real build/typecheck, but those results were lost before the final required-verification gate. | Attach measured evidence in the Python host, outside check stdout; persist it in typed verification results and phase build/typecheck fields. Final typecheck acceptance consumes typed results. |
| Accumulated work | Phase review could accept retained work from earlier attempts, while completion evaluated only the latest report. | Review, saved artifact revalidation and completion now share chronological, revision-compatible phase evidence. Rolled-back history and different revisions cannot supply missing writes. |
| Saved PASS | A deleted artifact could repeatedly be skipped until completion rejected it. Applying all criterion heuristics indiscriminately also conflicted with owned blueprint checkpoints. | Recheck owned artifact/verification facts and recorded missing source files, while preserving orchestration-only blueprint completion. Resume recreates a missing artifact without inventing completion or replanning retained work. |
| Failure ownership | Invalid package/version declarations were classified as executor outages, and generic exception text discarded the package-manager diagnosis. | Return package source errors to implementation repair with bounded/redacted cause information. Network/auth/tool outages retain executor ownership. Invalid package metadata fails acceptance instead of crashing the host. |
| Result parsing | A valid outer verification JSON object could contain malformed nested test/observation/failure arrays and crash subsequent orchestration. | Validate container shapes before consuming the result; malformed summaries become typed executor failures. Successful read-only verification is also no longer logged as web-only implementation. |
| Concurrent source changes | The isolated Vite verifier could restore an old snapshot over an unrelated edit to the real source, including on timeout. | The isolated executor compares and invalidates changed targets without overwriting them. Existing in-place verifier restoration behavior remains available for those executors. |
| Terminal state | A late worker or telemetry error could move a committed COMPLETED task back into recovery. | Preserve completed/archived state; telemetry failure does not undo completion. Retain in-memory telemetry until its persistence succeeds. |
| Load and source inspection | Every phase eagerly scanned source for marketing structure, including dependency/generated code, even when that contract was absent. Dependency text could also satisfy project structure checks. | Evaluate only requested acceptance contracts; exclude dependencies, generated output and symlinks from structural source inspection, with bounded file/byte limits. |

The existing chat-first tests were also made loadable both as package modules
and through direct discovery. An API routing corpus test had a stale exact
case count (441 actual versus 440 expected); it still exercises every current
case while preserving the minimum coverage requirement.

## Validation

- Backend related suite: 593 cases, 587 passed and six existing skips, covering
  planner validation, implementation contracts, rollback, executor failures,
  local retries, checkpoint continuation, false-PASS recovery, phase progress,
  finalization and independent chat/API routing.
- After the final metadata/diagnostic guards, 30 focused cases passed. These
  overlap the broad suite except for one added metadata case: 594 distinct
  backend cases were exercised overall, with the same six skips.
- Frontend: all 36 tests passed across CodingTaskProgress, CodingTaskReview and
  PendingInstructions.
- Real React/Vite/TypeScript build, compiled module loading, Chromium button
  interaction and real Vitest success/intentional failure are included.
- Dedicated regressions cover accumulated artifact finalization, deleted-file
  recovery through the runner, completed-state protection, telemetry failure,
  malformed verifier results, concurrent edits/timeouts, dependency failure
  ownership, irrelevant source scanning and dependency/symlink decoys.
- Python compilation, acceptance-runner JavaScript syntax and diff whitespace
  checks passed.

All test database/settings profiles and source projects were temporary.
Installed Node/Chromium resources were reused; the new build tests do not
download dependencies or invoke generation providers. No live OLCR GUI
acceptance or long-running real-model coding benchmark was performed.
The existing user OLCR instance remains running and needs restarting to load
the changed backend modules.
