# Coding boundary defects — 2026-10-30

This audit followed the recurring meal-planner verification failures. It found further defects despite the prior test run passing.

## Corrections

1. The previous explicit-retry fix compared a human-recovery counter that normal VERIFICATION_CONTRACT_INVALID resumes did not increment. Its earlier test directly changed that counter, missing the actual resume flow. Verification retries now have a dedicated persisted resume-state epoch incremented only when the explicit resume controller accepts a RESUMABLE verification failure. Automatic repetition within that epoch remains blocked. The regression now invokes the real resume controller before attempting check repair, while retaining source and passing checks. This does not reset replan/global permission budgets.
2. Verification failure context included only the first two buttons/fields, 160 characters of page text and the first line of the error. This could remove the actual recipe-list control and failing locator from the repair prompt. Bounded context now includes up to 32 buttons, 16 fields and 1200 characters of page/error text. A regression preserves a fourth recipe-list button and a locator on a later error line.
3. Generation.cancel previously stopped iterating callbacks after one cleanup exception. It now attempts all cancellation callbacks while leaving cancellation authoritative. A failure-injection test verifies other resources are closed even if one callback raises.
4. Optimistic prompt removal happened before asynchronous preparation finished. The prompt is now replaced only when the submission writes its authoritative user message, preserving display during preparation. Matching accepts the exact original text as well as normalized request text to avoid duplication for whitespace-preserving agent requests.

## Validation

- 27 backend modules: 553 collected, 548 passed, 5 existing obsolete-lifecycle skips.
- 9 frontend modules: 134 passed; the affected conversation/API suites were rerun after the final text-matching adjustment (32 passed).
- TypeScript and diff whitespace checks passed.
- Node and Chromium acceptance tests actually executed in temporary workspaces with isolated DBs. The installed browser path was injected into the isolated browser resolver. Model output/review in orchestration unit tests is a fixture, not proof of a real model's judgment.

No user application sources, real calendar entries, credentials or live task state were changed. No long-running real-model meal-planner generation was completed in this audit. The running OLCR app requires rebuild/restart to receive changes. Thinking modes and acceptance requirements were not weakened.
