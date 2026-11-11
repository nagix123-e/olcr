# Repository alignment — 2026-09-30

本書は添付 v2 を既存 OLCR 実装に合わせた実装指示 v3。以下の整合ルールは本文の概念図・順序より優先する。実装対象はこのディレクトリの OLCR 本体。ユーザーの既存変更を保持する。

## Product acceptance overlay: chat-first computer use and web scraping

The product entry point for both capabilities is the chat. Settings configure whether a capability is enabled, which targets/origins are in scope, and which OS permissions have been granted; Settings must not be the workflow launcher or contain workflow test buttons. When the user says “scrape this URL” in chat, OLCR should resolve the URL and request into a bounded scrape task, open/use its managed browser in a visible in-chat browser surface, show current page/evidence and progress in the conversation, and ask only for missing choices or a required permission. A chat card that exposes Studio forms or a text-only DOM snapshot is useful evidence but is not, by itself, the requested browser surface or proof of chat-controlled completion. When the user asks to run an E2E against their own site/game, OLCR should start or attach to the explicitly scoped local target, launch the managed browser, and carry out requested multi-step interactions from chat while showing the page and verified outcomes in the conversation. For native UI, chat should start the existing Computer Use session and use AX, OpenCV temporal/flow tracking, and UI-Venus escalation through the current pipeline to observe, act, and verify; starting capture alone is not a completed Computer Use task.

Do not make chat-triggered work equivalent to unrestricted machine access. Before execution, resolve user intent, target, and capability scope; use a separate explicit scope for a user-owned localhost E2E target rather than relaxing public-web egress rules; honor OS TCC and configured allowlists; and fail closed when target identity, permission, or requested scope is unclear. Natural-language chat is the task control surface for start, progress, pause/stop, and follow-up. Settings should expose capability ON/OFF, scope and permission state/request only; move workflow launch and web-search test actions out of Settings. A credential setup prerequisite may remain in a separate setup surface. Existing Scrape Studio and perception panels remain useful for detailed review and diagnostics, but must not be required to start the ordinary requested workflow.

Route selection is part of that chat intent resolution. A user's own website or browser game in a selected project is Local project E2E: `local_e2e_enabled`, the selected allowed workspace and a loopback origin bound to that task govern it. The separate `computer_use_enabled` toggle is for native desktop capture/actions against the explicitly Accessibility-scoped app and macOS permissions. Explicitly asking for E2E with Computer Use selects native session startup unless the request names a browser, Playwright, website/site, or URL; those signals keep it on the local browser route. This fixes the prior “real GUI E2E with Computer Use” collision at intent parsing, with frontend, backend and chat API tests. Requests whose target is still unclear should be clarified in chat before either route starts. The native path now grounds and verifies a single outcome-level button action from fresh AX evidence. One chat message can combine scoped native startup with one such goal action or an existing explicit sequence; generalized outcome-derived multi-step planning and a complete refresh-resilient conversation-owned run remain open. Capability toggles and OS permission grants authorize a bounded target scope, not unrestricted control of every app or network destination.

### Chat-first acceptance clarification (2026-10-03)

Treat the following as the observable product contract for CX1–CX7. The examples are ordinary chat messages; Settings is setup and policy only.

1. **Public-page scraping:** “Scrape `https://…` for the product names and prices” creates one conversation-owned task, opens the existing managed Playwright browser in the conversation, and shows the live page, progress, and evidence. OLCR drafts and runs the existing Recipe only after its normal validation succeeds. When requested fields require ordinary page navigation or a read-only reveal such as pagination or “load more,” chat should carry out those bounded Playwright steps in the same scoped session and show the resulting evidence. If the request is underspecified or validation cannot safely resolve fields, OLCR asks a focused question in chat and retains the browser/task; Scrape Studio can offer optional detailed repair, but must not be required to complete the ordinary request. Results, pause/stop, follow-up, and export remain available from chat. Preserve destination checks and ask before actions with external side effects beyond reading/navigation.
2. **User-owned browser E2E:** “Run E2E on my game/site” resolves the active chat project and a single in-scope dev-server command/origin; it starts the task-owned server or attaches to an explicitly scoped loopback site, opens the existing managed Playwright browser in the conversation, and executes bounded natural-language multi-step actions against fresh DOM/AX evidence. The user states the desired outcome; OLCR grounds the steps, so users do not have to provide exact selectors, labels, or assertions for every action. It verifies the requested visible effects and keeps follow-up/status/stop in the same conversation. Ask in chat if there are multiple plausible commands, origins, or targets. The in-chat browser view shows the task-owned page and evidence; the conversation is the control surface for actions and follow-up.
3. **Native computer use:** “Start real E2E with Computer Use” resolves a target from the configured app scope and the chat request, starts or attaches to that app from chat, and runs a bounded task through the existing ScreenCaptureKit → AX/OpenCV tracking → UI-Venus escalation → action → fresh-effect-verification pipeline. The originating chat embeds a compact live view of the exact app window being controlled, scoped to that task's capture session, PID and bundle ID; unrelated desktop content must be cropped before the preview reaches the chat UI. The user states the desired outcome; OLCR plans and verifies supported steps from fresh app evidence, asking in chat only when the target, intent, or action cannot be resolved safely. Requiring the user to provide exact labels and expected text for each click is a current implementation limitation, not the product contract. A capture session starting is only task setup. A user-owned app must not be terminated; only a process created and owned by the task may be stopped by its task controls.

   For a native app built by the selected project, target resolution must support the exact `.app` bundle path supplied in chat or resolved from that project's declared E2E/build output, even when Launch Services has not indexed it. Before launch, canonicalize the path, require it to remain inside the selected allowed workspace unless the user explicitly provided another scoped path, read its `CFBundleIdentifier`, and require that identifier to be in the Computer Use app allowlist. Launch that exact bundle URL through `NSWorkspace`, then wait for a live process with the same bundle ID before capture. Never search arbitrary disks for a matching app or launch a path chosen only by a model. Keep this as a chat-started task; Settings configures app/workspace scope and permissions but does not launch it.
4. **Settings boundary:** Settings may enable/disable Web scraping, local browser E2E, and native Computer Use; define allowed workspace/origin/app scope; and report or request the relevant OS permission. A fixed “active target for the next task” picker is not a substitute for resolving a target in chat. Chat resolves among the allowed scope and asks there when ambiguous. Starting a browser, server, capture session, or action sequence belongs to chat. Manual “observe app”, “start watching”, and “refresh World State” controls currently rendered inside the Settings view are diagnostic workflow actions; move them to chat task controls or a clearly separate diagnostics surface so Settings remains policy/configuration only.
5. **Reuse boundary:** Public scraping and browser E2E share the current managed Playwright/browser-session machinery while retaining their distinct destination policies and Recipe lifecycle. Playwright remains the action/evidence source for DOM-backed pages and games. OpenCV is for native/visual temporal tracking and should reduce repeated screen-frame work there; it does not replace browser DOM interaction. Native work reuses the current AX, ScreenCaptureKit, APC, OpenCV, UI-Venus, and verified-action pipeline. Do not add a second browser runner, scraper, or native executor to satisfy this contract.

Acceptance is end-to-end and conversation-owned: each example must resolve scope/target, launch or attach, show live state, perform the requested task, verify effects, and support follow-up/status/stop after refresh. Unit/API/fixture evidence and a successful session-start response alone do not satisfy it; record browser and native live evidence separately.

For browser work, reuse the existing Playwright manager and Recipe/ScrapeRun lifecycles. Prefer DOM semantics and Playwright for sites and DOM-backed browser games; use the existing native perception/action route for native apps or canvas/game controls that lack useful DOM semantics. OpenCV's change sentinels and grounded LK tracking reduce repeated pixel transfer and re-grounding in that native/visual path; they do not replace Playwright's DOM interaction or ScreenCaptureKit's OS capture. Chat chooses or asks about the route based on the requested target, not a second browser or a second action runtime. For a user's local project, reuse a declared E2E/dev-server configuration when present. Otherwise, resolve a single suitable command and local origin from the selected project's existing scripts/configuration or from the user's chat instruction; if multiple plausible commands or origins exist, ask the user to choose in chat. Once the selected workspace and local scope are authorized, start the task-owned server, wait for readiness, open its origin in the managed browser, and report/stop only the process owned by that task. A project manifest may be offered as a way to save a choice, but must not be the only way to start a one-off chat-requested E2E. Do not terminate an existing user process or expand local-origin access into public-web scraping policy.

CX1–CX7 are product-completion gates for the active implementation goal, alongside the P0–P30 and V/A/R/W/U engineering gates. The architecture gates remain required, but a technically complete backend with workflows that still require a separate Studio/panel is not a completed user-facing task. Chat is the sole workflow launcher and controller: a user must be able to ask there to open a requested URL for scraping, start/attach to a user-owned local web E2E target, or start a scoped native Computer Use task, then continue, inspect, verify, pause/stop and retrieve results in the same conversation. The user states the outcome; OLCR resolves and grounds the bounded action plan from the authorized target's current evidence. Asking the user to provide a target label and expected text for each native action is a current gap, not a lasting acceptance requirement. Settings only defines capability enablement, allowed target/origin/workspace scope, and OS permission state/request; it must not be a required launcher or contain test-run controls. Prioritize the chat journeys in this order: (1) resolve request, capability, target and missing choices from the originating chat; (2) keep a visible managed-browser surface and verified multi-step Playwright actions in that chat; (3) finish ordinary URL scraping there, including clarification, results and export; (4) launch or attach to the requested in-scope E2E target from chat and route native multi-step tasks through the existing AX/OpenCV/UI-Venus pipeline with fresh evidence and effect verification; (5) leave Settings for capability toggles, target/origin scope and OS permission state/request. Each task needs a conversation-owned handle so follow-up chat messages, refresh/reconnect, pause/stop and results address the same run. Task-card controls and Scrape Studio may supplement chat for detailed inspection, but cannot be required to start or finish the ordinary request. Do not claim goal completion until these gates, P0–P30 and V/A/R/W/U have fixture/API/UI coverage and the appropriate browser/native live evidence. Preserve Recipe validation, destination safety, action verification and user-confirmation boundaries.

### Implementation checkpoint: native chat target scope (2026-10-03)

Settings persists the Computer Use bundle-ID allowlist. Its running-app inventory uses NSWorkspace metadata and is available without Accessibility trust; AX trust remains required for UI snapshots/actions. Chat start resolves a named target against that scope and the live running-app inventory; without a name it starts only when exactly one allowed app is running. It asks for clarification in chat on ambiguity and blocks empty scope, out-of-scope names, absent targets, and mismatches with an already-running capture session. The backend rechecks bundle scope before accepting a RUNNING start event. Focused parser/target/UI tests, settings validation, API scope rejection, and OpenCV-enabled Rust tests cover this path.

This checkpoint is superseded by the 2026-10-03 chat-authorized launch follow-up below. At this point in the implementation history, native chat only targeted an already-running app; generalized verified action sequences, Settings cleanup, and live capture remained open. Scraping and browser E2E were already chat-started through the existing managed Playwright, Recipe/ScrapeRun and LocalE2E paths.

### Follow-up: closed native app launch and policy-only Settings (2026-10-03)

Chat can now resolve an explicitly named, allowlisted bundle ID even when its app is closed. Before Launch Services runs, the backend rechecks Computer Use enablement and exact bundle scope; the frontend then waits for the launched app's live PID before starting ScreenCaptureKit. Settings persists the bundle-ID allowlist independently of Accessibility trust. Capability toggles use a partial update, so changing Computer Use or another capability does not replace the current app scope. OLCR's own bundle ID is rejected as a target. The user-visible chat contract remains the task control surface: resolve target/goal, start or attach, show progress, run bounded actions, verify effects, then retain status/follow-up/stop in that conversation.

Evidence from isolated OpenCV GUI run `20261003-settings-patch-c7f48a21`: the dedicated profile stored `computer_use_allowed_bundle_ids=["com.apple.TextEdit"]` and `computer_use_enabled=true`; chat launched closed TextEdit (PID 97978). The exact current bundle then received `SCREEN_CAPTURE_PERMISSION_REQUIRED` before the first frame (`CoreGraphics request result=Some(false)`). This proves settings persistence and chat-authorized app launch, but not successful screen capture, OpenCV tracking, or native action. A Screen Recording approval for an earlier unique test app path did not grant this newly built bundle. The old app was quit from its menu; the exact backend process was stopped after health returned HTTP 500 after app quit, and its port closed. The health failure is recorded as unresolved run-lifecycle evidence; do not treat it as a TCC cause.

Focused frontend tests passed 18/18; the full frontend suite passed 117/117, typecheck and production build passed (existing large-chunk warning). Targeted Computer Use API tests passed 2/2, including scope persistence, preservation across a capability patch, launch authorization, and rejection of OLCR as its own target. The strict isolated launcher built and signed with the OpenCV feature and reported `READY_FOR_COMPUTER_USE=YES` with matching PID, port, build ID, profile and DB. At this checkpoint, Accessibility observation/monitoring and World State diagnostics still appeared in Settings; that gap is resolved by the separate diagnostics follow-up below.

### Follow-up: policy-only Settings and separate desktop diagnostics (2026-10-03)

Settings now hosts permission setup/status guidance, the Computer Use app allowlist, capability toggles, credentials, and Web policy. The “Observe focused window/app”, “Start watching”, “Refresh unified World State”, capture telemetry, and UI-Venus grounding tools are on a separate Desktop diagnostics surface. The normal Computer Use start/stop/status route remains chat-driven, and navigating into Settings hides the global capture stop action. No parallel capture, action, or observation runtime was introduced.

