# Coding repair handoff and stale failure regression

The reported task `7d76df56-bd83-4c1e-b463-8afa4131b931` stopped in
`OLCR_ACCEPTANCE` after its UI/source and test-creation phases. Read-only
inspection of its saved reports and model responses established:

1. The generated test had a contradictory history-order expectation. Browser
   initialization also read an undefined global instead of the module's exports.
2. The diagnostic model correctly described the test correction, but the separate
   operation writer returned unchanged full files twice. The final patch put a
   fragment from the test into `index.html`, with identical old/new fragments.
3. The snapshot and executor hashes matched. The reported “source changed” was
   misleading: the model's fragment never belonged to that target.
4. Bounded retry context dropped the actual preimage reason/match-count keys,
   and converted numeric zero into an empty string.
5. In isolated real-model reproduction, browser-only repair reused a resolved
   test diagnosis and regressed an already passing test. Small line-range errors
   also consumed complete phase retries before any useful change.

## Common corrections

- Ineffective acceptance repairs, rejected preimages and subsequent recovery use
  a source-bound line-edit transport. The implementer selected by the controller
  diagnoses and encodes the edit in one response. Repair no longer silently
  overrides the controller's model handoff. A bounded encoding correction may
  use the other existing configured coding role; no deployment is changed.
- The host compiles inclusive ranges from immutable current snapshots. It
  rejects invalid paths/ranges, overlap, and no-change proposals. Ordinary scope,
  live preimage hashes, package ownership, syntax, atomic rollback and actual
  acceptance verification remain in force.
- The transport selection survives rejected proposals/restart boundaries in the
  saved typed operation records. It does not reset recovery budgets.
- One bounded encoding correction can fix an invalid range or syntax error
  before any write. Both model calls remain visible in task telemetry. This is
  not an unbounded phase-retry loop.
- Current test failures are prioritized with their implementation. Browser-only
  repair excludes passing test paths from its editable schema and filters out
  resolved test history. Referenced markup remains available when a failing
  executable test actually reads it.
- Failing source frames are prioritized before the source-inventory limit.
- On stalled Node assertions, a temporary source copy can capture bounded local
  primitives/arrays immediately before the original assertion. The assertion
  remains intact, actual project files are not edited, and observations are
  diagnostic data rather than PASS evidence. Missing dependencies/unsupported
  statements simply yield no observation. This path avoids relying on Inspector,
  which the bundled Node does not provide.
- Snapshot/preflight/readback use the same byte-preserving UTF-8 decoding.
  CRLF files no longer look like concurrent edits merely due to newline
  normalization.
- Preimage diagnostics retain the target, zero match count, original reason and
  source/fragment relation through the bounded retry context.
- Repair prompts bind the first measured failed ID to its actual requirement.
  The remaining counterexamples are deferred to the host's next verification,
  rather than inviting unrelated changes to every unverified criterion.
- Original-user acceptance checks need independent semantic review even when
  their first execution passes. A placeholder, one sample value, or a weaker
  substitute is not proof of the original behavior. The host caches only
  CHECK_VALID review evidence; SOURCE_DEFECT does not establish full coverage.
- Executor identity 27 migrates older contracts. Original-user typed checks
  authored by the old observation compiler without semantic evidence are
  regenerated. A changed source is still rechecked; cached review is not PASS.
- The observation compiler rejects a generated check that simultaneously fills
  multiple lines, expects the line breaks to remain, and requires a single-line
  input tag. It reads all matched text nodes for list observations and rejects
  missing targets, avoiding strict-mode exceptions on legitimate multi-item
  histories. Real joined observations remain available to counterexample review.
- An authoring syntax failure immediately persists typed-format recovery even
  at a final-attempt or pause boundary. One bounded format-switch opportunity
  remains; Resume does not silently return to the same broken JavaScript.
- Initial check authoring remains batched. A rejected semantic/encoding batch
  is repaired one criterion at a time within the existing outer bound, retaining
  accepted checks. This avoids repeatedly mixing unrelated input/action states.
- Fresh SOURCE_DEFECT reviews no longer cancel an already selected controller
  handoff back to the short role. A headless browser/controller regression
  exercises no-op repair, role handoff, actual source repair and task completion.
- HTML preflight rejects self-closing raw-text/RCDATA containers before writing
  them. A generated `<textarea />` had swallowed the following controls during
  native investigation. SVG/MathML closing rules and strings inside scripts
  remain supported. This is a narrow structural guard, not full HTML validation.
- The same parser supplies deterministic current-source evidence during failed
  browser-check review, including for older generated files. A malformed HTML
  text boundary cannot be reclassified as a selector/check defect by the model.
  Upgrading the executor identity releases old authoring-stop signatures without
  resetting task recovery budgets or accepting an old PASS.
- Initial and recovery observation schemas now share typed measure/operator/
  expected-value branches. In particular, `includes` requires a nonempty string,
  as the compiler already did. Native generation had emitted empty-string
  inclusion checks that were schema-valid but rejected only after generation.
  Equality with an empty string remains available for genuine empty-state checks.
