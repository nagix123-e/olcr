# API routing and Coding Orchestrator ingress — 2026-10-19

## Scope and baseline

This repair addresses the shared chat ingress before provider execution, using
the existing graphify routing graph, production API acceptance examples,
`docs/fuzzy-routing/plan-and-352-prompts.md`, and historical GUI records as the
baseline. The 88-provider registry, provider adapters, upstream schemas,
argument compilers, typed renderers, and EmbeddingGemma/Laya final selection
remain the existing implementations in this repair.

## Findings

1. `ChatComposer` defaulted to `queue`. While a Coding Task card was present,
   every ordinary input was posted with that task ID as an additional task
   instruction. Even selecting `chat` used this local submission branch, which
   ignored the returned assistant response instead of using the main display
   path.
2. The shared backend consumed every input when a task was waiting for plan
   approval, requirements clarification, workspace setup, or protected scope.
   The generic WAITING_FOR_USER branch also ignored an explicit ordinary-chat
   choice. An unrelated provider question could therefore become a coding
   clarification answer or receive a task approval reminder.
3. Streaming delegated to Coding Orchestrator solely because a pending task
   existed. Its wrapper labeled the result as Coding Orchestrator output even
   if the shared chat handler returned an ordinary API answer.
4. Disabling orchestration did not prevent an automatically selected old task
   from accepting new coding text as a follow-up.

## Repair

- The composer now defaults to `auto`; explicit queue, steer, and ordinary-chat
  choices remain available. Automatic and ordinary submissions use the normal
  main-chat response path. Their ownership metadata is passed through both
  HTTP and streaming payloads.
- A shared ownership predicate distinguishes a fresh provider/factual request
  from explicit task controls and instructions. It also recognizes existing
  API argument-clarification guards so missing provider parameters are not
  mistaken for coding requirements.
- An automatic task ID is a candidate context, not authority to absorb every
  message. Independent inputs bypass coding waiting handlers on both
  transports; provider selection and authorization continue in the original
  shared API pipeline.
- Explicit task selection, queue/steer choices, approvals, scope checks, and
  resume controls retain their existing authority. Automatic resume commands
  call the shared resume controller. Task status requests are read-only host
  responses rather than queued work.
- With orchestration OFF, ordinary automatic input does not attach to an old
  task. Explicit task controls are still handled by their existing controller.
- Task state is read afresh at each control boundary, preserving scheduler
  concurrency behavior rather than caching an earlier waiting state.

## Verification

The final backend batch passed 253 tests, with 7 pre-existing skips (260 total).
The composer suite passed 40 tests. Frontend type checking, production build,
and `git diff --check` passed.

The new regression matrix covers:

- All 440 existing API examples: 88 canonical examples plus 352 angle prompts.
- 280 HTTP requests: seven representative API families × ten task states ×
  orchestration ON/OFF × normal/streaming chat. Every case reaches the expected
  existing provider path and leaves the selected coding task unchanged.
- External access OFF with a pending task, explicit ordinary chat during
  requirements clarification, raw coding with orchestration OFF, read-only
  task status, and automatic resume ownership.
- Thirteen existing control regressions for approval, scope authorization,
  ineffective replan recovery, resume, plan-only requests, and image repairs.
- Existing provider/renderer suites, the full fuzzy-routing matrix and
  acceptance contracts, and both Wikipedia chat transports.

The matrix uses fixture provider results/composition to isolate ingress;
existing adapter and renderer tests run separately in the same batch. The real
pinned Laya checkpoint is loaded once and reused across isolated database and
settings resets. This is offline regression evidence, not a new live execution
of every upstream API.

Final backend log: `/private/tmp/olcr-api-ingress-regression-checked-20261019.log`.

## Running GUI and activation

A pre-existing `olcr-desktop` process (PID 80539) was observed read-only.
Repository `AGENTS.md` prohibits launching or interacting with an alternate
GUI test instance in that state. The current user instance was not stopped,
and this repair does not claim a new real-GUI E2E run. Historical GUI evidence
has not been overwritten. Restart the backend/app to import the new Python
handler; packaged frontend builds also need the updated frontend assets.
