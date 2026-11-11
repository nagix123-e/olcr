# LFM Audioの実行経路と測定（2026-10-27）

音声入力と回答の読み上げを、Apple Siliconでは専用の常駐MLXプロセスで実行する。
日本語は `LiquidAI/LFM2.5-Audio-1.5B-JP`、英語は `LiquidAI/LFM2.5-Audio-1.5B`。
元のチェックポイントと初回ダウンロードの同意フローを使用する。UIの変更はない。

## 測定結果

このMacの既存の日本語チェックポイントを使用。外部送信、マイク録音、音声再生なし。
TTS入力は「こんにちは。今日はいい天気ですね。」。ASRはmacOSのKyokoで生成した
「こんにちは。今日はいい天気ですね。五分のタイマーを設定してください。」のPCM16/16kHz音声。
読み上げには確率的サンプリングがあり、生成音声の長さは試行ごとに変わる。

| 再利用時の指標 | 従来のTorch/MPS | 最適化後のMLX |
| --- | ---: | ---: |
| TTS最初のPCM出力 | 2.154秒 | 0.400〜0.416秒 |
| TTS生成時間 / 音声時間（RTF、小さいほど速い） | 1.118 | 0.598〜0.635 |
| ASR最初の文字 | 0.065秒 | 0.051秒 |
| ASR完了 | 0.643秒 | 0.445秒 |

プロセス起動・モデル読込を含む初回は、TTS最初のPCMが6.073→3.736秒、
ASR最初の文字が4.129→3.239秒。初回のロード待ちは残る。
これらは短文のローカル測定で、長い音声や他の負荷・端末に同じ倍率を保証するものではない。
英語の実モデルはこの測定ではダウンロードしておらず、言語別ルーティングをテストした。
生の測定値は [benchmarks/lfm-audio-2026-10-27.json](benchmarks/lfm-audio-2026-10-27.json)。

## 実装

- 従来はMLXではなく、`liquid-audio`のPyTorch/MPS生成＋CPU波形デコーダーだった。
- 専用MLX Pythonの子プロセスにモデルを保持し、ASR/TTSで共有する。言語変更時は旧モデルを解放する。
- 固定形状の8コードブック生成を`mx.compile`でまとめる。乱数状態を明示し、時間方向のLMキャッシュとコードブック方向のキャッシュを混同しない。
- 主LMは既存のハイブリッドKV/畳み込みキャッシュを使用する。動的キャッシュ全体を無理にコンパイルしない。
- ASRのモダリティ埋め込みを一括scatterし、不要なfloat32 LM/KVキャッシュ化を除去する。
- mlx-audio 0.4.6の音声ヘッドの正規化位置、Depthformer RoPEのthetaをLiquid参照実装と揃える。共有venvのライブラリ自体は書き換えない。
- 波形を25→8フレーム単位で出力する。層の受容野＋ISTFTの重なりを含む左文脈を保持し、全プレフィックスの反復デコードを除去する。1フレームを保留し、最後に必ずflushする。
- 波形デコーダーの絶対位置を維持し、音声専用プロセスだけ`MLX_ENABLE_TF32=0`にする。float32行列形状の変更による大きな数値差を抑える。BF16主LMはそのまま使用する。
- キャンセル時は所有する音声ワーカーだけを終了し、残りの出力を次の要求に流用しない。
- MLXが利用できない環境ではTorch経路を使用する。Torch側も波形のチャンク化を適用する。生成途中に別エンジンへ切り替えない。

`mx.compile`はモデル／キャッシュを何でも引数にすれば安全に高速化するものではない。
形状変更による再コンパイルや状態の扱いは[MLXのコンパイル仕様](https://ml-explore.github.io/mlx/build/html/usage/compile.html)に従う。
float32演算の既定の低精度化は[MLXの数値精度の仕様](https://ml-explore.github.io/mlx/build/html/usage/precision.html)を参照。

## 実行設定

既存の `.venv-mlx` の `mlx 0.32.0 / mlx-audio 0.4.6 / mlx-lm 0.31.3 / transformers 5.5.0` を使用する。
別環境を用意する場合の依存関係は `backend/requirements-audio-mlx.txt`。
バックエンド用のPython 3.12と音声MLX環境のパッケージを混在させない。

| 環境変数 | 既定値と用途 |
| --- | --- |
| `OLCR_AUDIO_ENGINE` | `auto`。`mlx`で明示要求、`torch`で比較・互換経路を使用 |
| `OLCR_AUDIO_MLX_PYTHON` | 未指定時は`OLCR_MLX_PYTHON`、次にリポジトリの`.venv-mlx/bin/python` |
| `OLCR_AUDIO_MLX_BITS` | `0`。追加量子化なし。`8`/`4`は主LMのLinearだけの明示的な比較用 |
| `OLCR_AUDIO_MLX_COMPILE` | `1`。`0`でコードブック生成のコンパイルを無効化し比較可能 |

4/8bit量子化は音声エンコーダー、音声ヘッド、埋め込み、波形デコーダーには適用しない。
8bitでは主LMの量子化により約0.97GBのMLXアクティブメモリ削減を確認したが、
広範な認識精度・発話品質の評価は未実施のため、既定は元の精度を維持する。

## 検証・再測定

通常の回帰テストと、既存ローカル重みを使う数値テスト：

```sh
PYTHONPATH=backend OLCR_TEST_LFM_LOCAL=1 .venv312/bin/python -m unittest \
  backend.tests.test_lfm_audio_real backend.tests.test_audio_performance
```

実モデルの117フレームを全体／チャンクで比較し、欠落・重複なし。
MLX波形の差は最大0.000067、RMS 0.00000115（PCM16の3段階以内）。
同じ隠れ状態でLiquid参照・MLX・コンパイル後の8音声コードが一致した。
キャンセル、ワーカー再利用、Python環境分離、入力検証、日英選択、ハンズフリーのローカル実行も検証した。

```sh
.venv312/bin/python scripts/benchmark_lfm_audio.py --engine mlx --runs 3 --output /tmp/lfm-mlx.json
.venv312/bin/python scripts/benchmark_lfm_audio.py --engine torch --legacy --runs 3 --output /tmp/lfm-legacy.json
.venv312/bin/python scripts/benchmark_lfm_audio.py --engine mlx --asr-wav /absolute/path/fixture.wav --output /tmp/lfm-asr.json
.venv-mlx/bin/python scripts/validate_lfm_audio_mlx.py /absolute/path/to/existing/snapshot
```

いずれも既存のローカル重みのみを使用する。モデル未取得時は失敗し、無断ダウンロードしない。
稼働中のユーザーのOLCRには操作・終了・再起動を行っていない。適用はOLCRの再起動後。
