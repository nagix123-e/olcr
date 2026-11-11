# Browser form drafts

通常の操作は[チャットからの指示](chat-personal-workflows.md)で行えます。タスク・目標・プロフィール・フォーム・ツールを起動するために設定画面へ移動する必要はありません。

The form-draft screen operates on an existing OLCR-managed browser session selected by the user. It does not open an arbitrary URL or attach to a browser outside OLCR. Open a public HTTPS page in OLCR's browser first, select its session, then enter values beside the detected field labels. Leave fields empty to keep them unchanged.

Choose **Review values**, inspect the exact page URL and each value, then choose **Fill these values only**. The review is stored locally with a five-minute expiry, bound to the session ID, the owned live Playwright MCP process PID, exact HTTPS URL and origin, document navigation timestamp, and field labels/selectors/types. Approval requires the exact original review. The browser and fields are inspected again before applying; permission revocation, navigation, reload, process replacement, expired reviews, changed values, or changed fields prevent applying that review. Each apply attempt consumes the review before mutation and cannot be replayed.

Only unique, visible, editable, explicitly labelled input/textarea fields in the top document are supported. Passwords, verification codes, bank/card/payment fields, credential contexts, hidden/disabled/read-only fields, ambiguous labels, selects, checkboxes, file uploads, iframes and shadow DOM are excluded. Login and checkout pages are excluded. Known credential/payment metadata and obvious card/key-shaped values are rejected; users must still keep credentials and payment information out of ordinary text fields.

Filling uses a fixed host-owned DOM setter. It does not send keyboard, input, change or submit events and does not click a control, call submit, or confirm a payment/reservation. Some framework-controlled sites will require the user to edit the field manually before their application recognizes its value. The site's own scripts may observe DOM values; this feature does not make an untrusted website safe. Review and finish any submission yourself on the website.

A failed apply may leave some values changed. Inspect the browser and create a fresh review instead of retrying an old approval. Pending reviews are capped at 100; expired and completed payloads are removed on subsequent form-feature activity. Form values are stored in the local OLCR SQLite database while review is pending and are cleared when consumed. They are not sent to a model or written into chat by this feature.

The routes require the normal authenticated GUI session and existing External access permission. The app's request-approval middleware applies separately in confirm mode. Marketplace-profile permission checks remain active on every managed browser operation.

API surface:

- `GET /api/browser-forms/sessions`: selectable existing public sessions.
- `GET /api/browser-forms/sessions/{session_id}/fields`: fresh safe labels and page identity, without reading current field values.
- `POST /api/browser-forms/sessions/{session_id}/prepare`: `{"values":{"Subject":"Hello"}}` creates an exact-value review.
- `POST /api/browser-forms/reviews/{id}/confirm`: `{"approve":true,"review":<original review>}` fills the reviewed values. `approve:false` cancels it.

No endpoint accepts JavaScript, click actions, submission actions, payment details or a replacement target URL.
