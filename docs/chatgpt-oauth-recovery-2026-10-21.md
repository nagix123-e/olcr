# ChatGPT OAuth recovery — 2026-10-21

Read-only diagnostics confirmed that the signed Keychain bridge still returns a complete OAuth credential set. The access token is expired. The current app/backend pair was identified by PID, parent PID, loopback listener and build ID. Its provider is configured, but has zero models and reports a rejected refresh token requiring reauthentication. The current app is an isolated API-matrix test profile; the normal profile still retains its OAuth model metadata. No credential values were logged, copied into files, or cleared.

The exact historical reason for refresh-token rejection cannot be established from local state alone. The following reproducible weaknesses have been fixed:

- Concurrent catalog/inference calls could refresh the same rotating token simultaneously. Refresh is now serialized and rechecks the latest in-memory credential.
- Runtime Keychain restoration updated external-tool environment variables but omitted the existing Brain credential store. It now updates that store without rebuilding Runtime, and cannot overwrite pending rotations or newer token sets with older snapshots.
- Rotations were saved only when a successful catalog refresh noticed them. The frontend now flushes pending credentials periodically during operation, serializes Keychain writes, acknowledges the exact saved version, and immediately saves a newer version when one arrived during a write. A Keychain failure retains the pending credential for retry and does not discard the catalog.
- A fresh profile could generate a new host identity despite restoring an existing OAuth client registration. It now preserves the saved credential's validated host ID.
- The picker conflated an expired grant, temporary catalog failure and missing authentication. Provider connection state now distinguishes them, retains credentials, disables unavailable expired-grant models, and offers ChatGPT reconnection. The model-settings panel also labels the expired connection correctly.

The rejected refresh token cannot be repaired or replaced locally. After loading the patched source, the user must complete **ChatGPTに再接続** once. This uses the existing official OAuth/PKCE sign-in flow and retains application/model routing. No API-key substitution, browser-cookie extraction or unauthorized alternate authentication is used.

References: [official sessions/refresh flow](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions), [token rotation](https://developers.openai.com/siwc/token-sharing-open-source/token-reference).

Regression coverage: `backend/tests/test_oauth_recovery.py`, existing Brain provider/model tests, `frontend/src/OAuthRecovery.test.tsx`, and the existing model-picker/settings tests. Tests use synthetic credentials; the OAuth callback test uses a loopback mock.
