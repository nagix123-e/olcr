# OLCR Mobile Client — Happierベース実リリース計画

初版: 2026-10-05  
再評価: 2026-11-10  
目的: 現行OLCRの実装・製品仕様とHappier/ACPの現行仕様を照合し、スマートフォンから自宅Mac上のOLCRを操作するアプリを、検証可能な段階に分けてリリースまで進める。

## 0. 2026-11-10時点の再評価

### 今回確認した基準

- Happier本体の基準は、前回計画の確認時点から更新された公開ドキュメント。Custom ACPは引き続きexperimentalで、session resumeは明示的な設定とACP `session/load` capabilityの確認を要する。
- OLCRソースは現在の作業ツリーを確認した。`olcr`リポジトリのHEADは`1556d62`（2026-09-21）で、tracked fileの広い変更と大量の生成物削除が未コミット。`docs/devlog/2026-11-09-default-mcp-connections.md`には実装・検証結果が記録されているが、作業ツリー全体をクリーンなリリース候補として再検証したものではない。
- READMEの配布例と、現行`pyproject.toml`/CLI versionにはバージョン表記のずれがある。リリース計画の起点は日付やdevlogではなく、意図したsource treeを固定し、release buildを再現したcommitに置く。

### 計画を変える実装事実

| 領域 | 現在確認した実装 | 方針変更 |
|---|---|---|
| 会話とタスク | SQLiteに`conversations`、`messages`、`tasks`、`coding_tasks`、`pending_actions`等があり、会話一覧/作成/取得、Chat SSE、coding task取得/phase report、cancel、confirmationのHTTP経路がある。 | ACP専用の会話/タスク保存層を新設しない。最初に既存の会話・タスク所有権をACP sessionへ対応づけ、同じ記録を使う。 |
| ストリームの寿命 | `/api/chat/stream`はSSE。通常生成ではHTTP generatorが切断/closeされた場合に`GeneratorExit`経路でgenerationをcancelし得る。イベント履歴をcursor指定で再生する一般的なstream APIは今回の確認範囲で確認できていない。 | 携帯電話の切断がHappier relay上だけで終わる通常ケースと、Happier daemon/ACP adapter/API stream自体が落ちるケースを分ける。前者は既存Happier同期に任せ、後者は明示的な再開・重複排除ができるかを最初の技術検証で確認する。生成を黙って再送しない。 |
| ローカルAPIの信頼境界 | CLIは`127.0.0.1`のランダムポートへbackendを起動する。Tauri desktopは`OLCR_GUI_SESSION_TOKEN`をbackendへ渡し、middlewareは設定時に`X-OLCR-Session`を要求する。health以外のtoken保護はtoken設定の有無に依存する。 | インターネット向けlistenerを追加しない。ACP adapter専用の認証済みローカル呼出し境界を決め、GUI tokenを探し出す/再利用する設計にしない。loopbackだけで十分とは扱わず、別プロセス呼出しの認証・port所有・DB競合を検証する。 |
| モデルと設定 | Brainは役割別model選択に加えて、effort、Quick、fallback設定を持つ。provider/model catalog、conversation-scoped config、purpose model controlsもある。既存ルーターがタスク種別を引き続き扱う。 | 前版の「effort未確認」は撤回する。ACP session configはOLCR設定全体を一対一で表現できる前提にせず、model/effort/Quick/fallbackの適用範囲と変更タイミングを明示する。 |
| 接続先とツール | Web/E2E、Computer Use、MCP server/connector、coding task等はOLCRの既存入口と固有の認可条件を持つ。2026-11-09のdevlogではMCP接続チェックとruntime packagingの更新を記録。 | 「ACPツール」として一括移植しない。OLCRの各能力を既存policy経由で呼べるか個別に対応表へ置き、リリース対象/非対応/OS許可必要を機能単位に表示する。 |
| Mobile上のAI | 既存read-aloudは現状Macの`/api/audio/synthesize/stream`を呼び、MacのLFM runtimeで音声を作ってPCMを返す。LFM2.5-Audioの日本語/英語model catalogは各`3,700,000,000` bytesを示す。 | MobileではOLCR Brain/LLMを実行せず、prompt/応答をMacへ送受信する。唯一のmobile model exceptionはread-aloud用LFM TTSで、端末内生成・再生を行う。 |
| Mobile TTS runtime | 現行LFM実行器はPython MLX/Torchと専用MLX子プロセスを使い、今回確認範囲ではiOS/Android runtimeはない。 | 既存Mac MLX実装がmobileへそのまま移せるとは仮定しない。iOS/Android native inference bridge、RAM/disk、streaming latency、thermals、licenseを先にspikeする。音声入力/ASRはmobile対象外。 |
| アカウントと端末リンク | Happierはdevice-key accountを使う。メール/外部IdPなしの匿名signupは可能だが、技術上はrelay内にaccount identityを作る。既存のAdd your phone QRは、ログイン済み端末の承認を経て新端末を同じaccountへ参加させる。 | 利用者にはメール/パスワード/OAuthでの登録を求めず、PCがpairing用device-key identityを準備し、QRと短い確認コードでスマホを接続する体験を目標にする。relay同期を保つため内部identityは残る。既存QR/restore flowを自動化できるかPhase 1で検証する。 |
| 記憶と履歴 | 会話メッセージ、task/coding task、Conversation Memory、project context、World Stateは異なる保存対象として存在する。 | HappierへSQLiteや検索index全体を複製しない。まず会話履歴のsource-of-truthとOLCR側のmemory/context使用を定義し、relayへ同期するsession contentの最小範囲を決める。 |
| ACP | PC側にACP v1 bridge、専用loopback認証、PC所有run/event cursor、Happier QR pairingを追加。スマホ側はUIのみ。 | 実装範囲と未対応は[foundation実装記録](OLCR_MOBILE_FOUNDATION_IMPLEMENTATION.md)を参照。Happier daemon自動登録・スマホ通信・端末単独revokeは引き続きrelease gateとする。 |

