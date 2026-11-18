# Coding Task プロバイダー別リリース Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 明示されたリリース依頼を、対象providerの現在の要件確認、ローカルbuild、限定された公開操作、remote state read-backまで安全に実行する。

**Architecture:** 公式調査計画の`ProviderEvidence`を事前条件として使い、provider固有`ReleaseAdapter`を型付き`ReleaseRequest`と制約付きコマンド/API transportの後ろに置く。永続operation state、重複・不確定操作の照合、明示的な停止理由を共通化し、各providerの公開とread-backを分離する。

**Tech Stack:** Python、SQLite、既存Coding Task orchestrator、provider CLI/typed API transport、pytest、React/TypeScript、Vitest。

**Spec:** [docs/superpowers/specs/2026-11-18-coding-research-and-release-design.md](../specs/2026-11-18-coding-research-and-release-design.md)  
**Dependency:** [docs/superpowers/plans/2026-11-18-coding-provider-research.md](2026-11-18-coding-provider-research.md) が定義する`ProviderEvidence`とsource persistence。Release単体テストでは調査serviceをfakeし、Live research planの適用と順序に依存させない。

## Global Constraints

- 実際の公開は、ユーザーが「公開」「リリース」など実配布を明示した場合だけ行う。「リリース向けbuild」は公開しない。
- 対象provider、アプリ/パッケージ、version、channelのどれかが曖昧なら外部公開を実行しない。明示依頼は確定した対象への公開許可とする。
- Web資料は公開先の現行要件、操作権限、規約・ポリシー確認に使う。資料の取得不能、External Access無効、未完了の利用規約同意がある場合、依存する公開操作を止める。
- 資格情報は既存provider login/API credentialから使い、チャットへ要求・表示・永続化しない。任意のmodel-supplied shell stringを実行しない。
- エージェントは規約に同意せず、価格・課金planを決めず、審査用の本人事実表明をしない。必要時は該当画面・項目を示して止まる。
- 公開結果をproviderから読み戻して確認する。審査待ちを`PUBLISHED`と呼ばず、結果が不確定なtimeoutでは再公開前にremote stateを検査する。
- provider adapterを持たない先では公式手順の調査・案内までとし、自動公開に対応したと報告しない。

## Review Focus

- 「リリース向けにbuild」と明示的な「リリース」を混同しないこと。Test: Task 1で前者はlocal workflow、後者だけがrelease authorizationを持つことを固定する。
- target/version/channelが複数候補または不明なときに公開しないこと。Test: Task 1とTask 8でprovider operation runnerが呼ばれないことを固定する。
- 利用規約同意、課金選択、審査者向け事実表明を自動確定しないこと。Test: Task 1と各該当adapter testでpreflightが`BLOCKED`を返しpublishしないことを固定する。
- 成功responseの欠落・timeout後に同じリリースを二重作成しないこと。Test: Task 2・Task 3で`UNCERTAIN`後のinspect-before-retryを固定する。
- submit済みと審査待ちを公開済みに見せないこと。Test: Task 8でprovider stateごとの日本語/英語表示と最終報告を固定する。

---

### Task 1: 明示的リリース意図とpreflight要件

**Files:**
- Modify: `backend/olcr_api/coding_tasks.py`
- Modify: `backend/olcr_api/app.py`
- Test: `backend/tests/test_coding_release_intent.py`

**Interfaces:**
- Produces: `ReleaseIntent(requested: bool, provider_id: str | None, app_or_package: str | None, version: str | None, channel: str | None, blockers: list[str])`。
- Produces: `detect_release_intent(goal: str, project_facts: dict[str, object]) -> ReleaseIntent`。語句判定に加え、既存metadataは候補に使うが、候補が複数なら推測で一つに決めない。
- Consumes official source research plan: current policy and source availability; release readiness requires provider-specific current official references.

