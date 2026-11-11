# OLCR mobile foundation implementation

Scope: PC-side authenticated ACP bridge, PC-owned durable event runs, Happier QR pairing UI; then an Expo mobile UI shell with explicit demo state and no server, model weights, relay client, or TTS runtime.

The approved release design is [OLCR_HAPPIER_MOBILE_RELEASE_PLAN.md](OLCR_HAPPIER_MOBILE_RELEASE_PLAN.md). Work is in the existing feature checkout because its current implementation includes extensive uncommitted changes that a HEAD-based worktree would omit. Only task-specific files and small integration points are changed.

- [x] PC: add isolated `mobile_runs.py` with SQLite event cursor/idempotency and host-owned consumers; exercise disconnect, duplicate submit, cancellation, and restart interruption.
- [x] PC: add `mobile_pairing.py` using the upstream Happier auth/pairing protocol, Keychain secrets, one-time QR, confirmation code and explicit unsupported per-phone revocation.
- [x] PC: add authenticated `mobile_api.py`, dedicated bridge descriptor and `olcr-acp` stdio CLI; preserve existing OLCR conversation/task/permission boundaries. Expose unsupported features through capabilities.
- [x] PC: add a desktop settings entry and pairing panel; preserve other settings.
- [x] Mobile: add independent Expo/React Native + Expo Router UI in the sibling [`olcr-mobile/`](../../olcr-mobile/README.md); OLCR semantic tokens, QR setup, sessions, conversation, task/artifact details, approval/question, Brain selection, local queue and settings. No backend implementation in this package.
- [x] Verify targeted PC tests, desktop typecheck, mobile lint/typecheck and web/native JS exports; review the changes and document current limits.

Upstream reference: Happier dev `7b37d65ca75e58560f8f3ca850958ac5407d2e75` (read-only checkout in `/private/tmp`), ACP v1, Expo SDK 58. No relay account is created and no real external model/API is invoked by tests.

Ruling: the mobile UI is an isolated Expo shell ready for later Happier integration. It does not pretend to be the finished Happier fork or an authenticated mobile client. Native signing/store delivery and LFM inference remain outside this requested UI-only stage.

## PC usage and current scope

The desktop source adds **設定 → スマホを接続**. Preparing a QR is an explicit user action: it creates a device-key identity in macOS Keychain and calls Happier's existing desktop pairing APIs. Tests use a mock relay and never create a real relay account. The QR panel currently targets the upstream `happier:///pair` protocol; the standalone mobile UI intentionally does not claim it.

`olcr-acp` is the new Custom ACP command. The desktop backend publishes a mode-0600 `mobile-host.json` in the OLCR profile, with a dedicated loopback bridge credential. The GUI credential is not handed to the adapter. With source installs:

```sh
PYTHONPATH=backend .venv312/bin/python -m olcr_cli.acp
# Or run the existing backend without the GUI, under a service supervisor:
PYTHONPATH=backend .venv312/bin/python -m olcr_cli.mobile_host --workspace /absolute/authorized/project
```

Only one backend may own an OLCR profile. The common backend lock runs before DB recovery and descriptor publication. The headless host command is foreground by design so launchd or an existing supervisor can own it. Automatic daemon installation, GUI reattachment to a headless owner, and Happier Custom ACP registration remain integration work; the application does not claim that pairing alone already enables remote sessions.

### Review fixes

- Coding handoff is monitored until the existing coding task reaches a terminal or user-waiting state. Cancel calls the existing pause/cancellation path.
- ACP `session/load` reattaches to an active PC run and replays its durable events; cancelled run creation retains the cancel intent.
- Nested OLCR `reasoning.text` is translated correctly.
- A second backend cannot overwrite the active profile descriptor or recover a live owner's runs as interrupted.
- Deleting a conversation purges its mobile event copies with a SQL trigger. A worker notices deleted ownership and cancels.

### Limits that remain explicit

- The shared recovery-key linking protocol does not provide independent phone revocation. Local unlink is not presented as cryptographic revocation; a relay/device-credential extension is a release gate.
- Detailed coding plan/scope approvals and structured questions are not fully mapped to native ACP forms yet. The PC remains the approval fallback; text replies and request-level approval are supported by the bridge.
- Some OLCR routes return a completed chunk rather than token streaming (for example the existing attached-folder path). This bridge preserves that source behavior.
- Generic rich blocks use `_olcr/event` extension events; a Happier OLCR presentation module must render them. Basic text/coding task state uses standard ACP updates.
- The mobile package is a UI shell, not an authenticated mobile transport, local backend, or finished store release.

## UI correction — existing desktop OLCR is the reference

The first mobile card/tab design was replaced after user feedback. The shell now opens the same chat workspace with a collapsible project/conversation sidebar, existing +/permission/Brain input controls and the existing settings hierarchy. All changes to spacing and control placement serve the portrait viewport. The former separate session home and bottom tabs were removed.

The mobile source now lives beside the PC repository in `lightweight local llm/olcr-mobile/`. The iOS development entry point is `olcr-mobile/ios/OLCRMobile.xcworkspace`; the Expo and React Native UI remains in the mobile folder. [Mobile setup and Xcode workflow](../../olcr-mobile/README.md).


## Verification evidence — 2026-11-10

- PC foundation, existing generation cancellation and permission policy: 22 tests pass. New tests use temporary SQLite profiles and a mock relay; no real account, Keychain read/write, AI request or PC tool action is performed.
- Desktop pairing/settings: 5 frontend tests pass; desktop frontend typecheck and production Vite build pass.
- Mobile: TypeScript and Expo lint pass. Expo exports iOS, Android and Web JavaScript bundles; this is not native signing, a simulator run, or store validation.
- Browser checks at 390×844 and 320×640: no horizontal overflow; project/conversation selection, settings hierarchy, split-role Brain, diff sheet, inline approval and next-turn queue were exercised. Device keyboard/VoiceOver/TalkBack behavior still needs real-device checks.
- Screenshots: [chat](verification/mobile-ui/chat-390.png), [sidebar](verification/mobile-ui/sidebar-390.png).

Review rulings: the existing feature checkout is retained to preserve current uncommitted implementation. The mobile UI is a standalone Expo presentation module with an explicit demo store until Happier communication is integrated. Shared-key phone revocation remains a known release blocker rather than a misleading local unlink button. No additional review findings were deferred as cosmetic work.
