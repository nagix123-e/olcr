# Thinking mode and received logs

Contract fixed before implementation (2026-10-28). This is a specification-first
extension, not a claim of legally independent clean-room development: the same
maintainer must inspect the existing integration boundaries.

- Existing conversation Brain `efforts` selects local `off | auto | on` per role.
  The existing effort slider maps Zero / middle / MAX to off / auto / on. There
  is no separate mode control or parallel configuration field. Null in existing
  profiles means auto. Switching takes effect on the next invocation;
  in-flight generation and the configured fallback sequence keep their snapshot.
- This controls local text models. External models retain their model-specific
  effort settings. Unsupported controls must never be represented as effective.
- Structured output and explicit no-thinking internal calls take priority over
  the user mode. No second planning/completion call is introduced. Auto chooses
  thinking for planning/complex analysis and skips it for routine prose.
- Model output has independent answer and reasoning channels. Only a leading
  model framing block is parsed; tags inside code/JSON remain data. Truncated
  reasoning must not become an answer or executable JSON.
- Capture only text already present in an ordinary provider response. Never add
  a summary prompt, `includeThoughts`, reasoning-summary request, or retry to
  obtain a log. Never decode opaque/encrypted reasoning or signatures.
- Logs are bounded to 64,000 characters per call, plainly marked if truncated
  or incomplete, and never fed back into conversation context or execution.
- UI shows a separate, initially collapsed “Thinking log” disclosure above the
  answer. Provider summaries are labelled as summaries. Expanding it uses no
  model call. Answer copy/read-aloud excludes logs. No empty disclosure.
- Native fields supported: Ollama `message.thinking`, MLX template think frame,
  compatible API `reasoning_content`, Anthropic thinking blocks, Gemini thought
  parts, and already-delivered Responses summary events. No promise that a
  provider supplies a log, or that it describes all internal computation.

Public protocol references (implementation is original OLCR code):

- https://docs.ollama.com/capabilities/thinking
- https://developers.openai.com/api/docs/guides/reasoning
- https://developers.openai.com/api/reference/resources/responses/streaming-events
- https://learn.chatgpt.com/docs/app-server (reasoning items, summary/text deltas,
  and final authoritative item state)
- https://ai.google.dev/gemini-api/docs/generate-content/thinking
- https://platform.claude.com/docs/en/build-with-claude/extended-thinking

The answer/summary event separation is the reference. An expandable disclosure
is OLCR's UI choice, not a claim that proprietary ChatGPT UI is specified here.
