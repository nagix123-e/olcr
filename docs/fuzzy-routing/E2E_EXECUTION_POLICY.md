# Fuzzy API Routing E2E Execution Policy

This is the standing execution policy for the 352 natural-language routing probes in `plan-and-352-prompts.md` and subsequent fuzzy-routing regressions.

## Batch-first loop

1. Keep every unresolved, testable case in the active test list. When the live queue contains 300+ cases, send all of them through the same existing OLCR chat and current GUI/backend build, recording each response and log, before starting any repair cycle. Do not stop to fix the first failure and do not rebuild or relaunch after an individual case.
2. Use one existing OLCR conversation for GUI E2E. Do not create a conversation per test category or case. Preserve its history/profile when moving to a new build.
3. For every case, compare the visible answer with the backend route, typed arguments, provider status, returned evidence, and final-response logs. An HTTP 200 or non-empty response alone is not a pass.
4. Remove a case from the unresolved list only after its expected source, relevant data, argument semantics, truthful limitations, and user-visible rendering all pass. Provider access denial, missing credentials, or an upstream outage is recorded separately from an OLCR implementation defect; never label it as a product pass.
5. After the complete batch, cluster failures by shared root cause and make the whole set of related repairs together. Run the relevant offline regression suites and the full routing matrix before creating one new GUI build.
6. In that build, rerun all failed cases from the batch together, plus representative already-passing controls affected by shared code. Keep the same conversation; verify exact absolute app path, matching backend/build identity, profile, and Keychain restore state before sending prompts.
7. Repeat the batch loop until every testable case passes or is explicitly classified as blocked by external/provider prerequisites. Keep blocked cases visible in the ledger rather than silently dropping them.

## Worldwide holidays are valid without a country

Do not apply the country-required clarification rule to an explicitly worldwide request. “What holidays are happening somewhere in the world today?” must route to the provider's global/current endpoint when supported. Clarify only when the geographic scope is genuinely missing or when a request asks for a country-specific calendar without naming the country. A request for a whole-year global catalog is a separate capability question and must not be silently reduced to the provider's shorter rolling window.

## Evidence and accounting

- Maintain a stable case ID and record prompt, expected capability, actual route, compiled arguments, provider outcome, visible-result verdict, build ID, and log reference.
- Change pass counts only after the grouped GUI rerun confirms the relevant cases; retain failure history for auditability.
- Batch size may follow a coherent execution group, but batching must not turn into one build per case. Shared defects discovered across groups should be fixed together before the next build whenever practical.
- Never expose credentials in test logs. Use isolated test profiles and read-only provider requests.
