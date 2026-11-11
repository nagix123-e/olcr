# 28件の修正後 GUI E2E 記録（2026-10-11）

## 結果

28件中5件を、修正後の署名済み隔離アプリで同じ会話に対してGUI E2Eしました。バックエンド上は3件完了、2件失敗です。意味判定はPASS 2、PARTIAL 1、FAIL 2。残り23件は未実施です。

| 元番号 | ケース | Task | 意味判定 | 結果 |
|---:|---|---|---|---|
| 7 | `space.launches#3` | completed | PASS | 現在予定・公表更新を表示。延期履歴と現状を混同しない。 |
| 24 | `space.iss_position#4` | completed | PARTIAL | 座標等は表示したが、近隣地域の概略推定は出ず。 |
| 27 | `space.exoplanets#3` | failed | FAIL | `WEB_PROVIDER_NOT_CONFIGURED`。画面がGenerating responseのまま残留。 |
| 34 | `japan.law#2` | completed | PASS | 目的条文と事業者義務を条文本文付きで比較。 |
| 42 | `earth.streamflow#2` | failed | FAIL | `WEB_PROVIDER_NOT_CONFIGURED`。画面がGenerating responseのまま残留し、続行不能。 |

## 実行条件

- build ID: `olcr-fix28-r47c-20261011-r6`
- bundle: `/private/tmp/olcr-fix28-r47c-20261011-r6-target/debug/bundle/macos/OLCR.app`
- profile: `/private/tmp/olcr-fix28-r47c-20261011-r6-profile`
- conversation: `743c2f20-54a2-414e-8d8a-7cf889f3488f`
- log: `/private/tmp/olcr-fix28-r47c-20261011-r6.log`
- Keychain restore enabled。Launcher reported READY_FOR_COMPUTER_USE=YES; app/backend PID and isolated port verified at startup.
- Exact isolated app was quit through its GUI. Shell-only check confirmed app/backend PID and dedicated port were no longer present.

## 停止理由

ケース27と42でバックエンドのtaskがfailedになった後も、GUIが生成中表示を解除せず、送信操作を再有効化しませんでした。ユーザー会話やプロセスへの影響を避けるため、42で停止しました。このローダー残留はE2E上の不具合として扱います。ケース47以降の23件は実行していません。

ケース別の証跡と修正台帳へのリンクは[JSON記録](SEMANTIC_FOLLOWUP_GUI_E2E_28_20261011.json)を参照してください。


## 再開セッション

新しい隔離ビルド `olcr-fix28-r47c-20261011-r7` で再開し、ケース47・53・56・58・102はGUIから送信され、外部APIタスクと回答が完了しました。ケース107はChess.comへのHTTP応答が返った後、通常Brain合成で止まり、回答・終端タスクが保存されないままGUIが生成中表示を続けました。ケース75・76・90・97・108は実際の送信が確認できなかったため、実行済みに数えていません。

累計で10件のAPIタスクが完了、2件が失敗、1件が未完了です。未実施は17件です。再開セッションのビルド、ログ、各ケースの記録は[JSON](SEMANTIC_FOLLOWUP_GUI_E2E_28_20261011.json)に追記しました。


## 2回目の再開セッション

ビルド `olcr-fix28-r47c-20261011-r8` でケース121・124をGUI実行し、API応答とアシスタント回答の保存を確認しました。121は徒歩と自転車の距離・時間差を算出しPASS、124は候補中の最短経路を提示しましたが、指定のない車を既定にしたためPARTIALとしました。

ケース142はNASA EONETから55件を受信した後、Brain合成が完了せず回答が保存されませんでした。GUIが再び生成中で止まったため終了しました。累計14件に着手し、12件のAPI taskがterminal、2件は失敗、2件は回答未完了です。残り14件は未実施です。


## 最終再開セッション

ケース59、75、76、90、97、108を実際にGUIから送信しました。75・76は完了、90は回答が正答を露出する意味上のFAIL、59・97・108は回答生成が終わらず未完了です。ケース165・169・174・192・316・345・346・148もGUI実行結果を追加記録しました。ケース165は回答合成に約2分、316も検索結果の整理に約1分半かかりました。

**28件すべてのプロンプトをGUIから試行**しました。タスクが終端状態になったのは23件（completed 20、failed 3）、応答未完了が5件です。未完了は107・142・59・97・108で、生成中表示が残り次の送信ができない状態を記録しました。

## 追加で確認した重要な失敗

- ケース90（Open Trivia DB）は四択クイズを返しましたが、回答本文に正解を含めました。正答を表示しない修正要件を満たしていません。
- ケース174（食品）は商品指定がないため、商品名またはバーコードを求める確認質問を返しました。
- ケース124（経路）は候補中の最短を示しましたが、指定のない車を移動手段に選んだためPARTIALです。

## E2E後の追加修正

この実行で識別したPARTIAL・FAIL・INCOMPLETE計11件と、ケース47の観察行欠落への補修は[追加修正台帳](SEMANTIC_FOLLOWUP_REPAIRS_AFTER_28_20261011.md)に記録しました。修正後のGUI再E2Eは未実施で、上記の実行結果・合否判定は保持しています。