- [ ] **Step 1: release intent境界のテストを書く**
  `test_build_only_does_not_authorize_publication`、`test_explicit_release_request_authorizes_only_resolved_target`、`test_unknown_or_conflicting_target_returns_blocker`を追加する。
- [ ] **Step 2: policy gateのテストを書く**
  `test_missing_current_official_requirements_blocks_release`、`test_external_access_off_or_missing_agreement_blocks_dependent_publish`、`test_price_terms_and_attestations_require_user_action`を追加する。
- [ ] **Step 3: ReleaseIntent extractionを実装する**
  `canonical_coding_requirements`と既存task creation/planning経路へ`release_intent`を保存する。ユーザーの明示依頼にない公開意図をmodel planから作らない。
- [ ] **Step 4: release readiness checklistを追加する**
  公開先別の必要資料、target、version、channel、credentials、local build/validation、required human actionsを`ReleasePreflight`へ整理する。利用規約の同意をチェック済みと推定しない。
- [ ] **Step 5: release intent testsを実行する**
  Run: `python -m pytest backend/tests/test_coding_release_intent.py -q`  
  Expected: PASS。authorization境界とblocker分類を検証する。

### Task 2: durable release operation stateと重複防止

**Files:**
- Modify: `backend/olcr_api/db.py`
- Create: `backend/olcr_api/release_workflow.py`
- Test: `backend/tests/test_coding_release_workflow.py`

**Interfaces:**
- Produces: `ReleaseRequest(task_id: str, provider_id: str, project_root: Path, app_or_package: str, version: str, channel: str, artifact_paths: tuple[Path,...], metadata: dict[str, object], authorization: ReleaseAuthorization)`。
- Produces: `ReleaseAuthorization(explicit_intent: bool, target_resolved: bool, external_access_enabled: bool, current_references: tuple[ProviderEvidence,...])`。Coordinatorはこの許可と現行のprovider根拠を検査し、全条件を満たす場合だけREADYにする。
- Produces: `ReleasePreflight(status: Literal["READY","BLOCKED"], checks: list[dict[str, object]], blockers: list[str])`。
- Produces: `ReleaseResult(status: Literal["PUBLISHED","SUBMITTED","UNCERTAIN","FAILED","BLOCKED"], provider_state: str, remote_url: str | None, remote_id: str | None, summary: str, retry_requires_inspection: bool)`。
- Consumes: `ReleaseRequest.authorization: ReleaseAuthorization`。DBへ保存するrequestにはcredentialsを含めず、evidenceはreference IDだけで関連付ける。
- Adds operation table keyed by `operation_id` and unique `task_id/provider_id/idempotency_key`. Store only nonsecret target/version/artifact hashes, timestamps, state, safe error codes, remote id/url and bounded provider result fields.

- [ ] **Step 1: operation lifecycle testsを書く**
  `test_operation_transitions_record_blocked_ready_publishing_and_final_states`、`test_operation_persists_without_credentials_or_response_secrets`を追加する。
- [ ] **Step 2: duplicate・timeout state testsを書く**
  `test_same_task_provider_version_reuses_operation_key`、`test_rebuilt_artifact_cannot_replace_existing_version`、`test_uncertain_operation_requires_inspect_before_publish_retry`、`test_inspect_confirms_remote_state_before_marking_published`を追加する。
- [ ] **Step 3: SQLite operation persistenceを実装する**
  additively add `coding_release_operations` and DB methods `create_release_operation`、`update_release_operation`、`release_operation`. Increment schema version and preserve existing task data.
- [ ] **Step 4: provider-independent coordinatorを実装する**
  `ReleaseCoordinator.preflight(request: ReleaseRequest) -> ReleasePreflight`、`ReleaseCoordinator.publish(request: ReleaseRequest) -> ReleaseResult`、`ReleaseCoordinator.inspect(operation_id: str) -> ReleaseResult`を定義する。publish前にexplicit intent、External Access、current official evidence、READY、auth preflightを再確認する。
