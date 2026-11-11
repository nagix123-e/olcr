# チャットからのタイマー・予定・YouTube操作

通常の入力欄と音声入力から利用する。専用のホーム画面やサイドバーメニューは追加しない。

## 使い方

| 操作 | 依頼例 |
| --- | --- |
| タイマー開始 | 「5分のタイマーをかけて」「set a timer for ten minutes」 |
| 一覧・停止・再開 | 「タイマー一覧を見せて」「5分のタイマーを止めて」「タイマーを一時停止して」 |
| 予定一覧 | 「今日の予定は？」「明日の予定を教えて」 |
| 予定追加 | 「明日の15時に『歯医者』の予定を追加して」 |
| 予定変更・削除 | 「明日の会議を16時に変更して」「『歯医者』の予定を削除して」 |
| YouTube再生 | 「YouTubeで曲名を再生して」「YouTubeで動画URLを再生して」 |
| 再生制御 | 「YouTubeを一時停止して」「YouTubeを再開して」「YouTubeを止めて」 |

`/timer start 5 minutes`、`/timer list`、`/calendar list`、`/calendar add`、`/youtube play 動画URL` なども使える。プロジェクトは不要。コーディングの追加指示をキューに送る設定でも、日常操作の依頼は通常チャットとして処理する。

## タイマー

1秒〜7日、実行中・一時停止中を合わせて100件まで。状態をアプリのDBに保存し、終了時刻をmacOS UserNotificationsへ登録する。会話の切り替えやOLCR終了後の配信はmacOSが担当する。通知許可が必要で、拒否された場合はチャット内にその旨を表示する。Macのスリープ中の配信時刻や集中モードでの表示はmacOSの設定に従う。

停止・再開はタイマーIDに対して実行する。複数ある場合は対象を選び、明示した場合にだけ一括操作する。通知許可を待っている途中で停止されたタイマーは、遅れて通知登録されない。

## Google Calendar

Settingsの「カレンダーと再生」から接続する。配布用クライアントIDが未設定の開発ビルドでは、初回のみGoogle CloudでCalendar APIを有効化し、デスクトップアプリ用OAuthクライアントIDとシークレットを登録する。OAuth同意画面がテスト状態なら利用するGoogleアカウントをテストユーザーとして登録する。

システムブラウザでOAuth認証し、PKCEと一回限りのstateでループバックコールバックを検証する。クライアント情報とリフレッシュトークンはmacOS Keychainに保存し、アプリDBには保存しない。`OLCR_GOOGLE_CALENDAR_CLIENT_ID` / `OLCR_GOOGLE_CALENDAR_CLIENT_SECRET` がアプリ起動時の環境にある場合は初期値として使用できる。

参照は接続先カレンダーを使用する。追加・変更・削除はチャットに確認内容を表示し、確認ボタン、または直後の「はい」「それで登録して」で実行する。日時・タイトルは確認画面で修正可能。変更・削除は取得時のETagで検証する。通信中断で書き込み結果が不明な場合は再実行せず、結果未確認と表示する。繰り返し予定の操作は表示した1回分が対象。

OAuthクライアントのGoogle側の登録・公開審査は配布者側の準備事項であり、架空のクライアントIDは同梱しない。

## YouTube

動画URLを直接指定できる。曲名検索には既存のBrave/Tavily認証情報を使用し、任意でYouTube Data APIキーもSettingsからKeychainへ登録できる。

公式IFrame Player APIを使い、会話内に見えるプレイヤーで再生する。TauriのカスタムスキームをYouTubeへ渡さないよう、秘密の短期チケットで保護したローカルHTTPドキュメントを介する。そこにはOLCRのセッショントークンやOAuth認証情報を渡さない。

ブラウザが自動再生を止めた場合は、公式プレイヤーの再生ボタンを押す。埋め込みを許可していない動画は「YouTubeで開く」から再生する。会話を切り替えた時、プレイヤーが見えなくなった時、アプリがフォーカスを失った時は一時停止する。過去の会話を開くだけでは再生しない。再生状態の表示はプレイヤーの実イベントから更新する。

## 実装箇所

- `backend/olcr_api/assistant_actions.py`: 指示の判定、タイマー永続化、予定操作の確認・重複防止
- `backend/olcr_api/assistant_integrations.py`: Google OAuth/Calendar、YouTube検索と公式プレイヤー
- `backend/olcr_api/assistant_api.py`: 既存Settingsとチャット内操作向けAPI
- `desktop/src-tauri/src/assistant_notifications.rs`: macOSの通知登録・キャンセル
- `frontend/src/AssistantActions.tsx`: 会話内の操作表示
- `frontend/src/AssistantSettings.tsx`: 必要な接続設定
- `frontend/src/assistantBridge.ts`: 通知とKeychain保存の同期

外部アクセスをオフにした場合もローカルタイマーは使用できる。カレンダーとYouTubeは既存の外部アクセス設定に従う。
