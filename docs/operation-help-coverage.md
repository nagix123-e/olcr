# Operation help question coverage

53 topics; 212 anticipated questions. Sources identify the implementation reviewed when writing the guide.

## OLCRの使い方 / Using OLCR

Guide: `overview`; parent: `overview`.

Implementation: `frontend/src/main.tsx`.

- OLCRの使い方を教えて
- このアプリで何ができる？
- OLCRの操作ガイドはある？
- How do I get started with OLCR?
- What can OLCR do?

## 言語の変更 / Changing language

Guide: `language`; parent: `overview`.

Implementation: `frontend/src/language.tsx`.

- OLCRを日本語にするには？
- UIの言語はどこで変える？
- OLCRを英語表示に変える方法は？
- How do I change the language in OLCR?
- Where is the OLCR language setting?

## モデルの選択 / Choosing models

Guide: `models`; parent: `overview`.

Implementation: `frontend/src/ModelsPanel.tsx`.

- Brainのモデルはどこで選ぶ？
- OLCRの推論強度はどう変える？
- コーディングのモデルを変える方法は？
- How do I choose a Brain model?
- Where do I adjust model effort in OLCR?

## APIアカウントの接続・追加 / Connecting API accounts

Guide: `accounts`; parent: `overview`.

Implementation: `frontend/src/ProviderAccounts.tsx`.

- ChatGPTをOLCRにつなぐには？
- APIアカウントの使い方を教えて
- 接続先の設定方法は？
- How do I connect my ChatGPT account to OLCR?
- Where do I configure API accounts?

## 音声入力 / Voice input

Guide: `voice`; parent: `overview`.

Implementation: `frontend/src/ChatComposer.tsx`.

- 音声入力の使い方を教えて
- 録音を開始するには？
- 波形ボタンで音声入力を送る方法は？
- How do I use voice input in OLCR?
- How do I send a voice transcript?

## 回答の読み上げ / Read aloud

Guide: `readout`; parent: `overview`.

Implementation: `frontend/src/readAloud.ts`.

- OLCRの読み上げはどこ？
- 回答を読み上げる方法は？
- 読み上げに使うモデルを教えて
- How do I enable read aloud?
- Which model does read aloud use?

## Thinkingとログ / Thinking and logs

Guide: `thinking`; parent: `overview`.

Implementation: `backend/olcr_api/thinking.py`.

- Thinkingの使い方を教えて
- 思考をMAXにするには？
- 思考の自動とゼロの違いは？
- How do I use Thinking in OLCR?
- What does MAX thinking mean?

## Quickモード / Quick mode

Guide: `quick`; parent: `overview`.

Implementation: `backend/olcr_api/brain_providers.py`.

- Quickモードの使い方は？
- OLCRの稲妻は何？
- Quickはどこでオンにする？
- How do I enable Quick?
- What is fast mode in OLCR?

## カスタムエージェント / Custom agents

Guide: `agents`; parent: `overview`.

Implementation: `frontend/src/CustomAgents.tsx`.

- カスタムエージェントを使うには？
- エージェントの作り方を教えて
- エージェントのアイコンを設定する方法は？
- How do I create a custom agent?
- How do I change an agent icon?

## Skillsの表示と利用 / Viewing and using Skills

Guide: `skills`; parent: `overview`.

Implementation: `frontend/src/SkillsLibrary.tsx`.

- OLCRのSkillsはどこ？
- スキルの見方を教えて
- Skillsの画面から戻るには？
- Where is the Skills screen?
- How do I browse installed skills?

## プロジェクトと作業フォルダー / Projects and folders

Guide: `projects`; parent: `overview`.

Implementation: `frontend/src/main.tsx`.

- OLCRの作業フォルダーの設定方法は？
- プロジェクトの使い方は？
- 作業フォルダを指定するには？
- How do I choose a project workspace?
- What is the working folder in OLCR?

## コーディングと再開 / Coding and resuming

Guide: `coding`; parent: `overview`.

Implementation: `backend/olcr_api/app.py`.

- コーディングオーケストレーターの使い方は？
- OLCRで実装を頼む方法は？
- コーディングの検証結果はどこ？
- How do I request coding work in OLCR?
- Where are coding verification results?

## 操作権限 / Permissions

Guide: `permissions`; parent: `overview`.

Implementation: `frontend/src/ChatSettingsCard.tsx`.

