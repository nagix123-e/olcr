# Clear submitted text immediately

The command branch, including `/skill`, cleared the composer only after `/commands` returned. While a Skill was generating, the submitted prompt therefore remained in the textarea and kept the send arrow visible. The late clear could also erase text typed for the next request.

The shared `App.send` path now clears the originating session's text immediately after accepting the send, before awaiting conversation creation or any command/chat request. Branch-specific and response-time clears are removed. The captured outgoing request still contains the original text. A failed request restores that text only if the composer is empty, preserving any later draft and keeping updates scoped to their originating conversation. Cancelling an accepted send leaves the submitted text cleared.

Actual-App tests with delayed responses verify immediate clearing for `/skill`, retention of subsequent typing through success/failure, retry recovery into an empty composer, and clearing during new-conversation creation without losing subsequent typing after session transfer. Existing conversation-isolation, agent-builder retry, composer and API cancellation tests pass. The frontend production build passes.

The debug macOS bundle is updated with build ID `olcr-clear-submitted-draft-20261028`. A running user instance is not terminated or relaunched; reopening the updated app applies the change.
