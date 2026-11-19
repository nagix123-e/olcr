# Coding Task subagents design

**Status:** Proposed design for review. This request asks for a plan; no implementation code is included in this change.

## Intent

Add Codex-like specialist agents to OLCR Coding Tasks so a large implementation can be divided among fixed-role model workers, coordinated by the existing Coding Task orchestrator, and reviewed as one task. The feature must remain isolated behind its own explicit setting and must never activate merely because the ordinary Coding Orchestrator is enabled or because a model proposes delegation.

## Current project shape

- `backend/olcr_api/app.py` owns Coding Task scheduling, phase execution, status APIs, and `_coding_phase_model` model selection. The implementation phase freezes a selected coding model before calling the runtime.
- `backend/olcr_api/brain_models.py` resolves role-specific or unified conversation model choices. A group of subagents must pin the resolved implementation model for that task phase, rather than choosing a role-specific alternate.
- `backend/olcr_api/coding_tasks.py` serializes model calls through a shared model slot. Task phase progress also assumes one phase is `RUNNING` at a time.
- `backend/olcr_api/task_workspace.py` locks a task workspace during execution. Concurrent agents must not apply overlapping edits directly to that shared tree.
- `backend/olcr_api/custom_agents.py` and `custom_agent_runtime.py` already support chat-oriented custom agents, but their lifecycle and tools are not the Coding Task execution boundary and must not be reused as a shortcut that widens permissions.
- `backend/olcr_api/config.py`, `SettingsInput`, `/api/settings`, and `frontend/src/ExternalToolsSettings.tsx` are the existing global setting path. A Coding Subagents setting must be independent of `task_manager_enabled`.

## Goals

1. Add a separate, default-off `coding_subagents_enabled` setting. The setting being on only makes the feature available; it never authorizes a specific task to spawn workers.
2. Spawn workers only when all three host-verified conditions hold: the feature is enabled, the task meets the “heavy and parallelizable” threshold, and the user either explicitly instructed delegation or approved a task-specific proposal.
3. Use a small, server-owned set of fixed role prompts. The parent may select only registered roles and must not rewrite their core instructions or model.
4. Pin every worker in a delegation group to the effective Coding Task implementation model for that phase. When Brain uses one unified selection, the pinned model must be that unified model. Do not silently substitute another model/provider when a worker fails.
5. Require a clear warning when enabling the feature that recommends against local models because coordination quality, latency, and local compute pressure may be worse. Acknowledge the warning before saving the setting.
6. Persist task-level delegation authorization, worker lifecycle, model identity, prompt version, bounded result summaries, and cancellation state. Show useful progress in the Coding Task UI.
7. Preserve the existing task workspace, source-scope, verification, sandbox, release, and final-report boundaries. Children cannot publish releases or perform other external operations on behalf of the task.

## Non-goals

- Turning multi-agent execution on by default, coupling it to the ordinary Coding Orchestrator switch, or allowing a model plan to change settings.
- Spawning agents for ordinary chat, a small bug fix, or a task with one dependent workstream.
- Exposing user-authored agent prompt files, arbitrary agent names, nested worker spawning, alternate per-worker model selection, or arbitrary shell/URL access in v1.
- Replacing the existing Coding Task planner, phase graph, database lifecycle, workspace manager, verification loop, or release adapters.
- Making a local model impossible to use. The UI must strongly advise against it, and the user can still choose to proceed.

## Authorization and heavy-task policy

The host enforces the gate immediately before worker creation:

```text
can_spawn = coding_subagents_enabled
            AND heavy_parallelizable_assessment
            AND (user_directive OR task_specific_approval_for_current_revision)
```

The feature flag is a hard global gate. If it is off, a direct user request is recorded as unavailable and the task continues through the ordinary single-agent path; it cannot turn the setting on. Plan approval, permission mode, a model-generated plan, an agent's own request, or a natural-language statement written by a model does not count as user approval.