Verification: `AccessibilityPanel.test.tsx` passes 15/15, including explicit assertions that policy mode hides observation/monitoring/World State controls and diagnostics mode exposes them without app-scope or permission-edit controls. Frontend typecheck and production build pass (existing >500 kB JS chunk warning). CX6 is therefore PASS; CX7 remains OPEN because native generalized actions, successful ScreenCaptureKit/TCC capture and live OpenCV evidence, and broad chat task recovery/reconnect are still missing.

### Follow-up: bounded multi-step native actions from chat (2026-10-03)

Chat now accepts an explicit sequence of two to four supported native actions, separated by `then`, `; then`, or Japanese “次に / 続けて / その後”. The conversation shows the current step while it observes, acts, and verifies. Every button press must name its exact visible label and expected new visible text. Each step re-reads the same target app's AX state, uses the existing deterministic plan endpoint, calls the existing native AX action, and submits the fresh observation to the existing verifier before the next step begins. A failed or inconclusive step stops the sequence and reports its position and count of earlier verified steps; prior side effects are not rolled back. No new action executor or planner was introduced.

`chatComputerUseAction.test.ts` passes 8/8, including English/Japanese parsing, quoted “then” handling, step-by-step fresh observation/progress, and fail-stop on an unverified effect. The full frontend suite passes 121/121, typecheck and production build pass (existing large-chunk warning). This is bounded explicit multi-step execution, not generalized natural-language native planning; each action still needs user-supplied expected visible text, and a live macOS capture/OpenCV/UI-Venus sequence has not yet been verified. CX5/CX7 remain PARTIAL/OPEN respectively.

### Follow-up: goal-derived local browser postconditions (2026-10-03)

The local E2E natural-language planner no longer requires the user to quote the exact visible result text when the requested outcome can safely supply a concise postcondition. Click, type, and navigation targets remain grounded in current page evidence; text-entry values still must come from the user's request; navigation still must resolve to a visible same-origin link. A model-derived `wait_for_text`/`assert_text` is accepted only after a grounded action in that plan and only if its phrase is absent from the pre-action accessibility snapshot. The existing Playwright executor must then confirm it in a fresh accessibility snapshot before reporting the effect. This reuses `validate_chat_local_e2e_plan`, `BrowserSessionManager`, and the existing executor; no new browser runner or verifier was added.

Backend `test_chat_intents.py` plus `test_chat_scrape_flow.py` passed 36/36, including a managed-Playwright fixture request “Start the game and confirm that it begins” whose inferred “Started:” postcondition was observed after clicking the grounded button, plus a regression rejecting “Started:” when it was already in the initial snapshot. This is fixture/API evidence with a mocked model plan, not new live selected-model acceptance. CX2 remains PARTIAL pending broader model quality, screenshot-viewport interaction, public-page continuation, and reconnect/recovery evidence; CX7 remains OPEN.

### Follow-up: chat-directed public-page read navigation (2026-10-03)

When a chat-owned scrape task is waiting for Recipe clarification, the user can direct its existing managed Playwright session to follow an exact visible same-origin link or activate a narrowly allowlisted read-only reveal button (for example, “Load more”). Links dispatch as actual Playwright clicks so SPA handlers run, with origin checked both before and after dispatch. The task retains its conversation/session identity and receives the refreshed URL, title, and accessibility snapshot before Recipe validation is retried. The manager rejects cross-origin links, known transactional links, ambiguous controls, non-allowlisted buttons, form-associated buttons, and submit buttons; public scrape interactions remain separate from project-local E2E task scope. Other transactional actions and interactions after a ScrapeRun starts remain outside this small capability.

Backend `test_chat_intents.py`, `test_browser_network.py`, and `test_chat_scrape_flow.py` most recently passed 66 tests plus 8 subtests, with one existing Starlette/AnyIO deprecation warning. The tests cover same-origin navigation, the exact current label for Load more, unsafe target rejection, ambiguous task selection, and refreshed conversation task evidence. This is fixture/API evidence, not live public-site acceptance. CX3 remains PARTIAL pending broader chat repair, live-site coverage, and task reconnect evidence; CX7 remains OPEN.

### Follow-up: goal-directed read-only public-page navigation (2026-10-03)

During a chat-owned scrape's pre-Recipe clarification phase, a user can now say “go to the next page” without naming a page control. `BrowserSessionManager.scrape_readonly_control_candidates()` returns only fresh same-origin anchors and fixed read-only reveal buttons, and returns a bounded set with a truncation marker. The configured local main model may choose one exact label from this list; deterministic validation rejects missing/duplicate labels or truncated evidence. The existing `click_scrape_readonly_control()` then takes fresh evidence again and rechecks unique target, same-origin destination, mutating labels/paths, a conservative read-only query-key allowlist, and the fixed reveal-button allowlist immediately before Playwright dispatch. URL query values are not forwarded with candidate evidence. Ambiguity stops in chat. This stays within the existing public scrape task, browser session, Recipe-validation and ScrapeRun lifecycle; it adds neither a new browser nor an arbitrary public action executor. The action is only available before the Recipe run exists.

Backend intent, API/managed-scrape and browser-manager fixture tests cover English/Japanese next-page intents, model-selected same-origin navigation, duplicate-label clarification, existing exact-label Load more, unsafe labels/targets and preservation of the pending task. These are fixture/API tests, not live public-site acceptance. CX3 remains PARTIAL; screenshot interaction, richer read-only page operations, active-run interaction and public-site behavior remain open.

### Follow-up: outcome-directed native button target selection (2026-10-03)

Native chat can now accept a one-step goal such as “Start the game and verify ‘Game started’” without asking the user to identify the exact button label. The exact visible result remains user-provided. The existing Laya/SemanticActionRuntime chooses among at most eight currently visible, enabled AX buttons that expose AXPress and have unique accessible labels; larger candidate sets, duplicate labels, missing expected text, stale/mismatched app identity, or an expected phrase already visible before the action are rejected. Execution still goes through the current native accessibility command and the existing plan-bound verifier, which requires fresh AX evidence of the requested text. Explicit multi-step sequences remain bounded and still require exact labels and expected text per button. This is a chat-routing and candidate-selection increment, not a generalized native planner, inferred postcondition, autonomous loop, or live GUI acceptance.

`backend/tests/test_computer_use.py` plus `backend/tests/test_api.py` passed 49/49; `backend/tests/test_chat_intents.py` plus `backend/tests/test_chat_scrape_flow.py` passed 39/39. The full frontend suite passed 123/123 across 20 files, TypeScript typecheck and `git diff --check` passed. This is unit/API evidence with mocked Laya/native bridges; it does not establish live capture, OpenCV tracking, UI-Venus pointer execution, or successful native E2E. CX5 remains PARTIAL and CX7 remains OPEN.

### Follow-up: outcome-only native button goals from chat (2026-10-03)

An active Computer Use session can now accept a single ordinary outcome request such as “Start the game” without requiring a quoted result or exact button label. The configured local `settings.main_model` proposes exactly one button label from currently visible enabled AXPress candidates and one concise visible postcondition from the chat goal and fresh AX context. Deterministic validation requires matching app identity and AX permission, a unique exact visible button label, a postcondition absent before dispatch, and a target label outside a bounded English/Japanese high-impact blocklist. Ambiguous, unsafe-labelled, or unusable plans return a chat clarification. Existing action-plan issuance, native AX dispatch, and fresh-text verification remain responsible for execution and success reporting; no parallel runtime was added.

The parser admits outcome-only goals only while a Computer Use session is running and excludes scrape/browser/local-E2E intents. The target is still the currently selected, in-scope app; this increment does not turn one request into a native multi-step plan, control pointer-only/canvas targets, or persist native task lifecycle across refresh. Backend Computer Use/API tests passed 53/53, chat intent and scrape-flow tests passed 39/39, and the full frontend suite passed 124/124 across 20 files with TypeScript typecheck. These are fixture/API/UI tests with mocked model and native bridge, not live macOS capture, OpenCV tracking, or UI-Venus acceptance. CX5 remains PARTIAL and CX7 remains OPEN.

### Follow-up: single-message scoped native start and action (2026-10-03)

One chat request may now contain a scoped Computer Use start clause followed by either one supported outcome-level action or the existing explicit bounded sequence. The frontend resolves the start clause separately, asks the existing target resolver to attach or launch only within Settings' allowlist, starts ScreenCaptureKit, persists the task event, then runs the action through the current fresh AX plan/execute/verify path. The action clause does not create a duplicate user message; the task event records that the same request will continue. If target resolution, capability policy, or capture startup fails, no follow-up action is dispatched. This reuses the existing lifecycle, target resolver, action router, semantic planner, native executor and verifier.

Validation passed the full frontend suite (126 tests across 20 files), TypeScript typecheck, `backend/tests/test_api.py` plus `backend/tests/test_chat_scrape_flow.py` (53 tests), `py_compile`, and `git diff --check`. This is focused fixture/API/UI evidence. No live GUI, TCC, ScreenCaptureKit frame, OpenCV, or UI-Venus action evidence was collected. CX5 remains PARTIAL; CX7 remains OPEN pending outcome-derived multi-step planning, public/local/native follow-up across refresh, and live acceptance.

### Follow-up: iterative native goal execution (2026-10-03)

Outcome-only native chat actions now re-observe AX after each verified button action and ask the configured local main model to choose exactly one next step or declare completion with a phrase present in current AX text. The loop records the original user request once, supplies prior verified effects as progress context, and stops after four dispatched actions; completion evidence must be present in fresh AX text, while ambiguous steps or a missed four-action limit return a chat clarification. Each button still passes the existing exact-label grounding, semantic action route, AX dispatch receipt, and fresh-effect verifier. Explicit action sequences retain their separate 2–4-step contract. This is bounded iterative replanning, not a generated stale action list or unrestricted agent loop.

Validation passed the full frontend suite (127 tests across 20 files), TypeScript typecheck, `backend/tests/test_api.py` plus `backend/tests/test_chat_scrape_flow.py` (54 tests), `py_compile`, and `git diff --check`. The tests mock the local model and native bridge. No native GUI action, ScreenCaptureKit frame, OpenCV tracking, or UI-Venus acceptance has been demonstrated live. CX5 remains PARTIAL and CX7 remains OPEN.

### Follow-up: exact chat launch still blocked by Screen Recording authorization (2026-10-03)

In isolated OpenCV run `20261003-083000-cu-tcc-reopen-8c42bb0a`, the fresh signed app reached `READY_FOR_COMPUTER_USE=YES`; the health build ID, app-support path, and DB path matched the unique run. A fresh profile stored Computer Use enabled with only `local.olcr.cu-fixture.20261003-055655-EDA0A344E5` in scope. A single chat request launched the exact project-local fixture bundle as PID 55211 and entered the native capture path. `CGRequestScreenCaptureAccess()` returned false and a subsequent CoreGraphics preflight also returned false before ScreenCaptureKit produced a frame. The fixture's “Start game” button was not clicked; no OpenCV processing, pointer dispatch, or postcondition check occurred.

System Settings showed the OLCR Screen & System Audio Recording switch ON. With the fixture and OLCR closed cleanly, an attempt to refresh the grant through the System Settings switch opened a macOS Privacy & Security Touch ID/password sheet before changing the permission; the sheet now awaits user authentication. The current run's backend PID 54855 was stopped after the app-menu quit. This is stronger evidence that exact chat target resolution and launch work, but the mismatch between the visible ON switch and CoreGraphics preflight is unresolved. Do not treat the Settings row as proof of capture authorization or repeat a GUI capture attempt until macOS authorization can be reconciled. P3/V1, live OpenCV, A5 live pointer action, and CX7 remain open.

### Historical observed chat-first baseline (2026-10-02; superseded for native target scope by the 2026-10-03 checkpoint above)

The implementation is not yet at the product acceptance above. Record these as current states; do not infer completion from the existing browser, capture, or task APIs:

