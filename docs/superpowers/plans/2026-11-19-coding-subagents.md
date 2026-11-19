# Coding Task subagents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an independently opt-in, fixed-role subagent workflow to Coding Tasks that runs only for heavy parallelizable tasks after user authorization and uses the exact Coding Task model selected for the active phase.

**Architecture:** OLCR owns settings, task classification, authorization, model pinning, persistence, workspace scope, cancellation, and the UI. A `CodingSubagentExecutor` port isolates the worker runtime; an early spike qualifies the OpenAI Agents SDK before deciding whether to adopt it or route calls through OLCR's existing model runtime. Worker calls may run in bounded parallel, but validated changes are applied serially by the existing host workspace boundary.

**Tech Stack:** Python/FastAPI, SQLite, existing OLCR model/runtime and workspace boundary, React/TypeScript, Vitest, pytest. Optional dependency candidate: OpenAI Agents SDK (`openai-agents`), subject to the compatibility spike and dependency/license review.

**Spec:** `docs/superpowers/specs/2026-11-19-coding-subagents-design.md`

## Global Constraints

- `coding_subagents_enabled` defaults to `false` and is stored separately from `task_manager_enabled`.
- Spawn requires `setting_enabled AND heavy_parallelizable_assessment AND (explicit_user_directive OR explicit_task_approval_for_current_plan_revision)`.
- A normal Coding Task plan approval, a model output, or an agent output never counts as user authorization.
- Task size gate for v1: at least three substantive implementation workstreams across at least two components, and at least two dependency-independent groups with disjoint write scopes.
- Policy interpretation: a qualifying plan must contain at least two distinct pairs of implementation phases whose transitive dependencies do not connect the pair and whose host-assigned file scopes do not overlap. Phase-only analysis, test-only phases, verification-only phases, shared paths, missing ownership, or unsupported dependency graphs fail closed.
- Every worker starts with the active phase's resolved Coding Task model/provider/runtime. Unified Brain mode resolves to the one unified selection. Snapshot the configured fallback sequence into the task approval; only that sequence may be used after primary failure. Workers cannot add or edit fallback choices.
- Maximum three workers in a group; no nested spawning in v1. The host keeps a separate bounded worker execution lane; non-agent model calls keep the existing global limit.
- Workers cannot publish, access release credentials, commit, or execute arbitrary shell/URL operations. They return typed, scope-bound proposals; OLCR validates and applies changes serially.
- Turning the setting on requires a modal acknowledgement of the local-model warning. The warning must explicitly advise against local models and explain coordination/latency/resource risks.
- Prompts are not security boundaries. Every agent tool call, model binding, task authorization, and mutation is validated in host code.
- The implementation must preserve Coding Task pause/resume, plan revision, file scope, sandbox, verification, and telemetry invariants.

## Review Focus

- Config OFF but direct user asks for agents — assert no spawn tool, worker record, or model call occurs; normal single-agent flow continues.
- Heavy task but only normal plan approval exists — assert a distinct worker approval remains pending.
- User instruction exists but task is small or workstreams overlap — assert the task remains single-agent.
- Unified model changes during a paused task — assert already authorized workers keep the snapshotted model and stale approval is invalidated when a new run/revision is created.
- Worker requests writes outside its assigned files or tries to publish — assert the host rejects the operation before workspace/provider access.

---

### Task 1: Qualify external agent runtimes and freeze the adapter boundary

**Files:**
- Create: `docs/adr/2026-11-19-coding-subagent-runtime.md`
- Create (temporary spike only): `backend/spikes/coding_subagent_sdk/` or an isolated `/private/tmp` prototype
- No permanent runtime code or test file in this task; preserve any spike tests under the temporary prototype only.

**Interfaces:**
- Produces: an ADR that freezes the required `CodingSubagentExecutor.run(request) -> result` and async cancellation contract, independent of the selected SDK. Task 5 owns the production interface and its permanent compatibility tests.
- Consumes: existing Coding Task model binding and the fixed-role policy from the spec.

- [x] **Step 1: Define the executor contract and a temporary compatibility harness**
  Cover exact model/provider/runtime forwarding, role instruction immutability, typed tool calls, cancellation, token/usage capture, structured result extraction, and no workspace mutation before host validation.
