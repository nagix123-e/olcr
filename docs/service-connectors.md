# Personal service connections

Open Settings → Service connections. The dedicated page groups icon tiles under Mail, Calendar, Files, and Shopping. Mac Calendar is managed in this same page using its existing macOS permission and connection state. A category toggle grants that whole category's supported operations; each service tile has its own connection on/off switch and connection state. For Apple Calendar, both the macOS connection and Calendar category permission must be on; category OFF also blocks prepared actions at confirmation time. Apple Mail uses accounts already configured in Mail.app through Apple Events. The first connection explicitly asks macOS to let OLCR control Mail; the category grant and per-service switch are both required for reading or sending. Turning the switch off disables OLCR's use, while the macOS Automation grant can be revoked separately in System Settings. Google and Shopify marks load from their official asset hosts while external access is on; Apple Calendar and Mail use the icons installed on this Mac. Local symbols remain available when external access is off. The existing switch design is used throughout, and the back button is shared with other Settings pages. New connections and categories start OFF. Connecting an account does not enable it.

Apple Mail connection actively calls `AEDeterminePermissionToAutomateTarget` with `askUserIfNeeded=true`; opening the settings page only probes with `false`. Connect starts Mail if needed. Inventory distinguishes unrequested consent (`permission_required`), a denied grant (`permission_denied`), Mail not running (`not_running`), and an unexpected OS probe failure (`permission_error`). A denied grant requires enabling Mail under **System Settings → Privacy & Security → Automation → OLCR**; reconnect does not reset macOS consent. Turning OLCR's switch off retains the OS grant.

Google Drive remains read-only. Google Calendar retains read access and can add confirmed medication events to owned calendars after reauthorization for `calendar.events.owned`; see [Medication management](MEDICATION_MANAGEMENT.md). Gmail and Apple Mail support reading plus plain-text sending. Apple Mail `list` returns up to 50 Inbox summaries in Mail.app order, and `get` reads one message body up to 200,000 characters. Apple Mail `list` query matching is local and searches sender/subject fields among those summaries. OLCR does not read Mail's on-disk database or store account credentials. Every OLCR-sent message has `このメールはAIエージェントによって送信されています。` appended after its body by the backend. Attachments and drafts are not supported.

The Mail category toggle grants both reading and sending. Existing read-only Mail category grants reset to OFF once on upgrade, requiring renewed consent; the individual provider switches are unchanged. Gmail accounts with older read-only OAuth grants keep inbox access but must reauthorize to grant the additional `gmail.send` scope before sending. Apple Mail uses the existing macOS Automation permission. Google requires a Google Cloud **Desktop OAuth client** and the corresponding service API to be enabled. An OLCR release can include its public client ID by setting `OLCR_CONNECTOR_GOOGLE_CLIENT_ID` when building the desktop app; with that configured, each service tile starts Google sign-in directly and returns to OLCR automatically. The ID is reused after the first connection. If the app has no client ID, the settings page keeps a one-time client-ID setup field and a link to Google Cloud. Desktop clients are public clients, so the UI does not ask users for a client secret. Storefront shopping requires a merchant-issued Shopify **public Storefront API token** and the merchant's `name.myshopify.com` domain.

Dedicated Keychain handles store connector bundles; refreshed Google credentials are synchronized privately to Keychain without entering chat content. Existing Brain/model credentials, knowledge API credentials, MCP/coding permissions, and model selections are unchanged. Disconnect disables the connector persistently, so a previously saved Keychain bundle is ignored across restart until a new explicit connection. An in-flight request may finish after OFF; subsequent transport steps recheck the saved policy and are denied.

## Use from chat

`/connectors` opens the existing permissions/settings card.

```text
/service service.google.gmail list {"query":"from:person@example.com","limit":10}
/service service.google.gmail get {"id":"message-id-from-list"}
/service service.google.gmail send {"to":"person@example.com","subject":"Meeting follow-up","body":"Thanks for meeting today."}
/service service.apple.mail list {"query":"travel","limit":10}
/service service.apple.mail get {"id":"12345"}
/service service.apple.mail send {"to":"person@example.com","subject":"Meeting follow-up","body":"Thanks for meeting today."}
/service service.google.calendar list {}
/service service.google.calendar get {"id":"event-id"}
/service service.google.drive list {"query":"project notes","limit":10}
/service service.google.drive get {"id":"file-id-from-list"}
/service service.commerce.checkout search {"query":"coffee","limit":10}
/service service.commerce.checkout cart {"lines":[{"variant_id":"gid://shopify/ProductVariant/123456789","quantity":1}]}
```

Explicit read requests such as `Gmailのメール一覧を表示して` and `Google Driveで「project notes」を検索して` also route to these connections. Other calendar commands keep the existing Mac Calendar behavior. Service responses are displayed in the conversation; Gmail list supplies message identifiers for the `get` action. Drive reads Google Docs via plain-text export and bounded text/JSON/XML files; unsupported binaries are reported rather than decoded as text.

## Shopping and checkout

The Shopping category toggle grants product search, cart creation, and checkout review together. With the Shopify service also enabled, a cart response produces the existing-style order review card. Shopify totals are estimates: shipping/tax are shown as determined at the merchant checkout, never invented as zero. Confirmation is bound to the exact reviewed order, expires after five minutes, and is consumed once. Both category and service revocation are checked again before confirmation.

Approval enables an explicit **Open merchant checkout** button. Payment and final order submission happen on the merchant website, using the merchant's payment system. OLCR does not submit a card, automatically place a paid order, retry an uncertain charge, or claim that opening checkout means a purchase succeeded.

