# Agentsと機能アイコンの整理

対象: React/Tauri版OLCRのサイドバー、チャット操作メニュー、コピー・添付、Agents一覧・作成・編集・利用。既存の白い面・緑のアクセント・システム書体を維持した。

| Severity | Location | Before | After | Why |
| --- | --- | --- | --- | --- |
| MEDIUM | frontend/src/ComposerActions.tsx:1 / frontend/src/main.tsx:1 / frontend/src/UiIcon.tsx:1 | 添付が＋、概念図が四角、スクレイピングがリスト記号、GUIテストとコピーが同じ四角記号、削除が× | クリップ・ノード図・地球と取得矢印・画面とチェック・重なる文書・ゴミ箱に変更 | Semantic icons: 動作の意味と図形を対応させ、同じ意味のアイコンを共用 |
| MEDIUM | frontend/src/CustomAgents.tsx:1 / frontend/src/custom-agents.css:1 | Agentsのボタンがブラウザー標準寄りで他画面と不統一、フォームの追加設定が常時展開 | 共通トークンとボタンスタイル、知識・ツール・スキル・連携先を選択数付き開閉セクションに整理 | Surfaces and hierarchy: 基本入力を見渡せる構成と既存アプリの密度を保つ |
| MEDIUM | frontend/src/CustomAgents.tsx:1 | 長い選択肢から探す必要があり、利用不可の保存済みモデルが選択欄から消える | ツール・スキル検索、選択済みモデルの状態付き表示、検索外の選択も維持 | Familiarity and feedback: 設定の現状を確認でき、選択を失わず絞り込める |
| LOW | frontend/src/CustomAgents.tsx:1 / frontend/src/custom-agents.css:1 | 初期読み込みの表示・再試行がなく、カードの構成情報が見えない | 読み込み状態・再試行、構成件数、空状態、削除の区別、画像の低不透明度outline | Consistency: 状態と次の操作を明確にする |

## 設計根拠

- Apple `icons.md › Standard icons / Editing`: “Copy” に `document.on.document`、“Delete” に `trash`、“Attach” に `paperclip` を挙げている。React用のSVGに置き換えた。
- Apple `icons.md › Best practices`: “If you create a custom interface icon, use a vector format like PDF or SVG.” 24pxグリッド・currentColor・装飾アイコンのaria-hiddenに統一。機能ラベルと既存のaria-labelを保持した。
- Apple `disclosure-controls.md › Best practices`: “Use a disclosure control to hide details until they’re relevant.” Agentsの追加設定をdetails/summaryに変更。基本情報・指示・モデルは常時表示する。
- Apple `entering-data.md › Best practices`: “When possible, offer choices instead of requiring text entry.” チェックリストを保ち、検索を補助として追加した。
- 機能のAgentsはアシスタントの線画、Skillsは開いた本。ユーザーが指定したエージェントの画像・絵文字・既存のデフォルト値は保存形式も含めて保持する。これは機能アイコンと個人のアバターを区別する設計判断。
- 色は既存surface #FFFFFF、primary #20231F（白に15.9:1）、secondary #6B7068（白に5.1:1）、accent #95E329。新しいブランド色は追加していない。Agentsの本文13px・見出し20/15px・補助12px。PCの1列フォーム、カード一覧は幅に応じて列数を変える。独自の追加演出はなし。

## 検証

- 関連4ファイル・53テスト成功: Agents保存、会話の分離、Composer、権限連携。追加テストで検索・折りたたみ後の選択保持、利用不可モデルの保持、初期読み込み失敗からの復帰を確認。
- 本番ビルド・型検査成功。既存のバンドルサイズ警告あり。
- 実Chrome＋隔離したモックAPIで一覧・作成・編集・利用・削除キャンセルを確認。Enterで開閉でき、検索外の選択・カスタム絵文字・利用不可モデルが保存時にも維持される。
- PC 1180pxと390pxの一覧・フォーム・チャットメニューを視覚確認。320px・125%拡大でもAgents領域内の横はみ出しなし。狭幅の設定ボタンのSVGも可視確認。
- hover・focus-visible・active（scale 0.96）・disabled・読み込み・空・エラーはコードで確認。SVGはcurrentColor、通常のメニューアイコンstroke 1.5、強調ラベル側stroke 2。既存サイドバーのchevron回転は120ms ease、reduced-motionでは無効。
- Not verified: macOS実機のエージェント実行環境・外部API・VoiceOver・ダークモード、アニメーションパネルの10%再生。実アカウントとユーザーDBを使わず、ブラウザー検証はモックAPIに限定した。

上記の変更範囲: Approve