- [ ] **Step 5: local artifactsとremote stateの相関を実装する**
  workspace配下に限定したartifact path、ファイル存在、サイズ上限、SHA-256を記録し、remote callの前後で同じoperationを更新する。remote resultが曖昧なら`UNCERTAIN`としてpublish再実行を止める。
- [ ] **Step 6: workflow testsを実行する**
  Run: `python -m pytest backend/tests/test_coding_release_workflow.py -q`  
  Expected: PASS。fake adapterを使い、network無しでoperation idempotencyとstop状態を検証する。

### Task 3: 制約付きadapter契約とGitHub Releasesの垂直実装

**Files:**
- Create: `backend/olcr_api/release_adapters/__init__.py`
- Create: `backend/olcr_api/release_adapters/base.py`
- Create: `backend/olcr_api/release_adapters/github.py`
- Test: `backend/tests/test_release_adapter_contract.py`
- Test: `backend/tests/test_github_release_adapter.py`

**Interfaces:**
- Produces: `ReleaseAdapter` protocol with `provider_id`、`preflight(request: ReleaseRequest) -> ReleasePreflight`、`publish(request: ReleaseRequest, operation_id: str) -> ReleaseResult`、`inspect(request: ReleaseRequest, operation_id: str) -> ReleaseResult`。
- Produces: `ReleaseCommandRunner.run(spec: CommandSpec) -> CommandResult`。`CommandSpec`はadapter固定executable、argv tuple、workspace cwd、timeout、allowlisted environment profileを持つ。`shell=False`、秘密を含むstdout/stderr redaction、stdin不使用。
- Produces: `ProviderApiTransport.request(spec: ProviderRequest) -> ProviderResponse` for typed HTTPS API adapters. `ProviderRequest`はprovider host profile、method、path、bounded bodyを持つ。認証は既存credential providerから注入し、任意URL/headerを受け付けない。
- Consumes: existing GitHub CLI integration in `github_review.py` and path/network helpers in `git_workspace.py`; do not alter reviewed Git/PR workflows.

- [ ] **Step 1: adapter contractとtransport境界のテストを書く**
  `test_adapter_protocol_requires_preflight_publish_and_inspect`、`test_runner_never_uses_shell_and_filters_environment`、`test_api_transport_restricts_method_and_official_host`、`test_runner_redacts_credentials_from_errors`を追加する。
- [ ] **Step 2: GitHub preflightのテストを書く**
  `test_github_preflight_requires_single_github_remote_valid_tag_and_workspace_artifacts`、`test_existing_release_is_inspected_before_create`、`test_missing_gh_auth_blocks_without_reading_token`を追加する。
- [ ] **Step 3: shared adapter protocol、safe runner、typed API transportを実装する**
  任意コマンド文字列やURLではなくCommandSpec/ProviderRequestを受け取る。env allowlistとprovider host profileを固定し、実行バイナリを検証し、stdout/stderrとprovider responseを上限化・redactする。
- [ ] **Step 4: GitHub Releasesのpreflight/publish/inspectを実装する**
  既存のremote解決・External Access gateを再利用し、GitHub remote、release tag、version、build artifactを照合する。release作成前後にremote stateを確認し、既存releaseがあれば同じartifact hash・tagを照合して二重作成しない。
- [ ] **Step 5: GitHub adapter testsを実行する**
  Run: `python -m pytest backend/tests/test_release_adapter_contract.py backend/tests/test_github_release_adapter.py -q`  
  Expected: PASS。実GitHub/gh loginなしでfake runnerを使う。

### Task 4: macOS notarization stageとApple App Store/TestFlight adapter

**Files:**
- Create: `backend/olcr_api/release_adapters/apple_macos.py`
- Create: `backend/olcr_api/release_adapters/app_store_connect.py`
- Test: `backend/tests/test_apple_release_adapters.py`

