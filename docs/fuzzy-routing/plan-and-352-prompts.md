# Fuzzy API routing: plan and natural-language probes

## Scope and evidence

The current `PRODUCTION_ACCEPTANCE_CASES` fixture exposes 89 knowledge APIs after the production exclusions. This plan contains four additional prompt candidates per API (356 total), phrased to vary the user’s angle rather than merely removing the API name from the existing canonical prompt. The existing `test_fuzzy_api_routing_matrix.py` also checks two source-name-free paraphrases per API. The `geo.poi` API was added to the plan after the earlier 88-API execution ledger; historical ledger counts below remain as recorded.

“Untested” here means not present in the canonical acceptance prompt or the two generated variants in that matrix. A full deduplication against every historical benchmark/log has not yet been run, so the prompts are candidates until that audit is completed.

## Execution ledger (2026-10-08)

- Offline batch: all 352 prompts across the 88 active APIs passed the candidate-route / typed-argument-or-safe-clarification suite. This is routing evidence only: provider calls were not made, and it is not GUI acceptance.
- Additional backend regression batch: 123 tests passed, 7 skipped. It exposed and fixed (a) empty CVE envelopes incorrectly reported as semantic data and (b) explicit ReliefWeb requests being silently redirected to HDX. These remain regression tests; excluded ReliefWeb is not a production target.
- Exploratory real-GUI probe: “CVE-2021-44228は、外部から悪用できる？必要な条件と影響を短く説明して。” incorrectly selected FIRST EPSS rather than CVE details. The route-priority fix and regression assertion are now in source; GUI retest is pending.
- Build `olcr-fuzzy-352-20261008-r2` compiles and was launched by its absolute bundle path with the same isolated profile and existing projectless conversation. Startup has not reached `KEYCHAIN_RESTORE completed`; therefore the 352 live GUI cases have not started. Keep them in the live queue. Do not create a conversation per API/test type.
- Additional GUI findings in r2: CVE was rerouted to CVE Services and returned a grounded record (but its prose still does not state a definitive yes/no and its affected-version rendering is incomplete); the RDAP prompt selected RDAP and received HTTP 200, but the user-visible answer contained only provider and domain. RDAP lost nested nameservers/events during generic normalization. Its dedicated normalization/renderer regression test now covers registration status, registrar, nameservers, and dates.
- The no-country rule now distinguishes global holiday scope from an underspecified local holiday request: explicit worldwide prompts route directly to Nager.Date's worldwide endpoint without requiring a country code; a local holiday request with no country still abstains for clarification. Added the exact GUI phrasing as a direct-route regression case.
- Build `olcr-fuzzy-352-20261008-r3` was produced and ad-hoc signed after Tauri could not find the configured `OLCR Local Debug Code Signing` identity (`security find-identity` returned 0 identities). It launched from the exact absolute bundle path in the same existing conversation/profile, but Keychain bundle restore timed out; the spawned Python process stalled during interpreter path/config-file initialization, never opened its API port, and the GUI reached FAILED. No prompt was sent and no conversation was created. This run is a startup failure, not live E2E evidence; the existing live queue remains unchanged.
- Resume attempt (2026-10-09): the Mac is unlocked. The exact existing GUI at `/tmp/olcr-e2e-global-holidays-20261008-04-target/debug/bundle/macos/OLCR.app` was inspected and showed `FAILED` with its composer disabled; that window was closed, releasing the single-instance lock. r4's executable and bundle are present, but Launch Services rejects the bundle with `kLSNoExecutableErr` (`-10827`), while direct execution is blocked by the app's process-identity guard (`Operation not permitted`). The copied r4 bundle's signature remains `CSSMERR_TP_NOT_TRUSTED`; no Keychain restore or live prompt was reached, and no conversation was created. Thus GUI E2E remains blocked on launching the signed r4 bundle through Launch Services.
- Signing-trust diagnosis after user authorization: `security find-identity -v -p codesigning` reports 0 valid identities; no matching OLCR certificate was found in the login or System keychain, and the app bundle contains no extractable certificate chain. Therefore there is no existing signer certificate available to add to trust settings. Recreating a signing identity would change the pinned root requirement (`969372f8911d3a2a85ea3339df864961a663f683`) and could invalidate the Keychain bridge / prior Always Allow association; do not silently rotate it. Await recovery of the original certificate/private key or explicit approval for a coordinated identity rotation.
- Offline regression rerun on 2026-10-09: `test_fuzzy_api_routing_matrix` plus `test_external_api_expansion` passed 83 tests with 7 skipped in an isolated temporary database. This validates routing/provider fixtures only, not live provider calls or GUI behavior; the live 352-case queue remains unchanged.
- Resume on 2026-10-09: recovered the existing `OLCR Local Debug Code Signing` identity without changing Keychain trust settings or rotating the identity. The current-source r5 bundle was launched by its absolute path with the same isolated profile; strict code-signature verification passed, backend reached READY on port 62150, and Keychain restore completed through the stable bridge (counts only: web=1, external=10). The active runtime manifest reports 88 registered production APIs matching 102 acceptance fixtures.
- In the existing projectless conversation `0be00b3f-2764-4f04-aa5c-91bf766a59f1`, the worldwide-holidays probe passed live: Nager.Date returned six countries for the local current date and the GUI displayed the provider-backed list. The next probe (“次に飛ぶ日本のロケットはいつで、どこから上がる予定？”) failed live: the GUI fell back to generic advice and did not call Launch Library 2. The r5 launch log has no per-request router/provider trace for that turn, so the visible answer is the available failure evidence; it does not prove a provider HTTP error.
- Root cause for the rocket probe: the deterministic `space.launches` matcher recognized literal “ロケット…打ち上げ/発射” and “打ち上げ予定” phrasing, but not the natural paraphrase “次に飛ぶ…いつ…どこから上がる”. Expanded the rule for Japanese schedule/location/date/change paraphrases and English upcoming/next-launch phrasing, and added a regression test covering all four planned launch angles plus an English paraphrase. The focused routing/compiler and rendering tests pass (2 tests).
- Live retry on 2026-10-09, same isolated profile and existing projectless conversation: build `olcr-fuzzy-352-20261009-r8` restored Keychain credentials (`web=1`, `external=10`), reported READY on port 50278, and passed strict deep signature verification. Repeated “次に飛ぶ日本のロケットはいつで、どこから上がる予定？” after the earlier worldwide-result failure. GUI now shows only Japan launches; first result is H3-24/MMX from Tanegashima, followed by Epsilon S and H3-22, with location, rocket, mission, agency, status, and provider links. Backend evidence: `space.launches` deterministic route, arguments compiled/validated, both provider requests HTTP 200, `DETERMINISTIC_RENDER_STATUS=PASS`; visible result matches. This probe is removed from the remaining live queue; permanent fixture remains. The 352 GUI probes are not complete.
- Same existing GUI/profile/conversation, r10 live batch: “今月予定されている打ち上げを、運ぶもの別に見比べたい。” first failed (one response returned nested rocket dictionaries, another generic provider failure). Root causes were oversized detailed pages and absent month/payload shaping. Backend now paginates detailed LL2 results in 10-item pages, applies the current-month window locally, normalizes nested fields, groups by payload type, and renders only the requested month. Retest passed live: three October launches grouped as Unknown, Government/Top Secret, and Communications; dates, mission/orbit, rocket, site, status, provider source and links were visible, with no raw dictionary. This probe is removed from the remaining live queue; its permanent fixture remains.
- In the same live conversation, the “延期や変更” probe initially returned current upcoming launches without distinguishing current status from historical change evidence. r11 retest passed after the renderer added an explicit limitation, current status, and provider `last_updated` timestamps; it states that a current snapshot cannot prove a historical delay/change. This probe is removed from the live queue; its permanent fixture remains.
- The r11 Mars-mission retest used LL2's documented `search=Mars` and returned two related entries (Mars Lander and ExoMars) plus one unrelated Inmarsat result. This exposed a substring false positive (`Inmarsat` contains “mars”). Source now validates Mars relevance with word-boundary/exomars matching, with a regression test. The Mars probe remains pending r12 GUI retest.
- Offline verification after these changes: 88 tests passed, 7 skipped. Four prompts have passed live so far: Nager.Date worldwide holidays, Japan launches, month-filtered payload grouping, and honest schedule-change status. The original 352 live probes remain an ongoing batch; all passed cases are removed only from the live queue and fixtures are retained.
- Current-source GUI build `olcr-fuzzy-352-20261009-r21` was launched from its absolute bundle path with Keychain restore enabled, isolated cloned profile, and the same existing projectless conversation. The full 352 offline routing/argument matrix plus external API regression batch passed: 103 tests, 7 skipped. In GUI, the Japan launch-angle probe “火星探査に関係する次の打ち上げ計画があれば知りたい。” now returns only relevant Mars missions (Terran R/Mars Lander and Falcon Heavy/ExoMars, plus New Glenn/Mars Telecommunications Orbiter); the prior Inmarsat substring false positive is absent. Remove this fifth probe from the remaining live queue; retain its regression fixture. Worldwide holidays were also visibly verified in this same conversation as six country/date rows for 2026-10-09, without asking for a country.
- r21 GUI also retested the ISS land/sea angle after fixing the typed-renderer intent handoff. The `space.iss_position` call now renders fresh coordinates, timestamp, and OpenStreetMap link, and correctly identifies that its response has no land/sea field instead of silently omitting the question. This is honest but does not answer the requested land/sea classification; keep the angle pending. The direction and nearby-city ISS angles also remain pending because the position-only API cannot establish those from one snapshot. Do not count the malformed earlier “ISS” input as a test.
- The exact nearby-city ISS angle was sent again in r21. The GUI returned a fresh position, timestamp, map link, and a clear note that reverse geocoding is not performed; no city/region was supplied. Keep this case pending. Five of the 352 probes have passed live (including the Mars retest), leaving 347 in the live queue; the 352 offline regression probes all remain in their permanent fixture.

