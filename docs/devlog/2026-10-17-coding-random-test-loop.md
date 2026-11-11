# 2026-10-17: 履歴テスト修復の反復と進捗表示の消失

## 報告された実行の証拠

- 起動元: `/private/tmp/olcr-launch-20261017-rs8-target/debug/bundle/macos/OLCR.app`
- プロファイル: `/private/tmp/olcr-launch-20261017-rs8-profile`
- Task: `a2735034-e6fb-4174-b671-c2257ec13fc5`
- 実ログ: `/private/tmp/olcr-launch-20261017-rs8.log`
- 18:38 のスクリーンショットでは `0 / 3`、原要件の検証を実行中。保存されたフェーズは実際には実装とテスト作成の2工程が `pass`。
- Task はユーザー要求で `RESUMABLE` / `PAUSED`。13件の工程レポート、計画改訂1、recovery epoch 0、Nodeテスト9件中8件成功・1件失敗。

失敗している履歴テストは、6候補をランダムに6回選び、長さ5の履歴に特定の料理が必ず含まれると断定していた。修復モデルが `history.includes('E')` と `history.includes('F')` 等を交互に変更し、同じテスト名の失敗が続いた。候補を追加した順番と抽選された順番は異なるため、この期待値は元仕様から導けない。

これは前回完了した別の隔離実行では発現しなかった経路であり、前回の成功をこの実行の成功証拠として扱わない。

## 修正

1. Node ESM スタックの `file:` URLを `fileURLToPath` で復号し、実際に失敗したassertionの行と周辺を修復モデルへ渡す。空白・日本語を含むパスでも従来のrealpath境界を維持する。
2. テスト作成・修復と停滞時の診断に、非決定的な結果は実際の戻り値を記録して状態と照合する規則を渡す。固定した料理名の入れ替え、長さだけへの弱体化、本番の乱数変更を解決策にしない。
3. 同じスケジュールの再実行ではグラフの完了・失敗・試行情報を保持する。過去の処理が消した完了行は、controllerの保存済みPASSレポートがある工程だけ復元する。明示的再計画時の未完了工程の初期化は維持する。
4. optional build の `NOT_RUN` が、controllerのPASSを未確認へ上書きしないよう修正。失敗概要にはラッパーの `verification command failed` より実際の失敗テスト名を優先する。
5. カードに実際の修復対象を表示し、追加の復旧枠を使う試行番号を `2 / 2` に丸めて隠さない。
6. 実モデルr2の再計画で、`done: ["],", "verify:["]` がschema上は文字列配列として受理され、達成不能な条件となる問題を追加発見。生成直後の `validate_plan` と保存済み計画の実行前の両方で、JSON断片・フィールド名だけ・非文字列の条件を拒否する。実際の要件を黙って削除したり、断片をPASSに変えたりしない。計画作成・schema修復の指示にも条件文とJSON構造の区別を追加。
7. 再試行時には直近3件の実測した失敗とassertionを渡す。過去のモデルの助言を証拠として渡さず、同じ期待値の入れ替えを繰り返していた履歴を修復用モデルから見えるようにする。
8. Nodeの読み込み例外にも実際のthrow行を付ける。`document is not defined` → `beforeEach is not defined` は別の例外であり、同じ `Node.js module loading` という見出しだけで停滞と判定しない。期待値だけを変更した同じassertionは引き続き停滞の対象。
9. ある失敗でwriterへ引き継いだ過去だけを理由に、残りの試行をwriterへ固定する処理を削除。直近の失敗に基づいて既存の修復ロールを選び直す。実Nodeを通す回帰では修復→writer→別の失敗の修復の順で完了する。
10. 同じhookの例外で全テストが失敗した場合、修復入力では共通のthrow行を1件にまとめ、影響を受けたテスト名を残す。保存済みの実行レポートは全件保持する。
11. Node事前確認は `requires_tests` の実行だけを行い、先頭の計画条件のIDを借用しない。以前は先頭が「ブラウザで開く」条件でも、Node失敗にそのIDを付けていた。失敗は `REQUIRED_TESTS` に帰属し、再計画へは実行済みブラウザ確認数と、未実行条件を失敗とみなさない指示を渡す。

## 検証記録

