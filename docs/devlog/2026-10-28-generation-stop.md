# Stop interrupts the active answer generation

The composer Stop button previously posted a task cancellation flag. The backend checked that flag only after the provider yielded another token. Buffered API/MLX requests and routes doing work before stream metadata could therefore keep the UI busy and continue inference after Stop.

Each chat/command request now has a unique generation ID before any response arrives. Stop cancels the conversation-owned frontend fetch/read immediately and posts that ID to `/api/generations/{id}/cancel`. Request registration handles a Stop POST arriving before the original request, and HTTP disconnect also signals cancellation. The endpoint is idempotent and does not require an operation approval. The new header is allowed by desktop CORS. Other conversations retain independent cancellation state.

The request scope propagates to generation providers. Ollama and compatible API/ChatGPT requests interrupt their HTTP socket while awaiting headers or tokens; cancellation is not retried as a fallback-model failure. Request-owned MLX subprocesses receive termination and a bounded kill fallback, without terminating another request or the persistent vision worker. Custom-agent cancellation uses the same request event. Buffered runtime execution returns a cancelled task without an error answer; operation application checks cancellation before writing further files.

The composer Stop control is a `button`, avoiding an additional empty form submission. Partial streamed text is retained in the UI and persisted with the cancelled task. Buffered cancelled replies are ignored instead of being displayed as errors or command metadata. The input is released without waiting for provider cleanup.

Validation uses disposable app-support/DB profiles. New tests exercise a click on the actual composer control, stopping before metadata/HTTP headers, stopping during a blocked read, buffered chat and command replies, conversation isolation, valid CORS preflight, partial-answer persistence, no fallback after cancellation, and termination of an owned subprocess. Local stalled HTTP servers verify that Ollama, ChatGPT Responses and JSON API connections actually close, both before and after response headers. Related API, model-role, custom-agent and runtime tests pass; the frontend build passes.

Additional provider regression tests expose four existing structured-output expectation failures. They also reproduce with the new cancellation middleware bypassed in a control run. A separate model-selection test requires local Ollama catalog access; it passes with that loopback access enabled (generation remains mocked).

The debug bundle is rebuilt with build ID `olcr-generation-stop-20261028`. The running user app is not terminated or relaunched; reopening the rebuilt app applies the update.
