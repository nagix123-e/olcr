# ADR: Runtime for Coding Task subagents

- Status: Accepted
- Date: 2026-11-19
- Decision owner: OLCR Coding Tasks

## Decision

Build the v1 `CodingSubagentExecutor` on OLCR's existing model/runtime boundary. Do not add OpenAI Agents SDK, LangGraph, or a hosted multi-agent service as a production dependency for this feature.

Keep the executor behind a small OLCR-owned interface. OLCR resolves and snapshots the active Coding Task implementation model, constructs registered role instructions, supplies only bounded input and tools, persists worker lifecycle, enforces the user authorization gate, validates typed file proposals, and applies accepted changes through the existing workspace boundary.

```python
async def run(request: CodingSubagentRequest,
              cancellation: CancellationToken) -> CodingSubagentResult: ...
```

The request contains a host-created role ID and prompt version, the immutable phase model binding, bounded task context, assigned paths, and enumerated actions. It does not accept a model override, arbitrary role prompt, nested spawn request, or raw workspace writer. The result contains a bounded summary, typed findings/proposals, usage metadata, and a stable error category. Worker model calls can run in the separately bounded worker lane; only the host applies proposals, serially.

## Context

Coding Tasks already resolve a phase model before invoking implementation work. `_coding_phase_model` selects the coding role, `BrainModels.resolve` maps the role through the conversation's split/unified selection, and the runtime accepts a model name plus an optional JSON schema. Coding Task state and workspace mutation already have OLCR-owned persistence and safety boundaries. The subagent feature must preserve those authorities and use the same selected model for every child.

The feature also has gates outside the model runtime: the separate setting must be on, the approved plan must be heavy and parallelizable, and a user directive or task-specific approval must authorize delegation. A framework's tool approval or agent-to-agent handoff cannot create that authorization.

## Candidates

| Candidate | Useful behavior | Fit for OLCR v1 |
|---|---|---|
| Existing OLCR runtime behind an executor port | Preserves existing Brain provider/model resolution, structured response schema, usage accounting, task persistence, and workspace controls. No extra runtime dependency. | **Selected.** OLCR can keep one model authority and add only the worker-specific coordination and typed result contract. |
| OpenAI Agents SDK | Fixed `Agent.instructions`, `Agent.as_tool()`, structured outputs, a per-run `ModelProvider`, tool approval interruptions, resumable `RunState`, and streaming cancellation. | Rejected for v1. Routing every supported OLCR model through it would still require a provider/model adapter and capability checks. Its per-tool approval mechanism does not replace OLCR's task-level authorization. Its sessions/run state would overlap OLCR's durable task state, and cancellation requires draining the stream and separately supervising side-effecting tool cleanup. |
| LangGraph | Subgraphs provide composable child graphs with state, and interrupts support pause/resume with a checkpointer. | Rejected for v1. The task already persists plans, phases, approvals, cancellation, and recovery. Introducing a second graph/checkpoint authority would increase reconciliation work without improving model identity or workspace enforcement. Reconsider if OLCR replaces its existing task graph. |
| Hosted Responses API multi-agent / Codex agents | Closest behavioral reference for spawn, message, follow-up, wait, interrupt, and list coordination. | Reference only. A hosted OpenAI path cannot be the default when the selected Coding Task model is local or belongs to another provider. It does not replace OLCR's task and workspace controls. |
| OpenHands Software Agent SDK | A fuller coding-agent runtime with its own agent/tool loop. | Not an incremental executor. It would expand the change into replacing or adapting workspace and coding lifecycle boundaries that this feature must preserve. |

## Evidence and limitations

The official Agents SDK documentation confirms that a `RunConfig` can carry a per-run `ModelProvider`, while model/provider feature support differs. `Agent.as_tool()` supports nested agents and typed tool inputs/outputs. Human approval appears as run interruptions that can be serialized and resumed. `RunResultStreaming.cancel()` exists, but the stream must be drained for cleanup; cancellation does not undo effects that already occurred. These are useful capabilities, but they do not satisfy OLCR's host-side user approval, persistence, and workspace requirements by themselves.

The official LangGraph documentation describes subgraph composition and checkpointer-backed interrupts. This is technically capable, but duplicates persistent state OLCR already owns. The hosted multi-agent guide is a separate Responses API surface; its current HTTP examples require beta SDK builds and an explicit beta feature header. It must not be treated as local Codex's built-in subagent feature or as a provider-neutral runtime.

The current checkout does not have the `agents` or `langgraph` Python packages installed, and network-restricted package installation is not part of this change. Therefore the SDK APIs above were assessed from current official documentation, not from an installed-package execution spike. The production decision is deliberately conservative: no dependency is adopted until a later compatibility suite can prove exact model/provider/runtime pinning, tool-surface restrictions, structured output, cancellation, and no duplicate task state. This evidence gap is a deferred verification item, not a claim that an Agents SDK adapter was executed.

## OLCR implementation contract

1. Resolve the exact implementation model/provider/runtime before any worker call. Unified Brain mode resolves one selection and all children receive that binding. Never fall back to another model.
2. Keep worker role prompts and prompt versions server-owned. Child requests reject unknown roles, model overrides, arbitrary instructions, nested delegation, and unregistered tools.
3. Treat model-generated plans, SDK/tool approvals, and worker output as data. Only an actual user message or a separate task-scoped user action can satisfy the host authorization rule.
4. Read-only workers may run concurrently within the fixed cap. Implementation workers return file-scoped, typed proposals with expected preimages. Host code validates current revision, approved scope, path ownership, and preimages, then applies each accepted proposal serially.
5. Parent pause/cancel stops queued and active workers. Restart recovery must not replay an already-applied proposal. Child records and summaries remain separate from the existing single-running-phase progress field.
6. Workers cannot commit, publish, deploy, access release credentials, write directly to the shared workspace, or spawn grandchildren.
7. When executor setup fails, report the reason and continue through the existing single-agent path using the already-resolved parent model; never silently substitute a provider or model.

## Revisit criteria

Reconsider an external runtime only when OLCR needs multi-turn tool orchestration that its model/runtime boundary cannot support cleanly. Any candidate must pass the same permanent compatibility suite without weakening the gates above, and must not become a second source of truth for task state, model selection, or workspace mutations.

## References

- [OpenAI Agents SDK: Models and per-run providers](https://openai.github.io/openai-agents-python/models/)
- [OpenAI Agents SDK: Agents as tools](https://openai.github.io/openai-agents-python/tools/)
- [OpenAI Agents SDK: Results and streaming cancellation](https://openai.github.io/openai-agents-python/results/)
- [OpenAI Agents SDK: Streaming lifecycle and cancellation](https://openai.github.io/openai-agents-python/streaming/)
- [OpenAI Agents SDK: Human approval and resumable interruptions](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- [LangGraph: Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [LangGraph: Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [OpenAI API: Responses multi-agent guide](https://developers.openai.com/api/docs/guides/responses-multi-agent)
- [Codex: Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- OLCR code boundaries: `backend/olcr_api/app.py` (`_coding_phase_model`), `backend/olcr_api/brain_models.py` (`BrainModels.resolve`), `backend/olcr_api/runtime.py` (`Runtime._generate_brain`), `backend/olcr_api/coding_tasks.py`, and `backend/olcr_api/task_workspace.py`.
