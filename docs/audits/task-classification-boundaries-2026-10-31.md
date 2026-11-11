# Task classification boundaries, 2026-10-31

Trigger: `ママレモンについて調べて` was routed to PubChem and returned a compound-name clarification.

## Findings and changes

- Open-topic research entered a closed catalog selector. Added a shared router admission rule for simple general research/overview requests without a deterministic specialist match or structured-data intent. These abstain to the existing knowledge policy before embedding/selection.
- Embedding similarity could override a completed Laya choice. Removed that override. Emergency selection requires structured-data intent, score >= .30 and margin >= .05. Missing/failed selectors otherwise abstain.
- Failed argument compilation for a model-suggested API was treated as proof that the user needed that API. Missing fields now trigger clarification only when a deterministic route or explicitly named provider independently grounds the selection; ungrounded selections continue through knowledge fallback.
- `APIで確認して` could be treated as underspecified software work. Read-only confirmation/retrieval wording is recognized by request ownership (explicit coding mutation still has precedence).
- Streaming checked managed coding ownership before retrieval-only requests. Moved retrieval-only dispatch first, matching the non-streaming path and retaining its response owner.

## Validation and limits

Regression coverage includes product/person/concept research in Japanese and English; adversarial confident PubChem selection; source-backed product answers through normal and streaming chat; existing specialist, coding, retrieval-only and provider boundaries. Tests use isolated app support and mocked external providers; no live provider request or user task execution. This change does not guarantee every natural-language intent can be classified without uncertainty. Ambiguous specialist suggestions abstain to the existing knowledge policy. Running desktop requires restart to load changed Python code.
