# Quick mode

The lightning button is shown only when the backend catalog reports `quick_supported`.
Quick is saved per canonical model ID in the conversation's Brain configuration,
shared by roles selecting the same model, and captured by the existing request scope.
A fallback uses its own saved model preference and checks its actual account endpoint.
Existing configurations default to Quick off. Effort remains independent.

First-party OpenAI endpoints use `service_tier: fast` when enabled and `default`
when disabled (including when a provider project defaults to Fast).
First-party Anthropic endpoints use `speed: fast`; preview models also receive
`anthropic-beta: fast-mode-2026-02-01`. Unknown models, proxies, local models and
other providers do not receive these parameters or show the button.
The capability allowlist lives in `brain_providers.quick_profile` and reflects
verified documentation as of 2026-10-28. Account entitlement and runtime capacity
still apply; the button represents the requested processing mode, not a guaranteed
speed multiplier. The tooltip discloses potentially higher usage/cost.

Sources:
- https://developers.openai.com/api/docs/guides/fast-mode
- https://learn.chatgpt.com/docs/agent-configuration/speed
- https://platform.claude.com/docs/en/build-with-claude/fast-mode

Verification uses temporary databases and mocked provider requests; no paid
account inference is performed.
