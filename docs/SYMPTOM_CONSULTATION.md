# 通常チャットでの症状相談（試験機能）

通常のチャットに「今、頭痛があるのだけど何が疑われる？」と入力する。原因候補、必要な追加質問、受診の目安と診療科、一般的な対処をすべて通常のアシスタントメッセージとして返す。専用画面、メニュー項目、診断カードは追加しない。

OpenSymptomは症状解説への参考リンク。症状評価にはInfermedica Engine APIを使い、OpenSymptomの記事の自動取得・複製・索引化は行わない。

## 対話の流れ

1. 「頭痛がする」など、個人の症状についての相談を検出する。一般知識の質問、プログラム実装の依頼、明示的なスキル呼出しは従来の経路へ渡す。
2. まず緊急症状をチャットで確認する。緊急性がある・判断できない場合は通常の候補表示を進めず、受診へ案内する。
3. 年齢、評価用の性別、現在の国、妊娠中・産後・免疫抑制状態の該当有無をチャットで尋ねる。初期対象は18〜64歳で、妊娠中・産後・免疫抑制状態は対象外。
4. Infermedicaへの送信内容を説明し、「同意します／同意しない」で確認する。同意までは症状の外部検索・評価を行わない。
5. 日本語の自由文解析は同APIの対応範囲外なので、症状名を検索し、該当する症状を番号で確認する。症状を推測して確定しない。
6. 必要な追加質問を同じ会話で行う。「分からない」は不明として扱い、「いいえ」と区別する。追加の評価質問は最大6回。
7. 情報が揃えば、エンジンの一致度順に原因候補を最大5件、受診の緊急度と診療科を文章で返す。数値の確率は表示しない。セルフケア可とされた頭痛に限り、薬・用量を指定しない一般的な対処も付ける。

「中止」「外部送信をやめて」で相談を終了できる。問診中に「緊急症状がある」と伝えた場合も救急案内へ切り替える。別の話題へ移った場合、症状問診へ無理に引き戻さない。緊急確認はあらゆる緊急疾患を検出する仕組みではない。

## 接続設定

機能は既定OFF。バックエンドの起動環境に次の設定が必要。

| 環境変数 | 設定 |
| --- | --- |
| `OLCR_SYMPTOM_ENABLED` | 明示的に有効化する場合のみ `1` |
| `OLCR_INFERMEDICA_APP_ID` | 利用者のInfermedica App ID |
| `OLCR_INFERMEDICA_APP_KEY` | 利用者のInfermedica App Key |

OLCRの外部アクセス設定も必要。日本語では`Model: infermedica-ja`を使い、契約の対応範囲を超えて英語へ自動切替しない。専門科推薦を取得できない場合は一般の受診窓口を示し、その旨を明記する。未設定やサービス障害もチャットで回答し、生成モデルに順位を捏造させない。

初期実装はバックエンド環境変数による設定で、キー入力画面やKeychain保存は含まない。Finder起動の既存アプリがシェルの環境変数を継承するとは限らない。キーをソースや会話に貼り付けない。

## 保存と送信

通常チャットのため、相談内容と回答は既存の会話履歴に保存される。対話を継続するための内部状態も、その会話のアシスタントメッセージに付属するメタデータとして保存する。別会話へ引き継がず、開始から30分を超えた状態では問診を再開しない。期限切れや中止によって過去の会話履歴が削除されたことにはならない。

同意後は検索する症状名、年齢、性別、選択した症状と回答をInfermedicaへ送信する。過去のチャット全文・添付ファイル・APIキーを問診データとして送らない。送信先での保存・削除は提供元の契約に従う。OpenSymptomの参考リンクには健康情報を付加しない。

## 実装・検証の範囲

`symptom_chat.py`が通常チャットのルーティング、対話状態、文章の回答を扱う。`/api/chat`と`/api/chat/stream`は共通処理を使い、追加のUIブロックを返さない。`symptom_consultation.py`が入力検証・評価を、`symptom_provider.py`が固定先のHTTPS通信を扱う。通信ログと入力エラーに健康情報を含めない。

テストは模擬API・合成データ・隔離したDBで実施。実契約・実API、臨床評価、ネイティブGUIでの受入確認は未実施。配布前の臨床評価・日本向け適用範囲の確認は残作業。

参照：[Infermedicaの日本語モデル](https://developer.infermedica.com/documentation/engine-api/basics/)、[症状検索](https://developer.infermedica.com/documentation/engine-api/build-your-solution/search/)、[問診と候補](https://developer.infermedica.com/documentation/engine-api/build-your-solution/diagnosis/)、[緊急度](https://developer.infermedica.com/documentation/engine-api/build-your-solution/triage/)、[診療科](https://developer.infermedica.com/documentation/engine-api/build-your-solution/specialist-recommender/)、[頭痛の一般的な対処：NHS](https://www.nhs.uk/symptoms/headaches/)、[OpenSymptom利用規約](https://www.opensymptom.com/terms)。
