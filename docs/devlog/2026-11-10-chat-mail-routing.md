# Natural mail request captured by calendar help

## Reproduction

Using an example.org address, the screenshot's natural request reproduced `operation_help.requested=True`, `help_topics=['calendar']`, `connector_chat.intent=None`, `action_intent=None`. The guide interpreted the email topic's 「予定」 and 「どうする」 as a question about OLCR. The old connector route recognized only provider-prefixed reads and explicit JSON sends.

## Change

The persisted workflow hub now owns explicit natural mail composition/send requests before travel/help/calendar paths. `mail_chat.py` separates explicit recipients/provider from generated subject/body. Review contains the provider, native default-sender limitation, exact recipients, subject and disclosed body. Existing draft IDs, conversation binding, expiry and durable single-use approval gate `ServiceConnectors.execute`. Permissions are rechecked by that service, without implicit grants or fallback providers. A failed/uncertain send is never retried automatically or reported as sent.

Missing recipient/provider is clarified in the conversation. Content edits containing addresses cannot change recipients. Provider names inside email content cannot override an explicitly selected service. A body question using 「どうすれば」 retains mail ownership. Unsupported explicit sender, CC/BCC, attachments and scheduled delivery collect clarification rather than silently dropping those instructions.

## Evidence

- Watched the original seven workflow regressions, both API transports, and mail card test fail before implementation.
- Full real AppleMailConnector validation against a fake native runner exposed empty optional CC/BCC payload incompatibility; removed empty optional fields and verified exact payload/footer without sending.
- Read-only review identified five edge cases. Added reproductions and corrected provider selection, recipient edits, header forms, body questions and `.cc` domains.
- Latest focused backend suite: 83 tests passed. Frontend: 21 passed; production build passed. No real mail sent.
- Local `ornith-1.5:9b` generated a structured review for the screenshot-shaped request using an isolated database/fake transport, preserving topic and recipient and making no send call.
- A broader operation-help suite also reveals pre-existing `mcp-check` Graphify lookups returning no results (2 failures/3 subtest errors). Disabling the added mail guard in memory still returns zero hits; no bundled guide/index files were changed by this fix.

## Activation

User quit OLCR and explicitly requested the latest-source launch. `scripts/start-dev-independent.sh` built/signed the unchanged source and launched build `olcr-dev-independent-20261110-200354`. Exact app PID 33305 owned backend PID 33320, listening on loopback port 62591; health confirmed the expected build and user app-support profile. No app was terminated by this agent.

Live authenticated `/api/chat/stream` verification in a newly created temporary conversation used an example.org recipient and the screenshot-shaped request. The actual configured normal-chat model produced `ASSISTANT_WORKFLOW`, domain `mail`, status `review`, preserving the recipient and Kyoto topic. No confirmation was submitted and no real mail was sent. The temporary conversation was deleted after verification. No GUI automation was used. Final focused verification: backend 83, frontend 21 passed; build passed.

## Interrupted mail confirmation and misleading completion

The screenshot-shaped failure was consistent with a consumed approval whose external-call result was never durably recorded. A retry could then fall through to the generic duplicate-confirmation response, whose missing status defaulted to `completed`. Also, the Apple Mail provider call could outlast the client stream, leaving no live status in the chat.

Mail approvals now retain an outcome (`in_progress`, `accepted`, `sent`, `not_sent`, or `uncertain`) and the provider result is persisted together with the assistant response even if the client disconnects after the send call. An interrupted or uncertain send is shown as an error that asks the user to check Sent/Outbox; OLCR does not automatically retry. Apple Mail's successful automation handoff is labeled `送信依頼済み` / `Request accepted`, not `完了`, because Apple Mail accepting the request does not prove delivery. Timeout errors now identify the timeout and show where to check.

The mail streaming route sends user-visible operational status before waiting for draft generation or Apple Mail. These status entries use OLCR's existing log panel and do not expose hidden model reasoning. The stream emits one metadata frame and then the final response/card.

Focused verification: 69 backend tests passed, 9 `AssistantWorkflowCard` tests passed, and the frontend production build passed. No real mail was sent. The current user app has not been relaunched with this fix.