**Interfaces:**
- Produces: `MacOSNotarizationStage.run(request: ReleaseRequest) -> BuildStageResult` and `AppStoreConnectAdapter` implementing `ReleaseAdapter`. Notarization stageは署名/notarization結果を返す。公開先へのupload/read-backがない限り、stage単体で`PUBLISHED`を返さない。
- The adapter uses only the credentials and provider tool already configured by the user; secret values are never returned in `ReleasePreflight` or stored in DB.

- [ ] **Step 1: notarization stage testsを書く**
  `test_macos_release_requires_signed_build_and_current_notarization_requirements`、`test_notarization_timeout_inspects_submission_before_retry`、`test_notarization_stage_does_not_claim_release_without_upload_readback`を追加する。
- [ ] **Step 2: App Store/TestFlight gate testsを書く**
  `test_app_store_connect_stops_for_unaccepted_agreement_or_required_attestation`、`test_app_review_waiting_is_submitted_not_published`、`test_app_store_auth_preflight_does_not_expose_key_material`を追加する。
- [ ] **Step 3: macOS notarization stageを実装する**
  current Apple official evidenceに基づき、build/sign/notarize/staple/checkをtyped stagesとして順番に実行する。build artifact作成と外部notarization submitを分ける。Apple ID/Keychain credentialは既存設定を参照する。配布完了はGitHub/hosting/App Store adapterのremote read-backで判定する。
- [ ] **Step 4: App Store Connect adapterを実装する**
  official API/toolと`ProviderApiTransport`でApp Store Connectへupload/TestFlight/App Review submitし、各stageのremote stateを読み戻す。agreement acceptance、metadataの本人確認が必要な場合はsubmit前に停止する。
- [ ] **Step 5: Apple adapter testsを実行する**
  Run: `python -m pytest backend/tests/test_apple_release_adapters.py -q`  
  Expected: PASS。transport/runnerをfakeし、署名アカウントやApp Store Connectへ接続しない。

### Task 5: Google Play adapter

**Files:**
- Create: `backend/olcr_api/release_adapters/google_play.py`
- Test: `backend/tests/test_google_play_release_adapter.py`

**Interfaces:**
- Produces: `GooglePlayReleaseAdapter` implementing `ReleaseAdapter`. Accepts typed track, package id, version code, artifact hash and staged rollout percentage; default rollout must be explicit in the release request rather than guessed. Implement requests through Task 3's `ProviderApiTransport`.

- [ ] **Step 1: Play release stop conditions testsを書く**
  `test_google_play_requires_package_track_and_version`、`test_google_play_stops_for_agreement_or_required_policy_attestation`、`test_staged_rollout_percentage_is_never_invented`を追加する。
- [ ] **Step 2: upload/review state testsを書く**
  `test_uploaded_or_in_review_is_submitted_not_published`、`test_google_play_timeout_reads_edit_and_release_state_before_retry`、`test_auth_probe_never_serializes_service_account_json`を追加する。
- [ ] **Step 3: typed Google Play adapterを実装する**
  Official Play publishing interface/clientを使い、edit/upload/track rolloutの各操作を固有operation stateで記録する。審査待ち・段階公開は読み戻したtrack statusどおりに報告する。
- [ ] **Step 4: adapter testsを実行する**
  Run: `python -m pytest backend/tests/test_google_play_release_adapter.py -q`  
  Expected: PASS。provider client fakeを使用しPlay Consoleへ接続しない。

### Task 6: npm/PyPI package registry adapters

**Files:**
- Create: `backend/olcr_api/release_adapters/npm.py`
- Create: `backend/olcr_api/release_adapters/pypi.py`
- Test: `backend/tests/test_package_release_adapters.py`

**Interfaces:**
- Produces: `NpmPublishAdapter` and `PyPIPublishAdapter` implementing `ReleaseAdapter`. Use Task 3's `ReleaseCommandRunner` and provider-specific credential profiles.
- Publish inputs include exact package name, version, registry, visibility, distribution tag or PyPI target; do not infer public visibility, prerelease channel, or registry from credentials.