- [x] **Step 2: Exercise the temporary contract harness against fakes**
  Run the spike's documented command. Expected: PASS using fake provider/model/tools only; no real API calls or user credentials. Do not add a permanent production dependency or contract test yet.
- [x] **Step 3: Qualify the OpenAI Agents SDK adapter without adding a production dependency**
  Verify `Agent.instructions`, `Agent.as_tool()`/`Runner.run`, run-level model-provider selection, approval interruptions, cancellation, and bounded structured outputs from current official docs. Use an installed-package fake-provider prototype only when the package is already available. If it is absent in the restricted environment, do not install a new runtime solely for the spike; record unverified behavior in the ADR and prefer the existing OLCR runtime. Explicitly pass only the task/phase context required by each role.
- [x] **Step 4: Compare LangGraph and the native OLCR runtime against the same contract**
  Record whether LangGraph checkpoint/interrupt behavior adds value beyond `coding_tasks` persistence. Do not replace the existing task graph or workspace executor as part of this feature.
- [x] **Step 5: Record the adoption decision and dependency review**
  Adopt `openai-agents` only if its adapter passes every compatibility test, preserves OLCR's exact selected model and bounded tools, supports cancellation/recovery adequately, and has acceptable Python/license/lockfile impact. Otherwise implement the same port over the existing OLCR model runtime. No conditional SDK failure may trigger model substitution.
- [x] **Step 6: Commit the decision record and record the temporary harness outcome**

### Task 2: Add an isolated setting and the required local-model warning

**Files:**
- Modify: `backend/olcr_api/config.py`
- Modify: `backend/olcr_api/app.py` (`SettingsInput`, GET/PUT settings validation)
- Modify: `frontend/src/ExternalToolsSettings.tsx`
- Modify: `frontend/src/ExternalToolsSettings.test.tsx`
- Modify: `backend/tests/test_local_coding_workflow.py` or create `backend/tests/test_coding_subagent_settings.py`

**Interfaces:**
- Produces: `Settings.coding_subagents_enabled: bool = False`; a dedicated API capability response reports enabled state and warning version.
- Consumes: existing `task_manager_enabled` setting without deriving, copying, or toggling it.

- [x] **Step 1: Write failing backend tests for OFF-by-default and setting independence**
  `test_coding_subagents_default_off`, `test_coding_subagents_setting_is_independent_of_task_manager`, `test_model_or_task_manager_update_cannot_enable_subagents`.
- [x] **Step 2: Write failing UI tests for enable confirmation**
  `test_enabling_coding_subagents_requires_local_model_warning_acknowledgement`, `test_cancel_warning_keeps_setting_off`, and `test_warning_is_shown_even_when_current_model_is_remote`.
- [x] **Step 3: Implement the server setting and strict API validation**
  Missing, malformed, or unknown settings must resolve to false. Only the explicit Settings control can update this flag; model plans, chat parsing, and `task_manager_enabled` updates cannot.
- [x] **Step 4: Implement a required enable-confirmation dialog**
  The Japanese and English copy must state “local models are not recommended” and mention role-following/synthesis quality, latency, and memory/compute load. Require a visible acknowledgement before persisting true; persist a warning revision so future copy changes can be shown again.
- [x] **Step 5: Run the focused setting tests**
  Run: `python -m pytest backend/tests/test_coding_subagent_settings.py -q`
  Run: `npm --prefix frontend test -- ExternalToolsSettings.test.tsx`
  Expected: PASS; no coding task or agent call is required to test settings.
- [x] **Step 6: Commit settings and warning behavior**

### Task 3: Define fixed roles, model pinning, heaviness assessment, and authorization facts

**Files:**
- Create: `backend/olcr_api/coding_subagents.py`
- Test: `backend/tests/test_coding_subagent_policy.py`
- The task-lifecycle call site in `backend/olcr_api/app.py` is owned by Task 6 so the pure policy can be tested without changing scheduling before persistence and approval endpoints exist.

**Interfaces:**
- Produces: immutable `CodingSubagentRole`, `CodingSubagentModelBinding`, `DelegationAssessment`, and `DelegationAuthorization` data types.
- Produces: `assess_delegation(plan, task_id, plan_revision, setting_enabled)`, `pin_subagent_model(effective_phase_model, brain_configuration)`, and revision/model-bound authorization helpers.
- Consumes: `_coding_phase_model` output, Brain configuration resolution, approved plan revision/hash, and actual user-originated task messages.

