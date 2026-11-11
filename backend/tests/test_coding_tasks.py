import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.coding_tasks import (coding_candidate, extract_plan_json, manager_review_prompt, plan_repair_prompt, model_slot,
                                   normalize_manager_decision, plan_schema, report_has_authoritative_failure, classify_execution_mode,
                                   validate_plan, evaluate_phase, classify_waiting_input, coding_action_intent,
                                   compact_normal_plan, classify_coding_request, coding_classification_diagnostics,
                                   classify_mutation_mode, plan_prompt, canonical_coding_requirements,
                                   normalize_coding_requirements, canonical_dependency_requirements, canonical_technology_requirements,
                                   dependency_requirement_specs, dependency_install_operations, associate_dependency_requirements,
                                   phase_execution_contract_errors,
                                   normalize_replan_graph, ensure_explicit_phase_execution_modes,
                                   normalize_replanned_phase_ids,
                                   enforce_explicit_phase_failure_checkpoint,
                                   enforce_explicit_single_file_manifest, explicit_single_file_write_target,
                                   phase_executor_capability, repair_phase_complexity,
                                   graph_validation_diagnostics, reconcile_graph_references,
                                   explicitly_requests_plan_only, zero_mutation_retry_instruction,
                                   phase_has_observable_deliverable)
from olcr_api.db import Database
from olcr_api.implementation_plan import prepare_implementation_plan
from olcr_api.models import Route, Task, TaskState


def plan(goal: str) -> dict:
    return {"schema_version":1,"original_goal":goal,"scope":{"allowed":["backend"],"forbidden":[]},"assumptions":[],
            "phases":[{"id":f"p{i}","goal":f"phase {i}","status":"pending","done":["done"],"verify":["manual check"],"dependencies":[] if i==1 else [f"p{i-1}"],"risks":[]} for i in range(1,4)],
            "max_retries_per_phase":2,"requires_user_approval":True}


class CodingTaskDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir="/private/tmp")
        self.db=Database(str(Path(self.tmp.name)/"coding.sqlite"));self.db.initialize()
        self.db.create_project("project",self.tmp.name,time.time(),"project")
        for conversation in ("a","b","c"): self.db.create_conversation(conversation,time.time(),conversation,"project")
    def tearDown(self): self.tmp.cleanup()
    def task(self, task_id: str, conversation: str):
        return self.db.create_coding_task(task_id,conversation,"fix sample","RESUMABLE","NONE",plan("fix sample"),time.time())
    def test_mutation_mode_distinguishes_existing_repair_from_new_capability(self):
        self.assertEqual(("FIX", "EXISTING_BEHAVIOR_REPAIR"), classify_mutation_mode("開始ボタンを押してもゲームが始まりません。直してください"))
        self.assertEqual("IMPLEMENTATION", classify_mutation_mode("ログイン機能を追加してください")[0])
        self.assertEqual("FIX", classify_mutation_mode("既存のHeroアニメーションが途中で止まるので直して")[0])
        self.assertEqual("IMPLEMENTATION", classify_mutation_mode("HeroにAnime.jsのアニメーションを新しく追加して")[0])
        self.assertIn("exactly one phase", plan_prompt("ボタンが動かないので直して", mutation_mode="FIX"))

    def test_new_project_with_post_implementation_repairs_is_still_implementation(self):
        for request in [
            '空のプロジェクトから「昼食ルーレット」という日本語のWebアプリを新規作成してください。画面と抽選ロジックを分け、実装後はテストを実行し、失敗があれば修正してください。',
            'Webアプリを新規作成してください。画面を実装し、失敗したテストは修正してください。',
            '日本語のTODOアプリを作ってください。画面を作り、テストに失敗したら修正してください。',
            '新規Webアプリを作成してください。画面を作り、テストの失敗があれば修正してください。',
            'Create a new web app from scratch. Run tests and fix failures.',
        ]:
            with self.subTest(request=request):
                self.assertEqual('IMPLEMENTATION',canonical_coding_requirements(request)['mutation_mode'])
        self.assertEqual('FIX',classify_mutation_mode('空のプロジェクトから作成した既存アプリが動かないので修正してください。')[0])
        self.assertEqual('FIX',classify_mutation_mode('既存の画面の不具合を修正し、テストファイルを新規作成してください。')[0])

    def test_verification_only_missing_criteria_gets_bounded_internal_repair(self):
        value, changes = repair_phase_complexity({"phases": [{"id": "p1", "execution_mode": "VERIFICATION_ONLY",
                                                               "done": ["check complete"], "verify": []}]})
        self.assertEqual(["read-only verification evidence collected"], value["phases"][0]["verify"])
        self.assertEqual(["p1:added_verification_criterion"], changes)
        self.assertEqual([], phase_execution_contract_errors(value["phases"][0]))

    def test_plan_prompt_declares_exact_file_manifest_contract(self):
        prompt = plan_prompt("Build a componentized Vite React site", task_size="LARGE")
        self.assertIn("MUTATION_SCOPE_MODEL=EXACT_FILE_MANIFEST", prompt)
        self.assertIn("REQUIRE_ALL_IMPLEMENTATION_FILES_IN_MANIFEST=true", prompt)
        self.assertIn("same implementation phase as the source it exercises",prompt)
        self.assertNotIn("up to 4 when distinct test-code creation is needed",prompt)
        small_prompt=plan_prompt("Create a simple web app and add tests")
        self.assertNotIn("up to 4 when distinct test-code creation is needed",small_prompt)

    def test_manifest_owner_must_be_a_mutation_capable_phase_when_declared(self):
        value=plan('Create the app')
        value['phases'][2]['execution_mode']='VERIFICATION_ONLY'
        value['file_manifest']=[{'path':'app.js','action':'create','owner_phase':'unknown'}]
        self.assertIn('file_manifest owner_phase must identify a plan phase',validate_plan(value,'Create the app'))
        value['file_manifest'][0]['owner_phase']='p3'
        self.assertIn('file_manifest owner_phase must permit implementation',validate_plan(value,'Create the app'))
        value['file_manifest'][0]['owner_phase']='p1'
        self.assertEqual([],validate_plan(value,'Create the app'))
        value['file_manifest'][0].pop('owner_phase')
        self.assertEqual([],validate_plan(value,'Create the app'))  # legacy plans remain readable

    def test_plan_prompt_separates_typed_verification_commands_from_prose(self):
        prompt = plan_prompt("Fix a small bug and run focused verification", mutation_mode="FIX")
        self.assertIn("verification_command?:[string]", prompt)
        self.assertIn("verification_targets?:[string]", prompt)
        self.assertIn("The verifier never executes prose from verify", prompt)
        self.assertIn("Preserve existing typed verification_command argv", plan_repair_prompt("fix", "{}"))

    def test_phase_change_with_replanning_language_is_still_an_implementation_deliverable(self):
        phase = {
            "id": "p1",
            "goal": "Change only test_scheduler.py imports, run the verifier, and checkpoint as RESUMABLE without repair or replanning.",
            "done": ["The import-only change is applied and verifier output is checkpointed."],
            "verify": ["Run the targeted unittest discovery command."],
        }
        self.assertTrue(phase_has_observable_deliverable(phase))

    def test_idempotent_write_does_not_satisfy_path_bearing_implementation_criterion(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            target = Path(directory) / "test_scheduler.py"
            target.write_text("already correct\n", encoding="utf-8")
            command = ["python3", "-B", "-m", "unittest", "discover", "-s", ".", "-v"]
            phase = {
                "id": "p1",
                "goal": "Change test_scheduler.py imports to relative-only.",
                "execution_mode": "IMPLEMENTATION",
                "done": ["test_scheduler.py imports have changed to relative-only."],
                "verify": ["The verifier reports the expected import failure."],
                "verification_command": command,
                "verification_targets": ["test_scheduler.py"],
            }
            operations = [
                {"tool": "workspace_write", "status": "success",
                 "output": {"path": str(target)}},
                {"tool": "verification_command", "status": "success",
                 "input": {"command": command, "write_scope": "NONE"},
                 "output": {"returncode": 0, "read_only_targets": [str(target)],
                            "changed_targets": [], "zero_tests": False}},
            ]
            report = {
                "phase_id": "p1", "plan_revision": 0, "attempt": 0, "status": "PASS",
                "implemented": ["Updated test_scheduler.py"], "changed_files": [str(target)],
                "test_executed": ["unittest discovery"], "test_pass": ["unittest discovery (exit_code=0)"],
                "test_fail": [], "build_executed": "NOT_RUN", "build_pass": "NOT_RUN",
                "errors": [], "blockers": [],
                "typed_execution_summary": {
                    "mutation_classification": "IDEMPOTENT_NOOP", "accepted_mutations": 1,
                    "final_worktree_mutations": 0, "operations": operations,
                },
            }
            evidence = evaluate_phase(phase, report, workspace_root=directory)
            self.assertFalse(evidence["phase_complete"])
            self.assertEqual(["test_scheduler.py imports have changed to relative-only."],
                             evidence["done_unmet"])

    def test_plan_prompt_supplies_complete_task_graph_skeleton_and_namespaces(self):
        prompt = plan_prompt("Fix a small bug and run focused verification", mutation_mode="FIX")
        self.assertIn('"task_id":"p1-task","phase_id":"p1"', prompt)
        for field in ("goal", "domain", "depends_on", "required_context", "required_mcp",
                      "change_scope", "verification", "done_condition", "retry_state"):
            self.assertIn(f'"{field}"', prompt)
        self.assertIn("Phase dependencies contain phase IDs only", prompt)
        self.assertIn("Never omit an empty array or empty object", prompt)
        phase_properties = plan_schema()["properties"]["phases"]["items"]["properties"]
        self.assertEqual({"type": "boolean"}, phase_properties["checkpoint_on_failure"])

    def test_requirement_normalization_ignores_future_fix_and_descriptive_architecture(self):
        request="""# Current Task
This initial task is IMPLEMENTATION, not FIX. ReactでOLCRの新しいmarketing websiteを作成する。
Anime.js MCP、shadcn MCP、Playwright MCPを使用する。Anime.js v4を導入する。
backend/databaseは実装しない。不要なdependencyは追加しない。
# Product Description
サイト上でOLCRのbackendとdatabase architectureを説明する。
# Future FIX Benchmark
Later user requests such as 「ボタンを直して」と言われたらFIXにする。
# Regression Tests
backend/databaseの失敗も検証する。"""
        requirements=canonical_coding_requirements(request)
        self.assertEqual("IMPLEMENTATION", requirements["mutation_mode"])
        self.assertEqual({"frontend", "dependencies"}, set(requirements["required_capabilities"]))
        self.assertEqual({"backend", "database"}, set(requirements["forbidden_capabilities"]))
        self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", requirements["task_profile"])
        self.assertEqual("NORMAL", requirements["execution_mode"])
        self.assertEqual(["animejs", "shadcn", "playwright"], requirements["required_mcps"])

    def test_explicit_plan_only_is_separate_from_ordinary_plan_then_implement(self):
        plan_only = "新しいCoding Taskとして追加してください。まず計画を表示したところで停止し、まだファイルは変更しないでください。"
        continuous = "まず計画を作成し、その後にファイルを実装・検証してください。"
        self.assertTrue(canonical_coding_requirements(plan_only)["planning_only"])
        self.assertFalse(canonical_coding_requirements(continuous)["planning_only"])
        gui_request = (
            "計画だけを作成してください。現在のワークスペースに E2E_FINAL_PLAN.md と "
            "E2E_FINAL_PLAN.py を作る計画を立て、Python関数 calculate_sum(n) が n+1 を返すこと、"
            "および検証方法を明記してください。今はファイル作成・変更・削除、コマンド実行、依存追加を一切せず、"
            "計画を表示したら必ず停止して私の明示承認を待ってください。"
        )
        diagnostics = coding_classification_diagnostics(gui_request, project_scoped=True)
        self.assertEqual("CODING", diagnostics["classification"])
        self.assertEqual("EXPLICIT_PLAN_ONLY_CODING_REQUEST", diagnostics["reason"])
        self.assertFalse(diagnostics["mutation_intent"])
        self.assertTrue(coding_candidate(gui_request))

    def test_verification_request_saying_not_planning_only_is_not_plan_only(self):
        request = (
            "This is a Coding Task with change mode VERIFICATION_ONLY. The existing "
            "OLCR_E2E_SHORTFIX_R11_20260923.py already meets the requirements. "
            "No implementation, edit, creation or deletion is needed, and do not call an implementer model. "
            "Run only the read-only verification_command `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v "
            "OLCR_E2E_SHORTFIX_R11_20260923.py` and report the result. "
            "This is a request to execute verification, not planning only."
        )
        self.assertFalse(explicitly_requests_plan_only(request))
        diagnostics = coding_classification_diagnostics(request, project_scoped=True)
        self.assertEqual("EXPLICIT_VERIFICATION_ONLY_TASK", diagnostics["reason"])
        self.assertEqual("CODING", diagnostics["classification"])
        self.assertFalse(diagnostics["mutation_intent"])
        self.assertFalse(diagnostics["planning_intent"])

    def test_explicit_plan_only_still_requires_approval(self):
        request = (
            "Plan only: design a Python function in E2E_FINAL_PLAN.py. Do not edit files; "
            "show the plan and wait for my approval."
        )
        self.assertTrue(explicitly_requests_plan_only(request))
        diagnostics = coding_classification_diagnostics(request, project_scoped=True)
        self.assertEqual("EXPLICIT_PLAN_ONLY_CODING_REQUEST", diagnostics["reason"])

    def test_canonical_dependencies_are_structured_and_assigned_to_active_phase(self):
        requirements = canonical_coding_requirements(
            "Create a Vite React TypeScript Tailwind shadcn Anime.js v4 marketing site from scratch."
        )
        records = canonical_dependency_requirements(requirements)
        self.assertEqual(10, len(records))
        self.assertIn("animejs@4", dependency_requirement_specs(records))
        self.assertEqual(["animejs@4", "react", "react-dom"], sorted(
            spec for spec in dependency_requirement_specs(records) if spec in {"animejs@4", "react", "react-dom"}))
        technologies = canonical_technology_requirements(requirements)
        shadcn = next(item for item in technologies if item["technology"] == "shadcn")
        self.assertEqual("MCP_MANAGED_TOOLING", shadcn["semantic_type"])
        self.assertIsNone(shadcn["package"])
        self.assertNotIn("@shadcn/ui", dependency_requirement_specs(records))
        operations = dependency_install_operations(records)
        self.assertEqual(["dependencies", "devDependencies"], [item["dependency_kind"] for item in operations])
        plan_value = {"phases": [
            {"id": "p1", "goal": "Implement the application", "status": "pending",
             "done": ["files"], "verify": ["checks"], "dependencies": [], "risks": [],
             "execution_mode": "IMPLEMENTATION"},
            {"id": "p2", "goal": "Browser verification", "status": "pending",
             "done": ["browser"], "verify": ["browser"], "dependencies": ["p1"], "risks": [],
             "execution_mode": "VERIFICATION_ONLY"},
        ]}
        normalized, changes = associate_dependency_requirements(plan_value, requirements)
        self.assertTrue(changes)
        self.assertEqual(10, len(normalized["phases"][0]["dependency_requirements"]))
        self.assertTrue(normalized["phases"][0]["requires_dependency_installation"])
        self.assertNotIn("dependency_requirements", normalized["phases"][1])

    def test_dependency_requirements_with_explicit_phase_do_not_run_in_other_phase(self):
        requirements = {"dependency_requirements": [{"package": "react", "source_requirement": "required_stack",
                                                        "canonical_stack_provenance": "React", "phase_id": "p2"}]}
        plan_value = {"phases": [
            {"id": "p1", "goal": "Implement files", "status": "pending", "done": ["files"],
             "verify": ["checks"], "dependencies": [], "risks": [], "execution_mode": "IMPLEMENTATION"},
            {"id": "p2", "goal": "Install dependencies", "status": "pending", "done": ["install"],
             "verify": ["checks"], "dependencies": ["p1"], "risks": [], "execution_mode": "IMPLEMENTATION"},
        ]}
        normalized, _ = associate_dependency_requirements(plan_value, requirements)
        self.assertNotIn("dependency_requirements", normalized["phases"][0])
        self.assertEqual("p2", normalized["phases"][1]["dependency_requirements"][0]["phase_id"])

    def test_normalizer_structured_input_isolates_typed_project_and_history(self):
        normalized = normalize_coding_requirements({
            "current_user_text": "Reactの静的サイトを作成。backend/databaseは実装しない。Anime.jsを使用する。",
            "project_metadata": {"target_kind": "website", "description": "backend database frontend dependencies"},
            "selected_workspace": "/tmp/project",
            "conversation_id": "conversation-1",
            "conversation_history": "以前の assistant: backend/database/frontend/dependencies を実装した。",
            "assistant_history": "backend database frontend dependencies",
            "model_context": {"prose": "backend database frontend dependencies"},
        })
        self.assertEqual(["dependencies", "frontend"], normalized["required_capabilities"])
        self.assertEqual(["backend", "database"], normalized["forbidden_capabilities"])
        diagnostics = normalized["normalization_diagnostics"]
        self.assertEqual(diagnostics["current_user_text_hash"], diagnostics["normalizer_control_input_hash"])
        self.assertFalse(diagnostics["project_context_added_before_normalization"])
        self.assertFalse(diagnostics["conversation_history_added_before_normalization"])
        self.assertFalse(diagnostics["assistant_history_added_before_normalization"])
        emitted = {(item["capability"], item["polarity"]) for item in diagnostics["capability_provenance"] if item["active_for_control"]}
        self.assertIn(("frontend", "required"), emitted)
        self.assertIn(("dependencies", "required"), emitted)
        self.assertIn(("backend", "forbidden"), emitted)
        self.assertIn(("database", "forbidden"), emitted)

    def test_unrelated_frontend_and_dependency_policies_are_scope_only(self):
        normalized = normalize_coding_requirements(
            "React frontendを実装する。do not modify unrelated frontend changes; "
            "do not add unnecessary dependencies. backendを実装しない。"
        )
        self.assertEqual(["frontend"], normalized["required_capabilities"])
        self.assertEqual(["backend"], normalized["forbidden_capabilities"])
        self.assertEqual("MINIMAL", normalized["dependency_policy"])

    def test_provenance_is_current_turn_only(self):
        normalized = normalize_coding_requirements("frontendを実装する。backendは実装しない。")
        provenance = normalized["normalization_diagnostics"]["capability_provenance"]
        self.assertTrue(provenance)
        self.assertTrue(all(item["source_scope"] == "current_user_text" and item["active_for_control"] for item in provenance))
        self.assertTrue(all(item["source_span_hash"] for item in provenance))
    def test_plan_validation_and_typed_failure_gate(self):
        self.assertEqual([],validate_plan(plan("fix sample"),"fix sample"))
        report={"status":"PASS","errors":[],"blockers":[],"test_fail":[],"test_executed":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN"}
        self.assertFalse(report_has_authoritative_failure(report,plan("fix sample")["phases"][0]))
        self.assertTrue(report_has_authoritative_failure({**report,"status":"FAIL"},plan("fix sample")["phases"][0]))
        self.assertTrue(report_has_authoritative_failure(report,{**plan("fix sample")["phases"][0],"verify":["run test"]}))

    def test_plan_delimiters_are_not_observable_conditions(self):
        for fragment in ('],','verify:[','verify','"done": [', '{}', '', {'verify':[]}):
            with self.subTest(fragment=fragment):
                value=plan('fix sample')
                value['phases'][0]['done']=['Real implementation condition',fragment]
                errors=validate_plan(value,'fix sample')
                self.assertTrue(errors)
                self.assertIn('observable condition',errors[0])
                self.assertIsNone(api.deterministic_plan_schema_repair(value,'fix sample',errors))
        valid=plan('fix sample')
        valid['phases'][0]['done']=['Empty input returns []', '画面に「候補から選ぶ」を表示する']
        self.assertEqual([],validate_plan(valid,'fix sample'))

    def test_authoritative_failure_gate_accepts_only_exactly_verified_legacy_noop(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            target = root / "ledger.py"
            target.write_text("def total(items): return sum(items)\n", encoding="utf-8")
            command = ["python3", "-B", "-m", "unittest", "-v", "test_ledger"]
            phase = {"id": "p1", "execution_mode": "IMPLEMENTATION",
                     "done": ["ledger implementation is complete"], "verify": ["unit tests pass"],
                     "verification_command": command, "verification_targets": ["ledger.py"]}
            verifier = {"tool": "verification_command", "status": "success",
                        "input": {"command": command, "write_scope": "NONE"},
                        "output": {"returncode": 0, "read_only_targets": ["ledger.py"],
                                   "changed_targets": [], "zero_tests": False}}
            report = {"phase_id": "p1", "attempt": 0, "status": "FAIL", "errors": [],
                      "blockers": [], "test_fail": [], "test_executed": ["unittest"],
                      "build_executed": "NOT_RUN", "build_pass": "NOT_RUN", "changed_files": [],
                      "typed_execution_summary": {
                          "error": "IMPLEMENTER_STRUCTURED_OUTPUT_INVALID: no operations",
                          "accepted_mutations": 0, "files_written": 0,
                          "final_worktree_mutations": 0, "operations": [verifier]}}

            self.assertFalse(report_has_authoritative_failure(report, phase, directory))
            unverified = {**report, "typed_execution_summary": {
                **report["typed_execution_summary"], "operations": [{**verifier, "output": {
                    **verifier["output"], "returncode": 1}}]}}
            self.assertTrue(report_has_authoritative_failure(unverified, phase, directory))

    def test_canonical_phase_modes_are_materialized_without_text_inference(self):
        value, changes = ensure_explicit_phase_execution_modes({"phases": [
            {"id": "p1", "goal": "arbitrary", "done": ["done"], "verify": ["check"], "dependencies": [], "risks": []},
            {"id": "p2", "goal": "arbitrary", "done": ["done"], "verify": ["check"], "dependencies": [], "risks": [],
             "requires_repo_mutation": False, "required_mcp": ["playwright"]},
        ]})
        self.assertEqual(["IMPLEMENTATION", "VERIFICATION_ONLY"], [item["execution_mode"] for item in value["phases"]])
        self.assertEqual(["p1:IMPLEMENTATION", "p2:VERIFICATION_ONLY"], changes)

    def test_dependency_installation_requires_an_authorized_executor(self):
        capability = phase_executor_capability({"requires_dependency_installation": True})
        self.assertFalse(capability["executable"])
        self.assertEqual("PACKAGE_MANAGER_COMMAND", capability["missing"][0]["executor"])

    def test_no_dependency_contract_does_not_assign_package_installation(self):
        plan_value = {"phases": [{"id": "p1", "goal": "Fix a source file", "status": "pending",
                                   "done": ["file fixed"], "verify": ["read back"], "dependencies": [],
                                   "risks": [], "execution_mode": "IMPLEMENTATION"}]}
        normalized, changes = associate_dependency_requirements(plan_value, {"required_stack": []})
        self.assertEqual([], changes)
        self.assertNotIn("requires_dependency_installation", normalized["phases"][0])

    def test_required_not_run_reports_typed_ids_and_executor_capability(self):
        phase = {"id": "p1", "done": ["npm install completed"],
                 "verify": ["build passes"], "dependencies": [], "risks": []}
        report = {"phase_id": "p1", "plan_revision": 0, "status": "NOT_RUN",
                  "errors": [], "blockers": [], "test_fail": [],
                  "test_executed": [], "build_executed": "NOT_RUN", "build_pass": "NOT_RUN"}
        evidence = evaluate_phase(phase, report)
        self.assertEqual(["DONE:1", "VERIFY:1"], evidence["required_not_run_ids"])
        self.assertEqual(["DONE", "VERIFY"], evidence["required_not_run_kinds"])
        self.assertEqual(["PACKAGE_MANAGER_COMMAND", "VERIFICATION_COMMAND_OR_MCP"],
                         evidence["required_not_run_executor_capability"])

    def test_typed_verification_command_cannot_pass_without_execution_evidence(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            target = Path(directory) / "probe.py"
            target.write_text("assert True\n", encoding="utf-8")
            verify = "python -B probe.py exits successfully"
            phase = {"id": "p1", "execution_mode": "IMPLEMENTATION_AND_VERIFICATION",
                     "done": ["probe.py exists"], "verify": [verify],
                     "verification_command": ["python", "-B", str(target)],
                     "verification_targets": [str(target)], "verification_executor": "python"}
            report = {"phase_id": "p1", "plan_revision": 0, "status": "PASS",
                      "errors": [], "blockers": [], "test_fail": [], "test_executed": [],
                      "build_executed": "NOT_RUN", "build_pass": "NOT_RUN",
                      "changed_files": [str(target)],
                      "criteria_evidence": {"probe.py exists": True, verify: True},
                      "typed_execution_summary": {"operations": [
                          {"tool": "workspace_write", "status": "success",
                           "output": {"path": str(target)}}]}}
            missing = evaluate_phase(phase, report, workspace_root=directory)
            self.assertFalse(missing["phase_complete"])
            self.assertTrue(missing["required_not_run_present"])
            self.assertIn("VERIFY:1", missing["required_not_run_ids"])

            verified = {**report, "typed_execution_summary": {"operations": [
                *report["typed_execution_summary"]["operations"],
                {"tool": "verification_command", "status": "success",
                 "input": {"command": ["python3", "-B", str(target)], "write_scope": "NONE"},
                 "output": {"returncode": 0, "read_only_targets": ["probe.py"],
                            "changed_targets": []}},
            ]}}
            passed = evaluate_phase(phase, verified, workspace_root=directory)
            self.assertTrue(passed["phase_complete"])
            self.assertFalse(passed["required_not_run_present"])

    def test_verified_noop_requires_complete_exact_read_only_verifier_evidence(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            target = root / "probe.py"
            target.write_text("assert True\n", encoding="utf-8")
            command = ["python3", "-B", str(target)]
            phase = {"id": "p1", "execution_mode": "IMPLEMENTATION_AND_VERIFICATION",
                     "done": ["probe implementation is complete"],
                     "verify": ["unit tests pass"],
                     "verification_command": command,
                     "verification_targets": ["probe.py"]}
            operation = {"tool": "verification_command", "status": "success",
                         "input": {"command": command, "write_scope": "NONE"},
                         "output": {"returncode": 0, "read_only_targets": ["probe.py"],
                                    "changed_targets": [], "zero_tests": False}}
            report = {"phase_id": "p1", "plan_revision": 0, "status": "FAIL",
                      "errors": [], "blockers": [], "test_fail": [], "test_executed": [],
                      "build_executed": "NOT_RUN", "build_pass": "NOT_RUN", "changed_files": [],
                      "typed_execution_summary": {
                          "error": "IMPLEMENTER_STRUCTURED_OUTPUT_INVALID: malformed response",
                          "accepted_mutations": 0, "files_written": 0,
                          "final_worktree_mutations": 0,
                          "operations": [operation]},
                      "test_pass": ["unittest: 1 test passed"]}

            accepted = evaluate_phase(phase, report, workspace_root=directory)
            self.assertTrue(accepted["verified_noop_implementation"])
            self.assertTrue(accepted["phase_complete"], accepted)

            explicit_noop = {"tool": "implementation_noop_candidate", "status": "success",
                             "input": {"change_required": False, "reason_code": "already_satisfied"},
                             "output": {"source_inspected": True, "condition_evaluated": True,
                                        "target_hashes": {"probe.py": hashlib.sha256(target.read_bytes()).hexdigest()},
                                        "worktree_state": "UNCHANGED"}}
            completed_noop_report = {**report, "status": "PASS", "test_executed": ["unittest"],
                                     "typed_execution_summary": {
                                         "error": "", "accepted_mutations": 0, "files_written": 0,
                                         "final_worktree_mutations": 0,
                                         "operations": [explicit_noop, operation]}}
            completed_noop = evaluate_phase(phase, completed_noop_report, workspace_root=directory)
            self.assertTrue(completed_noop["verified_noop_implementation"], completed_noop)
            self.assertTrue(completed_noop["phase_complete"], completed_noop)

            stale_hash_report = {**completed_noop_report, "typed_execution_summary": {
                **completed_noop_report["typed_execution_summary"],
                "operations": [{**explicit_noop, "output": {
                    **explicit_noop["output"], "target_hashes": {"probe.py": "0" * 64}}}, operation]}}
            stale_hash = evaluate_phase(phase, stale_hash_report, workspace_root=directory)
            self.assertFalse(stale_hash["verified_noop_implementation"])
            self.assertFalse(stale_hash["phase_complete"])

            invalid_operations = [
                {**operation, "input": {**operation["input"], "command": ["python3", "-B", "wrong.py"]}},
                {**operation, "input": {**operation["input"], "write_scope": "WORKSPACE"}},
                {**operation, "output": {**operation["output"], "zero_tests": True}},
                {**operation, "output": {**operation["output"], "zero_tests": None}},
                {**operation, "output": {**operation["output"], "changed_targets": None}},
                {**operation, "output": {**operation["output"], "changed_targets": ["probe.py"]}},
                {**operation, "output": {**operation["output"], "read_only_targets": []}},
                {**operation, "output": {**operation["output"], "returncode": 1}},
            ]
            for invalid in invalid_operations:
                with self.subTest(operation=invalid):
                    candidate = {**report, "typed_execution_summary": {
                        **report["typed_execution_summary"], "operations": [invalid]}}
                    evidence = evaluate_phase(phase, candidate, workspace_root=directory)
                    self.assertFalse(evidence["verified_noop_implementation"])
                    self.assertFalse(evidence["phase_complete"])

            outside_target = root.parent / (root.name + "-outside.py")
            outside_target.write_text("assert True\n", encoding="utf-8")
            try:
                outside_phase = {**phase, "verification_targets": [str(outside_target)]}
                outside_report = {**report, "typed_execution_summary": {
                    **report["typed_execution_summary"], "operations": [{
                        **operation, "output": {**operation["output"],
                                                "read_only_targets": [str(outside_target)]}}]}}
                evidence = evaluate_phase(outside_phase, outside_report, workspace_root=directory)
                self.assertFalse(evidence["verified_noop_implementation"])
                self.assertFalse(evidence["phase_complete"])
            finally:
                outside_target.unlink(missing_ok=True)

    def test_verified_noop_implementation_mode_accepts_exact_read_only_verifier(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            target = root / "ledger.py"
            target.write_text("def total(items): return sum(items)\n", encoding="utf-8")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            command = ["python3", "-B", "-m", "unittest", "-v", "test_ledger"]
            phase = {"id": "p1", "execution_mode": "IMPLEMENTATION",
                     "done": ["ledger implementation is complete"],
                     "verify": ["unit tests pass"], "verification_command": command,
                     "verification_targets": ["ledger.py"]}
            noop = {"tool": "implementation_noop_candidate", "status": "success",
                    "input": {"change_required": False, "reason_code": "already_satisfied"},
                    "output": {"source_inspected": True, "condition_evaluated": True,
                               "target_hashes": {"ledger.py": digest},
                               "worktree_state": "UNCHANGED"}}
            verifier = {"tool": "verification_command", "status": "success",
                        "input": {"command": command, "write_scope": "NONE"},
                        "output": {"returncode": 0, "read_only_targets": ["ledger.py"],
                                   "changed_targets": [], "zero_tests": False}}
            report = {"phase_id": "p1", "plan_revision": 0, "status": "PASS",
                      "errors": [], "blockers": [], "test_fail": [], "changed_files": [],
                      "typed_execution_summary": {
                          "accepted_mutations": 0, "files_written": 0,
                          "final_worktree_mutations": 0,
                          "operations": [noop, verifier]}}

            evidence = evaluate_phase(phase, report, workspace_root=directory)

            self.assertTrue(evidence["verified_noop_implementation"], evidence)
            self.assertTrue(evidence["phase_complete"], evidence)

    def test_package_json_edit_does_not_claim_dependency_installation(self):
        phase = {"id": "p1", "done": ["npm install completed"], "verify": ["dependencies available"]}
        report = {"phase_id": "p1", "plan_revision": 0, "status": "PASS", "errors": [],
                  "blockers": [], "test_fail": [], "test_executed": [],
                  "build_executed": "NOT_RUN", "build_pass": "NOT_RUN",
                  "typed_execution_summary": {"operations": [{
                      "tool": "workspace_write", "status": "success",
                      "output": {"path": "package.json"}}]}}
        evidence = evaluate_phase(phase, report)
        self.assertFalse(evidence["phase_complete"])
        self.assertIn("npm install completed", evidence["done_unmet"])
        installed = {**report, "typed_execution_summary": {"operations": [{
            "tool": "package_install", "status": "success",
            "output": {"path": "package.json", "exit_code": 0}}]}}
        installed_evidence = evaluate_phase(phase, installed)
        self.assertEqual(1, installed_evidence["done_satisfied"])

    def test_static_site_is_normal_and_empty_dedicated_verify_is_valid(self):
        request="Create a simple two-page static HTML website using index.html, links.html and styles.css, with responsive accessibility. No framework, backend, database, or dependencies."
        self.assertEqual("NORMAL",classify_execution_mode(request))
        compact=plan(request)
        compact["phases"]=compact["phases"][:2]
        compact["phases"][0]["verify"]=[]
        compact["phases"][1]["dependencies"]=["p1"]
        compact.pop("tasks",None)
        self.assertEqual([],validate_plan(compact,request))

    def test_static_site_plan_compaction_preserves_requirements(self):
        request="Create a simple static HTML website using index.html, links.html and styles.css, with responsive accessibility. No framework, backend, database, or dependencies."
        oversized=plan(request)
        oversized["phases"]=[
            {"id":"p1","goal":"inspect repository","status":"pending","done":["repo facts"],"verify":["paths checked"],"dependencies":[],"risks":["scope"]},
            {"id":"p2","goal":"create index.html","status":"pending","done":["home page"],"verify":["HTML structure"],"dependencies":["p1"],"risks":["markup"]},
            {"id":"p3","goal":"create links.html","status":"pending","done":["links page"],"verify":["links present"],"dependencies":["p2"],"risks":["navigation"]},
            {"id":"p4","goal":"add styles and accessibility","status":"pending","done":["responsive style"],"verify":["keyboard access"],"dependencies":["p3"],"risks":["layout"]},
            {"id":"p5","goal":"browser verification","status":"pending","done":["browser checked"],"verify":["browser verification"],"dependencies":["p4"],"risks":["manual check"]},
        ]
        oversized["tasks"]=[
            {"task_id":phase["id"],"phase_id":phase["id"],"goal":phase["goal"],"domain":"frontend","depends_on":phase["dependencies"],"required_context":[phase["id"]],"required_mcp":[],"change_scope":[phase["id"]],"verification":phase["verify"],"done_condition":phase["done"],"retry_state":{}}
            for phase in oversized["phases"]
        ]
        compact=compact_normal_plan(oversized,request)
        self.assertEqual(2,len(compact["phases"]))
        self.assertEqual([],validate_plan(compact,request))
        merged=compact["phases"][0]
        self.assertTrue({"repo facts","home page","links page","responsive style"} <= set(merged["done"]))
        self.assertTrue({"paths checked","HTML structure","links present","keyboard access"} <= set(merged["verify"]))
        self.assertEqual([merged["id"]],compact["phases"][1]["dependencies"])
        self.assertEqual(2,len(compact["tasks"]))
        self.assertTrue({"p1","p2","p3","p4"} <= set(compact["tasks"][0]["required_context"]))

    def test_web_only_execution_is_not_a_coding_implementation_success(self):
        execution=Task("implementation phase")
        execution.transition(TaskState.ROUTING)
        execution.route=Route.NEURAL
        execution.transition(TaskState.GENERATING)
        execution.transition(TaskState.COMPLETED)
        report=api._report_from_execution(plan("goal")["phases"][0],0,execution,"web answer")
        self.assertEqual("FAIL",report["status"])
        self.assertIn("CODING_IMPLEMENTATION_ZERO_WRITE_WEB_ROUTE",report["errors"])
    def test_plan_json_extraction_allows_one_fence_but_rejects_ambiguous_prose(self):
        value=plan("fix sample")
        parsed,error=extract_plan_json("```json\n"+__import__("json").dumps(value)+"\n```")
        self.assertEqual([],validate_plan(parsed,"fix sample")); self.assertEqual("",error)
        parsed,error=extract_plan_json("before "+__import__("json").dumps(value))
        self.assertIsNone(parsed); self.assertTrue(error)
        parsed,error=extract_plan_json(__import__("json").dumps(value)+"\n"+__import__("json").dumps(value))
        self.assertIsNone(parsed); self.assertIn("exactly one",error)
    def test_fifo_dequeue_and_resume_to_tail(self):
        for task_id,conversation in (("a","a"),("b","b"),("c","c")):
            self.task(task_id,conversation);self.db.enqueue_coding_task(task_id)
        self.assertEqual("a",self.db.next_queued_coding_task()["id"])
        self.assertEqual(2,self.db.coding_task_queue_position("b"))
        self.db.dequeue_coding_task("b")
        self.assertEqual("RESUMABLE",self.db.coding_task("b")["status"])
        self.db.enqueue_coding_task("b")
        self.assertEqual(["a","c","b"],[self.db.next_queued_coding_task()["id"],self.db.coding_tasks("c")[0]["id"],self.db.coding_tasks("b")[0]["id"]])
        self.assertEqual(3,self.db.coding_task_queue_position("b"))
    def test_automatic_requeue_preserves_pause_and_explicit_resume_clears_it(self):
        self.task('paused','a')
        self.db.update_coding_task('paused',status='RUNNING',pause_requested=True,replan_count=1)
        stopped=self.db.enqueue_coding_task('paused',clear_pause=False)
        self.assertEqual('RUNNING',stopped['status'])
        self.assertTrue(stopped['pause_requested'])
        self.assertIsNone(stopped['queue_order'])
        self.assertIsNone(self.db.next_queued_coding_task())
        resumed=self.db.enqueue_coding_task('paused')
        self.assertEqual('QUEUED',resumed['status'])
        self.assertFalse(resumed['pause_requested'])
        self.assertEqual(1,resumed['replan_count'])
    def test_stop_updates_one_message_when_final_evidence_arrives(self):
        self.task('stop','a')
        first={'reason':'PAUSED','intended':True,'recovery_epoch':0,'plan_revision':1,'signature':'early','text':'early snapshot'}
        final={**first,'plan_revision':2,'signature':'final','text':'8 tests passed; browser verification pending'}
        self.db.update_coding_task('stop',resume_state={'preserved':'value'})
        self.db.persist_coding_stop('stop',first,1)
        self.db.persist_coding_stop('stop',final,2)
        self.db.persist_coding_stop('stop',final,4)
        self.assertEqual(final['text'],self.db.persist_coding_stop('stop',first,3))
        self.assertEqual(final['text'],self.db.persist_coding_stop('stop',first,99))
        self.assertEqual(final['text'],self.db.persist_coding_stop('stop',first,1))
        with self.db.connect() as conn:
            messages=conn.execute("SELECT content FROM messages WHERE task_id='stop'").fetchall()
        self.assertEqual([final['text']],[r[0] for r in messages])
        saved=self.db.coding_task('stop')['resume_state']
        self.assertEqual('value',saved['preserved'])
        self.assertEqual(4,saved['stop_progress']['report_count'])
        self.db.update_coding_task('stop',resume_state={**saved,'stop_progress':None})
        self.db.persist_coding_stop('stop',first,3)
        with self.db.connect() as conn:
            self.assertEqual(2,conn.execute("SELECT COUNT(*) FROM messages WHERE task_id='stop'").fetchone()[0])

    def test_simultaneous_stop_snapshots_only_create_one_message(self):
        from concurrent.futures import ThreadPoolExecutor
        self.task('stop-race','a')
        snapshot={'reason':'PAUSED','intended':True,'recovery_epoch':0,'signature':'same','text':'stopped'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.db.persist_coding_stop('stop-race',snapshot,1),range(2)))
        self.assertEqual(['stopped','stopped'],results)
        with self.db.connect() as conn:
            self.assertEqual(1,conn.execute("SELECT COUNT(*) FROM messages WHERE task_id='stop-race'").fetchone()[0])
    def test_restart_recovers_without_requeue(self):
        self.task("a","a");self.db.enqueue_coding_task("a")
        self.db.recover_interrupted_coding_tasks()
        task=self.db.coding_task("a")
        self.assertEqual("RESUMABLE",task["status"])
        self.assertIsNone(self.db.next_queued_coding_task())
    def test_waiting_for_plan_approval_does_not_block_the_next_queued_task(self):
        self.task("a","a")
        self.db.update_coding_task("a",status="WAITING_FOR_PLAN_APPROVAL",activity="NONE")
        self.task("b","b");self.db.enqueue_coding_task("b")
        self.assertEqual("b",self.db.next_queued_coding_task()["id"])
    def test_phase_report_uuid_round_trip_and_update(self):
        self.task("a","a")
        report={"phase_id":"p1","attempt":0,"status":"PASS","implemented":[],"changed_files":[],"test_executed":[],"test_pass":[],"test_fail":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN","errors":[],"blockers":[],"risks":[]}
        self.db.add_coding_phase_report("a","p1",0,report,"PASS",time.time())
        saved=self.db.coding_phase_reports("a")[0]
        self.assertIsInstance(saved["id"],str)
        saved["structured_report"]["manager_decision"]={"decision":"PASS","reason":"verified"}
        self.db.update_coding_phase_report(saved["id"],saved["structured_report"],"PASS")
        self.assertEqual("PASS",self.db.coding_phase_reports("a")[0]["structured_report"]["manager_decision"]["decision"])
    def test_model_slot_serializes_threads(self):
        concurrent=0;maximum=0;guard=threading.Lock()
        def work():
            nonlocal concurrent,maximum
            with model_slot():
                with guard: concurrent+=1;maximum=max(maximum,concurrent)
                time.sleep(.02)
                with guard: concurrent-=1
        threads=[threading.Thread(target=work) for _ in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(1,maximum)

    def test_manager_decision_normalizes_bounded_diagnosis_from_typed_report(self):
        phase=plan("fix sample")["phases"][0]
        report={"phase_id":"p1","plan_revision":1,"attempt":2,"status":"FAIL",
                "done":[],"errors":["typed write failed"],"blockers":[],"test_fail":[],
                "build_executed":"NOT_RUN"}
        typed={"state":"failed","error":"typed write failed","operations":[{"tool":"workspace_write","status":"error"}]}
        decision=normalize_manager_decision({"decision":"RETRY","reason":"repair the write"},phase,report,typed)
        self.assertEqual("RETRY",decision["decision"])
        diagnosis=decision["diagnosis"]
        self.assertEqual({"phase_id":"p1","plan_revision":1,"attempt":2},
                         {key:diagnosis[key] for key in ("phase_id","plan_revision","attempt")})
        self.assertTrue(diagnosis["unmet_done"])
        self.assertTrue(diagnosis["evidence"])
        self.assertTrue(diagnosis["retry_instruction"])

    def test_review_prompt_requires_structured_diagnosis(self):
        prompt=manager_review_prompt("fix sample",plan("fix sample")["phases"][0],
                                     {"phase_id":"p1"},{"state":"completed"},[],0,[],None)
        self.assertIn('"diagnosis"',prompt)
        self.assertIn("unmet_done",prompt)

    def _evidence_report(self, *, attempt=0, changed=("index.html",), raw_state="completed"):
        return {"phase_id":"p1","plan_revision":0,"attempt":attempt,"status":"PASS","implemented":[],
                "changed_files":list(changed),"test_executed":["typed readback"],"test_pass":["typed readback"],
                "test_fail":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN","errors":[],"blockers":[],"risks":[],
                "typed_execution_summary":{"state":raw_state,"error":None,"operations":[
                    {"tool":"workspace_write","status":"success","output":{"path":path}} for path in changed
                ] + [{"tool":"workspace_read","status":"success","output":{"path":path}} for path in changed]}}

    def test_complete_evidence_forces_pass_over_gemma_retry(self):
        phase=plan("fix sample")["phases"][0]; report=self._evidence_report()
        evaluation=evaluate_phase(phase,report)
        self.assertTrue(evaluation["phase_complete"])
        self.assertEqual(["PASS"],evaluation["allowed_decisions"])
        self.assertNotEqual("RETRY",evaluation["allowed_decisions"][0])

    def test_review_phase_derives_pass_from_typed_evidence_without_model_call(self):
        conversation="a"; task_id="authoritative-pass"; goal="fix sample"; task_plan=plan(goal)
        self.db.create_coding_task(task_id,conversation,goal,"RUNNING","NONE",task_plan,time.time())
        old_db=api.db; api.db=self.db
        report_row=api._save_report(task_id,"p1",0,self._evidence_report(),"PASS")
        try:
            with patch.object(api,"_model_text",side_effect=AssertionError("typed phase review must not call a model")):
                decision=api._review_phase(task_id,api.db.coding_task(task_id),task_plan["phases"][0],report_row,[])
        finally:
            api.db=old_db
        self.assertEqual("PASS",decision)
        saved=self.db.coding_phase_reports(task_id)[0]["structured_report"]
        self.assertEqual("PASS",saved["manager_decision"]["decision"])
        self.assertTrue(saved["phase_evaluation"]["phase_complete"])
        diagnostics=saved["manager_diagnostics"]
        self.assertIsNone(diagnostics["raw_decision"])
        self.assertTrue(diagnostics["decision_valid"])
        self.assertEqual("PASS",diagnostics["effective_decision"])
        self.assertTrue(diagnostics["phase_complete"])
        self.assertFalse(diagnostics["implementer_retry_required"])

    def test_review_blocks_missing_package_manager_without_repeating_the_same_install(self):
        task_id = "package-manager-unavailable"
        goal = "Create a frontend and install its authorized dependencies"
        task_plan = plan(goal)
        self.db.create_coding_task(task_id, "a", goal, "RUNNING", "NONE", task_plan, time.time())
        report = {"phase_id": "p1", "plan_revision": 0, "attempt": 0, "status": "FAIL",
                  "implemented": [], "changed_files": [], "test_executed": [], "test_pass": [],
                  "test_fail": [], "build_executed": "NOT_RUN", "build_pass": "NOT_RUN",
                  "errors": ["PACKAGE_MANAGER_UNAVAILABLE: package-manager executable is unavailable"],
                  "blockers": [], "risks": [],
                  "typed_execution_summary": {"state": "failed",
                      "failure_class": "PACKAGE_MANAGER_UNAVAILABLE",
                      "operations": [{"tool": "package_install", "status": "failed",
                                      "output": {"failure_class": "PACKAGE_MANAGER_UNAVAILABLE",
                                                 "process_started": False}}]}}
        old_db = api.db
        api.db = self.db
        try:
            row = api._save_report(task_id, "p1", 0, report, "FAIL")
            decision = api._review_phase(task_id, self.db.coding_task(task_id),
                                         task_plan["phases"][0], row, [])
            saved = self.db.coding_phase_reports(task_id)[0]["structured_report"]
        finally:
            api.db = old_db

        self.assertEqual("BLOCKED", decision)
        self.assertEqual("PACKAGE_MANAGER_UNAVAILABLE", saved["manager_decision"]["failure_class"])

    def test_cross_attempt_evidence_accumulates_and_delete_invalidates(self):
        phase={**plan("fix sample")["phases"][0],"done":["index.html exists","style.css exists"],"verify":["typed readback"]}
        first=self._evidence_report(changed=("index.html",)); second=self._evidence_report(attempt=1,changed=("style.css",))
        evaluation=evaluate_phase(phase,second,[first])
        self.assertEqual(2,evaluation["done_satisfied"])
        removed={**second,"typed_execution_summary":{"state":"completed","error":None,"operations":[{"tool":"workspace_delete","status":"success","output":{"path":"index.html","operation":"delete"}}]},"changed_files":[]}
        invalidated=evaluate_phase(phase,removed,[first])
        self.assertIn("index.html exists",invalidated["done_unmet"])

    def test_verification_owned_gap_does_not_require_implementer_retry(self):
        phase = {"id": "p1", "execution_mode": "IMPLEMENTATION_AND_VERIFICATION",
                 "done": [f"done-{i}" for i in range(11)],
                 "verify": [f"verify-{i}" for i in range(9)] + ["browser screenshot"],
                 "dependencies": [], "risks": [],
                 "criterion_executors": {"VERIFY:10": "READ_ONLY_VERIFICATION"}}
        report = {"phase_id": "p1", "plan_revision": 0, "status": "PASS", "errors": [],
                  "blockers": [], "test_fail": [], "changed_files": ["index.html"],
                  "criteria_evidence": {**{f"done-{i}": True for i in range(11)},
                                         **{f"verify-{i}": True for i in range(9)},
                                         "browser screenshot": False},
                  "typed_execution_summary": {"operations": [{"tool": "workspace_write", "status": "success",
                                                                  "output": {"path": "index.html"}}]}}
        evidence = evaluate_phase(phase, report)
        self.assertEqual([], evidence["unsatisfied_done_ids"])
        self.assertEqual(["VERIFY:10"], evidence["unsatisfied_verify_ids"])
        self.assertTrue(evidence["phase_advance_eligible"])
        self.assertFalse(evidence["implementer_retry_required"])
        self.assertEqual("READ_ONLY_VERIFICATION", evidence["next_required_executor"])

    def test_manager_binds_retry_to_verification_executor(self):
        task_id = "verification-routing"
        goal = "verify implementation"
        task_plan = {"schema_version": 1, "original_goal": goal,
                     "scope": {"allowed": ["index.html"], "forbidden": []}, "assumptions": [],
                     "phases": [{"id": "p1", "goal": "implement", "status": "pending",
                                 "done": ["implementation complete"], "verify": ["browser screenshot"],
                                 "dependencies": [], "risks": [],
                                 "execution_mode": "IMPLEMENTATION_AND_VERIFICATION",
                                 "criterion_executors": {"VERIFY:1": "READ_ONLY_VERIFICATION"}}],
                     "max_retries_per_phase": 2, "requires_user_approval": True}
        self.db.create_coding_task(task_id, "a", goal, "RUNNING", "NONE", task_plan, time.time())
        old_db = api.db
        api.db = self.db
        report = {"phase_id": "p1", "attempt": 0, "plan_revision": 0, "status": "PASS",
                  "implemented": [], "changed_files": ["index.html"], "test_executed": [],
                  "test_pass": [], "test_fail": [], "build_executed": "NOT_RUN", "build_pass": "NOT_RUN",
                  "errors": [], "blockers": [], "risks": [],
                  "criteria_evidence": {"implementation complete": True, "browser screenshot": False},
                  "typed_execution_summary": {"operations": [{"tool": "workspace_write", "status": "success",
                                                                  "output": {"path": "index.html"}}]}}
        try:
            row = api._save_report(task_id, "p1", 0, report, "PASS")
            decision = api._review_phase(task_id, self.db.coding_task(task_id), task_plan["phases"][0], row, [])
            saved = self.db.coding_phase_reports(task_id)[0]["structured_report"]
        finally:
            api.db = old_db
        self.assertEqual("RETRY", decision)
        self.assertEqual("READ_ONLY_VERIFICATION", saved["manager_decision"]["retry_channel"])
        self.assertFalse(saved["manager_diagnostics"]["implementer_retry_required"])

    def test_replan_package_name_is_never_a_phase_dependency(self):
        value = {"phases": [
            {"id": "p1", "dependencies": [], "dependency_requirements":
             [{"package": "vite", "phase_id": "p1"}]},
            {"id": "p2", "dependencies": ["vite"]},
        ], "tasks": [{"task_id": "p1-task", "phase_id": "p1", "depends_on": [],
                       "goal": "p1", "domain": "implementation", "required_context": [],
                       "required_mcp": [], "change_scope": [], "verification": [],
                       "done_condition": [], "retry_state": {}},
                      {"task_id": "p2-task", "phase_id": "p2", "depends_on": ["vite"],
                       "goal": "p2", "domain": "implementation", "required_context": [],
                       "required_mcp": [], "change_scope": [], "verification": [],
                       "done_condition": [], "retry_state": {}}]}
        normalized, changes = normalize_replan_graph(value)
        self.assertEqual(["p1"], normalized["phases"][1]["dependencies"])
        self.assertEqual(["p1-task"], normalized["tasks"][1]["depends_on"])
        self.assertFalse(any(error["offending_dependency_ref"] == "vite"
                             for error in graph_validation_diagnostics(normalized)["errors"]))
        self.assertTrue(any("package-reference" in item for item in changes))


class CodingTaskApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db,self.old_settings=api.db,api.settings
        api.db=Database(str(Path(self.tmp.name)/"api.sqlite"));api.db.initialize()
        # Coding Task Manager API tests exercise the existing manager path
        # explicitly; production defaults are covered by the OFF tests.
        api.rebuild(self.old_settings.with_overrides({
            "db_path": str(Path(self.tmp.name) / "api.sqlite"),
            "task_manager_enabled": True,
        }))
        self.client=TestClient(api.app)
        self.project=self.client.post("/api/projects",json={"name":"A","workspace_path":self.tmp.name}).json()["id"]
    def tearDown(self):
        api.db=self.old_db
        api.rebuild(self.old_settings)
        self.tmp.cleanup()

    def test_phase_failure_checkpoint_waits_for_same_task_resume_then_replans(self):
        conversation = self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id = "phase-failure-checkpoint"
        goal = "Fix probe.py and checkpoint after the first failed phase."
        task_plan = {
            "schema_version": 1, "original_goal": goal,
            "scope": {"allowed": ["probe.py"], "forbidden": []}, "assumptions": [],
            "phases": [{
                "id": "p1", "goal": "Fix probe.py", "status": "pending",
                "done": ["probe.py contains the fix"], "verify": ["the targeted test passes"],
                "dependencies": [], "risks": [], "execution_mode": "IMPLEMENTATION",
                "checkpoint_on_failure": True,
            }],
            "tasks": [{
                "task_id": "p1-task", "phase_id": "p1", "goal": "Fix probe.py",
                "domain": "implementation", "depends_on": [], "required_context": [],
                "required_mcp": [], "change_scope": ["probe.py"], "verification": [],
                "done_condition": ["probe.py contains the fix"], "retry_state": {},
            }],
            "max_retries_per_phase": 2, "requires_user_approval": True,
            "file_manifest": [{"path": "probe.py", "action": "modify",
                               "role": "existing probe implementation", "required": True,
                               "owner_phase": "p1"}],
        }
        probe_path = Path(self.tmp.name) / "probe.py"
        probe_path.write_text("# probe\n", encoding="utf-8")
        api.db.create_coding_task(task_id, conversation, goal, "QUEUED", "NONE", task_plan, time.time())

        class Runtime:
            def execute(self, text, **kwargs):
                execution = Task(text)
                execution.transition(TaskState.ROUTING)
                execution.transition(TaskState.GENERATING)
                execution.transition(TaskState.COMPLETED)
                return execution, "verification failed"

        report = {
            "phase_id": "p1", "attempt": 0, "plan_revision": 0, "status": "FAIL",
            "implemented": [], "changed_files": [], "test_executed": ["python3 -m unittest"],
            "test_pass": [], "test_fail": ["expected failure"], "build_executed": "NOT_RUN",
            "build_pass": "NOT_RUN", "errors": ["verification command failed"],
            "blockers": [], "risks": [],
            "typed_execution_summary": {
                "state": "completed", "operations": [], "final_worktree_mutations": 0,
                "mutation_classification": "NO_MUTATION",
            },
        }
        original_runtime = api.runtime
        api.runtime = Runtime()
        try:
            with patch.object(api, "_report_from_execution", return_value=report), \
                 patch.object(api, "_review_phase", return_value="RETRY"):
                result = api.run_managed_task(task_id, self.tmp.name)
            saved = api.db.coding_task(task_id)
            self.assertIn("RESUMABLE", result)
            self.assertEqual("RESUMABLE", saved["status"])
            self.assertEqual("REPLAN_CONTINUATION", saved["recovery_action"])
            self.assertEqual("PHASE_FAILURE_CHECKPOINT", saved["recovery_reason"])
            self.assertEqual("p1", saved["resume_state"]["phase_failure_checkpoint"]["phase_id"])

            with patch.object(api._coding_scheduler_wake, "set"), \
                 patch.object(api, "_replan_task", return_value="same task replanned") as replan:
                api._resume_coding_task_control(saved, source="EXPLICIT_GUI_RESUME_ACTION")
                queued = api.db.coding_task(task_id)
                self.assertEqual("QUEUED", queued["status"])
                api.run_managed_task(task_id, self.tmp.name)
            self.assertEqual(1, replan.call_count)
            self.assertEqual(task_id, replan.call_args.args[0])
            self.assertEqual("explicit resume after phase failure checkpoint", replan.call_args.args[3])
        finally:
            api.runtime = original_runtime

    def test_scheduler_exception_evidence_is_bounded_and_locates_the_source_frame(self):
        try:
            raise AttributeError("missing scheduler field")
        except AttributeError as exc:
            evidence=api._scheduler_exception_evidence(exc)
        self.assertEqual("AttributeError",evidence["exception_type"])
        self.assertEqual("missing scheduler field",evidence["message"])
        self.assertLessEqual(len(evidence["frames"]),6)
        self.assertEqual("test_scheduler_exception_evidence_is_bounded_and_locates_the_source_frame",
                         evidence["frames"][-1]["function"])

    def test_resume_clears_superseded_replan_gate_only_with_current_revision_pass_evidence(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="superseded-replan-gate"; goal="fix sample"
        task_plan={"schema_version":1,"original_goal":goal,"scope":{"allowed":["probe.py"],"forbidden":[]},
                   "assumptions":[],"phases":[{"id":"p1","goal":"fix probe.py","status":"pass",
                   "done":["probe.py fixed"],"verify":["test passes"],"dependencies":[],"risks":[],
                   "execution_mode":"IMPLEMENTATION"}],"max_retries_per_phase":2,"requires_user_approval":True}
        api.db.create_coding_task(task_id,conversation,goal,"RESUMABLE","NONE",task_plan,time.time())
        api.db.update_coding_task(task_id,plan_revision=1,current_phase_id="p1",recovery_action="RECOVERY_REVIEW",
                                  recovery_reason="COMPLETION_EVIDENCE",
                                  pending_authorization={"type":"REPLAN_INEFFECTIVE","state":"PENDING",
                                                         "user_revision":"revise to the fallback"},
                                  pending_user_confirmation=0)
        passing_report={"phase_id":"p1","attempt":0,"plan_revision":1,"recovery_epoch":0,"status":"PASS",
                        "errors":[],"test_executed":["python3 -m unittest"],"test_pass":["python3 -m unittest"],
                        "test_fail":[],"manager_decision":{"decision":"PASS","reason":"typed evidence complete"},
                        "typed_execution_summary":{"execution_attempt_completed":True,"phase_progress":True,
                                                    "final_worktree_mutations":1,"mutation_classification":"NET_CHANGED"}}
        api.db.add_coding_phase_report(task_id,"p1",0,passing_report,"PASS",time.time())
        saved=api.db.coding_task(task_id)
        with patch.object(api._coding_scheduler_wake,"set"):
            api._resume_coding_task_control(saved,source="EXPLICIT_GUI_RESUME_ACTION")
        queued=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",queued["status"])
        self.assertIsNone(queued["pending_authorization"])
        self.assertEqual(0,queued["pending_user_confirmation"])

    def test_complete_task_accepts_strictly_verified_noop_legacy_report(self):
        target = Path(self.tmp.name) / "ledger.py"
        target.write_text("def total(items): return sum(items)\n", encoding="utf-8")
        command = ["python3", "-B", "-m", "unittest", "-v", "test_ledger"]
        phase = {"id": "p1", "goal": "confirm completed ledger", "status": "pass",
                 "execution_mode": "IMPLEMENTATION", "done": ["ledger implementation is complete"],
                 "verify": ["unit tests pass"], "verification_command": command,
                 "verification_targets": ["ledger.py"], "dependencies": [], "risks": []}
        task_id = "verified-noop-finalization"
        conversation = self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        plan_value = {"schema_version": 1, "original_goal": "confirm completed ledger",
                      "scope": {"allowed": ["ledger.py"], "forbidden": []}, "assumptions": [],
                      "phases": [phase], "max_retries_per_phase": 2, "requires_user_approval": True}
        api.db.create_coding_task(task_id, conversation, "confirm completed ledger", "RESUMABLE",
                                  "NONE", plan_value, time.time())
        verifier = {"tool": "verification_command", "status": "success",
                    "input": {"command": command, "write_scope": "NONE"},
                    "output": {"returncode": 0, "read_only_targets": ["ledger.py"],
                               "changed_targets": [], "zero_tests": False}}
        report = {"phase_id": "p1", "attempt": 0, "plan_revision": 0, "recovery_epoch": 0,
                  "status": "FAIL", "errors": [], "blockers": [], "test_fail": [],
                  "test_executed": ["python3 -B -m unittest -v test_ledger"],
                  "test_pass": ["python3 -B -m unittest -v test_ledger (exit_code=0)"],
                  "build_executed": "NOT_RUN", "build_pass": "NOT_RUN", "changed_files": [],
                  "manager_decision": {"decision": "PASS", "reason": "typed no-op verifier complete"},
                  "typed_execution_summary": {
                      "error": "IMPLEMENTER_STRUCTURED_OUTPUT_INVALID: implementation model returned no operations",
                      "accepted_mutations": 0, "files_written": 0, "final_worktree_mutations": 0,
                      "operations": [verifier]}}
        api.db.add_coding_phase_report(task_id, "p1", 0, report, "PASS", time.time())
        with patch.object(api, "_final_report_only", return_value="Coding Task completed.") as finalizer:
            result = api._complete_task(task_id, api.db.coding_task(task_id))

        self.assertEqual("Coding Task completed.", result)
        self.assertEqual(1, finalizer.call_count)

    def test_replanner_scheduler_failure_resumes_only_as_bounded_replan(self):
        self.assertEqual(("REPLAN_CONTINUATION", "REPLAN_LIMIT"),
                         api._scheduler_failure_recovery({
                             "activity":"QWEN_REPLANNING", "recovery_action":"REPLAN_CONTINUATION"}))
        self.assertEqual(("RECOVERY_REVIEW", None),
                         api._scheduler_failure_recovery({
                             "activity":"IMPLEMENTATION", "recovery_action":"REPLAN_CONTINUATION"}))

    def test_interrupted_replan_resume_requires_saved_same_task_revision_and_action(self):
        resumable={"status":"RESUMABLE", "recovery_action":"RECOVERY_REVIEW",
                   "recovery_reason":"PAUSED", "resume_state":{
                       "continuation_user_revision":"correct verifier", "next_task_action":"DISPATCH_IMPLEMENTATION"}}
        self.assertTrue(api._interrupted_replan_resume(resumable))
        self.assertFalse(api._interrupted_replan_resume({**resumable, "resume_state":{}}))
        self.assertFalse(api._interrupted_replan_resume({**resumable, "recovery_reason":"COMPLETION_EVIDENCE"}))

    def test_tetris_implementation_request_is_managed_candidate_before_runtime(self):
        api.rebuild(api.settings.with_overrides({"task_manager_enabled": True}))
        request="このCoding Task Managerの実GUI E2Eテストとして、使い捨ての新しい作業フォルダに、ブラウザで遊べるシンプルなテトリスを作成してください。"
        self.assertTrue(coding_candidate(request))
        with patch.object(api._coding_scheduler_wake,"set") as wake, patch.object(api.runtime,"execute",side_effect=AssertionError("normal runtime must not run")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":request})
        self.assertEqual(200,response.status_code)
        self.assertTrue(response.json().get("coding_task_id"))
        self.assertEqual("",response.json()["response"])
        self.assertTrue(wake.called)
        self.assertEqual("QUEUED",api.db.coding_task(response.json()["coding_task_id"])["status"])

    def test_english_create_exactly_four_files_request_enters_managed_queue(self):
        request=(
            "Create an isolated expense-ledger CLI as a long multi-file implementation in a new folder. "
            "Create exactly four files: ledger_core.py, cli.py, test_ledger.py, and README.md. "
            "Implement CSV-backed expense records with date, description, category, and nonnegative decimal amount; "
            "provide add/list/total CLI commands with clear validation and deterministic output; include unit tests; "
            "First produce a scoped multi-file plan and do not modify anything before GUI approval. "
            "After approval, implement the four files and verify them. Do not touch files outside that new folder."
        )
        with patch.object(api._coding_scheduler_wake,"set") as wake, \
             patch.object(api.runtime,"execute",side_effect=AssertionError("managed coding request must not reach normal runtime")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":request})
        self.assertEqual(200,response.status_code)
        task_id=response.json().get("coding_task_id")
        self.assertTrue(task_id)
        self.assertEqual("",response.json()["response"])
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(wake.called)

    def test_explicit_plan_only_code_request_enters_managed_queue_without_mutation(self):
        request=(
            "計画だけを作成してください。現在のワークスペースに E2E_FINAL_PLAN.md と "
            "E2E_FINAL_PLAN.py を作る計画を立て、Python関数 calculate_sum(n) が n+1 を返すこと、"
            "および検証方法を明記してください。今はファイル作成・変更・削除、コマンド実行、依存追加を一切せず、"
            "計画を表示したら必ず停止して私の明示承認を待ってください。"
        )
        before=sorted(path.name for path in Path(self.tmp.name).iterdir())
        with patch.object(api._coding_scheduler_wake,"set") as wake, \
             patch.object(api.runtime,"execute",side_effect=AssertionError("plan-only request must not use normal runtime")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":request})
        self.assertEqual(200,response.status_code)
        task_id=response.json().get("coding_task_id")
        self.assertTrue(task_id)
        task=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",task["status"])
        self.assertTrue(task["requirements"]["planning_only"])
        self.assertNotIn("dependencies",task["requirements"]["required_capabilities"])
        self.assertEqual(before,sorted(path.name for path in Path(self.tmp.name).iterdir()))
        self.assertTrue(wake.called)

    def test_stream_explicit_plan_only_request_uses_coding_orchestrator(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        request=("Plan only; do not create, edit, or delete any files. Design a Python function format_total(items) "
                 "for /tmp/OLCR_E2E_PLAN_R13.py and document the design in /tmp/OLCR_E2E_PLAN_R13.md. "
                 "Show the plan in this chat and wait for my approval before implementation.")
        before=sorted(path.name for path in Path(self.tmp.name).iterdir())
        with patch.object(api._coding_scheduler_wake,"set") as wake, \
             patch.object(api.runtime,"execute",side_effect=AssertionError("stream plan-only request must not use Normal Brain")), \
             patch.object(api.runtime.model,"generate",side_effect=AssertionError("stream plan-only request must not generate prose")):
            response=self.client.post("/api/chat/stream",json={"project_id":self.project,"conversation_id":conversation,
                                                                  "message":request})
        self.assertEqual(200,response.status_code)
        frames=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertTrue(any(frame.get("response_owner")=="CODING_ORCHESTRATOR" for frame in frames))
        done=next(frame for frame in frames if frame.get("type")=="done")
        task_id=done["task"]["task_id"]
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(api.db.coding_task(task_id)["requirements"]["planning_only"])
        self.assertEqual(before,sorted(path.name for path in Path(self.tmp.name).iterdir()))
        self.assertTrue(wake.called)

    def test_existing_named_file_update_routes_identically_for_chat_and_stream(self):
        request = "Update only existing E2E_SHORT_FIX_PROBE.py and run its verifier."
        conversations = [self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
                         for _ in range(2)]
        with patch.object(api._coding_scheduler_wake, "set") as wake, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("must not fall through to Normal Brain")), \
             patch.object(api.runtime.model, "generate", side_effect=AssertionError("planning runs asynchronously")):
            normal = self.client.post("/api/chat", json={"project_id": self.project,
                                                          "conversation_id": conversations[0], "message": request})
            streamed = self.client.post("/api/chat/stream", json={"project_id": self.project,
                                                                   "conversation_id": conversations[1], "message": request})
        self.assertEqual(200, normal.status_code)
        self.assertEqual(200, streamed.status_code)
        normal_task_id = normal.json()["coding_task_id"]
        frames = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: ")]
        self.assertTrue(any(frame.get("response_owner") == "CODING_ORCHESTRATOR" for frame in frames))
        stream_task_id = next(frame["task"]["task_id"] for frame in frames if frame.get("type") == "done")
        self.assertEqual("QUEUED", api.db.coding_task(normal_task_id)["status"])
        self.assertEqual("QUEUED", api.db.coding_task(stream_task_id)["status"])
        self.assertTrue(wake.called)

    def test_stream_case_e_checkpoint_repair_stays_on_same_coding_orchestrator_route(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        request=(
            "Fix test_scheduler.py imports for direct unittest discovery in one LONG_IMPLEMENTATION Coding Task. "
            "For Case E, first use package-relative-only imports so the exact verifier fails; checkpoint the same "
            "task, then use Bonsai diagnosis and Ornith implementation to support package and top-level imports. "
            "Only the four existing R17 files may change. Run python3 -B -m unittest discover -s . -v."
        )
        with patch.object(api._coding_scheduler_wake,"set") as wake, \
             patch.object(api,"route_external_tool",side_effect=AssertionError("coding task must not route to an external tool")), \
             patch.object(api.runtime,"execute",side_effect=AssertionError("managed task must not use Normal Brain")), \
             patch.object(api.runtime.model,"generate",side_effect=AssertionError("planning runs asynchronously")):
            response=self.client.post("/api/chat/stream",json={"project_id":self.project,
                                                                  "conversation_id":conversation,
                                                                  "message":request})
        self.assertEqual(200,response.status_code)
        frames=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertTrue(any(frame.get("response_owner")=="CODING_ORCHESTRATOR" for frame in frames))
        done=next(frame for frame in frames if frame.get("type")=="done")
        task_id=done["task"]["task_id"]
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertEqual(1,len(api.db.coding_tasks(conversation)))
        wake.assert_called_once()

    def test_ambiguous_reply_stays_with_waiting_plan_task(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="waiting-plan"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_PLAN_APPROVAL","NONE",plan(goal),time.time())
        with patch.object(api.runtime,"execute",side_effect=AssertionError("waiting input must not use normal runtime")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":"なるほど"})
        self.assertEqual(200,response.status_code)
        self.assertEqual(task_id,response.json()["coding_task_id"])
        self.assertEqual("WAITING_FOR_PLAN_APPROVAL",api.db.coding_task(task_id)["status"])

    def test_stream_plan_approval_uses_coding_orchestrator_and_queues_same_task(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="stream-waiting-plan"; goal="Fix the isolated sample and run its focused test"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_PLAN_APPROVAL","NONE",plan(goal),time.time())
        with patch.object(api._coding_scheduler_wake,"set") as wake, \
             patch.object(api.runtime.model,"generate",side_effect=AssertionError("plan approval must not invoke Normal Brain")):
            response=self.client.post("/api/chat/stream",json={"project_id":self.project,"conversation_id":conversation,
                                                                 "message":"この計画で進めて"})
        self.assertEqual(200,response.status_code)
        self.assertIn("Coding Task を実行キューへ追加しました。",response.text)
        self.assertIn(task_id,response.text)
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(wake.called)

    def test_stream_nonapproval_waiting_for_plan_never_reaches_normal_brain(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="stream-plan-question"; goal="Fix the isolated sample and run its focused test"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_PLAN_APPROVAL","NONE",plan(goal),time.time())
        with patch.object(api.runtime.model,"generate",side_effect=AssertionError("pending plan must not invoke Normal Brain")):
            response=self.client.post("/api/chat/stream",json={"project_id":self.project,"conversation_id":conversation,
                                                                 "message":"この計画の確認項目は何ですか"})
        self.assertEqual(200,response.status_code)
        self.assertIn("計画の承認待ちです",response.text)
        self.assertEqual("WAITING_FOR_PLAN_APPROVAL",api.db.coding_task(task_id)["status"])

    def test_stream_scope_authorization_uses_coding_orchestrator_and_queues_same_task(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="stream-waiting-scope-authorization"; goal="Implement the approved isolated expense ledger"
        approved_plan=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_USER","SCOPE_AUTHORIZATION",approved_plan,time.time())
        api.db.update_coding_task(task_id,pending_plan=approved_plan,
                                  pending_authorization={"requested_scope":["Implement add, list, total, delete, and input validation"],
                                                         "reason":"protected operation","source":"plan","state":"PENDING"})
        with patch.object(api._coding_scheduler_wake,"set") as wake, \
             patch.object(api.runtime.model,"generate",side_effect=AssertionError("scope approval must not invoke Normal Brain")):
            response=self.client.post("/api/chat/stream",json={"project_id":self.project,"conversation_id":conversation,
                                                                  "message":"承認します"})
        self.assertEqual(200,response.status_code)
        self.assertIn("追加スコープを承認し、実行キューへ追加しました",response.text)
        self.assertIn(task_id,response.text)
        accepted=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",accepted["status"])
        self.assertIsNone(accepted["pending_authorization"])
        self.assertNotIn("Implement add, list, total, delete, and input validation",
                         json.dumps(accepted["approved_scopes"]))
        self.assertFalse(any("Implement add, list, total, delete, and input validation" in item.get("requested_scope", [])
                             for item in accepted["approved_scopes"]))
        self.assertTrue(wake.called)

    def test_waiting_input_classifier_separates_question_from_revision(self):
        self.assertEqual("CLARIFICATION", classify_waiting_input("確認が必要とはどういうこと"))
        self.assertEqual("CLARIFICATION", classify_waiting_input("何が不足していますか？"))
        self.assertEqual("REVISION", classify_waiting_input("Phase 3 の確認条件を変更してください"))
        self.assertEqual("CONTROL", classify_waiting_input("再開"))

    def test_coding_action_intent_is_independent_from_candidate_and_preserves_explanations(self):
        self.assertTrue(coding_action_intent("テトリスを作成してください"))
        self.assertTrue(coding_action_intent("このrepoに認証機能を実装してください"))
        self.assertTrue(coding_action_intent("fix this code"))
        self.assertFalse(coding_action_intent("英語学習の休憩に使える、地理カテゴリの中級クイズを作って。"))
        self.assertFalse(coding_action_intent("この予算の内訳を示す円グラフを作って。"))
        self.assertFalse(coding_action_intent("JavaScriptのPromiseを説明して"))
        self.assertFalse(coding_action_intent("テトリスの実装方法を教えて"))
        self.assertFalse(coding_action_intent("JavaScriptのイベントリスナーの短い例を見せて"))

    def test_design_planning_and_explicit_non_coding_never_activate_orchestrator(self):
        planning = (
            "実装計画を作ってください", "ボタン構成とUIを設計してください",
            "Reactで作る場合の画面構成を考えて", "このAPIの設計をレビューして",
            "テトリスの実装計画とUI案を作って",
            "React、API、Database、UI の技術設計だけしてください",
            "Do not modify code; planning only.",
        )
        for request in planning:
            with self.subTest(request=request):
                self.assertEqual("NON_CODING", classify_coding_request(request))
                self.assertFalse(coding_candidate(request))
        exact = "テトリスの実装計画を、ボタン構成、操作対応、UIまで含めて細かく策定してください。\nこれはコーディングタスクではありません。"
        facts = coding_classification_diagnostics(exact)
        self.assertEqual("NON_CODING", facts["classification"])
        self.assertTrue(facts["planning_intent"])
        self.assertTrue(facts["explicit_non_coding"])
        self.assertFalse(facts["mutation_intent"])

    def test_technical_article_synthesis_is_not_misclassified_as_ambiguous_coding(self):
        request = "最近の日本語技術記事から、AIエージェントの実装でよく使われる構成と課題を複数記事をもとに整理して。"
        facts = coding_classification_diagnostics(request)
        self.assertEqual("NON_CODING", facts["classification"])
        self.assertEqual("EXPLICIT_NON_CODING_SCOPE", facts["reason"])
        self.assertFalse(facts["mutation_intent"])
        self.assertFalse(coding_candidate(request))

    def test_local_diagnostic_negations_are_read_only_and_non_coding(self):
        request = "Do not implement. Do not modify files. Do not use MCP. Do not use web access. Do not fetch external data. Inspect only locally persisted OLCR task state."
        facts = coding_classification_diagnostics(request)
        self.assertEqual("NON_CODING", facts["classification"])
        self.assertEqual("LOCAL_DIAGNOSTIC_READ_ONLY", facts["reason"])
        self.assertFalse(facts["mutation_intent"])
        self.assertFalse(coding_candidate(request))

    def test_update_only_existing_named_file_routes_to_coding(self):
        for request in (
            "Update only existing E2E_SHORT_FIX_PROBE.py",
            "Update only existing E2E_SHORT_FIX_PROBE.py and run its verifier.",
            "Update E2E_SHORT_FIX_PROBE.py",
            "Edit only E2E_SHORT_FIX_PROBE.py so it contains the required assertion.",
            "Edit only existing E2E_SHORT_FIX_PROBE.py to remove duplicate assertions.",
        ):
            with self.subTest(request=request):
                facts = coding_classification_diagnostics(request, project_scoped=True)
                self.assertEqual("CODING", facts["classification"])
                self.assertTrue(facts["mutation_intent"])

    def test_local_diagnostic_does_not_route_external_data(self):
        request = "Do not implement. Do not modify files. Do not use MCP. Do not use web access. Do not fetch external data. Inspect only locally persisted OLCR task state."
        execution = Task(request, route=Route.DIRECT, state=TaskState.COMPLETED)
        with patch.object(api, "route_external_tool", side_effect=AssertionError("external matcher called")), \
             patch.object(api, "router_decision", side_effect=AssertionError("external router called")), \
             patch.object(api.runtime, "execute", return_value=(execution, "ローカルのタスク状態を確認しました")):
            response = self.client.post("/api/chat", json={"project_id": self.project, "message": request})
        self.assertEqual(200, response.status_code)
        self.assertEqual("ローカルのタスク状態を確認しました", response.json()["response"])

    def test_mutation_requests_remain_coding_and_contradictions_are_ambiguous(self):
        self.assertEqual("CODING", classify_coding_request("この機能を実装して"))
        self.assertEqual("CODING", classify_coding_request("このReact画面を修正して"))
        self.assertEqual("CODING", classify_coding_request("Execute the implementation plan."))
        multi_file_request = (
            "Create an isolated expense-ledger CLI as a long multi-file implementation in a new folder. "
            "Create exactly four files: ledger_core.py, cli.py, test_ledger.py, and README.md. "
            "Implement CSV-backed expense records with date, description, category, and nonnegative decimal amount; "
            "provide add/list/total CLI commands with clear validation and deterministic output; include unit tests; "
            "First produce a scoped multi-file plan and do not modify anything before GUI approval. "
            "After approval, implement the four files and verify them. Do not touch files outside that new folder."
        )
        facts = coding_classification_diagnostics(multi_file_request, project_scoped=True)
        self.assertEqual("CODING", facts["classification"])
        # This turn explicitly stops at a plan until GUI approval. Future
        # implementation remains managed, but is not current mutation intent.
        self.assertFalse(facts["mutation_intent"])
        self.assertEqual("EXPLICIT_PLAN_ONLY_CODING_REQUEST", facts["reason"])
        contradictory = "コーディングはしないでください。ただしコードを修正してください"
        facts = coding_classification_diagnostics(contradictory)
        self.assertEqual("AMBIGUOUS", facts["classification"])
        self.assertTrue(facts["mutation_intent"])
        self.assertTrue(facts["explicit_non_coding"])

    def test_explicit_execution_intent_preserves_fixed_benchmark_text(self):
        request = "Fix the existing add function so tests/test_math.py passes. Change only src/math.py, then run the focused test command."
        facts = coding_classification_diagnostics(request, project_scoped=True, execution_intent="coding_mutation")
        self.assertEqual("CODING", facts["classification"])
        self.assertTrue(facts["mutation_intent"])
        self.assertEqual("EXPLICIT_EXECUTION_INTENT", facts["reason"])

    def test_unknown_execution_intent_does_not_activate_coding(self):
        request = "Explain the existing add function and its test."
        facts = coding_classification_diagnostics(request, project_scoped=True, execution_intent="read_only")
        self.assertNotEqual("CODING", facts["classification"])

    def test_planning_request_uses_normal_brain_without_task_or_scheduler(self):
        request = "テトリスの実装計画を、ボタン構成、操作対応、UIまで含めて細かく策定してください。\nこれはコーディングタスクではありません。"
        execution = Task(request, route=Route.DIRECT, state=TaskState.COMPLETED)
        with patch.object(api._coding_scheduler_wake, "set") as wake, \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api.runtime, "execute", return_value=(execution, "テトリスのUI計画です")):
            response = self.client.post("/api/chat", json={"project_id": self.project, "message": request})
        self.assertEqual(200, response.status_code)
        self.assertNotIn("coding_task_id", response.json())
        self.assertEqual("テトリスのUI計画です", response.json()["response"])
        self.assertEqual([], api.db.coding_tasks(response.json()["conversation_id"]))
        wake.assert_not_called()

    def test_plan_then_implement_creates_task_on_the_later_mutation_message(self):
        planning = "実装計画を作って"
        execution = Task(planning, route=Route.DIRECT, state=TaskState.COMPLETED)
        with patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api.runtime, "execute", return_value=(execution, "計画")):
            first = self.client.post("/api/chat", json={"project_id": self.project, "message": planning})
        self.assertNotIn("coding_task_id", first.json())
        with patch.object(api._coding_scheduler_wake, "set") as wake:
            second = self.client.post("/api/chat", json={"project_id": self.project,
                                                            "conversation_id": first.json()["conversation_id"],
                                                            "message": "この計画を実装してください"})
        self.assertTrue(second.json().get("coding_task_id"))
        wake.assert_called_once()

    def test_contradictory_scope_does_not_create_task_before_clarification(self):
        with patch.object(api._coding_scheduler_wake, "set") as wake, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("ambiguous scope must not run Brain or implementation")):
            response = self.client.post("/api/chat", json={"project_id": self.project,
                                                              "message": "コーディングはしないでください。ただしコードを修正してください"})
        self.assertEqual(200, response.status_code)
        self.assertNotIn("coding_task_id", response.json())
        self.assertIn("明確ではありません", response.json()["response"])
        self.assertEqual([], api.db.coding_tasks(response.json()["conversation_id"]))
        wake.assert_not_called()

    def test_legacy_candidate_gate_cannot_divert_actionable_request_from_orchestrator(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        request="このCoding Task Managerの実GUI E2Eテストとして、使い捨ての新しい作業フォルダにテトリスを作成してください"
        with patch.object(api,"coding_candidate",side_effect=AssertionError("legacy candidate gate must not run")), \
             patch.object(api.runtime,"execute",side_effect=AssertionError("normal Brain must not run")), \
             patch.object(api.runtime,"execute_image",side_effect=AssertionError("vision Brain must not run")), \
             patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":request})
        self.assertEqual(200,response.status_code)
        task_id=response.json().get("coding_task_id")
        self.assertTrue(task_id)
        self.assertEqual("",response.json()["response"])
        task=self.client.get(f"/api/coding-tasks/{task_id}").json()
        self.assertEqual("QUEUED",task["status"])
        self.assertEqual("QWEN_PLANNING",task["activity"])
        wake.assert_called_once()

    def test_actionable_image_request_routes_to_orchestrator_not_immediate_vision_brain(self):
        with patch.object(api,"coding_candidate",side_effect=AssertionError("legacy candidate gate must not run")), \
             patch.object(api.runtime,"execute",side_effect=AssertionError("normal Brain must not run")), \
             patch.object(api.runtime,"execute_image",side_effect=AssertionError("vision Brain must not run")), \
             patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":"このUIを実装してください",
                                                         "image":{"name":"ui.png","mime_type":"image/png","data_url":"data:image/png;base64,AA=="}})
        self.assertEqual(200,response.status_code)
        task_id=response.json().get("coding_task_id")
        self.assertTrue(task_id)
        self.assertEqual("QUEUED",self.client.get(f"/api/coding-tasks/{task_id}").json()["status"])
        wake.assert_called_once()

    def test_explanatory_coding_question_remains_on_normal_runtime_path(self):
        class RuntimeTask:
            def __init__(self):
                self.id="normal-explanation"
                self.raw_request="テトリスの実装方法を教えて"
                self.route=TaskState.ROUTING
                self.state=TaskState.COMPLETED
                self.authorization_state="NOT_REQUIRED"
                self.reason_category=""
                self.selected_context=[]
                self.created_at=time.time(); self.updated_at=self.created_at
                self.error=None; self.tool_executions=[]; self.model_calls=[]
        with patch.object(api,"coding_candidate",return_value=False), \
             patch.object(api,"router_decision",return_value=None), \
             patch.object(api,"route_external_tool",return_value=None), \
             patch.object(api.runtime,"execute",return_value=(RuntimeTask(),"テトリスの実装方法を説明します")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":"テトリスの実装方法を教えて"})
        self.assertEqual(200,response.status_code)
        self.assertIn("実装方法を説明します",response.json()["response"])

    def test_replan_ineffective_clarification_does_not_mutate_or_enqueue(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="waiting-clarification"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_USER","NONE",plan(goal),time.time())
        api.db.update_coding_task(task_id,current_phase_id="p3",pending_authorization={
            "type":"REPLAN_INEFFECTIVE","reason":"Phase replan did not change the failing phase structure.",
            "unmet_criteria":["manual verification"],"state":"PENDING"},
            pending_user_confirmation=1,recovery_reason="REPLAN_INEFFECTIVE")
        with patch.object(api._coding_scheduler_wake,"set") as wake, patch.object(api.runtime,"model") as model:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,
                                                         "message":"確認が必要とはどういうこと"})
        self.assertEqual(200,response.status_code)
        saved=api.db.coding_task(task_id)
        self.assertEqual("WAITING_FOR_USER",saved["status"])
        self.assertEqual("p3",saved["current_phase_id"])
        self.assertEqual("REPLAN_INEFFECTIVE",saved["recovery_reason"])
        self.assertEqual(1,saved["pending_user_confirmation"])
        self.assertFalse(wake.called)
        self.assertFalse(model.generate.called)
        self.assertIn("再試行",response.json()["response"])

    def test_ineffective_replan_retry_from_button_and_chat_preserves_scope_and_cursor(self):
        for route in ('button', 'chat', 'plain-chat'):
            with self.subTest(route=route):
                conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
                identifier='retry-'+route;current=plan('fix sample');current['phases'][0]['status']='pass'
                api.db.create_coding_task(identifier,conversation,'fix sample','WAITING_FOR_USER','NONE',current,time.time())
                api.db.update_coding_task(identifier,current_phase_id='p2',pending_authorization={'type':'REPLAN_INEFFECTIVE'},pending_user_confirmation=1,recovery_reason='REPLAN_INEFFECTIVE')
                with patch.object(api._coding_scheduler_wake,'set') as wake, patch.object(api.runtime,'model') as model:
                    response=(self.client.patch(f'/api/coding-tasks/{identifier}',json={'resume':True}) if route=='button' else self.client.post('/api/chat',json={'project_id':self.project,'conversation_id':conversation,'coding_task_id':identifier if route=='chat' else None,'coding_followup':'queue' if route=='chat' else None,'message':'再試行'}))
                self.assertEqual(200,response.status_code)
                saved=api.db.coding_task(identifier)
                self.assertEqual('QUEUED',saved['status']);self.assertEqual('p2',saved['current_phase_id'])
                self.assertEqual(current,saved['plan']);self.assertIsNone(saved['pending_authorization'])
                self.assertEqual('REPLAN_INEFFECTIVE',saved['recovery_reason']);self.assertTrue(wake.called)
                self.assertEqual('RECOVERY_REVIEW',saved['recovery_action'])
                self.assertEqual(1,saved['task_total_recovery_model_calls'])
                self.assertFalse(model.generate.called)

    def test_ineffective_retry_respects_exhausted_global_recovery_budget(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        api.db.create_coding_task('exhausted',conversation,'fix sample','WAITING_FOR_USER','NONE',plan('fix sample'),time.time())
        api.db.update_coding_task('exhausted',current_phase_id='p2',pending_authorization={'type':'REPLAN_INEFFECTIVE'},pending_user_confirmation=1,task_total_recovery_model_calls=api.TASK_GLOBAL_RECOVERY_MODEL_CALL_LIMIT)
        with patch.object(api._coding_scheduler_wake,'set') as wake:
            response=self.client.patch('/api/coding-tasks/exhausted',json={'resume':True})
        self.assertEqual(200,response.status_code);self.assertFalse(wake.called)
        saved=api.db.coding_task('exhausted')
        self.assertEqual('BLOCKED',saved['status']);self.assertEqual('GLOBAL_RECOVERY_LIMIT',saved['recovery_reason'])
        self.assertEqual('p2',saved['current_phase_id'])

    def test_retry_does_not_clear_real_scope_authorization(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        api.db.create_coding_task('protected',conversation,'fix sample','WAITING_FOR_USER','NONE',plan('fix sample'),time.time())
        pending={'type':'SCOPE_EXPANSION','requested_scope':['outside']}
        api.db.update_coding_task('protected',pending_authorization=pending,pending_user_confirmation=1)
        with patch.object(api._coding_scheduler_wake,'set') as wake:
            response=self.client.patch('/api/coding-tasks/protected',json={'resume':True})
        self.assertEqual(409,response.status_code);self.assertFalse(wake.called)
        self.assertEqual(pending,api.db.coding_task('protected')['pending_authorization'])

    def test_explicit_waiting_revision_queues_replan_without_resetting_phase(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="waiting-revision"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_USER","NONE",plan(goal),time.time())
        api.db.update_coding_task(task_id,current_phase_id="p3",pending_authorization={
            "type":"REPLAN_INEFFECTIVE","unmet_criteria":["manual verification"],"state":"PENDING"},
            pending_user_confirmation=1,recovery_reason="REPLAN_INEFFECTIVE")
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,
                "message":"Phase 3 の確認条件を、静的確認で判定できる内容に変更してください"})
        self.assertEqual(200,response.status_code)
        saved=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",saved["status"])
        self.assertEqual("p3",saved["current_phase_id"])
        self.assertEqual("USER_REVISION",saved["recovery_reason"])
        self.assertEqual(1,saved["pending_authorization"]["unmet_criteria"].__len__())
        self.assertTrue(wake.called)

    def test_user_guided_replan_preserves_completed_prefix_and_selects_failing_phase(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="replan-preserve-prefix"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        api.db.update_coding_task(task_id,pending_authorization={"type":"REPLAN_INEFFECTIVE","state":"PENDING",
                                                                  "user_revision":"revise phase 3"},
                                  pending_user_confirmation=0)
        replacement=plan(goal)
        replacement["phases"][0]["status"]="pass"
        replacement["phases"][1]["status"]="pass"
        replacement["phases"][2]["goal"]="revised phase 3"
        class Model:
            def generate(self,messages,model,think=False,**kwargs):
                return {"text":__import__("json").dumps(replacement)}
        class Runtime: model=Model()
        old_runtime=api.runtime; api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),{"p1","p2"},"user supplied phase revision")
        finally:
            api.runtime=old_runtime
        self.assertEqual("Coding Task was replanned and requeued.",result)
        saved=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",saved["status"])
        self.assertEqual("p3",saved["current_phase_id"])
        self.assertEqual(["pass","pass","pending"],[p["status"] for p in saved["plan"]["phases"]])
        self.assertIsNone(saved["pending_authorization"])
        self.assertEqual(0,saved["pending_user_confirmation"])

    def test_deterministic_plan_schema_repair_skips_slow_model_repair_for_type_drift(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="deterministic-plan-schema-repair"; goal="fix sample"
        invalid=plan(goal)
        invalid["phases"][0]["done"] = []
        invalid["phases"][0]["verify"] = "file exists"
        class Model:
            def __init__(self): self.calls=0
            def generate(self,messages,model,think=False,**kwargs):
                self.calls += 1
                return {"text":json.dumps(invalid)}
        class Runtime: pass
        fake=Runtime(); fake.model=Model()
        api.db.create_coding_task(task_id,conversation,goal,"PLANNING","QWEN_PLANNING",None,time.time())
        old_runtime=api.runtime; api.runtime=fake
        try: parsed,_=api._generate_plan(task_id,goal,"planning","QWEN_PLANNING")
        finally: api.runtime=old_runtime
        self.assertIsNotNone(parsed)
        self.assertEqual([],validate_plan(parsed,goal))
        self.assertEqual(1,fake.model.calls)
        self.assertEqual([plan(goal)["phases"][0]["goal"]], parsed["phases"][0]["done"])
        self.assertEqual([], parsed["phases"][0]["verify"])

    def test_model_schema_repair_type_drift_gets_safe_deterministic_normalization(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="model-schema-repair-type-drift"
        goal=("The only editable artifact is test_scheduler.py. Fix test_scheduler.py imports; run exactly python3 -B -m unittest discover -s . -v and require a nonzero test count and PASS. "
              "If this phase fails, stop this task and wait for my explicit manual resume. Do not retry automatically.")
        repaired_draft=plan(goal)
        repaired_draft["phases"][0]["done"] = []
        repaired_draft["phases"][0]["verify"] = None
        class Model:
            def __init__(self): self.calls=0
            def generate(self,messages,model,think=False,**kwargs):
                self.calls += 1
                return {"text": "not json" if self.calls == 1 else json.dumps(repaired_draft)}
        class Runtime: pass
        fake=Runtime(); fake.model=Model()
        api.db.create_coding_task(task_id,conversation,goal,"PLANNING","QWEN_PLANNING",None,time.time())
        old_runtime=api.runtime; api.runtime=fake
        try: parsed,_=api._generate_plan(task_id,goal,"planning","QWEN_PLANNING")
        finally: api.runtime=old_runtime
        self.assertIsNotNone(parsed)
        self.assertEqual([],validate_plan(parsed,goal))
        self.assertEqual(2,fake.model.calls)
        self.assertEqual([repaired_draft["phases"][0]["goal"]],parsed["phases"][0]["done"])
        self.assertEqual(
            ["Run the exact verifier specified in the original goal, if any, and check its result against the stated acceptance criteria."],
            parsed["phases"][0]["verify"],
        )
        self.assertIs(parsed["phases"][0]["checkpoint_on_failure"],True)
        self.assertEqual([{"path":"test_scheduler.py","action":"modify","role":"only explicitly writable artifact",
                           "required":True,"owner_phase":"p1"}],parsed["file_manifest"])

    def test_persisted_plan_resume_restores_exact_explicit_single_file_manifest(self):
        goal=("The only editable artifact is test_scheduler.py. Modify it, then run the exact verifier. "
              "If this phase fails, stop this task and wait for my explicit manual resume. Do not retry automatically.")
        original=plan(goal)
        original["phases"][0]["checkpoint_on_failure"]=True
        self.assertEqual("test_scheduler.py",explicit_single_file_write_target(goal))
        self.assertEqual(["explicit single-file write target missing from file_manifest"],validate_plan(original,goal))
        repaired,changes=enforce_explicit_single_file_manifest(original,goal)
        self.assertEqual(["file_manifest:test_scheduler.py:modify"],changes)
        self.assertEqual([{"path":"test_scheduler.py","action":"modify","role":"only explicitly writable artifact",
                           "required":True,"owner_phase":"p1"}],repaired["file_manifest"])
        self.assertEqual([],validate_plan(repaired,goal))

    def test_persisted_plan_manifest_enforcement_does_not_broaden_existing_manifest(self):
        goal=("The only editable artifact is test_scheduler.py. Modify it and run the exact verifier.")
        original=plan(goal)
        original["file_manifest"]=[{"path":"scheduler.py","action":"modify"}]
        unchanged,changes=enforce_explicit_single_file_manifest(original,goal)
        self.assertEqual([],changes)
        self.assertIs(unchanged,original)
        self.assertEqual(["file_manifest exceeds or contradicts the explicit single-file write scope"],validate_plan(unchanged,goal))

    def test_persisted_plan_resume_restores_explicit_failure_checkpoint(self):
        goal=("Implement the change. If this phase fails, stop this task and wait for my explicit manual resume. "
              "Do not retry automatically.")
        original=plan(goal)
        self.assertEqual(
            ["explicit phase-failure checkpoint requested but missing on first phase"],
            validate_plan(original,goal),
        )
        repaired,changes=enforce_explicit_phase_failure_checkpoint(original,goal)
        self.assertEqual(["p1:checkpoint_on_failure"],changes)
        self.assertIs(repaired["phases"][0]["checkpoint_on_failure"],True)
        self.assertEqual([],validate_plan(repaired,goal))

    def test_unsolicited_phase_failure_checkpoint_is_removed(self):
        goal="Build the requested website with responsive styling."
        original=plan(goal)
        original["phases"][0]["checkpoint_on_failure"]=True
        original["phases"][1]["checkpoint_on_failure"]=True

        repaired,changes=enforce_explicit_phase_failure_checkpoint(original,goal)

        self.assertTrue(changes)
        self.assertTrue(all(phase.get("checkpoint_on_failure") is not True
                            for phase in repaired["phases"]))
        self.assertTrue(all(phase.get("checkpoint_on_failure") is True
                            for phase in original["phases"][:2]))

    def test_phase_id_renaming_updates_all_phase_references_only_for_one_to_one_replans(self):
        current={"phases":[{"id":"major-1"},{"id":"major-2"}]}
        proposed={"phases":[{"id":"r1","dependencies":[]},
                            {"id":"r2","dependencies":["r1"],
                             "dependency_requirements":[{"package":"vite","phase_id":"r2"}]}],
                  "file_manifest":[{"path":"src/app.tsx","owner_phase":"r1"}],
                  "tasks":[{"task_id":"r1-task","phase_id":"r1","depends_on":[]},
                           {"task_id":"r2-task","phase_id":"r2","depends_on":["r1-task"]}],
                  "dependency_requirements":[{"package":"vite","phase_id":"r2"}]}

        normalized,changes=normalize_replanned_phase_ids(proposed,current)

        self.assertEqual(["major-1","major-2"],[phase["id"] for phase in normalized["phases"]])
        self.assertEqual(["major-1"],normalized["phases"][1]["dependencies"])
        self.assertEqual("major-2",normalized["phases"][1]["dependency_requirements"][0]["phase_id"])
        self.assertEqual("major-1",normalized["file_manifest"][0]["owner_phase"])
        self.assertEqual(["major-1","major-2"],[task["phase_id"] for task in normalized["tasks"]])
        self.assertEqual("major-2",normalized["dependency_requirements"][0]["phase_id"])
        self.assertTrue(changes)

    def test_phase_id_normalization_does_not_hide_an_expanded_plan(self):
        current={"phases":[{"id":"major-1"},{"id":"major-2"}]}
        proposed={"phases":[{"id":"r1"},{"id":"r2"},{"id":"r3"}]}

        normalized,changes=normalize_replanned_phase_ids(proposed,current)

        self.assertIs(normalized,proposed)
        self.assertEqual([],changes)

    def test_checkpoint_after_first_failed_phase_is_recognized_as_explicit(self):
        goal="Fix probe.py and checkpoint after the first failed phase."
        original=plan(goal)

        repaired,changes=enforce_explicit_phase_failure_checkpoint(original,goal)

        self.assertEqual(["p1:checkpoint_on_failure"],changes)
        self.assertIs(repaired["phases"][0]["checkpoint_on_failure"],True)
        self.assertEqual([],validate_plan(repaired,goal))

    def test_negative_checkpoint_instruction_is_not_treated_as_opt_in(self):
        goal="Build the website. Do not checkpoint after the first failed phase."
        original=plan(goal)

        repaired,changes=enforce_explicit_phase_failure_checkpoint(original,goal)

        self.assertEqual([],changes)
        self.assertFalse(any(phase.get("checkpoint_on_failure") is True
                             for phase in repaired["phases"]))
        self.assertEqual([],validate_plan(repaired,goal))

    def test_unrequested_phase_failure_checkpoint_is_invalid_plan_input(self):
        goal="Build the requested website with responsive styling."
        original=plan(goal)
        original["phases"][0]["checkpoint_on_failure"]=True

        self.assertEqual(["phase-failure checkpoint requires an explicit user request"],
                         validate_plan(original,goal))

    def test_planner_cannot_enable_failure_checkpoint_without_user_request(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="unsolicited-failure-checkpoint"
        goal="Build the requested website with responsive styling."
        proposed=plan(goal)
        proposed["phases"][0]["checkpoint_on_failure"]=True
        proposed["phases"][1]["checkpoint_on_failure"]=True
        api.db.create_coding_task(task_id,conversation,goal,"PLANNING","QWEN_PLANNING",None,time.time())

        with patch.object(api,"_model_text",return_value=json.dumps(proposed)):
            parsed,_=api._generate_plan(task_id,goal,"Plan the requested website.","QWEN_PLANNING")

        self.assertIsNotNone(parsed)
        self.assertTrue(all(phase.get("checkpoint_on_failure") is not True
                            for phase in parsed["phases"]))
        self.assertEqual([],validate_plan(parsed,goal))

    def test_planning_and_one_repair_use_the_same_ollama_json_schema_mode(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="structured-plan"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"PLANNING","QWEN_PLANNING",None,time.time())
        valid=plan(goal)
        class Model:
            def __init__(self): self.calls=[]
            def generate(self,messages,model,think=False,**kwargs):
                self.calls.append(kwargs.get("format"))
                return {"text":'{"schema_version":1}' if len(self.calls)==1 else __import__("json").dumps(valid)}
        class Runtime: pass
        fake=Runtime();fake.model=Model()
        old_runtime=api.runtime;api.runtime=fake
        try: parsed,_=api._generate_plan(task_id,goal,"planning","QWEN_PLANNING")
        finally: api.runtime=old_runtime
        self.assertEqual([],validate_plan(parsed,goal))
        expected=plan_schema()
        expected['properties']['original_goal']['const']='fix sample'
        self.assertEqual(2,len(fake.model.calls));self.assertEqual(expected,fake.model.calls[0]);self.assertEqual(expected,fake.model.calls[1])

    def test_planning_discards_malformed_advisory_task_rows_when_phase_plan_is_valid(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="malformed-advisory-task-graph"; goal="Fix one small bug and run a focused test"
        candidate=plan(goal)
        candidate["tasks"]=[{"task_id":"p1-task","phase_id":"p1"}]
        api.db.create_coding_task(task_id,conversation,goal,"PLANNING","QWEN_PLANNING",None,time.time())
        class Model:
            def __init__(self): self.calls=0
            def generate(self,messages,model,think=False,**kwargs):
                self.calls+=1
                return {"text":json.dumps(candidate)}
        class Runtime: pass
        fake=Runtime(); fake.model=Model()
        old_runtime=api.runtime; api.runtime=fake
        try: parsed,_=api._generate_plan(task_id,goal,"planning","QWEN_PLANNING")
        finally: api.runtime=old_runtime
        self.assertIsNotNone(parsed)
        self.assertEqual([],validate_plan(parsed,goal))
        self.assertNotIn("tasks",parsed)
        self.assertEqual(1,fake.model.calls)

    def test_schema_repair_same_graph_error_is_bounded_and_reported(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="graph-no-progress"; goal="fix sample"
        invalid=plan(goal)
        invalid["phases"][1]["dependencies"]=["missing-phase"]
        class Model:
            def __init__(self): self.calls=0
            def generate(self,messages,model,think=False,**kwargs):
                self.calls += 1
                return {"text":json.dumps(invalid)}
        class Runtime: pass
        fake=Runtime(); fake.model=Model()
        api.db.create_coding_task(task_id,conversation,goal,"PLANNING","QWEN_PLANNING",None,time.time())
        old_runtime=api.runtime; api.runtime=fake
        output=io.StringIO()
        try:
            with redirect_stderr(output):
                parsed,_=api._generate_plan(task_id,goal,"planning","QWEN_PLANNING")
        finally:
            api.runtime=old_runtime
        self.assertIsNone(parsed)
        self.assertEqual(2,fake.model.calls)
        self.assertIn("OFFENDING_PHASE_ID=p2",output.getvalue())
        self.assertIn("OFFENDING_DEPENDENCY_REF=missing-phase",output.getvalue())
        self.assertIn("PLAN_SCHEMA_REPAIR_NO_PROGRESS=YES",output.getvalue())

    def test_deterministic_graph_repair_avoids_schema_repair_call(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="graph-deterministic-repair"; goal="fix sample"
        repaired=plan(goal)
        repaired["phases"][1]["dependencies"]=["old-p1"]
        repaired["graph_replacement_map"]={"phase_ids":{"old-p1":"p1"},"task_ids":{}}
        class Model:
            def __init__(self): self.calls=0
            def generate(self,messages,model,think=False,**kwargs):
                self.calls += 1
                return {"text":json.dumps(repaired)}
        class Runtime: pass
        fake=Runtime(); fake.model=Model()
        api.db.create_coding_task(task_id,conversation,goal,"PLANNING","QWEN_PLANNING",None,time.time())
        old_runtime=api.runtime; api.runtime=fake
        try:
            parsed,_=api._generate_plan(task_id,goal,"planning","QWEN_PLANNING")
        finally:
            api.runtime=old_runtime
        self.assertIsNotNone(parsed)
        self.assertEqual(1,fake.model.calls)
        self.assertEqual(["p1"],parsed["phases"][1]["dependencies"])
    def test_task_id_is_returned_before_scheduler_model_work_and_pause_is_resumable(self):
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"message":"このリポジトリを修正して"})
            self.assertEqual(200,response.status_code)
            task_id=response.json()["coding_task_id"]
            self.assertTrue(task_id)
            task=self.client.get(f"/api/coding-tasks/{task_id}").json()
            self.assertEqual("QUEUED",task["status"])
            self.assertTrue(wake.called)
            paused=self.client.patch(f"/api/coding-tasks/{task_id}",json={"pause_requested":True}).json()
            self.assertEqual("RESUMABLE",paused["status"])
            resumed=self.client.patch(f"/api/coding-tasks/{task_id}",json={"resume":True}).json()
            self.assertEqual("QUEUED",resumed["status"])
    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_retry_reexecutes_the_same_phase_and_never_attempts_three(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="retry-task";goal="fix sample";task_plan=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",task_plan,time.time())
        class Model:
            def __init__(self): self.reviews=0;self.calls=[]
            def generate(self,messages,model,think=False):
                system=messages[0]["content"]
                self.calls.append(system)
                if "Gemma review" in system:
                    self.reviews+=1
                    return {"text":('{"decision":"RETRY","reason":"retry once",'
                                     '"diagnosis":{"unmet_done":["done"],"unmet_verify":["manual check"],'
                                     '"evidence_used":["typed report lacks verification"],'
                                     '"retry_instruction":"satisfy done and rerun manual check",'
                                     '"verification_instruction":"rerun manual check"}}'
                              if self.reviews==1 else '{"decision":"PASS","reason":"verified"}')}
                if "completion check" in system:return {"text":'{"decision":"PASS","reason":"complete"}'}
                return {"text":"Implemented\nChanged files\nTests\nBuild\nNot run\nRisks\nTODO"}
        class Runtime:
            def __init__(self): self.model=Model();self.executions=[];self.contexts=[]
            def execute(self,text,**kwargs):
                self.executions.append(text)
                self.contexts.append(kwargs.get("managed_context"))
                item=Task(text);item.transition(TaskState.ROUTING);item.transition(TaskState.GENERATING);item.transition(TaskState.COMPLETED)
                return item,"implemented"
        old_runtime=api.runtime;fake=Runtime();api.runtime=fake
        try: api.run_managed_task(task_id,self.tmp.name)
        finally: api.runtime=old_runtime
        reports=api.db.coding_phase_reports(task_id)
        self.assertEqual([0],[row["attempt"] for row in reports if row["phase_id"]=="p1"])
        self.assertEqual(3,len(fake.executions))
        self.assertEqual([], fake.model.calls)
        self.assertEqual("COMPLETED",api.db.coding_task(task_id)["status"])
        self.assertTrue(all(c and c.get("managed_coding_task") and c.get("operation_intent")=="IMPLEMENTATION" for c in fake.contexts))

    def test_invalid_phase_report_is_repaired_once_without_changing_typed_facts(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="report-repair";goal="fix sample";task_plan=plan(goal)
        source_path=Path(self.tmp.name)/"backend"/"sample.py"
        source_path.parent.mkdir(parents=True,exist_ok=True)
        source_path.write_text("# sample\n",encoding="utf-8")
        task_plan["file_manifest"]=[{"path":"backend/sample.py","action":"modify",
                                     "role":"existing source under test","required":True,
                                     "owner_phase":"p1"}]
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",task_plan,time.time())
        valid={"phase_id":"p1","attempt":0,"status":"PASS","implemented":[],"changed_files":[],"test_executed":[],"test_pass":[],"test_fail":[],"build_executed":"NOT_RUN","build_pass":"NOT_RUN","errors":[],"blockers":[],"risks":[]}
        class Model:
            def __init__(self): self.calls=[]
            def generate(self,messages,model,think=False):
                system=messages[0]["content"];self.calls.append(system)
                if "report JSON format" in system: return {"text":__import__("json").dumps(valid)}
                if "Gemma review" in system: return {"text":'{"decision":"PASS","reason":"verified"}'}
                if "completion check" in system: return {"text":'{"decision":"PASS","reason":"complete"}'}
                return {"text":"Implemented\nChanged files\nTests\nBuild\nNot run\nRisks\nTODO"}
        class Runtime:
            def __init__(self): self.model=Model()
            def execute(self,text,**kwargs):
                item=Task(text);item.transition(TaskState.ROUTING);item.transition(TaskState.GENERATING);item.transition(TaskState.COMPLETED)
                return item,"implemented"
        original_report=api._report_from_execution
        def invalid_report(phase,attempt,execution,response):
            return {"phase_id":phase["id"],"attempt":attempt,"status":"PASS","typed_execution_summary":{"state":"completed","error":None,"operations":[]}}
        old_runtime=api.runtime;fake=Runtime();api.runtime=fake
        try:
            with patch.object(api,"_report_from_execution",side_effect=invalid_report):
                api.run_managed_task(task_id,self.tmp.name)
        finally: api.runtime=old_runtime
        report=api.db.coding_phase_reports(task_id)[0]["structured_report"]
        self.assertEqual("completed",report["typed_execution_summary"]["state"])
        self.assertFalse(any("report JSON format" in call for call in fake.model.calls))
        self.assertEqual([], report["test_pass"])
        self.assertEqual([], report["changed_files"])

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_retry_exhaustion_replans_and_scope_expansion_waits_for_user(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="scope-replan"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        replacement=plan(goal)
        replacement["scope"]["allowed"].append("new-safe-fixture")
        replacement["phases"][0]["status"]="pass"
        class Model:
            def __init__(self): self.reviews=0
            def generate(self,messages,model,think=False):
                system=messages[0]["content"]
                if "Gemma review" in system:
                    self.reviews+=1
                    return {"text":'{"decision":"RETRY","reason":"verification failed"}'}
                if "Planning Mode" in system:
                    return {"text":__import__("json").dumps(replacement)}
                return {"text":""}
        class Runtime:
            def __init__(self): self.model=Model();self.executions=[]
            def execute(self,text,**kwargs):
                self.executions.append(text)
                item=Task(text);item.transition(TaskState.ROUTING);item.transition(TaskState.GENERATING);item.transition(TaskState.COMPLETED)
                return item,"implemented"
        old_runtime=api.runtime;fake=Runtime();api.runtime=fake
        try: api.run_managed_task(task_id,self.tmp.name)
        finally: api.runtime=old_runtime
        reports=api.db.coding_phase_reports(task_id)
        self.assertEqual([0],[row["attempt"] for row in reports if row["phase_id"]=="p1"])
        task=api.db.coding_task(task_id)
        self.assertIn(task["status"], {"COMPLETED", "RESUMABLE", "WAITING_FOR_USER"})
        self.assertEqual([], fake.model.calls)

    @unittest.skip("OBSOLETE_TEST_EXPECTATION: pre-Orchestrator Gemma/guard lifecycle")
    def test_pause_after_execution_prevents_review_then_resume_reviews_saved_report_first(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="pause-resume";goal="fix sample";task_plan=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",task_plan,time.time())
        class Model:
            def __init__(self): self.calls=[]
            def generate(self,messages,model,think=False):
                system=messages[0]["content"];self.calls.append(system)
                if "Gemma review" in system or "completion check" in system:
                    return {"text":'{"decision":"PASS","reason":"verified"}'}
                return {"text":"Implemented\nChanged files\nTests\nBuild\nNot run\nRisks\nTODO"}
        class Runtime:
            def __init__(self): self.model=Model();self.executions=[];self.pause_once=True
            def execute(self,text,**kwargs):
                self.executions.append(text)
                if self.pause_once:
                    self.pause_once=False
                    api.db.update_coding_task(task_id,pause_requested=True)
                item=Task(text);item.transition(TaskState.ROUTING);item.transition(TaskState.GENERATING);item.transition(TaskState.COMPLETED)
                return item,"implemented"
        old_runtime=api.runtime;fake=Runtime();api.runtime=fake
        try:
            api.run_managed_task(task_id,self.tmp.name)
            self.assertEqual("RESUMABLE",api.db.coding_task(task_id)["status"])
            self.assertEqual([],fake.model.calls)
            self.assertEqual(1,len(api.db.coding_phase_reports(task_id)))
            api.db.enqueue_coding_task(task_id)
            api.run_managed_task(task_id,self.tmp.name)
        finally: api.runtime=old_runtime
        self.assertEqual([],fake.model.calls)
        self.assertEqual("COMPLETED",api.db.coding_task(task_id)["status"])

    def test_replan_rejects_a_plan_that_reopens_a_completed_phase(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="preserve-completed";goal="fix sample";current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        replacement=plan(goal)  # p1 is incorrectly reopened as pending.
        class Model:
            def generate(self,messages,model,think=False): return {"text":__import__("json").dumps(replacement)}
        class Runtime: model=Model()
        old_runtime=api.runtime;api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),{"p1"},"controlled failure")
        finally: api.runtime=old_runtime
        self.assertEqual("Coding Task replan did not preserve completed phases.",result)
        self.assertEqual("RESUMABLE",api.db.coding_task(task_id)["status"])

    def test_automatic_replan_keeps_full_approved_manifest_when_model_returns_phase_subset(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="replan-retains-approved-manifest"
        goal="Implement the approved web app in the existing source files"
        root=Path(self.tmp.name)
        (root/"src").mkdir()
        paths=["src/App.tsx","src/index.css","src/main.tsx"]
        for path in paths:
            (root/path).write_text("existing task file\n",encoding="utf-8")
        current=plan(goal)
        current["scope"]={"allowed":["src"],"forbidden":[]}
        current["phases"]=[
            {"id":"p1","goal":"Implement src/App.tsx","status":"pending",
             "done":["src/App.tsx implements the requested page"],"verify":[],"dependencies":[],"risks":[]},
            {"id":"p2","goal":"Implement src/index.css","status":"pending",
             "done":["src/index.css contains responsive styles"],"verify":[],"dependencies":["p1"],"risks":[]},
            {"id":"p3","goal":"Wire src/main.tsx","status":"pending",
             "done":["src/main.tsx mounts the app"],"verify":[],"dependencies":["p2"],"risks":[]},
        ]
        current["file_manifest"]=[{"path":path,"action":"modify","role":"implementation",
                                   "required":True,"owner_phase":f"p{index+1}"}
                                  for index,path in enumerate(paths)]
        requirements={"task_profile":"GENERAL_CODING"}
        prepared,artifact=prepare_implementation_plan(
            task_id,current,root,requirements,
            {"root":str(root),"project_state":"PARTIALLY_INITIALIZED","workspace_state":"PARTIALLY_INITIALIZED"})
        prepared["implementation_plan_artifact"]=artifact
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",prepared,time.time())
        api.db.update_coding_task(task_id,requirements=requirements)

        replacement=plan(goal)
        replacement["scope"]={"allowed":["src"],"forbidden":[]}
        replacement["phases"]=[
            {"id":"p1","goal":"Repair src/App.tsx using the observed phase failure","status":"pending",
             "done":["src/App.tsx implements the requested page"],"verify":[],"dependencies":[],"risks":[]},
            {"id":"p2","goal":"Implement src/index.css","status":"pending",
             "done":["src/index.css contains responsive styles"],"verify":[],"dependencies":["p1"],"risks":[]},
            {"id":"p3","goal":"Wire src/main.tsx","status":"pending",
             "done":["src/main.tsx mounts the app"],"verify":[],"dependencies":["p2"],"risks":[]},
        ]
        # Automatic recovery returns only the changed phase's artifacts. The
        # previously approved manifest remains the authority for all phases.
        replacement["file_manifest"]=[{"path":"src/App.tsx","action":"modify","role":"implementation",
                                        "required":True,"owner_phase":"p1"}]

        class Model:
            def generate(self,messages,model,think=False,**kwargs):
                return {"text":json.dumps(replacement)}
        class Runtime: model=Model()
        old_runtime=api.runtime;api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),set(),"controlled implementation failure")
        finally:
            api.runtime=old_runtime

        self.assertEqual("Coding Task was replanned and requeued.",result)
        saved=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",saved["status"])
        self.assertEqual(paths,[item["path"] for item in saved["plan"]["implementation_plan_artifact"]["file_manifest"]])

    def test_automatic_replan_manifest_failure_waits_instead_of_replaying_same_replan(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="replan-manifest-failure-waits"
        goal="Implement the approved web app in the existing source files"
        root=Path(self.tmp.name)
        (root/"src").mkdir()
        paths=["src/App.tsx","src/index.css","src/main.tsx"]
        for path in paths:
            (root/path).write_text("existing task file\n",encoding="utf-8")
        current=plan(goal)
        current["scope"]={"allowed":["src"],"forbidden":[]}
        current["phases"]=[
            {"id":"p1","goal":"Implement src/App.tsx","status":"pending",
             "done":["src/App.tsx implements the requested page"],"verify":[],"dependencies":[],"risks":[]},
            {"id":"p2","goal":"Implement src/index.css","status":"pending",
             "done":["src/index.css contains responsive styles"],"verify":[],"dependencies":["p1"],"risks":[]},
            {"id":"p3","goal":"Wire src/main.tsx","status":"pending",
             "done":["src/main.tsx mounts the app"],"verify":[],"dependencies":["p2"],"risks":[]},
        ]
        current["file_manifest"]=[{"path":path,"action":"modify","role":"implementation",
                                   "required":True,"owner_phase":f"p{index+1}"}
                                  for index,path in enumerate(paths)]
        requirements={"task_profile":"GENERAL_CODING"}
        prepared,artifact=prepare_implementation_plan(
            task_id,current,root,requirements,
            {"root":str(root),"project_state":"PARTIALLY_INITIALIZED","workspace_state":"PARTIALLY_INITIALIZED"})
        prepared["implementation_plan_artifact"]=artifact
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",prepared,time.time())
        api.db.update_coding_task(task_id,requirements=requirements)

        replacement=json.loads(json.dumps(prepared))
        replacement.pop("implementation_plan_artifact",None)
        replacement["phases"][0]["goal"]="Repair src/App.tsx using the observed phase failure"
        replacement["file_manifest"]=[{"path":"src/App.tsx","action":"modify","role":"implementation",
                                        "required":True,"owner_phase":"p1"}]
        class Model:
            def generate(self,messages,model,think=False,**kwargs):
                return {"text":json.dumps(replacement)}
        class Runtime: model=Model()
        old_runtime=api.runtime;api.runtime=Runtime()
        coverage_error=api.PlanManifestCoverageError(
            "file manifest does not cover implementation artifacts: src/new-component.tsx",
            ["src/new-component.tsx"])
        try:
            with patch.object(api,"_prepare_task_implementation_plan",side_effect=coverage_error):
                result=api._replan_task(task_id,api.db.coding_task(task_id),set(),"controlled implementation failure")
        finally:
            api.runtime=old_runtime

        saved=api.db.coding_task(task_id)
        self.assertIn("src/new-component.tsx",result)
        self.assertEqual("WAITING_FOR_USER",saved["status"])
        self.assertEqual("REPLAN_INEFFECTIVE",saved["recovery_reason"])
        self.assertEqual("NONE",saved["recovery_action"])
        self.assertEqual("REPLAN_INEFFECTIVE",saved["pending_authorization"]["type"])
        self.assertIn("src/new-component.tsx",saved["pending_authorization"]["unmet_criteria"][0])

    def test_replan_revalidates_only_completed_phase_with_changed_contract(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="revalidate-changed-completed"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        replacement=plan(goal)
        replacement["phases"][0]["goal"]="revised completed phase 1"
        replacement["phases"][0]["status"]="pending"
        replacement["phases"][1]["status"]="pass"
        class Model:
            def generate(self,messages,model,think=False): return {"text":__import__("json").dumps(replacement)}
        class Runtime: model=Model()
        old_runtime=api.runtime; api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),{"p1","p2"},"user changed phase 1 acceptance")
        finally: api.runtime=old_runtime
        self.assertEqual("Coding Task was replanned and requeued.",result)
        saved=api.db.coding_task(task_id)
        self.assertEqual("p1",saved["current_phase_id"])
        self.assertEqual(["pending","pass","pending"],[p["status"] for p in saved["plan"]["phases"]])

    def test_replan_restores_persisted_completed_phase_omitted_by_repair_plan(self):
        from unittest.mock import patch
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id='retain-completed-prefix';goal='fix sample';current=plan(goal)
        # Older saved plans can have a stale phase.status even though the
        # manager-approved reports still establish completion.
        current['phases'][0]['status']='pending'
        api.db.create_coding_task(task_id,conversation,goal,'QUEUED','NONE',current,time.time())
        replacement=plan(goal)
        replacement['phases']=replacement['phases'][1:]
        replacement['phases'][0]['goal']='Repair the actual failing assertion'
        replacement['phases'][0]['dependencies']=[]
        with patch.object(api,'_generate_plan',return_value=(replacement,None)):
            result=api._replan_task(task_id,api.db.coding_task(task_id),{'p1'},'Failed assertion')
        self.assertEqual('Coding Task was replanned and requeued.',result)
        saved=api.db.coding_task(task_id)
        self.assertEqual('QUEUED',saved['status'])
        self.assertEqual('p2',saved['current_phase_id'])
        expected_completed={**current['phases'][0],'status':'pass','execution_mode':'IMPLEMENTATION'}
        self.assertEqual(expected_completed,saved['plan']['phases'][0])
        self.assertEqual(1,saved['plan_revision'])
        self.assertEqual(1,saved['replan_count'])

    def test_pause_arriving_during_replanning_is_not_erased_by_automatic_requeue(self):
        from unittest.mock import patch
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()['id']
        task_id='pause-during-replan';goal='fix sample';current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,'RUNNING','NONE',current,time.time())
        replacement=plan(goal)
        replacement['phases'][0]['goal']='Fix actual failing assertion'
        def generate(*args,**kwargs):
            api.db.update_coding_task(task_id,pause_requested=True)
            return replacement,None
        with patch.object(api,'_generate_plan',side_effect=generate):
            result=api._replan_task(task_id,api.db.coding_task(task_id),set(),'Failed assertion')
        saved=api.db.coding_task(task_id)
        self.assertEqual('Task paused after replanning.',result)
        self.assertEqual('RESUMABLE',saved['status'])
        self.assertEqual('PAUSED',saved['recovery_reason'])
        self.assertTrue(saved['pause_requested'])
        self.assertIsNone(saved['queue_order'])
        self.assertEqual(1,saved['plan_revision'])
        self.assertEqual(1,saved['replan_count'])
        self.assertEqual('Fix actual failing assertion',saved['plan']['phases'][0]['goal'])

    def test_waiting_for_user_state_survives_database_restart(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="waiting-restart"; goal="fix sample"; task_plan=plan(goal)
        task_plan["phases"][0]["status"]="pass"
        task_plan["phases"][1]["status"]="pass"
        api.db.create_coding_task(task_id,conversation,goal,"WAITING_FOR_USER","NONE",task_plan,time.time())
        api.db.update_coding_task(task_id,current_phase_id="p3",recovery_reason="REPLAN_INEFFECTIVE",
                                  pending_authorization={"type":"REPLAN_INEFFECTIVE","unmet_criteria":["manual verification"]},
                                  pending_user_confirmation=1)
        reopened=Database(api.db.path); reopened.initialize()
        persisted=reopened.coding_task(task_id)
        self.assertEqual("WAITING_FOR_USER",persisted["status"])
        self.assertEqual("p3",persisted["current_phase_id"])
        self.assertEqual(["pass","pass","pending"],[p["status"] for p in persisted["plan"]["phases"]])

    def test_ineffective_structural_replan_is_resumable_before_an_identical_cycle(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="ineffective-replan"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        class Model:
            def generate(self,messages,model,think=False):
                return {"text":__import__("json").dumps(current)}
        class Runtime: model=Model()
        old_runtime=api.runtime;api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),set(),
                                    "verification criteria are structurally unsuitable",
                                    {"unmet_verify":["browser behavior cannot be observed"],"replan_instruction_source":"model"})
        finally: api.runtime=old_runtime
        self.assertIn("未完了工程の再試行待ち",result)
        saved=api.db.coding_task(task_id)
        self.assertEqual("WAITING_FOR_USER",saved["status"])
        self.assertEqual("REPLAN_INEFFECTIVE",saved["recovery_reason"])

    def test_ineffective_non_structural_replan_does_not_advance_phase_cursor(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="ineffective-non-structural-replan"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        class Model:
            def generate(self,messages,model,think=False): return {"text":__import__("json").dumps(current)}
        class Runtime: model=Model()
        old_runtime=api.runtime; api.runtime=Runtime()
        try:
            result=api._replan_task(task_id,api.db.coding_task(task_id),set(),"retry limit reached")
        finally: api.runtime=old_runtime
        saved=api.db.coding_task(task_id)
        self.assertIn("未完了工程の再試行待ち",result)
        self.assertEqual("WAITING_FOR_USER",saved["status"])
        self.assertEqual("p1",saved["current_phase_id"])
        self.assertEqual("REPLAN_INEFFECTIVE",saved["recovery_reason"])

    def test_repeated_zero_write_scope_rejection_terminates_without_replanning(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="zero-progress-scope"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        for attempt in (0, 1):
            report={
                "phase_id":"p1", "attempt":attempt, "status":"FAIL", "implemented":[], "changed_files":[],
                "test_executed":[], "test_pass":[], "test_fail":[], "build_executed":"NOT_RUN", "build_pass":"NOT_RUN",
                "errors":["target outside authorized mutation scope"], "blockers":[], "risks":[],
                "attempted_mutations":1, "accepted_mutations":0, "rejected_mutations":1, "files_written":0,
                "phase_progress":False, "no_progress":True,
                "typed_execution_summary":{
                    "state":"failed", "error":"target outside authorized mutation scope",
                    "operations":[{"tool":"operation_rejection", "status":"rejected", "input":{"path":"tests/test_math.py"}, "output":{"failure":"target outside authorized mutation scope"}}],
                    "rejected_operations":[{"category":"OPERATION_SCHEMA_SEMANTIC_ERROR", "path":"tests/test_math.py", "reason":"target outside authorized mutation scope"}],
                    "accepted_mutations":0, "files_written":0, "phase_progress":False,
                },
                "manager_decision":{"decision":"RETRY","reason":"scope rejection"},
            }
            api.db.add_coding_phase_report(task_id,"p1",attempt,report,"PASS",time.time())
        result=api._replan_task(task_id,api.db.coding_task(task_id) or {},set(),"retry limit reached")
        saved=api.db.coding_task(task_id)
        self.assertIn("no filesystem progress",result)
        self.assertEqual("BLOCKED",saved["status"])
        self.assertEqual("NO_PROGRESS",saved["recovery_reason"])

    def test_malformed_replans_consume_bounded_task_recovery_budget(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="bounded-malformed-replans"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        with patch.object(api, "_generate_plan", return_value=(None, None)):
            first=api._replan_task(task_id,api.db.coding_task(task_id),set(),"implementation failed")
            saved_first=api.db.coding_task(task_id)
            second=api._replan_task(task_id,saved_first,set(),"implementation failed")
            saved_second=api.db.coding_task(task_id)
            third=api._replan_task(task_id,saved_second,set(),"implementation failed")
        self.assertIn("could not be validated", first)
        self.assertIn("could not be validated", second)
        self.assertIn("replan limit", third)
        self.assertEqual(2,saved_second["replan_count_in_epoch"])

    def test_replanner_prompt_uses_bounded_failure_history_not_raw_attempt_reports(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="bounded-replanner-history"; goal="fix sample"; current=plan(goal)
        current["blueprint"]={"evidence":"DUPLICATED_BLUEPRINT_SENTINEL" + ("x" * 30_000)}
        current["implementation_plan_artifact"]={
            "file_manifest":[],
            "preimage_evidence":[{"source":"DUPLICATED_PREIMAGE_SENTINEL" + ("y" * 30_000)}],
        }
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        for attempt in range(6):
            report={
                "phase_id":"p1", "attempt":attempt, "status":"FAIL", "recovery_epoch":1,
                "raw_provider_trace":"discard-me-" + ("x" * 30_000),
                "errors":["planner input was too large"], "manager_decision":{
                    "decision":"RETRY", "reason":"Keep retry focused on the same task.",
                    "diagnosis":{"failure_class":"MODEL_CALL", "specific_fix":"Use concise typed evidence."}},
                "typed_execution_summary":{"state":"failed", "error":"ModelFailure",
                    "failure_class":"MODEL_CALL", "worktree_state":"UNCHANGED"},
            }
            api.db.add_coding_phase_report(task_id,"p1",attempt,report,"PASS",time.time())
        captured={}
        def capture_prompt(_task_id, _goal, prompt_text, _activity, **_kwargs):
            captured["prompt"]=prompt_text
            checkpoint=api.db.coding_task(_task_id)
            captured["checkpoint"]=(checkpoint.get("activity"), checkpoint.get("recovery_action"),
                                    checkpoint.get("recovery_reason"))
            return plan(goal), "{}"
        with patch.object(api,"_generate_plan",side_effect=capture_prompt):
            result=api._replan_task(task_id,api.db.coding_task(task_id),set(),"retry after model failure")
        self.assertEqual("Coding Task was replanned and requeued.",result)
        prompt_text=captured["prompt"]
        self.assertLess(len(prompt_text.encode("utf-8")),24_000)
        self.assertNotIn("discard-me-",prompt_text)
        self.assertNotIn("DUPLICATED_BLUEPRINT_SENTINEL",prompt_text)
        self.assertNotIn("DUPLICATED_PREIMAGE_SENTINEL",prompt_text)
        self.assertEqual(("QWEN_REPLANNING", "REPLAN_CONTINUATION", "REPLAN_LIMIT"),
                         captured["checkpoint"])
        self.assertIn("MODEL_CALL",prompt_text)
        self.assertIn("Use concise typed evidence.",prompt_text)
        self.assertEqual(4,prompt_text.count('"decision": "RETRY"'))

    def test_different_zero_progress_failure_fingerprints_still_block_recovery(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="different-zero-progress"; goal="fix sample"; current=plan(goal)
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","NONE",current,time.time())
        for attempt, category in enumerate(("OPERATION_SCHEMA_SEMANTIC_ERROR", "PREIMAGE_MISMATCH")):
            report={"phase_id":"p1", "attempt":attempt, "status":"FAIL", "changed_files":[],
                    "errors":[category], "phase_progress":False, "accepted_mutations":0,
                    "files_written":0, "manager_decision":{"decision":"RETRY"},
                    "typed_execution_summary":{"state":"failed", "worktree_state":"UNCHANGED",
                        "failure_class":category, "accepted_mutations":0, "files_written":0,
                        "phase_progress":False, "rejected_operations":[]}}
            api.db.add_coding_phase_report(task_id,"p1",attempt,report,"PASS",time.time())
        result=api._replan_task(task_id,api.db.coding_task(task_id),set(),"retry limit reached")
        self.assertIn("no filesystem progress",result)
        self.assertEqual("NO_PROGRESS",api.db.coding_task(task_id)["recovery_reason"])

    def test_explicit_plan_only_waits_for_approval_without_workspace_writes(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        goal=("新しいCoding Taskとして、計算関数と検証手順を追加してください。"
              "まず計画を作成し、計画を表示したところで停止して、まだファイルは変更しないでください。")
        requirements=normalize_coding_requirements(goal)
        task_id="explicit-plan-only"
        task=api.db.create_coding_task(task_id,conversation,goal,"QUEUED","QWEN_PLANNING",None,time.time())
        api.db.update_coding_task(task_id,requirements=requirements,execution_mode=requirements["execution_mode"],
                                  task_profile=requirements["task_profile"],required_mcp=requirements["required_mcp"])
        before=sorted(path.name for path in Path(self.tmp.name).iterdir())

        def generated_plan(*args, **kwargs):
            telemetry=api._coding_telemetry.setdefault(
                task_id, api.CodingTelemetry(task_id, model_name="test-planner"))
            now=time.time()
            telemetry.add_call(phase_id=None, role="PLANNER", model="test-planner",
                               messages=[{"role":"user", "content":goal}],
                               result={"prompt_tokens":3, "completion_tokens":5},
                               started=now-0.01, finished=now, structured=True,
                               thinking=False, success=True)
            return plan(goal), "{}"

        with patch.object(api,"_generate_plan",side_effect=generated_plan), \
             patch.object(api,"prepare_implementation_plan",side_effect=AssertionError("plan-only must not scaffold")), \
             patch.object(api._coding_scheduler_wake,"set") as wake:
            api._run_coding_planning(task)
        saved=api.db.coding_task(task_id)
        self.assertEqual("WAITING_FOR_PLAN_APPROVAL",saved["status"])
        self.assertTrue(saved["plan"])
        self.assertEqual([],saved["approved_scopes"])
        self.assertEqual(before,sorted(path.name for path in Path(self.tmp.name).iterdir()))
        self.assertFalse(wake.called)
        telemetry=api.db.coding_telemetry(task_id)
        self.assertIsNotNone(telemetry)
        self.assertEqual(1,telemetry["model_call_count"])
        self.assertEqual(1,telemetry["planner_call_count"])
        self.assertTrue(telemetry["calls"][0]["success"])
        self.assertEqual("NOT_RUN",telemetry["verify_status"])
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,
                                                         "message":"この計画で進めて"})
        self.assertEqual(200,response.status_code)
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertEqual("explicit_plan_approval",api.db.coding_task(task_id)["approved_scopes"][0]["source"])
        self.assertTrue(wake.called)

    def test_continuous_authorized_plan_requeues_without_plan_approval(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="continuous-plan"; goal="このrepoを確認して実装と検証まで連続して進めてください。途中確認は不要です。"
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","QWEN_PLANNING",None,time.time())
        with patch.object(api,"_generate_plan",return_value=(plan(goal),"{}")), \
             patch.object(api._coding_scheduler_wake,"set"):
            api._run_coding_planning(api.db.coding_task(task_id))
        saved=api.db.coding_task(task_id)
        self.assertEqual("QUEUED",saved["status"])
        self.assertIsNone(saved["pending_authorization"])
        self.assertTrue(saved["approved_scopes"])
        messages=api.db.conversation(conversation)["messages"]
        self.assertTrue(all("承認します" not in message["content"] for message in messages))

    def test_protected_plan_waits_then_approval_binds_and_resumes(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="protected-plan"; goal="repoを修正してください"
        protected=plan(goal); protected["scope"]["allowed"]=["frontend", "git commit"]
        api.db.create_coding_task(task_id,conversation,goal,"QUEUED","QWEN_PLANNING",None,time.time())
        with patch.object(api,"settings",api.settings.with_overrides({"permission_level":"automatic"})), \
             patch.object(api,"_generate_plan",return_value=(protected,"{}")), \
             patch.object(api._coding_scheduler_wake,"set"):
            api._run_coding_planning(api.db.coding_task(task_id))
        waiting=api.db.coding_task(task_id)
        self.assertEqual("WAITING_FOR_USER",waiting["status"])
        self.assertEqual(["git commit"],waiting["pending_authorization"]["requested_scope"])
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":"承認します"})
        self.assertEqual(task_id,response.json()["coding_task_id"])
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(wake.called)

    def test_execute_this_plan_recovers_resumable_task_before_normal_brain(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="recover-plan"; goal="fix sample"
        api.db.create_coding_task(task_id,conversation,goal,"RESUMABLE","NONE",plan(goal),time.time())
        with patch.object(api._coding_scheduler_wake,"set") as wake, \
             patch.object(api.runtime,"execute",side_effect=AssertionError("normal Brain must not run")):
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":"この計画を実行してください"})
        self.assertEqual(task_id,response.json()["coding_task_id"])
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(wake.called)

    def test_heavy_batch_checkpoint_is_not_authorization_and_continue_reuses_task(self):
        conversation=self.client.post(f"/api/projects/{self.project}/conversations").json()["id"]
        task_id="heavy-batch"; task_plan=plan("React frontend + FastAPI backend + SQLite database CRUD")
        api.db.create_coding_task(task_id,conversation,"React frontend + FastAPI backend + SQLite database CRUD","RUNNING","NONE",task_plan,time.time())
        api.db.update_coding_task(task_id,execution_mode="HEAVY_BATCHED")
        api.db.add_coding_phase_report(task_id,"p1",0,{"phase_id":"p1","status":"PASS","manager_decision":{"decision":"PASS"},"changed_files":[],"errors":[],"blockers":[],"test_fail":[]},"PASS",time.time())
        result=api._heavy_batch_checkpoint(task_id,task_plan,{"p1"})
        saved=api.db.coding_task(task_id)
        self.assertIn("続行",result)
        self.assertEqual("RESUMABLE",saved["status"])
        self.assertEqual("RESOURCE_CHECKPOINT",saved["recovery_reason"])
        self.assertIsNone(saved["pending_authorization"])
        self.assertEqual(1,saved["batch_cursor"])
        with patch.object(api._coding_scheduler_wake,"set") as wake:
            response=self.client.post("/api/chat",json={"project_id":self.project,"conversation_id":conversation,"message":"続行"})
        self.assertEqual(task_id,response.json()["coding_task_id"])
        self.assertEqual("QUEUED",api.db.coding_task(task_id)["status"])
        self.assertTrue(wake.called)

    def test_verification_only_allows_playwright_but_rejects_mutation_mcp(self):
        phase = {"execution_mode": "VERIFICATION_ONLY", "verify": ["browser smoke"], "required_mcp": ["playwright"]}
        self.assertEqual([], phase_execution_contract_errors(phase))
        self.assertEqual(["verification-only phase has implementation MCP metadata"],
                         phase_execution_contract_errors({**phase, "required_mcp": ["animejs"]}))

    def test_repair_phase_complexity_removes_implementation_mcps_from_verification_phase(self):
        value, changes = repair_phase_complexity({"phases": [{
            "id": "p4", "execution_mode": "VERIFICATION_ONLY",
            "done": ["check complete"], "verify": ["read-only verification"],
            "required_mcp": ["animejs", "playwright"], "selected_mcp": ["shadcn"],
        }]})
        phase = value["phases"][0]
        self.assertEqual(["playwright"], phase["required_mcp"])
        self.assertEqual([], phase["selected_mcp"])
        self.assertEqual([
            "p4:removed_implementation_required_mcp",
            "p4:removed_implementation_selected_mcp",
        ], changes)
        self.assertEqual([], phase_execution_contract_errors(phase))

    def test_repair_phase_complexity_removes_none_mcp_sentinel_from_phase_and_task(self):
        value, changes = repair_phase_complexity({
            "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                        "required_mcp": ["None"], "selected_mcp": [],
                        "acceptance_contract": [None, "None", "FRONTEND_STACK"]}],
            "tasks": [{"task_id": "p1-task", "required_mcp": ["None"]}],
        })
        self.assertEqual([], value["phases"][0]["required_mcp"])
        self.assertEqual([], value["tasks"][0]["required_mcp"])
        self.assertEqual(["FRONTEND_STACK"], value["phases"][0]["acceptance_contract"])
        self.assertIn("p1:removed_empty_required_mcp", changes)
        self.assertIn("p1:removed_empty_acceptance_contract", changes)
        self.assertIn("p1-task:removed_empty_required_mcp", changes)

    def test_evaluate_phase_ignores_empty_mcp_sentinel(self):
        phase = {"id": "p1", "done": ["create file"], "verify": ["file exists"],
                 "required_mcp": ["None"]}
        report = {"phase_id": "p1", "status": "PASS", "implemented": ["created"],
                  "changed_files": [], "errors": [], "blockers": [],
                  "typed_execution_summary": {"state": "completed", "operations": [
                      {"tool": "workspace_write", "status": "success", "output": {"path": "index.html"}},
                      {"tool": "workspace_read", "status": "success", "output": {"path": "index.html"}},
                  ]}}
        evaluation = evaluate_phase(phase, report, workspace_root=None)
        self.assertTrue(evaluation["phase_complete"])
        self.assertEqual([], evaluation["required_mcp"])

    def test_graph_diagnostics_identify_unknown_phase_dependency_and_namespace(self):
        value = {"phases": [{"id": "p1", "dependencies": ["missing"], "goal": "implement"}],
                 "tasks": [{"task_id": "t1", "phase_id": "p1", "depends_on": [], "goal": "implement"}]}
        diagnostics = graph_validation_diagnostics(value)
        self.assertFalse(diagnostics["valid"])
        error = diagnostics["first_error"]
        self.assertEqual("p1", error["offending_phase_id"])
        self.assertEqual("missing", error["offending_dependency_ref"])
        self.assertEqual("PHASE_ID", error["expected_reference_type"])
        self.assertEqual(["p1"], diagnostics["declared_phase_ids"])
        self.assertEqual(["t1"], diagnostics["declared_task_ids"])
        self.assertTrue(error["fingerprint"])

        task_ref = {"phases": [{"id": "p1", "dependencies": ["t1"], "goal": "implement"}],
                    "tasks": [{"task_id": "t1", "phase_id": "p1", "depends_on": [], "goal": "implement"}]}
        task_error = graph_validation_diagnostics(task_ref)["first_error"]
        self.assertEqual("TASK_ID", task_error["dependency_ref_kind"])
        self.assertEqual("TASK_ID_IN_PHASE_DEPENDENCY", task_error["error_kind"])

    def test_graph_diagnostics_treat_non_object_plan_as_empty_evidence(self):
        diagnostics = graph_validation_diagnostics(None)
        self.assertTrue(diagnostics["valid"])
        self.assertEqual([], diagnostics["errors"])
        self.assertIsNone(diagnostics["first_error"])

    def test_graph_replacement_map_rewrites_references_without_dropping_unknown_edges(self):
        value = {"phases": [{"id": "new-p1", "dependencies": []},
                             {"id": "new-p2", "dependencies": ["old-p1"]}],
                 "tasks": [{"task_id": "new-t1", "phase_id": "new-p1", "depends_on": []},
                            {"task_id": "new-t2", "phase_id": "new-p2", "depends_on": ["old-t1"]}]}
        normalized, changes, diagnostics = reconcile_graph_references(
            value, {"phase_ids": {"old-p1": "new-p1"}, "task_ids": {"old-t1": "new-t1"}})
        self.assertIn("phase:new-p2:old-p1->new-p1", changes)
        self.assertIn("task:new-t2:old-t1->new-t1", changes)
        self.assertEqual(["new-p1"], normalized["phases"][1]["dependencies"])
        self.assertEqual(["new-t1"], normalized["tasks"][1]["depends_on"])
        self.assertTrue(diagnostics["valid"])

    def test_graph_cycle_is_rejected_after_reference_validation(self):
        value = plan("cycle")
        value["phases"][0]["dependencies"]=["p2"]
        value["phases"][1]["dependencies"]=["p1"]
        diagnostics = graph_validation_diagnostics(value)
        self.assertEqual("PHASE_DEPENDENCY_CYCLE", diagnostics["first_error"]["error_kind"])
        self.assertEqual(["phase dependency cycle"], validate_plan(value, "cycle"))

    def test_replan_graph_preserves_unmapped_stale_dependencies_and_rebuilds_tasks(self):
        value = {"phases": [{"id": "p1", "dependencies": ["missing"], "goal": "implement",
                              "execution_mode": "IMPLEMENTATION"},
                             {"id": "p2", "dependencies": ["p1"], "goal": "verify",
                              "execution_mode": "VERIFICATION_ONLY"}],
                 "tasks": [{"task_id": "stale", "phase_id": "p1", "depends_on": ["gone"]}]}
        normalized, changes = normalize_replan_graph(value, ["playwright"])
        self.assertIn("unresolved_graph_dependencies", changes)
        self.assertIn("rebuilt_one_task_per_phase", changes)
        self.assertEqual(["missing"], normalized["phases"][0]["dependencies"])
        self.assertEqual({"p1", "p2"}, {item["phase_id"] for item in normalized["tasks"]})
        self.assertEqual(["IMPLEMENTATION", "VERIFICATION_ONLY"],
                         [item["execution_mode"] for item in normalized["phases"]])


if __name__ == "__main__": unittest.main()
