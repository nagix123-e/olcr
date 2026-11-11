# ローカルモデルのコンテキスト設定

設定の「チャットで使う機能」にある共通トグルで、YaRN・コンテキスト自動圧縮のオン／オフを切り替える。切替はその場で保存される。モデル設定の「ローカルモデルのコンテキスト」では延長先・通常の長さだけを編集し、機能がオフでも事前設定できる。数値の保存はトグルを上書きしない。保存値はアプリの設定DBに永続化され、次のリクエストから反映される。既存プロファイルでは両機能ともオフ。

| 設定 | 初期値 | 動作 |
| --- | --- | --- |
| YaRN | オフ | MLX版Ornith/Bonsaiのテキスト注意層へ推論時に適用 |
| YaRNの延長先 | 524288トークン | モデル設定の本来の長さから倍率を計算。本来の長さ以下ならRoPEを変更しない |
| 自動圧縮 | オフ | 推定入力量＋応答予約量がウィンドウの80%に達したら古い文脈・参考資料を抜粋 |
| 通常の長さ | 32768トークン | 圧縮基準。YaRNを有効にしたMLXでは延長先を使用。Ollamaでは`options.num_ctx`にも使用 |

両トークン数の設定範囲は2048〜2097152。YaRN有効時は延長先を通常の長さ以上にする。検索資料の文字数制限（既存のRAG設定）とは別の設定。

自動圧縮はモデルによる要約ではなく、古いメッセージの先頭・末尾や参考資料の抜粋。省略を明示し、元の会話DBは書き換えない。現在の入力、通常のsystem指示、ツール呼出し情報を保持する。短縮は文字数・推定トークン数の両方が減る場合だけ適用する。補助資料の識別用プレフィックスを含むユーザー入力もそのまま保持する。MLXは実トークン数で残り容量を計算し、応答の最大トークン数を収まる範囲まで減らす。入力だけで上限に達した場合は`CONTEXT_WINDOW_EXCEEDED`を返す。Ollamaは圧縮後も推定入力量と応答予約量の合計が上限を超える場合、送信前に`context_window_exceeded`で停止する。応答枠は通常の半分以下を予約して`options.num_predict`にも適用する。Ollamaの入力トークン数は推定値。

外部APIと画像推論には適用しない。OllamaのAPIからYaRNの周波数設定は変更できないため、Ollama選択時にYaRNをオンにした呼出しは`yarn_unsupported`。MLXを選ぶかYaRNをオフにする。既存のフォールバック設定は引き続き適用される。

YaRNは重みや配布モデルの設定ファイルを書き換えない。BonsaiではMRoPEの位置軸を保持してYaRN周波数とattention scalingを設定する。attention scalingを省く既存のfused kernelはこのリクエストでは使わない。長いコンテキストほどメモリと処理時間が増加し、品質も変化する。設定上限は品質保証値ではない。

API: 共通トグルは`PATCH /api/settings/capabilities`で`yarn_enabled`・`context_auto_compress`を部分更新。数値設定は`PATCH /api/settings/retrieval`で更新。後者は互換性のため`yarn_enabled`, `yarn_target_tokens`, `context_auto_compress`, `local_context_tokens`を部分更新。`GET /api/settings`で取得。既存の設定更新・再読込にも対応。

検証: `PYTHONPATH=backend:backend/tests .venv312/bin/python -m unittest test_context_extension test_context_selection_policy test_mlx_worker test_model_roles`（`OLCR_DB_PATH`を一時DBに指定）。Metal数値テストは`OLCR_TEST_MLX_CONTEXT=1 PYTHONPATH=backend:backend/tests .venv-mlx/bin/python -m unittest test_context_extension_mlx`。長文での品質評価と実GUIでの確認は未実施。

参考: [MLX YaRN実装](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/models/rope_utils.py)、[Ollamaのコンテキスト長設定](https://github.com/ollama/ollama/blob/main/docs/context-length.mdx)。実装時はローカルに固定されたMLXランタイムのAPIも確認。
