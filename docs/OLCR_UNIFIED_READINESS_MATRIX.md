# OLCR Unified Architecture Readiness Matrix

Updated: 2026-10-04 (Asia/Tokyo)

This is the consolidated index for the V3 implementation objective. The full evidence and unresolved conditions remain in the linked canonical row of [OLCR_UNIFIED_IMPLEMENTATION_STATUS.md](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md). This matrix does not upgrade a gate because code exists; fixture, live, build, and benchmark evidence stay distinguished in the canonical record.

**Scope:** 31 phase gates (P0–P30), 47 acceptance gates (V1–V9, A1–A6, R1–R10, W1–W16, U1–U6), and seven cross-cutting chat-first experience gates (CX1–CX7), for 85 indexed gates. The original 78 records are unchanged. CX1–CX5 are `PARTIAL`, CX6 is `PASS`, and CX7 is `OPEN`. P31 is a supplemental Keychain diagnosis item outside the numbered plan range and remains in the canonical ledger. On 2026-10-03, isolated GUI run `20261003-220000-scrollfix` verified one vision-grounded OpenCV-tracked AXButton click and independent AX postcondition. A5 remains `PARTIAL` for representative display scaling, multi-display/window-movement recovery, repeat live acceptance and the closed-disclosure route; the verified button route is now part of A5's current-source scope.

`PARTIAL` means at least part of the behavior has evidence, while one or more planned acceptance conditions remain open. The linked ledger row records what is implemented, evidence collected, and what remains.

## Chat-first product acceptance overlay

The user journey and current gap are recorded in the canonical status ledger's [chat-first overlay](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#chat-first-product-experience). These gates are binding completion criteria for the active implementation goal, in addition to component gates, and preserve existing policy, Recipe, Playwright, AX/OpenCV/UI-Venus and verification contracts. The product target is outcome-level chat instructions; requiring the user to supply every native action label and expected text is an implementation gap.