- [x] **Step 1: Write policy tests for the hard gate**
  Include `test_setting_off_never_eligible`, `test_small_task_rejected_even_when_user_directs_agents`, `test_dependent_workstreams_rejected`, `test_three_independent_scoped_workstreams_are_heavy`, `test_model_plan_text_never_counts_as_user_authorization`, `test_plan_approval_is_not_agent_approval`, `test_directive_must_come_from_user_message`, and `test_replan_invalidates_task_approval`.
- [x] **Step 2: Write model-pinning tests**
  `test_workers_use_phase_coding_model_not_planner_model`, `test_unified_brain_selection_pins_same_model_for_all_roles`, `test_split_mode_pins_the_current_implementation_role_model`, and `test_subagent_binding_snapshots_configured_fallbacks_in_order`.
- [x] **Step 3: Implement the fixed role registry**
  Define only `repo_explorer`, `implementation_worker`, and `test_reviewer` for v1. Store immutable developer instructions and prompt versions server-side. Task 5 owns the worker request schema that rejects unknown roles, user-supplied role prompts, nested-spawn options, and model overrides.
- [x] **Step 4: Implement deterministic assessment and authorization types**
  Count only substantive implementation workstreams; prove component separation, dependency independence, and disjoint write scopes. Store authorization source (`USER_DIRECTIVE` or `TASK_APPROVAL`) plus source message/approval event, task ID, plan revision/hash, model binding, role set, and scopes.
- [x] **Step 5: Implement a single model-binding resolver for child calls**
  Resolve and freeze provider/runtime/model identity before any spawn. Unified mode must resolve to the current single selection. Replan, provider change, or implementation model change makes any pending task authorization stale.
- [x] **Step 6: Run policy and model-pinning tests**
  Run: `python -m pytest backend/tests/test_coding_subagent_policy.py -q`
  Expected: PASS with provider fakes and no worker execution.
- [x] **Step 7: Commit role registry and host policy**

### Task 4: Persist worker lifecycle and add a separate user approval transition

**Files:**
- Modify: `backend/olcr_api/db.py`
- Modify: `backend/olcr_api/app.py` (Coding Task agent proposal/approve/decline endpoints)
- Test: `backend/tests/test_coding_subagent_workflow.py`
- Test: `backend/tests/test_coding_subagent_api.py`

**Interfaces:**
- Produces: additive `coding_subagent_runs` table with run/worker IDs, task/phase/revision, role, prompt version, fixed model binding, scopes, status, timestamps, safe summary, bounded usage, and error category. Do not store credentials, raw hidden reasoning, or arbitrary full provider responses.
- Produces: typed approval API actions for `APPROVE_DELEGATION` and `DECLINE_DELEGATION`; approval is one-shot and bound to the assessment hash.
- Consumes: existing task events, user-facing plan approval, cancellation, and resume APIs without conflating their authorization semantics.

- [x] **Step 1: Write failing persistence and approval tests**
  Cover valid state transitions, duplicate spawn/idempotency, stale approval rejection, task-plan approval separation, replan invalidation, restart state, no secret persistence, and no agent creation on a stale or declined approval.
- [x] **Step 2: Implement additive SQLite records and transition validation**
  Keep each worker's lifecycle outside `subtask_progress`, which assumes one running phase. Persist states `PROPOSED`, `AWAITING_USER_APPROVAL`, `AUTHORIZED`, `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT`, and `INTERRUPTED` with a strict transition map.
- [x] **Step 3: Add host endpoints and event records**
  Approve/decline endpoints must validate task owner, current plan revision/hash, model binding, setting state, heavy assessment, and pending proposal. Only an actual user action changes authorization to approved. A prompt saying “approve” in model output has no effect.
- [x] **Step 4: Implement recovery and cancellation semantics**
  Parent pause/cancel cancels queued and active children. On process restart, unfinished workers become interrupted and are inspected/recovered without replaying accepted file mutations. Changing the global setting off blocks new workers and cancels active worker generations before continuing the parent in single-agent mode.
- [x] **Step 5: Run persistence/API tests**
  Run: `python -m pytest backend/tests/test_coding_subagent_workflow.py backend/tests/test_coding_subagent_api.py -q`
  Expected: PASS with no external providers.
- [x] **Step 6: Commit persistence and authorization lifecycle**

### Task 5: Implement the bounded executor and safe result integration