- OLCRの権限設定はどこ？
- 外部アクセスをオンにするには？
- フルアクセスの操作方法を教えて
- How do I enable external access?
- Where are OLCR permissions?

## GitHub連携とGit操作 / GitHub and Git

Guide: `github`; parent: `overview`.

Implementation: `frontend/src/GitHubSettings.tsx`.

- GitHubを接続するには？
- OLCRでコミットする方法は？
- ステージとプッシュの使い方は？
- How do I connect GitHub?
- How do I stage and commit changes in OLCR?

## YouTubeの検索と再生 / YouTube search and playback

Guide: `youtube`; parent: `overview`.

Implementation: `frontend/src/AssistantActions.tsx`.

- YouTubeで動画を見る方法は？
- OLCRで音楽を探すには？
- 動画のサムネイル候補はどう選ぶ？
- How do I search YouTube in OLCR?
- How do I select video thumbnails?

## Macのカレンダー接続 / Mac Calendar connection

Guide: `calendar`; parent: `overview`.

Implementation: `backend/olcr_api/apple_calendar.py`.

- OLCRのカレンダーの使い方は？
- Macの予定を確認するには？
- Googleカレンダー用のAPI発行は必要ですか？
- How do I connect Mac Calendar?
- How do I list calendar events?

## タイマーとアラーム / Timers and alarms

Guide: `timers`; parent: `overview`.

Implementation: `backend/olcr_api/assistant_actions.py`.

- タイマーを使うには？
- タイマーの一時停止方法は？
- タイマーを一覧で見る方法は？
- How do I pause a timer?
- Where can I list timers?
- 時刻を指定したアラームの設定方法は？
- タイマーが切れたら通知される？
- How do I set an alarm at a specific time?
- Will a timer send a notification?

## 添付ファイルとファイルリンク / Attachments and file links

Guide: `files`; parent: `overview`.

Implementation: `frontend/src/main.tsx`.

- OLCRで添付する方法は？
- CSVに変換するには？
- 添付画像を読み取る方法は？
- How do I attach a file?
- How do I convert an attachment to CSV?

## メッセージ編集と生成停止 / Editing messages and stopping generation

Guide: `editing`; parent: `overview`.

Implementation: `frontend/src/main.tsx`.

- OLCRのメッセージ編集はどこ？
- 回答停止の操作方法は？
- 生成停止はどうする？
- How do I edit a message?
- How do I stop generation?

## 画像理解モデルの設定 / Configuring vision models

Guide: `vision`; parent: `overview`.

Implementation: `frontend/src/PurposeModelsPanel.tsx`.

- 画像理解モデルを選ぶには？
- Visionの設定はどこ？
- 画像認識の対応モデルを選ぶ方法は？
- How do I choose a vision model?
- Where do I configure image understanding?

## ルーチンの記録 / Routine evidence

Guide: `routines`; parent: `overview`.

Implementation: `frontend/src/RoutinePanel.tsx`.

- ルーチン画面はどこ？
- ルーチン候補の見方は？
- ルーチンの承認は何をする？
- How do I open routines?
- Does routine approval execute it?

## ウェブ収集の管理 / Managing web scraping

Guide: `scraping`; parent: `overview`.

Implementation: `frontend/src/ScrapeStudio.tsx`.

- スクレイピング管理を開く方法は？
- ウェブ収集の件数を指定するには？
- スクレイピング結果の見方を教えて
- How do I manage scraping?
- How do I specify fields for web scraping?

## Macのアプリ操作 / Controlling Mac apps

Guide: `computeruse`; parent: `overview`.

Implementation: `frontend/src/AccessibilityPanel.tsx`.

- Macの画面操作の使い方は？
- コンピューター操作はどう始める？
- アクセシビリティの許可はどこ？
- How do I enable computer use?
- How do I choose the app for screen control?

## 設定画面の開き方 / Opening Settings

Guide: `settings`; parent: `overview`.

Implementation: `frontend/src/ChatSettingsCard.tsx`.

- OLCRの設定画面はどこ？
- 設定の開き方を教えて
- OLCRのオプションはどう変更する？
- Where are Settings in OLCR?
- How do I open the OLCR settings?

## 録音が開始できないとき / When recording will not start

Guide: `voice-permission`; parent: `voice`.

Implementation: `frontend/src/ChatComposer.tsx`.

- 音声入力のマイク権限はどこ？
- 録音が始まらないときは？
- Why can recording not start?
- Where is microphone permission?

## macOSとLFMの切り替え / Switching macOS and LFM transcription