|Gate|Current evidence|State|Remaining acceptance|
|---|---|---|---|
|CX1 — chat intent and target|`chat_scrape_request()` recognizes a narrow explicit HTTP(S) scrape request; `chat_local_e2e_request()` recognizes explicit local E2E start/attach requests, optionally constrained to an exact loopback URL, a named `dev`/`start`/`serve` package script, or an explicitly quoted one-off command; native Computer Use has disjoint session lifecycle intents plus labelled disclosure-open, explicit Find/Search-field and exact-label button-plus-expected-text requests. Scraping/E2E requests are excluded from native routing.|PARTIAL|General browser-task intent and clarification remain; one-off commands must be supplied by the user and explicitly bind one loopback host and port; model-inferred commands are not yet supported. Native action parsing remains intentionally narrow and requires the selected app/session; automatic focused-app resolution remains open. Ask in chat when target or script is ambiguous, and fail closed when scope is unclear.|
|CX2 — managed browser from chat|Chat opens the existing `BrowserSessionManager` for public scraping or an exact-origin local E2E session and persists a task block. A session-bound screenshot viewport is rendered in the conversation. Local E2E accepts up to six explicitly delimited click, text-entry, same-origin path-navigation, wait-for-text and assert-text actions; each uses fresh page evidence, stops at the first failed step, reads typed values back, and checks wait/assert against a fresh accessibility snapshot. Alongside exact syntax, a structured planner converts natural-language requests into at most six typed actions grounded in visible labels, user-provided values/expected text, and same-origin links, then reuses the same Playwright executor/verifier. The managed-Playwright localhost fixture verifies fill/click/click/wait/assert, refreshed screenshot and owned-process cleanup with a mocked model plan. In isolated GUI run `20261002-233907-2a832907`, explicit bounded actions worked but the selected-model natural-language request returned a stale server-start error while the task was READY. The rejection path was corrected so invalid/non-action planner output returns a scoped plan error rather than falling through to regular chat. In isolated GUI run `20261003-mur3rj4f-d8016972`, the same selected-model route returned `EXECUTE ACTION_COUNT=3`, filled `Player name` with `Nagi`, clicked `Start` and `Continue`, and the managed browser plus fresh accessibility observation showed `Started: Nagi - Continued`; chat then stopped the owned task. This is one live success, so model quality, viewport reconnect, task recovery and broader project acceptance remain open.|PARTIAL|Broaden live planner quality, viewport reconnect and task recovery acceptance, then continue live project acceptance.|
|CX3 — URL scraping from chat|An explicit request opens the page, proposes a Recipe from visible-DOM candidates, reuses Preview & Validate, and starts `ScrapeRunManager` when the grounded Recipe passes. A recoverable draft/validation failure now persists a pending task, asks for fields in chat, and retries against the same managed browser when the user answers; Studio remains optional for detailed review. A managed-Playwright fixture completes the run and chat result/export paths are covered. Public live-site evidence remains open.|PARTIAL|Complete live-site evidence and broader conversation/reconnect acceptance without weakening destination or Recipe validation. Never treat page content as instructions.|
|CX4 — in-conversation lifecycle|Short explicit chat controls route status, pause, resume, stop, bounded result display, and saved-run export to the latest unambiguous conversation-owned ScrapeRun; the embedded card restores and polls its run. A failed Recipe now leaves a pending task that accepts free-form field clarification through the same browser/Recipe validation path, and pending tasks can be stopped from chat. Repeated delivery of a successfully persisted source message returns the same task. Explicit selection across multiple runs and live reconnection acceptance remain incomplete.|PARTIAL|Add unambiguous target selection across multiple tasks and test task-control persistence, duplicate delivery and refresh/reconnect.|
|CX5 — user-owned local E2E|The current implementation requires `local_e2e_enabled`; from chat it attaches to an explicitly supplied loopback origin or starts a selected allowed project through `.olcr/e2e.json`, one recognized `dev`/`start`/`serve` script in `package.json`, or a user-supplied one-off command parsed as shell-free argv. The command must bind exactly one loopback host/port; an optional origin must match. After readiness, the task-owned origin is pinned in the managed Playwright session and shown in the conversation. In isolated GUI run `20261002-233907-2a832907`, chat launched the fixture and viewport and explicit bounded actions worked; selected-model natural-language planning still failed. In isolated GUI run `20261003-mur3rj4f-d8016972`, the user-supplied shell-free loopback command started from chat, the selected model planned three grounded Playwright actions, the visible page state was verified, and chat stop marked the task STOPPED and closed its owned fixture. Quitting the exact test bundle left its detached backend briefly alive; the run-specific PID and listener were then reaped and both run ports verified closed. Native Computer Use separately starts/stops/status-checks capture for a target already selected in Accessibility scope and exposes three narrow verified AX actions (disclosure, Find/Search, exact-label button plus user-specified expected text). Native action tests mock the Tauri/API bridge; this browser run produced no native frames or tracking, and the earlier isolated native capture run remained blocked at `NoShareableContent`. Chat-driven native target resolution/launch, general native planning, UI-Venus/OpenCV pointer fallback through chat, screen-capture authorization resolution and live native E2E remain open.|PARTIAL|Connect chat target resolution and native launch to existing allowlists/process ownership, route bounded native plans through existing AX and grounded UI-Venus/OpenCV paths, and complete live permission/capture and wider user-project acceptance.|
|CX6 — Settings as capability/scope|Default-OFF capability toggles, allowed workspace/origin/app scope, and macOS permission state/request live in Settings. App scope is stored as policy, including the native bundle-ID allowlist; chat resolves the particular task target from that scope. Observation, monitoring, launch, and workflow controls do not belong in Settings.|PARTIAL|Keep Settings limited to capability, allowed scope, and OS permission setup; move its remaining Accessibility observation/monitoring/World State diagnostic controls to chat task controls or a separate diagnostics surface. Keep credential setup as configuration and enforce scope when a task runs.|
|CX7 — chat-only E2E orchestration|Browser scraping already starts from an explicit URL request and reuses Recipe/Preview & Validate/ScrapeRun; local browser E2E starts from chat and now has one live selected-model natural-language action journey with visible task state, verified result and chat stop, in addition to mocked planner coverage. Native chat starts scoped capture and routes bounded AX/OpenCV/UI-Venus actions through chat. Settings remains capability, scope and OS permission setup. The current acceptance is still a set of chat-started vertical slices rather than one continuous chat-owned task spanning scrape/browser/native use.|OPEN|Make chat the sole target-resolution, task-launch and follow-up surface: resolve the scoped project/app/site and ask in chat for missing choices; start or attach to the authorized target; show live browser state and progress in the conversation; embed a compact live view of the native target window in its Computer Use task card, accepting frames only when capture session, PID and bundle ID match that task and cropping away unrelated desktop content; translate bounded natural-language goals into existing Playwright or AX/OpenCV/UI-Venus actions; verify requested effects; and retain status/pause/stop/results across refresh. Keep Settings limited to capability toggles, scopes and OS permission setup; reuse existing runners and permission stores.|

Current user-visible paths are partly aligned with the target. An explicit URL scraping request starts a managed-browser task from chat, proposes and validates a Recipe, asks follow-up questions in chat when fields are ambiguous, and returns results/export there. Its in-chat screenshot viewport displays page state but is not a direct pointer surface; page interaction is still commanded through chat/Playwright. Public live-site acceptance remains open. For local browser E2E, chat attaches to a supplied loopback URL or starts the selected allowed project from its `.olcr/e2e.json`, a recognized package script, or an explicitly supplied shell-free command; the managed Playwright viewport, bounded natural-language action plan, visible effect verification, and task stop remain in chat. One selected-model live journey has succeeded; broader planner quality, reconnect/recovery, and project coverage remain open. Native Computer Use resolves a running app name or launches an exact allowlisted bundle from chat, then can start/status-check/stop ScreenCaptureKit. It supports three narrow verified AX action forms plus explicit two-to-four-step supported sequences, but not goal-level natural-language native planning. There is no successful live capture/action evidence because the latest exact-identity launch was blocked before its first frame by TCC. Settings holds capability, scope, and OS-permission setup; diagnostic observation controls are in a separate surface. Browser DOM-backed games use Playwright; chat-driven visual actions for canvas-only browser games remain unverified and must not be inferred from native OpenCV support.

### Chat-first acceptance examples (required product behavior)

- “このURLのページをスクレイピングして”: chat resolves the URL and collection intent, opens the managed browser surface inside the conversation, proposes/validates a bounded Recipe, runs it when safe, and returns progress/results there. If a field or repeated-row choice is genuinely ambiguous, it asks in chat and resumes from the answer. The user does not need to open Scrape Studio or a Settings test button.
- “選択中のプロジェクトを実E2Eで起動して、ログイン後に購入ボタンまで進めて”: chat checks the E2E capability and project/origin scope, starts only the declared project command or attaches to an allowed loopback app, then uses managed Playwright for DOM-backed browser work and presents the live page plus verified step results in the conversation. User-owned processes are never terminated; only a task-owned server can be stopped by chat.
- “Computer UseでCalculatorの設定を開いて”: after the capability, that app bundle ID is allowlisted, and macOS Screen Recording/Accessibility permissions are granted, chat resolves the target from its request and the allowlist and can launch the closed app by exact bundle ID. Chat asks there if the target is missing or ambiguous; it then uses AX first and the existing grounded visual/OpenCV/UI-Venus route where required, binding fresh evidence and verifying each effect. Session start alone does not fulfill the requested action. The current build supports three narrow AX action forms and explicit two-to-four-step sequences; goal-level natural-language planning and successful live capture remain open parts of CX7.

For browser games with DOM controls, prefer Playwright; for canvas-only browser games, use the managed browser's grounded visual route if its contracts support it; for native applications, use ScreenCaptureKit plus AX/OpenCV/UI-Venus. Do not create parallel browser, capture, or action runtimes to satisfy these examples.

## Model decision

ユーザー指定により GUI semantic escalation は UI-Venus-1.5-2B の既存 MLX 6bit deployment `ui_venus_mlx_6bit`、`role:gui_grounding` を使用する。モデル解決は `backend/olcr_api/model_registry.py`、呼出しは `model_provider.py` / `mlx_worker.py` を再利用し、manifest・readiness・revision を確認する。新モデルや新ランタイムを導入しない。FastVLM-0.5B を本計画の必須経路にしない。既存 general_image role は別用途として維持する。小型モデルで必ず成功するとの仮定を置かず、ROI と実機課題で精度・コストを測定する。既存 fallback はその設定・権限・readiness の範囲で使用する。GUI role の反復呼出しでは既存専用 MLX worker を再利用し、明示的に開始された ScreenCapture の直後にローカル fixture で暖機してから画面を pin する。worker 起動を毎フレームの遅延へ繰り返し加算しない。frame/tracker 鮮度条件は fail-closed のまま保ち、暖機後の ROI 推論が鮮度上限を満たさなければ live result は `INCONCLUSIVE` とする。

## OpenCV AI Hackathon priority

OpenCV use is a project priority for the hackathon. Add OpenCV-backed CV work to the existing ScreenCaptureKit, provenance, UI-Venus and tracker contracts; preserve current behavior through an explicit feature-off fallback. Prefer OpenCV for bounded image operations, temporal change detection, connected-component ROIs and seeded optical-flow tracking where it fits. The opt-in path now receives APC compute levels: L0 runs change/scene sentinels and defers components, while L1+ recovers the pending changed ROI and skips component labeling on stable frames. Keep OS capture, AX semantics, OCR and UI-Venus semantic interpretation in their current roles. Do not create a second capture/action pipeline. The `opencv-perception` feature remains opt-in: an arm64 signed self-contained app package was verified on the current host with 96 bundled dylibs (76.5 MiB native dependency closure, 93 MiB app) and launched with `DYLD_LIBRARY_PATH` and `DYLD_FALLBACK_LIBRARY_PATH` unset. Clean-machine runtime, live capture/cost, and end-to-end task-success evidence remain unverified, so keep the feature off by default. For local development, `cd frontend && npm run tauri:dev:opencv` opts into the feature, restricts direct OpenCV links to the six tracker modules, and adds the Xcode Swift runtime fallback; this is a development launcher, not release or GUI acceptance evidence.

## Existing implementation / reuse map

パスは OLCR ルート相対。以下はコード確認済みの接続先であり、機能全体の PASS 宣言ではない。

|領域|既存接続先|修正方針・不足|
|---|---|---|
|Planner / task lifecycle|`backend/olcr_api/implementation_plan.py`, `coding_tasks.py`, `orchestration.py`, `runtime.py`|既存の実行・中断・再開・回復を拡張。第二のタスクエンジンを作らない|
|Laya / tools / policy|`laya_runtime.py`, `tools.py`, `auth.py`, `capability_scope.py`|候補選択、allowlist、認可を維持。Action Router は機構選択の拡張であり Laya を置換しない|
|Perception|`vision.py`: `ScreenState`, `DOMAXObservationProvider`, `VisionRouter`|既存型へ互換拡張。WorldState は薄い envelope。現コードは AX 優先であり概念 ladder に合わせた無条件の優先順変更をしない|
|Actions / verification|`providers.py`: `DeterministicActionExecutor`, `SemanticActionRuntime`; `computer_use.py`|既存 allowlisted handler と postcondition 検証を拡張。handler 成功を効果の CONFIRMED と同一視しない|
|Pointer execution|The current source supports a narrow UI-Venus + frame-pair tracker + Core Graphics click fallback for a uniquely grounded, selected-app `AXButton` when AXPress and usable AX geometry are unavailable; it also retains the bounded disclosure fallback. The live fixture journey `20261003-220000-scrollfix` verified one tracked button click and a fresh AX postcondition. This is not an arbitrary screen clicker, and representative coordinate calibration is still open.|Keep AXPress first; allow the visual fallback only for a unique fresh AX target under the selected app/session/window identity and require independent post-action verification. A4/A5 remain partial until representative live coordinate validation and physical post-action acceptance pass|
|Frontend Computer Use|`frontend/src/screenObservation.ts`, `computerUse.ts`|現状は可視 DOM と限定された disclosure 操作。任意の macOS アプリ操作・OS AX action が実装済みとは扱わない|
|Desktop / capture / AX|`desktop/src-tauri/src/main.rs`, `screen_capture.rs`, `accessibility.rs`|既存 Tauri の native adapter と ScreenCaptureKit を採用。macOS AX は安全な `axuielement` wrapper と Apple AX API を利用し、独自 FFI/tree protocol を作らない。許可はユーザー操作から要求し、値を除外した bounded snapshot を既存 ScreenState へ互換化する。AX 通知・操作・座標入力の不足だけ段階追加し、専用の別アプリを作らない|
|Routines|`procedures.py`: `Procedure`, `ProcedureRunner`; `db.py`: procedures / tool_executions|新 DSL・DB・workflow engine を作らず既存 step schema を拡張。既存 validated bool だけでは環境適合・毎step検証・鮮度の保証にならないため追加する|
|Web Read|`web.py`: `fetch`, `validate_url`, `_SafeRedirects`, `BrowserProvider`|既存 fetch の出力・呼出し契約を保ち main-content / Markdown を追加。BrowserProvider は unavailable の stub であり browser 完成扱い不可|
|Browser|`mcp_manifest.py`, `mcp_runtime.py`, `node_mcp_runtime.py`|Playwright MCP が既に登録されるが policy 無効・localhost 制限・許可 tool 限定。ユーザー向け Web scraping が既に利用可能とは言わない|
|Persistence / telemetry|`db.py`, `model_telemetry.py`, `coding_telemetry.py`|既存 DB migration・task id・model revision と接続。フレームごとの重量ログを作らない|
|Tests / benchmarks|`backend/tests/test_vision_router.py`, `test_computer_use.py`, `test_semantic_actions.py`, `test_web.py`, `test_model_roles.py`; `coding_benchmark.py`, `api_acceptance.py`, `scripts/api_live_e2e.py`|既存検証入口に追加。保存済み fixture 成功を OS 実機成功や計算量削減の根拠にしない|
|Frontend|`frontend/src`, `frontend/package.json`|既存 UI / API に Scrape Studio を追加。WebView が外部 Chromium の DOM を直接読めると仮定しない|

