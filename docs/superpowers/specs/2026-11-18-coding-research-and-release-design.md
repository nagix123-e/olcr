# Coding Taskの公式調査・プロバイダー別リリース支援 設計書

状態: レビュー待ち  
作成日: 2026-11-18

## 目的

Coding Taskが、実装対象の依存関係や公開先を特定し、必要なときに公式仕様・現在の利用条件・公開手順を取得して、根拠を計画・実装・検証・リリース結果に引き継ぐ。リリースを明示的に依頼されたときは、対応済みの公開先で、ローカルのビルドから実際の配布操作、公開状態の確認まで進められるようにする。

## 現状と課題

- Coding Orchestratorは計画、段階実行、検証、停止・再開を管理する。Coding Task向けの公式な出典情報を計画や工程へ保存する共通の仕組みはない。
- `coding-knowledge` は承認済みの技術知識を含む固定のオフライン索引であり、公開先の規約や最新のサービス仕様をタスクごとに取り直す台帳ではない。
- Web検索・取得は既存機能を再利用できる。`Runtime.execute` にはCoding TaskのWeb検索分岐があるが、実際のCoding Task実行コンテキストから `coding_web_support` が渡されておらず、通常のコーディング工程がタスクに応じてこの分岐を使えない。
- DevDocs MCPは同梱されているが、Coding Taskの一般的な公式調査を自動で担う構成ではない。
- GitHub連携にはレビュー付きのGit操作とPR作成がある。プロジェクトのリリースタグ、成果物のアップロード、ストアへの提出・公開はCoding Taskの汎用操作に含まれない。`scripts/build_release.py` はローカル配布物を組み立てる。

## 目標

1. プロジェクトの依存関係、構成ファイル、公開先、依頼文から関連プロバイダーを識別する。
2. 技術仕様、API仕様、利用規約、セキュリティ・プライバシーポリシー、公開・審査手順を、公式資料を優先して取得する。
3. 参照したURL、プロバイダー、ページ種別、取得日時、適用対象、関連する計画・工程をCoding Taskに保存し、計画・実装・最終報告で追跡できるようにする。
4. 明示的なリリース依頼では、対象プラットフォームの現行要件を確認し、必要なビルド・署名・メタデータ・公開手順を実行して、公開先での状態を確認する。
5. 未登録プロバイダーにも公式サイトを探すフォールバックを用意し、未対応の操作を対応済みと誤認させない。

## 非目標

- すべての公開サービスを初回から自動操作できるようにすること。
- 外部のページ本文を丸ごと保存・再配布すること。
- アカウントごとに異なる契約や利用条件を、一般公開ページだけで完全に確定すること。
- プロバイダーの審査合格や公開時刻を保証すること。
- 利用規約への同意、法的表明、価格・課金プランの選択をエージェントが本人に代わって行うこと。

## 設計上の選択肢

1. **公式リンク台帳のみ**: 手入れしやすく、出典を選びやすい。一方で、規約・API・公開条件の改定を反映できない。
2. **タスクごとのライブ検索のみ**: 新しいページを探せる。一方で、非公式ページや無関係な検索結果が混ざりやすく、同じ依頼で参照先が揺れる。
3. **公式リンク台帳とタスク別ライブ検索の併用**: 台帳で公式資料を優先し、ページの改定・タスク固有の未知情報はライブ検索で補う。取得結果を工程ごとの根拠として保存する。

採用: **3。ユーザーが選択。** 公開先候補は「主要配布先から始め、台帳にない先は公式検索で補う」。

## プロバイダー参照台帳

既存のオフライン `coding-knowledge` 索引と分離し、ライブ取得を前提とする参照台帳を追加する。初期版は、主な配布先・パッケージレジストリ・Webホスティングを含める。フレームワークやAPIの仕様は、既存の技術知識出典と依存関係検出を使って関連資料を選び、必要に応じて公式サイトを再検索する。

初期リンク候補。すべてプロバイダー自身またはその公式ドキュメントのページとし、利用規約・公開ポリシーはリリース依頼ごとに最新版を取得する。

