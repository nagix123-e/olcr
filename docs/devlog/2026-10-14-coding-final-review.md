# コーディング機能の最終コードレビュー

2026-10-13夜〜14日。既存Graphifyでチャット・承認・workspace・実装・検証・再開の関係を参照したうえで、現在のソースを確認した。

## 修正

| 対象 | 問題と対応 |
|---|---|
| workspace準備 | prepareが共有bookkeepingロックを保持してcaptureのworkspaceロックを待つ一方、実行中の処理はworkspaceロックを保持してbookkeepingロックを必要としていた。prepareもworkspaceロックを先に取得し、待機中に共有ロックを占有しない形へ変更 |
| 完了後の追加Task | 子Taskの作成後、親の指示をAPPLIEDにする前に失敗すると、再試行で後着指示まで反映済みになる可能性があった。子Task作成のINSERTと同時に引き継いだ指示IDをrequirementsへ保存し、そのIDだけを反映済みにする。dispatchも直列化 |
| 追加Taskの実行開始 | 作成直後からQUEUEDだと、作業場所を予約する前にschedulerが実行できた。FOLLOWUP_PREPARINGの再開可能状態で作成し、workspace設定後にキューへ登録。途中失敗でも同じ子Taskの準備を再試行できる |
| 引き継ぎ情報 | 指示IDの受領記録をrequirementsの再正規化・定義への回答後にも保持する |
| 計画承認 | revision/status確認、承認scope保存、planning_only解除、キュー登録が別処理だった。BEGIN IMMEDIATEの一つのトランザクションにまとめ、異なるrevision・保護操作待ち・アーカイブ済み・二重承認を拒否 |
| チャット切り替え | 切り替え後に前チャットの停止・再開・アーカイブ応答が届くと、現在のカードを上書きできた。選択TaskのIDとplan revisionで反映を制限し、古いエラーも現在のカードへ反映しない |
| 初期Task一覧とストリームの競合 | ストリームから新Taskを選択した後に古い初期一覧が返ると、新Taskのカードが消える可能性があった。新しい選択がある場合、初期一覧による上書きを止める |

## 変更ファイル

- `backend/olcr_api/task_workspace.py`
- `backend/olcr_api/db.py`
- `backend/olcr_api/app.py`
- `backend/olcr_api/task_review_api.py`
- `backend/tests/test_orchestrator_regressions.py`：回帰テスト4件追加
- `frontend/src/ChatComposer.tsx`
- `frontend/src/ChatComposer.test.tsx`：停止・再開・アーカイブの遅延応答3ケースと、初期一覧の競合1ケースを追加
- `frontend/src/HandsFreePanel.test.tsx`：既に廃止されたON/OFFボタン名の参照を、現在のrole=switchの操作へ更新。製品の動作変更なし

## 検証結果

- バックエンドの関連23モジュール：**514件実行、509件合格、既存5件スキップ**。ログ：`/private/tmp/olcr-coding-final-suite.log`。
- フロントエンド全体：**33ファイル・260件合格**。ログ：`/private/tmp/olcr-coding-final-frontend-v2.log`。
- `npm run typecheck`通過。
- `git diff --check`通過。
- 実SQLite・スレッド・ファイルシステム・一時Git fixtureと、モデル応答/APIのテストダブルを使用。

バックエンドでは既知の通常Web検索テスト2件（`test_api_miss_uses_brave_even_for_vague_prompt_without_lookup_verbs`、`test_automatic_information_lookup_uses_brave_then_inference`）を今回も除外した。全バックエンドテストの合格を意味しない。フロントエンドの初回全体実行はHandsFreePanelの旧セレクターで4件失敗し、現在の操作へ更新後に全件合格した。

## 確認範囲

現在の生成モデルによる全工程完遂や、実GUIの再実行は行っていない。テストは隔離したDB・成果物で実施し、稼働中のユーザーTaskには操作していない。起動中アプリへの修正反映には再起動が必要。今回の修正で任意のモデル応答・外部障害に対する無不具合を保証するものではない。
