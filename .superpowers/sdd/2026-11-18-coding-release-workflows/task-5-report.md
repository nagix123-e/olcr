# Task 5 report — Google Play release adapter

## Result

Implemented `GooglePlayReleaseAdapter` with fail-closed preflight for explicit package, track, version/versionCode, artifact, agreement, attestation, OAuth scope, and rollout settings. It uses the typed Android Publisher API transport and a fixed-host resumable media upload transport. It does not accept service-account JSON or expose credentials/session URLs in results.

The adapter creates or inspects an edit, uploads exactly one workspace-confined APK/AAB, verifies the artifact SHA-256 (APK `binary.sha256`; AAB top-level `sha256`), reads the current track, preserves existing release metadata, validates and commits the edit, then reads back the exact track artifact and release lifecycle. `edits.commit` uses `changesInReviewBehavior=ERROR_IF_IN_REVIEW`, because Google's default cancels an existing in-review change. A track's `completed` value alone is insufficient to report publication: only the lifecycle API's exact matching `RELEASE_LIFECYCLE_STATE_PUBLISHED` is reported as `PUBLISHED`. In-review and approved-but-manual releases stay `SUBMITTED`; rejected, draft, and not-sent releases stop for user action.

Production rollout percentage must be explicit from 1 through 100. A partial rollout is `PUBLISHED` only after Google's lifecycle API confirms it is available to users; the result retains `inProgress` and states that it is partial. Existing release notes, country targeting, and in-app update priority are preserved. A country-targeted release cannot be promoted to a different status automatically because that could broaden availability.

If a production track already has country-targeted releases, adding a new version is also blocked. This adapter does not yet accept an explicit country target for the new version, and silently adding an untargeted release could broaden availability.

Resumable upload sessions recover interrupted transfers within the current process by querying the session offset and resending only the remaining bytes. A session URI is held only in memory and validated against the exact upload path and official Google upload hosts. After a process restart the adapter first inspects the edit and artifact list; it may need a new session if an incomplete session URI was lost.

## Verification

Focused Google Play, shared contract, GitHub, and Apple adapter suites:

```text
PYTHONPATH=backend <project-venv>/bin/pytest -q \
  backend/tests/test_google_play_release_adapter.py \
  backend/tests/test_release_adapter_contract.py \
  backend/tests/test_github_release_adapter.py \
  backend/tests/test_apple_release_adapters.py
63 passed in 0.14s
```

Python byte-compilation and `git diff --check` passed. Tests use fake API/upload transports and never contact Play Console or upload an artifact.

## Official behavior checked

- [Android Publisher edit resource](https://developers.google.com/android-publisher/api-ref/rest/v3/edits) documents edit IDs and expiry.
- [Edit commit](https://developers.google.com/android-publisher/api-ref/rest/v3/edits/commit) documents the default `CANCEL_IN_REVIEW_AND_SUBMIT` behavior and `ERROR_IF_IN_REVIEW` option; the adapter opts out of canceling a current review.
- [Track release fields and statuses](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.tracks) documents track status, `userFraction`, retained version codes, and country-targeting constraints.
- [APK resource](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.apks) nests hashes under `binary`; [Bundle resource](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.bundles) exposes top-level SHA-256.
- [APK upload](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.apks/upload) and [AAB upload](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.bundles/upload) define the fixed media endpoints and Android Publisher OAuth scope.
- [Media upload guidance](https://developers.google.com/android-publisher/upload) recommends resumable transfer for reliable larger uploads and documents status queries and offset-based resume.
- [ReleaseSummary and lifecycle values](https://developers.google.com/android-publisher/api-ref/rest/v3/applications.tracks.releases) define `PUBLISHED`, review, approval, rejection, draft, and manual-action states. [`applications.tracks.releases.list`](https://developers.google.com/android-publisher/api-ref/rest/v3/applications.tracks.releases/list) provides the release records for exact track/version read-back.

## Review

Independent review found four issues: treating legacy track `completed` as proof of publication; dropping fields from a same-version release update (which could broaden country availability); reading APK SHA-256 from the wrong JSON location; and using fragile one-shot uploads for large artifacts. After fixes, a second review found a fifth issue: adding a new version on a country-targeted production track could broaden availability. The adapter now blocks that operation; a regression test confirms no track update is sent. Final independent re-review confirmed all five findings are addressed with no remaining finding in scope.