## Build vs adopt decisions

- Routine compiler/cache: PARTIAL_REINVENTION。既存 ProcedureRunner と SQLite migration を採用し、trace → bounded steps、preconditions、verification、version、stats、disabled 状態のみ追加。実行途中で検証不能なら止め、再観測して既存回復に返す。単発成功から自動で validated にしない。
- Native Accessibility: macOS AX protocol は Apple ApplicationServices、Rust 呼出しは `axuielement` safe binding を採用する。OLCR 固有なのは許可 UX・対象 tree の上限・要素絞り込み・既存ScreenStateへの provenance mapping。AXValue、全文dump、独自AX FFIは追加しない。
- Router/state/verification: PARTIAL_REINVENTION。既存 provider 契約の上で適合する経路を選択する。別々の Router サービス、ElementRef hierarchy、verification framework を先に全面構築しない。domain 固有 evidence と stale 判定を残す。
- Browser sessions/network/pagination: PARTIAL_REINVENTION。既存 Playwright runtime の所有・停止・timeout を再利用する。MCP tool が足りない場合は同じ管理系に必要な Playwright API adapter を足す。DOM 操作・waiting・network events は Playwright を採用し独自実装しない。公共 Web 許可は scraping 専用 scope として設計し、既存 localhost E2E policy を一括で緩めない。
- Scraper recovery: P26 reuses the managed session's fixed DOM picker and fixed read-only selector probe, capped at 12 picker-derived row selectors per request. Preview/Validate and runtime runs store only selector/match-count evidence. If a validated DOM Recipe produces no rows or configured fields match no cells while bounded alternatives exist, the run stops as `SELECTOR_RECOVERY` before checkpointing that page. Choosing an alternative returns the Recipe to `DRAFT`; resume requires a newer `VALIDATED` version of the same Recipe and retries the failed page. For JSON responses, reuse the pinned Playwright network tools and the existing Recipe lifecycle: explicit user-authored JSON Pointer mappings, same-origin endpoint path only, 64 KiB response cap, sensitive-key redaction, bounded rows/fields, no body persistence, and the same Preview & Validate/run/checkpoint/export path. Ambiguous or unavailable responses fail closed. Do not add another browser, interception, or extraction runtime. Keep OCR/Vision and wider user-intervention fallbacks open.
- Web Read / Scrapling boundary: Scrapling's original-plan role is W1 HTML parsing/content extraction only. OLCR retains ownership of its SSRF-safe HTTP fetch, redirect/DNS policy, byte/context caps, and output contract; an optional Scrapling adapter may parse the already-fetched HTML and return title/content/links into that contract. Do not use Scrapling Fetcher, DynamicFetcher, StealthyFetcher, its browser/session, proxy rotation, crawler/spider, anti-bot or stealth features. W2–W16 remain on the one managed Playwright browser for rendered DOM, navigation, waiting, network evidence, picker and Recipe execution; OLCR retains Recipe validation, persistence, checkpoints, dedupe and export. Scrapling's adaptive parser is not a second automatic Recipe store: consider it only as a bounded selector-recovery candidate generator if current Playwright picker/probes show a concrete miss, and require user review plus live Playwright Preview & Validate before the canonical Recipe changes or runs. The current code has not adopted Scrapling; first compare the existing safe-fetch + `_Text` path against an optional parser adapter on representative fixtures and check Python compatibility, transitive dependencies, license and packaging. Adopt only if a measured extraction/recovery gap is improved without weakening the existing policy or lifecycle.
- CV / OCR / native capture: ScreenCaptureKit を採用。OpenCV AI Hackathon priority supersedes the earlier measurement-gated dependency decision: the additive `opencv-perception` feature now applies OpenCV 5 to bounded frame change/ROI operations and seeded optical-flow tracking within the existing capture/tracker contracts. Keep the existing Rust `image` and Apple Vision paths as feature-off fallbacks and comparison baselines; keep AX, OCR, and UI-Venus semantic interpretation in their current roles. Do not create a second capture/action pipeline. The opt-in packaging path now bundles its Homebrew-linked dylibs and verifies a signed self-contained macOS app on the current host. Keep the feature opt-in until live capture, supported-device rate/cost, clean-machine runtime, and task-success evidence pass. OCR continues to use the suitable OS/project engine rather than a new OCR implementation.
- Streaming: Python csv/json と既存 artifact 出力を採用。大規模 dedupe は SQLite UNIQUE key 等を利用し RAM の集合だけに依存しない。pause/resume は既存 lifecycle + checkpoint を使い、無検証の exactly-once を主張しない。

水平再利用は latest-frame mailbox（古いフレームを積まず上書き、分析の遅延を制限）と、step 実行 journal + checkpoint（中断後の再実行前に効果を再観測）を採用候補とする。処理しなかった短い画面変化、クラッシュ直前の未確定アクションが制約。新しい event bus や workflow engine を追加しない。

## Compatibility / security corrections

- 新しい CONFIRMED / NOT_CONFIRMED / INCONCLUSIVE は内部 verification result として追加し、既存 PASS / BLOCKED / verified boolean の外部契約を互換 adapter で維持。NOT_CONFIRMED は失敗が観測された場合、INCONCLUSIVE は観測不足の場合として区別する。
- AX notification と DOM event は invalidation のヒントであり authoritative truth ではない。session/window id、observation revision、timestamp、coordinate space / scale、TTL を照合する。フォーカス移動・resize・画面変更で cached target を無効化し、action 直前に適合確認。
- Routine match は normal planning 前の bounded fast path。authorization と preconditions が成立しなければ既存 Laya/planner へ戻る。Routine と action mechanism を同じ ladder の相互排他的 route として実装しない。
- URL policy は既存 validate_url / redirect 検査を拡張する。HTTP fetch・search provider は全DNS解決結果を検査し、socketを検証済みIPへ固定しつつTLS SNI/証明書検証は元hostnameで行う。検証済みの同じ接続でない環境proxy経由は無効。Playwright は BrowserContext.route で navigation / subresource を検査し、request interception を迂回する service worker は block する。upstream origin/host allowlist はredirectやprivate-address再解決を止める保証として扱わず、Chromiumの実socketに同じ destination policy を強制できる方法を別途検証する。network extraction、detail links にも destination policy を適用する。loopback のbackend通信と明示的なtest fixtureは限定scopeの例外で、公開ページがprivate addressに接続できる例外にしない。
- Browser profile は OLCR 管理の authorized profile id を保存。既存 credential / Keychain lifecycle を維持し、Recipe に password / cookies を埋め込まない。external DOM/network/OCR は data plane。
- P5 Desktop World State は既存 `ScreenState` を SQLite へ正規化して再利用し、対象アプリ session ごとに最新状態1件だけを保存する。GUI session 認証、256 KiB 上限、最大32 session、10分TTLを適用し、frame/screenshot履歴は保存しない。保存と復元は画面から明示的に開始し、event は最新状態の更新ヒントとしてだけ扱う。
- macOS GUI 実機検証は AGENTS.md の exact bundle path / backend PID / health build_id / profile 確認および Keychain restoration / signing 条件に従う。

## Revised execution order and completion

P0 既存差分を保持し baseline / readiness / native・browser 能力を監査。
P1 既存 ScreenState / provider / verification の最小互換拡張。
P2 既存 Web Read 改善と scoped browser adapter の接続可能性を検証。
P3–P11 native capture / AX / CV / APC / ROI を段階追加し UI-Venus で semantic escalation を検証。
P12–P15 既存 executor に AX / shortcut / deterministic input を追加して閉ループ検証。
P16–P17 Procedure ベースの bounded Routine MVP。
P18–P27 managed browser / picker / Recipe / deterministic execution / navigation / export / detail / network / recovery / NL preview。
P28–P30 本体統合、回帰、実機、比較実験、文書・最終レポート。
依存順を調整してよいが各 P0–P30 と V/A/R/W/U acceptance の status と evidence を追跡する。未実装を reuse 済みという理由で PASS にしない。OCR は評価結果により採用または根拠付き N/A。全件成功・必要な build/unit/integration/live/benchmark が測定根拠を持つまで goal を complete にしない。外部要因で実行不能なものは具体的 blocker として残す。

## Primary references checked

- Playwright network events / response API: https://playwright.dev/python/docs/network
- Apple ScreenCaptureKit: https://developer.apple.com/documentation/screencapturekit
- Apple AX system-wide focused element: https://developer.apple.com/documentation/applicationservices/1462095-axuielementcreatesystemwide
- Apple AX trust / permission prompt: https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions
- Apple AX position and global screen coordinates: https://developer.apple.com/documentation/applicationservices/kaxpositionattribute
- Apple AXValue can contain editable field contents: https://developer.apple.com/documentation/applicationservices/kaxvalueattribute
- `axuielement` safe Rust bindings (MIT OR Apache-2.0): https://docs.rs/axuielement/0.10.0/axuielement/
- Apple Accelerate / vImage: https://developer.apple.com/documentation/accelerate/vimage-library
- Scrapling capabilities / optional adoption evaluation: https://scrapling.readthedocs.io/en/latest/

---

# AI_IMPL_PROMPT
# OLCR Unified Perception, Action & Scraping Architecture v3 — Repository-aligned

## 0. Mission

既存OLCR repoに、以下を**OLCR本体の正式な機能として統合実装**する。

```text
Track V — OLCR Vision / Computer Use
Track W — Web Read / Scrape Studio
Track U — Unified State / Verification / Recovery
Track A — Deterministic Action Architecture
Track R — Verified Routine Compilation
```

独立したデモアプリ、ハッカソン専用fork、別Computer Use stackを作らない。

既存OLCRの：

- Planner / Reasoner
- Laya
- Tool Router
- model runtime
- 既存UI-Venus GUI Vision model
- Computer Use
- target-PID keyboard shortcut injection
- telemetry
- task lifecycle
- config
- frontend
- state management
- benchmark infrastructure

を最大限再利用する。

OpenCV AI Competitionでは後から **OLCR Vision / Adaptive Computer Use部分を重点的にsubmissionする**。

---

# 1. Core Principle

OLCRのComputer Useを、

```text
Screenshot
→ VLM
→ coordinate prediction
→ mouse
→ Screenshot
→ VLM
→ ...
```

から、

```text
Structured information
+
Persistent state
+
Deterministic perception
+
Deterministic action
+
Adaptive computation
+
Semantic inference only on uncertainty
```

へ移行する。

基本原則：

> If the system already knows something deterministically, do not ask a semantic model to rediscover it.

> If nothing meaningful changed, do not reinterpret it.

> If only one region is uncertain, do not send the whole screen.

> If the OS exposes an action directly, do not reproduce it with pixel-level mouse movement.

> If a successful semantic execution can safely become a deterministic routine, do not reason through it again every time.

---

# 2. Two Ladders

Computer Useを2つの独立したladderとして設計する。

## 2.1 Perception Ladder

```text
Structured API / Network
        ↓
DOM
        ↓
Accessibility
        ↓
Cached State
        ↓
OpenCV
        ↓
OCR
        ↓
UI-Venus GUI Vision
        ↓
higher semantic reasoning only if existing OLCR provides it
```

## 2.2 Action Ladder

```text
Native / Structured Action
        ↓
Playwright Action
        ↓
Accessibility Action
        ↓
Verified Cached Routine
        ↓
Keyboard Shortcut
        ↓
Deterministic Pointer / Keyboard
        ↓
Vision-grounded Pointer Action
```

These are not rigid global orders.

Select the cheapest reliable mechanism appropriate to the current task and environment.

---

# 3. Overall Architecture

Target conceptual architecture:

```text
                         USER GOAL
                            │
                            ▼
                     Planner / Laya
                            │
                            ▼
                        Tool Router
                            │
                            ▼
                        WORLD STATE
                            │
              ┌─────────────┴─────────────┐
              │                           │
       PERCEPTION ROUTER             ACTION ROUTER
              │                           │
      Structured/API                Native action
              ↓                           ↓
             DOM                    Playwright
              ↓                           ↓
       Accessibility                  AX Action
              ↓                           ↓
        Cached State              Routine Cache
              ↓                           ↓
           OpenCV                    Shortcut
              ↓                           ↓
            OCR                Deterministic input
              ↓                           ↓
         UI-Venus GUI Vision           Vision-grounded input
              │                           │
              └─────────────┬─────────────┘
                            ▼
                          ACTION
                            │
                            ▼
                       VERIFICATION
                            │
                 ┌──────────┴──────────┐
                 │                     │
              SUCCESS               FAILURE
                 │                     │
                 ▼                     ▼
        Routine Candidate          Recovery
                 │                     │
                 ▼                     ▼
         Routine Compiler             Laya
                 │                     ↺
                 ▼
           Routine Cache
```

