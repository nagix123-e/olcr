# Graphify を OLCR のコード文脈検索に統合する計画

状態: 2026-10-05 に基盤実装と通常／明示検索／ストリーミング接続を追加。実モデル・配布GUIの受入ゲートは未完了。対象は Graphify Labs の OSS `graphifyy` / `graphify`。Graphiti は対象外。

## 2026-10-05 の採用設計・運用手順

ユーザーの追加方針に従い、検証済み Graphify をプロジェクト RAG の正とする。`graphify_enabled=true` が既定。ただし索引のないプロジェクトには従来検索を残す。グラフが存在しても旧形式・破損・ルート不一致なら明示的に fallback を記録する。正当な索引で該当証拠がない場合は abstain とし、別の RAG や会話記憶で空欄を埋めない。設定の `graphify_enabled=false` または `OLCR_GRAPHIFY_ENABLED=false` で無効化できる（保存済み設定が環境より優先）。下記の初期設計にある opt-in／補助レイヤー方針はこの追補で置き換える。

実装経路は `Graphify AST → .olcr/graphify/graph.json → 検証・候補化 → EmbeddingGemma → Laya Choice → EXTRACTED エッジ探索 → 現在ソース抜粋 → ContextManager`。探索は最大2ホップ・32ノード、回答証拠は最大3件。検索中の Graphify 子プロセス起動、LLM抽出、モデル取得、会話への自動書込みはしない。回答生成モデルは既存のまま。

### 索引を準備・更新する

検証した Graphify は `graphifyy==0.9.73`。OLCR の Python 環境へ依存を追加せず、インストール済み Graphify の Python で次を実行する。このマシンでは `/Users/nagimachino/.local/share/uv/tools/graphifyy/bin/python` が該当する。

```sh
/Users/nagimachino/.local/share/uv/tools/graphifyy/bin/python scripts/prepare_graphify_index.py . \
  --include backend/olcr_api --include frontend/src --include desktop/src-tauri/src
```

`--include` は必須・複数指定可能。ルートは対象プロジェクトへ変更する。元の `graphify-out` は上書きしない。索引にプロジェクト絶対ルート、Graphify版、対象パス、全入力ファイルSHA-256を保存し、抽出／export中の変更を検知して失敗時には旧索引を保持する。更新は staging から atomic replace。索引は `.gitignore` 対象。既存 Graphify のグラフは行とルートが古く、ハッシュを持たなかったためそのまま現在の証拠として採用しなかった。

### 軽量モデル層と安全境界

- Embedding は既存の `settings.embedding_model`（既定 `embeddinggemma:latest`）を利用する。最大64ファイル、1記述700文字、32件バッチ、HTTP要求は各2秒。候補打切りは trace に出す。初回のモデル費用はキャッシュ命中時より大きい。
- キャッシュはモデル識別子を検証できた場合のみ再利用する。Ollamaがdigestを返さない場合もembeddingは継続できるが、その要求内だけのキャッシュとする。
- Laya は既存の startup-loaded multilingual provider を共有し、7候補＋該当なしの最大8択のみ。APIルーティングの既定プロンプトは変更せず、Graphify専用の選択指示を渡す。
- Embedding停止時は検証済み字句検索へ劣化。Laya不在時に意味近傍だけでは採用しない。Layaの不正選択・例外は abstain。巨大生成モデルで検索判定を代行しない。
- ソースは毎要求SHA-256・現在行・ルート内resolveを検証する。 stale／削除／外部symlinkは排除。EXTRACTEDだけを探索し、双方向の移動を逆向きの呼出関係として断定しない。抜粋内の指示は信頼しない。
- 計算・ツール実行・外部API・Web取得・権限チェック・managed implementation は既存経路を維持する。Graphifyで会話メモリやSkill/API候補検索を置換しない。

### 残るゲートと想定障壁