**Files:**
- Create: `backend/olcr_api/coding_subagent_executor.py` (production executor port, request/result types, and adapter)
- Modify: `backend/olcr_api/coding_tasks.py` (separate bounded worker lane and cancellation propagation)
- Modify: `backend/olcr_api/app.py` (phase coordinator integration)
- Modify: `backend/olcr_api/task_workspace.py` only for the host's serialized apply operation if required
- Test: `backend/tests/test_coding_subagent_executor.py`
- Test: `backend/tests/test_coding_subagent_workspace.py`
- Test: `backend/tests/test_coding_subagent_runtime_compatibility.py` (permanent executor contract suite specified by Task 1 ADR)

**Interfaces:**
- Produces: `CodingSubagentRequest(task_id, phase_id, plan_revision, worker_id, role_id, model_binding, task_input, assigned_paths, allowed_actions)` and `CodingSubagentResult(summary, findings, proposed_mutations, changed_paths, usage, error_category)`.
- Produces: executor adapter selected by Task 1 (Agents SDK only if accepted; otherwise existing OLCR runtime). The adapter cannot choose model, role, tools, scope, or provider host.
- Consumes: registered immutable roles, approved authorization, current Coding Task model binding, and existing typed workspace mutation pipeline.

- [x] **Step 1: Write fake-executor tests for bounded orchestration**
  Cover maximum three workers, per-runtime concurrency, local model serialized capacity, remote worker capacity, cancellation while queued/running, timeout, partial result aggregation, no nested spawn, and use of only the snapshotted configured fallback sequence.
- [x] **Step 2: Write workspace-scope and conflict tests**
  `test_worker_proposal_outside_assigned_paths_is_rejected`, `test_stale_preimage_is_rejected`, `test_overlapping_worker_proposals_are_serialized_or_blocked`, `test_worker_cannot_invoke_release_publish`, and `test_worker_never_commits_changes`.
- [x] **Step 3: Implement provider/runtime-bound execution**
  Pass the exact persisted model binding to every role. Disable role-level model overrides and unregistered tools. Meter tokens/time and preserve the existing global execution limit for ordinary calls. Only the feature's explicitly authorized executor may use its bounded parallel lane; local model workers are serialized by default.
- [x] **Step 4: Implement typed proposal validation and serial application**
  Workers return file-scoped proposals. Validate against task approved scopes, assigned file ownership, expected preimages, workspace path, and current plan revision. Apply only accepted proposals through the existing host workspace APIs in a deterministic order. Reject conflicts and let the parent decide the next single-agent step.
- [x] **Step 5: Add cancellation, timeout, and result synthesis**
  Stop workers on task cancel/pause and aggregate concise worker results into the existing parent phase context. Keep worker summaries/result evidence separate from hidden reasoning. If executor setup fails, continue with the already-selected parent model and report a single-agent fallback reason.
- [x] **Step 6: Run executor/workspace tests**
  Run: `python -m pytest backend/tests/test_coding_subagent_executor.py backend/tests/test_coding_subagent_workspace.py -q`
  Expected: PASS with fake provider/executor only; no project-controlled shell/process is run outside current sandbox policy.
- [x] **Step 7: Commit executor and workspace integration**

### Task 6: Integrate the gate into Coding Task lifecycle and task progress UI

**Files:**
- Modify: `backend/olcr_api/app.py` (after approved plan/model resolution and before implementation phase work)
- Modify: `backend/olcr_api/task_review_api.py` if the review response needs child summaries
- Modify: `frontend/src/CodingTaskProgress.tsx`
- Modify: `frontend/src/CodingTaskProgress.test.tsx`
- Modify: `frontend/src/coding-review.css`
- Test: `backend/tests/test_coding_subagent_integration.py`

**Interfaces:**
- Produces: task response fields `delegation_assessment`, `delegation_authorization`, and bounded `subagents[]` summaries with worker ID, role, scope, state, model label, and safe result.
- Consumes: existing Coding Task progress/review, plan revision, follow-up, pause, cancellation, and task event surfaces.

- [x] **Step 1: Write integration tests for all three spawn conditions**
  Include setting off, setting on but task small, heavy task waiting for approval, user direct instruction, declined approval, plan revision invalidation, and direct directive while setting off.
