# Ultimate Web Scraperに寄せたOLCRの実装（2026-10-05）

ユーザー添付の機能整理を比較基準とした。既存のmanaged Playwright、公開先へのDNS固定プロキシ、Recipe、チェックポイント付き実行管理を再利用する。通常チャットから開始し、その会話内のブラウザーと表で確認・編集する。

| 機能 | 採用内容 |
|---|---|
| List Extractor | 反復するカード・表行を自動検出。ブラウザープレビューの1行をクリックして共通親の類似兄弟を選択できる（最低3行）。選択後の枠と件数、抽出プレビューを表示。テキスト、葉要素の列、表セル、リンク、画像・alt、ARIAラベル、CSS背景画像を収集。 |
| Page Extractor | 複数URLを順番に開き、各ページ1行。title、description、author、date、image、OpenGraph、JSON-LD、Microdataの実データを収集。欠落値は空欄、推測しない。 |
| Page Text | 本文・タイトル・著者・日付・語数。本文保存は5万文字で切り、切り詰めフラグも出力。 |
| Email / Social | 本文とmailtoのメール、主要SNS/GitHubリンク。複数URLに対応。同一サイトの最初のページから見つかった内部リンクも、指定ページ上限の範囲で収集できる。 |
| Image Downloader | lazy画像、通常画像、CSS背景、video posterのURL一覧。明示操作で公開画像をZIP取得（20件・合計20MB・1画像5MBまで）。失敗もmanifestに残す。既存のDNS固定HTTPクライアントとリダイレクト検証を使用。 |
| Shopify | 対象ストアの公開 `/products.json?limit=250` をmanaged browserで開き、バリエーションごとの行に展開。非公開商品や管理者APIには接続しない。 |
| Sitemap | XMLのlocを一覧化し、URL/種類を表に表示。表に表示中のURLをPage Extractorへ渡せる。 |
| Pagination / Load More / Scroll | 既存ランナーを再利用。Nextリンクまたは安全なLoad MoreボタンをDOMから検出して設定可能。明示指定で無限スクロール。Nextリンクは同一originのURLへ遷移する既存方式。 |
| Speed | Fast=追加待機0秒、Normal=1秒、Slow=3秒。Recipeの `page_delay_seconds` で0〜30秒指定可能。待機中も停止・一時停止・時間上限を確認。 |
| Data Table | サーバー上の全結果を対象に検索、50行ずつ表示。セル編集、列名変更・削除・左右並べ替え、数値変換、2列結合、行の重複・空行除去。選択列がある場合はその列の値で重複判定。 |
| Export | 編集後の列と行からCSV/JSON/JSONL/XLSX。CSVは数式対策を維持。XLSXは数式を作らないinline string方式、1万行・XML約32M文字まで。大きな結果はストリーミングCSV/JSONを使う。 |
| Recipes | 名前付き保存、再検証、再実行。JSONインポート・エクスポート。インポートは常にDRAFTとし、再検証する。 |
| チャット開始 | 自然文＋URL（複数可）で開始。一般的な抽出ツールはモデルなしで動く。名前・価格などを明示した従来の列指定はモデルによるセレクタ提案を維持し、モデル接続失敗時は自動抽出を試す。 |

## 利用例

- `https://example.com の本文を抽出して`
- `https://example.com/catalog をスクレイピングして`
- `https://example.com/one と https://example.com/two のメールを抽出して`
- `https://example.com のサイト内のメールを収集して`（最初のページの内部リンク、最大10ページ）
- `https://example.com/catalog を全ページスクレイピングして`（見つかったNext / Load More、最大10ページ）
- `https://example.com の画像を抽出して` → 会話内の表でZIPダウンロード
- `https://store.example のShopify商品をスクレイピングして`
- `https://example.com/sitemap.xml のサイトマップを抽出して`

停止・一時停止・再開・結果・エクスポートは既存のチャット操作を継続する。表の編集は終了・停止した実行に対して行う。失敗・中断した部分結果を編集すると、その実行をSTOPPEDとして確定するため、続きの抽出と編集済みデータは混ざらない。保存レシピから新しい実行は開始できる。

## 今回の境界と残る差分

- Chrome拡張ではなく、OLCRの会話内にmanaged browserを表示する。Settingsは権限管理として残す。
- 選択枠はクリック後に表示する。ホバー中の連続検出、ドラッグによる列幅変更、日付・URL等の専用型、条件付きセル書式は未実装。
- 内部リンクは1階層のみ。Sitemap indexの再帰巡回、CSVファイルや既存任意テーブルからのURL一括取り込み、ページ間の複雑な階層巡回は未実装。手入力複数URLとSitemap表の表示中URL受け渡しは実装済み。
- Shopifyは公開JSONの1ページ（最大250商品）を展開する。全カタログの専用ページ送り、Shopify取り込み専用CSVは未実装。通常CSV/JSON/XLSXは利用できる。
- 1回の自動抽出は最大1万行。ページ数500、全実行のレコード数10万、時間3600秒の既存上限を維持する。
- 抽出URLは既存方針に合わせquery/fragmentを保存しない。署名付き画像URL等はZIP取得に失敗する場合がある。ログインprofileは既存機能を維持するが画像ZIPは公開HTTP取得のみ。
- クラウド実行、スケジューラ、住宅プロキシ、Webhook、Google Sheets連携、課金サービスは追加していない。別の外部実行基盤を要するため、このローカル改善から切り離した。

## 検証

- 実Chromiumのネットワーク遮断fixtureで8方式、クリックによる同種行選択とCSS座標変換を検証。
- 実managed Playwright + チャットAPIで自然文の2URLメール収集 → 実行完了 → 表検索・列変更 → CSV/JSON/XLSX → Recipe export/importを検証。モデル呼び出しを例外化し、呼び出しゼロを確認。
- 既存DOM/Network JSON、ページ送り、Load More、Scroll、停止・再開、ブラウザーegressの回帰テスト。
- 表編集のトランザクション、列変換失敗時ロールバック、行ID保持、XLSX非数式化、画像取得の公開HTTP検証を検証。
- フロントエンドのプレビュー後実行、実行中の編集無効化、検索とページ送り、セル編集、出力形式選択を検証。TypeScript・本番フロントエンドビルド。
- macOS実GUIの再ビルド・起動、およびインストール済みアプリの置換は今回未実施。