1. 実Laya／EmbeddingGemmaの日本語検索precision・recall・初回／warm latencyを測定する。現環境はLaya checkpoint未準備、Ollama接続不可。fake providerテストを実モデル受入と混同しない。
2. 64ファイルの字句shortlistは大規模リポジトリ・日本語同義語で重要ファイルを落とし得る。coverageをtraceに記録した上で、事前embedding索引やcommunity shortlistを次段階で評価する。現実装は全体網羅を保証しない。
3. 索引は手動更新。変更されたファイルは次回更新まで採用されない。watcher／差分更新・多worktree・更新競合ロックは後続。読取と原子公開は実装済みだが、複数builderのlast-writer-winsは未解決。
4. ASTのみなので、Markdown等の意味抽出・推論エッジ・完全な影響分析はまだ保証しない。関数の抜粋は最大10行850文字であり、全関数要約の完全性を宣言しない。
5. 検索時間の総deadline／Laya推論cancel、プロジェクト数増大時のcache負荷、並行要求の性能評価は後続。各HTTP timeoutは要求全体のtimeoutではない。
6. 配布版にはGraphify runtimeの同梱をまだ追加していない。読取側は標準ライブラリだけで動作するが、索引作成は別runtimeが必要。インストール済みアプリへの変更・実GUI受入は行っていない。

次の実行ループは「失敗fixture追加 → 最小修正 → Graphify/通常chat/stream回帰 → 原子的索引再生成 → 実検索 → devlog更新」。実モデル準備後に日本語・同義語・該当なしのgolden setを加え、既存P0〜P6の未完了ゲートを順番に閉じる。

## 1. 目的と完成条件

OLCR のプロジェクトに紐づいたコードベースから、関数呼び出し、import、継承、設定から利用箇所への経路、変更影響などを質問に応じて取り出す。Graphify の構造情報は、OLCR が既に扱うファイル証拠、会話、明示的な core context と同じ最終的な文脈選択過程に通す。

完成の定義は「Graphify がインストールできた」ではない。次のすべてを満たすこととする。

1. 構造質問で Graphify 由来の関連証拠が回答に入り、実在するプロジェクト内のファイルと行へ追跡できる。
2. 通常チャット、明示的検索、ストリーミング、管理対象コーディングタスクの各経路について、Graphify を使用する条件と使用しない条件がテストで固定される。
3. プロジェクト A のグラフ、会話、ファイルがプロジェクト B に混入しない。許可されていないパス、シンボリックリンク、古いファイル内容を現在の証拠として採用しない。
4. Graphify の未インストール、未索引、更新中、破損、タイムアウト、MCP 応答変更で OLCR の既存経路が継続し、劣化理由が記録される。
5. 文脈予算、起動時間、検索時間、追加モデル呼び出し数を実測し、定めた閾値を満たす。
6. opt-in で有効化・無効化でき、無効時には既存の回答経路とテストが変わらない。ソース版と配布版の動作差を検証する。

初期スコープはローカルのコード構造検索と変更影響の読取である。Graphify の PR 連携、Web 取得、ドキュメントへの LLM 抽出、学習 overlay、自動的な会話記憶への書込みは初期スコープに含めない。後続で必要なら別の評価ゲートを設ける。

## 2. 現行実装から確定している接続点