This is a merchant-configured Shopify connection and a checkout handoff, not universal automated purchasing across arbitrary merchants. No card storage or verified order-history integration is included.

## Verification and boundaries

The new connector routes use `/api/connectors/...`, dedicated category/connection/review tables, and namespaced IDs. They are not registered as Brain providers, knowledge tools, or coding MCP servers. The existing global external-access capability retains its current permission-level semantics; both category and connector OFF are always enforced, including full-access mode. Existing partial action grants require a new category grant rather than silently gaining additional permissions during migration.

Custom Agents can be given the `email_send` tool. It uses the same category/provider authorization and disclosure enforcement as `/service ... send`; the model instructions say to send only when the user explicitly requests it and to ask when recipients are unclear. Live Google authentication, macOS Automation consent, and mailbox delivery require the user's real accounts and were not exercised during implementation.

## Japan consumer marketplaces

Shopping now includes Amazon.co.jp, 楽天市場, Yahoo!ショッピング, au PAY マーケット, Qoo10 and メルカリ. Enable Shopping and the individual service, then choose **OLCRブラウザーでログイン / Sign in with OLCR browser**. The app opens the provider's fixed official home in a dedicated local Chromium profile (`market-amazon`, etc.); finish sign-in yourself in that window. Passwords and cookies are not collected into the chat or connector credential fields. The profile persists on this Mac. A successfully opened browser is **sign_in_unverified**, never evidence of an authenticated consumer account. Provider MFA, CAPTCHA, passkeys and automation restrictions can still prevent sign-in.

Turning off a marketplace or Shopping in this settings page closes its open OLCR browser session and retains the profile. **Disconnect service** disables access and closes the session; **Forget browser sign-in** closes it and deletes the local profile. Forgetting a profile does not revoke an account session on the provider's other devices. Category/service permissions continue to gate connector operations, including after a saved profile exists.

Supported connector actions are `search`, `account` (official home handoff), and `product` (canonical official product URL only). Without developer credentials `search` returns an encoded official search link; it does not scrape search results or prove that the user is signed in. `product` rejects arbitrary domains, account/checkout URLs, URL credentials, query parameters and fragments. Search/account handoffs are links the user chooses to open; they do not submit a cart or an order.

```text
/service service.commerce.amazon search {"query":"USB充電器"}
/service service.commerce.rakuten search {"query":"コーヒー"}
/service service.commerce.yahoo search {"query":"本","limit":10}
/service service.commerce.aupay account {}
/service service.commerce.qoo10 account {}
/service service.commerce.mercari product {"url":"https://jp.mercari.com/item/m123456789"}
```

Named searches with a quoted keyword also work, for example `楽天市場で「充電器」を検索して`.

Yahoo supports optional **developer** search configuration: a Yahoo! JAPAN application Client ID with Shopping API access, stored in the dedicated `OLCR_CONNECTOR_YAHOO_SHOPPING` Keychain slot as `{"application_id":"..."}`. This calls the fixed official read-only v3 search endpoint. Its state is `developer_api_ready`, with `account_connected: false`; it grants no consumer account, order-history, cart, or payment access. API product links are restricted to canonical Yahoo storefront product pages. Redirects are disabled, response size/time are bounded, and permission is rechecked immediately before transmitting the developer credential. Result prices are JPY estimates; final stock, shipping, discounts, tax and total are determined on the provider's site. No credential is included in inventory or result data. [Yahoo! Shopping Item Search v3](https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html).

The current release does not integrate consumer cart/order/purchase APIs for these six providers. It does not claim payment success, and no marketplace action accepts payment details. Shopify's existing exact-order review, expiry, single-use confirmation and final merchant-checkout handoff remain separate. A browser profile or developer search key never bypasses the purchase review mechanism.

Why account sign-in and developer product APIs are separate: Amazon's documented Login with Amazon scopes provide customer profile/postal-code information, while its Creators API provides product content to Associates; neither is wired here as a consumer purchasing integration. [Login with Amazon scopes](https://developer.amazon.com/docs/login-with-amazon/customer-profile.html), [Creators API introduction](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction). Rakuten's current official explorer identifies the 2026-07-01 search endpoint and application/access keys; its main contract was unavailable during implementation, so Rakuten API search is deferred rather than relying on retired API versions or claiming an account connection. [Rakuten official explorer](https://webservice.rakuten.co.jp/explorer/api/IchibaItem/Search), [Rakuten API help](https://webservice.faq.rakuten.net/hc/en-us/sections/25589752328217-Rakuten-Ichiba-API).

The official consumer entry points used are [Amazon.co.jp](https://www.amazon.co.jp/), [楽天市場](https://www.rakuten.co.jp/), [Yahoo!ショッピング](https://shopping.yahoo.co.jp/), [au PAY マーケット](https://wowma.jp/), [Qoo10](https://www.qoo10.jp/), and [メルカリ](https://jp.mercari.com/). Reviewed 2026-11-10. External mobile access remains outside this change.

### Additional account-browser connections

ヨドバシ.com、ビックカメラ.com、ZOZOTOWN、eBay、楽天ラクマ、Yahoo!フリマもサービス一覧からOLCRの専用ブラウザーで開けます。これらはアカウント用ブラウザーの接続のみです。未確認の商品検索APIや注文APIは表示しません。各社でログイン・多要素認証が必要になり、サイトによるブラウザーの制限がある場合もあります。

Verified official entry points: [Yodobashi](https://www.yodobashi.com/), [Bic Camera](https://www.biccamera.com/), [ZOZOTOWN](https://zozo.jp/), [eBay](https://www.ebay.com/), [Rakuma](https://fril.jp/), [Yahoo! Flea Market](https://paypayfleamarket.yahoo.co.jp/).
