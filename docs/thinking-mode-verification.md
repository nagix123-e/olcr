# 思考モードと受信ログの実装・検証（2026-10-28）

既存の推論強度スライダーを使い、ローカルモデルのゼロ / 中間 / MAXを
`off / auto / on` に対応させた。追加した専用の切り替えUIと独立した設定項目は削除。
既存の `efforts` を会話・用途ごとに保存するため、外部APIの強度設定とモデル選択UIは維持する。
古いプロファイルの未設定値は自動として扱い、設定変更だけでモデルを呼び出さない。
変更は次の呼び出しに適用し、実行中の呼び出しは設定とフォールバック順を固定する。

JSONの実行指示および明示的に思考オフを要求する内部処理は、MAXでもオフを維持する。
JSON文法と自由な思考出力を混在させない。追加の分析呼び出しは作らない。

## 仕様と実装の境界

[動作契約](thinking-mode-contract.md) と独立した判定・パーサーのテストを先に分け、
その後に既存プロバイダー・保存処理・画面へ接続した。同じ実装者が既存ソースも
確認しているため、厳密な意味のクリーンルーム開発ではない。

Codex App Serverの独立したreasoning項目、差分イベント、完了時の確定状態と、
Responses APIの回答／要約の分離を参考にした。公開仕様は契約文書のリンクを参照。
独自コードを実装し、ChatGPT/Codexの非公開UIコードは使用していない。

## ログの動作

- 通常の応答に含まれている思考出力だけを、回答とは別の折りたたみに表示する。
  開閉のための推論、要約、追加リクエストはない。
- OpenAIやGeminiに、新たな要約生成・`includeThoughts`を要求しない。
  プロバイダーがテキストを返さない場合は、空のログ欄を表示しない。
- Ollamaは思考の差分を表示。現在のMLXワーカーと非ストリームの外部APIは
  応答が戻った時点で表示する。MLXの思考を逐次表示する実装ではない。
- 完了・未完了・省略を区別し、1呼び出し64,000文字に制限する。
  ストリームは差分を送り、最後に確定したログで置き換える。
- SQLiteに回答と分けて保存し、履歴を開き直しても表示できる。
  ログは会話コンテクスト、実行JSON、回答のコピー／読み上げに加えない。
- 暗号化された思考、署名、redactedブロックを表示・復号しない。

思考オン自体の計算・トークンは消費する。ログ表示のための追加生成がないことと区別する。

## 検証結果

| 検証 | 結果 |
| --- | --- |
| Pythonの判定・パーサー・受信ログ・API往復・役割・フォールバック・コンテクスト・アカウントの関連テスト | 260件成功 |
| Thinking / BrainModels / ModelsPanel / ConversationIsolation / ChatComposer のUIテスト | 88件成功 |
| フロントエンド本番ビルド・型検査 | 成功。従来の大きいchunk警告あり |
| Pythonの変更9モジュールの構文解析、変更した追跡ファイルの `git diff --check` | 成功 |
| 実Ornith MLX：オフ・オン・JSON | 3ケース成功 |
| 実Bonsai MLX：オフ・オン・JSON | 3ケース成功 |
| ブラウザの固定応答：ゼロ／自動／MAX保存、保存中の操作抑止、展開・キーボード操作、受信中→完了 | 確認済み |
| 会話を切り替えた後の遅延ログ、失敗時の未完了ログ、保存復元、後続コンテクストへの不混入 | テスト成功 |
| 要約表示の追加要求なし、通常応答1回の呼び出し数・報告トークン数の維持 | テスト成功 |

実モデルの入力は短い `2 + 2` のみ。オンではOrnithが163文字、Bonsaiが74文字の
思考を回答と分けて返し、オフとJSONでは思考テキストなし。JSONは両方とも
`answer = 4` を満たした。速度・品質の比較ベンチマークではない。

検証ログは `/private/tmp/olcr-thinking-verified-regression.log`、
`/private/tmp/olcr-thinking-ui-verified.log`、`/private/tmp/olcr-thinking-verified-build.log`、
`/private/tmp/olcr-thinking-live-smoke.log` に保存。

## 変更前から失敗する既存テスト

上記260件では次の5件を除外した。作業開始時に保存したソースを別プロセスで
読み込む比較実行でも、5件すべて同じ失敗を再現した。
比較記録：`/private/tmp/olcr-thinking-baseline-results.log`。

- `test_brain_providers.BrainProviderTests.test_google_and_anthropic_keep_system_messages_and_structured_contract`
- `test_brain_providers.BrainProviderTests.test_nvidia_nim_discovers_models_and_uses_openai_compatible_chat_api`
- `test_brain_providers.BrainProviderTests.test_remote_calls_require_external_permission_and_keep_requested_model`
- `test_brain_providers.ProviderUsageTelemetryTests.test_missing_usage_is_a_request_and_schema_retries_count_each_attempt`
- `test_brain_api.BrainApiTests.test_stream_uses_conversation_selection_and_retains_neural_route`

前4件は既存の構造化出力ポリシーに対する期待値、後1件は既定モデルがOllamaという
期待値に関係する。今回の変更でこれらのポリシーは変更していない。

## better-uiレビュー

### 配置・プロジェクトのトークン

| Severity | Location | Before | After | Why |
| --- | --- | --- | --- | --- |
| HIGH（修正済み） | `frontend/src/thinking.css:2` | 既存の回答位置オフセットで新しいログと回答の左端が欠ける | ログを持つ回答を列内に収め、開閉しても幅を維持 | 読める配置を保ち、展開操作で内容を失わせない |
| MEDIUM（修正済み） | `frontend/src/brain-models.css` の `.brain-local-effort` | 新たに表示するローカルの強度名が淡いアクセント色 | 既存の本文色トークンを使用 | 選択状態を読みやすい静的な文字で示す。外部APIのカードは維持 |

### Match icon stroke to text weight / Motion restraint

| Severity | Location | Before | After | Why |
| --- | --- | --- | --- | --- |
| LOW（修正済み） | `frontend/src/thinking.css` の `.reasoning-chevron` | 通常本文に既定2pxのアイコン | 1.5px、currentColorに統一 | 本文の太さと揃える |

開閉は120ms、`cubic-bezier(0.2,0,0,1)`、transformだけを遷移する。
reduced motionでは遷移なし。初期表示時の入場アニメーションなし。
開閉、選択済み、保存中、空、受信中、完了、未完了、省略、キーボードフォーカスを確認。
hoverと正確なtransition指定はソースで確認した。

Not verified：ネイティブOLCRアプリ全体のGUI E2E、実際の外部AIサービスへの送信、
IABでのOSクリップボード書き込み、10%速度のAnimationsパネル再生。
OSクリップボードはIABで読み出せなかった。コピーのハンドラーは回答本文だけを渡すことを
コードで確認した。ブラウザ確認は専用固定応答であり、実バックエンドのGUI E2Eではない。

No actionable UI-polish findings remain in the inspected additions.

Approve（上記の確認範囲のみ）
