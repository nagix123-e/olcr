# 概念図の全画面編集と配色

対象は、OLCRの入力欄から図を添えて依頼するための概念図エディター。図の作成・編集とチャットへの復帰を一つの流れで行う。既存の包含、ベン図、縦横サイズ、接続、矢印、削除、履歴、破棄、送信機能を保持する。

| Severity | Location | Before | After | Why |
| --- | --- | --- | --- | --- |
| MEDIUM | frontend/src/ConceptDiagram.tsx:46 | 入力欄内の限られた高さのみ | 同じドラフト・履歴で全画面と通常表示を往復、Esc対応、フォーカスを閉じ込めて復元 | Agency / keyboard support: 大きな図の編集と会話への復帰を両立 |
| MEDIUM | frontend/src/ConceptDiagram.tsx:101 | 多くの操作・説明・寸法を常時表示 | 頻用操作をツールバーに、包含・ベン図・破棄をその他に、寸法と配色を選択時に表示 | Hierarchy: 作図領域を広げ、機能は残す |
| MEDIUM | frontend/src/concept-diagram.css:40 / frontend/src/diagramColors.ts:1 | 緑がかった面と固定色 | 白い作図面、黒・淡い赤青黄のパレット、線・枠・文字を個別設定 | Surface consistency: 色の意味は作図者が決め、操作の選択表示はニュートラルな灰色に |
| MEDIUM | backend/olcr_api/concept_diagrams.py:33 / backend/olcr_api/app.py:1715 | 色の保存契約なし、検索コンテキスト経路で保存済み図を省略 | v2の任意フィールドとして色を保存し、後続依頼でも図を参照 | Feedback / persistence: 送信と表示で図の情報を保持 |
| LOW | frontend/src/ConceptDiagram.tsx:60 | 幅の変更後に図が画面外に残る | 作図面の幅変更時に全体表示 | Responsive layout: 通常表示と狭い幅でも図を見つけやすくする |

根拠: `toolbars.md › Best practices`: “Choose items deliberately to avoid overcrowding.” 頻用コマンドを残し、追加操作をMoreにまとめる。全画面はOSのウィンドウ制御を模倣せず、アプリ内の編集領域を広げる。

配色は黒 #242424、枠・線に赤 #D5949B / 青 #94B2DB / 黄 #D8C073。文字は同系色の #A4515D / #416C9E / #7F6A2B とし、白背景とのコントラストはそれぞれ5.37:1 / 5.44:1 / 5.25:1（黒15.52:1）。淡い線の識別は色だけに依存せず、形状・文字・選択アウトラインを維持する。色選択には名前とチェック・押下状態を付ける。白い作図面はユーザー指定。アプリの書体と密度、共通SVGのcurrentColorアイコンを継承する。新しい登場アニメーションは追加せず、押下のscale(.96)はreduced-motionで無効にする。

通常幅は「見出し / ツールバー / 選択時の書式 / 作図面 / 状態」、狭い幅も同じ順で書式が折り返す。全画面では作図面が残りの高さを使う。

検証:

- フロントエンド51テスト、バックエンド10テスト、production build成功。
- モックAPIのChromeで全画面と通常表示・Esc・フォーカス範囲・履歴・色変更・サイズ・包含・ベン図追加解除・破棄キャンセル・再オープン・送信とプレビューを検証。1180×940と390×844、幅変更時の全体表示、メニューが編集領域内に収まること、ページ例外なし。
- バックエンドは一時DBで通常/ストリーム送信・保存・後続ターンの色付き図参照、旧v1との互換性、不正な色の拒否を検証。実ユーザーDB・外部APIは利用しない。
- Not verified: Tauri実機、実モデル、VoiceOver、OSの大きな文字・高コントラスト・ダーク外観、全hover/loading/empty状態の実ブラウザー操作、Animationsパネルでの10%再生。通常UIのhover/disabled/focus/pressedおよびreduced-motionはコードを確認。

Approve（上記で検証した範囲）。未検証項目を含む全面的なHIG準拠の保証は行わない。