The offline 352-case regression matrix stays in place for future regression detection. Once a specific case passes end-to-end in the existing conversation, remove it from the *remaining live execution queue* only; do not delete its permanent regression fixture.

## What the screenshot suggests

The prompt “今日の世界の祝日は？” is underspecified along at least two axes: “today” needs a date/time-zone interpretation, and “world holidays” may mean international observances, country-specific public holidays, or a list of holidays worldwide. The displayed response instead looks like a full-year Japanese statutory-holiday calendar starting at January 1. That suggests both intent/argument-resolution problems: the router chose a plausible holiday API without resolving geography and date scope, and the adapter appears to have defaulted to Japan/year-list behavior. The fix should not be a broader keyword trigger alone; it must preserve the distinction between a plausible API and a sufficiently grounded request.

## Routing and execution plan

1. **Build a capability card for every production API.** Include subject/entity types, supported operations, geographic coverage, time semantics (instant, date, interval, history, forecast), filters/sort/pagination, returned fields, credential/external-access requirements, freshness, and known limitations. Derive it from adapter schemas and acceptance fixtures; do not infer unsupported filters from provider names.
2. **Retrieve broadly, select conservatively.** Normalize Japanese/English and mixed-script entities; use lexical/domain/entity hints plus the existing EmbeddingGemma→Laya candidate path. Keep overlapping providers in a shortlist, then score fit against the full request (subject, operation, geography, period, requested output). Do not hard-route from a single generic word such as “today”, “data”, “holiday”, or “analysis”.
3. **Resolve missing slots before calling.** Infer only safe defaults (e.g. an explicitly local “today” using the user's locale). Ask a concise clarification when country/region, date range, entity identity, or metric changes the answer materially. If multiple APIs are complementary, plan a small multi-source call; if one API is clearly nearest, use it. Keep user-requested source constraints authoritative.
4. **Compile typed arguments from intent.** Separate entity resolution from argument construction; validate dates, units, country codes, identifiers, and bounds against the adapter. Never silently convert “today” into “all of this year”, or a global request into Japan-only data. Preserve source result counts, time coverage, and provenance.
5. **Use explicit fallback policy.** For a factual/explanatory request with no suitable structured API, fall back to Wikipedia first. If the user requests search/current reporting, try the search route; if Brave is unavailable or insufficient, fall back to Wikipedia with an honest limitation. Do not present fallback as though the requested API succeeded.
6. **Add ambiguity-aware offline evaluation.** For each prompt, assert: expected provider set (allow only truly equivalent APIs), candidate recall, selected provider, argument slots, clarification-vs-call decision, and forbidden near-neighbor providers. Add negative controls for lookalike terms and explicit “do not use external sources” constraints. Keep the current 88 canonical source-specific live acceptance cases unchanged.
7. **Batch and diagnose before GUI E2E.** Run all 352 prompts offline through the real candidate-retrieval and router-decision path with provider calls stubbed. Group misses by failure class (candidate recall, wrong-neighbor selection, missing slot, bad argument compilation, false-positive call). Fix shared logic first, then rerun the full batch. Once the matrix is stable, run a small authorized live API smoke set and finally one real GUI E2E pass; compare visible answer, selected API, typed arguments, and redacted logs.

## Test contract

- These are natural-language routing probes, not requests to expose keys or execute mutations. External provider execution should be mocked in the 352-case routing suite.
- A “pass” requires the right API (or an explicitly declared equivalent), correct arguments/clarification, no unrelated API call, and a truthful response shape. Merely returning non-empty text is not a pass.
- Record expected API, intent/operation, entity, geography, period, required slots, and whether clarification is acceptable beside each prompt when converting this plan to machine-readable fixtures.
- Freeze each prompt after it is run; revisions receive a new case ID so coverage history stays auditable.

## Prompt candidates (89 APIs × 4 angles)

### 1. `calendar.public_holidays` — Nager.Date
1. 今日、世界のどこかで祝日になっている国を知りたい。国ごとに教えて。
2. 来週旅行するなら、行き先で祝日に重なって店が閉まりそうな日はある？フランスの日程を確認して。
3. 日本と韓国で、今年の連休の並び方にどんな違いがある？
4. 祝日が月曜に移動する制度がある国について、今年の日付を調べて。

### 2. `space.launches` — launch schedule
1. 次に飛ぶ日本のロケットはいつで、どこから上がる予定？
2. 今月予定されている打ち上げを、運ぶもの別に見比べたい。
3. 直近の打ち上げ予定で、延期や変更が出ているものはある？
4. 火星探査に関係する次の打ち上げ計画があれば知りたい。

### 3. `space.space_weather` — space weather
1. 今の太陽活動で、無線通信やGPSに影響が出そうな兆候はある？
2. オーロラを見られる可能性が高まりそうな宇宙天気か、今の状況から教えて。
3. Kp指数が普段より上がっているか、現在値を基準と一緒に知りたい。
4. 宇宙天気の今の状態を、衛星運用への影響という観点で短く説明して。

### 4. `space.satellite_orbit` — satellite orbital elements
1. ISSの軌道データを使って、東京上空を通る時刻を見積もる材料がほしい。
2. ISSとハッブル宇宙望遠鏡の軌道要素を並べて比べたい。
3. ISSの軌道が最近変わったか確認できる最新の要素を出して。
4. TLEの数字を軌道計算に使うので、ISSの最新セットを取得して。

### 5. `space.ephemeris` — planetary ephemeris
1. 今年の年末ごろ、地球から火星までどれくらい離れている？
2. 来月の同じ時刻で、金星と火星のどちらが地球に近いか比べて。
3. ある観測日の木星の地心距離を知りたい。2027年1月15日で計算して。
4. 火星までの距離が一年の中で大きく変わる時期を調べるため、四半期ごとの値を出して。

### 6. `space.iss_position` — ISS live position
1. ISSは今、陸地の上と海の上のどちらを飛んでいる？
2. 今の位置から、ISSが日本の近くへ来る方向を知りたい。
3. 現在地の緯度・経度を地図に載せられる形で教えて。
4. 今ISSがいる場所を、近い都市や地域の目安と一緒に説明して。

### 7. `space.exoplanets` — exoplanet catalog
1. TRAPPIST-1の惑星たちは、地球と比べると大きさや一年の長さがどう違う？
2. 公転周期が短い既知の系外惑星をいくつか見せて。
3. ケプラーで見つかった惑星のうち、半径が地球に近いものを探して。
4. TRAPPIST-1 eと同じ恒星を回る惑星をまとめて、基本的な違いを知りたい。

### 8. `japan.diet_transcript` — Japan Diet proceedings
1. 国会で生成AIの規制について、政府側はどんな答弁をしている？
2. 最近の国会質疑で、少子化対策をめぐって与野党の論点がどう違ったか調べて。
3. 防災に関する大臣答弁を、発言者と会議日が分かる形で探して。
4. 国会でマイナンバー制度の課題が取り上げられた最近のやり取りを見たい。

### 9. `japan.law` — Japanese statutes
1. 個人情報を第三者に渡すとき、法律上どんな条件がある？該当する条文を確認したい。
2. 個人情報保護法の目的条文と、事業者の義務に関係する条文を見比べて。
3. この制度が最近改正されたか、現行法の該当箇所と施行時期を調べて。
4. 個人データの開示請求をする人に関係する条文を、条番号付きで教えて。

### 10. `geo.elevation` — elevation lookup
1. 富士山の山頂と東京駅の標高差はどれくらい？
2. この住所で洪水リスクを考える前提として、地面の標高を知りたい：東京駅。
3. 京都駅周辺と大阪駅周辺では、標高にどのくらい差がある？
4. 登山口から山頂までの標高差を見積もりたい。富士山五合目と山頂を比べて。

### 11. `earth.streamflow` — river gauge data
1. ポトマック川の水位は平年より高そう？直近の観測値を見たい。
2. ワシントン周辺で、今流量が大きい河川観測所を確認して。
3. 川遊びに行く前に、近くの水位・流量の最新状況を知りたい。ポトマック川で。
4. 近隣の複数観測地点で流量に差があるか、最新データを並べて。

### 12. `biology.occurrence` — species occurrence records
1. カワセミは東京のどの地域でよく観察されている？記録を探して。
2. この種が季節によって見つかる場所が変わるか、東京の観察記録で確認したい。
3. 近所で最近見られた野鳥を調べたい。上野公園周辺の記録を見せて。
4. カワセミの観察記録について、いつ・どこで報告されたか最近のものを知りたい。

### 13. `biology.phylogeny` — phylogenetic relationships
1. 犬と猫は進化の系統樹で、どのあたりから別の枝になったの？
2. オオカミとキツネの共通祖先に近い分類ノードを知りたい。
3. ヒトとチンパンジーの系統関係を、共通祖先をたどって説明して。
4. クジラとカバが近縁だと言われる根拠になる系統上のつながりを見せて。

### 14. `biology.structure` — protein structures
1. ヘモグロビンの立体構造を、酸素結合状態が異なるもの同士で比べたい。
2. ヒト以外の生物のヘモグロビン構造も登録されている？代表例を探して。
3. タンパク質構造を論文で引用したいので、解像度や登録情報が分かるPDB記録を見せて。
4. ヘモグロビンの複合体構造と単独構造を見つけて違いを確認したい。

### 15. `biology.protein` — UniProt protein records
1. BRCA1は細胞の中でどんな役割を担っている？信頼できるタンパク質記録を見たい。
2. ヒトTP53とマウスの対応タンパク質を比べて、基本機能を知りたい。
3. インスリンの前駆体から成熟タンパク質までの情報を確認して。
4. あるタンパク質の別名や配列長も知りたい。ヒトのEGFRを調べて。

### 16. `security.cve` — CVE vulnerability records
1. Log4Shellをまだ使っているサーバーがある場合、何が危険でどの版が対象？
2. このCVEはリモートから悪用できるのか、攻撃条件と影響範囲を確認したい：CVE-2021-44228。
3. Apache Log4jの脆弱性について、修正版と暫定回避策が記録されているか調べて。
4. CVE-2021-44228と同じ製品に関係する関連脆弱性を探して。

### 17. `security.exploit_probability` — EPSS
1. CVEの深刻度が高くても悪用されにくい場合はある？Log4ShellのEPSSと合わせて判断したい。
2. この脆弱性の悪用される可能性を、今日のEPSS値で確認して：CVE-2021-44228。
3. いくつかの未修正CVEを優先順位付けしたい。Log4ShellとCVE-2023-34362のEPSSを比べて。
4. CVSSではなく、実際に悪用される見込みに基づいてこのCVEを評価する材料がほしい。

### 18. `crypto.bitcoin_network` — Bitcoin network status
1. すぐ承認してほしい送金なら、今どの程度の手数料を設定すればよさそう？
2. mempoolが混んでいるか、低・中・高優先度の手数料目安で教えて。
3. 今のネットワーク状況だと、手数料を抑えた送金はどれくらい待ちそう？
4. 直近のBitcoin手数料水準が落ち着いているか、混雑状況と一緒に確認して。

### 19. `internet.domain_registration` — domain RDAP
1. openai.comの登録期限とレジストラが分かる公開情報を確認して。
2. このドメインは今も登録済み？ネームサーバーも見たい：example.com。
3. example.orgの登録者情報が公開されている範囲と、登録状況を調べて。
4. 2つのドメインのネームサーバー設定を比較したい：openai.comとwikipedia.org。

### 20. `internet.ip_info` — IP/ASN information
1. 自社ログに出たこのIPのネットワーク運営者と国を確認したい：1.1.1.1。
2. 8.8.8.8を広告で見たけれど、どの組織のASNから告知されている？
3. 2つのDNSサービスのIPが同じ事業者ネットワークに属するか比べて：1.1.1.1と8.8.8.8。
4. この接続元IPの地域とネットワーク情報をインシデント記録に残したい：9.9.9.9。

### 21. `media.tv` — TV metadata
1. Breaking Badの最初のシーズンは何話構成で、各話の公開順は？
2. 同じシリーズのシーズン1と2で、初回放送日の間隔を知りたい。
3. 「Ozymandias」というタイトルのBreaking Badの話数と放送日を探して。
4. ある俳優が出演したドラマのエピソード情報を確認したい。Breaking Badの出演回を見せて。

### 22. `games.pokemon` — Pokémon records
1. ピカチュウから進化するポケモンと、その進化条件を知りたい。
2. ピカチュウとライチュウを比べて、タイプと能力値の違いを見せて。
3. じめんタイプに弱いポケモンを育成する前に、ピカチュウの相性を確認したい。
4. カントー地方の電気タイプで、素早さが高いポケモンを探して。

### 23. `games.trivia` — trivia question generator
1. 小学生と遊べる宇宙のクイズを、やさしい四択で1問出して。
2. 映画好き向けに、少し難しめのクイズを一つお願い。
3. 英語学習の休憩に使える、地理カテゴリの中級クイズを作って。
4. 答えをすぐ表示せず、科学の四択問題だけ先に出して。

### 24. `food.recipe` — recipe search/generation
1. 冷蔵庫に鶏肉とキャベツがある。平日の夕食に作れる料理をいくつか教えて。
2. 乳製品を避けたい人向けに、手元の豆腐ときのこで作れる料理を提案して。
3. 余ったご飯を使い切れる、辛くない昼食の作り方を知りたい。
4. 同じ食材で、調理時間が短いものと作り置き向きのものを比べて。

### 25. `art.met_collection` — Met collection
1. ゴッホの作品のうち、風景画と自画像をそれぞれ探して所蔵情報を見たい。
2. 19世紀フランス絵画の代表作を、作家や制作年と一緒に見せて。
3. メトロポリタン美術館にある日本美術で、江戸時代の版画を探して。
4. ゴッホの作品がいつ制作され、どんな素材で描かれたか記録を確認したい。

### 26. `games.pc_deals` — PC game deals
1. Steamで遊べるCyberpunk 2077が、今どのストアで一番安い？
2. セール中の協力プレイ用PCゲームを、価格と割引率で比べて。
3. Elden Ringを買うなら、Steamキーと公式ストアの現在価格に差がある？
4. 予算20ドル以内で評価の高いPCゲームのセールを探して。

### 27. `games.chess` — public chess games
1. Hikaru Nakamuraの最近の対局で、よく使っているオープニングを見たい。
2. 直近の公開対局を、相手と勝敗が分かる形で並べて。
3. Nakamuraが黒番で指した最近の対局を棋譜付きで探して。
4. 彼の最近の対局から、短手数で決着したものと長いものを比べたい。

### 28. `sports.formula1` — Formula 1 standings
1. 2025年はチームメイト同士で、ドライバーズポイントにどんな差が出た？
2. シーズン終盤に順位が上がったドライバーを、最終順位と一緒に見たい。
3. 2025年のコンストラクターズ順位とドライバーズ順位を照らし合わせて。
4. あるドライバーが年間タイトルを取った年の最終ポイントを確認したい。2025年で。

### 29. `knowledge.wikidata` — Wikidata structured facts
1. EU加盟国を人口と面積の両方で並べると、順位はどう変わる？
2. ノーベル物理学賞を受賞した日本出身者と受賞年を探して。
3. 東京と大阪について、人口・座標・行政区分を同じ形式で比べて。
4. ある作家の著作と言語、出生地のつながりを構造化データで確認したい。村上春樹で。

### 30. `environment.air_quality` — air quality
1. 今日、東京で屋外運動をするなら、空気が比較的きれいな時間帯はいつ？
2. 東京と横浜のPM2.5を比べて、外出時に気をつける差があるか教えて。
3. AQIが悪化しているとき、今の東京で主に問題になっている汚染指標を知りたい。
4. 明日の東京の空気の状態を見て、子どもの屋外活動に注意が必要か判断したい。

### 31. `geo.routing` — route planning
1. 東京駅から皇居まで、徒歩と自転車なら距離と所要時間はどれくらい違う？
2. 渋谷駅から東京駅へ、車で行く場合のルート距離を知りたい。
3. 新宿駅から羽田空港まで、車と徒歩で経路の長さを比べて。
4. 東京駅から皇居まで、移動距離が短い道順を出して。

### 32. `geo.country_facts` — country profiles
1. フランスと日本で、通貨・公用語・人口規模を比べて。
2. 海に面していないヨーロッパの国を挙げて、首都と通貨も知りたい。
3. ブラジルの面積と人口を、南米の近隣国と比べた位置づけで教えて。
4. 出張準備のため、スイスの首都、通貨、主要言語をまとめて。

### 33. `econ.worldbank` — World Bank indicators
1. 日本と韓国の一人当たりGDPは、この10年でどう差が変わった？
2. 日本のCO₂排出量を人口一人あたりで見た推移を知りたい。
3. 東アジア各国の女性労働参加率を同じ指標で比べて。
4. 2010年以降、ブラジルのGDP成長率がどう上下したか年ごとに見せて。

### 34. `econ.oecd` — OECD indicators
1. 日本の失業率は、OECDの中で高い方？最近の各国値で比べて。
2. ここ数年の日本の実質賃金と物価の動きを比較できるデータを探して。
3. OECD諸国で高齢者の就業率がどう違うか、直近年で見たい。
4. 景気の弱さが雇用に出ているか判断するため、日本の失業率の年次推移を教えて。

### 35. `earth.earthquakes` — earthquake catalog
1. 日本の近くで最近起きた地震を、震源の深さも含めて確認して。
2. 太平洋沿岸で過去1週間にM4以上の地震は何回あった？
3. 今日の地震のうち、規模が大きいものを震源地別に見たい。
4. 最近の地震が特定地域に集中しているか、地図に載せる位置情報付きで出して。

### 36. `earth.disasters` — active disasters
1. 今、世界で避難が必要な規模の洪水やサイクロンは起きている？
2. 東南アジアで進行中の災害について、発生地域と開始日を知りたい。
3. 現在の山火事と洪水を、災害の種類ごとに比べて。
4. 旅行先のチリで最近発生した大規模災害があるか確認して。

### 37. `ocean.tides` — tide predictions
1. サンフランシスコで明日、海岸を歩くなら潮が一番低いのは何時？
2. 今日の満潮と干潮の高さの差を知りたい。場所はサンフランシスコ。
3. 週末の釣りに向けて、潮の変わり目の時刻を調べて。
4. 同じ日にサンフランシスコとオークランドで潮汐時刻がどれくらい違う？

### 38. `astronomy.sunrise` — sunrise/sunset
1. 明日の東京で、写真撮影に向く日の出前後の時間を知りたい。
2. 東京と札幌では、明日の日照時間にどのくらい差がある？
3. 今月末の大阪の日の入りは何時ごろ？
4. 来週のキャンプで使うので、長野市の日の出と日の入りを調べて。

### 39. `books.openlibrary` — Open Library
1. 『ナルニア国物語』のシリーズ順と、最初に読む巻を確認したい。
2. Ursula K. Le Guinの代表的な本を、出版年と一緒に探して。
3. この本の別版や翻訳版があるか調べて：ISBN 9780140328721。
4. 児童向けの海洋冒険小説で、書誌情報が見つかる本をいくつか教えて。

### 40. `web.archive` — Wayback snapshots
1. openai.comのトップページが2020年ごろどう変わったか、保存版を探したい。
2. このサイトが閉鎖される前のページを見たい：example.com。
3. 2018年から2024年まで、同じURLの保存スナップショットがどれくらいある？
4. ある企業サイトのリニューアル前後を比較したい。openai.comの過去版を見せて。

### 41. `chemistry.pubchem` — PubChem compounds
1. カフェインとテオブロミンは、分子式や分子量がどう違う？
2. アスピリンの化学構造と基本的な物性を知りたい。
3. この化合物の同義名や識別番号を調べて：水に含まれる塩化ナトリウム。
4. 分子量が約180の糖類を探して、化学式を確認して。

### 42. `health.clinical_trials` — clinical trials registry
1. 日本で募集中の2型糖尿病の試験は、どんな介入を比較している？
2. アルツハイマー病の臨床試験を、募集状況と実施地域で探して。
3. 小児喘息を対象にした試験で、現在参加者を募集しているものはある？
4. がん免疫療法の試験について、試験段階と主な評価項目を確認したい。

### 43. `health.openfda` — FDA safety data
1. ibuprofenで報告されている副作用の種類を、最近の報告から確認したい。
2. FDAの添付文書では、アセトアミノフェンの重大な注意事項はどう書かれている？
3. 同じ成分の薬で、ブランド名によって副作用報告の傾向に違いがあるか比べて。
4. この薬に関するリコールや安全性情報が出ていないか調べて：ibuprofen。

### 44. `food.product` — Open Food Facts
1. このシリアルにアレルゲンや添加物が含まれているか見たい。バーコード3017620422003。
2. 砂糖が多い商品か判断したいので、栄養成分と100g当たりの値を確認して。
3. 同じブランドの商品の原材料と栄養スコアを比べたい：3017620422003。
4. 商品のビーガン／ベジタリアン表示と原産国が登録されているか調べて。

### 45. `news.hackernews` — Hacker News
1. Hacker Newsで、今日開発者の間で議論が盛り上がっている話題は何？
2. AIではなくデータベースに関する人気投稿を探して、コメントの多い順に見たい。
3. トップ記事の中で、実際の議論が活発なものをタイトルとリンク付きで教えて。
4. 最近のHacker Newsで、Rustの新しい使い方に関する投稿が注目されている？

### 46. `knowledge.devto` — DEV Community
1. Find practical DEV posts explaining how teams debug flaky tests, with authors and links.
2. What are people publishing about accessibility in React lately? Show a few useful articles.
3. I’m new to containers; find beginner-friendly DEV.to guides and tell me when they were posted.
4. Find recent posts comparing server-side rendering approaches, including tags and reaction counts.

### 47. `dev.github` — GitHub releases
1. FastAPIの直近のリリースで、前の版から何が変わったか知りたい。
2. このライブラリの最近の更新頻度を、リリース日から把握したい：pydantic。
3. Kubernetesの最新安定版がいつ公開され、どんな変更を含むか調べて。
4. リリースノートを見て、Node.jsの現行LTS系列で追加された機能を確認して。

### 48. `aviation.opensky` — OpenSky aircraft states
1. 今、成田空港の近くを飛んでいる機体はどの方向へ向かっている？
2. 羽田周辺の航空機を、高度や速度も含めて確認したい。
3. 現在の関西空港周辺で、旅客便らしい機体をいくつか見せて。
4. 東京上空にいる航空機の位置を、地図に使える座標付きで一覧にして。

### 49. `language.dictionary` — dictionary lookup
1. “resilient” は仕事の文脈でどんな意味？例文と品詞も知りたい。
2. “affect” と “effect” の使い分けを、辞書的な意味と発音から説明して。
3. “record” が名詞と動詞で発音のどこが変わるか教えて。
4. 「思いがけない幸運」に近い英単語の意味と使い方を調べて。

### 50. `language.translation` — translation
1. “Could you give me a hand?” を、友人向けと仕事向けで自然に訳し分けて。
2. 「検討します」が必ずしも承諾ではないことを、英語で誤解なく伝えたい。
3. この案内文を、旅行者に分かりやすい日本語にして：Please proceed to the designated boarding area.
4. “お疲れさま” を会議後の同僚に言う場合、自然な英語表現をいくつか教えて。

### 51. `render.quickchart` — chart rendering
1. 月ごとの売上が 10, 14, 9, 18 だったので、変化が分かる折れ線グラフにして。
2. 東京・大阪・福岡の人口を比べる棒グラフを作りたい。値は1400、880、160万人。
3. 予算の内訳が人件費50%、開発30%、その他20%だと分かる円グラフを作って。
4. 2022年から2025年までの温度を 18, 19, 20, 21 度として、軸ラベル付きで画像にして。

### 52. `policy.us_federal_register` — Federal Register
1. 米国のAIに関する最近の政府指針で、企業に新たな義務を課すものはある？
2. 連邦官報で提案段階と最終決定済みの環境規則を区別して探して。
3. ある規則への意見募集がいつ締め切られるか、公式文書から知りたい。
4. 2025年以降に発効したプライバシー関連の連邦規則を見つけて。

### 53. `conflict.ucdp` — UCDP conflict events
1. ウクライナ東部で直近に記録された戦闘イベントは、どの地域に集中している？
2. 今年のウクライナの紛争イベントを、月ごとの件数と死者数で比べたい。
3. UCDPの記録で、民間人が被害を受けた最近の暴力イベントを探して。
4. ある州の紛争状況を説明するため、日付・場所・暴力種別が分かる記録を見せて：ドネツク州。

### 54. `humanitarian.hdx` — Humanitarian Data Exchange
1. スーダンの避難民に関するデータは、どの機関がいつ更新している？
2. 食料不安が深刻な国を比べたいので、最新の関連指標を探して。
3. ハイチの人道状況を把握するため、避難・食料・保健のデータセットを見たい。
4. HDXに登録されているウクライナの人口移動データについて、期間と提供元を確認して。

### 55. `humanitarian.unhcr` — UNHCR refugee statistics
1. シリアから逃れた人々は、トルコ以外ではどの国に多く滞在している？
2. 近年、ウガンダで受け入れている難民数はどう変化した？
3. 国籍別と受入国別で難民数を見比べたい。アフガニスタン出身者について。
4. 最新の公表統計で、世界の難民・庇護申請者の規模を知りたい。

### 56. `sanctions.unsc` — UN Security Council sanctions
1. 北朝鮮制裁リストに載る団体について、指定理由と制裁委員会を確認したい。
2. 国連制裁対象として指定された船舶があるか調べて。
3. 国連安保理の制裁対象について、別名や指定日がどう登録されているか確認したい。
4. イラン関連の国連制裁対象が現在のリストに含まれるか探して。

### 57. `sanctions.opensanctions` — OpenSanctions search
1. Rosneftに関連する企業や別名が、どの制裁リストに載っている？
2. この会社と同名の人物・法人が複数あるか、識別情報を比べて確認したい：Acme Holdings。
3. 制裁対象企業の親会社や関連組織を調べるため、公開エンティティ記録を探して。
4. 取引先の名前が制裁・PEPデータに一致するか、候補と出典を確認して：Rosneft。

### 58. `econ.imf` — IMF data
1. 日本と米国の消費者物価上昇率は、最近どちらの方が速い？
2. ここ10年の日本の経常収支を、GDP比で見たい。
3. IMF統計で、新興国グループのインフレ率を国ごとに比べて。
4. 日本の失業率と物価上昇率を同じ期間で並べ、景気の背景を把握したい。

### 59. `trade.un_comtrade` — UN Comtrade
1. 日本は米国以外に、乗用車をどの国へ多く輸出している？
2. 2020年以降、日本の自動車輸出額は年ごとにどう変わった？
3. 日本とドイツの対米乗用車輸出額を、同じ年で比較して。
4. 半導体製造装置の主要な輸出国を、相手国別の金額で調べて。

### 60. `finance.bis` — BIS statistics
1. 日本の家計向け信用と企業向け信用で、伸び方に差がある？
2. 国境を越えた銀行債権が大きい国を、最新のBIS統計で比べて。
3. ここ数年、日本の不動産向け信用は増えているか時系列で見たい。
4. 為替変動を踏まえて国際与信の変化を確認するため、BISの関連系列を探して。

### 61. `development.un_sdg` — UN SDG indicators
1. 日本の目標8.5.2（失業率）は、性別や年齢層でどんな差がある？
2. SDG 7の再生可能エネルギー指標を、東アジア各国で比べたい。
3. 日本の目標13に関する最新指標と、データが対象とする年を教えて。
4. 目標4の教育指標で、過去数年に改善が見られる国を探して。

### 62. `aviation.adsb_live` — ADSB.lol live aircraft
1. 今、羽田に着陸しそうな航空機を、進行方向や高度から見たい。
2. 成田周辺で現在飛行中の機体を、コールサインが分かるものだけ確認して。
3. 大阪空港の近くにいる航空機の位置と速度を教えて。
4. 東京湾上空の航空機を、今の座標と高度付きで一覧にして。

### 63. `econ.adb` — Asian Development Bank data
1. フィリピンの成長率を、インドネシアやベトナムと比べて。
2. ADBの予測では、アジア太平洋のインフレ見通しは国ごとにどう違う？
3. フィリピンの貧困率や雇用に関する長期指標を探して。
4. ADBの統計で、南アジア各国の電力アクセスの変化を見たい。

### 64. `econ.afdb` — African Development Bank data
1. アフリカ開発銀行の公開データで、ケニアとタンザニアのGDP成長率を最近の年で比べて。
2. AfDBの指標で、東アフリカの物価上昇率が高い国を確認したい。
3. ケニアの一人当たり所得や雇用の推移を見せて。
4. アフリカ諸国の電力アクセスがどう改善してきたか、国別データを探して。

### 65. `econ.cepalstat` — CEPALSTAT
1. アルゼンチンとチリの物価上昇率を、最近の期間で比べて。
2. ラテンアメリカで若年失業率が高い国を、同じ指標で見たい。
3. ブラジルの所得格差が近年どう動いているか、統計系列を探して。
4. 中南米の女性労働参加率を国別・年別に比較して。

### 66. `development.idb` — IDB projects
1. ブラジルで進んでいる交通インフラ案件の規模と進捗を知りたい。
2. カリブ海諸国で、気候変動対策に資金を出しているIDB事業を探して。
3. 直近に承認された教育プロジェクトを国ごとに比べて。
4. ある国で完了した保健分野の案件と、現在実施中の案件を見比べたい：ペルー。

### 67. `econ.eurostat` — Eurostat
1. ドイツとフランスのインフレ率は、ユーロ圏平均と比べてどう？
2. EU各国の若年失業率を、最新年で高い順に見たい。
3. ドイツの地域別人口変化を、州に相当する地域単位で比べられる？
4. ユーロ圏で再生可能エネルギーの割合が増えたか、年次データを確認して。

### 68. `finance.ecb` — ECB reference rates
1. ユーロを円に替える目安として、今日のECB参考レートを知りたい。
2. ユーロ対ドルのレートが先週からどう動いたか、日ごとに見せて。
3. ユーロと英ポンド、スイスフランの参考レートを同じ日付で比べて。
4. 旅行の両替計画のため、直近のユーロ対円レートを確認して。

### 69. `politics.eu_parliament` — European Parliament records
1. 欧州議会でAI法の実施状況について最近どんな議論があった？
2. 欧州議会の委員会で、再生可能エネルギーに関する報告書や質疑を探して。
3. データ保護に関する決議が、議会内でどの段階まで進んでいるか知りたい。
4. 特定の議員が最近提出した移民政策関連の議会文書を見たい。

### 70. `law.eu_cellar` — EUR-Lex / CELLAR
1. AI Actの条文で、高リスクAIの事業者に求められることを確認したい。
2. このEU規則の公式番号、採択日、適用開始日を調べて：GDPR。
3. 消費者保護に関するEU指令の改正前後の公式文書を探して。
4. ある法令が現在も有効か、改正や統合版の公式記録を確認したい：AI Act。

### 71. `politics.uk_parliament` — UK Parliament
1. 英国議会で最近提出されたAI関連法案は、審議のどこまで進んだ？
2. 住宅政策について、政府に提出された最近の議会質問を探して。
3. 気候変動に関する法案の提案者と、直近の審議日を知りたい。
4. 英国議会でデジタル市場規制がどんな論点で議論されたか確認して。

### 72. `politics.br_chamber` — Brazil Chamber of Deputies
1. ブラジル下院で、AI規制法案にどんな変更案が出ている？
2. 教育予算に関する下院の法案と、委員会審査の状況を探して。
3. 最近提出された環境関連の議員提案を、提出者と日付付きで見たい。
4. ブラジル下院で労働法改正が審議された記録を調べて。

### 73. `politics.br_senate` — Brazil Senate
1. ブラジル上院でAI法案は、下院案と比べてどこが争点になっている？
2. 上院で審議中の最近の気候・森林保護法案を探して。
3. 年金制度に関する上院の採決や議案の進捗を確認したい。
4. ブラジル上院で議論された公衆衛生法案を、議案番号付きで見せて。

### 74. `politics.ca_openparliament` — Canadian Parliament
1. カナダ議会でAIと著作権の関係が最近どう議論された？
2. 気候政策に関する法案の提出者と、現在の審議状況を調べて。
3. 下院での住宅供給に関する採決や発言を、最近のものから探して。
4. カナダ議会の委員会が出したプライバシー関連報告書を見たい。

### 75. `politics.us_congress` — U.S. Congress
1. 米議会のAI法案で、超党派の共同提案者がいるものを探して。
2. 最近提出された医療費関連法案の審議段階と提出議員を知りたい。
3. 法案が委員会を通過したか、最新の議会アクションを確認して：S. 1234。
4. 米議会でデータプライバシーを扱う法案を、上下院別に比べて。

### 76. `policy.us_rulemaking` — Regulations.gov
1. 米国のAI規制案に対する意見募集で、締め切りがまだ先の案件はある？
2. 環境規則案について、一般から寄せられたコメント数と募集期間を確認したい。
3. 最近の規則案で、医療機器メーカーに影響しそうなものを探して。
4. ある規則が提案段階なのか最終化されたのか、公式 docket 情報で見分けたい。

### 77. `politics.us_campaign_finance` — FEC campaign finance
1. 2024年選挙で、候補者本人の選挙委員会と外部PACの収入を比べたい。
2. 大統領候補の選挙資金は、どの時期に大きく集まった？四半期ごとに見せて。
3. ある候補への個人献金と企業・団体関連の支出を分けて確認したい。
4. 2024年の主要候補について、資金調達額と支出額の差を比較して。

### 78. `gov.us_spending` — USAspending.gov
1. NASAがSpaceXに発注した契約のうち、金額が大きいものと目的を知りたい。
2. 2024年度の契約額と実際の支出額は、NASAから同じ会社にどれくらい出た？
3. 米政府機関別に、SpaceXへの契約がどこから出ているか比べて。
4. ある受注者への連邦契約を、年度と契約機関で絞って一覧にしたい：Space Exploration Technologies Corp.

### 79. `law.us_courts` — CourtListener
1. Section 230を引用した判決の中で、プラットフォーム責任を最近どう判断している？
2. 同じ法的論点について、連邦控訴裁判所と最高裁の判例を比べたい。
3. この判例の裁判所、判決日、引用関係を調べて：Gonzalez v. Google。
4. 著作権とオンラインサービスに関する最近の連邦判決を探して。

### 80. `defense.us_contracts` — U.S. defense contracts
1. 国防総省のLockheed Martin向け契約で、最近金額が大きかった案件を探して。
2. 2025年に契約された航空機関連案件を、発注機関と金額で見比べたい。
3. ある契約の変更通知で、追加額や作業場所がどう変わったか確認したい。
4. 防衛企業への契約がどの軍種から出ているか、最近の発表を分類して。

### 81. `sanctions.us_ofac` — OFAC sanctions list
1. Iran AirがOFACリストに載っているか、別名や制裁プログラムも含めて確認して。
2. 取引先の法人名に表記揺れがあるので、制裁リスト上の別名を探したい：Rosneft。
3. 指定対象の住所や識別番号が公開されているか確認して：Iran Air。
4. この団体に適用されるOFAC制裁プログラムと指定根拠を調べて。

### 82. `econ.fred` — FRED economic series
1. 米国の政策金利は、過去3年でどの局面に上がったり下がったりした？
2. 政策金利とインフレ率を同じ期間で並べて、金融引き締めの流れを見たい。
3. 2000年以降のFF金利を年平均で比較したい。
4. 次の会議の準備に、米国の長期金利と短期金利の最近の推移を確認して。

### 83. `econ.us_labor` — U.S. labor statistics
1. 米国の失業率を州ごとに比べて、最も高い地域を知りたい。
2. 建設業と医療分野で雇用者数が最近どう変わった？
3. 若年層の労働参加率が、ここ数年で上がっているか調べて。
4. 雇用統計の中で、平均時給と失業率を同じ期間で見せて。

### 84. `energy.us_eia` — U.S. EIA energy data
1. 米国の原油在庫は、過去数か月の季節パターンと比べて多い？
2. 米国の天然ガス生産と消費が、今年どう推移しているか見たい。
3. ガソリン価格の地域差を、州別データで確認して。
4. エネルギー輸入依存度を考えるため、米国の石油輸出入の月次変化を調べて。

### 85. `econ.jp_estat` — Japan e-Stat
1. 日本の消費者物価を、食料とエネルギーを分けて最近の動きを見たい。
2. 東京と全国平均で、物価上昇の傾向に差がある？
3. 世帯の年齢層によって消費支出の内訳がどう違うか、最新統計を探して。
4. 日本の人口が減っている地域を、都道府県別の統計で確認して。

### 86. `maritime.us_vessel_traffic` — MarineCadastre AIS
1. ニューヨーク港で貨物船の交通が多い水域を、年別に見たい。
2. 2023年と2022年で、港周辺の船舶通航密度はどう変化した？
3. 船種ごとに航行パターンが違うか、対象海域のAISデータで確認して。
4. 港湾計画の参考に、船舶位置の集中箇所を地図用データで取得したい：ニューヨーク港。

### 87. `aviation.operational_weather` — METAR/TAF
1. 羽田では今、着陸に影響しそうな風や視程の条件がある？
2. 現在のMETARと数時間先のTAFを比べて、天候が変わりそうか教えて。
3. 成田と羽田の風向・視程を比べて、運航条件に違いがあるか見たい。
4. RJTTの最新観測で、雲底と気圧がどう報告されている？

### 88. `knowledge.qiita` — Qiita
1. 日本語で、Next.jsのApp Routerへ移行した人の実例や注意点を探して。
2. Pythonで非同期処理が詰まる原因を、Qiitaの記事から初心者向けに調べたい。
3. ReactとVueでフォームの状態管理を比べた日本語記事はある？
4. 最近の投稿から、生成AIを使った開発で実際に役立った工夫を探して。

### 89. `geo.poi` — nearby places
1. 東京駅から半径1km以内の病院を5件探して。
2. 京都駅から半径2km以内にある学校を探して。
3. シアトル中心部から半径2km以内の薬局を探して。
4. 札幌駅から半径1.5km以内の店舗を地図で表示して。

## Execution ledger update (2026-10-09, r15)

- Built and launched current source as `olcr-fuzzy-352-20261009-r15` from the exact absolute app bundle path, retaining the isolated profile and the same existing projectless conversation. Keychain restore completed (counts only: web=1, external=10); backend reached READY.
- The first NOAA SWPC live pass (“今の太陽活動で、無線通信やGPSに影響が出そうな兆候はある？”) returned current Kp/G0, a cautious geomagnetic-storm assessment, and the Kp-only limitation. The r14 GUI had exposed three adjacent prompts whose answers were too generic/short despite HTTP 200: aurora likelihood, recent Kp trend vs usual level, and satellite operations.
- Added intent-specific NOAA rendering for aurora scope, comparison with the immediately preceding sample (explicitly not a long-term baseline), G1 threshold, and satellite-operations relevance/limitations. Added three deterministic regressions. Batch `test_external_api_expansion` + `test_fuzzy_api_routing_matrix`: 94 passed, 7 skipped.
- r15 same-conversation GUI retest passed all three: aurora answer does not infer regional visibility from planetary Kp and recommends OVATION/location; trend compares latest Kp 4 with previous sample Kp 2 and distinguishes that from a climatological baseline, with G1 at Kp≥5; satellite answer addresses geomagnetic-storm relevance and explicitly says Kp alone cannot assess radiation, charging, atmospheric drag/orbit change, or spacecraft-specific warnings. Each selected NOAA SWPC, compiled `space.space_weather` arguments, received HTTP 200, and produced visible typed output.
- Live queue: 9/352 prompts passed (worldwide holidays; four Launch Library 2 angles; four NOAA SWPC angles). Those nine are removed from the remaining live queue only; all fixtures remain. 343 prompts remain. No per-test conversation was created.

## Execution ledger update (2026-10-09, r17)

- Continued in the same projectless GUI conversation and isolated profile; no new conversation was created. The latest absolute-path `OLCR.app` build (`olcr-fuzzy-352-20261009-r17`) was running with its matching backend and profile.
- Retested all four `space.satellite_orbit` prompts in the real GUI. All visibly returned CelesTrak TLE data, and the backend log recorded deterministic `space.satellite_orbit` routing, compiled/validated arguments, HTTP 200 `text/plain`, and typed renderer PASS.
- Prompt 1 returned the current ISS TLE and correctly clarified that a pass-time estimate needs a location/time window and SGP4 propagation; it did not fabricate a Tokyo pass time.
- Prompt 2 returned both ISS (NORAD 25544) and Hubble/HST (NORAD 20580), their respective epochs and TLE lines, and warned that their snapshot epochs differ.
- Prompt 3 returned the latest ISS TLE but correctly stated that a current snapshot alone cannot establish whether the orbit recently changed; historical TLE snapshots are required.
- Prompt 4 returned both raw ISS TLE lines for downstream orbital calculations.
- This batch exposed a parser mismatch: CelesTrak returns plain-text TLE, whereas the adapter previously attempted JSON parsing. Added a bounded text-response request path for this provider and changed only the satellite adapter to use it; the targeted satellite tests and routing matrix pass, and the GUI confirms the fix.
- Live queue: 13/352 prompts passed (worldwide holidays; four Launch Library 2 angles; four NOAA SWPC angles; four CelesTrak satellite-orbit angles). Remove those 13 from the active queue; 339 remain. All fixture prompts remain in this document.

## Execution ledger update (2026-10-09, r18)

- Continued in the same projectless GUI conversation and isolated profile; no new project or conversation was created. Launched the r18 build from the stable absolute `OLCR.app` bundle path with its matching isolated backend/profile. Startup confirmed Keychain restore, exact API registry count (88), Laya readiness, and backend readiness.
- Retested all four `space.ephemeris` prompts together in the real GUI after the r18 fix. All visibly returned JPL Horizons Earth-centered distances with km and au, and the backend log recorded `space.ephemeris` routing, compiled/validated arguments, provider HTTP 200, and deterministic typed renderer PASS for every query (two provider responses for the two-body comparison and four for quarterly sampling).
- “今年の年末ごろ…” resolved to 2026-12-31 and Mars: 137,902,694 km (0.921823 au).
- “来月…” retained both Venus and Mars at the same date (2026-11-09), returned their distances, and correctly identified Venus as closer.
- The explicit Jupiter request honored 2027-01-15 and returned 668,813,401 km (4.470741 au).
- The quarterly Mars request expanded to Jan/Apr/Jul/Oct 2026 sample dates and visibly returned all four distances plus an explanation that the first day of each quarter-month is used.
- Fixes in this round prioritize dates stated in the user prompt over incomplete model arguments, preserve all user-mentioned bodies, expand quarterly requests into multiple epochs, parse and chronologically order the epoch rows, and render the requested dates alongside each distance. Targeted regressions and the full backend fuzzy-routing batch passed before the GUI rerun.
- Live queue: 17/352 prompts passed; 335 remain. Remove only these four ephemeris prompts from the active queue. Keep every fixture in this document.

## Execution ledger update (2026-10-09, r23)

- Reconciled the interim r21 count (347 remaining) against the later r15/r17/r18 batch ledgers: those completed batches subsequently raised the verified live total to 17/352, so the r21 figure is historical and superseded, not the current queue size.
- In the same existing projectless conversation and isolated profile, the absolute-path r23 build retested the first exoplanet angle. NASA Exoplanet Archive returned exactly TRAPPIST-1 b–h (7 planets), HTTP 200, correct host filter, and the visible answer now includes the Earth baseline (Earth radius = 1; Earth year ≈365.26 days). This case passes; cumulative verified live passes are 18/352 and 334 remain.
- The next angle (“公転周期が短い既知の系外惑星をいくつか見せて。”) exposed a live defect: despite HTTP 200, the visible preview started with HD 2039 b (1120 days), so the period intent was not applied to the NASA query. Keep this prompt pending.
- Backend changes now compile explicit period ascending, Kepler discovery-facility, Earth-like radius range/nearest-to-Earth ordering, and same-host-star filters. Regression tests assert each generated TAP query. Full offline batch `test_fuzzy_api_routing_matrix` + `test_external_api_expansion`: 108 tests passed, 7 skipped. The changed backend has not yet been included in a GUI build; all four exoplanet prompts other than the already-passed TRAPPIST system prompt remain in the live queue.

## Execution ledger update (2026-10-09, r25)

- r25 was launched from its exact absolute bundle path with Keychain restore enabled, the isolated profile cloned forward, and the same projectless conversation. Registry count remained 88; startup reached READY.
- The short-period probe passed in the GUI after the query now retrieves a larger candidate set and sorts the returned NASA rows by `pl_orbper` locally. The five visible results are ordered 0.685, 0.720, 0.882, 1.495, and 1.499 days; provider log records `space.exoplanets` and HTTP 200. Cumulative verified live passes: 19/352; 333 remain.
- The Kepler/Earth-radius probe still fails live: the UI shows a generic data-fetch failure and the backend records HTTP 403 HTML. A direct read-only query with the same honest OLCR client identity is denied by Cloudflare, while a browser-identifying User-Agent succeeds. We will not impersonate a browser or bypass the provider's anti-bot gate; keep this case pending as provider access restriction unless an official machine-client access route is available.
- The “same host star” probe correctly filters to TRAPPIST-1 but the GUI showed only b–f (5 rows), omitting g and h because the default result limit was applied. This is a product defect, not a pass. Backend now raises the default cap for an unbounded host-system request and has a regression asserting all seven synthetic rows survive. Offline full batch: 111 tests passed, 7 skipped. This host-limit change has not yet been GUI-verified; keep that prompt pending.
