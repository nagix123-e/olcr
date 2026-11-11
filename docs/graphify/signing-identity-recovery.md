# OLCR macOS signing and sandbox recovery

## Confirmed root cause, 2026-10-29

Restricted tool execution hid the existing login-Keychain signing identity. `ps` was denied, and `security find-identity` misleadingly returned zero valid identities. Attempts to import a new identity failed for the same environment. These observations did NOT establish a lost certificate or damaged keychain. Earlier statements in this document claiming disappearance are withdrawn.

Outside the sandbox, `/usr/bin/security find-identity -v -p codesigning` immediately returned the original `OLCR Local Debug Code Signing`, SHA1 `969372F8911D3A2A85EA3339DF864961A663F683`. No identity rotation, trust change, or Keychain Bridge replacement was needed. The authorized launcher successfully built and signed latest source, verified the signature, launched through Launch Services, and logged `backend_api_ready=true` with build ID `olcr-dev-independent-20261029-234511`.

## Required recovery procedure

1. If ps fails or Keychain identities are invisible in a sandbox, rerun read-only identity checks with approved execution outside the sandbox. Never infer certificate deletion from restricted output.
2. Run `scripts/start-dev-independent.sh` outside the sandbox with the user's GUI-session and Keychain access. This is the normal persistent source launcher.
3. Require successful signing, strict signature verification, matching backend build ID, and backend readiness. Keep Keychain restoration enabled.
4. Only investigate genuine identity recovery if the unrestricted check also fails. Never rotate an existing signer based on a sandbox-only failure.

## Prevention

The launcher now checks process-list and signing-identity visibility before building and emits `OLCR_LAUNCH_ENVIRONMENT_RESTRICTED` or `OLCR_SIGNING_IDENTITY_NOT_VISIBLE` with recovery guidance. No private-key material belongs in project logs or Graphify.