| 既存箇所 | 現在の役割 | 統合時の注意 |
| --- | --- | --- |
| `backend/olcr_api/app.py` の `/api/chat` | プロジェクト、会話、core context、計画、コーディング状態、添付、会話メモリを連結して `Runtime.execute` へ渡す | 普通の「この関数の呼び出し元は？」はここから入る。検索ルータだけ変更しても網羅できない |
| `backend/olcr_api/runtime.py` の `Runtime.execute` | 明示的 `search/find/grep` の場合だけ `RetrievalRouter.retrieve` を呼ぶ。それ以外は通常のモデル経路 | Graphify の候補選択は明示的検索だけに依存させない |
| 同ファイルの `ContextManager.build` | `context_budget` に基づいて core context と `SearchResult` を選択する | 現状は文字数近似で、長い先行証拠があると後続を `break` で落とす。Graphify 追加前に予算と選択順をテストで固定する |
| `backend/olcr_api/retrieval.py` | ファイル名 → ripgrep → FTS5 → ベクトルの順で早期 return する | Graphify を末尾に追加すると既存層がヒットした時に呼ばれない。構造質問の分岐と候補統合が必要 |
| `backend/olcr_api/conversation_memory.py` | プロジェクトで絞った過去会話の Embedding 検索 | コード構造グラフとデータ源を混ぜない。`/memory off` は会話メモリだけに効くか、全メモリに効くかを UI 文言とテストで明示する |
| `backend/olcr_api/app.py` の `/api/chat/stream` | 同じ `Runtime.execute` に常に委譲する構造ではない | 同期 `/api/chat` のみ成功してストリームで無効、という状態を防ぐ |
| `backend/olcr_api/mcp_runtime.py` | タスク単位の MCP 境界 | 既存の初期化・プロセス管理が Graphify の実サーバで動くか実測する。合わなければ専用 adapter を作る |
| `backend/olcr_api/config.py` | 検索・予算・許可 root などの設定 | 新設定は既定 OFF。永続設定の移行、`rebuild()` の再構築、公開設定 API を通して試す |

この計画で使う `graph.json` は Graphify が生成する索引成果物であり、OLCR の会話 DB ではない。Graphify は公式に `query_graph`、`get_node`、`get_neighbors`、`shortest_path` 等の MCP ツールを公開している。`query_graph` の戻り値は `file:line` を含むテキストで、各関係には EXTRACTED / INFERRED / AMBIGUOUS の区別がある。この公開インターフェースを第一候補とし、`graph.json` の非公開な内部スキーマへの直接依存は互換性試験が成立する場合に限る。

## 3. 目標構造

```text
Project(project_id, authorized workspace)
  ├─ 保存された core context / 現在の会話・計画状態
  ├─ ConversationMemory (過去会話)
  ├─ 現在の File / FTS5 / Vector retrieval
  └─ GraphIndexManager ── graphify extract/update ── graphify-out/graph.json
           └─ GraphifyAdapter ── 許可した read-only MCP tools
                    │
                    ▼
             GraphEvidenceValidator
                    │  パス、行、内容、索引鮮度、project_id を検証
                    ▼
             EvidenceFusion (候補統合・重複排除・予算予約)
                    ▼
             ContextManager / selected_context / task telemetry
                    ▼
                  モデル
```

### 3.1 所有権と境界

- **正本**: 現在のワークスペースファイル、保存済み core context、SQLite の会話・タスク。Graphify のグラフは再生成可能な派生索引であり、事実の最終根拠ではない。
- **単位**: `project_id` と正規化した `workspace_path` と Graphify 索引パスをひも付ける。別プロジェクトが同じフォルダを共有する場合も、回答時の project_id と許可 root を必ず検査する。
- **索引配置**: 最初は OLCR の app support 領域に project_id ごとに置く案を採用する。ただし Graphify の CLI が workspace 外の出力先を指定できることを P0 で確認する。できない版では workspace 内の `graphify-out/` を使い、生成物を除外設定に登録する。配置を推測で実装しない。
- **書込み権限**: 初期版の Graphify 統合は索引更新のみ。グラフの MCP ツールは `query_graph`、`get_node`、`get_neighbors`、`shortest_path`、`graph_stats` に制限し、PR/ネットワーク系ツールを利用しない。
- **信頼階層**: グラフから返る文字列、コードコメント、README は外部証拠として扱う。システム指示、ファイル書込み承認、管理対象タスクのスコープを上書きしない。

### 3.2 `GraphEvidence` 契約

内部型として、最低限 `project_id`、正規化した `source_path`、`line`、`relation`、`from_symbol`、`to_symbol`、`confidence_kind`、`graph_version`、`index_fingerprint`、`retrieved_at`、検証済み `excerpt` を持たせる。`SearchResult` に変換する時も、監査用の元メタデータは `selected_context` に保持する。

