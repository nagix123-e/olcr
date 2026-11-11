# 保留中の追加指示と1回限りの送信

現在の編集UIは[通常入力欄での編集](2026-10-25-composer-edit.md)へ更新済み。以下は初回実装時の検証記録。
コーディングタスク向け。未取得のWAITING（完了後）・QUEUED（次の区切り）を入力欄の近くに表示し、編集・取り消し・WAITINGからQUEUEDへの移動を提供する。入力欄の「今の作業へ送る」はその1件にだけsteerを指定し、選択中の送信先・アプリ設定を変更しない。反映は既存ランナーの次の工程・計画境界で行われ、進行中のモデル応答のトークン列へ途中挿入する機能ではない。

| Severity | Location | Before | After | Why |
| --- | --- | --- | --- | --- |
| MEDIUM | frontend/src/PendingInstructions.tsx:1 | 保留された指示は履歴で見るだけ | 個別の編集・取り消し・今回だけ反映待ちへ移動 | Agency / feedback: 未取得の指示を修正できる |
| MEDIUM | frontend/src/ChatComposer.tsx:1 | 送信方法を選択し直してsteer送信 | 「今の作業へ送る」でその1件だけ上書き | Consistency: 永続設定・次回の送信先を維持する |
| MEDIUM | backend/olcr_api/task_workspace.py:1 | 保留指示を変更するAPIがない | メッセージ・状態の比較付きトランザクション、取得開始後は拒否 | State feedback: 適用済み指示を取り消したと誤認させない |

- 鉛筆は共通edit SVG、取り消しはclose、今回だけ送る操作は同じ24pxグリッド・currentColorの方向変更矢印。Apple `icons.md › Standard icons / Editing`と既存の機能アイコンの規則に合わせた。
- Apple `entering-data.md › Best practices`: “Dynamically validate field values.” 空白のみの保存・送信、重複操作を無効化。競合時は編集本文を残す。
- WAITINGを編集してもWAITINGのまま。取り消しはCANCELLEDとして監査履歴に残し、会話から指示本文を削除、会話記憶を無効化する。編集時も会話本文を同期し、古い記憶を無効化する。
- ランナーが取得したAPPLYING・APPLIED・NEEDS_REVIEWは編集対象外。完了後の後続タスク作成と保留操作も共通ロックで順序を守る。
- リストは高さ制限付きで内部スクロール。本文13px・補助12px・既存トークンを使用。フォーカス表示、押下scale 0.96、reduced-motionで押下変形を無効化。

検証:

- フロントエンド56テスト成功（保留リスト、ChatComposer、既存変更レビュー）。
- バックエンドの追加5テスト成功。編集・会話同期・取り消し・単一指示の昇格・ランナー取得後の競合・HTTP操作を確認。
- 既存TaskReviewTests全体では24件成功、1件失敗。`test_interrupted_steering_requires_review_after_restart`はNEEDS_REVIEWを取得しない期待値だが、今回未変更の`take_steering`はNEEDS_REVIEWを取得する実装。この既存の不一致は変更範囲から分離して記録する。
- 実Chrome＋モックAPIで編集中の本文保存、取り消し、保留→反映待ち、今回だけsteer、送信先queueの維持、設定変更なしを確認。通常幅・390pxで表示を確認。
- 型検査・本番ビルド成功。既存のバンドルサイズ警告あり。
- Not verified: 実Tauri・実モデルを実行した工程境界での反映、VoiceOver、ダークモード、10%速度でのアニメーション再生。実アカウントとユーザーDBは使っていない。

変更した保留操作・送信UIの確認範囲: Approve