上記はソースを読むことで確認した範囲であり、既存APIが第三者ACP adapter向けの安定contractとして設計済みであることを意味しない。ここでテストを追加・実行していないため、devlog記載のPASSは現在の作業ツリー全体に対する本評価の再検証結果ではない。

### 維持する方針と変更する方針

**維持:** Happier forkをスマホクライアント基盤にする。Happierのdaemon/relay/E2EE sync/reconnect/notifications/approvalsを使う。OLCRの実行・モデル・Keychain・workspace・権限はMacに置く。OLCR固有UIをOLCR moduleに隔離し、Happier shared UIを不用意に変更しない。

**Mobile上のAI実行範囲:** mobile appにはOLCR Brain/LLM、routing/planning/coding、vision、embedding/retrieval modelsを同梱せず、取得可能にもせず、スマホで実行しない。promptはHappier経由でMac上のOLCRへ渡し、OLCRの応答・tool/task状態を受信する。モデル同梱の唯一の例外は読み上げ専用LFM2.5-Audio TTSで、受信したユーザー向けassistant textを端末内で音声化し再生する。ここでいう「同梱」はOLCR mobile releaseに署名・revision固定されたTTS配布assetとして含める意味とする。約3.7 GB/modelのため初期アプリ実行ファイルへ全モデルを埋め込まず、端末の選択言語に合うassetを公式配布経路から取得し、完了後はオフラインでも使えるようにする。別言語assetの追加取得は任意とし、OS/ストアの制約を満たすことをPhase 0で確認する。TTS本文/音声をHappier relayへ送らない。音声入力/ASRはこの例外に含めない。

**採用方針 — アカウント登録なしの初回接続:** 利用者にはアカウント作成を求めず、PCのOLCRから「スマホを接続」を選び、スマホでQRを読み取り、PCとスマホに出る短い確認コードを照合して接続する。Happierのrelay同期・E2EE・通知・catch-upを維持するため、内部にはOLCRインストール単位のdevice-key account identityを使う。これはメールや外部IdPを伴わない自動生成の内部IDであり、厳密な「サーバー上にもaccount recordを一切作らない」方式ではない。pair前に暗号化同期用identityとrelay利用を一文で説明し、利用者が接続を確定する。既存Happierのdevice-link flowを使ってこの導線を実現できるか、初期identity作成がauth lifecycleに沿うかをPhase 1で検証する。QRには短命・一回限りのpairing requestだけを含め、PC承認後に失効させる。接続解除・紛失端末のrevokeをPCから行えるようにする。

**変更:** ACPの前にOLCR実行基盤を新しく作る計画から、既存conversation/task/APIを使うACP bridgeの縦切り検証へ変更する。まず“電話が切れてもdaemonはstreamを消費し続ける”標準経路を通し、daemon/adapter/API再起動を別の回復契約として設計する。すべてのOLCR機能を初回対応とせず、ツール/承認/ファイル差分を能力単位で追加する。Happier Custom ACP generic UIだけでOLCR体験を完成させる考えは採らず、Happier mobile clientに専用session presentationを持つ。

**スマホUIの採用基準（ユーザー確定）:** 現行OLCR PC版の画面構成・用語・部品・機能を基準にし、ほぼ同じ操作をスマホ縦画面へ最適化する。チャットを初期画面に保ち、プロジェクト/会話のサイドバーを開閉式にし、入力欄の＋/権限/Brainを残す。独自のセッションカードhomeや下部タブを中心にした別UIは採らない。設定・モデル・サービス・記憶・スキル・MCP・カスタムエージェントの情報階層もPC版に合わせる。UI-only stageのローカル操作と、PC通信が必要な機能を表示上で区別する。

## 1. 計画上の結論

