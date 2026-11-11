# 設定画面の整理

対象: OLCR / React・Tauriのデスクトップ設定画面。一般設定、外部アクセス、機能の許可を確認・変更する画面。既存の白い面、緑のアクセント、システム書体を維持した。

## 実施した変更

| Severity | Location | Before | After | Why |
| --- | --- | --- | --- | --- |
| MEDIUM | frontend/src/ExternalToolsSettings.tsx:50 / frontend/src/ModelsPanel.tsx:35 / frontend/src/WebSettings.tsx:11 | ツール・モデル接続先・検索認証状態の一覧が常時表示 | 初期状態を閉じたdetails/summary。ツール件数、モデル設定済み件数を表示 | Progressive disclosure: 必要な詳細だけ開いて確認できる |
| MEDIUM | frontend/src/ExternalToolsSettings.tsx:61 / frontend/src/settings.css:13 | ON/OFF二択ボタンとGitHubのチェックボックスが混在 | ラベル付きスイッチ、説明、区切り線に統一 | One visual language: 同じ意味の操作を同じ形にする |
| MEDIUM | frontend/src/ChatSettingsCard.tsx:44 / frontend/src/settings.css:1 | 一般設定が導入見出しより先、権限カードだけ別の面・文字サイズ | 導入→一般設定→モデルの順、見出し15px・説明12px・同一トークンと区切り線 | Surfaces and hierarchy: 項目間の視覚的な優先順位を揃える |
| LOW | frontend/src/ModelsPanel.tsx:26 / frontend/src/settings.css:69 | 日本語画面に英語見出し、診断リンクが閉じるボタンの文字サイズを継承 | 「モデルのフォールバック」と翻訳、診断リンク12px | Consistency: 補助操作・見出しの役割を明確にする |

## 根拠と設計判断

- Apple `disclosure-controls.md › Best practices`: “Use a disclosure control to hide details until they’re relevant.” 状態の概要を残し、一覧を開閉可能にした。
- Apple `settings.md › Best practices`: “Minimize the number of settings you offer.” 機能の削除はせず、見える情報を整理する形で適用した。
- Apple `color.md › Best practices`: “Avoid using the same color to mean different things.” 全ツール状態を一律に緑色で示す扱いを避け、返却された状態ラベルを表示する。
- 既存トークン: surface #FFFFFF、primary #20231F（白に15.9:1）、secondary #6B7068（白に5.1:1）、accent #95E329（primaryとの比10.1:1）、border #E8EBE6。新しいパレット・書体・テーマ・モーションは導入していない。コントラストはsRGB相対輝度で算出。
- 通常幅は最大920pxの一列。狭幅ではセレクトと状態行を縦に並べる。構造は「見出し→説明→一般→モデル→接続・権限→機能」。既存の静かなローカルツールとしての外観を保ち、追加の装飾は省いた。

## 検証

- 型検査・本番ビルド成功。既存のバンドルサイズ警告あり。
- 関連6ファイル・25テスト成功。外部アクセスの更新、機能単独PATCH、保存待ちの操作抑止、保存失敗時の状態保持を含む。
- 実Chrome + 隔離したモックAPI: 初期折りたたみ、Enter/Spaceで開閉、スイッチ変更後の反映、モデル接続先の編集・キャンセル、ブラウザー例外なしを確認。
- 1150pxと390pxの画面を視覚確認。320px・125%拡大で横はみ出しなし。フォーカス表示・hover・押下・disabled・読み込み中・空一覧・エラーのコードを確認。
- 新規の開閉アニメーションはなし。押下scaleは0.96、prefers-reduced-motion時は無効。
- Not verified: Tauri実機でのKeychain・macOS権限ダイアログ、実外部API、VoiceOver、ダークモード。今回のブラウザー検証は実アカウントやユーザーDBを利用していない。Animationsパネルでの10%再生は未実施（新規アニメーションなし）。

変更した設定UIの確認範囲: Approve
