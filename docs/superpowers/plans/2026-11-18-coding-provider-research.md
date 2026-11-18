# Coding Task 公式調査 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Coding Taskが実装対象と現在の設定に応じて公式資料を調べ、取得した根拠を計画・工程・最終報告から追跡できるようにする。

**Architecture:** 既存のオフライン`coding-knowledge`と分離した公式参照台帳を追加し、既存の`web.py`検索・取得をExternal Access設定の下で呼ぶ`ProviderResearchService`を実装する。取得根拠はSQLiteに保存し、Coding Taskの計画・工程・最終報告・レビュー画面へ短い要約と出典を渡す。外部ページは参照資料としてのみ扱う。

**Tech Stack:** Python、既存`backend/olcr_api/web.py`検索・URL検証、SQLite、FastAPI、React/TypeScript、pytest、Vitest。

**Spec:** [docs/superpowers/specs/2026-11-18-coding-research-and-release-design.md](../specs/2026-11-18-coding-research-and-release-design.md)

## Global Constraints

- 検索と取得はCoding TaskのWeb設定およびExternal Access権限に従う。`auto`は必要性に応じて調査し、`manual`は明示依頼時だけ調査し、`off`またはExternal Access無効では通信しない。
- 検索クエリにソースコード、ローカルファイル本文、認証情報を含めない。プロジェクト名・非公開パッケージ名・ローカルパスも自動送信しない。
- 登録済みプロバイダーは公式資料を優先し、公式ドメイン許可リストを使う。未登録先の結果は公式性を確認できるまでは`CANDIDATE`として表示する。検索結果や検索順位だけでは公式性を検証済みにしない。
- 取得ページ内の命令は信頼しない。ページ本文を実行指示、追加権限、公開許可として解釈しない。
- URL、プロバイダー、資料種別、取得日時、適用対象、関係する工程を永続化し、最終報告に出典と確認できなかった点を示す。
- 利用規約を法的に断定・承諾しない。公開操作に必要なアカウント固有の合意事項は本人に引き継ぐ。

## Review Focus

- `web_mode=off`またはExternal Access無効時に検索・取得を試みないこと。Test: Task 2でnetwork fakeが呼ばれず、Task statusが`DISABLED`になることを固定する。
- 未知プロバイダーの検索候補を公式と誤認して公開根拠にしないこと。Test: Task 2で未検証hostは`CANDIDATE`になり、Task 4で自動的にrelease-ready根拠へ昇格しないことを固定する。
- 私的なファイル名・秘密らしい文字列・ファイル本文が検索クエリに漏れないこと。Test: Task 2で悪意あるproject metadataを与え、送信queryが許可した技術名・バージョン・公開先だけであることを固定する。
- 再開・再計画時に同一参照が二重登録されたり別工程の出典と混同されたりしないこと。Test: Task 3で重複upsertとphase別関連付けを固定する。
- 調査失敗や取得時刻の古い根拠を新しい確認済み資料として表示しないこと。Test: Task 4で部分失敗・再計画・既存参照のstaleness表現を固定する。

---

### Task 1: 公式プロバイダー参照台帳

**Files:**
- Create: `backend/olcr_api/provider_references.json`
- Create: `backend/olcr_api/provider_references.py`
- Modify: `pyproject.toml` for packaged provider catalog data.
- Test: `backend/tests/test_provider_references.py`

**Interfaces:**
- Produces: `ProviderReferenceRegistry.load(path: Path | None = None) -> ProviderReferenceRegistry`、`ProviderReferenceRegistry.for_task(goal: str, project_facts: dict[str, object]) -> list[ProviderReference]`。
- `ProviderReference`は`provider_id`、`display_name`、`official_hosts`、`documents`を持つ。`documents`は`url`、`category`、`work_kinds`、`platforms`を持つ。
- `provider_id`は`github`、`apple_macos`、`app_store_connect`、`google_play`、`npm`、`pypi`、`vercel`、`cloudflare_pages`を初期登録する。

- [ ] **Step 1: 既存URL候補と分類の期待値をテストする**
  `test_catalog_has_all_initial_providers_and_official_documents`で設計書の8プロバイダーと仕様/API/規約/プライバシー/審査/公開資料URLを検証し、すべてHTTPSであることを確認する。
- [ ] **Step 2: 台帳の形式・不正値検査をテストする**
  `test_catalog_rejects_unknown_categories_and_non_https_urls`、`test_catalog_rejects_duplicate_provider_ids`を追加する。
