# Purpose model switching — 2026-10-08

Implemented in Models settings, independently of Brain selections:

- Embedding: recommended EmbeddingGemma, installed Ollama embedding models, OpenAI-compatible embeddings APIs or loopback servers.
- Typed decisions: recommended multilingual Laya, Jev-compatible hosted or local servers. Clef/OpenJev alternatives must expose the Jev contract; a model name alone does not establish compatibility.
- Vision light/heavy/fallback: recommended assets, local Ollama vision models, OpenAI-compatible vision APIs or loopback servers, registered Google/Anthropic/OpenAI/NIM generic model APIs. Models without usable vision are rejected by the probe. OAuth-only Brain connections are not offered as vision endpoints.
- Japanese/English transcription: recommended LFM audio, OpenAI-compatible audio transcription APIs or loopback servers.

Local alternatives are selected from Ollama or connected through a compatible local server. Arbitrary weight-file formats are not automatically converted or run. Purpose-only API credentials can be registered inside each purpose's form. Generic Brain models are outside the auxiliary inventory.

## Switching contract

1. Probe the proposed endpoint/model through the same adapter as inference. Validate counts, dimensions, finite embedding vectors, typed decisions, vision output (including UI grounding JSON), or WAV transcription output.
2. Reserve a single-use, expiring activation token and inactive per-role Keychain slot. Only one pending switch per role is allowed, preventing concurrent Keychain slot overwrite.
3. Save a new credential using the existing stable credential-bundle bridge. Connection metadata, not API keys, goes in SQLite.
4. Activate the validated configuration. Failed compatibility/Keychain operations retain the old selection; a failed Keychain operation releases the reservation. A token cannot be replayed. Switching back to a preset also checks availability and runtime compatibility first.

Selections and the chosen credential slot survive backend restart. No new helper, signing identity, or per-build secret store is introduced. External inference obeys External Access. New remote endpoints cannot silently inherit credentials from the previous origin. Redirects are not followed.

Embedding changes propagate to API/skill retrieval, document vectors, Graphify, conversation memory, and coding knowledge. Model namespaces keep vectors separate; old indexes are not destroyed. Existing retrieval consumers snapshot their model/key so an in-flight index operation cannot mix spaces. Authorized document vectors rebuild in the background. Public reviewed coding knowledge is re-embedded in memory without modifying its release artifact. If an endpoint fails during reindexing, affected documents may need reindexing after restoring availability.

Effective selections are exposed by `/api/purpose-models/bindings`, `/api/health`, `/api/models/status`, and the Brain purpose inventory; the immutable manifest remains the recommended preset manifest.

## Acquisition

Individual downloads and recommended-preset batch downloads require the model consent dialog. Downloading does not silently select a model or change Brain. Jobs expose status/completed models and support retry after failure. HF curated assets use pinned revisions; Ollama downloads are checked against the expected manifest digest. Missing or incomplete weights are not reported as downloaded. Downloaded assets are stored in the app-support model directory, not written into the app bundle. ASR uses its established LFM runtime/download implementation.

Jev adapter protocol reference: [TypeSafe API reference](https://docs.typesafe.ai/api). Choice maps fixed candidate IDs; Score's three rubric levels are normalized to OLCR's 0–1 score; Noul provides candidate probabilities.

## Verification boundaries

Final targeted regression: 157 backend tests, 35 frontend tests, and 2 native credential-bundle tests passed. TypeScript/Vite build and the signed Tauri debug bundle succeeded. Native test execution needed the Xcode Swift runtime search path; no system libraries were changed.

Backend regression covers runtime adapters, actual loopback HTTP requests, switching consumers, startup configuration/credential-slot restoration, invalid responses, cancellation, authentication, and coding-knowledge vector separation. Frontend tests cover download consent and probe → Keychain → activation order/failure. Native tests cover the allowlisted A/B credential handles. These do not substitute for service-specific inference with a user's real endpoint, nor for actual multi-GB downloads.

The signed debug app was built. A pre-existing manual E2E app at `/private/tmp/olcr-manual-e2e-20261007-08-target/debug/bundle/macos/OLCR.app` (GUI PID 14628, backend PID 14683, port 65318) prevented a second GUI launch; it was not stopped or repurposed. Real GUI acceptance for these new controls remains unperformed in this round.
