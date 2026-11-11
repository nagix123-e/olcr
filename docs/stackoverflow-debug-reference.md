# Stack Overflow evidence in coding repair

Managed coding now uses Stack Overflow public questions/answers as optional bug-repair evidence, not as a general chat API or a new mutation tool.

## Entry points

- Bug-fix planning and replanning (`QWEN_PLANNING`, `QWEN_REPLANNING`).
- Approved implementation phases, including repair retries. Previous structured review/verifier diagnostics supply error signals.
- Read-only verification-only execution and JSON/schema repair do not trigger this lookup.

The original goal, phase goal and available diagnostics are inspected locally. Only allowlisted framework names, common exception names/error phrases and compiler error codes form the outbound query. Full source code, raw tracebacks, private project identifiers, paths, URLs, email addresses and credentials are not sent. A normal feature implementation or a bug with no safe technical signal does not issue a request.

External Access must be enabled. `coding_stackoverflow_enabled` defaults to true and is persisted through Settings; it can be turned off via settings PATCH or `OLCR_CODING_STACKOVERFLOW_ENABLED=false`. Task-specific offline/no-network constraints are also respected. No settings are automatically enabled by this feature.

## Retrieval and use

Read-only anonymous GET requests use the [Stack Exchange v2.3 API](https://api.stackexchange.com/docs), with `site=stackoverflow`. This implementation does not require or read a user API key and does not register/post/edit questions or answers. Provider anonymous-access quotas still apply.

[Advanced search](https://api.stackexchange.com/docs/advanced-search) uses relevance and technical tags. Candidate answers are fetched in one [questions/answers batch](https://api.stackexchange.com/docs/answers-on-questions), preferring an accepted answer among retrieved answers, then score. A bounded repair context contains up to three question/answer pairs with title, excerpts, URLs, author/profile, accepted flag, score, activity date and content license. This is a repair-context budget, not a cap on other knowledge API output.

Reference text is marked untrusted and JSON-escaped. The model must check stack/version compatibility, cite sources/authors when using ideas, and verify changes locally. Posts cannot expand approved paths, authorize dependency installation or commands, override the task, or prove a successful repair. The existing recovery limits and verifier authority are unchanged.

GZIP/DEFLATE responses are decoded with response-size limits; redirects are refused. Results are cached for ten minutes, including empty results; failures are briefly cached. [Backoff and quota signals](https://api.stackexchange.com/docs/throttle) suppress subsequent requests without blocking sleeps. A timeout, malformed response, exhausted quota or missing evidence merely omits Stack Overflow context; local repair continues. Logs contain status/reference-count/cache-hit only, not raw queries or logs.

## Acceptance

Deterministic tests cover Japanese/English technical query extraction, privacy, GET and compressed responses, accepted-answer selection, source metadata, cache/backoff/quota handling, failure fallback, untrusted-context boundaries, global/task-specific network gates, and integration with planner invocation. Tests do not contact the live Stack Overflow API or use real credentials. Real provider/GUI acceptance is not claimed.
