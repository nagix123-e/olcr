# Verification storage-failure capability

The reported verifier rejected a passing replacement for VERIFY:4 because it
omitted the required storage-failure error path. That rejection was appropriate.
The authoring boundary, however, offered read-only page evaluation and literal
fill/click observations without a supported way to induce a storage error.

The disposable browser runner now provides setStorageFailure(boolean). The
observation compiler exposes storage_failure/localStorage/boolean in recovery
checks. Authors are instructed to inject the fault, perform the ordinary UI save
and assert visible error guidance, without modifying application source or DOM.
The host preserves the original Storage.setItem descriptor and restores it on
false. Init scripts preserve the selected fault across reloads; each check has
its own page/context.

Validation: the existing lifecycle suite's other 22 tests passed; a new real
Chromium check passed after exercising failure UI and restoration to normal
saving. This does not claim that the user's retained meal-planner task has
completed; that requires resuming its model-authored acceptance checks.