For the first release, a task is eligible only if its approved plan contains at least three substantive implementation workstreams across at least two components, and the host can identify at least two dependency-independent groups with non-overlapping write scopes. Research-only, review-only, test-only, and verification-only steps do not count toward the implementation threshold. This threshold is deliberately conservative and should be adjusted only from an evaluation set, not from one model's unverified “large task” label.

If the user did not directly request delegation, the task pauses and presents a separate approval proposal containing the roles, worker count, proposed scopes, selected model, and expected tradeoffs. Approving the Coding Task plan is not approval to spawn workers. Approval is bound to the task ID, approved plan revision/hash, model binding, and proposed roles/scopes; replan or model change invalidates it. A direct user instruction can authorize a heavy task without another confirmation, but it does not bypass the global setting or heavy-task test.

## Roles, model, tools, and workspace

V1 uses a versioned role registry owned by OLCR. Initial roles are `repo_explorer` (read-only code map), `implementation_worker` (narrow assigned files), and `test_reviewer` (read-only review of acceptance evidence). The host constructs instructions from the role registry and passes bounded task context; no user or model input can override role invariants.

The host resolves the implementation model from the active phase before making any child call and persists its provider, runtime, model ID, and model revision/provenance. All children in one group receive that same binding explicitly. If the conversation is in unified Brain mode, resolve the unified selection once and use it for every child. Child prompts and SDK configuration cannot select another model. If the selected model is unavailable or incompatible, stop delegation and continue with the original implementation path using its already-selected model.

The first version allows parallel reasoning, but workspace mutations remain host-controlled. Read-only agents may run concurrently. Implementation workers return typed, file-scoped change proposals; OLCR validates ownership, preimages, and approved scopes and applies accepted mutations serially through the existing workspace boundary. Conflicting proposals are rejected or sent back for serial resolution; workers do not write the shared workspace directly, create commits, run release adapters, or spawn grandchildren. Agent prompts are not considered a security boundary: the host validates every tool and file operation.

Use an explicit bounded worker limit of three, independent of any upstream default. The current global model-call slot is one; the plan must add a separate, bounded Coding Subagent execution lane without allowing unrelated model calls to bypass their existing limit. Remote-provider concurrency may be up to the configured cap; local model calls remain serialized unless a runtime-specific, tested capacity policy allows more. Snapshot the active Coding Task model's configured fallback sequence into the user-approved binding and allow only that sequence; an empty sequence means no fallback, and workers cannot add or edit fallback choices.

## Settings warning

Transitioning the setting from off to on must open a confirmation dialog with a required acknowledgement. The dialog must say that local models are not recommended for multi-agent coding because role following and synthesis can be less reliable, while multiple contexts can increase latency, memory/compute pressure, and resource usage. If the effective Coding Task model is local, show the same warning again in the task-specific delegation proposal. Store a warning version/acknowledgement so UI re-renders or existing settings state cannot silently enable the feature.

## External project assessment

The implementation plan includes a time-boxed compatibility spike for the **OpenAI Agents SDK**. Its official Python SDK provides fixed `Agent.instructions`, `Agent.as_tool()`, run-level model/provider selection, approval interruptions, and sessions. Its `ModelProvider` boundary could potentially route every role through OLCR's already-selected Coding Task model. However, parent context is not automatically inherited by `Agent.as_tool()` children, approval on a tool is not the required task authorization policy, and the SDK does not replace OLCR's database, workspace, or sandbox enforcement. Adopt it only if the spike proves exact model pinning, existing provider compatibility, bounded typed tool calls, cancellation, and result mapping without duplicating task state.

**LangGraph** is a secondary comparison candidate for subgraphs, checkpointing, and interrupt/resume; its state graph could duplicate OLCR's existing persistent task phases. **OpenHands Software Agent SDK** is a later replacement candidate for a complete coding worker/workspace loop, not an incremental multi-agent coordinator. The hosted **Codex Agents API/Responses Multi-agent** is a close behavioral reference and possible future backend, but it is beta and cloud/model constrained; it cannot be the default path for a task whose selected model is local or from another provider.