|Gate|Status|Acceptance target|
|---|---|---|
|CX1 Chat intent and target resolution|PARTIAL|Chat routes explicit URL scraping, scoped project E2E, and native start/stop/status separately. Explicit “real GUI E2E with Computer Use” now selects native startup; website/site/URL/Playwright E2E selects the project browser path. Frontend/backend intent tests and a chat API event test cover these examples. Native start resolves an explicit app name against Settings' bundle-ID allowlist and running app identities; an exact allowlisted bundle can be launched from chat when closed. The latest explicit-label fixture chat launch passed capture, AXPress and fresh postcondition. Run 20261003-105300-plan-reason-c321's sanitized diagnostic showed that the planner had grounded the model-selected button but rejected its omitted postcondition. The backend now fills that omitted field only from the exact result phrase already parsed from the user's request, then keeps the existing fresh-AX validator; focused tests pass, but the fallback awaits live GUI validation.|
|CX2 Chat-managed browser control|PARTIAL|The existing manager serves public scrape sessions and exact-origin local-E2E sessions, and the task card renders a session-bound viewport and accessibility snapshot in chat. Scrape Studio refreshes the selected browser screenshot every 2.5 seconds while its session is open, including pre-run clarification; session changes immediately clear the previous session image, and a failed screenshot refresh removes the stale frame. BrowserViewport/ScrapeStudio/ChatComposer tests pass 32/32 and typecheck passes. Local E2E supports up to six grounded click/type/same-origin navigation/wait/assert actions. Its structured planner grounds labels and links in fresh page evidence and input values in the user's request; it may infer a concise expected result from the stated goal only after a grounded action and only when absent from the pre-action accessibility snapshot. The existing Playwright path must then confirm it in a fresh accessibility snapshot. A managed-Playwright API regression covers an inferred “Started:” postcondition and rejects it when already visible before the click. In isolated GUI run `20261003-mur3rj4f-d8016972`, the selected model planned three actions from a natural-language request; the chat visibly verified `Started: Nagi - Continued`, and chat Stop closed the browser session and owned fixture process. The planner rejection path was also changed so an invalid/non-action plan cannot fall through to stale normal-chat output. Model inference quality, direct screenshot-viewport interaction, public-page continuation, viewport reconnect and task recovery remain open.|
|CX3 Chat-triggered scraping|PARTIAL|Chat proposes a bounded visible-DOM Recipe, validates through Preview & Validate, and starts a ScrapeRun only on passing evidence. Recoverable Recipe failures ask a focused question in chat. Before retry, chat supports exact visible labels and narrow natural-language requests such as “go to the next page”; the configured local main model selects one exact target from fresh manager-filtered same-origin anchors and allowlisted read-only reveal buttons, and the existing manager revalidates evidence, origin, mutating path, a conservative read-only query-key allowlist and button policy before dispatch. Truncated or ambiguous candidates clarify without clicking. Query values are not sent to the planner. The same task receives a fresh snapshot and reuses Recipe validation. Cross-origin links, known transactional links, form buttons and unsafe controls are rejected. Manager/API tests cover inferred next-page selection, duplicate-label clarification, same-origin click, Load more and unsafe-target rejection. Public live-site acceptance remains open.|
|CX4 Chat task lifecycle|PARTIAL|Status/pause/resume/stop, bounded results and export target conversation-owned runs. Pending pre-run tasks can be clarified or stopped in chat; stopping closes the managed browser. Run cards restore/poll and export uses authenticated streaming. Multi-run selection, reconnect and live recovery acceptance remain incomplete.|
|CX5 Local E2E and Computer Use launch|PARTIAL|Chat starts/attaches scoped project E2E through the existing task-owned server and Playwright route. Native chat authorizes and launches an exact allowlisted bundle when closed. Explicit 2–4 step button sequences show per-step chat progress, reuse plan/execute/fresh-visible-effect verification for each step, and stop on the first failure. A single outcome-only button goal uses the configured local main model to infer one exact AXPress label and absent visible postcondition from fresh AX; deterministic validation grounds the label and rejects high-impact labels, then existing action execution and fresh-text verification run. One chat message can start scoped capture and continue into a goal or explicit sequence; goal-only requests replan one action at a time and stop after four dispatched actions. Earlier live run 20261003-095500-window-scope-f825 passed explicit-label launch, capture/OpenCV change processing, AXPress, postcondition, status/stop and shutdown. Later fresh runs 20261003-101500-opencv-after-screen-grant-9c066855 and 20261003-110200-opencv-tcc-regrant-e0988adc failed before the first frame with CGPreflightScreenCaptureAccess=false despite the visible Settings row being ON. The installed app and retained test bundles shared bundle ID, signing authority and designated requirement while per-build CDHashes differed; that comparison does not prove TCC grant or mapping. Separate run 20261003-113400-permission-once-f31c later verified two start/stop cycles with preflight true under an existing grant, without resolving the earlier cause. A later isolated current-source run visually confirmed the exact target-window preview without a recurring Allow prompt or mirror recursion; its chat click failed with `VISION_POINTER_GROUNDING_PROVENANCE_INVALID` because validation read stale React render state. The client now uses `targetApplicationRef.current`, with focused ScreenCapture tests 6/6. A subsequent live run grounded and tracked the exact window but stopped before dispatch with `VISION_POINTER_AX_OBSERVATION_STALE` after two-frame tracking exceeded the 2.5-second AX freshness bound. The client now reacquires same-PID/bundle AX and replans from the fresh observation; focused regression and typecheck pass. Source inspection then found a zero-sized AX frame from the tracking fixture was treated as present by native preflight, causing rejection of the correctly tracked point. Invalid live AX frames now enter the existing guarded fallback (unique exact target plus fresh grounded/tracked point inside the selected app window); valid-frame checks were not relaxed. A zero-frame regression assertion was added; post-fix test and GUI rerun results are pending. Live click rerun remains open. Run 20261003-105300-plan-reason-c321's sanitized reason was `NATIVE_CHAT_POSTCONDITION_INVALID`, which means the grounded plan omitted expected text; the backend now fills that omission from the exact user-parsed result phrase and retains the current AX validator. Run 20261003-111000-static-text-a4c0 passed a chat-only outcome-level fixture goal and independent visible-text verification after the AXStaticText fix. Backend planner cases (4), frontend action tests (16), typecheck, and the native static-text privacy test (1) pass. Project-built app path authorization and exact-path launch are implemented with project containment, bundle-ID allowlist, and Info.plist checks; API/frontend tests pass, but live launch of an unindexed project bundle remains unverified. A later current-source run `20261003-220000-scrollfix` successfully completed one explicit-label vision-grounded OpenCV-tracked pointer action and fresh AX verification; live continuous tracking, outcome-derived multi-action completion, broader apps and reliability remain open.|
|CX6 Settings as policy boundary|PASS|Settings contains capability/scope and OS-permission setup; observation, capture telemetry, monitoring, grounding and World State diagnostics are in a separate Desktop diagnostics surface. Screen Recording status/request/refresh are explicit Settings actions; chat capture start now performs a preflight only and cannot raise a recurring per-task request. Narrow backend scope/capability APIs preserve unrelated policy, and targeted API/UI/Rust tests pass. In isolated GUI run `20261003-113400-permission-once-f31c`, an existing grant remained visible in Settings and two consecutive chat capture start/stop cycles passed with no OS permission request. The first-time/denied TCC request-button path remains untested.|
|CX7 Chat-only E2E orchestration|OPEN|Scraping and local browser E2E launch from chat; public scrape clarification resumes through the existing Recipe/Playwright flow, and one local E2E model-planned journey has passed live GUI verification. Native chat launches scoped targets and routes bounded AX/OpenCV/UI-Venus actions. `ComputerUseCard` embeds a target-window-only preview from a dedicated native command. Targeted ScreenCaptureKit sessions now filter to the exact `SCWindow` matching capture PID and bundle ID; they no longer capture the full display and crop afterward, which allowed the OLCR chat window and system prompt to cover the target in the old crop. Preview freshness remains guarded by capture session, PID and bundle ID; the card polls status every 1.2 seconds and clears stale frames on stop or identity change. An isolated current-source GUI run visually confirmed the exact target-window preview without a recurring Allow prompt or mirror recursion. Its chat click failed with `VISION_POINTER_GROUNDING_PROVENANCE_INVALID` because pointer validation read render-time React target state instead of the synchronously updated `targetApplicationRef.current` after capture start. The client fix has focused ScreenCapture coverage 6/6. A later live run grounded/tracked the exact window but stopped before dispatch with `VISION_POINTER_AX_OBSERVATION_STALE`; the original AX snapshot exceeded the 2.5-second freshness bound during two-frame tracking. The client now reacquires same-PID/bundle AX and replans; focused regression and typecheck pass. The earlier `20261003-053307-chatview-E80098FA` remains a historical recursive-mirror failure. Source inspection found the subsequent `ACCESSIBILITY_TARGET_CHANGED_BEFORE_ACTION` came from zero AX geometry in the fixture. Treating only the live frame as unavailable exposed that the cached expected frame was also invalid (`VISION_POINTER_LIVE_AX_FRAME_UNAVAILABLE`). The corrected path normalizes both cached expected and live AX frames through `usable_pointer_frame` (invalid becomes `None`, valid is retained), then recomputes the tracked point against the current target window. Regression coverage checks zero-to-None and valid-frame retention; post-fix GUI validation is pending. Live click rerun and refresh/reconnect acceptance remain open. Outcome-only native goals use the configured local model, fresh AX, deterministic validation and the existing verifier; iterative planning permits at most four individually verified actions. The explicit-label fixture journey passed cold launch, capture/OpenCV change processing, AXPress, independent verification, status/stop and shutdown in earlier runs. Post-refresh/reconnect, live multi-step completion, continuous tracker/pointer acceptance and broader apps/sites remain open. Latest GUI evidence supersedes the earlier click failures: run `20261003-220000-scrollfix` logged PLANNED/AIMING/CLICKED for an OpenCV-tracked native pointer action (frame 396, confidence 1.00, mapped distance 501.48 points) and the fixture’s fresh AX state showed `Started — tracking verified`. This closes one fixture journey only; multi-step outcome planning, reconnect/recovery and broader apps/sites remain open.|

