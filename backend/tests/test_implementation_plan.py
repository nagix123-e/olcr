import tempfile
import unittest
import json
from pathlib import Path

from olcr_api.implementation_plan import (PlanManifestCoverageError, PlanPathValidationError,
                                          PlanStackConformanceError,
                                          canonical_stack_contract,
                                          deterministic_manifest_completion, deterministic_manifest_completion_diagnostics,
                                          manifest_coverage,
                                          manifest_coverage_message, manifest_repair_no_progress_message,
                                          manifest_repair_progress,
                                          normalize_plan_artifact_paths,
                                          path_validation_message, prepare_implementation_plan, stack_conformance,
                                          workspace_state)
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.retrieval import DisabledVectorStore, FileRetriever, FTSRetriever, RetrievalRouter
from olcr_api.runtime import Runtime


class ImplementationPlanTests(unittest.TestCase):
    def test_path_validation_messages_preserve_failure_semantics(self):
        self.assertIn("存在しないファイル", path_validation_message("MISSING_MODIFY_TARGET", replan=True))
        self.assertIn("再計画します", path_validation_message("MISSING_MODIFY_TARGET", replan=True))
        self.assertNotIn("ファイル範囲", path_validation_message("MISSING_MODIFY_TARGET", replan=True))
        self.assertIn("プロジェクト外", path_validation_message("OUTSIDE_AUTHORIZED_ROOT"))
        self.assertIn("読み取り専用", path_validation_message("READ_ONLY_SCOPE_VIOLATION"))
    def test_manifest_is_normalized_and_create_is_safely_scaffolded(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            plan = {"phases": [{"id": "p1", "dependencies": [], "verify": ["typecheck"]}],
                    "file_manifest": [{"path": "src/App.tsx", "action": "create", "owner_phase": "p1"},
                                      {"path": "README.md", "action": "create"}]}
            prepared, artifact = prepare_implementation_plan("task", plan, directory, {"task_profile": "GENERAL_CODING", "requirements_hash": "abc"})
            self.assertEqual(str(Path(directory).resolve()), artifact["target_root"])
            self.assertEqual(2, artifact["file_manifest_count"])
            self.assertEqual(2, artifact["scaffold_created_count"])
            self.assertIn("implementation_plan_artifact", prepared)
            self.assertIn("export default", Path(directory, "src/App.tsx").read_text())

    def test_all_entries_are_validated_before_first_write(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            plan = {"phases": [], "file_manifest": [{"path": "first.ts", "action": "create"},
                                                       {"path": "../outside.ts", "action": "create"}]}
            with self.assertRaises(ValueError):
                prepare_implementation_plan("task", plan, directory)
            self.assertFalse(Path(directory, "first.ts").exists())

    def test_path_failure_exposes_structured_repair_diagnostics_without_writing(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            plan = {"phases": [{"id": "p1"}], "file_manifest": [
                {"path": "../outside.ts", "action": "create", "owner_phase": "p1"},
            ]}
            with self.assertRaises(PlanPathValidationError) as caught:
                prepare_implementation_plan("task", plan, directory)
            self.assertEqual("../outside.ts", caught.exception.diagnostics[0]["offending_plan_path"])
            self.assertEqual("file_manifest[0].path", caught.exception.diagnostics[0]["offending_plan_field"])
            self.assertEqual("p1", caught.exception.diagnostics[0]["offending_phase"])
            self.assertTrue(caught.exception.diagnostics[0]["path_validation_reason"])
            self.assertEqual("OUTSIDE_AUTHORIZED_ROOT", caught.exception.diagnostics[0]["path_validation_code"])
            self.assertFalse(Path(directory, "outside.ts").exists())

    def test_explicit_semantic_technology_name_is_not_a_valid_modify_target(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            with self.assertRaises(PlanPathValidationError) as caught:
                prepare_implementation_plan(
                    "task",
                    {"phases": [{"id": "p1"}], "file_manifest": [
                        {"path": "Anime.js", "action": "modify", "owner_phase": "p1"},
                    ]},
                    directory,
                )
            diagnostic = caught.exception.diagnostics[0]
            self.assertEqual("Anime.js", diagnostic["offending_plan_path"])
            self.assertEqual("file_manifest[0].path", diagnostic["offending_plan_field"])
            self.assertEqual("modify target does not exist", diagnostic["path_validation_reason"])
            self.assertEqual("MISSING_MODIFY_TARGET", diagnostic["path_validation_code"])

    def test_greenfield_missing_modify_is_safely_normalized_to_create(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            prepared, artifact = prepare_implementation_plan(
                "task", {"phases": [{"id": "p1"}], "file_manifest": [
                    {"path": "src/main.tsx", "action": "modify", "owner_phase": "p1"},
                ]}, directory, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertEqual("create", prepared["file_manifest"][0]["action"])
            self.assertEqual(["src/main.tsx"], artifact["normalized_create_from_modify"])
            self.assertTrue(Path(directory, "src/main.tsx").exists())

    def test_greenfield_manifest_must_cover_detected_frontend_stack(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            plan = {"file_manifest": [
                {"path": "package.json", "action": "create"},
                {"path": "tsconfig.json", "action": "create"},
            ], "phases": [{"id": "p1", "goal": "Build React Vite TypeScript Tailwind Anime.js shadcn Hero and CTA site",
                             "execution_mode": "IMPLEMENTATION", "done": ["site exists"], "verify": ["build passes"],
                             "dependencies": [], "risks": []}]}
            with self.assertRaises(PlanManifestCoverageError) as caught:
                prepare_implementation_plan("coverage", plan, directory,
                                            {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertIn("src/App.tsx", caught.exception.missing)
            self.assertFalse(Path(directory, "src").exists())

    def test_componentized_phase_requires_explicit_component_paths(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            base = ("index.html", "package.json", "src/App.tsx", "src/index.css", "src/main.tsx")
            plan = {"file_manifest": [{"path": path, "action": "create"} for path in base],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Build componentized homepage sections Hero Philosophy HowItWorks API MCP CTA",
                                 "done": ["homepage exists"], "verify": ["build passes"],
                                 "dependencies": [], "risks": []}]}
            coverage = manifest_coverage(plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertFalse(coverage["phase_mutation_scope_complete"])
            self.assertIn("src/components/Hero.tsx", coverage["missing_implementation_paths"])
            self.assertIn("src/components/CTA.tsx", coverage["missing_paths"])

    def test_componentized_phase_is_ready_when_manifest_enumerates_components(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            names = ("Hero", "Philosophy", "HowItWorks", "API", "MCP", "CTA")
            paths = ("index.html", "package.json", "src/App.tsx", "src/index.css", "src/main.tsx") + tuple(
                f"src/components/{name}.tsx" for name in names)
            plan = {"file_manifest": [{"path": path, "action": "create"} for path in paths],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Build componentized homepage sections Hero Philosophy HowItWorks API MCP CTA",
                                 "done": ["homepage exists"], "verify": ["build passes"],
                                 "dependencies": [], "risks": []}]}
            coverage = manifest_coverage(plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertTrue(coverage["phase_mutation_scope_complete"])
            self.assertEqual([], coverage["missing_implementation_paths"])

    def test_componentized_known_artifacts_are_deterministically_added_with_provenance(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state.update({"new_project_intent": True, "host_workspace_authorized": True,
                          "workspace_state": "EMPTY_GREENFIELD"})
            plan = {"file_manifest": [{"path": "package.json", "action": "create"}],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Build a componentized Vite React homepage with Hero Philosophy API MCP CTA sections",
                                 "done": ["homepage exists"], "verify": ["build passes"],
                                 "dependencies": [], "risks": []}]}
            coverage = manifest_coverage(plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            decision = deterministic_manifest_completion_diagnostics(plan, coverage, directory, state)
            self.assertTrue(decision["eligible"])
            self.assertIn("src/components/Hero.tsx", decision["candidate_paths"])
            candidate, additions = deterministic_manifest_completion(plan, coverage, directory, state)
            self.assertIn("src/components/Hero.tsx", additions)
            prepared, artifact = prepare_implementation_plan(
                "component-complete", candidate, directory,
                {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertTrue((artifact["manifest_coverage"])["valid"])
            self.assertIn("NORMALIZED_PHASE_ARTIFACT",
                          artifact["artifact_provenance"]["src/components/Hero.tsx"])

    def test_bare_app_is_canonicalized_only_for_confirmed_vite_react_profile(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            plan = {"file_manifest": [{"path": "App.tsx", "action": "create"}],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Build Vite React TypeScript site", "done": ["site exists"],
                                 "verify": ["build passes"], "dependencies": [], "risks": []}]}
            normalized, changes = normalize_plan_artifact_paths(
                plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"})
            self.assertEqual("src/App.tsx", normalized["file_manifest"][0]["path"])
            self.assertEqual("VITE_REACT_CANONICAL_PROFILE", changes[0]["source"])

    def test_unknown_component_prose_does_not_invent_a_path(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            coverage = manifest_coverage(
                {"file_manifest": [{"path": "index.html", "action": "create"}],
                 "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                              "goal": "Build a Vite React page with an Avatar concept",
                              "done": ["page exists"], "verify": ["build passes"]}]},
                {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertNotIn("src/components/Avatar.tsx", coverage["expected_paths"])

    def test_completion_reports_candidate_rejection_when_scope_is_narrow(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state.update({"new_project_intent": True, "host_workspace_authorized": True,
                          "workspace_state": "EMPTY_GREENFIELD",
                          "authorized_mutation": ["index.html", "src/App.tsx"]})
            plan = {"file_manifest": [{"path": "package.json", "action": "create"}],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Build componentized Vite React Hero Philosophy homepage",
                                 "done": ["homepage exists"], "verify": ["build passes"]}]}
            coverage = manifest_coverage(plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            decision = deterministic_manifest_completion_diagnostics(plan, coverage, directory, state)
            self.assertTrue(decision["eligible"])
            self.assertEqual([], decision["added_paths"])
            self.assertTrue(decision["candidate_diagnostics"])
            self.assertTrue(any(item["rejection_reason"] == "ARTIFACT_OUTSIDE_AUTHORIZED_MUTATION_SCOPE"
                                for item in decision["candidate_diagnostics"]))

    def test_partial_workspace_active_task_provenance_allows_modify_but_unrelated_file_does_not(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            (root / "package.json").write_text("{}\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "components").mkdir()
            (root / "src" / "components" / "Hero.tsx").write_text("export default function Hero() {}\n", encoding="utf-8")
            state = workspace_state(directory)
            state.update({"new_project_intent": True, "host_workspace_authorized": True,
                          "workspace_state": "PARTIALLY_INITIALIZED",
                          "current_task_provenance": {"src/components/Hero.tsx": "CREATED_BY_CURRENT_TASK_SCAFFOLD"}})
            plan = {"file_manifest": [{"path": "package.json", "action": "create"}],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Build a componentized Vite React page with Hero Philosophy sections", "done": ["page exists"],
                                 "verify": ["build passes"]}]}
            coverage = manifest_coverage(plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            # Hero.tsx is an existing active-task artifact and therefore
            # exercises the provenance-gated MODIFY path.
            state["authorized_mutation"] = ["src/components", "index.html", "src"]
            decision = deterministic_manifest_completion_diagnostics(plan, coverage, directory, state)
            hero_candidate = next(item for item in decision["candidate_diagnostics"] if item["path"] == "src/components/Hero.tsx")
            self.assertEqual("modify", hero_candidate["intended_operation"])
            self.assertEqual("AUTHORIZED", hero_candidate["authorization_result"])

    def test_stack_composite_is_semantic_not_a_missing_filesystem_artifact(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            coverage = manifest_coverage(
                {"file_manifest": [{"path": "src/index.css", "action": "create"}],
                 "phases": [{"id": "p1", "goal": "Use Vite/React/TS/Tailwind/shadcn/Anime.js", "execution_mode": "IMPLEMENTATION"}]},
                {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertNotIn("Vite/React/TS/Tailwind/shadcn/Anime.js", coverage["missing_paths"])
            self.assertIn("Vite/React/TS/Tailwind/shadcn/Anime.js", coverage["semantic_requirements"])
            self.assertIn("TECHNOLOGY_REQUIREMENT", coverage["requirement_types"])

    def test_legacy_derived_manifest_does_not_promote_stack_composite_to_path(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            plan = {"phases": [{"id": "p1", "goal": (
                "Build Vite/React/TS/Tailwind/shadcn/Anime.js with index.html "
                "src/main.tsx src/App.tsx src/index.css"), "execution_mode": "IMPLEMENTATION"}]}
            prepared, artifact = prepare_implementation_plan(
                "legacy-composite", plan, directory,
                {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertNotIn("Vite/React/TS/Tailwind/shadcn/Anime.js",
                             [item["path"] for item in prepared["file_manifest"]])
            self.assertEqual("DERIVED", artifact["manifest_source"])

    def test_concrete_index_css_remains_required(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            coverage = manifest_coverage(
                {"file_manifest": [{"path": "index.html", "action": "create"}],
                 "phases": [{"id": "p1", "goal": "Build React Vite TypeScript Tailwind site", "execution_mode": "IMPLEMENTATION"}]},
                {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertIn("src/index.css", coverage["missing_paths"])

    def test_adequate_concrete_manifest_passes_without_claiming_dependencies(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            files = ["index.html", "src/main.tsx", "src/App.tsx", "src/index.css", "package.json", "tsconfig.json"]
            coverage = manifest_coverage(
                {"file_manifest": [{"path": path, "action": "create"} for path in files],
                 "phases": [{"id": "p1", "goal": "Build React Vite TypeScript Tailwind Anime.js site", "execution_mode": "IMPLEMENTATION"}]},
                {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertTrue(coverage["valid"])
            self.assertNotEqual("SATISFIED", coverage["dependency_validation"]["status"])

    def test_manifest_coverage_message_is_distinct_from_path_validation(self):
        message = manifest_coverage_message()
        self.assertIn("成果物が不足", message)
        self.assertIn("計画を再生成します", message)
        self.assertNotIn("ファイルパスを検証", message)

    def test_manifest_repair_progress_detects_schema_valid_identical_missing_set(self):
        before = {"valid": False, "missing_paths": ["index.html", "src/App.tsx"],
                  "manifest_paths": ["package.json"]}
        after = {"valid": False, "missing_paths": ["index.html", "src/App.tsx"],
                 "manifest_paths": ["package.json", "README.md"]}
        progress = manifest_repair_progress(before, after)
        self.assertEqual("REPAIR_NO_CHANGE", progress["classification"])
        self.assertFalse(progress["missing_reduced"])
        self.assertTrue(progress["manifest_changed"])
        self.assertIn("自動修復できなかった", manifest_repair_no_progress_message())

    def test_deterministic_manifest_completion_requires_authorized_greenfield_scope(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state.update({"new_project_intent": True,
                          "authorized_mutation": ["index.html", "src", "package.json", "tsconfig.json"]})
            plan = {"file_manifest": [{"path": "package.json", "action": "create"},
                                      {"path": "tsconfig.json", "action": "create"}],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Build React Vite TypeScript Tailwind site"}]}
            coverage = manifest_coverage(plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            candidate, additions = deterministic_manifest_completion(plan, coverage, directory, state)
            self.assertEqual(["index.html", "src/App.tsx", "src/index.css", "src/main.tsx"], additions)
            prepared, artifact = prepare_implementation_plan("deterministic", candidate, directory,
                {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertTrue((artifact.get("manifest_coverage") or {}).get("valid"))
            self.assertEqual(6, artifact["file_manifest_count"])
            self.assertTrue(Path(directory, "src", "App.tsx").exists())

    def test_deterministic_manifest_completion_declines_without_mutation_contract(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            plan = {"file_manifest": [{"path": "package.json", "action": "create"}],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Build React Vite TypeScript Tailwind site"}]}
            coverage = manifest_coverage(plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertEqual((None, []), deterministic_manifest_completion(plan, coverage, directory, state))

    def test_dependency_installation_requires_package_manifest_artifact(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state.update({"new_project_intent": True, "host_workspace_authorized": True,
                          "workspace_state": "EMPTY_GREENFIELD"})
            plan = {"file_manifest": [{"path": path, "action": "create"} for path in
                                       ("index.html", "src/main.tsx", "src/App.tsx", "src/index.css")],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Initialize the Vite project and install dependencies for React, TypeScript, Tailwind, shadcn, and Anime.js"}]}
            coverage = manifest_coverage(plan, {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertIn("package.json", coverage["missing_paths"])
            self.assertTrue(coverage["dependency_installation_required"])
            candidate, additions = deterministic_manifest_completion(plan, coverage, directory, state)
            self.assertIn("package.json", additions)
            self.assertEqual("create", next(item["action"] for item in candidate["file_manifest"] if item["path"] == "package.json"))

    def test_replan_reconciles_current_task_scaffold_create_to_modify(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            Path(directory, "index.html").write_text("<div>current task scaffold</div>", encoding="utf-8")
            plan = {"file_manifest": [{"path": "index.html", "action": "create", "owner_phase": "major-blueprint"}],
                    "phases": [{"id": "major-blueprint", "execution_mode": "VERIFICATION_ONLY", "goal": "blueprint"}]}
            prepared, artifact = prepare_implementation_plan(
                "replan", plan, directory,
                {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"},
                {"greenfield": True, "new_project_intent": True,
                 "replan_reconciliation": True,
                 "current_task_created_paths": ["index.html"],
                 "current_task_provenance": {"index.html": "CREATED_BY_CURRENT_TASK_SCAFFOLD"}})
            self.assertEqual("modify", prepared["file_manifest"][0]["action"])
            self.assertEqual(["index.html"], artifact["reconciled_create_to_modify"])

    def test_replan_keeps_preexisting_create_collision_rejected(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            Path(directory, "index.html").write_text("preexisting", encoding="utf-8")
            plan = {"file_manifest": [{"path": "index.html", "action": "create"}],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION", "goal": "implement"}]}
            with self.assertRaises(PlanPathValidationError):
                prepare_implementation_plan(
                    "collision", plan, directory,
                    {"task_profile": "GENERAL_CODING"},
                    {"greenfield": True, "new_project_intent": True,
                     "replan_reconciliation": True,
                     "current_task_created_paths": [],
                     "current_task_provenance": {"index.html": "PREEXISTING_BEFORE_TASK"}})

    def test_vite_stack_rejects_next_router_layout_before_manifest_or_scaffold(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state.update({"new_project_intent": True, "host_workspace_authorized": True,
                          "workspace_state": "EMPTY_GREENFIELD"})
            requirements = {"task_profile": "FRONTEND_ONLY_MARKETING_SITE",
                            "required_stack": ["Vite", "React", "TypeScript", "Tailwind", "shadcn", "Anime.js v4"],
                            "forbidden_stack": ["Next.js", "GSAP", "Framer Motion", "Three.js"]}
            plan = {"file_manifest": [{"path": "vite.config.ts", "action": "create"},
                                      {"path": "app/page.tsx", "action": "create"},
                                      {"path": "app/layout.tsx", "action": "create"}],
                    "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                                 "goal": "Implement the Vite React landing page"}]}
            result = stack_conformance(plan, requirements)
            self.assertFalse(result["valid"])
            self.assertIn("APP_ROUTER_LAYOUT_PAIR", result["observed_stack_signals"])
            self.assertTrue(any("Vite bootstrap" in item for item in result["stack_conflicts"]))
            with self.assertRaises(PlanStackConformanceError):
                prepare_implementation_plan("stack-conflict", plan, directory, requirements, state)
            self.assertFalse(Path(directory, "app", "page.tsx").exists())

    def test_vite_stack_allows_harmless_app_directory_with_bootstrap(self):
        requirements = {"task_profile": "FRONTEND_ONLY_MARKETING_SITE",
                        "required_stack": ["Vite", "React", "TypeScript"],
                        "forbidden_stack": ["Next.js"]}
        plan = {"file_manifest": [{"path": path, "action": "create"} for path in
                                  ("index.html", "src/main.tsx", "src/App.tsx", "src/index.css", "app/content.tsx")],
                "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                             "goal": "Implement Vite React UI using an app content directory"}]}
        result = stack_conformance(plan, requirements)
        self.assertTrue(result["valid"])
        self.assertIn("APP_DIRECTORY", result["observed_stack_signals"])

    def test_vite_stack_rejects_next_dependency_metadata(self):
        requirements = {"task_profile": "FRONTEND_ONLY_MARKETING_SITE",
                        "required_stack": ["Vite", "React", "TypeScript"],
                        "forbidden_stack": ["Next.js"]}
        plan = {"file_manifest": [{"path": "package.json", "action": "create",
                                    "content": {"dependencies": {"next": "latest"}}}],
                "phases": [{"id": "p1", "execution_mode": "IMPLEMENTATION",
                             "goal": "Implement the landing page"}]}
        result = stack_conformance(plan, requirements)
        self.assertFalse(result["valid"])
        self.assertIn("NEXT_JS_SIGNAL", result["observed_stack_signals"])

    def test_canonical_stack_contract_is_explicit_for_frontend_profile(self):
        contract = canonical_stack_contract({"task_profile": "FRONTEND_ONLY_MARKETING_SITE"})
        self.assertEqual(["Vite", "React", "TypeScript", "Tailwind", "shadcn", "Anime.js v4"], contract["required"])
        self.assertIn("Next.js", contract["forbidden"])

    def test_coverage_failure_happens_before_scaffold(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            state = workspace_state(directory)
            state["new_project_intent"] = True
            plan = {"file_manifest": [{"path": "package.json", "action": "create"}],
                    "phases": [{"id": "p1", "goal": "Build React Vite TypeScript Tailwind site", "execution_mode": "IMPLEMENTATION"}]}
            with self.assertRaises(PlanManifestCoverageError) as caught:
                prepare_implementation_plan("coverage-order", plan, directory,
                                            {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)
            self.assertIn("src/index.css", caught.exception.missing)
            self.assertFalse(Path(directory, "package.json").exists())

    def test_greenfield_state_must_be_explicit_for_missing_modify(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            with self.assertRaises(PlanPathValidationError) as caught:
                prepare_implementation_plan(
                    "task", {"phases": [], "file_manifest": [{"path": "src/main.tsx", "action": "modify"}]}, directory,
                    {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"})
            self.assertEqual("MISSING_MODIFY_TARGET", caught.exception.diagnostics[0]["path_validation_code"])

    def test_partially_initialized_workspace_keeps_missing_modify_strict(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            Path(directory, "package.json").write_text("{}\n", encoding="utf-8")
            state = workspace_state(directory)
            self.assertEqual("PARTIALLY_INITIALIZED", state["project_state"])
            state["new_project_intent"] = True
            with self.assertRaises(PlanPathValidationError):
                prepare_implementation_plan(
                    "task", {"phases": [], "file_manifest": [{"path": "src/main.tsx", "action": "modify"}]}, directory,
                    {"task_profile": "FRONTEND_ONLY_MARKETING_SITE"}, state)

    def test_greenfield_create_is_allowed_inside_root(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            _, artifact = prepare_implementation_plan(
                "task", {"phases": [], "file_manifest": [{"path": "src/main.tsx", "action": "create"}]}, directory)
            self.assertEqual(1, artifact["file_manifest_create_count"])

    def test_derived_missing_proper_noun_is_not_added_to_mutable_manifest(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            prepared, artifact = prepare_implementation_plan(
                "task",
                {"phases": [{"id": "p1", "goal": "Use Anime.js for the hero animation"}]},
                directory,
            )
            self.assertEqual([], prepared["file_manifest"])
            self.assertEqual("DERIVED", artifact["manifest_source"])

    def test_existing_bare_capitalized_file_remains_a_valid_modify_target(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            target = Path(directory, "App.js")
            target.write_text("export default {}\n", encoding="utf-8")
            prepared, artifact = prepare_implementation_plan(
                "task",
                {"phases": [], "file_manifest": [{"path": "App.js", "action": "modify"}]},
                directory,
            )
            self.assertEqual(["App.js"], [item["path"] for item in prepared["file_manifest"]])
            self.assertEqual(1, artifact["file_manifest_modify_count"])

    def test_read_only_manifest_entries_are_kept_as_evidence_and_never_mutated(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            prepared, artifact = prepare_implementation_plan(
                "task",
                {"phases": [], "file_manifest": [
                    {"path": "../olcr/backend/olcr_api/app.py", "action": "modify", "role": "read_only"},
                    {"path": "index.html", "action": "create"},
                ]},
                directory,
            )
            self.assertEqual(["../olcr/backend/olcr_api/app.py"], [x["path"] for x in artifact["read_only_manifest"]])
            self.assertEqual(["index.html"], [x["path"] for x in prepared["file_manifest"]])
            self.assertFalse(Path(directory, "..", "olcr", "backend", "olcr_api", "app.py").exists())

    def test_modify_requires_existing_file_and_does_not_truncate(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            target = Path(directory, "existing.ts")
            target.write_text("const preserved = true;\n")
            _, artifact = prepare_implementation_plan("task", {"phases": [], "file_manifest": [{"path": "existing.ts", "action": "modify"}]}, directory)
            self.assertEqual(1, artifact["file_manifest_modify_count"])
            self.assertEqual("const preserved = true;\n", target.read_text())

    def test_non_empty_create_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            target = Path(directory, "index.html")
            target.write_text("existing")
            with self.assertRaises(ValueError):
                prepare_implementation_plan("task", {"phases": [], "file_manifest": [{"path": "index.html", "action": "create"}]}, directory)

    def test_managed_project_root_is_authorized_even_when_global_retrieval_root_differs(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as global_directory, tempfile.TemporaryDirectory(dir="/private/tmp") as project_directory:
            db = Database(str(Path(global_directory, "olcr.db")))
            db.initialize()
            class Model:
                def generate(self, *_args, **_kwargs):
                    return {"text": json.dumps({"operations": [{"op": "write", "path": "index.html", "content": "<h1>ok</h1>"}]}), "latency_ms": 1, "prompt_tokens": 1, "completion_tokens": 1}
            retrieval = RetrievalRouter(FileRetriever([global_directory]), FTSRetriever(db), DisabledVectorStore(), False)
            runtime = Runtime(Settings(allowed_roots=(global_directory,), db_path=db.path, main_model="mock").validated(), db, retrieval, Model())
            task, _ = runtime.execute("Implement the requested project", workspace_root=project_directory,
                                     managed_context={"managed_coding_task": True, "operation_intent": "IMPLEMENTATION"})
            self.assertEqual("completed", task.state.value)
            self.assertEqual("<h1>ok</h1>", Path(project_directory, "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