採用条件は次の通り。

1. `source_path` は現在の project workspace の内側に実在し、シンボリックリンク解決後も許可 root 内にある。
2. `line` は実在する行である。引用は Graphify の文章を無条件に採用せず、現在のファイルから読み直す。
3. ファイル内容が索引時の hash と異なる場合は候補を stale とし、更新成功まで採用しない。Graphify がファイル hash を提供しなければ OLCR 側の索引 manifest に保存する。
4. `EXTRACTED` でも、引用行が存在することだけでは関係が真とは限らない。関係の両端と対象コードを照合し、検証不能なら「関連候補」に格下げする。`INFERRED` と `AMBIGUOUS` は関係の確証として断言しない。
5. 取得件数、1件の長さ、総出力、経路深さ、実行時間に上限を設ける。採用されなかった候補の理由も telemetry に数だけ残す。

`query_graph` が構造化メタデータを返さず、テキストからの安定した `file:line`・関係抽出も保証できない場合は、文字列の曖昧なパースを本番の証拠生成に使わない。その場合は `get_node` 等で再確認するか、ピン留めした Graphify 版のスキーマを adapter 内に限定して読み、互換性試験を必須にする。

OLCR 内部の接続契約は `GraphifyAdapter.search(project_id, workspace_root, question) -> GraphEvidence[]`、`GraphIndexManager.status/update(project_id) -> IndexStatus` とする。`app.py` はプロジェクト、質問、利用可能な文脈を渡し、`Runtime.execute` は別引数の検証済み evidence を受け取る。Graphify 結果を `core_context` の文字列に連結しない。`selected_context` は従来の検索結果や skill routing の記録に Graphify の記録を追加する形にし、上書きで消さない。明示検索で JSON を直接返す場合も Graphify の採否と出典を同じ形式で返す。

### 3.3 ルーティングと文脈予算

構造質問を「誰が呼ぶ」「どこから参照」「依存」「継承」「変更影響」「実行経路」「設定の伝播」等の意図で判定する。初期版は説明可能なルールと明示コマンドで開始する。通常の雑談、最新 Web 情報、計算、画像質問は Graphify を呼ばない。コーディングタスクでは、読み取り・計画フェーズにだけ構造証拠を供給し、実行・承認フェーズの権限には影響させない。

`EvidenceFusion` で Graphify 候補と従来検索候補を集め、同じ `path:line` や同一関係を重複排除してから予算配分する。既存の core context、現行タスク状態、ユーザー要求を先に確保し、残余からグラフ証拠へ上限を与える。初期値は Graphify 最大 3 件、各証拠 500 文字、MCP 応答 2,000 token、深さ 2、検索タイムアウト 2 秒とする。数値は P0 の計測で固定する。`ContextManager` が一件の長文で以後の短文候補を落とさないよう、全候補を長さと優先度で選ぶ回帰テストを先に追加する。

回答には検証済み `path:line` を表示する。`selected_context` には採用元、Graphify version、索引 fingerprint、confidence、検証結果、選択理由を保存する。ただしソース全文や秘密値をログへ複写しない。

### 3.4 索引更新と障害時

- 初回索引は opt-in 後の明示操作またはバックグラウンドジョブで行い、通常のチャット応答中には全量構築しない。
- 更新は変更検出後に排他制御し、最後に検証済みの索引を atomic に切り替える。更新途中・失敗時に半端なグラフを公開しない。前版の索引を残す場合も、変更されていないファイルの証拠しか供給しない。
- コードだけを対象にした最初の検証には Graphify の `extract --code-only` を使えるかピン留め版で試す。ドキュメントの関係抽出を使う時だけ別のモデル・コスト・秘匿性ゲートを設ける。
- 編集、ブランチ切替、git pull、ワークスペース変更、プロジェクト削除、Graphify 版変更を更新・無効化トリガーとして扱う。git hook は自動インストールしない。OLCR 側から更新を管理する。
- Graphify が停止・未索引なら既存のファイル/FTS/ベクトル検索に落とし、`GRAPH_UNAVAILABLE` 等の型付き理由を `selected_context`/診断へ記録する。既存の検索層自体が故障している場合は、そのエラーを Graphify の空結果で隠さない。