## P0–P30 phases

|Gate|Status|Evidence and remaining work|
|---|---|---|
|P0 repository audit + baseline|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L11)|
|P1 provenance / unified state|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L12)|
|P2 Web Read|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L13)|
|P3 ScreenCaptureKit|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L14)|
|P4 change sentinel|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L15)|
|P5 desktop persistent state|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L16)|
|P6 Accessibility perception|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L17)|
|P7 accessibility notifications|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L18)|
|P8 temporal visual state|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L19)|
|P9 Adaptive Perception Controller|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L20)|
|P10 OCR evaluation|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L21)|
|P11 UI-Venus escalation|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L22)|
|P12 Action Router|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L23)|
|P13 accessibility actions|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L24)|
|P14 shortcut integration|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L25)|
|P15 action verification|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L26)|
|P16 routine trace capture|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L27)|
|P17 routine compiler/cache|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L28)|
|P18 browser session manager|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L29)|
|P19 Scrape Studio element picker|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L30)|
|P20 Recipe model|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L31)|
|P21 deterministic recipe execution|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L32)|
|P22 navigation|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L33)|
|P23 streaming export|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L34)|
|P24 detail pages|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L35)|
|P25 network observer|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L36)|
|P26 scraper recovery|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L37)|
|P27 NL to Recipe|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L38)|
|P28 unified integration|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L39)|
|P29 benchmark / ablations|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L40)|
|P30 documentation|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L41)|