---

# 4. Execution Contract

## Repo-first

Before modifying code, inspect the actual repository.

All names in this document such as:

```text
WorldState
ElementRef
ActionRouter
AdaptivePerceptionController
RoutineCompiler
```

are conceptual roles.

If OLCR already contains an equivalent abstraction, extend it.

DO NOT create parallel abstractions merely to match this prompt.

## Do not stop at planning

Perform:

```text
audit
→ implementation
→ tests
→ integration
→ benchmark where feasible
→ report
```

Do not stop after writing an implementation proposal unless a genuine blocker prevents safe implementation.

## Do not invent

Never invent:

- paths
- classes
- model identifiers
- APIs
- config systems
- schemas
- build commands
- test commands
- IPC architecture

Inspect first.

---

# 5. Mandatory RepoCheck

Identify actual paths/components for:

```text
Planner
Laya
Tool Router

Computer Use
Screenshot / capture
Vision invocation
UI-Venus GUI Vision model
Model Router
Mouse executor
Keyboard executor
Scroll executor

World / Task / Session state

Web tools
HTTP client
Scrapling
Playwright
browser lifecycle

Telemetry
Benchmarks
Tests
Config
Frontend

macOS native integration
Accessibility support
```

Also search for existing equivalents of:

```text
WorldState
DesktopState
BrowserState
ElementRef
Observation
Action
ExpectedEffect
Verification
Workflow
Macro
Routine
Cache
StateMachine
Shortcut
Accessibility
```

Reuse before adding.

---

# 6. Reinvention Rules

Prefer mature existing technology.

Use:

```text
Screen capture        → ScreenCaptureKit
Accessibility         → macOS Accessibility API / AXUIElement
Computer vision       → OpenCV 5 for bounded pixel/ROI and seeded temporal tracking in the Hackathon build; retain existing Rust `image` and Apple Vision paths as feature-off fallback and comparison
Browser automation    → Playwright
HTML extraction       → existing safe fetch + evaluated content extractor; Scrapling optional
Browser structure     → DOM / Accessibility
OCR                   → existing OS/project OCR if suitable,
                        otherwise benchmark mature OCR before custom work
State transitions     → existing project state machinery if present
Input                  → existing OLCR executor / platform APIs
Serialization         → existing OLCR libraries
Telemetry             → existing telemetry
```

DO NOT hand-write replacements for mature primitives such as optical flow, template matching, CSS selection, OCR engines, browser rendering, accessibility protocols, etc.

Custom work is justified primarily for:

```text
Adaptive Perception policy
Perception Router
Action Router
cross-source state/provenance
semantic escalation policy
verification policy
recovery policy
routine compilation policy
OLCR integration
benchmarking
```

---

# TRACK V — OLCR VISION

# V0 — Baseline

Measure existing Computer Use before changing behavior.

Capture where possible:

```text
task_success
task_ms

actions_total
actions_failed

vision_calls
vision_fullscreen_calls
vision_inference_ms
vision_pixels_total

retries
```

Optional where reliable:

```text
CPU time
peak memory
```

Preserve reproducible baseline artifact.

---

# V1 — Continuous Screen Capture

Use ScreenCaptureKit.

Architecture:

```text
ScreenCaptureKit
↓
bounded frame stream
↓
Perception subsystem
```

Do not build the new architecture around repeated explicit screenshot requests.

Requirements:

```text
clean start
clean stop
bounded buffering
cancellation
no unbounded frame history
no orphan worker
```

---

# V2 — Multi-Signal Change Sentinel

Upgrade the original Change Sentinel.

Do NOT rely solely on pixel differences.

Combine cheap activity signals where available:

```text
                 ┌─ Accessibility notifications
                 ├─ OLCR input/action events
                 ├─ window/focus events
Activity Signals ├─ ScreenCaptureKit frames
                 └─ OpenCV pixel difference
                           │
                           ▼
                    Activity Estimator
                           │
                           ▼
                          APC
```

Pixel path:

```text
frame
↓
downsample
↓
grayscale if useful
↓
absdiff
↓
threshold
↓
changed regions / ratio
```

Event signals should be able to trigger perception immediately without waiting for periodic polling.

---

# V3 — Persistent Desktop State

Extend nearest existing state abstraction.

Conceptually retain:

```text
active_window
windows[]

ui_elements[]
    id
    semantic_label
    bbox
    provenance
    confidence
    visible
    last_seen
    tracking_state
    available_actions

regions[]

scene_epoch
motion_state
perception_state

semantic_cache[]
expected_effects[]
```

Do not duplicate existing state systems.

---

# V4 — Accessibility as Perception

Use macOS Accessibility as preferred semantic source for conventional native UI.

Acquire only useful/task-relevant information:

```text
role
label/title
value where appropriate
position
size
enabled
focused
selected
```

Accessibility answers:

```text
What is it?
What state is it in?
Where does the OS say it is?
```

Do not dump complete huge AX trees into Laya context.

---

# V5 — Accessibility Notifications

Where feasible use AX notifications to update World State and wake APC.

Examples:

```text
window created
window moved
focus changed
value changed
selection changed
UI element changed
```

Use events to reduce unnecessary visual polling.

Do not assume AX notifications cover every visual change; bounded pixel-change processing remains complementary. The hackathon OpenCV feature now adds seeded optical-flow tracking; further tracking modes or reacquisition logic still require measured fixture quality and cost.

---

# V6 — Bounded Temporal Visual State Engine

Use bounded temporal visual-state maintenance. For the OpenCV AI Hackathon build, enable the additive `opencv-perception` Cargo feature: OpenCV 5 then performs grayscale conversion, bounded resize, absolute frame difference, thresholding, connected-component ROI extraction and seeded Lucas–Kanade optical-flow tracking. The feature-off build retains the existing Rust `image` sentinel and Apple Vision tracker unchanged as compatibility fallbacks and comparison baselines. Seeded optical flow accepts only a grounded AX or UI-Venus box and returns through the existing tracker contract; it does not grant action authority. After the existing one-shot UI-Venus grounding and verified frame-pair seed, the opt-in path can arm an in-memory continuous LK tracker from that exact latest frame. A healthy explicitly grounded OpenCV track renews the existing APC `grounded_track_active` lease, entering L2/HIGH and requesting 20 fps on macOS 14+ within the runtime capture ceiling (macOS 13 remains capped at 2 fps). The 5-second lease is renewed by fresh track notices/heartbeats and ends on stop, loss, or expiry; capture stops LK if APC falls below L2. While tracking is healthy, native code emits only compact bbox/confidence metadata and the frontend pauses `latest_screen_frame` JPEG IPC and UI-Venus refresh. Capture frames still arrive in memory for LK; this avoids repeated JPEG/model requests, not frame acquisition. It stops on stale/gapped frames, session/target/geometry mismatch, confidence below 0.5, excessive displacement, or tracker error; loss resumes image refresh, and re-grounding remains an explicit passive user request. Frontend track notices are accepted only in strictly increasing sequence order within the active capture session, preventing an out-of-order older active notice from overriding a newer stop. The screen-capture p50/p95 is explicitly labeled as change-detector processing only; it excludes LK update and end-to-end perception latency. Continuous track notices separately report rolling p50/p95 for up to the latest 120 finite LK update timings; these exclude capture, IPC, UI, model, and action time. This reduces repeated image/model requests, not capture-frame acquisition. Apple Vision remains available when the feature is off. A synthetic 320×200 textured-box fixture passed the same 20-sample coordinate-error and IoU gates on both trackers. OpenCV returned `[0.31250036,0.35000098,0.56250036,0.55000097]`, confidence 1.0, p50 1.98 ms / p95 2.09 ms; Apple Vision returned `[0.31432608,0.3539923,0.5530946,0.54128504]`, confidence 0.914, p50 1.93 ms / p95 2.12 ms in its latest focused run. These synthetic fixture timings are session-specific, not broad performance claims or a controlled head-to-head speed test. OpenCV calls fail closed on invalid frames, insufficient features, low retained-flow fraction, high median LK photometric error, or out-of-bounds results. A new full-occlusion regression found LK could return 100/100 successful status bits, confidence 1.0 and a displaced box after the grounded control was replaced with a uniform patch. Continuous tracking now also rejects median OpenCV LK photometric patch error above 32/255; the synthetic moving-control fixture stays below 9/255 at p95 and the full-occlusion fixture exceeds 80/255 at its median. Full and 75% partial occlusion regressions now fail closed without advancing the stored bbox, while a +18/255 brightness-shift (hover-like) fixture continues tracking and the 20-frame continuous-motion fixture still passes. Latest full native suites pass 72/72 with `opencv-perception` and 69/69 feature-off. This heuristic is calibrated only on these synthetic fixtures; real UI state/appearance transitions, other partial occlusion patterns and tuning remain open. A separate OpenCV-only test, `opencv_tracker_updates_a_grounded_box_across_sequential_frames_in_memory`, advances one synthetic 320×200 fixture through 20 sequential frames. It ended at `[0.3750285,0.4000189,0.62502867,0.600019]`, confidence 1.0 and IoU 1.0; LK update-only p50/p95 was 0.43/0.57 ms in that run. This timing covers only `OpenCvFlowTracker::update`, excluding capture, IPC, UI, VLM and system action; it is synthetic, not live validation, and is a separate fixture from the earlier two-frame comparison. A current-host arm64 signed self-contained release bundle is now verified: its 96 Homebrew dylibs are bundled (76.5 MiB native closure; 93 MiB app), the executable has no absolute Homebrew load paths, and the exact app reached GUI READY with both DYLD override variables unset. Keep the feature off by default until clean-machine runtime, live capture/cost, and end-to-end task-success gates pass.

OpenCV work is phased by [the OpenCV plan reconciliation](OPENCV_PLAN_RECONCILIATION.md): bounded OpenCV change/ROI and grounded LK tracking are the current hackathon MVP; global-motion classification, per-frame template matching, and reacquisition are deferred pending evidence. Reuse the existing APC and verifier rather than adding parallel controllers or vision-only action success.

The first deterministic V7 fixed-30-fps versus APC-adaptive replay is implemented in `screen_capture.rs` and recorded as [V7-REPLAY-01](../benchmark_results/perception/v7_fixed30_adaptive_replay_20261001.md). In five runs per build, the OpenCV path processed 181 fixed-rate vs 34 adaptive frames, detected the transient ROI with 0 ms observed delay, and reacquired the persistent ROI within 500 ms; the feature-off comparison processed 181 vs 33 and reacquired within 366.7 ms. This synthetic fixture establishes component-detection/frame-work behavior only. Task success, action verification, end-to-end and OS CPU/capture cost, callback FPS, dropped frames, and real Vision-call savings remain open; V7 stays PARTIAL.

Follow-up replay [V7-REPLAY-02](../benchmark_results/perception/v7_action_effect_replay_20261002.md) adds a post-click control-state transition detected by an exact-pixel fixture oracle independent of the change/ROI detector; only that observation sends APC `expected_effect`. Five runs per build processed 181 fixed vs 35 adaptive frames in both the OpenCV and feature-off variants. Both variants observed the fixture effect at 16.3 ms, with persistent-ROI delay 166.7 ms for OpenCV and 33.3 ms feature-off. Median summed change-pipeline time was 0.837/0.159 ms fixed/adaptive with OpenCV and 64.651/12.408 ms feature-off; the latter is the existing Rust `image` sentinel, not OpenCV. This adds synthetic effect-loop evidence only; the action and oracle are fixtures, not the production executor/verifier, and there are still no live capture, achieved-FPS, OS-drop, or system cost results.

The synthetic 320×200 textured-box fixture moved from expected normalized box `[0.25,0.30,0.50,0.50]` to `[0.3125,0.35,0.5625,0.55]`; the returned box was `[0.31432608,0.3539923,0.5530946,0.54128504]` at confidence 0.914. The 20-sample test now requires confidence ≥0.5, per-coordinate normalized error <0.02, and IoU ≥0.85. Historical Apple Vision feature-off sessions measured p50/p95 2.09/4.59 ms (full suite) and 1.97/2.08 ms (focused); these predate the separate feature comparison above. These are fixture- and session-specific API timing observations, not general performance claims; they exclude capture, JPEG/UI, and model work. The earlier full native 48/48, frontend 70/70, and ScreenCapturePanel start/stop-refresh test 1/1 were pre-handoff checkpoint results; that UI test did not cover tracking. The latest handoff has separate ScreenCapture coverage 1/1, full frontend 14 files / 75 tests, TypeScript typecheck, and production build (727.01 kB JS / 219.38 kB gzip with the existing chunk warning). Focused backend runtime-observability + GUI-feature tests pass 50 tests and 4 subtests; focused native screen-capture tests pass 16/16. The latest controlled full `cargo test --bin olcr-desktop` run passed 59/59 with required Xcode Swift 5.5 runtime fallback and controlled build environment. Historical Apple Vision feature-off timing sessions measured 2.09/4.59 ms (full-suite), 1.97/2.08 ms (focused), and earlier 2.13/4.70 ms, 2.04/4.20 ms, and 2.01/2.12 ms. A single earlier cold call was 451.54 ms, not a percentile. Keep these distinct from the OpenCV-versus-Apple-Vision focused feature runs above; they are API-only measurements, not a controlled package or live-screen benchmark.

