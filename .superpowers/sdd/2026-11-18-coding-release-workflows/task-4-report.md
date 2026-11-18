# Task 4 report — macOS notarization and Apple App Store/TestFlight

## Result

Implemented `MacOSNotarizationStage` and `AppStoreConnectAdapter` against the Task 3 command/API boundaries.

The macOS stage accepts one workspace-confined signed `.app`, verifies the Developer ID authority, Hardened Runtime, and secure timestamp with `codesign`, packages the app as a ZIP with `ditto`, submits it with the configured Keychain profile, inspects an existing submission ID before retry, then staples and validates the ticket. It returns only local build-stage states (`NOTARIZED`, `BLOCKED`, `UNCERTAIN`, or `FAILED`); it has no `PUBLISHED` state.

The App Store Connect adapter validates one confined `.pkg` for macOS or `.ipa` for other supported Apple platforms, hashes it, uploads exact byte ranges to Apple-issued HTTPS blob storage URLs without forwarding the API JWT, commits the SHA-256 checksum, and reads back upload state and build relationship before progressing. It supports App Review submission and internal TestFlight group assignment. App Review waiting and TestFlight upload remain `SUBMITTED`; `PUBLISHED` requires the requested build to be linked to a version that App Store Connect reads back as `READY_FOR_SALE`.

External TestFlight distribution is blocked before provider calls because this adapter does not implement Beta App Review submission and the required tester-facing information. For internal distribution, every specified group is read back first and must belong to the selected app and report `isInternalGroup=true`; the build/group relationship is read back after assignment. A build without a specified group is reported as uploaded and waiting for internal group selection, not as available to testers.

The adapter signs App Store Connect JWTs in memory from the configured key. Preflight performs a local token-signing check without returning or persisting key material. Ambiguous upload or ReviewSubmission results remain `UNCERTAIN`; an empty ready-for-review submission blocks a second submission after an uncertain create response.

## Verification

Focused Apple suite:

```text
PYTHONPATH=backend <project-venv>/bin/pytest -q backend/tests/test_apple_release_adapters.py
16 passed in 0.07s
```

Shared contract and GitHub regression suite:

```text
PYTHONPATH=backend <project-venv>/bin/pytest -q backend/tests/test_release_adapter_contract.py backend/tests/test_github_release_adapter.py
30 passed in 0.06s
```

Python byte-compilation and `git diff --check` passed. Tests use fake command/API/upload transports and a test-only P-256 key; no Apple account, App Store Connect, notarization service, or live upload was contacted.

## Official behavior checked

- Apple’s [notarization workflow](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution/) and [customizing notarization](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow) cover `notarytool`, hardened runtime, secure timestamps, and ticket stapling.
- Apple’s [Build Upload API](https://developer.apple.com/documentation/appstoreconnectapi/post-v1-builduploads), [Build Upload File API](https://developer.apple.com/documentation/appstoreconnectapi/post-v1-builduploadfiles), and [file commit API](https://developer.apple.com/documentation/appstoreconnectapi/patch-v1-builduploadfiles-_id_) define the reservation, upload, and checksum commit workflow.
- Apple’s [App Store Connect API token guidance](https://developer.apple.com/documentation/appstoreconnectapi/generating-tokens-for-api-requests) sets the JWT audience and token lifetime constraints used here.
- Apple’s [Review Submission API](https://developer.apple.com/documentation/appstoreconnectapi/post-v1-reviewsubmissions), [Review Submission Item API](https://developer.apple.com/documentation/appstoreconnectapi/post-v1-reviewsubmissionitems), and [submission update API](https://developer.apple.com/documentation/appstoreconnectapi/patch-v1-reviewsubmissions-_id_) support the App Review handoff and read-back.
- Apple documents the [`isInternalGroup` beta group field](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-betagroups-_id_) and explains [external tester review and required test information](https://developer.apple.com/help/app-store-connect/test-a-beta-version/invite-external-testers).

## Review

An independent subagent review found four issues: external groups were not distinguished from internal groups; `READY_FOR_SALE` did not prove that the requested build was attached; an empty review submission could be duplicated after a lost response; and preflight accepted a malformed private key. The implementation now blocks external distribution, verifies group ownership/type before upload and assignment, requires exact build relationship read-back before reporting publication, fails closed on an ambiguous empty submission, and signs a JWT during preflight.

The reviewer’s requested post-fix re-review could not start because the subagent provider hit its usage limit. I reviewed those four boundaries against the current code and added regression tests for each. The focused suites above pass; no provider calls were made.