- The current host source-defect diagnosis is a separate structured handoff to
  the implementer. Switching into source-bound repair previously rebuilt the
  prompt and lost this freshly reviewed explanation. Both the repair and its
  one pre-write correction now retain it, independently of stale report history.
- Typed workflows support up to 32 awaited interactions rather than 8, with
  one shared schema/compiler limit. The old limit could not express ordinary
  multi-item workflows (e.g. twelve input/add operations) and encouraged weaker
  verification. A real browser regression checks all 24 actions and item order;
  grammar/compiler tests still reject an over-limit sequence. Layout/network
  criteria retain their stricter interaction scopes.
- When two focused semantic-check authoring attempts fail, one alternate
  existing writer may be tried within the same outer/per-criterion bound.
  Independent acceptance review remains with the designer. Narrowing a batch
  starts the focused attempt counter; it cannot inherit an already exhausted
  malformed-response count from the wider request. A successful handoff is
  retained only for that criterion, not pinned onto later independent work.
- Final coverage re-enters the cached semantic gate if a passing original-user
  check was preserved from an earlier mixed failing batch without review. Filling
  the last coverage gap cannot turn that unreviewed passing assertion into PASS.
  A regression preserves a passing tautology while fixing another check and
  verifies that the tautology is still rejected and replaced before completion.
- Recovery no longer reduces verifier expressiveness to final-state literals.
  Stateful checks may use the existing bounded, line-framed JavaScript form to
  record actual intermediate outputs and compute relational expectations.
  Simple layout/network checks remain literal-only. Both forms retain syntax,
  read-only execution and independent semantic review; mixing both forms in one
  check is rejected. A separate running-balance UI regression passes cumulative
  outputs and fails an overwrite implementation, including after syntax recovery.
- Semantic review declares the necessary verification capability. When actual
  intermediate observations/derived comparisons are required, the replacement
  schema admits only line-framed code, so another writer cannot downgrade the
  check to a shallow final-state literal. This capability survives the persisted
  review finding and remains subject to the ordinary independent review gates.
- Native logs established a feedback truncation defect: a 9,610-character
  finding placed its review at offset 9,235, while the repair prompt kept only
  the first 2,200 characters. The writer never received the actual diagnosis.
  The prompt now budgets diagnosis and measured UI facts structurally, dropping
  bulky action-state snapshots before either can displace the other. The result
  stays valid JSON within 2,200 characters. Both the large-snapshot and existing
  long-advice regressions require diagnosis, visible guidance and control state
  to survive together.
- Partial-batch reconstruction preserves semantic reviews for exact unchanged
  check bodies and invalidates evidence for removed/replaced checks. Native
  investigation showed previously reviewed IDs disappearing while another ID
  was repaired, forcing old requirements back through authoring/review.
  Final semantic-gate re-entry also has an explicit bound so it cannot silently
  replenish the authoring loop through recursive calls.

## Validation

The final combined acceptance, repair lifecycle, orchestrator, source-bound
repair, managed runtime, native grammar and model-role suites passed
**357 tests** on the same final source (`/tmp/olcr-final-progress-regression.log`).
This includes real headless Chromium checks, native tokenizer/grammar/Metal
checks, generic multi-item workflows, cross-language source repair, source
concurrency/CRLF protection and bounded pause/resume recovery. Model responses
in controller/lifecycle scenarios are fixtures; those scenarios do not prove
real-model task completion.

The earlier broad run exposed two obsolete role-digest fixtures using an unused
Qwen role. Those fixtures now exercise the active semantic-embedding binding.
No model deployment or settings were changed. Restricted-sandbox runs lacking
localhost/Metal access were rerun with those required local capabilities; their
environmental failures were not treated as product passes.

Isolated native investigation reproduced the reported invalid patch with
matching source hashes, corrected the generated Node test to **16/16 passing**,
and progressed past the browser initialization exception. It also exposed the
contradictory/weak verification checks described above.

The final executor-27 native acceptance audit passed **all 9 criteria**, including
the six original browser requirements, real **16/16 Node tests**, browser startup
without errors, and local/no-external-resource checks. All six original IDs have
independent semantic-review evidence; there are no uncovered criteria or changed
verification targets. The final history check captures each result independently
and compares the actual five displayed items with the last five results in either
display order. The no-repeat check records twelve actual selections and compares
adjacent results. This does not rely on fixed random outcomes or history sets.

See `2026-10-24-coding-source-repair-validation.json` for the compact evidence and
source hashes. Native acceptance starts from the previously repaired temporary
copy of the reported artifacts. It is not a fresh empty-project/full GUI run or
a proof that the original production task's exhausted recovery budget completed.
The harness invokes verifier/repair components directly; production controller
and recovery-budget scenarios use model-response fixtures in the regressions.
Earlier executor audits either stopped on real check-authoring defects or were
interrupted when switching to newly corrected code. They are not native passes.

The native reproduction operates on a temporary copy of the reported task's
files and a separate database. No user project/database, model deployment,
commit, push, or running user OLCR process is changed by this investigation.