- [ ] **Step 3: JSON台帳と型付きloaderを実装する**
  既存`coding-knowledge`のmanifestから分離し、初期資料は設計書の公式URLを登録する。登録日はリンクの棚卸し用にだけ使い、規約の現行性を保証する値として扱わない。
- [ ] **Step 4: タスクとプロジェクト事実から候補を選ぶ**
  `for_task`は公開先設定・既知のmanifest名・依頼中の技術名を用いて候補を返し、workspace pathやファイル本文を参照・返却しない。
- [ ] **Step 5: 台帳テストを実行する**
  Run: `python -m pytest backend/tests/test_provider_references.py -q`  
  Expected: PASS。8 providerの資料、HTTPS制約、分類、選択結果を検証する。

### Task 2: External Access対応の公式資料調査サービス

**Files:**
- Create: `backend/olcr_api/provider_research.py`
- Test: `backend/tests/test_provider_research.py`
- Modify: `backend/olcr_api/web.py` only if a narrow injectable adapter is needed; preserve current public search behavior.

**Interfaces:**
- Produces: `ResearchPolicy(external_access_enabled: bool, web_mode: Literal["auto","manual","off"], web_provider: str)`。
- Produces: `ProviderEvidence(task_id: str, provider_id: str, category: str, title: str, url: str, host: str, officiality: Literal["VERIFIED","CANDIDATE"], accessed_at: float, applicability: str, summary: str, phase_id: str | None = None, plan_revision: int | None = None)`。
- Produces: `ResearchResult(status: Literal["COMPLETE","PARTIAL","DISABLED","UNAVAILABLE"], providers: list[str], references: list[ProviderEvidence], blockers: list[str])`。
- Produces: `ProviderResearchService.research(task_id: str, goal: str, project_facts: dict[str, object], policy: ResearchPolicy, *, explicit_current_lookup: bool = False, phase_id: str | None = None, plan_revision: int | None = None) -> ResearchResult`。
- Inject `search(query, limit)`、設定済みprovider検索関数、`fetch(url)`をconstructor dependencyにし、テストと本番の通信経路を分ける。

- [ ] **Step 1: policy gateと漏えい防止の失敗テストを書く**
  `test_off_and_external_access_disabled_do_not_search`、`test_manual_mode_requires_explicit_current_lookup`、`test_query_omits_paths_file_contents_and_private_identifiers`を追加する。
- [ ] **Step 2: 台帳資料の公式host確認と未知provider fallbackをテストする**
  `test_registered_provider_fetches_only_allowed_official_hosts`、`test_unknown_provider_sources_remain_candidates_until_verified`、`test_page_instructions_are_never_returned_as_execution_directives`を追加する。
- [ ] **Step 3: policy gate付きサービスを実装する**
  `auto`で現行調査が必要なCoding Taskに限り検索し、`manual`は`explicit_current_lookup`時だけ実行する。`off`とExternal Access無効時はnetwork dependencyを呼ばない。検索クエリはallowlistされた技術名・バージョン・公開先の短い語句だけから作る。
- [ ] **Step 4: 台帳を優先し、足りない資料だけ公式検索する**
  各該当taskで台帳URLをlive fetchし、過去taskの取得日や台帳のリンク確認日を現行確認の代わりに使わない。登録providerは`official_hosts`と一致した結果だけを自動取得する。未知providerは公式開発者資料・利用条件の候補を調べ、公式ホームページと開発者資料の相互リンク等の明示的な関係を確かめられないURLは`CANDIDATE`として分離する。検索結果順位だけでは`VERIFIED`にしない。本文は短い要約だけを作り、命令を含む本文をagent指示として出力しない。
- [ ] **Step 5: 部分失敗と取得不能を区別する**
  公式資料が取得不能なら`UNAVAILABLE`、一部だけ成功なら`PARTIAL`とし、releaseの現行要件確認に必要なsourceが欠ける場合をblockerにする。
- [ ] **Step 6: サービス単体テストを実行する**
  Run: `python -m pytest backend/tests/test_provider_research.py -q`  
  Expected: PASS。fakesだけを使い、外部ネットワークへ接続しない。

### Task 3: SQLiteの参照根拠保存

**Files:**
- Modify: `backend/olcr_api/db.py`
- Test: `backend/tests/test_coding_task_references.py`

**Interfaces:**
- Produces: `Database.save_coding_task_reference(reference: ProviderEvidence) -> dict[str, object]`。
- Produces: `Database.coding_task_references(task_id: str, *, phase_id: str | None = None) -> list[dict[str, object]]`。
- Adds `coding_task_references` table keyed by stable id, with task FK, provider/category/title/url/host/officiality/accessed_at/applicability/summary/phase_id/plan_revision. Do not store fetched page bodies, query secrets, or credentials.