Recommended architecture: OLCR owns the hard gates, model binding, workspace scope, durable task state, and presentation. Keep a small `CodingSubagentExecutor` interface. Try the Agents SDK behind that interface first; if any required invariant fails, use OLCR's existing model provider/runtime behind the same interface instead. No third-party dependency is added without a passing spike and lockfile/license review.

## Codex subagent behavior verified from official sources

Verified 2026-11-19 against the Codex Subagents and OpenAI Multi-agent documentation:

- Local Codex delegates when directly asked or when applicable project/skill instructions request it; the Codex subagent feature is enabled by default in current releases. Subagents have separate working contexts/threads, while the parent coordinates, waits, steers, and synthesizes the result. Each worker's model and tool work increases total token use.
- Codex custom agents use standalone TOML definitions with `name`, `description`, and `developer_instructions`. Built-ins include `default`, `worker`, and `explorer`. Narrow roles and matching tool surfaces are recommended.
- Local Codex global settings include `agents.enabled`, concurrent-thread limits, default subagent model/reasoning, and interruption-message behavior. Explicit spawn values, global defaults, parent values, and custom-agent model overrides have a precedence order. OLCR should not inherit that model flexibility: the host pins the task model.
- Subagents inherit the parent's provider, tools/MCP, sandbox, and approval mode. A custom role prompt cannot create a stronger sandbox or replace parent permission settings. Therefore OLCR must validate tools and writes in host code.
- Separate contexts do not imply isolated workspaces. Agents working on the same files require coordination. The Agents API uses one environment for root and children; creating a child does not create a new workspace.
- Responses API multi-agent exposes spawn, message, follow-up, wait, interrupt, and list actions; the root is responsible for collecting a final answer. Its guide recommends a concurrency limit of three, although a separate Agent Sessions API reference currently documents a different default of six. OLCR must set its own explicit cap instead of relying on a changing upstream default.
- The hosted Responses multi-agent interface is beta and documents limitations for `/responses/compact`, `reasoning.summary`, and `max_tool_calls` while automatic per-agent compaction is used.

### Primary references

- [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [OpenAI Responses API Multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent)
- [OpenAI Agents API Multi-agent](https://developers.openai.com/api/docs/guides/agents-api/multi-agent)
- [OpenAI Agents SDK: Agents as tools and approvals](https://openai.github.io/openai-agents-python/tools/)
- [OpenAI Agents SDK: Model providers](https://openai.github.io/openai-agents-python/models/)
- [OpenAI Agents SDK: Sessions](https://openai.github.io/openai-agents-python/sessions/)
- [LangGraph subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)

## Acceptance criteria

1. With the setting absent or false, a Coding Task never receives a spawn tool or creates a worker row, even when the task prompt or plan mentions agents.
2. With the setting on, a small or non-parallelizable task never spawns workers, even after direct user instruction.
3. A heavy task with no direct delegation request waits for a separate explicit approval. Approving the plan alone does not unblock it.
4. A direct user delegation instruction plus enabled setting and a heavy plan can spawn without a second approval, with authorization recorded against the user message and current plan revision.
5. All workers in a delegation group use the exact model/provider/runtime selected for the Coding Task phase, including unified-model mode; failed calls do not switch models.
6. Enabling the setting cannot happen without displaying and acknowledging the local-model warning.
7. Workers have fixed role prompts and bounded tool access, cannot spawn more agents or invoke publishing credentials, and return typed results with file scopes.
8. Parent cancellation stops active workers; restart/recovery does not duplicate accepted file mutations; the UI shows worker state and summary.
9. When an external SDK is adopted, the compatibility suite proves the same invariants. Otherwise the built-in executor meets the same suite.