## 4. 実装とテストの完走ループ

各フェーズは **設計/変更 → 最小限の回帰テスト → 実 Graphify または実 API で確認 → 失敗分類 → 修正 → 同じゲート再実行** のループで進める。失敗を「再試行すれば通る」とは扱わない。入力、索引 fingerprint、Graphify version、失敗分類、修正内容を記録し、同じ原因が再現しなくなるまで次のフェーズに進まない。全フェーズに前段の合格状態を維持する停止条件を置く。

| フェーズ | 実作業 | テストと通過条件 | 失敗時の戻り先 |
| --- | --- | --- | --- |
| P0 契約確認 | `graphifyy` の正確な版と hash を固定し、隔離環境で小さな Python/TypeScript 2 プロジェクトを索引。出力先指定、MCP 初期化、`query_graph`、`get_node`、更新、欠落ファイルの扱いを調査。OLCR 現行の検索精度・p50/p95・文脈量を測る | 期待する呼び出し関係を検索でき、行の所在が検証できる。全操作はローカルで完結。実 MCP 応答 fixture と baseline を保存 | API/版の選定をやり直す。安定した出典が得られなければ実装を開始しない |
| P1 契約と設定 | `GraphEvidence`、有効フラグ、索引メタデータ、状態列挙、設定の保存/復元を定義。`rebuild()` でも設定を反映 | 未設定なら従来動作と同一。設定保存、無効化、再起動、未知設定、プロジェクト分離が通る | 設定移行と既定値を修正 |
| P2 索引ライフサイクル | プロジェクト単位の build/update、ファイル除外、hash manifest、排他、atomic 切替、状態/診断 API を実装 | 索引初回・差分・削除・リネーム・ブランチ切替・壊れた更新を再現し、変更のない旧索引の部分または従来検索へ安全に落ちる | manifest と更新単位を修正。古い候補を有効扱いしない |
| P3 読み取り adapter | 許可した Graphify MCP 呼び出しをラップ。実応答を型検査、サイズ/時間制限、パス検証、行の読み直しを実装 | 実サーバへの接続試験と fake server の異常応答試験。未許可 tool、別 workspace、symlink、存在しない行を拒否 | MCPRuntime の適合性を再評価し、必要なら専用 adapter に切替 |
| P4 候補統合 | 構造質問の判定、Graphify と既存 retrieval の候補集約、重複排除、`ContextManager` の予算選択と監査記録を実装 | 構造・単純文字列・会話記憶・Web・画像の各質問で、呼ぶ層と採用順が期待どおり。長文候補が短い有用候補を飢餓させない | 意図判定または予算配分に戻る |
| P5 API 各経路 | `/api/chat`、`/api/chat/stream`、明示検索、管理対象コーディングタスクの読取フェーズに同一 service を組込む | 通常/ストリームの回答と `selected_context` が整合。承認なし書込み・外部 tool 呼出しが増えない。中断時にプロセスが残らない | 当該経路の wiring を修正。API 全体へ安易に例外を握りつぶさない |
| P6 実評価と配布 | 複数規模の実プロジェクトで A/B。ソース版と配布版を起動。起動、更新、検索、メモリ、CPU、ディスクを計測 | 明示した評価ゲートを満たす。Graphify が無い機体でも起動でき、無効化で従来経路へ戻る | 性能・パッケージ・設定へ戻り、評価を再実行 |

### 4.1 評価用の固定質問セット

テスト用の小規模リポジトリに、正解の `path:line` と関係を事前登録する。少なくとも以下を含む。

