# Conversation memory settings

Permissions and settings → Conversation memory provides three independent controls:

- **Use past conversations** (`conversation_memory_enabled`, default `true`) controls retrieval into new answers. Turning it off retains conversation history and its search index; completed turns continue to be processed for later use.
- **Across-project memory** (`conversation_memory_cross_project_enabled`, default `false`) permits retrieval from all projects and conversations without a project. With this off, retrieval stays in the current project. A conversation without a project can retrieve only other conversations without a project. The active conversation is always excluded. Switching this off never deletes memory.
- **Conversation memory relevance threshold** (`conversation_memory_min_relevance`, default `0.55`) accepts a finite number from `0` through `1`. Higher values require a closer match. This floor applies to both cosine similarity and the keyword fallback used when embedding requests fail. Their scores are different matching measures, so the same number is not an answer-confidence estimate. This setting is separate from `rag_min_relevance` for other retrieved context.

Updates apply to the next request and are saved in the local application settings database. Changing these controls does not require reindexing. Changing project assignment changes eligibility immediately because retrieval uses the live conversation relationship.

OLCR keeps the original transcript and indexes short excerpts from up to eight turns together. Short conversations become eligible after a day without updates. Search ranks these summaries and up to 32 recent, uncompressed turns. If the first pass finds no suitable evidence, or the request asks for exact wording, OLCR checks the original turns of up to two shortlisted summaries. Historical summaries are built incrementally in the background after chat; an existing profile can take time to catch up. The number of summaries to rank still grows with the size of the archive.

An edit invalidates the affected summary and any later summaries in that conversation. Deleting a conversation or project removes its derived memory along with its transcript.

`GET /api/settings` returns these fields. `PATCH /api/settings/memory` accepts any nonempty subset of the three fields above and rejects unrelated fields. Boolean settings require JSON booleans; threshold values must be numeric. The existing full settings API also accepts the fields. Storage/deletion continues to follow the existing conversation deletion controls.

Optional process defaults use `OLCR_CONVERSATION_MEMORY_ENABLED`, `OLCR_CONVERSATION_MEMORY_CROSS_PROJECT_ENABLED`, and `OLCR_CONVERSATION_MEMORY_MIN_RELEVANCE`. Persisted settings override these defaults.

Cross-project retrieval does not override current-web or image-request isolation. Memory remains contextual evidence without instruction authority.
