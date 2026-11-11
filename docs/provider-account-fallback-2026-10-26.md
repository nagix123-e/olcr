# プロバイダ内の複数アカウントとモデルフォールバック

2026-10-26 JST。

## 動作

設定 → モデル → 接続先 → 各プロバイダの「アカウントと優先順位」で、最大3つの接続を設定できる。OpenAI互換API、Google、Anthropic、NVIDIA NIM、Hugging Face、およびChatGPT OAuthに対応。

- APIキーは「＋ アカウントを追加」または各順位の設定から登録。表示名も設定可能。保存済みキーを変更しない編集では、キー欄を空欄にできる。
- ChatGPT OAuthも各枠から個別に認証し、それぞれのトークン・更新トークン・Keychain保存先を分離する。各枠に接続したいChatGPTアカウントで認証する。
- ↑ / ↓ でプロバイダ内順位を変更する。順位変更は認証情報の移動・コピーを伴わない。
- 既存のAPIキー／ChatGPT OAuthは従来の保存先を使うアカウント1として引き継ぎ、初期順位は1位。

実行順は次のとおり。開始モデルは従来どおり現在の役割で選択されているモデル。

```
現在のモデルA：そのプロバイダの1位 → 2位 → 3位
    ↓ 全アカウントで失敗
設定済みフォールバックモデルB：そのプロバイダの1位 → 2位 → 3位
    ↓ 全アカウントで失敗
次のフォールバックモデルC …
```

未接続の枠、およびそのモデルが登録されていない枠はスキップする。次モデルが同じプロバイダでも、保存した1位から再開する。成功した時点で後続のアカウント・モデルは呼ばない。上限・認証期限・一時的な失敗・空応答・不正な応答形式をアカウント切り替えの対象とし、外部アクセス権限は迂回しない。

コーディング、短いコーディング・修正、推論、チャットで同じ規則を使用する。Brain接続を使う画像モデルも同じアカウント順を使い、画像をテキストへ変換して再送しない。APIを使わないローカルモデルの経路は従来のまま。

アカウント切り替え時も既存のフォールバック通知経路へ情報を渡す。モデルIDは引き続き `api:<provider>:<model>` で、同じモデルがアカウント数だけ選択肢に重複しない。

## 保存と互換性

認証情報はメモリとmacOS Keychainに保持。SQLiteには接続先、モデル一覧、表示名、アカウント番号、順位のみを保存する。追加枠のKeychain名は `OLCR_BRAIN_<PROVIDER>_2` / `_3`。既存の1枠目は従来名を保持する。ネイティブ側の許可リストも追加枠に対応し、4枠目の認証情報は受け付けない。

ChatGPT OAuthの認証開始はアカウント番号を指定し、コールバックの保存先もその枠に固定する。モデル一覧のキャッシュ・認証期限の状態・更新トークンの保存待ち確認を枠ごとに分離。順位が変わっても保存済みの認証情報と確認対象が混ざらない。単一枠で複数のOAuth認証を同時に開始することは拒否する。

HTTP APIの追加・拡張:

- `PUT /api/brain/providers`: `account_id`（1〜3、省略時1）。
- `PUT /api/brain/providers/{provider}/accounts/order`: `{"account_order":[2,1,3]}`。3枠の順列を検証。
- `POST /api/brain/oauth/start?account_id=2`: 指定したChatGPT OAuth枠で認証。
- `GET /api/brain/models`: プロバイダごとに `accounts` と `account_order` を返し、モデル一覧は重複排除。
- OAuth保存待ち応答にはアカウント別の `handle` と確認用 `version` を含める。キー・トークンを一般のモデル一覧に含めない。

## 検証

- バックエンド96件合格: `test_provider_accounts`, `test_brain_providers`, `test_brain_fallback_order`, `test_brain_models`, `test_purpose_models`, `test_brain_api`, `test_external_credentials`。
- UI30件合格: `ProviderAccounts.test.tsx`, `ModelsPanel.test.tsx`, `BrainModels.test.tsx`, `OAuthRecovery.test.tsx`。
- Rustネイティブ許可リスト2件合格: `cargo test --offline --manifest-path desktop/src-tauri/Cargo.toml credential_bundle_allowlist`。
- フロントエンドの型検査・本番ビルド、Pythonコンパイル、対象ファイルの差分チェック合格。

主な回帰検証は、各Brain役割で全3アカウントを試してから次モデルへ進むこと、同一プロバイダの次モデルで1位から再開すること、2枠目にしか存在しないモデルの解決、モデル一覧キャッシュの分離、ChatGPT OAuthの2枠目への認証・トークン更新・上限後の切り替え、3枠分のKeychain保存待ち処理、画像入力を保持した切り替え、不正応答からの復帰、4枠目の拒否。

テストは偽の認証情報・モック応答とループバックのOAuthコールバックで実施した。最終のHTTP APIテストは `/private/tmp/olcr-account-api-tests-20261026` の専用DBを使用。実際の複数ChatGPTアカウントの認証や、有料モデルへの実通信、実GUI E2Eは今回実施していない。

既存テスト3件が現行仕様と不一致だったため、廃止済み `vision_fallback` の期待、画像入力とテキスト生成の両対応モデルの除外期待、明示的なOllama選択をMLXへ自動移行する期待を更新した。製品側のローカルモデル選択動作は変更していない。