## V acceptance

|Gate|Status|Evidence and remaining work|
|---|---|---|
|V1 continuous capture / P3|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L50)|
|V2 event/pixel change detection / P4|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L51)|
|V3 persistent desktop state / P5|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L52)|
|V4 AX semantics / P6|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L53)|
|V5 AX notifications / P7|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L54)|
|V6 temporal visual state + APC / P8–P9|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L55)|
|V7 OCR recovery / P10|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L56)|
|V8 UI-Venus semantic escalation / P11|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L57)|
|V9 ROI-first vision / P11|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L58)|

## A acceptance

|Gate|Status|Evidence and remaining work|
|---|---|---|
|A1 Action Router|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L59)|
|A2 AX action|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L60)|
|A3 shortcut route|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L61)|
|A4 deterministic pointer fallback|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L62)|
|A5 Vision-grounded final fallback|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L164)|
|A6 all routes verified|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L64)|

## R acceptance

|Gate|Status|Evidence and remaining work|
|---|---|---|
|R1 trace captured|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L65)|
|R2 routine candidate generated|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L66)|
|R3 preconditions checked|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L67)|
|R4 deterministic routine executes|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L68)|
|R5 routine success verified|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L69)|
|R6 failed routine returns safely|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L70)|
|R7 routine matching|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L71)|
|R8 failure handling|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L72)|
|R9 routine revalidation|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L73)|
|R10 bounded generalization|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L74)|

## W acceptance

|Gate|Status|Evidence and remaining work|
|---|---|---|
|W1 Web Read|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L75)|
|W2 browser session|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L76)|
|W3 login sessions|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L77)|
|W4 element picker|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L78)|
|W5 repeating-row detection|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L79)|
|W6 field selection|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L80)|
|W7 Recipe|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L81)|
|W8 Navigation|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L82)|
|W9 detail pages|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L83)|
|W10 deterministic execution|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L84)|
|W11 NL→Recipe|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L85)|
|W12 Network Observer|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L86)|
|W13 scraper recovery ladder|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L87)|
|W14 validation/dedup/stop|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L88)|
|W15 streaming export|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L89)|
|W16 Scrape Studio UI|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L90)|

## U acceptance

