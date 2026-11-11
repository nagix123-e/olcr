# r28 fuzzy-routing E2E initial batch ledger

Date: 2026-10-09 (Asia/Tokyo)

## Scope and execution

- Planned acceptance matrix: 352 prompts (88 enabled APIs × 4 angles).
- Previously passing and removed from the unresolved queue: 20 cases.
- Initial unresolved queue: 332 cases.
- Submitted in the same projectless OLCR conversation: 332 cases total: 4 Diet cases through the GUI, 1 public-holiday case through the authenticated local chat endpoint, and 327 cases in the authenticated batch runner.
- Conversation ID: `0be00b3f-2764-4f04-aa5c-91bf766a59f1`.
- App bundle used: `/private/tmp/olcr-e2e-fuzzy352-20261009-r28-target/debug/bundle/macos/OLCR.app`.
- Batch log (327 JSONL records): `/private/tmp/olcr-fuzzy352-r28-authenticated-batch.jsonl`.
- Backend diagnostic log: `/private/tmp/olcr-e2e-fuzzy352-20261009-r28-profile/olcr-gui-fuzzy352-r28.log`.
- Isolated database: `/private/tmp/olcr-e2e-fuzzy352-20261009-r28-profile/olcr.db`.
- The initial unauthenticated transport probe returned HTTP 401 for 328 requests before routing/provider execution and is not counted as test execution. It made no chat/provider calls and is not evidence of provider failures.
- The 327-case batch reached the existing conversation and persisted responses. It used the live app's authenticated local chat endpoint rather than typing every prompt into the visible composer; therefore the router/provider/backend path was exercised, but rendering all 332 turns in the GUI is not yet verified.

## First-pass findings (before fixes)

These are observed failures or high-confidence defects, not an assertion that every source-free response is wrong:

1. One request timed out at the client boundary: `knowledge.devto#1` (300-second read timeout). It needs a bounded retry/retest.
2. Five natural-language content requests were stolen by Coding Task/task-control routing instead of reaching their APIs:
   - `games.trivia#3` and `render.quickchart#3` were classified as new Coding Tasks.
   - `development.idb#1`, `politics.br_senate#3`, and `knowledge.qiita#3` hit a pre-existing blocked-task response because generic words “進捗” or “状態” were treated as task control.
   - Two isolated Coding Task records were created by the first pair in the test DB. They were blocked; no repository files were modified by them.
3. Successful structured provider responses could render only a provider label, including `econ.eurostat#1–4` (“Eurostat”) and `ocean.tides#1–3` (“Ocean”). This is a data-to-text rendering defect, not a failed HTTP request.
4. US Congress requests containing `米議会` or a US bill identifier such as `S. 1234` could be stopped by the jurisdiction clarification guard, despite specifying the jurisdiction in ordinary user language.
5. The remaining source-free/empty/failure responses were not automatically classified as defects: some are legitimate clarification, provider-empty, rate-limit, upstream availability, or timeout outcomes. They remain queued until the saved response and backend diagnostics establish the cause.

## Batch-first repair loop

The execution policy is recorded in `E2E_EXECUTION_POLICY.md` and Graphify: run the entire unresolved set in one chat/build and save its results first; only then group root causes and apply fixes together; build once per repair batch; rerun the unresolved set and remove only cases whose behavior and logs both pass. No per-case rebuilds.

## Post-analysis checkpoint