`ScreenCaptureManager` now retains at most one pinned grounding seed plus one latest RGB frame in bounded Arc-backed memory; JPEG encoding happens only when requested, and Stop clears the frames and pin. `pin_latest_grounding_frame` returns the exact frame later sent for perception. `track_latest_grounded_screen_region` requires that seed's sequence and builds a pair from the pinned frame plus a newer frame, rejecting seed/session/target/display/dimension/time mismatches and gaps over 2.5 seconds. Each capture notice/latest frame/tracking result carries the selected display ID, logical origin and size. The tracker runs Vision on a blocking worker and returns its own tracking observation, both frame IDs/timestamps, frame dimensions, and display geometry. A tested pure transform maps a normalized box center through Retina/downscaled image aspect to global logical display points and fails closed on malformed geometry or aspect mismatch. The explicit data-URL frame-pair command remains available for fixtures/callers. The panel states that frames remain in memory for grounded tracking and no frame files are written.

At the earlier mailbox-only checkpoint, no AX/UI-Venus caller supplied geometry to the tracking command. The current ScreenCapturePanel now sends the qualified UI-Venus candidate bbox and exact pinned seed sequence to that tracker. It keeps the model candidate bbox/confidence distinct from the tracker bbox/confidence and rejects mismatched provenance as `INCONCLUSIVE`. This is still a passive perception/tracking path: a tracked box is not an action plan, permission to click, expected-effect observation, or `CONFIRMED` postcondition. The pure display transform is covered by synthetic Retina/negative-origin fixtures only; it does not establish the live relationship among ScreenCaptureKit, Accessibility, Quartz mouse-event coordinates, or representative multi-display arrangements. Actual capture permission/live screen validation, pointer injection/calibration, frame-gap scenarios, multi-frame drift/failure, scroll/reacquisition behavior, capture/API cost attribution, and package/build impact remain open. The OpenCV tracker uses the grounded seed and existing frame provenance, but long-horizon drift, occlusion, scrolling and feature reacquisition remain open. Continuous tracking is opt-in with `opencv-perception`; the feature-off Apple Vision fallback does not arm this continuous mode. No pointer event, action plan, or postcondition is produced by tracking. Build-time OpenCV integration is verified; release packaging is not. The earlier measurement-gated OpenCV decision is superseded by the hackathon priority, while the feature-off fallback remains the default until bundling is resolved.

The repository already downsamples frames to a 64×36 luma grid and suppresses unchanged-frame image transfers. Preserve that path and use a strict cap on per-frame regions so small UI changes do not expand into a screen-sized ROI when they occur far apart.

Required MVP:

```text
ROI change detection
connected components where useful
known-element tracking
global motion detection
scroll/movement detection
template matching
feature-based reacquisition where justified
scene-change detection
action-effect observation
```

Optimize for GUI behavior:

```text
static controls
small movements
scroll
window movement
modal appearance
button-state changes
layout transitions
```

not general video understanding.

---

# V7 — Adaptive Perception Controller

Mandatory.

Treat these rates as policy targets, separate from configured and observed capture rates. The app supports macOS 13+ and enables the pinned `screencapturekit` 11 `macos_14_0` binding feature while retaining a runtime OS check. The existing stream now receives APC target revisions through `update_configuration` on macOS 14+, clamped to the supported 2–30 fps range; on macOS 13 the stream stays at its 2 fps ceiling. Status and frame notices report configured FPS separately from an observed-FPS estimate over callback arrivals in the recent 5-second window (bounded to 120 timestamps). This is an implemented code path, not live rate or cost validation, and no cost-based gate is implemented yet. Keep targets distinct from achieved rate; do not claim a transition until observed metrics confirm it. APC compute levels now schedule the OpenCV L0/L1 change/ROI work on both supported OS versions; higher-cost LK remains tied to an explicit grounded track, while OCR/UI-Venus stay request-driven. Do not restart capture for routine policy transitions. Keep macOS 13 support unless a separate compatibility decision changes the deployment target. See [the OpenCV plan reconciliation](OPENCV_PLAN_RECONCILIATION.md).

States:

```text
IDLE
ACTIVE
HIGH
BURST
```

Initial policy target rates (only achievable when supported by the platform capture API):

The current implementation uses internal `PerceptionRates` defaults; these are injectable through Rust construction but are not persisted or user-configurable settings, so the original request for configurable starting ranges is only partially met. Keep policy targets separate from configured and observed capture rates. Expose a user/runtime configuration only after matched supported-device measurements establish safe bounds; tune the typed defaults only through matched measurements in the meantime.

```text
IDLE
1–2 Hz

ACTIVE
5–10 Hz

HIGH
15–30 Hz

BURST
~30 Hz for bounded short duration
```

These are starting parameters, not hard-coded theoretical constants.

---

## Event-driven transitions

Examples:

```text
AX change
→ ACTIVE

OLCR scroll
→ HIGH

significant optical/pixel motion
→ HIGH

OLCR click
→ BURST

expected action effect observed
→ ACTIVE / IDLE

stable scene
→ IDLE
```

Prevent irrelevant animation from keeping HIGH active.

Keep capture callbacks observable with compact session aggregates: callbacks
received, image-pipeline frames processed, callbacks skipped before a valid
change result, and cumulative controller time in each policy state. Do not
persist one record per frame. Label callback processing skips separately from
ScreenCaptureKit/OS dropped frames; the latter require platform drop evidence.
Persist APC state totals only through the existing advisory state-update path,
and do not interpret them as CPU, capture, or end-to-end cost.

For dynamic controls, ground a target once from the pinned frame using the
existing AX/UI-Venus route, then use the OpenCV seeded tracker on subsequent
in-memory frames while confidence and provenance remain valid. This can avoid
repeated full-frame JPEG encoding/IPC and semantic re-grounding; it cannot
avoid acquiring new frames while tracking movement. Stop on loss or stale
identity/geometry and use the existing independent verifier for effects.

The current APC bounds repeated visual-only HIGH signals to 5 seconds from first escalation, then holds those signals at ACTIVE for a 5-second cooldown; three stable frames clear the guard. Explicit action and scroll signals retain their own immediate HIGH/BURST behavior. This is initial policy code covered by a synthetic repeated-change regression, not yet calibrated against recorded/live GUI animation. Add animation replay and task-responsiveness evidence before tuning the bounds.

---

# V8 — Adaptive Compute Depth

Use:

```text
L0
event/change sentinel

L1
ROI / connected-component analysis

L2
tracking / motion / template matching

L3
OCR / semantic ROI preparation

L4
UI-Venus GUI Vision
```

Most observations should terminate before L4.

---

# V9 — OCR Recovery Layer

Add OCR only as a bounded intermediate perception mechanism.

Target use case:

```text
Accessibility unavailable
+
ROI appears text-centric
```

Flow:

```text
OpenCV ROI
↓
OCR
↓
text + bbox
↓
World State
```

Example:

```text
Canvas-rendered "Export CSV"
```

should not automatically require a VLM.

## OCR implementation rule

Before introducing a new OCR dependency:

1. check existing OLCR dependencies;
2. check platform-native options;
3. benchmark an appropriate mature OCR engine;
4. compare latency, memory and accuracy against the existing UI-Venus GUI Vision path.

Do not automatically adopt Tesseract merely because it is mature.

Do not build a custom OCR engine.

---

# V10 — Semantic Escalation

Existing UI-Venus GUI Vision becomes semantic fallback.

Escalation reasons may include:

```text
UNKNOWN_REGION
TRACKING_LOST
NEW_LAYOUT
AMBIGUOUS_TARGET
ACCESSIBILITY_UNAVAILABLE
ACCESSIBILITY_CONFLICT
OCR_INSUFFICIENT
VERIFICATION_FAILED
SEMANTIC_REVALIDATION
```

Every call must retain escalation reason.

---

# V11 — ROI-first Vision

Prefer:

```text
uncertain ROI
→ crop
→ existing Vision runtime
```

over full-screen inference.

Record:

```text
full_screen
roi
multi_roi
```

and pixel count.

---

# V12 — Semantic Cache

Persist semantic identity while evidence remains valid.

Example:

```text
Vision:
"This is Save"

↓

ElementRef:
semantic_label = Save
semantic_source = vision
location_source = opencv
```

Do not call Vision again just because the object moved slightly.

Invalidate on meaningful uncertainty.

---

# TRACK A — DETERMINISTIC ACTION ARCHITECTURE

# A1 — Action Router

Add a routing layer before low-level GUI execution.

Goal:

> Use the cheapest reliable action mechanism rather than defaulting to pointer simulation.

Conceptual hierarchy:

```text
Native / structured action
↓
Playwright action
↓
Accessibility action
↓
Verified cached routine
↓
Keyboard shortcut
↓
Deterministic mouse/keyboard
↓
Vision-grounded pointer action
```

Selection must consider:

```text
availability
reliability
task semantics
current state
verification capability
security
```

Do not blindly choose the cheapest mechanism if it changes the semantics of the requested task.

---

# A2 — Accessibility Actions

Upgrade Accessibility from read-only perception source to action source where supported.

Examples conceptually include:

```text
press
increment
decrement
set/focus/select where supported and appropriate
```

Prefer:

```text
AX element
→ supported AX action
```

over:

```text
AX bbox
→ calculate center
→ move mouse
→ click
```

when the structured action faithfully performs the intended interaction.

Preserve fallback to pointer execution.

---

## Element Affordances

Extend existing ElementRef/equivalent with executable capabilities where appropriate.

Conceptual representation:

```text
ElementRef

identity
provenance
geometry
state

affordances:
    playwright_action
    accessibility_actions[]
    keyboard_shortcuts[]
    pointer_action
    vision_grounded_action
```

Do not require every source to support every affordance.

---

# A3 — Shortcut Registry

Use keyboard shortcuts as first-class deterministic actions.

Before creating a large custom shortcut database, inspect:

- existing OLCR shortcut handling
- application menu Accessibility metadata
- platform conventions
- discoverable app actions

Represent shortcuts with provenance.

Example:

```text
action: save

shortcut:
    keys: Cmd+S
    source: application_menu / known_system / verified_cache
```

Do not assume a shortcut exists purely from convention when the consequence could be destructive.

---

## Structured Tool Substitution

When the user's requested outcome is semantically equivalent and OLCR policy allows it, structured native tools may replace GUI choreography.

Examples:

```text
file operation
system preference query
application command
```

However:

> Do not silently replace a user-requested GUI demonstration with a hidden CLI action when the interaction method itself matters.

Separate:

```text
goal semantics
```

from:

```text
interaction semantics
```

Action Router must respect both.

---

# A4 — Deterministic Pointer

If a structured or AX action is unavailable but geometry is known, use an explicit, narrow native executor:

```text
known ElementRef
+
reliable bbox
↓
identity-bound macOS pointer executor
```

No Vision inference is required for this route. The executor must bind geometry to a fresh AX observation and target PID/bundle ID, re-read the target role/label/enabled state/box immediately before input, and use the platform's global coordinate contract. Require explicit post-event permission from a user-operated Settings control, ensure the selected app is frontmost, reject points outside active display bounds, and verify the requested state transition from a new observation. Keep AXPress ahead of pointer input.

Current source of truth: AXPress remains preferred. A fresh visible enabled `AXButton` without AXPress may use the UI-Venus/OpenCV-or-Apple-Vision tracked point when AX geometry is unusable; a closed disclosure retains its bounded geometry fallback. Both paths bind the action to one unique AX element in the selected app, capture session and target window, revalidate live role/label/state and display/window bounds before dispatch, require pointer-event permission, and return a fresh same-app observation for independent verification. The button route completed one live fixture journey in `20261003-220000-scrollfix` (tracked click and fresh AX result). Representative display-scale calibration, additional apps, and non-AX canvas targets have not passed acceptance. Treat the verified narrow paths as implemented; do not interpret them as a general mouse API.

Vision-grounded pointer execution is the last visual fallback.

---

# A5 — Vision-grounded Pointer Fallback

For an action plan, use UI-Venus grounding only after structured actions, AX actions, verified routines, shortcuts, and deterministic geometry have been considered and are unavailable or semantically unsuitable. “Ground target” remains a passive perception/tracking affordance. The explicit “Ground and open with tracked pointer” action is a separate narrow production path for a closed AX disclosure when its AX frame and AXPress are unavailable; it does not authorize arbitrary targets. Bind any candidate to the exact captured-frame sequence, selected display geometry, target PID/bundle ID, and a fresh task observation. Require the candidate label to match exactly one AX element across all roles and that element's native ID to be the selected disclosure. Require confidence and geometry validation, then perform an independent post-action observation; malformed, ambiguous, or stale grounding must fail closed.

Vision is a fallback policy, not permission to click arbitrary screen content. Do not use attached screenshots or a frame without a verified pixel-to-global-coordinate transform for pointer input. The passive grounding bridge binds a UI-Venus result to a fresh, pinned ScreenCaptureKit frame, selected app PID/bundle, capture session and display metadata, then can hand the candidate bbox to the existing feature-selected native frame-pair tracker (OpenCV Lucas–Kanade when `opencv-perception` is enabled; Apple Vision in the feature-off build). The model bbox and later tracker bbox remain separate observations; tracker output is not an action or verified effect. Current production pointer execution is limited to (1) a unique, fresh, visible enabled AXButton without AXPress or usable AX geometry, and (2) the separately bounded closed-disclosure fallback. The button route must bind the candidate label to exactly one AXButton and the fresh capture/session/window provenance; the native layer rechecks the live AX identity/state, pointer permission, frontmost app, selected window and display bounds before one click. A new same-app AX observation and backend postcondition verifier are mandatory. Malformed, ambiguous or stale grounding fails closed. The `20261003-220000-scrollfix` fixture run supplies one live tracked-button journey; representative coordinate calibration and non-AX canvas interaction remain open.