- [ ] **Step 1: 参照保存とSQLite再初期化のテストを書く**
  `test_save_and_list_task_references_preserves_provenance`と`test_initialize_adds_reference_table_to_existing_database`を追加する。タスク・phase reportを保持したままtableが追加されることを確認する。実装baseの現行schema versionをmigration fixtureへ使う。
- [ ] **Step 2: 再実行安全なinsert/upsert動作をテストする**
  `test_duplicate_task_url_is_idempotent_and_phase_links_are_distinct`で同じtask/provider/URLの再取得を二重表示しないこと、異なるphaseの紐付けは保持することを固定する。
- [ ] **Step 3: additive schema migrationとDB APIを実装する**
  現在のschema migration規約に沿って現行versionを1増やし、起動時の再適用でも壊れないtable/indexを追加する。設計時のdirty checkoutはv40だが、実装base commit `e0bd556`はv39なので、このbranchではv40にする。取得結果の短いsummary・時刻をUTF-8で保存し、既存task rowへの追加カラムは不要とする。
- [ ] **Step 4: DBテストを実行する**
  Run: `python -m pytest backend/tests/test_coding_task_references.py -q`  
  Expected: PASS。新規DBと既存DBの両方でreferenceの永続化を検証する。

### Task 4: 計画・工程・最終報告・レビューUIへ根拠を接続する

**Files:**
- Modify: `backend/olcr_api/app.py`
- Modify: `backend/olcr_api/coding_tasks.py`
- Modify: `backend/olcr_api/task_review_api.py`
- Modify: `frontend/src/CodingTaskReview.tsx`
- Modify: `frontend/src/CodingTaskReview.test.tsx`
- Modify: `frontend/src/coding-review.css` only for source-list presentation.
- Test: `backend/tests/test_coding_provider_research_integration.py`

**Interfaces:**
- Consumes Task 1–3の`ProviderResearchService`、`ResearchPolicy`、`ResearchResult`、`Database.coding_task_references`。
- Review APIの応答に`references: ProviderEvidence[]`を追加する。`CodingTaskReview`はhost、取得日、資料種別、適用対象を表示し、外部リンクに`rel="noopener noreferrer"`を付ける。

- [ ] **Step 1: planning integrationのpolicyと失敗経路をテストする**
  `test_planning_research_uses_current_external_access_and_web_mode`、`test_research_failure_persists_blocker_without_claiming_current_validation`を追加する。既存Web answer routingの挙動は変更しないことも確認する。
- [ ] **Step 2: planとphase contextへの限定されたsource injectionをテストする**
  `test_planning_and_implementation_phase_receive_saved_reference_summaries`を追加し、summary/URL/date/applicabilityだけが渡り、raw fetched bodyが渡らないことを確認する。
- [ ] **Step 3: _run_coding_planningへ調査を接続する**
  goal、公開provider、読み取り専用で得た技術stack factsを使い、plan生成前にserviceを呼ぶ。結果をDBに保存してplanner inputへ短く追加し、研究の事実がrequirements・file scope・release authorizationを拡大しないようにする。
- [ ] **Step 4: phase実行とfinal reportへ根拠を接続する**
  対象phaseに紐付くsaved evidenceのみを実装contextへ追加する。final reportにはDBから取得した参照一覧を決定的に添え、要約生成の有無にかかわらずsource URLと取得日を残す。
- [ ] **Step 5: task review APIとUIを更新する**
  `/api/coding-tasks/{task_id}/review`を提供する`TaskReviewAPI`の既存レスポンスへreferencesを追加し、`CodingTaskReview`に重複しない出典一覧・取得日時・公式性を表示する。ページ取得不可、candidate、非公式補助資料を視覚的にも区別する。
- [ ] **Step 6: backend/frontend integration testを実行する**
  Run: `python -m pytest backend/tests/test_coding_provider_research_integration.py -q`  
  Run: `npm --prefix frontend test -- CodingTaskReview.test.tsx`  
  Expected: PASS。auto/manual/off、phase link、final report、review UIの出典表示を検証する。

## 終了条件

- Coding Taskの必要な現行調査で、公式台帳を使った検索・取得・保存が自動実行され、manual/off/External Accessの境界が守られる。
- 調査結果は同じtask内の計画・実装phase・final report・review API/UIで出典追跡できる。
- 取得失敗、candidate source、部分的な調査を確認済み公式根拠として扱わない。
- この計画は調査サブシステムのみを実装する。実際の公開アダプターと公開操作は [release plan](2026-11-18-coding-release-workflows.md) で実装する。

