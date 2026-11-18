### Task 4: macOS notarization stageとApple App Store/TestFlight adapter

**Files:**
- Create: `backend/olcr_api/release_adapters/apple_macos.py`
- Create: `backend/olcr_api/release_adapters/app_store_connect.py`
- Test: `backend/tests/test_apple_release_adapters.py`

**Interfaces:**
- Produces: `MacOSNotarizationStage.run(request: ReleaseRequest) -> BuildStageResult` and `AppStoreConnectAdapter` implementing `ReleaseAdapter`. Notarization stageは署名/notarization結果を返す。公開先へのupload/read-backがない限り、stage単体で`PUBLISHED`を返さない。
- The adapter uses only the credentials and provider tool already configured by the user; secret values are never returned in `ReleasePreflight` or stored in DB.

- [x] **Step 1: notarization stage testsを書く**
  `test_macos_release_requires_signed_build_and_current_notarization_requirements`、`test_notarization_timeout_inspects_submission_before_retry`、`test_notarization_stage_does_not_claim_release_without_upload_readback`を追加する。
- [x] **Step 2: App Store/TestFlight gate testsを書く**
  `test_app_store_connect_stops_for_unaccepted_agreement_or_required_attestation`、`test_app_review_waiting_is_submitted_not_published`、`test_app_store_auth_preflight_does_not_expose_key_material`を追加する。
- [x] **Step 3: macOS notarization stageを実装する**
  current Apple official evidenceに基づき、build/sign/notarize/staple/checkをtyped stagesとして順番に実行する。build artifact作成と外部notarization submitを分ける。Apple ID/Keychain credentialは既存設定を参照する。配布完了はGitHub/hosting/App Store adapterのremote read-backで判定する。
- [x] **Step 4: App Store Connect adapterを実装する**
  official API/toolと`ProviderApiTransport`でApp Store Connectへupload/TestFlight/App Review submitし、各stageのremote stateを読み戻す。agreement acceptance、metadataの本人確認が必要な場合はsubmit前に停止する。
- [x] **Step 5: Apple adapter testsを実行する**
  Run: `python -m pytest backend/tests/test_apple_release_adapters.py -q`
  Expected: PASS。transport/runnerをfakeし、署名アカウントやApp Store Connectへ接続しない。