- 直接呼び出し、2 hop の呼び出し、import の別名、継承、同名シンボル、動的呼び出し。
- 設定項目から利用箇所、API から DB、変更対象から関連テストへの経路。
- 存在しない関数、Graphify が推定しただけの関係、コメント内の命令文。
- 編集直後、削除/リネーム直後、索引更新中、他プロジェクトの同名ファイル。
- 日本語質問と英語質問。Graphify が日本語質問で候補を拾えない場合、OLCR 側で検索語抽出を追加するか、明示的な検索コマンドに限定する。

まず現行方式と Graphify 方式を同じ質問セット・同じワークスペース・同じ回答モデルで比較する。構造質問では、正しいファイル/関係への recall@3、誤った関係の提示率、回答に根拠のない断言がない割合を測る。非構造質問では無用な Graphify 呼び出し率を測る。p50/p95、追加モデル呼び出し、索引作成/更新時間、索引サイズも保存する。閾値は P0 の baseline と機体条件を記録した上で凍結し、評価後に都合よく変えない。最低条件は「構造質問の正解率が現行未満にならない」「プロジェクト越境 0 件」「検証できない出典の採用 0 件」「無効時の追加 Graphify 呼び出し 0 件」。

### 4.2 実行コマンド案

実装時の検証は、既存プロジェクトの `backend` ディレクトリで `python3 -m unittest discover -s tests -v`、`frontend` で `npm run typecheck` と `npm run build` を基本とする。P0 で選んだ隔離環境から、ピン留めした Graphify CLI と MCP の契約試験を追加する。ライブ試験はネットワークや実ユーザーデータに依存させず、一時的な検証リポジトリとローカル Graphify を使う。時間がかかる配布試験は、焦点を絞った回帰が通った後に一回実行する。

## 5. 想定障壁、バグ、検知と対策

| 障壁・バグ | 起こり方 | 検知方法 | 対策・完了条件 |
| --- | --- | --- | --- |
| パッケージと Python の不一致 | OLCR は Python 3.9+、Graphify は 3.10+ | 起動前 preflight、ソース/配布双方の環境試験 | 別ランタイムまたは 3.10+ の同梱を選び、通常の OLCR 起動を Graphify に依存させない |
| MCP 応答形式の差 | 現 `MCPRuntime` が想定する改行 JSON と実サーバの形式が異なる | P0 の実 handshake と tool call fixture | 適合する MCP client/adapter を実装。戻り値を strict に検証 |
| テキスト結果からの誤った出典抽出 | `file:line` が説明文に混ざる、同名ファイルがある | 同名シンボル・架空出典テスト | 正規化パスと実ファイルの行で再確認できた候補のみ採用 |
| 早期 return で Graphify 未使用 | ripgrep/FTS が先にヒット | 構造質問で Graphify 呼び出し数を計測 | 構造質問は候補集約経路に通す |
| 通常チャットと stream の乖離 | 実装先が `/api/chat` だけになる | 同一質問の両 endpoint 回帰 | 共通 service を共有し、表示・監査の整合を検証 |
| `core_context` の膨張 | Graphify テキストを単に連結する | prompt 長・選択内容の fixture | 証拠専用 budget を使い、`ContextManager` で採否を決める |
| 過去の索引による誤回答 | 編集、git pull、削除、Graphify 更新が検出されない | hash mismatch、索引時刻、削除/リネーム試験 | stale 候補を出さず、更新を予約。ソースの読み直しを必須化 |
| 不完全な索引で上書き | 大規模 walk 失敗やクラッシュ | build 失敗・ノード減少・部分出力試験 | 一時領域に構築し、検証後に切替。`--allow-partial` は初期版で使わない |
| 生成物の再取り込み | `graphify-out/`、配布物、モデル、venv が対象になる | 索引ファイル一覧、ノード数急増 | `.graphifyignore` と `.gitignore` の実効確認、許可対象の限定 |
| プロジェクト越境 | shared MCP に別の `project_path`、古い project_id が残る | 2 プロジェクトの同名ファイル・秘密マーカー試験 | server インスタンスを project に固定し、問い合わせ引数の root を受け付けない |
| シンボリックリンク/相対パス | グラフが許可 root 外を参照する | path traversal と symlink 試験 | `Path.resolve()` 後の root 検査、外側なら破棄 |
| 不確実な関係の断定 | INFERRED/AMBIGUOUS を事実と扱う | 誤関係を含む fixture | confidence を保持し、確証扱いを EXTRACTED + 現行ソース検証に制限 |
| コード内 prompt injection | コメントや文書に「命令を無視」と書かれる | 悪意ある fixture とモデル入力検査 | graph 出力を低信頼証拠として囲い、システム/承認文脈に昇格させない |
| MCP プロセス残留・ハング | タイムアウト後の子プロセス | タイムアウト/キャンセル試験とプロセス数 | 強制終了と read-only fallback、プロセス寿命を service で管理 |
| Graphify の版上げで形式変更 | tool 名、引数、出典表記が変わる | ピン留め版の契約 fixture と upgrade CI | 版を固定し、変更時に P0〜P3 を再実行 |
| ローカル限定からの逸脱 | optional backend が外部 LLM/API を呼ぶ | 子プロセスの環境変数とネットワーク試験 | code-only 初期版、env allowlist、外部 backend 設定を渡さない |
| 配布物だけで失敗 | 同梱 Python、PATH、起動 cwd が異なる | release smoke | 実行ファイルと索引パスを明示解決し、未同梱なら機能無効と診断 |