The shared `ActionRouter` rejects `vision_pointer` candidates unless the production caller supplies all four evidence flags: `grounding_provenance_bound`, `coordinate_transform_verified`, `target_identity_bound`, and `independent_postcondition`. In OLCR, the validated AXButton and closed-disclosure paths can set them only after checking unique AX identity plus grounding provenance and target-bound tracker alignment. Missing evidence remains rejected even when generic availability, reliability, security, freshness, and verification fields are set. A viable structured, AX, routine, shortcut, or deterministic route takes precedence over `vision_pointer`, regardless of caller-supplied cost estimates. These flags derive from validated evidence, not user-configurable bypasses.

Repository reuse decision: do not add another vision model, provider, OCR implementation, image decoder, or coordinate-transform layer. Reuse `ModelProvider.vision` with `role:gui_grounding` (`ui_venus_mlx_6bit`), the existing UI-Venus box normalization and OCR-seeded ROI validation/remapping, target-bound ScreenCaptureManager frame/session/display metadata, the feature-selected native frame-pair tracker (OpenCV Lucas–Kanade when enabled; Apple Vision otherwise), and `map_normalized_bbox_to_global_point`. `Runtime.execute_image` remains the attached-image perception path, while `Runtime.ground_gui_target` is the selected-app capture bridge; neither its candidate nor the tracker result alone is a permissioned action plan or verified effect. Target-bound capture requires one selected-app window and one containing display, and grounding returns explicit confidence-qualified geometry with provenance. The current production pointer paths support a unique selected-app AXButton fallback and a bounded closed-disclosure fallback. The button path has one live fixture click and independent AX postcondition; the disclosure route still has mocked/unit evidence. Neither synthetic tests nor one fixture journey establishes representative AX/Quartz/ScreenCaptureKit calibration. Keep A5 partial for that remaining acceptance, and do not claim arbitrary canvas or general coordinate-driven interaction.

The explicit read-only alignment preflight remains available and does not request Accessibility or pointer-event permission, activate the target, or post an event. The live button journey demonstrates one end-to-end coordinate mapping and effect verification on one fixture/window. It does not measure calibration across display scaling, multiple displays, window movement, or other applications; A5 remains partial until representative coordinate and recovery acceptance is recorded.

---

# A6 — Verification Across Routes

Every route must return `CONFIRMED`, `NOT_CONFIRMED`, or `INCONCLUSIVE` from fresh, same-session evidence. AX verification additionally requires Accessibility permission to remain granted and the same valid application PID and nonempty bundle ID before and after the action; a matching session ID alone does not establish application identity. A successful event post is not action success. A UI-Venus candidate or feature-selected native tracker box (OpenCV Lucas–Kanade when `opencv-perception` is enabled; Apple Vision otherwise) is perception evidence only and cannot by itself confirm an expected effect; confirmation still requires a separate fresh post-action observation. Preserve the attempted route, its source provenance, expected effect, observed effect, and any fallback reason in the existing task/telemetry path.

Repository correction: OLCR has a narrowly guarded Core Graphics click executor for current AX-bound actions, including the live-tested AXButton fallback described above. It is not a general ElementRef or arbitrary vision-grounded mouse executor: AX-free canvas targets, arbitrary screen coordinates, drag, wheel scrolling, and generic keyboard entry are not supported by native chat. The live fixture click establishes one coordinate/action/verifier journey only; representative scale/display calibration and target movement recovery remain unverified. Keep AXPress/structured actions first and reject unknown origin, scale, or geometry.

---

# TRACK R — VERIFIED ROUTINE COMPILATION

# R1 — Principle

When Laya successfully solves a repeated deterministic interaction, OLCR may convert the verified execution trace into a reusable routine.

Goal:

```text
semantic reasoning once
→ deterministic execution many times
```

NOT:

```text
semantic reasoning every time
```

---

# R2 — Successful Execution Trace

Capture enough structured information from successful interactions:

```text
goal
application/context

preconditions

observed state

actions[]

expected effects[]

verification evidence

final state

provenance
```

Do not store unnecessary screenshots.

---

# R3 — Routine Candidate

After a successful verified trace, generate a candidate such as:

```text
routine:
    save_document

context:
    application: X

preconditions:
    document_open
    unsaved

actions:
    shortcut: Cmd+S

expected:
    unsaved == false
```

A single successful trace does not automatically imply universal validity.

---

# R4 — Routine Compiler

Compile eligible traces into a constrained deterministic representation.

Do NOT compile arbitrary free-form LLM reasoning into unrestricted executable code.

Routine language should be declarative/bounded.

Conceptually:

```text
PRECONDITIONS

ACTION

EXPECTED EFFECT

ON SUCCESS

ON FAILURE
```

Use existing workflow/state-machine abstractions if available.

Do not invent a general programming language.

Repository reuse decision for GUI routines: do not register native GUI actions as directly executable backend `ProcedureRunner` tools. That would bypass the frontend's explicit execution choice, OS permission checks, fresh target identity checks, and independent post-action verification. The MVP supports approved, bounded one-action GUI routines by matching them before semantic planning, returning the normal `ActionRouter` plan, dispatching only after the user's explicit Execute choice through the existing native executor, and recording the verified outcome through `Runtime.procedures`. Do not add a second GUI executor or claim that plan creation itself is execution. Multi-action GUI routines remain deferred until the existing native action contract can safely execute and verify every step.

---

# R5 — Routine Eligibility

Only compile when:

```text
actions are deterministic
preconditions are identifiable
expected effect is verifiable
scope is bounded
no unresolved semantic ambiguity remains
```

Do NOT automatically cache sensitive/destructive workflows such as:

```text
payment
account deletion
credential changes
irreversible destructive actions
```

unless existing OLCR policy explicitly supports safe confirmation.

---

# R6 — Routine Cache

Store:

```text
routine_id
version

goal_signature

environment/application signature

preconditions

actions

expected effects

success/failure stats

last_verified
```

Integrate with existing persistence/cache architecture.

Do not create a standalone database if unnecessary.

---

# R7 — Routine Matching

Before full semantic planning, check whether:

```text
goal
+
World State
```

matches a verified routine.

Flow:

```text
Goal
↓
World State
↓
Routine candidate?
    │
    ├─ NO → normal Laya
    │
    └─ YES
         ↓
     preconditions valid?
         │
         ├─ NO → Laya
         │
         └─ YES
              ↓
          execute routine
              ↓
           verify
```

Before routing an approved candidate, bind the request observation back to the saved World State: source, observation/session IDs, coordinate space, and complete bounded element list must match. For AX, also require permission to remain granted and the same valid application PID and nonempty bundle ID as the saved active window. A session or observation ID alone does not establish target identity; any mismatch falls back to normal planning.

---

# R8 — Failure

Routine failure:

```text
routine
↓
NOT_CONFIRMED
↓
stop deterministic continuation
↓
reacquire state
↓
Laya / normal recovery
```

Do not blindly continue through a stale macro.

---

# R9 — Routine Revalidation

Track success/failure.

If environment changes or repeated failures occur:

```text
ACTIVE
→ SUSPECT
→ DISABLED / RECOMPILE
```

Do not silently mutate important routines without traceability.

---

# R10 — No premature generalization

MVP only needs to demonstrate a few bounded reusable routines.

Do not attempt:

```text
general lifelong learning
general GUI skill induction
arbitrary program synthesis
```

in this implementation.

---

# TRACK W — WEB READ / SCRAPE STUDIO

# W1 — Web Read

Implement/reuse:

```text
URL
↓
existing safe Web fetch / optional evaluated Scrapling adapter
↓
HTML
↓
main-content extraction
↓
clean Markdown
↓
context budget
↓
existing OLCR semantic model
```

Output:

```text
url
title
clean_markdown
metadata
timestamp
```

---

# W2 — Browser Session Manager

Use Playwright Chromium.

Support:

```text
open
close
navigate
reload
back
persistent authorized context when required
```

Do not build a browser renderer.

---

# W3 — Login Sessions

Allow manual user login through controlled browser context.

Recipe may reference browser profile/session identifier.

Never store raw password/cookies inside Recipe.

---

# W4 — Element Picker

Support selecting:

```text
Row
Field
Navigation
```

Collect useful browser evidence:

```text
DOM path
selector candidates
role
text
attributes
bbox
```

---

# W5 — Repeating Row Detection

Generalize a selected repeated record.

Example:

```text
article.product:nth-child(1)
→
article.product
```

Prefer stable structural selectors over absolute XPath.

---

# W6 — Field Selection

Support:

```text
text
inner_html
href
src
attribute
number
date
boolean
list
```

where appropriate.

---

# W7 — Recipe

Persist deterministic scraping recipe.

Conceptual structure:

```yaml
version: 1

start:
  url: ...

record:
  selector: ...

fields:
  title:
    selector: ...
    extract: text

navigation:
  type: click_next
  selector: ...

wait:
  strategy: dom_stable

deduplicate:
  keys:
    - url

stop:
  max_pages: ...

output:
  format: csv
```

Reuse existing schema infrastructure where possible.

---

# W8 — Navigation

Support:

```text
URL pagination
Next
Load More
Infinite Scroll
Numbered Pagination
```

---

# W9 — Detail Pages

Support:

```text
list row
↓
detail page
↓
extra fields
↓
next record
```

without per-record semantic inference after recipe establishment.

---

# W10 — Deterministic Execution

Core:

```text
LLM
↓
Recipe
↓
deterministic execution × N pages
```

not:

```text
LLM × N pages
```

---

# W11 — Natural Language → Recipe

Example:

```text
このサイトから
名前、所属、選挙区、URLを全部CSVにして
```

Flow:

```text
request
↓
DOM
↓
existing semantic model
↓
candidate Recipe
↓
Preview
↓
deterministic run
```

---

# W12 — Network Observer

Observe normal browser:

```text
XHR
fetch
JSON
```

responses.

If equivalent structured data is already received by the user's authorized browser session, prefer it where appropriate.

Do not bypass access control.

---

# W13 — Scraper Recovery Ladder

```text
selector failed
↓
alternative selector
↓
DOM reacquisition
↓
browser Accessibility
↓
network evidence
↓
OpenCV
↓
OCR where text-specific
↓
UI-Venus GUI Vision
↓
user intervention
```

This should reuse the same deterministic-before-semantic principle as Computer Use.

When a runtime Recipe failure has no usable selector alternative, expose an
explicit user-reviewed browser-accessibility recovery step through the
existing managed Playwright `browser_snapshot` provider. Keep the bounded
snapshot page-local and untrusted, leave the saved Recipe unchanged, and
provide a manual recovery editor backed by the existing Recipe GET/PUT paths
for row, list-field, and detail-field selectors. Saving uses the existing
Recipe validation/persistence contract and returns the Recipe to DRAFT; require
the usual Preview & Validate gate before Resume. This is a review affordance,
not automatic AX-to-selector reconstruction. Keep OCR/Vision fallback stages
separate until their provenance and Recipe-mapping contracts are implemented.

Reuse the pinned Playwright MCP `browser_network_requests` and
`browser_network_request` tools for network evidence. OLCR may add an explicit,
bounded, same-origin JSON-response review affordance with redaction, but must
not add a second browser/network interception stack. Treat response JSON as
untrusted. A JSON Recipe may persist only the same-origin endpoint path and
user-authored JSON Pointer mappings; never store the response body. Runtime
must reselect exactly one observed successful GET by path in the managed
session, enforce MIME and byte/complexity limits, redact sensitive-key values,
and feed bounded mapped rows into the existing Preview & Validate, checkpoint,
dedupe and export contracts. Do not infer mappings or selectors. DOM remains
the default Recipe source; the JSON source is explicit and opt-in.

---

# W14 — Validation / Dedup / Stop

Support:

```text
required/optional fields
types
validation warnings

canonical URL / ID / selected hash dedupe

max_pages
max_records
max_duration
no_new_records
repeated_page
same_url_loop
manual_stop
```

No unbounded crawling.

---

# W15 — Streaming Export

Pipeline:

```text
record
↓
validation
↓
normalization
↓
dedupe
↓
streaming writer
```

Support:

```text
CSV
JSON
JSONL
```

Do not retain massive result sets entirely in RAM.

---

# W16 — Scrape Studio UI

Integrate into existing OLCR frontend.

MVP:

```text
Browser
Preview
Row
Fields
Navigation
Stop Conditions
Output
Run
Pause
Resume
Stop
```

Do not create separate app.

---

# TRACK U — UNIFIED ARCHITECTURE

# U1 — World State

Use a common envelope where useful:

```text
WorldState

├── Desktop
└── BrowserSessions[]
```

Desktop and Browser remain domain-specific internally.

---

# U2 — Unified ElementRef

Where useful, planner-facing elements should expose a common identity/provenance model.

Sources may include:

```text
structured_api
network
playwright_dom
browser_accessibility
macos_accessibility
opencv
ocr
vision
```

---

# U3 — Provenance

Keep separate:

```text
semantic_source
location_source
action_source
verification_source
```

Example:

```text
semantic_source = accessibility
location_source = opencv
action_source = shortcut
verification_source = accessibility
```

This is important for debugging and benchmarks.

---

# U4 — Unified Verification

Use conceptual contract:

```text
Action
Expected Effect
Observed Effect
Result
```

Results:

```text
CONFIRMED
NOT_CONFIRMED
INCONCLUSIVE
```