- The first grouped source repair has been applied to task-intent classification, task-control interception, US legislative jurisdiction recognition, and provider summary rendering. Its focused deterministic regression set passed 57 tests.
- A full backend discovery run completed: 1,516 tests, 15 failures, 22 errors, 14 skipped. It is a failed suite, not a pass. The raw command output was truncated by the terminal integration, so the exact per-test diagnostics must be recovered with targeted reruns before treating any of those 37 failures as resolved.
- A coarse response-text grouping of the 327 records found 44 clarifications, 38 no-match responses, 24 provider-failure responses, 15 timeout-like responses, 5 credential gates, 5 task interceptions, 1 client timeout, and 195 other responses. These buckets are only triage; they are not pass/fail counts. 188 records have source metadata and 138 do not, which likewise does not determine correctness.
- Re-running `backend.tests.test_web` reproduced three regressions in fetched-source title selection and content fallback (39 total tests); a grouped renderer fix now uses fetched source titles, falls back to cleaned page excerpts when Brain composition is empty, and strips flattened navigation prefixes without dropping the factual paragraph. The full `test_web` module now passes 39/39.
- No post-fix bundle build or unresolved-batch rerun has yet occurred. The r28 test app/backend are still running as PID 96106 / 96161 on port 63774 under the isolated profile. The Mac is currently locked, so the exact GUI cannot be interacted with to close r28 and satisfy the repository's no-parallel-OLCR rule. Continue non-GUI source/test work; after unlock, close that exact test app through its UI, verify its PIDs/port exited, then build and test one fresh isolated bundle.
# Full backend-suite status (2026-10-10)

- `unittest discover -s backend/tests`: 1,516 tests, 15 failures, 22 errors, 14 skipped; runtime 3,728.6 s. The complete result was captured by the active test process, but output truncation means the terminal transcript is not a sufficient durable failure report. This suite is not a pass and must not be represented as one.
- Focused regression suite after the first grouped patch: 57 tests passed (`test_coding_tasks`, `test_fuzzy_api_routing_matrix`, `test_extended_external_tools`).
- Initial live-batch problem families confirmed across the full unresolved set: task-control interception of ordinary API prompts; provider-only / placeholder summaries; generic `REQUEST_SUCCEEDED` transport envelopes; lossy array/time-series values; incorrect or overly broad query arguments; false jurisdiction clarifications; provider/network/auth failures and timeouts (not automatically classified as app defects).
- Next action is a single grouped repair pass over source-addressable families, then one fresh build and one full unresolved-batch rerun in the same conversation. Cases with only upstream timeout/auth/provider failure remain unresolved unless a controlled retry or alternate in-scope source establishes correctness.

## R29 unresolved-set batch and grouped repair checkpoint (2026-10-10)

- R29 full unresolved-set execution: 332 cases, 332 HTTP 200, zero transport failures/timeouts, all in conversation `0be00b3f-2764-4f04-aa5c-91bf766a59f1`, app `/private/tmp/olcr-e2e-fuzzy352-20261010-r29-target/debug/bundle/macos/OLCR.app`, build `olcr-fuzzy352-r29-20261010`; no mock provider was used. Primary JSONL: `/private/tmp/olcr-fuzzy352-r29-authenticated-batch.jsonl`.
- Semantic audit: 193/332 passed the audit; 139 remain unresolved (127 no acceptable selected result, 12 wrong capability). These counts supersede the earlier rough “62 route misses” estimate; an HTTP 200 is not a semantic pass.
- Grouped backend fixes since R29: isolate standalone external-data questions from old transcript/project/coding evidence while preserving actual follow-up context; add the missing domain routes and argument compilation for F1, IMF/BIS/ECB, UNHCR, FEC, Section 230, IP metadata, Wayback years, New York Harbor vessel traffic, Kenya/Tanzania GDP; compile and fan out multi-location sunrise, elevation and METAR requests; support an all-reporter UnComtrade exporter rank; normalize every Qiita route consistently; do not assume Haneda for location-free “today's weather”.
- Focused fuzzy-routing matrix after the grouped changes: 18 tests passed, including all 352 permanent offline prompts and provider-fanout mocks. Qiita/router focused regression: 14 passed. A broader backend discovery run completed 1,521 tests in 80.8 s with 10 failures, 23 errors, 14 skipped; many failures are stale mocks of the now-read-only `PurposeDecisionProvider.loaded`, missing optional `pytest`/`jwt`, deliberately underspecified assumptions, migration fixtures with incomplete legacy schemas, or expected wording drift. The complete failure log is `/private/tmp/olcr-r29-backend-suite-r1.log`; do not call the full suite green.
- R29 remains the currently visible GUI build and does not contain these source changes. No build has yet been made from this grouped patch, and no post-fix GUI batch has been executed. Keep all 139 audit misses active; classify provider timeout/rate-limit/auth failures separately from OLCR defects, but do not remove them until retested or explicitly blocked in the ledger.
- Holiday scope rule: explicit worldwide “today” requests are supported without a country and must not be redirected into a clarification. Existing offline regression `test_global_holiday_request_routes_worldwide_without_country_clarification` passes. A missing scope and a full-year worldwide catalog are distinct cases.
- R29 source regression rerun after the F1/IMF/ECB argument-compiler batch: 211 tests, 204 passed, 7 skipped, 0 failures. The first rerun exposed eager construction of all provider specs invoking F1/IMF validation for unrelated tools; validation now runs only for the selected adapter. Added `test_provider_specific_validation_does_not_run_for_other_adapters`. Log: `/private/tmp/olcr-r29-api-regression-r5.log`.
- This is backend regression evidence only. No current-source app build or GUI rerun has happened yet. Keep all 139 R29 misses in the active live queue until a grouped same-conversation GUI rerun confirms them or records a provider prerequisite/outage separately.

