# コーディングオーケストレーター：競合・保存・再開の監査

2026-10-13。前回の操作解釈・実行契約の監査に続き、異なる系統の障害を調査して修正した。既存Graphifyの関係情報を参照し、現在のソースと回帰テストで確認した。

## 修正

| 問題 | 対応 |
|---|---|
| 同じ作業場所のTaskが並行してファイルを書き換える | workspace実行境界でrootごとの再入可能ロックを取得。例外でも解放。異なるrootの並行実行は保持 |
| schedulerを同時に起動すると複数のownerが生まれる | 起動処理をロックで直列化 |
| 古いキュー情報で停止・アーカイブ済みTaskを実行する | workspace準備前と実行ロック取得後にDBの最新状態を読み直す |
| 同じTaskの再enqueueでFIFO順序が変わる | 既にQUEUEDの順番を保持。アーカイブ済みTaskはenqueueしない |
| 完了状態だけ保存され、チャットの報告が保存されない | 完了状態と報告メッセージを単一SQLiteトランザクションで保存。失敗時は両方rollbackし、重複呼び出しも一度だけ保存 |
| 報告の整形中に停止要求・計画変更・追加指示が入る | 保存時に最新状態を検査。停止なら再開可能状態へ戻し、計画変更なら古い報告を公開せず、追加指示なら先に適用 |
| 古い呼び出し元の計画、空の工程表、削除済み成果物、RETRY判断を完了とする | 完了時に最新計画を再取得。空の工程表を拒否し、明示的な工程契約を再評価、managerの判断も確認 |
| 承認待ちの追加指示が承認後も未処理扱いになる／後着指示まで適用済みになる | 承認対象の指示IDとrevisionを保存し、そのbatchだけを承認と同じトランザクションでAPPLIEDへ更新 |
| 失敗した追加指示が再開時にも取り込まれない | 実行境界でNEEDS_REVIEWの指示も再取得。承認待ちの保護と既存の復旧回数制限は維持 |
| 保存済みartifactのrootが実際の実行workspaceを置き換える | root不一致ならWORKSPACE_IDENTITY_MISMATCHとして実行前に停止。異なる場所を暗黙に書き換えない |
| follow-upのdispatch例外でschedulerの起床が省略される | finallyで起床通知を保証 |

対象ソース：`backend/olcr_api/app.py`、`backend/olcr_api/db.py`、`backend/olcr_api/task_workspace.py`。

## 検証

- `backend/tests/test_orchestrator_regressions.py`に15件の回帰テストを追加。停止・アーカイブ、成果物削除・RETRYなどはsubTestでも確認。
- 関連20モジュール：**487件実行、482件合格、既存5件スキップ**。
- 実スレッドで同一rootの排他、別rootの並行実行、同時scheduler起動を確認。
- SQLite triggerで報告INSERTを失敗させ、状態とメッセージの原子性を確認。
- 整形中の計画変更・停止・追加指示を注入し、古い完了報告を公開しないことを確認。
- 保存済みroot不一致時にはruntimeとmanifest取得を呼ばず、別workspaceのファイルを変更しないことを確認。
- 最終ログ：`/private/tmp/olcr-orchestrator-lifecycle-final.log`。
- `git diff --check`通過。DB・成果物は隔離した一時fixtureを使用。ユーザーの稼働中Taskや成果物は変更していない。

既知の通常Web検索テスト2件（`test_api_miss_uses_brave_even_for_vague_prompt_without_lookup_verbs`、`test_automatic_information_lookup_uses_brave_then_inference`）は除外。前回、変更した実装経路を通らず同じ失敗になることを確認済み。全バックエンドテストの合格を意味しない。

## 確認範囲

モデル応答はテストダブルを使用し、実Runtime、SQLite、スレッド、ファイル実行器、検証器、Gitの一時fixtureで制御経路を確認した。実生成モデルによる全工程の完遂とGUIの再確認は未実施。現在起動中のアプリには、このソース修正はまだ反映されていない。

完了直前の成果物削除は検出するが、検証後に外部から内容だけを書き換えられた場合の全ファイルhash照合は追加していない。古い工程契約には互換経路も残る。外部モデルの可用性や任意の自然言語条件の意味的な正しさを保証する変更ではない。
