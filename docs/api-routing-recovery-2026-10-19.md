# External API routing recovery — 2026-10-19

## Observed failure

The saved execution for `大谷翔平について教えて` selected
`knowledge.wikimedia` correctly but recorded `WIKIMEDIA_NOT_FOUND`.
Search recovery subsequently fetched source pages, but its optional Brain
composer timed out. The runtime rendered source excerpts and then incorrectly
marked the task failed, causing the outer recovery owner to discard that
answer and generate an unverified general-knowledge response.

The Wikimedia failure was reproduced against the live Japanese API. Full-text
search returned pages mentioning Ohtani rather than his article. Search snippets
also split his name with adjacent `searchmatch` HTML spans, defeating the old
literal matching logic. A direct title request returned the correct article.
This incident's recorded primary failure was not an embedding digest mismatch.

## Changes

- Resolve an exact Wikipedia title and redirects before full-text search.
  Use the resolved article's title for its source URL. If that lookup misses,
  strip HTML and decode entities before ranking search hits and try up to three
  relevant articles. Missing pages, empty extracts and malformed API envelopes
  are never reported as usable evidence.
- Keep provider-specific search hints out of Wikipedia recovery: normalize the
  original user question into the article subject.
- Once usable API or fetched-page evidence exists, a composer failure falls
  back to the existing deterministic provider renderer or fetched excerpts
  with sources. The user-facing task completes and retains the composition
  failure in diagnostic metadata. It does not switch to general knowledge.
- Preserve the original EmbeddingGemma/Laya candidate routing, deterministic
  provider selection, external-access gates, configured search provider and
  Brain fallback sequence. General inference with a disclosure remains the
  last recovery step when usable factual sources are exhausted.
- Preserve FRED's requested observation count instead of raising every query
  to 36 observations. Explicit start-year queries retain the larger coverage
  allowance. Country comparisons preserve mention order.
- Keep verified Congress list records when optional detail/action/cosponsor
  requests fail; retain field-level enrichment failures on the result.

## Verification

279 tests passed; 7 existing cases were skipped. The independent suites were:

| Suite | Result |
| --- | --- |
| External tools, extended providers, API expansion | 165 passed, 7 skipped |
| Runtime contracts, fuzzy routing matrix, acceptance manifest | 35 passed |
| Web and source recovery | 41 passed |
| Wikipedia chat routing | 33 passed |
| Wikipedia entity lookup regressions | 5 passed |

The final chat suite reused the once-loaded real pinned Laya checkpoint across
isolated database/settings resets. Model startup is covered separately by the
runtime contract suite. Both `/api/chat` and `/api/chat/stream` exercised the
real Wikimedia adapter with fixture HTTP responses and an unavailable Brain.
Regression cases also cover embedding failure, source exhaustion, external
access disabled, source preservation after model timeout, and malformed or
empty Wikimedia results.

Live read-only requests through route → argument compiler → adapter succeeded
for Wikipedia (Ohtani), Open-Meteo (Tokyo), and Frankfurter (USD/JPY). Each
returned `DATA`, provider provenance, and a valid normalized result contract.
The full 88-provider registry was checked offline; this is not a claim that
all authenticated upstream services were contacted live.

Historical assertions were updated for existing richer contracts: comparison
`requests`, preserved `request_text`, exact/reviewed UniProt search, provider
enrichment calls, current launch-update caveats, explicit SEC error ownership,
and RDAP role labels. Laya's empty-cache test now uses an isolated cache rather
than assuming the developer machine has no staged checkpoints.

The running desktop/backend process must restart to import the changed Python
modules. No running user instance was stopped or replaced during these checks.
