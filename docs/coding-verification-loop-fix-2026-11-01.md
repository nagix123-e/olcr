# Coding verification loop follow-up — 2026-11-01

## Observed failure

The user reported another coding task that appeared to loop during its final verification phase. The task remained at 2/3 completed phases. Read-only inspection of its stored progress showed 12 `planner_replanner` invocations and 5 `long_implementation` invocations; the largest observed planner input was 18,120 tokens. Prefill for the last Bonsai call advanced through 14,468/14,469 tokens before generation began, so the captured state was repeated expensive work rather than an idle inference-slot wait.

The phase had 18 verification criteria. `_host_verification_phase` allowed `max(4, 2 * len(criteria)) + 1` authoring-loop iterations, up to 37, and recursively re-entered itself for unreviewed criteria. Each recursive call recreated the local attempt and review counters. This made the nominal retry bound ineffective across the full phase and let repeated parse, selector, and failed-check repairs consume an hour while the phase stayed visibly unchanged.

The task was already `RESUMABLE` with a user pause request when inspected. Its source project and task data were left untouched; this repair does not resume that task.

## Repair

- Share one verification-call budget across recursive semantic-review passes: at most 12 model calls, 7 check-authoring calls, 6 model-based reviews, and 2 semantic recursion passes. The authoring budget is sized at two attempts per eight-criterion batch, with a minimum of 3 and a phase-wide hard limit. Deterministic source-evidence reviews do not consume a model-call slot.
- Keep a failed batch bounded, but retain all failed criterion IDs for its repair. Shrinking every repair to a single criterion made the old batch-derived allowance run out before the remaining contract was authored. A single batch can now be retried without discarding the other uncovered criteria, while the phase-wide budget still stops repeated failure.
- Persist the call counts and stop reason with verification-preparation failures so a same-source resume can fail fast with retained work.
- Emit localized progress counters for check authoring and independent review so the UI shows the current attempt and phase-wide call count.

## Follow-up evidence

After the first change, the same 18-criterion phase used 4 authoring calls and 3 reviews, then stopped with 11 criteria uncovered. Its first response had three failed IDs from one shared Playwright `setViewportSize` misuse. The repair path narrowed that failure set to one ID per call, while the budget assumed eight-ID batches. A later syntax error also consumed an authoring attempt. The follow-up aligns repair batch size and the retry allowance, without removing the bounded stop.

## Scope

Only OLCR verification orchestration and its progress presentation changed. No user-project source, existing task record, or application data was edited. The normal latest-source app launch follows the repository's independent launcher procedure.
