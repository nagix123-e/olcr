# 回答の読み上げ / Read aloud

「設定 → 一般 → 読み上げ」で有効にする。初期値はオフ。設定はこの端末に保存される。

- 日本語: `LiquidAI/LFM2.5-Audio-1.5B-JP`
- English: `LiquidAI/LFM2.5-Audio-1.5B`
- モデル未取得時は同意後にダウンロード。音声入力と同じキャッシュを共用する。
- 回答の文章を短く区切ってローカル合成し、24 kHz PCMを順番に再生する。回答全体の完成を待たず、音声も生成途中からストリーミングする。合成・再生の遅延は端末性能による。
- オフ、会話・言語の変更、生成停止、音声入力の開始、読み上げ停止で、未再生の音声と合成要求をキャンセルする。
- 履歴の表示やバックグラウンドの別会話は読み上げない。コードブロック、内部のコーディング状態、認証設定カードは音声化しない。

## 実装

`audio_synthesis.py` はASRとモデル・排他ロックを共用する。Liquid AudioのCUDA固定の遅延デコーダーをMacでも使えるようCPUに構築する。生成は専用スレッド内で行い、切断・キャンセル時にロックを解放する。

`readAloud.ts` は新規送信だけを対象に文章を分割し、逐次PCMをWeb Audioにスケジュールする。設定変更や会話移動で古い要求を中止し、遅れて到着した音声を再生しない。

API: `GET /api/audio/synthesis/status?language=ja|en`、`POST /api/audio/synthesize/stream`。ダウンロードには既存の同意付き `/api/audio/model/prepare` を使用する。

公式のTTSプロンプトと音声コードの仕様:

- https://huggingface.co/LiquidAI/LFM2.5-Audio-1.5B-JP#tts
- https://github.com/Liquid4All/liquid-audio
