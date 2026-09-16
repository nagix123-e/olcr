import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_import_tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
os.environ["OLCR_DB_PATH"] = str(Path(_import_tmp.name) / "control-plane-import.sqlite")
os.environ["OLCR_ALLOWED_ROOTS"] = _import_tmp.name

from fastapi.testclient import TestClient

import olcr_api.app as api
from olcr_api.config import Settings
from olcr_api.coding_tasks import (evaluate_phase, frontend_stack_acceptance,
                                   marketing_design_structure_acceptance,
                                   canonical_coding_requirements, normalize_task_graph,
                                   required_mcp_contract, required_verification_contract, task_profile,
                                   normalize_coding_requirements)
from olcr_api.mcp_manifest import server_definition
from olcr_api.db import Database
from olcr_api.models import Route, Task, TaskState


BENCHMARK = """Build a React TypeScript Vite Tailwind shadcn/ui marketing site.

Frontend:
React
TypeScript
Vite
Tailwind CSS
shadcn/ui

Backend:
不要

Database:
不要

Do Not Implement:
Backend
Database
Authentication
Docker
CMS
Analytics
external API

Required:
shadcn MCPを実際に使用してください。
Playwright MCPを実際に使用して desktop mobile navigation responsive console accessibility を検証してください。"""


def plan(goal=BENCHMARK):
    phases = []
    for index, goal_text in enumerate(("repo inspection", "UI implementation", "browser verification", "final report", "browser verification", "completion report"), 1):
        phases.append({"id": f"p{index}", "goal": goal_text, "status": "pending",
                       "done": [goal_text], "verify": ["check " + goal_text],
                       "dependencies": [] if index == 1 else [f"p{index - 1}"], "risks": []})
    return {"schema_version": 1, "original_goal": goal,
            "scope": {"allowed": ["frontend"], "forbidden": ["backend", "database"]},
            "assumptions": [], "phases": phases, "max_retries_per_phase": 2,
            "requires_user_approval": True}