- [x] **Step 2: Integrate assessment at the host boundary**
  Run it only for a Coding Task after the task plan is stable and immediately before eligible implementation work. Normal chat, direct coding fallback, verification-only phases, release publication, and task-manager-off flow do not expose a subagent spawn path.
- [x] **Step 3: Keep parent/child state separate**
  Store worker rows and send task events without marking multiple `coding_tasks.subtask_progress` phases as RUNNING. Parent plan and final report remain authoritative.
- [x] **Step 4: Implement approval proposal and active-worker progress UI**
  Show why the task qualifies, roles/count/scopes, the selected model, and local-model warning as applicable. Provide separate approve/decline controls. Show status, safe summaries, elapsed time, and cancel; do not display raw hidden reasoning or sensitive tool output.
- [x] **Step 5: Run backend/frontend integration tests**
  Run: `python -m pytest backend/tests/test_coding_subagent_integration.py -q`
  Run: `npm --prefix frontend test -- CodingTaskProgress.test.tsx`
  Expected: PASS; worker tests use fake executors.
- [x] **Step 6: Commit task lifecycle and UI integration**

### Task 7: Documentation, operational evaluation, and security review

**Files:**
- Modify: `docs/LOCAL_CODING_WORKFLOW.md`
- Modify: `backend/olcr_api/help_knowledge/coding-ja.md`
- Modify: `backend/olcr_api/help_knowledge/coding-en.md`
- Modify: `backend/olcr_api/help_knowledge/settings-ja.md`
- Modify: `backend/olcr_api/help_knowledge/settings-en.md`
- Test: `backend/tests/test_coding_subagent_docs.py`

**Interfaces:**
- Produces: user-facing documentation for default-off behavior, how to enable, warning about local models, task authorization, when work qualifies, model pinning, worker visibility, cancellation, and fallback.
- Consumes: actual implementation/support limits from Tasks 1–6; do not document unsupported provider/model modes as working.

- [x] **Step 1: Write matrix/wording checks**
  Assert docs state the default is OFF, the setting is separate from Coding Orchestrator, local models are not recommended, user authorization is mandatory, and children use the task's selected model.
- [x] **Step 2: Add Japanese/English settings and task help**
  Explain that enabling the setting does not automatically spawn agents, and a small or non-parallelizable task will remain single-agent.
- [x] **Step 3: Evaluate the adoption path with fake and selected-provider tests**
  Run the same acceptance contract against every enabled executor. Use fake providers in automated CI. Any manual provider compatibility check must use a test project and explicit credentials; it must not publish, deploy, or change user repositories.
- [x] **Step 4: Run full targeted suites and final diff review**
  Run: `python -m pytest backend/tests/test_coding_subagent_*.py -q`
  Run: `npm --prefix frontend test -- ExternalToolsSettings.test.tsx CodingTaskProgress.test.tsx`
  Run: `git diff --check`
  Expected: all feature suites PASS; independent review confirms the hard gate, model identity, user approval source, local-model warning, file-scope enforcement, cancellation, and no release-capability leakage.
- [x] **Step 5: Commit docs and review fixes**

## End-to-end exit criteria

- With the feature OFF, ordinary Coding Orchestrator behavior has no subagent capability and produces no child records/model calls.
- With feature ON, only a plan passing the heavy and independent-scope threshold can reach the authorization step.
- A task-specific user approval or direct user instruction is required; approval is revision-bound, and normal plan approval never suffices.
- Every worker uses the exact selected Coding Task model/provider/runtime. Unified selection stays unified. Failure does not route to a different model.
- The local-model warning is visible and acknowledged before setting enablement.
- Child work is fixed-role, observable, cancellable, resumable, file-scoped, and host-validated before mutation.
- External SDK adoption, if any, passes the same test contract and introduces no second task/workspace authority.

## Risks kept explicit

- OpenAI Agents SDK/OpenAI hosted multi-agent APIs evolve quickly; the SDK compatibility spike must pin a tested version and inspect licensing/dependency behavior at implementation time.
- Local model concurrency may remain serialized even when multiple workers have separate context. The setting warning must not promise a latency reduction.
- Parallel file proposals can conflict; the host must prefer rejecting conflicts and continuing serially over applying ambiguous overlapping edits.
- Heavy-task heuristics can over- or under-select. Record the assessment evidence for evaluation, but do not let it grant authorization.

## Plan review

Independent builder check on 2026-11-19 found no material unanswered questions (`nothing important left`).