- 回帰: `/private/tmp/olcr-loop-regression-native-20261017.log` — 261件中259成功。既知のWeb検索系2件のみ失敗。
- 最初のsandbox内実行ではloopback listenがEPERMとなったため、その結果を機能の合否に用いていない。実ブラウザを許可した上記実行で再確認。
- フロントエンド: `/private/tmp/olcr-loop-progress-ui-20261017.log` — 24件成功。
- フロントエンド型チェック成功。
- ESM回帰では空白・日本語付きディレクトリの実 `.test.mjs` をNodeで失敗させ、登録行4だけでなくassertion行6が得られることを確認。
- 隔離再現ドライバー: `/private/tmp/olcr-loop-replay-20261017.py`。元DBをread-onlyでbackupし、成果物をコピー、参照パスだけコピー先に付け替える。元Task・元成果物は操作しない。
- コピー時点で元会話の行はすでに削除されていた。初回r1は所有会話なしで開始前に停止。r2ではコピー内だけ所有会話の参照を再構成。Task ID、元要件、承認、13件のレポート、改訂・epoch・試行・復旧カウンターは保持する。
- 実モデルr2のログ: `/private/tmp/olcr-loop-replay-20261017-r2.log`。断片条件の受理を確認したためコピー内だけ一時停止を要求し、実行境界で `RESUMABLE / PAUSED` へ停止。元の履歴失敗に加え、この計画問題の再現証拠として保持する。r2のTask完了は主張しない。
- 断片条件と進捗の回帰: `/private/tmp/olcr-plan-fragment-tests-20261017.log` — 10件成功。補修後も断片が残る場合は、schema修復1回で停止する経路も追加。
- 新規実行: `/private/tmp/olcr-loop-fresh-20261017-profile`、ログ `/private/tmp/olcr-loop-fresh-20261017.log`。空フォルダ・自然文依頼・実計画・チャット承認から通常のcontrollerを通す。最終結果は追記する。
- 上記新規実行でNode読み込み例外の誤分類とwriter固定を追加発見。Task ID `native-audit-725f731dfaa1447592775435aaadd86d` のまま一時停止し、`--resume` で最新ソースへ移行。ログ `/private/tmp/olcr-loop-fresh-resumed-20261017.log`。epoch 0 → 0、計画・試行・復旧カウンターを手作業で初期化していない。「最終ソースで無介入の新規完走」という証拠ではない。
- 最終回帰（Node preflight帰属の最後の修正を除く）: `/private/tmp/olcr-loop-final-routing-all-20261017.log` — 753件、748成功・5スキップ。既知のWeb検索系2件は従来の回帰driverで対象外。
- 最後のpreflight帰属修正の回帰: `/private/tmp/olcr-loop-preflight-final-tests-20261017.log`。結果は追記する。
- 元成果物の独立診断 `/private/tmp/olcr-loop-original-history-oracle-20261017.json`。乱数0を入力条件として固定した実API呼び出しでは、抽選 `A,B,A,B,A,B`、履歴 `B,A,B,A,B` は元仕様の直近5件・連続回避を満たす一方、元のテストの固定料理名集合は満たさない。元成果物を書き換えずにテストの期待値の誤りを確認。

## 2026-10-17 18:38 のループの追加再現と修正

- 新規Taskでも acceptance 中の長い再試行を再現。計画は3工程だが、最終確認工程の `verify` が28件に膨張し、元仕様と同じ挙動の言い換えも重複していた。
- Structured Output の `checks.maxItems=1` が全条件のうち1件だけを返させていた根因は解消済み。最大6件を1呼び出しで作成するschemaに変えた状態で実モデルを再実行し、実際に6件を含む出力を確認した。
- ただし、モデルが作ったブラウザチェックが失敗すると、Reviewerが「チェックの問題」と誤判定して同じID群を再生成する。実行結果は、複数行入力の試行を単一行 `<input type="text">` が空白に潰していることを示していたが、モデルの判定は実装欠陥への引き継ぎを行わず、再生成を続けた。修正前の隔離Taskは4回以上のモデル呼び出し後、追加ループを避けるため `RESUMABLE/PAUSED` とした。Task ID `native-audit-725f731dfaa1447592775435aaadd86d`、プロファイル `/private/tmp/olcr-loop-fresh-20261017-profile`、最終ログ `/private/tmp/olcr-loop-fresh-resumed-v4-20261017.log`。このTaskは完了していない。
- 既存の構造判定は `assert.equal(inputValue, multilineText)` の形式に依存していた。新たに、元の依頼に1行ごとの入力要件があり、失敗したブラウザcheckが改行を含む入力操作を行い、DOM観測で同じIDの単一行inputが改行を保持できず、HTMLソースも一致する場合、Reviewerの意見を待たずに `SOURCE_DEFECT` と確定する。観測値は実装成功の証拠には使わず、実装工程へ戻す欠陥診断にだけ使う。抽選チェック自体が失敗した場合にも、元依頼とinput観測から原因を結び付ける。
- 元依頼の「1行に1つ」と矛盾するplanner条件「single-line input field」も、開始時の正規化で最終確認フェーズと同フェーズのTask検証条件から除く。元依頼の明示的な行ごと入力条件は保持する。textareaに正しく直したあとPlannerの誤条件が残って再失敗する二重拘束を防ぐ。
- 検証: `/private/tmp/olcr-loop-regression-tests-final2.log` — `test_executable_acceptance` と `test_orchestrator_regressions` 合計182件すべて成功。Chromium runnerのloopback通信が必要なためsandbox外の許可された実行環境で実施。`py_compile`、対象ファイルの `git diff --check` も成功。
- したがって、確認できた修正は、契約を6件単位で出すこと、今回の実際の単一行入力欠陥を同じcheck修復に戻さず実装へ帰属させること、Planner由来の矛盾条件を正規化で除くこと。現行モデルによる修正後の新規Task完走までは実証していない。元のユーザー作業フォルダおよびGUI上のTaskは変更していない。

現行Bonsai / Ornithの構成を維持。commit・push、既存GUIへの入力、ユーザープロジェクトの手作業修正は行っていない。