## R30 same-conversation unresolved-set batch (2026-10-10)

- All 139 R29 audit misses were submitted in one batch to the same existing projectless conversation `0be00b3f-2764-4f04-aa5c-91bf766a59f1`, on one current-source GUI build (`olcr-fuzzy352-r30-20261010`, exact app `/private/tmp/olcr-e2e-fuzzy352-20261010-r30-target/debug/bundle/macos/OLCR.app`). No mock providers were used. Full request/response/route/tool/error JSONL: `/private/tmp/olcr-fuzzy352-r30-authenticated-unresolved-batch.jsonl`; summary: `/private/tmp/olcr-fuzzy352-r30-authenticated-unresolved-summary.json`; backend diagnostic log: `/private/tmp/olcr-e2e-fuzzy352-20261010-r30.log`.
- Batch completed 139/139; status buckets: 31 tool-returned, 30 no-tool/clarification, 77 provider failures, 1 client timeout. These are triage buckets, not pass counts. The returned-result and no-tool rows were semantically audited; valid clarification, API equivalence, credential gates and genuine provider outages must not be counted as product passes or app defects without evidence.
- Clear OLCR-side argument/intent defects: `earth.streamflow#4` and `web.archive#3` were routed to the correct API but sent unusable empty arguments; `food.recipe#4` and `law.us_courts#2` fell to generic NEURAL behavior despite lacking the ingredients/legal issue required by their referential prompts. The latter two should ask a scoped clarification rather than fabricate conversational context or emit a generic external failure.
- Grouped source repair added preflight clarification guards for missing streamflow location, Wayback URL, recipe ingredients, and court issue; the streamflow and Wayback compilers now also reject empty required identifiers. Added adjacent sufficient-input controls and explicit compiler regression coverage. Worldwide holiday requests remain valid without a country, and the existing global-holiday regression still covers that distinction.
- Verification after this grouped patch: `test_argument_compiler.py` 23/23 passed; `test_fuzzy_api_routing_matrix.py` 19/19 passed, including all 352 planned prompts and explicit global-holiday coverage. Logs: `/private/tmp/olcr-fuzzy352-r30-argument-tests.log` and `/private/tmp/olcr-fuzzy352-r30-routing-tests.log`. `pytest` is not installed in this environment; tests were run with `unittest` discovery.
- R30 was closed normally through its exact absolute-path window; PID 40856 and paired backend PID 41038 / port 58227 were verified exited. The grouped changes have not yet been rebuilt or live-retested. Keep all 139 audit misses active until the next same-chat batch is classified; provider-empty, auth, rate-limit, bad-response, timeout and route errors remain individually visible in the JSONL and are not silently removed.
