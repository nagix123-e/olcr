# OLCR カスタムエージェント

実装日: 2026-10-07

## 使い方

1. 左サイドバーの「新しいプロジェクト」の上にある **More** を開く。
2. **カスタムエージェント** を選ぶ。
3. 「エージェントを作成」から、名前・説明・アイコン・指示・モデル・用途・知識・ツール・スキル・委任先を設定し、保存する。
4. 一覧の「使う」で、新しいエージェント会話を開始する。現在選んでいるプロジェクトに属し、プロジェクトなしでも利用できる。
5. チャット上部に利用中のエージェント名が表示される。「通常の会話を始める」で通常チャットへ戻る。

アイコンは文字／絵文字。知識にはテキストファイルを取り込むか、メモを直接入力できる。作成済みの定義は編集・削除できる。削除は一覧からの除外であり、既存会話を削除しない。

## 責務

```text
OLCR
├─ 既存モデルロール、Bonsai / Ornith / Vision、APIの実行・権限制御
├─ 既存Task、モデル／ツールのテレメトリ、API結果の検証
├─ カスタムエージェント定義と会話への紐付け（SQLite）
├─ 作成・編集・一覧・利用UI
└─ private stdio adapter
   └─ @librechat/agents 4.0.4
      ├─ agent graph / tool loop
      ├─ context pruning / summarization
      ├─ session stream events
      ├─ LangGraph SQLite checkpoints + JSONL sessions
      └─ selected subagents（入れ子なし）
```

LibreChat本体のサーバー、MongoDB、認証、プロバイダー管理、UIは導入しない。実行SDKを採用し、OLCRのPython側からモデル・ツールの呼び出しを渡す。モデル／ツールの代理呼び出し用HTTPサーバーは公開しない。

### 定義・会話

- `custom_agents`: 定義JSON、revision、archivedを保持。
- `custom_agent_conversations`: 会話IDと、利用開始時の定義・委任先のスナップショットを保持。
- 編集は次に作る会話へ反映する。進行中の会話は開始時のrevisionで一貫する。
- 既存プロフィールDBにも、従来のマイグレーション完了後に追加テーブルを作成する。
- 一つの会話への同時実行はOSの`flock`で制限する。
- 状態はそのプロフィールの`agent-sessions/`に保存する。既存の会話削除ではOLCRのメッセージ／紐付けを消すが、SDKの状態ファイルの自動削除は初期版に含めない。

### OLCRの境界

- 優先モデル未指定なら既存Brain設定を使用。用途は既存`normal_chat`、`planner_replanner`、`short_coding_fix_debug`、`long_implementation`ロールへ接続する。
- 優先モデルは既存モデルカタログにある利用可能なモデルだけを選べる。モデルがなくなった場合、勝手に別モデルへ切り替えない。
- 初期ツールは、既存の安全な組込ツールと、実装済み外部APIツール。ツール選択は実行許可リストとしてホスト側でも毎回確認する。
- 外部APIは既存External Access設定、引数検証、正規化結果の検証を通す。ツールの失敗も既存Taskの実行記録に残す。
- シェル、任意ファイル変更、ネイティブComputer Use、MCPの任意ツールはこの新しい実行経路へ公開しない。通常のOLCR会話の既存機能は継続する。
- 用途を「実装」にしても、エージェントにファイル書き込み権限を与えるものではない。
- スキルは既存SkillManagerで有効・利用可能なものだけ読み込む。選択が無効化／破損した場合、初期設定のまま勝手に実行しない。
- 子エージェントは明示的に選んだ定義のツール・モデル・スキルを使用。親の許可リストを暗黙に継承しない。委任先は最大5件、子からの再委任は無効。
- 画像は既存`Runtime.execute_image`で処理し、その観察結果をSDKへ渡す。概念図は既存の構造文脈変換を使う。
- 子プロセスへプロバイダー資格情報やトレースキーを環境変数で渡さない。モデル用資格情報は既存OLCR側に留める。

### 制限

名前100文字、説明1,000文字、指示32,000文字、知識20件（各100,000文字）、定義全体300KB、ツール30件、スキル20件、委任先5件。知識ファイルの取り込みは1件100KBまで。

大きい知識資料は既存ContextManagerで質問に合わせた抜粋を選び、抜粋であることをモデルへ明示する。保存資料を削らない。スキル本文の合計64KBを超えた場合は実行を拒否する。

実行はLangGraphのrecursionLimit 30、子のmaxTurns 8、ホストの実行時間上限600秒で制限。ホストで実行中のモデル呼び出しはプロバイダー側のタイムアウトを使用する。

## 配布

`@librechat/agents`はNode 24以上を要求するため、既存Node 20のMCPランタイムから分離する。

```sh
npm ci --prefix agent-runtime
python3 scripts/prepare_agent_runtime.py
npm --prefix frontend run build
```

準備スクリプトは既存Nodeランタイム用ライセンス収集処理を再利用する。Node 24.18.0、LibreChat Agents 4.0.4、パッケージロック・runnerのハッシュ、ライセンス目録をmanifestに記録する。

配布ビルドは`agent-runtime/`を同梱する。別の準備済みディレクトリを使用する場合は`OLCR_AGENT_RUNTIME_SOURCE`。実行時の明示指定は`OLCR_AGENT_RUNTIME`。

今回インストールされたNode・依存全体は約635MB（展開後）。複数プロバイダー用の依存をSDKが含むため、Python側の接続コードを薄くしても配布サイズは増える。パッケージの削除・独自ループへの置き換えは行っていない。

## 初期実装での確認範囲

- フロントエンドのTypeScript型チェック、本番Viteビルド成功。
- 新規Pythonモジュールと接続ファイルのコンパイル成功。
- Node runnerの構文チェック成功。
- LibreChatセッション・SQLiteチェックポイントモジュールのインポート成功。
- Nodeランタイム・依存の準備とmanifest／ライセンス目録生成を完了。
- API実行、実モデルによる会話、ツール呼び出し、チェックポイント再開、子エージェント、実ブラウザー／ネイティブGUIの受け入れ検証は未実施。今回の依頼ではテスト実行が指定されていないため、新規テストの追加・実行はしていない。
- インストール済みOLCRアプリの交換はしていない。新しいソース／配布ビルドを使用する必要がある。

SSEではSDKのツール・ステップ進行イベントと最終回答を返す。現在のモデル接続は1回のモデル応答を受け取ってからSDKへ渡すため、生成途中のトークン単位表示ではない。停止は次のモデル／ツール呼び出しを防ぐ境界で反映され、実行中のモデル呼び出しは即時中断しない。

## 主なファイル

- `frontend/src/CustomAgents.tsx` / `custom-agents.css`: 管理画面。
- `frontend/src/main.tsx` / `ChatComposer.tsx`: More、会話選択、送信・ショートカットの分離。
- `backend/olcr_api/custom_agents.py`: 定義、ストア、選択ツールの目録。
- `backend/olcr_api/custom_agent_runtime.py`: private stdio、モデル／ツール境界、テレメトリ。
- `backend/olcr_api/custom_agent_api.py`: 定義API、会話作成、共通チャット取り込み。
- `agent-runtime/runner.mjs`: SDKのセッションとLangGraph実行。
- `scripts/prepare_agent_runtime.py` / `scripts/build_release.py`: 準備・同梱。

参考: [LibreChat Agents SDK](https://github.com/LibreChat-AI/agents)、[LibreChat Agents機能](https://www.librechat.ai/docs/features/agents)。