class BenchmarkProfileTests(unittest.TestCase):
    def test_negated_backend_and_database_are_not_positive(self):
        profile = task_profile(BENCHMARK)
        self.assertNotIn("backend", profile["POSITIVE_SIGNALS"])
        self.assertNotIn("database", profile["POSITIVE_SIGNALS"])
        self.assertIn("backend", profile["NEGATED_SIGNALS"])
        self.assertIn("database", profile["NEGATED_SIGNALS"])

    def test_frontend_benchmark_is_normal_and_fullstack_stays_heavy(self):
        profile = task_profile(BENCHMARK)
        self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", profile["TASK_PROFILE"])
        self.assertEqual("NORMAL", profile["EXECUTION_MODE"])
        self.assertEqual("HEAVY_BATCHED", task_profile("React frontend + FastAPI backend + SQLite database")["EXECUTION_MODE"])

    def test_full_benchmark_prompt_keeps_backend_and_database_forbidden(self):
        requirements = canonical_coding_requirements(BENCHMARK)
        self.assertEqual(["frontend"], requirements["required_capabilities"])
        self.assertEqual(["backend", "database"], requirements["forbidden_capabilities"])
        self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", requirements["task_profile"])
        self.assertEqual("NORMAL", requirements["execution_mode"])
        self.assertEqual(["browser"], requirements["required_verification"])

    def test_top_level_normalizer_has_deterministic_canonical_hash(self):
        request = BENCHMARK + "\nAnime.js MCPを使用し、Anime.js v4を導入する。\n# Future FIX Benchmark\n後でボタンを直してをFIXとして試験する。"
        focused = normalize_coding_requirements(request)
        self.assertEqual(["dependencies", "frontend"], focused["required_capabilities"])
        self.assertEqual(["backend", "database"], focused["forbidden_capabilities"])
        self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", focused["task_profile"])
        self.assertEqual("NORMAL", focused["execution_mode"])
        self.assertEqual(["animejs", "shadcn", "playwright"], focused["required_mcps"])
        self.assertTrue(focused["normalization_diagnostics"]["canonical_requirements_valid"])

    def test_shadcn_opt_out_does_not_create_a_requirement(self):
        self.assertEqual([], required_mcp_contract("Build a React site; do not use shadcn MCP."))
        self.assertEqual([], required_mcp_contract("Reactサイトを作るが shadcn MCPを使用しない。"))

    def test_explicit_english_and_japanese_mcp_contract_is_authoritative(self):
        required = required_mcp_contract("Use the shadcn MCP for components. Playwright MCPを実際に使用して browser verification をしてください。")
        self.assertEqual(["shadcn", "playwright"], required)
        self.assertEqual(["shadcn"], required_mcp_contract("Use shadcn MCP. Do not use Playwright MCP."))
        requirements = canonical_coding_requirements("Use the shadcn MCP. Use the Playwright MCP for browser verification. Run npm run typecheck and npm run build.")
        self.assertEqual(["shadcn", "playwright"], requirements["required_mcps"])
        self.assertEqual(requirements["required_mcps"], requirements["required_mcp"])
        self.assertEqual(["typecheck", "build", "browser"], requirements["required_verification"])

    def test_graph_has_no_report_phase_or_duplicate_browser_phase(self):
        normalized = normalize_task_graph(plan(), "FRONTEND_ONLY_MARKETING_SITE", ["shadcn", "playwright"])
        self.assertLessEqual(len(normalized["phases"]), 2)
        self.assertFalse(any("report" in phase["goal"].lower() for phase in normalized["phases"]))
        self.assertEqual(1, sum("browser" in phase["goal"].lower() for phase in normalized["phases"]))
        self.assertEqual(["shadcn"], normalized["tasks"][0]["required_mcp"])
        self.assertEqual(["playwright"], normalized["tasks"][-1]["required_mcp"])

    def test_preflight_only_phase_is_removed_before_the_implementation_executor(self):
        normalized = normalize_task_graph(plan(), "FRONTEND_ONLY_MARKETING_SITE", ["shadcn", "playwright"])
        self.assertEqual(2, len(normalized["phases"]))
        self.assertEqual("p1", normalized["phases"][0]["id"])
        self.assertIn("UI implementation", normalized["phases"][0]["goal"])
        self.assertNotIn("repo inspection", normalized["phases"][0]["goal"].lower())
        self.assertNotIn("mcp", normalized["phases"][0]["goal"].lower())
        self.assertEqual("implementation", normalized["tasks"][0]["domain"])

    def test_static_html_does_not_satisfy_stack_or_design_contract(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            (root / "index.html").write_text("<header>Header</header><main>Hero Features Workflow Development Tools CTA Links product visual</main><footer>Footer</footer>")
            self.assertFalse(frontend_stack_acceptance(str(root)))
            self.assertTrue(marketing_design_structure_acceptance(str(root)))

    def test_file_write_without_required_mcp_or_stack_cannot_pass(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            phase = {"id": "p1", "goal": "implement UI", "done": ["UI complete"], "verify": ["check UI"],
                     "required_mcp": ["shadcn"], "acceptance_contract": ["FRONTEND_STACK"]}
            report = {"phase_id": "p1", "status": "PASS", "errors": [], "blockers": [], "test_fail": [],
                      "build_executed": "PASS", "build_pass": "PASS", "typed_execution_summary": {"operations": [
                          {"tool": "workspace_write", "status": "success", "output": {"path": "index.html"}}]}}
            evaluation = evaluate_phase(phase, report, workspace_root=directory)
            self.assertFalse(evaluation["phase_complete"])
            self.assertIn("shadcn", evaluation["missing_mcp"])
            self.assertIn("FRONTEND_STACK", evaluation["missing_acceptance"])


class IdempotencyAndContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_settings = api.db, api.settings
        api.db = Database(str(Path(self.tmp.name) / "test.sqlite")); api.db.initialize()
        api.rebuild(Settings(db_path=str(Path(self.tmp.name) / "test.sqlite"), allowed_roots=(self.tmp.name,), task_manager_enabled=True))
        api.db.create_project("project", self.tmp.name, time.time(), "project")
        api.db.create_conversation("conversation", time.time(), "conversation", "project")
        self.client = TestClient(api.app)

    def test_api_chat_uses_same_canonical_normalizer_input(self):
        request = BENCHMARK + "\nAnime.js MCPを使用し、Anime.js v4を導入する。\n# Future FIX Benchmark\n後でボタンを直してをFIXとして試験する。"
        expected = normalize_coding_requirements(request)
        with patch.object(api.settings, "task_manager_enabled", True), patch.object(api._coding_scheduler_wake, "set"):
            response=self.client.post("/api/chat", json={"project_id":"project", "conversation_id":"conversation", "message":request})
        self.assertEqual(200, response.status_code)
        saved=api.db.coding_tasks("conversation")[0]
        self.assertEqual(expected["required_capabilities"], saved["requirements"]["required_capabilities"])
        self.assertEqual(expected["forbidden_capabilities"], saved["requirements"]["forbidden_capabilities"])
        self.assertEqual(expected["normalization_diagnostics"]["canonical_requirements_hash"], saved["requirements"]["normalization_diagnostics"]["canonical_requirements_hash"])

    def test_exact_19691_character_gui_payload(self):
        text = (Path(__file__).parent / 'fixtures' / 'animated_website_19691.txt').read_text()
        self.assertEqual(19691, len(text))
        with patch.object(api._coding_scheduler_wake, 'set'):
            response = self.client.post('/api/chat', json={'message': text, 'project_id': 'project',
                'conversation_id': 'conversation', 'message_id': 'exact-long-scope'})
        self.assertEqual(200, response.status_code)
        task = api.db.coding_task(response.json()['coding_task_id'])
        r = task['requirements']
        self.assertEqual(['dependencies', 'frontend'], r['required_capabilities'])
        self.assertEqual(['backend', 'database'], r['forbidden_capabilities'])
        self.assertEqual(['animejs', 'shadcn', 'playwright'], r['required_mcps'])
        self.assertEqual('IMPLEMENTATION', r['mutation_mode'])
        self.assertEqual('FRONTEND_ONLY_MARKETING_SITE', task['task_profile'])
        self.assertEqual('NORMAL', task['execution_mode'])
        self.assertEqual('QUEUED', task['status'])
        self.assertEqual(normalize_coding_requirements(text)['normalization_diagnostics']['capability_provenance'],
                         r['normalization_diagnostics']['capability_provenance'])

    def test_invalid_canonical_requirements_are_persisted_blocked_and_never_queued(self):
        request = "React frontendを実装する。ただし frontendは実装しない。"
        expected = normalize_coding_requirements(request)
        self.assertFalse(expected["normalization_diagnostics"]["canonical_requirements_valid"])
        with patch.object(api._coding_scheduler_wake, "set") as wake:
            response = self.client.post("/api/chat", json={"project_id": "project", "conversation_id": "conversation", "message": request})
        self.assertEqual(200, response.status_code)
        saved = api.db.coding_tasks("conversation")[0]
        self.assertEqual("BLOCKED", saved["status"])
        self.assertEqual("INVALID_REQUIREMENTS", saved.get("recovery_reason"))
        self.assertIsNone(api.db.next_queued_coding_task())
        wake.assert_not_called()

    def tearDown(self):
        api.db, api.settings = self.old_db, self.old_settings
        api.rebuild(self.old_settings)
        self.tmp.cleanup()

    def test_same_message_creates_one_task_and_duplicate_returns_it(self):
        request = "このrepoにReactアプリを実装してください"
        with patch.object(api._coding_scheduler_wake, "set"):
            first = self.client.post("/api/chat", json={"project_id": "project", "conversation_id": "conversation", "message": request, "message_id": "send-1"})
            second = self.client.post("/api/chat", json={"project_id": "project", "conversation_id": "conversation", "message": request, "message_id": "send-1"})
        self.assertEqual(200, first.status_code); self.assertEqual(200, second.status_code)
        self.assertEqual(first.json()["coding_task_id"], second.json()["coding_task_id"])
        self.assertEqual(1, len(api.db.coding_tasks("conversation")))

    def test_multiple_resumable_never_falls_to_brain(self):
        for task_id in ("first", "second"):
            api.db.create_coding_task(task_id, "conversation", "fix repository", "RESUMABLE", "NONE", plan("fix repository"), time.time())
        with patch.object(api.runtime, "execute", side_effect=AssertionError("brain intercepted")), patch.object(api._coding_scheduler_wake, "set") as wake:
            response = self.client.post("/api/chat", json={"project_id": "project", "conversation_id": "conversation", "message": "続行"})
        self.assertEqual("複数の回復可能な Coding Task があるため、対象を選択してください。", response.json()["response"])
        wake.assert_not_called()

    def test_required_mcp_unavailable_stops_before_planner(self):
        task = api.db.create_coding_task("required-mcp", "conversation", BENCHMARK, "QUEUED", "QWEN_PLANNING", None, time.time())
        with patch.object(api, "node_mcp_launch_command", return_value=None), \
             patch.object(api, "_generate_plan", side_effect=AssertionError("planner must not run")):
            api._run_coding_planning(task)
        saved = api.db.coding_task("required-mcp")
        self.assertEqual("BLOCKED", saved["status"])
        self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", saved["task_profile"])
        self.assertEqual(["shadcn", "playwright"], saved["required_mcp"])
        self.assertTrue(saved["mcp_evidence"])

    def test_planning_reaches_qwen_only_after_required_shadcn_is_ready(self):
        task = api.db.create_coding_task("ready-before-qwen", "conversation", BENCHMARK, "QUEUED", "QWEN_PLANNING", None, time.time())
        with patch.object(api, "node_mcp_launch_command", return_value=["bundled-node"]), \
             patch.object(api, "_run_required_mcp", return_value=({"status": "PASS"}, None)) as mcp, \
             patch.object(api, "_generate_plan", return_value=(None, [])) as planner:
            api._run_coding_planning(task)
        mcp.assert_called_once()
        planner.assert_called_once()

    def test_gui_chat_endpoint_persists_and_consumes_one_canonical_benchmark_pipeline(self):
        def preflight(task_id, server_id, workspace_root, purpose):
            api._mcp_evidence(task_id, server_id, status="PASS", purpose=purpose, tool_name="initialize", result={})
            api._mcp_evidence(task_id, server_id, status="PASS", purpose=purpose, tool_name="tools/list", result={})
            return api._mcp_evidence(task_id, server_id, status="PASS", purpose=purpose,
                                     tool_name="search_items_in_registries", result={"content": "card"}), None

        with patch.object(api, "node_mcp_launch_command", return_value=["bundled-node"]), \
             patch.object(api, "_run_required_mcp", side_effect=preflight), \
             patch.object(api, "_generate_plan", return_value=(plan(), [])) as planner, \
             patch.object(api.db, "enqueue_coding_task"), \
             patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.post("/api/chat", json={"project_id": "project", "conversation_id": "conversation",
                                                             "message": BENCHMARK, "message_id": "gui-benchmark"})
            self.assertEqual(200, response.status_code)
            task = api.db.coding_task(response.json()["coding_task_id"])
            api._run_coding_planning(task)
        saved = api.db.coding_task(response.json()["coding_task_id"])
        self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", saved["task_profile"])
        self.assertEqual("NORMAL", saved["execution_mode"])
        self.assertEqual(["frontend"], saved["requirements"]["required_capabilities"])
        self.assertEqual(["backend", "database"], saved["requirements"]["forbidden_capabilities"])
        self.assertEqual(["shadcn", "playwright"], saved["required_mcp"])
        self.assertEqual("PASS", saved["requirements"]["preflight"]["shadcn"])
        self.assertEqual(3, len(saved["mcp_evidence"]))
        self.assertTrue(all(item.get("evidence_id") and item.get("created_at") for item in saved["mcp_evidence"]))
        self.assertEqual(2, len(saved["plan"]["phases"]))
        self.assertIn("ORCHESTRATOR_PREFLIGHT_ALREADY_COMPLETED", planner.call_args.args[2])

    def test_profile_mismatch_fails_closed_before_planning(self):
        task = api.db.create_coding_task("profile-mismatch", "conversation", BENCHMARK, "QUEUED", "QWEN_PLANNING", None, time.time())
        bad = {**canonical_coding_requirements(BENCHMARK), "task_profile": "GENERAL_CODING", "execution_mode": "HEAVY_BATCHED"}
        api.db.update_coding_task(task["id"], requirements=bad)
        with patch.object(api, "_generate_plan", side_effect=AssertionError("planning must not start")):
            api._run_coding_planning(api.db.coding_task(task["id"]))
        saved = api.db.coding_task(task["id"])
        self.assertEqual("BLOCKED", saved["status"])
        self.assertEqual("TASK_PROFILE_MISMATCH", saved["recovery_reason"])

    def test_lost_shadcn_preflight_evidence_fails_as_a_control_plane_invariant(self):
        normalized = normalize_task_graph(plan(), "FRONTEND_ONLY_MARKETING_SITE", ["shadcn", "playwright"])
        api.db.create_coding_task("lost-shadcn-evidence", "conversation", BENCHMARK, "QUEUED", "NONE", normalized, time.time())
        requirements = {**canonical_coding_requirements(BENCHMARK), "preflight": {"shadcn": "PASS"}}
        api.db.update_coding_task("lost-shadcn-evidence", requirements=requirements, required_mcp=["shadcn", "playwright"])
        api._initialize_subtask_progress("lost-shadcn-evidence", normalized)
        with patch.object(api.runtime, "execute", side_effect=AssertionError("implementation must not run")):
            api._run_managed_task("lost-shadcn-evidence", self.tmp.name)
        saved = api.db.coding_task("lost-shadcn-evidence")
        self.assertEqual("BLOCKED", saved["status"])
        self.assertEqual("MCP_EVIDENCE_LOST", saved["recovery_reason"])

    def test_orchestration_only_phase_cannot_reach_the_implementation_executor(self):
        orchestration_plan = plan()
        orchestration_plan["phases"] = [orchestration_plan["phases"][0]]
        api.db.create_coding_task("non-artifact-phase", "conversation", BENCHMARK, "QUEUED", "NONE", orchestration_plan, time.time())
        with patch.object(api.runtime, "execute", side_effect=AssertionError("implementation must not run")):
            api._run_managed_task("non-artifact-phase", self.tmp.name)
        saved = api.db.coding_task("non-artifact-phase")
        self.assertEqual("BLOCKED", saved["status"])
        self.assertEqual("NON_ARTIFACT_PHASE", saved["recovery_reason"])

    def test_planner_receives_completed_preflight_evidence(self):
        api.db.create_coding_task("preflight-context", "conversation", "implement", "QUEUED", "NONE", plan("implement"), time.time())
        (Path(self.tmp.name) / "package.json").write_text("{}")
        api._mcp_evidence("preflight-context", "shadcn", status="PASS", purpose="component selection",
                          tool_name="search_items_in_registries", result={"content": "card"})
        context = api._planning_preflight_context("preflight-context", self.tmp.name)
        self.assertIn("package.json", context["repo_summary"]["workspace_entries"])
        self.assertEqual("shadcn", context["shadcn_mcp_evidence"][0]["mcp_name"])
        self.assertIn("Required MCP preflight is already complete.", context["constraints"])

    def test_active_plan_revision_is_persisted_atomically(self):
        api.db.create_coding_task("revision", "conversation", "implement", "QUEUED", "NONE", plan("implement"), time.time())
        revision_one = {**plan("implement"), "phases": plan("implement")["phases"][:2]}
        saved = api.db.update_coding_task("revision", plan=revision_one, plan_revision=1)
        self.assertEqual(1, saved["plan_revision"])
        self.assertEqual(revision_one, saved["plan"])
        self.assertEqual(revision_one, saved["active_plan"])

    def test_fresh_benchmark_plan_starts_with_mutating_ui_work_after_preflight(self):
        task = api.db.create_coding_task("fresh-preflight-plan", "conversation", BENCHMARK, "QUEUED", "QWEN_PLANNING", None, time.time())
        with patch.object(api, "node_mcp_launch_command", return_value=["bundled-node"]), \
             patch.object(api, "_run_required_mcp", return_value=({"mcp_name": "shadcn", "status": "PASS"}, None)), \
             patch.object(api, "_generate_plan", return_value=(plan(), [])) as planner, \
             patch.object(api.db, "enqueue_coding_task"), \
             patch.object(api._coding_scheduler_wake, "set"):
            api._run_coding_planning(task)
        saved = api.db.coding_task("fresh-preflight-plan")
        self.assertEqual("QUEUED", saved["status"])
        self.assertEqual(0, saved["plan_revision"])
        self.assertEqual(["p1", "p2"], [phase["id"] for phase in saved["plan"]["phases"]])
        self.assertIn("UI implementation", saved["plan"]["phases"][0]["goal"])
        self.assertEqual("implementation", saved["plan"]["tasks"][0]["domain"])
        self.assertEqual(["shadcn"], saved["plan"]["tasks"][0]["required_mcp"])
        self.assertIn("ORCHESTRATOR_PREFLIGHT_ALREADY_COMPLETED", planner.call_args.args[2])

    def test_first_executor_run_uses_ui_phase_and_reuses_shadcn_preflight_evidence(self):
        normalized = normalize_task_graph(plan(), "FRONTEND_ONLY_MARKETING_SITE", ["shadcn", "playwright"])
        api.db.create_coding_task("first-ui-execution", "conversation", BENCHMARK, "QUEUED", "NONE", normalized, time.time())
        api.db.update_coding_task("first-ui-execution", required_mcp=["shadcn", "playwright"],
                                  requirements=canonical_coding_requirements(BENCHMARK),
                                  task_profile="FRONTEND_ONLY_MARKETING_SITE")
        api._initialize_subtask_progress("first-ui-execution", normalized)
        api._mcp_evidence("first-ui-execution", "shadcn", status="PASS", purpose="completed preflight",
                          tool_name="search_items_in_registries", result={"content": "card"})
        execution = Task("implement UI", route=Route.IMPLEMENTATION, state=TaskState.COMPLETED,
                         tool_executions=[{"tool": "workspace_write", "status": "success", "output": {"path": "src/App.tsx"}}])
        with patch.object(api, "_run_required_mcp", side_effect=AssertionError("preflight must not repeat")), \
             patch.object(api.runtime, "execute", return_value=(execution, "UI written")) as execute, \
             patch.object(api, "_review_phase", return_value="BLOCKED"):
            api._run_managed_task("first-ui-execution", self.tmp.name)
        self.assertIn("Phase: UI implementation", execute.call_args.args[0])
        self.assertIn("[FRONTEND_UI_QUALITY_RULES]", execute.call_args.args[0])

    def test_quality_guidance_is_scoped_to_marketing_frontends(self):
        marketing = {"requirements": canonical_coding_requirements(BENCHMARK), "task_profile": "FRONTEND_ONLY_MARKETING_SITE"}
        self.assertIn("CTA", api._frontend_quality_guidance(marketing, {"goal": "UI implementation", "required_mcp": ["shadcn"]}))
        self.assertIn("[FRONTEND_UI_QUALITY_SELF_REVIEW]", api._frontend_quality_guidance(marketing, {"goal": "browser verification", "required_mcp": ["playwright"]}))
        self.assertEqual("", api._frontend_quality_guidance({"task_profile": "GENERAL_CODING"}, {"goal": "editor implementation"}))

    def test_generic_quality_guidance_has_no_olcr_brand_color(self):
        marketing = {"requirements": canonical_coding_requirements(BENCHMARK),
                     "task_profile": "FRONTEND_ONLY_MARKETING_SITE", "original_goal": "Build a marketing site."}
        guidance = api._frontend_quality_guidance(marketing, {"goal": "UI implementation", "required_mcp": ["shadcn"]})
        self.assertNotIn("#95E329", guidance)
        self.assertIn("one clear primary CTA", guidance)
        self.assertIn("two consecutive card-grid", guidance)
        self.assertIn("SOURCE=NEUTRAL_DEFAULTS_NO_BRAND_PALETTE", guidance)

    def test_explicit_task_brand_color_overrides_repository_tokens(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            Path(directory, "tokens.css").write_text(":root { --brand-accent: #5B5BD6; }")
            marketing = {"requirements": canonical_coding_requirements(BENCHMARK),
                         "task_profile": "FRONTEND_ONLY_MARKETING_SITE",
                         "original_goal": "Build a marketing site.\nBrand accent color: #95E329"}
            guidance = api._frontend_quality_guidance(marketing, {"goal": "UI implementation"}, directory)
        self.assertIn("SOURCE=EXPLICIT_CURRENT_TASK", guidance)
        self.assertIn("PALETTE=#95E329", guidance)
        self.assertNotIn("#5B5BD6", guidance)

    def test_alternate_explicit_brand_color_is_preserved_without_olcr_bias(self):
        marketing = {"requirements": canonical_coding_requirements(BENCHMARK),
                     "task_profile": "FRONTEND_ONLY_MARKETING_SITE",
                     "original_goal": "Build a marketing site.\nPalette color: #5B5BD6"}
        guidance = api._frontend_quality_guidance(marketing, {"goal": "UI implementation"})
        self.assertIn("SOURCE=EXPLICIT_CURRENT_TASK", guidance)
        self.assertIn("PALETTE=#5B5BD6", guidance)
        self.assertNotIn("#95E329", guidance)

    def test_repository_design_tokens_are_used_without_task_color(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            Path(directory, "tokens.css").write_text(":root { --brand-accent: #5B5BD6; --color-primary: #113355; }")
            Path(directory, "tailwind.config.js").write_text("module.exports = { theme: { colors: { primaryAccent: '#7C3AED' } } }")
            marketing = {"requirements": canonical_coding_requirements(BENCHMARK),
                         "task_profile": "FRONTEND_ONLY_MARKETING_SITE", "original_goal": "Build a marketing site."}
            guidance = api._frontend_quality_guidance(marketing, {"goal": "UI implementation"}, directory)
        self.assertIn("SOURCE=EXISTING_REPOSITORY_DESIGN_TOKENS", guidance)
        self.assertIn("primaryAccent=#7C3AED", guidance)
        self.assertIn("--brand-accent=#5B5BD6", guidance)
        self.assertIn("--color-primary=#113355", guidance)

    def test_invalid_advisory_task_graph_does_not_reject_a_valid_phase_plan(self):
        model_plan = normalize_task_graph(plan(), "FRONTEND_ONLY_MARKETING_SITE", ["shadcn", "playwright"])
        model_plan["tasks"][0]["phase_id"] = "already-completed-preflight"
        with patch.object(api, "_model_text", return_value=json.dumps(model_plan)) as model:
            parsed, _ = api._generate_plan("invalid-advisory-graph", BENCHMARK, "planning", "QWEN_PLANNING")
        self.assertIsNotNone(parsed)
        self.assertNotIn("tasks", parsed)
        self.assertEqual(1, model.call_count)

    def test_fresh_benchmark_e2e_blocks_before_any_model_or_filesystem_work(self):
        with patch.object(api, "node_mcp_launch_command", return_value=None), \
             patch.object(api._coding_scheduler_wake, "set"):
            response = self.client.post("/api/chat", json={"project_id": "project", "conversation_id": "conversation",
                                                             "message": BENCHMARK, "message_id": "fresh-benchmark"})
            self.assertEqual(200, response.status_code)
            task_id = response.json()["coding_task_id"]
            api._run_coding_planning(api.db.coding_task(task_id))
            saved = api.db.coding_task(task_id)
            self.assertIsNotNone(saved)
            self.assertEqual("BLOCKED", saved["status"])
            self.assertEqual("FRONTEND_ONLY_MARKETING_SITE", saved["task_profile"])
            self.assertIsNone(saved["plan"])
            self.assertEqual("REQUIRED_MCP_UNAVAILABLE", saved["recovery_reason"])
            self.assertEqual("BLOCKED", saved["mcp_evidence"][0]["status"])

    def test_required_mcp_runs_initialize_list_and_allowlisted_tool(self):
        api.db.create_coding_task("mcp-lifecycle", "conversation", "implement", "RUNNING", "NONE", plan("implement"), time.time())
        calls = []

        class FakeMCP:
            def __init__(self, *args, **kwargs):
                calls.append(("construct", args[1]))
            def start(self): calls.append(("start",)); return "STARTING"
            def initialize(self):
                calls.append(("initialize",)); return {"status": "AVAILABLE", "response": {"result": {}}}
            def tools_list(self):
                calls.append(("tools/list",)); return {"status": "AVAILABLE", "response": {"result": {"tools": [{"name": "search_items_in_registries"}]}}}
            def call(self, tool, arguments):
                calls.append(("call", tool, arguments)); return {"status": "AVAILABLE", "response": {"result": "ok"}}
            def close(self): calls.append(("close",))

        definition = {"enabled_by_policy": True, "allowed_tools": ["search_items_in_registries"]}
        with patch.object(api, "server_definition", return_value=definition), \
             patch.object(api, "node_mcp_launch_command", return_value=["fake-mcp"]), \
             patch.object(api, "MCPRuntime", FakeMCP):
            item, error = api._run_required_mcp("mcp-lifecycle", "shadcn", self.tmp.name, "component discovery")
        self.assertIsNone(error); self.assertEqual("PASS", item["status"])
        self.assertIn(("initialize",), calls); self.assertIn(("tools/list",), calls)
        self.assertIn(("call", "search_items_in_registries", {"query": "card", "registries": ["@shadcn"], "limit": 5}), calls)
        self.assertIn(("close",), calls)
        evidence = api.db.coding_task("mcp-lifecycle")["mcp_evidence"]
        self.assertEqual(["initialize", "tools/list", "search_items_in_registries"], [entry["tool_name"] for entry in evidence])

    def test_required_playwright_calls_its_allowlisted_navigation_tool(self):
        api.db.create_coding_task("playwright-lifecycle", "conversation", "verify", "RUNNING", "NONE", plan("verify"), time.time())
        calls = []

        class FakeMCP:
            def __init__(self, *args, **kwargs): pass
            def start(self): return "STARTING"
            def initialize(self): return {"status": "AVAILABLE", "response": {"result": {}}}
            def tools_list(self): return {"status": "AVAILABLE", "response": {"result": {"tools": [{"name": "browser_navigate"}]}}}
            def call(self, tool, arguments):
                calls.append((tool, arguments)); return {"status": "AVAILABLE", "response": {"result": "navigated"}}
            def close(self): pass

        definition = {"enabled_by_policy": True, "allowed_tools": ["browser_navigate"]}
        with patch.object(api, "server_definition", return_value=definition), \
             patch.object(api, "node_mcp_launch_command", return_value=["fake-mcp"]), \
             patch.object(api, "MCPRuntime", FakeMCP):
            item, error = api._run_required_mcp("playwright-lifecycle", "playwright", self.tmp.name, "browser verification")
        self.assertIsNone(error); self.assertEqual("PASS", item["status"])
        self.assertEqual([("browser_navigate", {"url": "http://127.0.0.1:5173"})], calls)

    def test_shadcn_network_failure_is_not_reported_as_runtime_unavailable(self):
        api.db.create_coding_task("network-block", "conversation", "implement", "RUNNING", "NONE", plan("implement"), time.time())

        class FakeMCP:
            def __init__(self, *args, **kwargs): pass
            def start(self): return "STARTING"
            def initialize(self): return {"status": "AVAILABLE", "response": {"result": {}}}
            def tools_list(self): return {"status": "AVAILABLE", "response": {"result": {"tools": [{"name": "search_items_in_registries"}]}}}
            def call(self, tool, arguments): return {"status": "AVAILABLE", "response": {"result": {"isError": True, "content": [{"text": "getaddrinfo ENOTFOUND ui.shadcn.com"}]}}}
            def close(self): pass

        definition = {"enabled_by_policy": True, "allowed_tools": ["search_items_in_registries"]}
        with patch.object(api, "server_definition", return_value=definition), \
             patch.object(api, "node_mcp_launch_command", return_value=["fake-mcp"]), \
             patch.object(api, "MCPRuntime", FakeMCP):
            item, error = api._run_required_mcp("network-block", "shadcn", self.tmp.name, "component discovery")
        self.assertIsNone(item); self.assertIn("network", error)
        self.assertEqual("BLOCKED_NETWORK", api.db.coding_task("network-block")["mcp_evidence"][-1]["status"])

    def test_shadcn_policy_is_enabled_for_frontend_use(self):
        self.assertTrue(server_definition("shadcn")["enabled_by_policy"])