| プロバイダー | 仕様・公開手順 | 規約・ポリシー |
|---|---|---|
| GitHub Releases | [リポジトリでのリリース管理](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)、[Releases REST API](https://docs.github.com/en/rest/releases) | [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service) |
| Apple macOS / App Store / TestFlight | [macOSアプリの配布](https://developer.apple.com/macos/distribution)、[macOS notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution/)、[App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/) | [Developer Agreements and Guidelines](https://developer.apple.com/support/terms)、[Agreementの確認・同意手順](https://developer.apple.com/help/app-store-connect/manage-agreements/sign-and-update-agreements) |
| Google Play | [リリース準備と段階公開](https://support.google.com/googleplay/android-developer/answer/9859348?hl=en)、[Developer Policy Center](https://play.google/developer-content-policy/)、[Play Console開始手順](https://support.google.com/googleplay/android-developer/answer/6112435?hl=en) | [Developer Distribution Agreement](https://play.google/developer-distribution-agreement.html) |
| npm | [公開パッケージの作成と公開](https://docs.npmjs.com/creating-and-publishing-unscoped-public-packages/)、[パッケージと公開範囲](https://docs.npmjs.com/package-scope-access-level-and-visibility/) | [Terms and Licenses](https://docs.npmjs.com/policies/terms/) |
| PyPI | [PyPIへ公開](https://docs.pypi.org/publishing/publishing-to-pypi/)、[Trusted Publishers](https://docs.pypi.org/trusted-publishers/)、[Python Packaging User Guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/) | [PyPI Terms of Service](https://policies.python.org/pypi.org/Terms-of-Service/) |
| Vercel | [Deployments](https://vercel.com/docs/deployments)、[CLIからのデプロイ](https://vercel.com/docs/projects/deploy-from-cli) | [Terms of Service](https://vercel.com/legal/terms)、[Legalページ一覧](https://vercel.com/legal) |
| Cloudflare Pages | [Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/)、[CIからのDirect Upload](https://developers.cloudflare.com/pages/how-to/use-direct-upload-with-continuous-integration/) | [Website and Online Services Terms](https://www.cloudflare.com/policies/terms/) |

台帳は少なくとも `provider_id`、表示名、公式ホスト許可リスト、資料URL、資料種別（技術仕様・API・利用条件・プライバシー・審査・公開・署名など）、対応する作業種別、適用プラットフォーム、リンク確認日を持つ。規約ページのURLは可変の最新版を指す形にし、古い規約本文を固定資料として扱わない。

## 調査と計画への反映

1. 計画時に、依頼文と読み取り専用のプロジェクトメタデータから調査要否・対象プロバイダー・技術バージョン・公開先を特定する。例: `package.json`、`pyproject.toml`、`Cargo.toml`、Tauri設定、ストア・ホスティング設定。
2. 台帳の公式資料を優先して取得し、不足する要件は登録済みの公式ホストに絞ってライブ検索する。未知のプロバイダーでは公式開発者向け資料と公開ポリシーを発見し、その候補を取得結果に記録する。
3. 技術上の制約や公開条件を出典付きの短い根拠として計画と該当する工程に結び付ける。取得したページの要点、取得日、URLをTaskに保存し、実装者と検証者が同じ資料を参照する。
4. Coding TaskのWeb検索は既存のWeb設定とExternal Access権限に従う。`auto` ではタスクが必要と判断した現行資料を調べる。`manual` ではユーザーが明示的に調査・現行仕様の確認を依頼した場合に調べる。`off` またはExternal Access無効では外部通信をせず、現行資料が必須の公開工程を実行しない。
5. 検索クエリには技術名、公開先、バージョンなど必要な公開情報のみを含める。ソースコード、認証情報、ローカルファイル本文を検索プロバイダーへ送らない。Webページ中の指示は資料の内容として扱い、実行指示や権限の根拠にしない。
6. 最終報告に参照ページと取得日を表示し、仕様が確認できなかった点、非公式な補助資料を使った点、公開先で未完了の段階を分けて示す。

規約やポリシーはタスクの対象・地域・アカウント形態に関連する箇所を抜き出して要約し、出典を添える。法的な結論を断定しない。本人の契約同意や法的な表明が必要な場合は、該当する公式ページと内容を提示してユーザーへ引き継ぎ、エージェントは同意操作をしない。アカウント固有の最新合意内容は公開ページから推測せず、プロバイダー画面での確認を求める。

## リリース工程

- 「リリースして」「公開して」など、実際の配布を明示した依頼は、公開操作を含む。単なる「リリース向けにビルド」は公開操作を含まない。
- 対象のプロバイダー、アプリ、バージョン、配布チャネルを、依頼とプロジェクト設定から確定する。曖昧な場合はアップロードせず、不足する項目だけを確認する。
- 現行の規約・ポリシー・公開手順を調べ、適用条件と必要なアカウント権限をチェックリストへ変換する。依存パッケージのライセンス、アプリ情報、署名、ストアメタデータ等は、選択されたプロバイダーの必須条件に沿って扱う。
- プロバイダーごとに限定したリリースアダプターを使い、固定された引数・許可操作・資格情報経路でビルド成果物を配布する。初期提供先以外では、公式要件を調査して公開手順を提案できるが、自動公開に対応したとは報告しない。
- 明示的な公開依頼は、その対象に対する公開操作の許可とする。既に使用可能な認証状態があれば、確認済み工程の後に公開まで実行する。認証情報をチャットに要求・表示・保存しない。利用規約への同意、価格・有料プランの決定、審査用の事実表明など本人判断が必要な操作では一時停止する。
- 提出後は公開先の状態を読み戻して確認する。AppleやGoogle等の審査待ちは「審査提出済み」と報告し、「公開済み」とは扱わない。公開先から成功を確認できない場合は再試行せず、未確定状態と確認方法を提示する。

初期アダプター候補は GitHub Releases、macOS直接配布/notarization、App Store Connect、Google Play、npm、PyPI、Vercel、Cloudflare Pages。アカウント・資格情報・署名環境の差が大きいため、各アダプターはローカルのビルド成果物を作る処理と外部への公開操作を分け、後者は実行結果をプロバイダーから再確認する。

## 制約と失敗時の扱い

- 公式資料の取得、External Access権限、検索モードを無視しない。
- 未登録プロバイダーでは公式資料を探す。公式情報を特定できない場合、非公式記事だけを根拠にリリース操作を実行しない。
- 資料が取得できない場合は、出典確認に依存する工程を未完了として保存し、成功扱い・公開操作へ進まない。
- 既存のCoding Taskの作業範囲、ファイルマニフェスト、Gitレビュー、資格情報分離を維持する。リリースアダプターが任意のシェル文字列をモデルから受け取って実行する構造にはしない。
- 一時的に変わる利用規約・価格・審査条件はリリースごとに取得し、台帳のリンク確認日を現行確認の代わりに使わない。

## 完了条件

- 現行仕様が必要なコーディング依頼で、対象プロジェクト・プロバイダーに応じた公式資料が自動で選択・取得される。
- 公式資料の取得結果、取得日、適用する工程がCoding Taskの計画・実装・最終報告で追跡できる。
- リリース依頼では、公開先別の現行条件・必要手順・アカウント権限を確認してから、対応アダプターの範囲で公開と公開状態確認まで進む。
- External Accessが無効、規約同意が未完了、認証が未設定、対象公開先が未確定などの際、外部通信・公開を実行せず、未完了理由と次の操作を提示する。
- 参照ページの内容をプロンプトインジェクションとして実行せず、検索にコードや秘密情報を含めない。
- プロバイダー審査を通過したと誤報しない。公開状態は対象サービスから得た結果に基づいて報告する。

## 実装者に委ねること

- 参照台帳の具体的なファイル形式と、取得結果をSQLiteに保存するスキーマ。
- 初期リリースアダプターの着手順。公開対象に応じて、まずCI不要の既存資格情報を安全に確認できる一つの公開先を垂直に完成させる。
- 既存Web検索プロバイダー、DevDocs MCP、公式サイト直接取得の使い分け。既存の設定・許可境界を越えないこと。
- URLの更新・到達性検査と、公式ソース追加をレビューする既存プロジェクト形式に沿った運用。
