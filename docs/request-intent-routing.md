# Request ownership

`olcr_api.request_intent.resolve_request_intent` is the shared authority for
software work, clarification, domain operations and ordinary requests. It uses
the deterministic coding classifier; it does not infer authorization from model
output, retrieved documents, Skill bodies, or conversation history.

- Chat and streaming resolve ownership before domain, conversion, agent and
  scraping routers. The scoped classification is refined after attachment and
  workspace ingestion using the same authority.
- A software request mentioning YouTube, calendars, CSV conversion, APIs or
  scraping remains software work. Actual service commands retain their typed
  executors. Unknown software actions are clarified rather than searched.
- `/skill` is an instruction envelope. Coding goals go through the managed task
  path; the validated selected Skill is saved with the task and passed to both
  planning and implementation. Command responses publish the task ID to the UI.
- Runtime uses the same mutation authority. Its generic Web branch cannot own
  a coding answer; local retrieval may supply evidence but cannot return a
  search-only terminal response for software work. Authorized managed coding
  research remains auxiliary to implementation.
- Turning orchestration off still forbids managed task creation and implicit
  file execution. It does not reclassify software work as an information search.

Tests cover paired software/service requests, Japanese/English, research plus
implementation, ambiguous scopes, chat/stream/commands, pinned Skills, and
unmanaged Runtime lookup fallthrough. These are routing guarantees; they do not
prove an arbitrary model can finish every requested project.
