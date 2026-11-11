# Brain fallback order — 2026-10-19

## Contract

Each generation starts with the model currently selected for its active role:

| Invocation role | Split-mode selection |
| --- | --- |
| normal_chat | coder |
| long_implementation | coder |
| short_coding_fix_debug | short_coding |
| planner_replanner | general_inference |

Unified mode starts with selections.brain. The initial model and fallback list
are captured from the same request/conversation configuration. After failure,
visit fallback_models in its stored order, excluding the already attempted
initial model and duplicate candidates. Other roles' selected models remain
valid fallback candidates. No implicit insertion of another role's model occurs
inside this chain. The legacy explicit custom Ollama chat setting starts with
that explicit model and then uses the same configured chain.

## Repairs

- Provider errors, empty/invalid responses, provenance mismatches and interrupted
  streams now reach the same fallback coordinator.
- With fallbacks configured, stream attempts are buffered until completion, so
  failed partial output cannot be mixed into the next model's answer. Without
  configured fallbacks, successful stream frames continue to be emitted live.
- Fallback metadata identifies the actual starting model and successful candidate.
- Settings permit a model selected for another role in the shared sequence.
- Split-mode graph displays “Current model selected for the active role” instead
  of displaying the unused unified Brain selection as a fixed starting point.
- Auxiliary embedding, routing and perception bindings retain their own contracts.

## Verification

- Backend: 43 passing tests across test_brain_fallback_order, test_brain_models,
  test_conversation_concurrency and test_brain_providers.
- Frontend: 5 passing tests across ModelsPanel and PurposeModelsPanel.
- Frontend TypeScript typecheck: passed.
- Provider calls are fixtures/mocks; this is not a live API or native GUI E2E result.
- Existing running OLCR instances and user settings were not changed.

Backend test output: /private/tmp/olcr-fallback-order-tests.log