Guide: `voice-provider`; parent: `voice`.

Implementation: `frontend/src/ChatSettingsCard.tsx`.

- 文字起こしをmacOSに切り替える方法は？
- 音声入力のLFMとmacOSはどう変える？
- How do I switch transcription to macOS?

## 音声モデルの初回ダウンロード / First-use audio download

Guide: `audio-download`; parent: `voice`.

Implementation: `backend/olcr_api/audio_transcription.py`.

- LFMの初回ダウンロードはどうする？
- 音声モデルを日本語にすると再ダウンロードが必要？
- How does the first LFM audio model download work?

## ハンズフリーモードの使い分け / Hands-free mode

Guide: `handsfree`; parent: `voice`.

Implementation: `frontend/src/HandsFreePanel.tsx`.

- ハンズフリーモードはどこで切り替える？
- ハンズフリーをやめる方法は？
- How do I turn hands-free mode off?

## Thinkingオンでもログがない理由 / Missing thinking logs

Guide: `thinking-missing`; parent: `thinking`.

Implementation: `backend/olcr_api/thinking.py`.

- Thinkingオンなのにログが出ないのはなぜ？
- 思考ログが表示されない理由を教えて
- Why are thinking logs missing when enabled?

## 稲妻が表示されない・速度が変わらない / Quick availability and speed

Guide: `quick-missing`; parent: `quick`.

Implementation: `backend/olcr_api/brain_providers.py`.

- ローカルモデルにQuickの稲妻がないのはなぜ？
- Quickにしても速度が変わらないのは？
- Why is the Quick lightning button missing?

## アカウントとモデルのフォールバックの違い / Account versus model fallback

Guide: `fallback-order`; parent: `models`.

Implementation: `backend/olcr_api/model_provider.py`.

- フォールバックはアカウントとモデルどちらが先？
- 代替モデルの順序はどこで変える？
- What is the account and model fallback order?

## アカウント追加と3枠の上限 / Account slots and limits

Guide: `account-slots`; parent: `accounts`.

Implementation: `frontend/src/ProviderAccounts.tsx`.

- アカウントを4つ追加できますか？
- アカウント追加の上限はいくつ？
- How many account slots can I add?

## 保存済みキーと再接続 / Saved credentials and reconnection

Guide: `keychain`; parent: `accounts`.

Implementation: `frontend/src/brainModels.ts`.

- APIキーは毎回再入力する必要がありますか？
- 保存済みキーを変更する方法は？
- Can I leave the saved API key blank when editing?

## ローカルモデルが選べないとき / Unavailable local models

Guide: `local-unavailable`; parent: `models`.

Implementation: `backend/olcr_api/brain_models.py`.

- ローカルモデルが選べないときはどうする？
- Ollamaのモデルが表示されない理由は？
- What should I check when a local model is unavailable?

## 長い会話・YaRN・自動圧縮 / Long context, YaRN and compression

Guide: `context-length`; parent: `models`.

Implementation: `frontend/src/ContextExtensionSettings.tsx`.

- YaRNと自動圧縮はどこで設定する？
- 長い会話のコンテキストを増やす方法は？
- How do YaRN and context compression differ?

## 参考資料が回答に入らないとき / Retrieval relevance controls

Guide: `rag-controls`; parent: `models`.

Implementation: `frontend/src/RetrievalSettings.tsx`.

- RAGの関連度を変えるには？
- 参考資料が回答に入らないときはどうする？
- Where do I change RAG relevance controls?

## ウェブ検索の設定 / Web search configuration

Guide: `web-search`; parent: `settings`.

Implementation: `frontend/src/WebSettings.tsx`.

- ウェブ検索のmanualとautoの違いは？
- Braveのキーはどこで設定する？
- How do I configure Web search and its mode?

## MCP接続確認の意味 / Checking MCP connections

Guide: `mcp-check`; parent: `settings`.

Implementation: `frontend/src/MCPConnectionsSettings.tsx`.

- MCPの接続確認はツールも実行する？
- MCP接続に失敗したときはどこを見る？
- Does an MCP connection check execute tools?

## エージェントの知識とモデル / Agent knowledge and model roles

Guide: `agent-knowledge`; parent: `agents`.

Implementation: `backend/olcr_api/custom_agent_runtime.py`.

- エージェントの画像モデルと回答モデルは同じ？
- エージェントの知識は全部毎回読まれる？
- Are agent knowledge and vision models separate?