- [ ] **Step 1: package metadata/authorization testsを書く**
  `test_npm_requires_name_version_registry_and_visibility`、`test_pypi_requires_valid_distribution_and_configured_auth`、`test_preexisting_version_checks_artifact_digest_before_publish`を追加する。
- [ ] **Step 2: registry timeout and error testsを書く**
  `test_npm_timeout_inspects_registry_before_retry`、`test_pypi_publish_secret_is_redacted_from_result`、`test_existing_different_artifact_version_is_blocked`を追加する。
- [ ] **Step 3: npm adapterを実装する**
  official npm evidenceに沿ってpackage validation、auth probe、publish、registry read-backを型付きoperationで行う。scope/access/distribution-tagはReleaseRequestから受け取る。
- [ ] **Step 4: PyPI adapterを実装する**
  official PyPI evidenceに沿ってbuild metadata validation、auth/Trusted Publishing preflight、upload、index read-backを行う。Trusted PublishingのOIDC条件を満たさなければ既存設定済み認証のみを使い、secretを要求・保存しない。
- [ ] **Step 5: package adapter testsを実行する**
  Run: `python -m pytest backend/tests/test_package_release_adapters.py -q`  
  Expected: PASS。provider CLI/APIをfakeし、live registryへuploadしない。

### Task 7: Vercel/Cloudflare Pages deployment adapters

**Files:**
- Create: `backend/olcr_api/release_adapters/vercel.py`
- Create: `backend/olcr_api/release_adapters/cloudflare_pages.py`
- Test: `backend/tests/test_web_release_adapters.py`

**Interfaces:**
- Produces: `VercelReleaseAdapter` and `CloudflarePagesReleaseAdapter` implementing `ReleaseAdapter`. Use Task 3's `ReleaseCommandRunner` and provider-specific credential profiles.
- Inputs include exact project/site, environment, domain target and deployment channel. Preview deploymentとproduction alias promotionを別操作として表す。

- [ ] **Step 1: target/environment gates testsを書く**
  `test_vercel_requires_resolved_project_and_environment`、`test_cloudflare_requires_account_project_and_environment`、`test_preview_does_not_promote_to_production`を追加する。
- [ ] **Step 2: remote state and retry testsを書く**
  `test_deploy_timeout_inspects_existing_deployment_before_retry`、`test_deployment_ready_state_requires_provider_readback`を追加する。
- [ ] **Step 3: Vercel adapterを実装する**
  official Vercel evidenceを使いauth/status, deploy, production promotion, read-backを分離する。現行projectのlink metadata以外からaccount/projectを推測しない。
- [ ] **Step 4: Cloudflare Pages adapterを実装する**
  official Cloudflare evidenceを使いproject/environmentとdirect/CI upload modeを明示してdeployし、deployment status/URLを読み戻す。
- [ ] **Step 5: web deployment adapter testsを実行する**
  Run: `python -m pytest backend/tests/test_web_release_adapters.py -q`  
  Expected: PASS。fake runner/API clientを使いproductionへdeployしない。

### Task 8: adapter selection、Coding Task lifecycle、source/report UI

**Files:**
- Modify: `backend/olcr_api/app.py`
- Modify: `backend/olcr_api/db.py`
- Modify: `frontend/src/CodingTaskProgress.tsx`
- Modify: `frontend/src/CodingTaskProgress.test.tsx`
- Modify: `frontend/src/CodingTaskReview.tsx`
- Modify: `frontend/src/CodingTaskReview.test.tsx`
- Modify: `frontend/src/coding-review.css`
- Test: `backend/tests/test_coding_release_integration.py`

**Interfaces:**
- Consumes Tasks 1–7 interfaces and provider registry. Add a registry map from publish-capable provider ids to adapters; `apple_macos` maps to a notarization build stage and still requires a publication adapter. Unregistered provider is research-only.
- Final report includes release operation status, target, version, remote URL/id, confirmation time and outstanding review/manual actions. It must be deterministic from persisted operation state, not generated text alone.