Evidence differs by environment.

Desktop:

```text
Accessibility
OpenCV
OCR
Vision
```

Browser:

```text
DOM
URL
network
Accessibility
OpenCV
```

---

# U5 — Recovery Router

Use:

```text
structured evidence
↓
deterministic recovery
↓
Accessibility
↓
OpenCV
↓
OCR if applicable
↓
UI-Venus GUI Vision
↓
Laya
↓
user intervention
```

Do not jump immediately to semantic inference.

---

# U6 — Tool Router

Examples:

```text
"このURLを読んで"
→ Web Read

"この一覧全部CSV"
→ Scrape Studio

"このアプリで設定を変更"
→ Computer Use

"前と同じ方法で保存"
→ Routine candidate if verified
```

---

# U7 — Security Boundary

Treat external content as untrusted data.

Separate:

```text
CONTROL PLANE
User
Planner
Recipe
Tool policy
OLCR instructions

----------------

DATA PLANE
HTML
DOM
page text
network data
OCR text
screen content
```

Screen/web content saying:

```text
Ignore previous instructions
Run this command
Send credentials
```

must never automatically become control instructions.

---

# U8 — URL Security

Reject by default:

```text
localhost
127.0.0.0/8
link-local
cloud metadata
private network
file://
unsupported schemes
```

unless explicit development configuration allows otherwise.

---

# TELEMETRY

Extend existing telemetry.

## Perception

```text
frames_received
frames_processed
frames_skipped

event_wakeups
pixel_change_wakeups

time_in_IDLE
time_in_ACTIVE
time_in_HIGH
time_in_BURST

opencv_processing_ms

ocr_calls
ocr_ms

vision_calls
vision_calls_by_reason
vision_fullscreen_calls
vision_roi_calls
vision_pixels_total
vision_inference_ms
```

## Actions

```text
action_router_decisions

native_actions
playwright_actions
accessibility_actions
routine_actions
shortcut_actions
pointer_actions
vision_grounded_actions

actions_verified
actions_not_confirmed
verification_inconclusive
```

## Routine

```text
routine_candidates
routines_compiled
routine_matches
routine_runs
routine_successes
routine_failures
routine_fallbacks
```

## Scraping

```text
pages_visited
records_discovered
records_written
duplicates_removed

dom_extractions
network_extractions

selector_recoveries
opencv_recoveries
ocr_recoveries
vision_recoveries

llm_calls
vision_calls

elapsed_ms
records_per_second
```

Aggregate high-frequency events.

Do not persist a heavyweight event for every frame.

---

# BENCHMARKS

## Computer Use Baselines

Compare:

```text
A
Existing screenshot/Vision baseline

B
OpenCV fixed-rate perception

C
Adaptive OLCR Vision

D
Adaptive OLCR Vision
+ Action Router

E
Full architecture
+ Routine Cache
```

Use same tasks.

---

## Metrics

```text
Task Success Rate
Task Time

Actions / Task
Failed Actions

Vision Calls
Full-screen Vision Calls
ROI Vision Calls
Vision Pixels
Vision Inference Time

OCR Calls
OpenCV Processing Time
Frames Processed

AX Actions
Shortcut Actions
Pointer Actions

Verification Success
Recovery Success
```

---

# Mandatory APC Ablation

Compare:

```text
fixed 30fps
```

against:

```text
event-driven adaptive perception
```

Measure:

```text
task success
latency
frames processed
OpenCV processing
CPU time where reliable
Vision calls
```

Prove Continuous Vision is not merely shifting VLM cost to CPU.

---

# Action Router Ablation

Compare equivalent tasks:

```text
pointer-first
```

against:

```text
Action Router
```

Measure:

```text
steps
latency
failure rate
Vision calls
verification failures
```

---

# Routine Benchmark

Run repeated tasks.

Example structure:

```text
Run 1
normal semantic planning

Run 2+
routine candidate available
```

Measure:

```text
LLM calls
Vision calls
actions
latency
success
fallback frequency
```

Goal:

> repeated verified tasks should increasingly execute deterministically without sacrificing correctness.

Do not claim "learning" unless benchmark evidence supports the exact claim.

---

# Scraper Benchmark

Measure:

```text
Record Accuracy
Record Completeness
Duplicate Rate

Navigation Success
Recovery Success

Records/sec

LLM calls/run
Vision calls/run

memory / 1000 records
```

Semantic calls should not scale linearly with record count after Recipe establishment.

---

# TESTING

## Vision unit tests

```text
identical frames
localized change
large change
event wakeup
tracking
tracking loss
scene reset
APC transitions
dynamic-content suppression
OCR escalation
Vision escalation
```

## Action tests

```text
AX action available
AX unavailable
shortcut available
shortcut invalid
known pointer target
Vision fallback
verification failure
```

## Routine tests

```text
candidate generation
precondition match
precondition mismatch
successful run
verification failure
fallback
stale routine
disable/revalidation
```

## Scraper tests

```text
row generalization
fields
pagination
load more
infinite scroll
detail page
dedupe
validation
stop conditions
selector recovery
network JSON
streaming export
```

Use controlled fixtures where possible.

---

# IMPLEMENTATION ORDER

Follow unless repo architecture provides a concrete reason to alter.

```text
P0
Repo audit + baseline

P1
minimal unified provenance / state extensions

P2
Web Read

P3
ScreenCaptureKit

P4
Multi-Signal Change Sentinel

P5
Desktop persistent state

P6
Accessibility perception

P7
Accessibility notifications

P8
OpenCV temporal state

P9
Adaptive Perception Controller

P10
OCR evaluation + bounded integration if justified

P11
UI-Venus semantic escalation

P12
Action Router

P13
Accessibility Actions

P14
Shortcut integration

P15
Action Verification

P16
Routine trace capture

P17
Routine Compiler + Cache MVP

P18
Browser Session Manager

P19
Scrape Studio element picker

P20
Recipe model

P21
deterministic recipe execution

P22
navigation

P23
streaming export

P24
detail pages

P25
network observer

P26
scraper recovery

P27
NL → Recipe

P28
full unified integration

P29
benchmarks / ablations

P30
documentation
```

---

# Acceptance Gates

Do not mark PASS unless tested.

## Vision

Gate crosswalk: the V labels follow the P3–P11 phase sequence above. P7 accessibility notifications are V5; P8 temporal visual state plus P9 APC are V6; P10 OCR recovery is V7; P11 UI-Venus semantic escalation and ROI-first escalation are V8/V9. OpenCV implementation is an explicit hackathon priority within V6 temporal/tracking work, using the additive `opencv-perception` feature and preserving existing feature-off fallbacks. OpenCV V6 evidence must identify which backend ran; release acceptance also requires a signed bundle that launches without a Homebrew OpenCV installation. Do not call the feature-off Rust `image` sentinel OpenCV.

```text
V1 continuous capture (P3)
V2 event/pixel change detection (P4)
V3 persistent desktop state (P5)
V4 AX semantics (P6)
V5 AX notifications (P7)
V6 temporal visual state + APC (P8–P9)
V7 OCR recovery (P10)
V8 UI-Venus semantic escalation (P11)
V9 ROI-first vision escalation (P11)
```

## Action

```text
A1 Action Router works
A2 at least one AX action works
A3 application-menu shortcut route works
A4 deterministic pointer fallback works
A5 Vision-grounded final pointer fallback works
A6 all routes are independently verifiable
```

## Routine

```text
R1 successful trace captured
R2 routine candidate generated
R3 preconditions checked
R4 deterministic routine executes
R5 success verified
R6 failed routine returns safely to normal OLCR
```

## Web

```text
W1 Web Read
W2 browser session
W3 row selection
W4 fields
W5 Preview
W6 Recipe
W7 pagination
W8 infinite/load-more
W9 export
W10 deterministic rerun
W11 bounded recovery
```

## Unified

```text
U1 provenance
U2 World State
U3 verification
U4 recovery
U5 telemetry
U6 regressions
```

---

# DoNotImplement

Do NOT implement:

```text
Windows Computer Use
Linux Computer Use

custom browser renderer
custom Chromium fork

custom OCR engine
custom optical flow
custom feature matcher

new LLM runtime
new VLM
model training/fine-tuning

general programming language for routines
general lifelong-learning system

CAPTCHA bypass
anti-bot evasion
auth bypass

X-specific scraper

distributed crawler

AWS runtime dependency
COOL integration

hackathon-only OLCR fork
```

These may be separate future tasks.

---

# Competition Boundary

OpenCV AI Competition submission should remain focused on:

# OLCR Vision

Specifically:

```text
Continuous Visual State
+
OpenCV 5 seeded visual-state tracking (hackathon build; behind the bounded feature selector)
+
Event-driven Adaptive Perception
+
OS Accessibility
+
Deterministic Action Routing
+
UI-Venus Semantic Fallback
+
Closed-loop Verification
```

Routine compilation may be shown as an additional consequence of persistent visual state.

Scrape Studio remains an OLCR capability but is not the competition's central project.

The competition claim should be established experimentally:

> How much expensive semantic visual inference can be replaced by persistent state, classical computer vision, mature OS interfaces, and deterministic actions while preserving Computer Use task success?

Do not claim equivalence to GPT-class Computer Use systems unless benchmarked directly under comparable tasks and conditions.

---

# Final Report Required

Return:

## A. Repo map

Actual paths for:

```text
Planner
Laya
Tool Router
Computer Use
Vision
UI-Venus GUI Vision model
Action Executor
Accessibility
Capture
Web
Browser
State
Telemetry
Tests
Benchmark
Frontend
```

## B. Existing components reused

Explicitly list what was reused rather than rebuilt.

## C. Changed files

```text
path
purpose
```

## D. Implementation status

For every stage:

```text
IMPLEMENTED
PARTIAL
BLOCKED
NOT STARTED
```

## E. Verification

Separate:

```text
BUILD
UNIT
INTEGRATION
LIVE
BENCHMARK
```

Each:

```text
PASS
FAIL
NOT RUN
```

## F. Architecture

Show actual resulting architecture using real repo component names.

## G. Benchmarks

Measured values only.

## H. Reinvention review

For every substantial custom component, state whether existing project/platform/library capability was reused first.

Highlight any remaining custom subsystem that appears replaceable by mature prior art.

## I. Remaining blockers

Concrete issues only.

## J. Competition readiness

State separately whether measured evidence currently supports:

```text
OpenCV continuous perception
adaptive-rate perception
event-driven perception
reduced processed frames
ROI-first semantic escalation
reduced VLM usage
structured/AX actions
action verification
task-success retention
compute reduction
```

Do not infer unmeasured claims.

---

# Final System Invariant

The implementation should converge toward:

```text
                 GOAL
                  │
                  ▼
            Persistent State
                  │
        ┌─────────┴─────────┐
        │                   │
   PERCEPTION            ACTION
        │                   │
 Structured data       Native action
        ↓                   ↓
 Accessibility          AX / Playwright
        ↓                   ↓
 Cached state           Routine
        ↓                   ↓
 OpenCV                  Shortcut
        ↓                   ↓
 OCR                 Deterministic input
        ↓                   ↓
 UI-Venus GUI Vision       Vision-grounded input
        │                   │
        └─────────┬─────────┘
                  ▼
                VERIFY
                  │
          ┌───────┴───────┐
          │               │
       SUCCESS          FAILURE
          │               │
   Routine Candidate    Recovery
                          │
                          ▼
                         Laya
```

The architecture must make **semantic intelligence an escalation mechanism rather than the default execution mechanism**.

The distinctive OLCR behavior should therefore be:

```text
unknown
→ reason
→ act
→ verify
→ remember structure

known
→ deterministic action
→ verify

changed
→ update only changed state

uncertain
→ escalate only the uncertain part

repeated and verified
→ compile into bounded deterministic routine
```

Implement this as part of OLCR itself, not as a separate demonstration system.


## 2026-10-03: Cursor feedback decision after live OpenCV click

The current explicit-label native journey is live-verified in isolated run
`20261003-202000-cx7seedbudget`: normal chat start, exact target-window preview,
hash-bound OCR grounding, OpenCV Lucas–Kanade alignment, native pointer dispatch,
and independent AX effect verification. This is one fixture acceptance result;
it does not close natural-goal multi-step or reconnect/recovery acceptance.

Adopt cursor feedback as a staged extension of the existing native executor,
not a second input runtime. A pointer state should carry capture session, target
PID/bundle/window identity, display geometry, frame sequence, current native
pointer position, tracked target point, and their delta/distance. Map both points
through the same target-window geometry used by capture and dispatch. Render a
virtual cursor and destination marker inside the chat preview from those values;
the overlay must stay out of capture/model input and disappear when its session,
window, geometry or frame evidence becomes invalid. A preview marker is feedback,
not proof that input reached the target.

For hover, drag, or continuous game actions, introduce a bounded move stage in
the same deterministic executor: ground/track → move → sample actual pointer and
fresh target → validate containment/position → dispatch → verify the requested
effect. Record user interference and target movement rather than assuming a
simulated cursor reached the target. Keep existing AX/DOM semantic actions as
the first route; use this extension only when pointer behavior is needed. Do not
add a separate browser/native executor, infer movement distance from rendered
preview pixels, or invoke VLM on every cursor update. The completed click path
now uses bounded sampled native mouse-move events, reports actual pointer
coordinates, and requires a fresh same-target AX control/window containment
check before the existing guarded click. Frontend and native unit tests pass;
isolated GUI acceptance of staged movement remains open. The preview overlay
stays outside capture/model input, and broader drag/continuous actions remain
future work.