OLCRモバイル版は「OLCR本体をスマートフォンへ移植」するのではなく、**Mac上のOLCRランタイムをHappierのdaemon経由で動かし、Happierのモバイルクライアントからプロンプト送信・返答受信を行う構成**を基本線とする。Happierの既存daemon/relay/暗号化同期/再接続/通知/承認機構を使い、OLCRのBrain・推論・ルーティング・tool実行・権限・作業状態はMacに残す。mobileに含める唯一のモデルはread-aloud用LFM TTSで、モデルassetを端末へ取得した後に端末内で音声化する。初回接続では利用者がHappier accountを手入力で作らず、PCからQR pairingする体験を優先する。ただしHappier relayを使うため内部device-key accountは自動作成し、メール等のログイン資格情報は求めない。

ただし実装着手時は、現行OLCRのconversation/task APIを薄いACP bridgeから安全に利用できるかを最初に検証する。新しい会話・実行基盤は作らず、現行HTTP/SSE境界が長時間実行や再接続を支えられないと確認された場合に限り、OLCR内部のdurable service boundaryを補う。HTTP APIをLAN/WANへ公開する構成は採らない。

製品成果物は、(1) バージョン固定されたHappierフォーク、(2) OLCR ACPアダプター/CLI、(3) Mac側インストールと接続手順、(4) iOS/Android配布パッケージとストア資料、(5) 互換性・セキュリティ・リリース証跡、の5点とする。

## 2. 現状照合