## 6. ロールアウトと最終判定

1. **開発フラグ**: 既定 OFF。ローカル一プロジェクトで Graphify 索引と read-only 検索を有効化する。
2. **shadow 比較**: Graphify 候補をログ上の監査対象として収集し、回答には注入しない。品質・鮮度・時間の baseline と比較する。
3. **限定有効化**: 構造質問だけ回答文脈に注入する。タイムアウトや不正出典は従来経路へ落とす。
4. **配布検証**: 一時プロジェクトを使い、無効状態、初回索引、更新、再起動、アンインストール相当の Graphify 不在を確認する。
5. **完成判定**: セクション 1 の六条件と P0〜P6 のゲートを全て満たし、既知の未解決事項・測定値・Graphify 版・有効化方法をリリースノートに記録する。満たせない条件がある場合は完了扱いせず、該当フェーズへ戻す。

無効化は設定フラグで即時に行える。索引成果物の削除は別操作とし、無効化時にユーザーのデータを暗黙に消さない。Graphify を外しても core context、過去会話、FTS/ベクトル索引、既存 API は利用可能な状態を維持する。

## 7. P0 で確定すべき未決事項

1. Graphify のピン留め版、配布 hash、macOS arm64 での実インストール方法。
2. ワークスペース外への索引出力をその版が正式に扱えるか。不可なら workspace 内配置を採用する。
3. `query_graph` と `get_node` が返す引用、confidence、ノード ID の実形式。source hash の有無。
4. OLCR の既存 `MCPRuntime` が Graphify の stdio MCP と互換か。互換でなければ専用 client の最小要件。
5. 現在の配布 Python と `graphifyy` の依存関係が共存可能か。重ければ隔離された実行環境にする。
6. `conversation_memory_enabled` を OFF にした場合の UI 文言と Graphify の扱い。提案は Graphify を独立した「プロジェクトのコード検索」設定にすること。

これらを実測・決定してから P1 に進む。Graphify の公開仕様は変わり得るため、実装時にはピン留めした版の公式 CLI/MCP ドキュメントと実応答を再確認する。

## 参考

- Graphify 公式リポジトリ: https://github.com/Graphify-Labs/graphify
- Graphify MCP tools: https://www.graphiffy.com/docs/mcp-tools
- Graphify quickstart: https://www.graphiffy.com/docs
