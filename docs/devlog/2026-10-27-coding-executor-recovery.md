# Coding verification and continuation repair

The supplied meal-planner run stopped because the acceptance runner read
`manifest.browser.path` from the Node component. Current installations deliver
Node tools and Chromium as separate components, and the Node manifest has no
`browser` field. The resulting host exception produced no acceptance summary
and was incorrectly retried as an application defect until replan exhaustion.

## Changes

- Resolve Chromium from the installed browser component, while retaining support
  for older bundles whose Node manifest contains a browser path. Node-only
  verification does not prepare Chromium.
- Refresh host-owned commands when retrying persisted tasks, so old commands
  also receive the current browser executable without regenerating checks.
- Emit and consume typed executor failures. Missing or malformed host summaries
  also count as executor failures. They cannot pass acceptance, request source
  repair, or consume architecture replan attempts. Preparation probes preserve
  existing checks and pause before requesting further model authoring.
- Recover the executor ownership of historical host crashes that have no
  result summary. Ordinary application test failures retain their ownership.
- Keep short resume/retry requests on the coding control route in both chat
  transports, including punctuation. Archived tasks retain an explanatory
  coding response and remain archived; they cannot fall through to scraping.
- Fix the undefined folder-context service on the normal planning/chat path.
- Require an explicit file/format object for English `change` conversion
  commands, preventing coding instructions such as “files may change ... to
  support imports” from becoming file-conversion requests.

## Validation

- Main related regression run: 444 tests, zero failures/errors, four existing
  skips. This covers executable acceptance, orchestrator repair, coding
  classification, task continuation, working folders, file conversion and
  existing assistant-action routing.
- Node runtime resolution tests: 11 passed with an isolated runtime profile.
- Final continuation checks after unifying short retry handling: seven passed
  (one new regression and six repeated existing checks).
- Total distinct cases: 456, of which 452 passed and four were skipped.
- Real Chromium regression: 12 independent browser checks each perform 12
  awaited actions/assertions, save to LocalStorage and reload; a mixed Node
  check also succeeds. An intentionally wrong application assertion fails
  without being mislabeled as an executor failure.
- `git diff --check` passed for the affected files.

The original generated app and its 27 saved checks were copied to a temporary
workspace and replayed with external requests blocked. The runner completed
without the original infrastructure exception: 21 checks passed and six failed.
Remaining failures include empty-state/notification expectations, a hidden
control, and project tests without registered `node:test` cases. They remain
explicit unmet acceptance criteria; the generated app was not marked complete.

The live OLCR app, its database, and the user's generated application were not
modified by these checks. No external model calls or live OLCR GUI acceptance
run were performed. Existing running backend processes need restarting to load
the Python changes.