## 画像付きエージェントが失敗するとき / Image agent failures

Guide: `agent-image-error`; parent: `agents`.

Implementation: `backend/olcr_api/custom_agent_api.py`.

- 画像付きエージェントがエラーになる原因は？
- エージェントで画像を読めないときは？
- What should I check for an image agent error?

## コーディングカードを閉じた後の再開 / Resuming after closing the coding card

Guide: `coding-console`; parent: `coding`.

Implementation: `backend/olcr_api/app.py`.

- コーディングのカードを消したらどう再開する？
- オーケストレーターを閉じても続きは残る？
- How do I resume coding after closing the card?

## 実装中の追加・修正指示 / Steering coding work

Guide: `coding-followup`; parent: `coding`.

Implementation: `frontend/src/ChatComposer.tsx`.

- 実装途中の追加指示はどう送る？
- コーディングの修正指示と過去メッセージ編集は同じ？
- How do I send a coding follow-up instruction?

## カレンダー接続を拒否した場合 / Denied Calendar access

Guide: `calendar-denied`; parent: `calendar`.

Implementation: `frontend/src/AssistantSettings.tsx`.

- カレンダーの権限を拒否した後はどうする？
- カレンダーに接続できないときの許可はどこ？
- How do I recover denied Calendar permission?

## 予定の書き込みと確認 / Calendar write review

Guide: `calendar-write`; parent: `calendar`.

Implementation: `backend/olcr_api/apple_calendar.py`.

- 予定を削除する前に確認はある？
- カレンダーが読み取り専用のとき変更できますか？
- Can I write to a read-only calendar?

## YouTubeと検索APIキーの違い / YouTube versus Web search setup

Guide: `youtube-setup`; parent: `youtube`.

Implementation: `backend/olcr_api/assistant_integrations.py`.

- YouTubeにAPIキー設定は必要？
- 動画を見る前にYouTube接続が必要ですか？
- Does YouTube require an API key or setup?

## 再生できない・全画面から戻る / Playback and fullscreen recovery

Guide: `youtube-playback`; parent: `youtube`.

Implementation: `frontend/src/AssistantActions.tsx`.

- 動画が自動再生できないときは？
- YouTubeの全画面から戻る方法は？
- How do I return from fullscreen playback?

## 読み上げを止める・録音との違い / Stopping read aloud versus recording

Guide: `readout-stop`; parent: `readout`.

Implementation: `frontend/src/readAloud.ts`.

- 読み上げだけ止めるには？
- 読み上げをオフにすると音声入力も止まる？
- How do I stop read aloud without changing voice input?

## スキル一覧と/skill実行の違い / Skill browsing versus execution

Guide: `skills-usage`; parent: `skills`.

Implementation: `frontend/src/SkillsLibrary.tsx`.

- スキル一覧にあるだけで使われるの？
- /skillの使い方を教えて
- What is the difference between listing and using a skill?

## プロジェクトなしでできること / Working without a project

Guide: `projectless`; parent: `projects`.

Implementation: `backend/olcr_api/custom_agent_api.py`.

- プロジェクトなしでエージェントは使える？
- 質問するだけでもプロジェクトは必要？
- Do I need a project to use an agent?

## Git操作の確認と自律実行 / Git review and autonomy

Guide: `git-review`; parent: `github`.

Implementation: `frontend/src/GitHubSettings.tsx`.

- Gitの自律実行をオフにすると確認できる？
- GitHubにプッシュしないよう指定できますか？
- Can I review Git actions before pushing?

## 空白入りファイルパスとリンク / Paths with spaces and file links

Guide: `file-links`; parent: `files`.

Implementation: `frontend/src/main.tsx`.

- 空白入りのファイルリンクは開ける？
- ファイルリンクを押しても開けない場合は？
- How do file links handle spaces in paths?

## 通信エラーと処理失敗の切り分け / Network versus processing failures

Guide: `network-error`; parent: `settings`.

Implementation: `frontend/src/api.ts`.

- NETWORK_ERRORの原因はネットだけ？
- HTTP_502の意味と対処を教えて
- What does NETWORK_ERROR mean in OLCR?

## 操作ガイドが答える範囲 / Operation help availability

Guide: `help-offline`; parent: `overview`.

Implementation: `backend/olcr_api/operation_help.py`.

- 操作ガイドはモデルなしのオフラインでも答える？
- 内蔵ガイドが答える範囲は？
- Does operation help work offline without a model?