|Gate|Status|Evidence and remaining work|
|---|---|---|
|U1 provenance|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L91)|
|U2 World State|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L92)|
|U3 verification|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L93)|
|U4 recovery|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L94)|
|U5 telemetry|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L95)|
|U6 regressions|PARTIAL|[Canonical ledger row](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#L96)|

## Cross-cutting current blockers

- Current-source backend regression after the R8/R9 changes passed 1,061 tests, 7 skipped, and 548 subtests in 193.14 seconds with an isolated temporary DB. Frontend passed 14 files / 87 tests, typecheck, and production build (760.34 kB JS / 227.14 kB gzip). These fixture/unit checks do not close live TCC, physical GUI, live-site, or broad ablation gates.
- The bounded 2026-10-03 permission repair now passes a live explicit-label native fixture journey. See the [verified checkpoint](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md#verified-native-permission-and-chat-action-checkpoint-2026-10-03) for exact run identity, measured frames, fixes and shutdown. Earlier TCC denials are historical; the current Launch Services launcher passed CoreGraphics preflight with the existing grant.
- Run `20261003-111000-static-text-a4c0` also passed a chat-only outcome-level goal on the allowed fixture: OLCR pressed “Start game,” the fixture showed “Game started,” and the independent verifier returned success after the AXStaticText-only read fix. Chat Stop released capture and shell checks confirmed both isolated processes and the port were gone. This is one live fixture goal, not broad planner reliability or vision-pointer evidence.
- Live ScreenCaptureKit and OpenCV visual-change processing are verified on the allowed fixture. Achieved capture FPS, physical capture cost, continuous grounded tracking, UI-Venus pointer actions and comparative task ablation remain unverified.
- The current-source fixture run `20261003-220000-scrollfix` exercised live UI-Venus/OCR grounding, OpenCV Lucas–Kanade tracking, Workspace activation, physical native pointer dispatch and independent AX effect verification on an explicitly allowlisted fixture (frame 396; confidence 1.00; mapped move distance 501.48 points). The chat preview showed the tracked target and pointer feedback. This closes that one target-bound button journey. A5 remains partial for representative display scaling, multi-display/window-movement recovery, repeated live acceptance and the separate disclosure route. Chat text entry accepts both value-first and target-first Japanese phrasing plus English “fill the field with value”. It can focus a unique, visible, enabled, non-secure AX text field/area within the selected app before replacing its value, and verifies private AXValue readback without returning the value. Planner/API/frontend/native compile checks pass; this focus-first path still awaits live GUI acceptance. Chat also supports one allowlisted Enter/Escape/Space/arrow key on a uniquely focused visible AX target when the user gives a new visible-text postcondition; focused planner/API/frontend/native tests pass, but live GUI acceptance remains open. Native chat also has a bounded wheel-scroll route that identifies a unique visible AX scroll area and a geometrically associated vertical scrollbar, sends a small target-PID wheel event, and verifies a fresh directional scroll-value change. Its planner/API/frontend/native geometry tests pass, but live direction behavior is still unverified. Staged pointer movement still awaits isolated GUI acceptance. Other key input, drag, and AX-free canvas/game actions remain outside chat's current action set.
- OpenCV remains opt-in. Signed native build/runtime loading and current live change-processing evidence pass; this does not close live continuous tracker, cost or end-to-end ablation gates.
- Verified routines now count target-bound confirmed successes and native post-dispatch unconfirmed effects. Two consecutive such failures move a candidate to `SUSPECT`. The one-action MVP dispatches through the existing frontend/native boundary and records through `ProcedureRunner`; direct backend GUI-tool registration is intentionally excluded. Multi-action routine support and live routine acceptance remain open.
- Scrape Studio reuses managed Playwright and its existing Recipe lifecycle, including the bounded network-JSON Recipe path. Persistent login-profile implementation now exists; real sign-in and authenticated-site acceptance, broad live-site quality, and full recovery/large-export acceptance remain open.
- The final A–J report and blog remain deferred until the requested implementation gates have evidence; the blog still awaits the user’s explicit instruction.
