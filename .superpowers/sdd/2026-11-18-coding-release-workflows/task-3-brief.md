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
- Consumes: `ReleaseRequest.metadata["build_commit"]`, a full SHA captured by the validated local build. It must match the current workspace HEAD and the GitHub tag target. Resolve lightweight and annotated tags, and repeat the tag check during missing-release recovery and final read-back.

- [x] **Step 1: adapter contractとtransport境界のテストを書く**
  `test_adapter_protocol_requires_preflight_publish_and_inspect`、`test_runner_never_uses_shell_and_filters_environment`、`test_api_transport_restricts_method_and_official_host`、`test_runner_redacts_credentials_from_errors`を追加する。
- [x] **Step 2: GitHub preflightのテストを書く**
  `test_github_preflight_requires_single_github_remote_valid_tag_and_workspace_artifacts`、`test_existing_release_is_inspected_before_create`、`test_missing_gh_auth_blocks_without_reading_token`を追加する。
- [x] **Step 3: shared adapter protocol、safe runner、typed API transportを実装する**
  任意コマンド文字列やURLではなくCommandSpec/ProviderRequestを受け取る。env allowlistとprovider host profileを固定し、実行バイナリを検証し、stdout/stderrとprovider responseを上限化・redactする。
- [x] **Step 4: GitHub Releasesのpreflight/publish/inspectを実装する**
  既存のremote解決・External Access gateを再利用し、GitHub remote、release tag、version、build artifactを照合する。release作成前後にremote stateを確認し、既存releaseがあれば同じartifact hash・tagを照合して二重作成しない。
- [x] **Step 5: GitHub adapter testsを実行する**
  Run: `python -m pytest backend/tests/test_release_adapter_contract.py backend/tests/test_github_release_adapter.py -q`
  Expected: PASS。実GitHub/gh loginなしでfake runnerを使う。