2026-11-10時点の確認事項と、今回の方針変更の根拠は「[2026-11-10時点の再評価](#0-2026-11-10時点の再評価)」にまとめた。特に、現行APIをACP adapterから呼ぶ契約は未検証であり、Happier/OLCR双方のclean release baselineも未確定である。以下の実装計画はそれらを前提条件としている。

## 3. 目標構成と責務

```text
OLCR iOS/Android client (Happier fork)
  ├─ OLCR mobile experience: session list, conversation, approvals, artifacts
  ├─ background device-key identity; no email/password/IdP setup in first-run UX
  ├─ QR pairing approved by the OLCR PC; revoke paired phones from the PC
  ├─ Happier encrypted sync, reconnect, notifications, device/session controls
  ├─ no OLCR Brain/task models or inference; prompts and agent work execute on Mac
  ├─ bundled LFM2.5-Audio TTS assets + mobile-native inference/audio bridge for read-aloud only
  └─ minimal pairing/status setup (relay/account provisioning handled by the PC flow)
                  │ authenticated encrypted WebSocket + catch-up
Happier relay (official hosted or self-hosted)
                  │ authenticated daemon channel; Mac-side process control
Happier daemon on home Mac
                  │ stdio ACP, one subprocess per OLCR session
OLCR ACP bridge/CLI (macOS, launched as a Happier daemon child)
  ├─ ACP initialize/session/new|load/prompt/cancel and capability negotiation
  ├─ persistent mapping: Happier session ↔ ACP session ↔ OLCR conversation/task
  ├─ translates OLCR SSE/task/approval/artifact state to ACP updates/requests
  └─ uses an authenticated private local OLCR boundary; never a public API
                  │ stable internal service interface preferred;
                  │ existing loopback API is the first spike, not assumed contract
OLCR runtime + existing SQLite, Keychain, workspace/OS permission boundary, local models
```

Pairing flow: `OLCR PC first-run → provision internal device-key identity + register daemon → show short-lived QR → phone scans and confirms → PC approves → phone joins the same encrypted relay identity`. The QR is a pairing request, not a reusable bearer credential. Happier distinguishes Add your phone (account device-link/restore) from terminal QR (daemon authorization); the OLCR flow must use the phone device-link trust semantics. To retain access away from home, the phone and Mac still need a reachable Happier relay (hosted by default or configured self-hosted); a local-only direct connection would be a separate LAN-only mode.

Mobile read-aloud flow: `Mac OLCR assistant text → Happier session sync → mobile text filter/chunker → local LFM TTS → device audio output`. The current OLCR desktop endpoint `/api/audio/synthesize/stream` remains the desktop implementation; the Happier mobile client must synthesize locally and must not call that endpoint for read-aloud. Only the TTS function is exposed on mobile; microphone/ASR is outside this model exception.

**安全境界:** relayはジョブ実行主体ではない。ACPプロセスとモデル推論はMacで実行する。adapterはOLCR APIをLAN/WANへ公開しない。既存GUI session tokenをACPへ渡すだけで安全と判断せず、専用ローカル認証/所有権・lifecycleを検証する。Remote accessはHappierの認証済みdaemon/relay経路だけとし、self-host時も公式TLS/認証/長時間WebSocket要件に従う。資格情報・SQLite・検索index・画面キャプチャ全体を同期対象にしない。

## 4. 段階計画とゲート

### Phase 0 — 製品基準・リポジトリ固定

**作業:** Happierの具体的なcommit/tagをfork基点に固定し、Custom ACP/runtime、mobile build、daemon、notifications、approval、account/device-key auth、QR pairing、license/noticeを棚卸しする。OLCR作業ツリーから意図した変更を選び、generated artifactsを除くrelease candidate branchを作る。README/CLI/pyprojectのversionを揃え、clean checkoutからmacOS bundleと必要なunit/release validationを再現する。OLCR側は既存Chat SSE、conversations/tasks/coding_tasks/confirmation、Brain role/effort/fallback/Quick、MCP/Web/Computer Use権限の対応表を作る。PC UIを実画面でlight/darkと主要状態ごとに記録する。pairing時のidentity生成、鍵保管/recovery、phone交換、PCからのrevokeも設計する。

**成果物:** upstream commit manifest、OLCR↔Happier capability matrix、data-flow/sequence図、UI inventory、mobile model exclusion/allowlist、LFM model revision/license/asset-size inventory、account/pairing identity lifecycle、recovery/revoke matrix、ライセンス/notice inventory、リスク登録簿。

**ゲート:** clean release candidateとversion manifestが確定。OLCRのconversation/task ID、stream owner、cancel/confirmation/plan approval経路が根拠つきで追跡できる。ACPから呼び出せる既存境界・不足差分を特定済み。ユーザー登録フォームを出さずにdevice-key identityを安全に準備できるauth lifecycleとrelay到達性を確認済み。実現できない場合はこの接続UXを維持できるHappier fork側の対応範囲を見積もり、未認証listenerを追加せずに解決する。Happier license/third-party notices/商標とapp-store obligationsの担当が確認済み。

### Phase 1 — ACP相互接続スパイク（UI拡張より先）

**作業:** まず既存OLCR APIを使うthrowaway ACP vertical sliceで、Happier Custom ACPからconversation作成→`/api/chat/stream`→ACP agent message updates→完了まで通す。次にconversation list/reload、task status/progress, cancel、pending confirmation/plan approvalを一つずつmappingする。stdio JSON-RPC framing、stderr/stdout分離、capability negotiation、timeout、child lifecycleを確認。APIをACP adapterが直接使うcontractとして維持できるか、auth token lifecycle、multi-process SQLite、SSE disconnect semantics、resume時の重複副作用を判断する。adapterは`session/load`を実装できるまでHappierでresume supportを有効にしない。

**Pairing vertical slice:** 利用者にemail/password/IdP入力を求めず、OLCR PCから内部device-key accountを準備してQRを出し、スマホが読み取ってPC承認で参加するまでを検証する。pair requestのTTL/一回使用、confirmation code、鍵の安全な受け渡し、期限切れ・再発行・revokeを含める。既存Happier APIでaccount restore操作が必要な場合、認証lifecycleを壊さずUIから隠せるか判断する。できなければ独自の永続認証APIを安易に追加せず、fork維持コストと実現可能な代替を示す。

**セッション対応表:** Happier session ID ↔ ACP session ID ↔ OLCR conversation ID ↔ OLCR task/coding-task IDをMac側に永続化する。cwd/workspaceはOLCRが認可したprojectだけを使う。Brain role/effort/Quick/fallbackは「session作成時に適用した値」と「実際のtask route/actual model」を別々に記録する。途中変更はOLCRの既存設定APIが適用タイミングを保証する項目に限る。

**ゲート:** 新規/既存会話、stream、tool/task status, approval/confirmation, cancel, reconnect/catch-upがend-to-endで成立。電話の切断中にdaemonがstreamを保ち生成が続くことと、daemon/adapterの再起動後に`session/load`で履歴を復元することを別々に証明する。ユーザー登録フォームなしのQR pairingが安全に成立し、端末紛失後にPCからrevoke・再pairできる。重複promptを抑止し、非対応機能を機械可読manifestとUIに出す。resume実装前はresume capabilityをadvertiseしない。

**停止条件:** 現行SSEの寿命がphone/relayとは独立したdaemon側ownerに結びつかない、あるいはresume後の既存event stateを安全に復元できない場合、実装固有の回復ロジックをACP adapter内に増殖させない。OLCR側にdurable run/event service boundaryを作ってから再開する。ローカルRESTの公開やUI専用token流用でspikeを通過扱いにしない。

### Phase 2 — Mac実行基盤とセッション信頼性

**作業:** Happier daemonからOLCR ACP launcherを検出・起動する。OLCR/Happier/ACP/adapter versionを検査する。電話切断時にdaemon childを存続させる。daemon/adapter再起動時にOLCR conversation/task stateとHappier catch-upを突合できる必要がある場合は、OLCR側にdurable event cursor/idempotencyを追加し、streamのresumeと副作用の重複防止を実装する。既存MacへのHappier daemon/OLCR adapter導入、初回workspace認可、OS Keychainへのdevice-key保管、PCからのQR発行・pair/revoke画面、モデル準備、upgrade/rollbackを整える。標準ユーザーにrelay URLやtoken入力を要求しない。

**ゲート:** phone network loss/app suspension does not terminate the Mac-owned task. Catch-up restores one ordered session timeline with current task/approval state. daemon/adapter/API restart behavior is explicitly tested or clearly bounded; replay cannot re-run a write/action. Version mismatch blocks launch with a repair path, and OLCR/adapter rollback is available.

### Phase 3 — OLCRモバイル体験と情報設計

画面設計を固める前に、LFM TTSを端末内で実行する小さな技術spikeを行う。現行のJapanese/English model IDs/revisionsそれぞれについてiOS/Android native runtimeの候補、weightsの形式、RAM/working set、冷起動とfirst-audio latency、streaming、短文/長文品質、発熱/電池を確認する。assistantの表示可能な文章だけを分割してTTSし、reasoning/tool/private dataを読み上げない。生成textとaudioは端末内に保持し、relay/backendへ送らない。現行の約3.7 GB/modelというcatalog sizeに基づき、app本体とassetの実圧縮配布サイズ、展開後容量、一時領域、差分更新サイズを計測する。既定は端末の選択言語に合う一つのmodel assetを取得し、他言語は利用者が追加選択した場合のみ取得する。iOSは現行のBackground Assets経路、Androidはアプリ種別で利用可能なPlay配布方式または署名検証付き公式asset配布方式を調査する。対応OS/ストアごとの上限と、モデルを一度に取得・更新する条件を実配布形式で検証する。量子化や別artifactを使うなら品質差とLFM license条件を別途評価する。

**画面構成案:**

1. **First connect + workspace:** 初回は「PCのQRを読み取る」から始め、アカウント登録/サインイン画面を出さない。pairing後はPC版と同じチャット画面を開く。プロジェクト/会話を開閉式サイドバーへ置き、選択・新規作成・名前変更・pin・状態確認へ進める。独立したカード一覧homeは追加しない。
2. **Session:** PC版のheader、user/assistant表示、Coding Orchestrator進捗、承認/質問、＋/権限/Brain付きComposerを保つ。横並び領域を縦方向/開閉領域へ整理し、safe area/keyboardへ追従する。
3. **Task details sheet:** plan/progress、ツール実行、ログ、結果を折りたたみ表示。
4. **Review:** 変更ファイル一覧→差分→ログ/結果へ段階移動。大きなdiffはファイル単位、コードは横スクロールとline numberを保つ。
5. **Approval/question:** 内容、対象、権限/副作用、要求元を明示し、許可/拒否または回答を一件ずつ確認できる高優先シート。通知から対象sessionと要求へ直行。
6. **Brain controls:** 新規session時に既存OLCRの役割別モデル/effort、Quick、fallbackを理解できる選択面。詳細はadvanced sheetへ。設定済みfallback chainを勝手に変更せず、実際にfallbackした場合はどのモデルへ切り替わったかsession timelineに表示する。
7. **Read-aloud assets:** LFM TTSの未取得/取得中/利用可能/更新失敗/空き容量不足を示し、必要容量・進捗・再試行を確認できる設定面。モデル取得前もprompt/replyは利用可能にする。

**デザイン方針:** OLCRの明るい作業面、濃い文字、ライム系アクセント、控えめな罫線と既存用語をスマホ幅向けに再構成する。Happierの共通tokens/primitiveは維持し、OLCR専用brand/semantic token層で割り当てる。共有UIの色や余白を直接差し替えない。ライト/ダークは両方を設計・検証し、OLCR PCで未定義のdark paletteは勝手な反転で確定せず、まず意味色の候補を比較して決定する。

**ゲート:** iOS/Android実機で小/大画面、安全領域、キーボード中、VoiceOver/TalkBack、文字拡大、画面回転、オフライン、長いtool出力、空/失敗/承認状態を確認。web/desktopの共有UIにOLCR専用token差分が漏れないことを確認。

LFM TTS gate: both app targets have a supported native runtime; user-facing answer text streams to audio without returning to the Mac; stop/pause/error/background behavior is correct; locale-matched TTS asset is delivered through a supported signed route, passes checksum/revision/license validation, and works offline after download; measured package/expanded size, memory/latency/power are acceptable on supported devices. Until it passes, remote prompt/reply remains usable and read-aloud is explicitly marked unsupported on that device/build.

### Phase 4 — 通知・承認・作業回収

**作業:** OLCRのtool confirmation (`pending_actions`)、coding plan approval / scope authorization、question/user clarificationを分類し、それぞれACP request typeとHappier UI affordanceを決める。model/provider OAuthやKeychain setupはsession approvalと混ぜない。要求ID、expiry、already-answered、reject/cancel outcomeを一意に扱う。push payloadはopaque ID中心とし、本文取得はauth後。notification deep linkはsession/requestへ直行。追加指示はOLCR/ACPの実行中変更が確認できない限りnext-turn queueとして表現する。

**ゲート:** 承認待ちを一覧・session内・通知で見落としにくい。別デバイスで既に応答した要求への二重回答を拒否。拒否、期限切れ、接続先Mac offline、adapter再起動の回復状態が表示される。通知許可を拒否してもアプリ内で承認待ちを発見できる。

### Phase 5 — Hardening・互換性・配布準備

**作業:** ACP protocol conformanceとproperty-based event replay、relay/daemon reconnect matrix、負荷試験（長いsession、連続toolイベント、大きいdiff）、権限境界レビューを実施する。暗号化対象/メタデータ、通知情報、ログのPII/secret、Keychainを確認。依存license/SBOM、Happier MIT attribution、OLCR Apache-2.0表示、LFM Open License v1.0/model attribution、third-party notices、上流由来改変記録を整備。iOS bundle ID/署名/provisioning、Android application ID/signing、privacy manifest/data safety、通知資格、クラッシュ報告/サポートURL、利用規約/プライバシー、アイコン/スクリーンショット、ストア審査用説明を準備。LFM weightsを公式ストア配布assetとして含められる方式と展開サイズをplatform別に確認し、model revision/checksumをrelease manifestに固定する。未notarizedの現OLCR macOS配布状態を踏まえ、Mac adapter/daemon launcher配布を署名・notarizationするリリース工程を追加する。

**公開形態:** internal TestFlight/Play closed test → 少数のMac/phone構成でbeta → production rollout。最初の公開版はmacOS Apple Silicon host + iOS/Android phoneを明確な対応範囲とし、Linux/Windows hostやHappier全provider動作を暗示しない。初回セットアップから既存OLCR PC上のQR pairingまでemail/password/OAuth入力なしで進み、端末紛失・revoke・再pairを検証する。

**ゲート:** clean install、upgrade、rollback、認証喪失、端末交換、relay復旧、Mac再起動、通知deep link、低電力/バックグラウンド復帰のリリース候補試験が合格。iOS/Android store-package and expanded asset totals stay within current platform limits. Supported-locale LFM asset is install-time or store-delivered, revision-pinned, and works offline once installation completes. 脆弱性対応窓口と互換サポート期間を公開。

### Phase 6 — 段階リリースと運用

内部配布、招待制beta、一般公開の順に進める。クラッシュ率、daemon online率、ACP起動失敗、event catch-up差分、承認deep-link到達率、重複/欠落レポートを個人情報や本文を収集せず集計する。各段階で即時rollback条件（ACP起動失敗、session欠落、認証/暗号化問題、誤った権限要求）を定義。Happier上流の追従は固定版更新ブランチで行い、OLCR forkへ直接大量cherry-pickしない。

## 5. 初回公開範囲と後続範囲

### 初回公開に必要（段階rollout）

- OLCR conversation start / prompt / streaming / stop / reconnect / catch-up。
- session list state、Mac online/offline、project/workspace identity。
- coding task/tool progress/result/error、approval/confirmation、notification deep link。
- Brain model/provider/role/effort/fallback/Quick設定の可視化と、現在対応する範囲の変更。
- Mobile appにOLCR Brain/task modelsを含めず、推論はMacへ送る。
- 日本語/英語のLFM2.5-Audio TTS assetをOLCR mobile releaseで署名・revision固定して配布する。端末の選択言語assetを取得して端末内でread-aloudし、取得完了後はネットワークなしで利用できる。他言語assetは任意取得とし、実圧縮サイズ・配布条件をPhase 0で通過した言語だけを初回対象にする。ASRはmobileで公開しない。
- file/artifact/log/diff閲覧。
- Computer Use/Web/MCP等はそれぞれのOS permission, target scope, policy, persistenceが検証済みの能力だけ有効化。未対応表示は同じsession画面から発見できる。
- Mac側OLCR/daemon導入・認証・トラブルシュート。
- 初回に利用者がHappier accountを作成・記憶しなくてもよいQR pairing。内部のdevice-key identityはrelay同期のために自動作成する。
- Happier relay認証・E2EE同期、self-host選択をするなら公式に準拠したTLSと認証。遠隔接続ではrelayまたは設定済みprivate networkが必要。
- 未対応機能とversion非互換をUIで明示。

### 初回から外し、明示的にunsupportedとする候補

ACP/Happier/OLCR双方で能力・権限・resumeが確認できるまで、任意MCP追加/管理者shell、スマホからのKeychain秘密値管理、desktop capture frameの常時同期、Happier agent delegation、unverified session resume、実行中prompt書換えを有効にしない。EffortはOLCRで現にサポートされるが、ACP mappingを確認するまではread-only表示またはsession creation時の値に限定する。既存Computer Use/Web/MCPを削除せず、capability-specific support stateを出す。

## 6. 横断受け入れ基準

1. phoneから新規OLCR sessionを開始し、選択したworkspace/Brain設定（Mac上で使うモデル/role/effort等）でMac上のモデルが動作する。mobileにBrain modelのweight/download/inferenceが存在しない。
2. text stream、tool start/update/result/error、完了/キャンセルが順序どおり表示される。
3. approval/questionを通知とアプリ内から一度だけ応答できる。
4. phoneを切断・ロック・バックグラウンド化し、回線を切り替えても、Macの処理を止めずに同じsessionを復元する。
5. event重複・遅延・順序逆転・daemon再起動・Mac再起動のテストで欠落/重複副作用を許容基準内に抑える。
6. workspace外ファイル、未許可Computer Use target、public endpoint、秘密情報経路が既存OLCR policyを迂回できない。
7. OLCRの語彙・状態分類・Brain設定を保ったスマホ情報設計を、実機で確認する。
8. Happier共通UI変更はmobile専用scopeに閉じ、通常のHappier desktop/webの画面差分を作らない。
9. OLCR Apache-2.0、Happier MIT、LFM Open License v1.0とmodel attribution、依存物license/著作権表示を配布物内に保持する。商標・ロゴの利用条件は配布前に別途確認する。
10. インストール可能な署名済みiOS/Android package、Mac companion手順、公開support/privacy/legal資料が揃う。
11. 端末内read-aloudが配布した選択言語LFM TTS assetで動作し、モデル取得後はオフラインで音声化できる。端末上に存在するOLCR用AI modelはread-aloudのLFM TTSだけで、prompt・生成・tool処理はMacのOLCRが担う。
12. 初回接続で利用者にemail/password/OAuthを求めず、暗号化同期用identityとrelay利用を簡潔に説明した上で、PCの短命QRとPC承認だけで接続できる。QRは一回限りで、PCから端末revoke・再pairできる。relay上には内部device-key identityがあることをプライバシー資料に記載する。

## 7. 主なリスクと先行判断

| リスク | 重大度 | 対応/判断期限 |
|---|---|---|
| OLCRの会話/task APIをACP adapterから安全・継続的に呼べるか未検証 | 高 | Phase 0でclean release candidateを固定し、Phase 1のvertical sliceで認証/lifecycle/SSEを検証。必要な場合のみdurable service boundaryを補う。 |
| ACP Custom統合が実験的で互換性変動 | 高 | Happier commit + ACP version固定、conformance suite、互換matrix、更新は検証後のみ。 |
| OLCR専用UIをCustom ACP generic pathだけでは表現できない | 高 | OLCR機能をHappier mobile feature moduleとして追加し、Custom ACP登録とは分ける。 |
| OLCRの実行中状態とHappierのsession/message semanticsが一致しない | 高 | 初回は非対応項目を隠さず説明。暗黙のfallbackや再実行を禁止。 |
| Mac daemonの停止/スリープで実行継続が途切れる | 高 | 実行所有者/daemon/host sleepの実機条件を仕様化。スリープ中継続を約束しない。復帰時に状態検査。 |
| notification本文やログから機密が漏れる | 高 | push payloadはopaque ID中心、本文取得は認証後、redacted logging、暗号化/retention確認。 |
| 既存モデル設定をHappierの単一model pickerに潰す | 中〜高 | role-scoped model/effort、Quick、fallbackとrouterの責務を保持し、provider catalogとのmappingを検証する。 |
| iOS/Android配布・署名・プライバシー準備の遅れ | 中〜高 | アダプタ検証と並行し、Phase 0からbundle/signing/store checklistを起票。 |
| LFM TTS assetが日本語/英語各約3.7 GBで、アプリ容量・更新容量・端末空き容量・OS別配布方式に制約がある | 高 | Brain modelは一切含めず、選択言語のTTS assetのみ取得する。Phase 0でiOS/Androidの最新公式配布方式、実圧縮/展開サイズ、初回取得と更新要件を確認。対象言語で配布/実機条件を満たせなければ、別言語提供は延期し、TTS非対応状態を明示する。 |
| 現行MacのPython MLX/Torch LFM実装がiOS/Androidへ移植できない | 高 | Phase 3より前に各OSのnative runtimeとLFM weights変換可否をspikeする。動作するruntimeと品質基準が通らない端末では、prompt/replyを維持してread-aloudをunsupported表示する。 |
| device-key identityの自動準備と端末linkingが現Happier auth lifecycleに適合しない可能性がある | 高 | 採用する「登録フォームなしのQR pairing」をPhase 1で検証し、既存device-link API/UIの再利用かfork側の最小変更かを決める。未認証listenerや公開可能な長期tokenで近道しない。 |
| QRを盗み見られる/誤承認されると端末を追加される | 高 | QRは短命・一回限り、コード照合とPC側承認を必須にする。pair完了後に失効し、PCから端末名確認・revoke・再pairを可能にする。 |

**提案する初回計画の順序:** Phase 0 → Phase 1 → (Phase 2 と UI情報設計を並行) → Phase 3 → Phase 4 → Phase 5 → internal/beta/production。Phase 1のゲートが通る前にモバイルUIの本実装を大規模に始めない。

## 8. 参照資料

### OLCRリポジトリ内

- [OLCR README](../README.md) — 現行OS、インストール、モデル、ワークスペース、ライセンス。
- [Unified implementation status](OLCR_UNIFIED_IMPLEMENTATION_STATUS.md) — 機能の実装/未完了範囲と証跡。
- [Unified readiness matrix](OLCR_UNIFIED_READINESS_MATRIX.md) — P/V/A/R/W/U gates。
- [Unified architecture prompt v3](OLCR_UNIFIED_ARCHITECTURE_IMPL_PROMPT_V3.md) — Chat-first、権限、既存実行器再利用。
- [Computer use autonomy plan](computer-use-autonomy-plan.md) — conversation-owned state/reconnect/stopの現在の設計課題。
- [2026-11-09 MCP connection validation](devlog/2026-11-09-default-mcp-connections.md) — current worktreeのMCP connector/runtime検証記録（全体release baselineの証明ではない）。
- [LFM purpose-model catalog](../backend/olcr_api/purpose_models.py#L499) と [LFM language model IDs](../backend/olcr_api/audio_transcription.py#L13) — OLCRが日本語/英語に別のLFM2.5-Audio modelを割り当て、各3,700,000,000 bytesとしてcatalog化している根拠。
- [Current read-aloud client](../frontend/src/readAloud.ts#L11) と [current synthesis route](../backend/olcr_api/app.py#L21003) — 現状の読み上げがdesktop frontendからMacのstreaming synthesis APIを利用している根拠。

### 外部仕様（2026-11-10に確認）

- [Happier DESIGN.md](https://github.com/happier-dev/happier/blob/dev/DESIGN.md) — canonical tokens/components、mobileは縮小版にしない、連続性・回復・アクセシビリティ。
- [How Happier fits together](https://docs.happier.dev/self-hosting/how-it-works) — host daemonで実行、relayは同期、WebSocket本経路、seq catch-up/reconcile。
- [Happier onboarding](https://docs.happier.dev/getting-started/onboarding) と [server auth](https://docs.happier.dev/self-hosting/auth) — 現在のrelay選択とaccount/device-key authの前提。通常onboardingではrelay accountを使う。
- [Happier device linking and restore](https://docs.happier.dev/accounts/device-linking-and-restore) と [App authentication](https://docs.happier.dev/apps/app) — Add your phone QR、restore approval、terminal QRとの違い。device linkingはaccountなしのLAN pairingではない。
- [Happier Custom ACP](https://docs.happier.dev/agents/custom-acp) — 実験的な一般ACP backend登録、専用provider UIではない。
- [Happier feature matrix](https://docs.happier.dev/agents/feature-matrix) — ACP backendを含む現在のexperimental/stable範囲。
- [Happier self-hosted Docker](https://docs.happier.dev/self-hosting/docker) と [environment variables](https://docs.happier.dev/self-hosting/env) — self-hosted server構成・運用の確認先。
- [ACP session setup](https://agentclientprotocol.com/protocol/v1/session-setup) — initialize capability、session/new/load/resume、履歴再生規則。
- [ACP overview](https://agentclientprotocol.com/protocol/v1/overview) — protocol framing/roles/communication。
- [LFM2.5-Audio-1.5B-JP model card](https://huggingface.co/LiquidAI/LFM2.5-Audio-1.5B-JP) と [LFM2.5-Audio-1.5B model card](https://huggingface.co/LiquidAI/LFM2.5-Audio-1.5B) — TTS model ID、公開サイズ、適用license/attributionをrelease時に照合する。
- [Apple-hosted Background Assets overview](https://developer.apple.com/help/app-store-connect/manage-asset-packs/overview-of-apple-hosted-asset-packs) と [On-demand resource size limits](https://developer.apple.com/help/app-store-connect/reference/app-uploads/on-demand-resources-size-limits/) — asset配布方式とOS世代別上限を確認する。iOS 27以降はOn-demand Resourcesがdeprecatedと記載されているため、最新のBackground Assets経路を優先評価する。
- [Apple: Reducing the Size of Your Core ML App](https://developer.apple.com/documentation/coreml/reducing-the-size-of-your-core-ml-app) — 大きなモデルのdevice download/compile方式の参考。
- [Android App Bundle FAQ](https://developer.android.com/guide/app-bundle/faq) と [Play Asset Delivery](https://developer.android.com/guide/playcore/asset-delivery) — app/module download limitと大容量asset配布方式を確認する。Play Asset Deliveryのアプリ種別要件を含め、OLCRアプリで利用可能な経路をPhase 0で検証する。

## 9. 計画の限界

この文書は現在仕様に沿った実装計画であり、ACP adapterやモバイルアプリを実装済みとはしない。Happier上流の現行commit・アプリストア審査条件・Apple/Google署名要件は変化するため、Phase 0と配布直前に再確認する。OLCR側のroute/service契約やapproval/question表現は、現ソースの追跡後にphase taskへ落とし、推測で互換性を約束しない。