- [ ] **Step 1: full lifecycle fake testsを書く**
  `test_release_task_researches_preflights_builds_publishes_and_inspects`、`test_release_build_failure_never_invokes_adapter_publish`、`test_unregistered_target_is_researched_but_not_published`を追加する。
- [ ] **Step 2: interruption and manual handoff testsを書く**
  `test_restart_resumes_by_inspecting_existing_operation`、`test_required_human_agreement_pauses_before_publish`、`test_review_pending_is_reported_as_submitted`を追加する。
- [ ] **Step 3: orchestrator integrationを実装する**
  Planning creates a release phase only for explicit intent. Release phase depends on successful source collection, local build and project verification; only then coordinator preflights and invokes registered adapter. Keep file scope and Git review boundaries unchanged.
- [ ] **Step 4: stop/restart/replan handlingを接続する**
  Save operation transitions and blockers with existing task events/resume state. On restart or timeout call `inspect` before any repeat publish call. Never infer success from a CLI exit status without provider read-back.
- [ ] **Step 5: provider/result stateをreport and UIへ出す**
  Coding Task progress/review surface displays `BLOCKED`, `PUBLISHING`, `UNCERTAIN`, `SUBMITTED`, and `PUBLISHED` with provider-native URL. Distinguish Apple/Google review pending and deployment rollout from live availability.
- [ ] **Step 6: integration and UI testsを実行する**
  Run: `python -m pytest backend/tests/test_coding_release_integration.py -q`  
  Run: `npm --prefix frontend test -- CodingTaskProgress.test.tsx CodingTaskReview.test.tsx`  
  Expected: PASS。fake providers only。explicit intent, failure, timeout, manual gate, read-back and final reporting are covered.

### Task 9: 公開サポート表・runbook・運用確認

**Files:**
- Modify: `docs/LOCAL_CODING_WORKFLOW.md`
- Modify: `docs/github-git-workspace.md`
- Create: `docs/provider-release-support.md`
- Test: `backend/tests/test_release_support_matrix.py`

**Interfaces:**
- Publishes one machine-readable support matrix shared by help/runbook and adapter registration; list each provider as `RESEARCH_ONLY`, `BUILD_READY`, `PUBLISH_READY` or `REQUIRES_HUMAN_STEP` with exact supported channels.

- [ ] **Step 1: support matrix consistency testを書く**
  `test_every_registered_release_adapter_has_provider_docs_and_capability_status`、`test_unknown_provider_is_never_marked_publish_ready`を追加する。
- [ ] **Step 2: provider runbookを追加する**
  credential preparation, supported target/channel, local build prerequisites, legal/manual stop conditions, timeout recovery, source references, status meaningsを日本語/英語で説明する。secret valueをログへ出さない手順を記す。
- [ ] **Step 3: help/docsとadapter matrixを一致させる**
  adapter/stage registrationをsupport matrixのsource of truthとし、GitHub PR workflowの既存手順とrelease workflowを区別する。Notarization-onlyを公開済みと分類せず、個人アカウント契約内容を公開資料から断定しない。
- [ ] **Step 4: support matrix testsを実行する**
  Run: `python -m pytest backend/tests/test_release_support_matrix.py -q`  
  Expected: PASS。全adapterに資料とstatusがあり、未知providerは自動公開不可である。

## End-to-end exit criteria

- Explicitly requested release runs official research, preflight, build/verification, provider-specific publish and remote read-back; a local build request never invokes publish.
- The first adapter integration supports the complete operation contract: idempotent release creation, timeout inspection, secret redaction and result reporting.
- Each provider in the spec has a real adapter or an explicit `REQUIRES_HUMAN_STEP`/`RESEARCH_ONLY` status; no unsupported provider is described as automated.
- A publish failure, source failure, unavailable auth, target ambiguity, agreement gate or provider review state produces a precise persistent task result and recovery instruction.

