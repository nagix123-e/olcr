import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.coding_tasks import (
    bound_oversized_plan_phases,
    deterministic_phase_slicing,
    normalize_automatic_replan_phases,
)
from olcr_api.db import Database
from olcr_api.implementation_plan import concrete_artifact_contract


class RecoveryPhaseSplitNormalizationTests(unittest.TestCase):
    def current_plan(self):
        return {
            "schema_version": 1,
            "original_goal": "Build the requested site and verify it",
            "scope": {"allowed": ["src/App.tsx", "src/sections.tsx"], "forbidden": []},
            "assumptions": [],
            "max_retries_per_phase": 2,
            "requires_user_approval": True,
            "blueprint": {"source": "host persisted"},
            "phases": [
                {"id": "major-blueprint", "kind": "ORCHESTRATOR_BLUEPRINT", "status": "pass",
                 "goal": "Repository context and implementation blueprint",
                 "done": ["The implementation blueprint is persisted"], "verify": [],
                 "dependencies": [], "risks": [], "execution_mode": "IMPLEMENTATION"},
                {"id": "major-1", "status": "pending", "goal": "Implement all sections",
                 "done": ["The shared app shell is complete", "All requested sections are complete"],
                 "verify": ["The production build succeeds"], "dependencies": ["major-blueprint"],
                 "risks": [], "execution_mode": "IMPLEMENTATION_AND_VERIFICATION"},
                {"id": "major-2", "status": "pending", "goal": "Verify the rendered site",
                 "done": ["The requested interactions are checked"],
                 "verify": ["The rendered site has all sections"], "dependencies": ["major-1"],
                 "risks": [], "execution_mode": "VERIFICATION_ONLY"},
            ],
            "file_manifest": [
                {"path": "src/App.tsx", "action": "modify", "role": "app shell",
                 "required": True, "owner_phase": "major-1"},
                {"path": "src/sections.tsx", "action": "modify", "role": "sections",
                 "required": True, "owner_phase": "major-1"},
            ],
        }

    def split_plan(self):
        return {
            "schema_version": 1,
            "original_goal": "Build the requested site and verify it",
            "scope": {"allowed": ["src/App.tsx", "src/sections.tsx"], "forbidden": []},
            "assumptions": [],
            "phases": [
                {"id": "slice-shell", "status": "pending", "goal": "Complete the shared app shell",
                 "done": ["The shared app shell is complete"], "verify": [], "dependencies": [],
                 "risks": [], "execution_mode": "IMPLEMENTATION"},
                {"id": "slice-sections", "status": "pending", "goal": "Complete the requested sections",
                 "done": ["All requested sections are complete"], "verify": ["The production build succeeds"],
                 "dependencies": ["slice-shell"], "risks": [],
                 "execution_mode": "IMPLEMENTATION_AND_VERIFICATION"},
                {"id": "major-2", "status": "pending", "goal": "Verify the rendered site",
                 "done": ["The requested interactions are checked"],
                 "verify": ["The rendered site has all sections"], "dependencies": ["slice-sections"],
                 "risks": [], "execution_mode": "VERIFICATION_ONLY"},
            ],
            "file_manifest": [
                {"path": "src/App.tsx", "action": "modify", "role": "app shell",
                 "required": True, "owner_phase": "slice-shell"},
                {"path": "src/sections.tsx", "action": "modify", "role": "sections",
                 "required": True, "owner_phase": "slice-sections"},
            ],
        }

    def test_authorized_split_preserves_completed_work_criteria_manifest_and_dependencies(self):
        current = self.current_plan()
        proposed = self.split_plan()
        result, changes = normalize_automatic_replan_phases(
            proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
            verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)

        self.assertEqual(["major-1", "slice-sections", "major-2"],
                         [phase["id"] for phase in result["phases"]])
        self.assertEqual({"The shared app shell is complete", "All requested sections are complete",
                          "The requested interactions are checked"},
                         {criterion for phase in result["phases"] for criterion in phase["done"]})
        self.assertEqual({"The production build succeeds", "The rendered site has all sections"},
                         {criterion for phase in result["phases"] for criterion in phase["verify"]})
        downstream = next(phase for phase in result["phases"] if phase["id"] == "major-2")
        self.assertEqual(["Verify the rendered site"], [downstream["goal"]])
        failing = next(phase for phase in result["phases"] if phase["id"] == "major-1")
        self.assertEqual(["major-blueprint"], failing["dependencies"])
        self.assertEqual({"major-1", "slice-sections"}, set(downstream["dependencies"]))
        owners = {entry["path"]: entry["owner_phase"] for entry in result["file_manifest"]}
        self.assertEqual({"src/App.tsx": "major-1", "src/sections.tsx": "slice-sections"}, owners)
        self.assertEqual(["major-1-task"], result["tasks"][1]["depends_on"])
        self.assertIn("accepted_split_phase:slice-sections", changes)
        self.assertEqual(current["blueprint"], result["blueprint"])

    def test_split_restores_omitted_unfinished_prerequisite_and_failed_phase_edge(self):
        current = self.current_plan()
        prerequisite = {
            "id": "major-prereq", "status": "pending", "goal": "Prepare the shared data model",
            "done": ["The shared data model is ready"], "verify": [],
            "dependencies": ["major-blueprint"], "risks": [], "execution_mode": "IMPLEMENTATION",
        }
        current["phases"].insert(1, prerequisite)
        current["phases"][2]["dependencies"] = ["major-blueprint", "major-prereq"]
        result, _ = normalize_automatic_replan_phases(
            self.split_plan(), current, {"major-blueprint"}, preferred_phase_id="major-1",
            verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)
        failing = next(phase for phase in result["phases"] if phase["id"] == "major-1")
        restored = next(phase for phase in result["phases"] if phase["id"] == "major-prereq")
        self.assertEqual(["major-blueprint", "major-prereq"], failing["dependencies"])
        self.assertEqual("Prepare the shared data model", restored["goal"])

    def test_approved_manifest_split_reassigns_only_failing_phase_paths(self):
        mutation_phase_ids = {"major-1", "slice-sections", "major-2"}
        self.assertEqual("slice-sections", api._approved_manifest_replan_owner(
            "major-1", "slice-sections", mutation_phase_ids,
            path_was_approved=True, recovery_split_allowed=True,
            failing_phase_id="major-1", split_phase_ids={"major-1", "slice-sections"},
        ))
        for prior_owner in ("major-2", "major-blueprint"):
            with self.subTest(prior_owner=prior_owner):
                self.assertEqual(prior_owner, api._approved_manifest_replan_owner(
                    prior_owner, "slice-sections", mutation_phase_ids,
                    path_was_approved=True, recovery_split_allowed=True,
                    failing_phase_id="major-1", split_phase_ids={"major-1", "slice-sections"},
                ))
        self.assertEqual("slice-sections", api._approved_manifest_replan_owner(
            "", "slice-sections", mutation_phase_ids,
            path_was_approved=False, recovery_split_allowed=True,
            failing_phase_id="major-1", split_phase_ids={"major-1", "slice-sections"},
        ))

    def test_authorized_split_rejects_dropped_existing_acceptance_criterion(self):
        current = self.current_plan()
        proposed = self.split_plan()
        proposed["phases"][1]["verify"] = []
        with self.assertRaisesRegex(ValueError, "omitted existing"):
            normalize_automatic_replan_phases(
                proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
                verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)

    def test_split_can_add_a_scoped_checkpoint_when_new_slice_has_no_prior_done_criterion(self):
        current = self.current_plan()
        current["phases"][1]["done"] = ["The shared app shell is complete"]
        proposed = self.split_plan()
        proposed["phases"][1]["done"] = ["The assigned section files are implemented"]
        result, _ = normalize_automatic_replan_phases(
            proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
            verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)
        self.assertEqual({"The shared app shell is complete", "The assigned section files are implemented",
                          "The requested interactions are checked"},
                         {item for phase in result["phases"] for item in phase["done"]})

    def test_split_rejects_new_checkpoint_on_an_existing_phase(self):
        current = self.current_plan()
        proposed = self.split_plan()
        proposed["phases"][0]["done"].append("An unrelated checkpoint was added")
        with self.assertRaisesRegex(ValueError, "outside a new slice"):
            normalize_automatic_replan_phases(
                proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
                verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)

    def test_split_rejects_a_slice_that_exceeds_the_criteria_bound(self):
        current = self.current_plan()
        current["phases"][1]["done"] = [f"Requirement {index} exists" for index in range(7)]
        proposed = self.split_plan()
        proposed["phases"][0]["done"] = current["phases"][1]["done"][:6]
        proposed["phases"][0]["verify"] = ["The production build succeeds"]
        proposed["phases"][1]["done"] = current["phases"][1]["done"][6:]
        proposed["phases"][1]["verify"] = []
        with self.assertRaisesRegex(ValueError, "per-slice acceptance-criteria bound"):
            normalize_automatic_replan_phases(
                proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
                verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)

    def test_authorized_split_rejects_unrelated_phase_without_prior_criteria_or_paths(self):
        current = self.current_plan()
        proposed = self.split_plan()
        proposed["phases"].append({
            "id": "unrelated-cleanup", "status": "pending", "goal": "Clean up unrelated files",
            "done": [], "verify": [], "dependencies": ["slice-sections"], "risks": [],
            "execution_mode": "IMPLEMENTATION",
        })
        with self.assertRaisesRegex(ValueError, "unrelated to the failing deliverable"):
            normalize_automatic_replan_phases(
                proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
                verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)

    def test_authorized_split_cannot_move_manifest_ownership_from_other_phases(self):
        for owner in ("major-2", "major-blueprint"):
            with self.subTest(owner=owner):
                current = self.current_plan()
                path = f"src/{owner}.tsx"
                current["file_manifest"].append({"path": path, "action": "modify", "role": owner,
                                                 "required": True, "owner_phase": owner})
                proposed = self.split_plan()
                proposed["file_manifest"].append({"path": path, "action": "modify", "role": owner,
                                                  "required": True, "owner_phase": "slice-sections"})
                with self.assertRaisesRegex(ValueError, "unaffected manifest owner"):
                    normalize_automatic_replan_phases(
                        proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
                        verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)

    def test_authorized_split_requires_new_slice_to_follow_the_failing_phase(self):
        current = self.current_plan()
        proposed = self.split_plan()
        proposed["phases"][1]["dependencies"] = []
        with self.assertRaisesRegex(ValueError, "dependency-linked to the failing phase"):
            normalize_automatic_replan_phases(
                proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
                verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)

    def test_authorized_split_counts_completed_prefix_against_total_phase_limit(self):
        current = self.current_plan()
        proposed = self.split_plan()
        proposed["phases"].append({
            "id": "slice-extra", "status": "pending", "goal": "An extra implementation slice",
            "done": ["An extra part is complete"], "verify": [], "dependencies": ["slice-sections"],
            "risks": [], "execution_mode": "IMPLEMENTATION",
        })
        with self.assertRaisesRegex(ValueError, "total phase limit"):
            normalize_automatic_replan_phases(
                proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
                verification_requested=True, allow_additional_phases=True, max_total_phase_count=3)

    def test_extra_phase_remains_visible_for_host_rejection_without_split_authorization(self):
        current = self.current_plan()
        proposed = self.split_plan()
        result, _ = normalize_automatic_replan_phases(
            proposed, current, {"major-blueprint"}, preferred_phase_id="major-1",
            verification_requested=True)
        self.assertIn("slice-sections", [phase["id"] for phase in result["phases"]])
        self.assertTrue(any("unapproved_additional_phase_pending_host_rejection" in change
                            for change in _))

    def test_host_slicing_bounds_large_failed_phase_without_a_planner(self):
        current = self.current_plan()
        failing = current["phases"][1]
        failing["done"] = [f"Requirement {index} is implemented" for index in range(20)]
        failing["verify"] = [f"Check {index} passes" for index in range(5)]
        current["file_manifest"] = [
            {"path": f"src/generated_{index}.ts", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(33)
        ]

        sliced = deterministic_phase_slicing(
            current, "major-1", completed_phase_ids={"major-blueprint"}, max_total_phase_count=8)

        self.assertIsNotNone(sliced)
        result, changes = sliced
        slices = [phase for phase in result["phases"] if phase["id"].startswith("major-1")]
        self.assertEqual(6, len(slices))
        self.assertTrue(all(len(phase["done"]) + len(phase["verify"]) <= 6 for phase in slices))
        self.assertTrue(all(sum(1 for entry in result["file_manifest"]
                                if entry["owner_phase"] == phase["id"]
                                and entry["action"] in {"create", "modify", "delete"}) <= 6
                            for phase in slices))
        original_criteria = Counter(
            [("done", f"Requirement {index} is implemented") for index in range(20)]
            + [("verify", f"Check {index} passes") for index in range(5)])
        sliced_criteria = Counter((field, item) for phase in slices for field in ("done", "verify")
                                  for item in phase[field])
        self.assertTrue(all(sliced_criteria[key] == count for key, count in original_criteria.items()))
        self.assertEqual(26, sum(len(phase["done"]) + len(phase["verify"]) for phase in slices))
        self.assertEqual(33, sum(1 for entry in result["file_manifest"] if entry["owner_phase"] in
                                 {phase["id"] for phase in slices}))
        downstream = next(phase for phase in result["phases"] if phase["id"] == "major-2")
        self.assertEqual({phase["id"] for phase in slices}, set(downstream["dependencies"]))
        self.assertEqual(len(result["phases"]), len(result["tasks"]))
        self.assertIn("deterministic_phase_slicing", changes)

        normalized, _ = normalize_automatic_replan_phases(
            result, current, {"major-blueprint"}, preferred_phase_id="major-1",
            verification_requested=True, allow_additional_phases=True, max_total_phase_count=8)
        # The accepted normalizer folds the completed orchestrator blueprint
        # into its persisted blueprint artifact, leaving six slices + one
        # downstream phase in the active phase graph.
        self.assertEqual(7, len(normalized["phases"]))

    def test_host_slicing_returns_none_when_plan_phase_cap_cannot_fit_slices(self):
        current = self.current_plan()
        current["phases"][1]["done"] = [f"Requirement {index}" for index in range(13)]
        current["file_manifest"] = [
            {"path": f"src/file_{index}.ts", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(13)
        ]
        self.assertIsNone(deterministic_phase_slicing(
            current, "major-1", completed_phase_ids={"major-blueprint"}, max_total_phase_count=4))

    def test_initial_plan_is_proactively_bounded_before_any_model_implementation_call(self):
        current = self.current_plan()
        current["phases"][1]["done"] = [f"Requirement {index}" for index in range(20)]
        current["phases"][1]["verify"] = [f"Check {index}" for index in range(5)]
        current["file_manifest"] = [
            {"path": f"src/file_{index}.ts", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(33)
        ]
        bounded, changes = bound_oversized_plan_phases(current)
        self.assertEqual(8, len(bounded["phases"]))
        self.assertEqual(8, len(bounded["tasks"]))
        self.assertTrue(all(len(phase["done"]) + len(phase["verify"]) <= 6
                            for phase in bounded["phases"] if phase["id"].startswith("major-1")))
        self.assertTrue(all(sum(1 for item in bounded["file_manifest"]
                                if item.get("owner_phase") == phase["id"]
                                and item.get("action") in {"create", "modify", "delete"}) <= 6
                            for phase in bounded["phases"] if phase["id"].startswith("major-1")))
        self.assertIn("deterministic_phase_slicing", changes)

    def test_global_verification_contract_runs_only_after_all_implementation_slices(self):
        current = self.current_plan()
        phase = current["phases"][1]
        phase["execution_mode"] = "IMPLEMENTATION_AND_VERIFICATION"
        phase["done"] = [f"Requirement {index} is implemented" for index in range(13)]
        phase["verify"] = ["The complete production build succeeds"]
        phase["verification_command"] = ["npm", "run", "build"]
        phase["verification_targets"] = ["package.json", "src/App.tsx"]
        phase["verification_executor"] = "READ_ONLY_VERIFICATION"
        phase["acceptance_contract"] = ["FRONTEND_STACK", "MARKETING_STRUCTURE"]
        phase["required_mcp"] = ["animejs", "playwright"]
        phase["selected_mcp"] = ["animejs", "playwright"]
        phase["host_project_test_defect"] = True
        phase["host_project_test_repair_paths"] = ["src/App.tsx"]
        phase["host_current_command_failure"] = True
        phase["host_current_command_evidence"] = {"returncode": 1}
        phase["criterion_executors"] = {
            "DONE:1": "IMPLEMENTER", "DONE:13": "IMPLEMENTER",
            "VERIFY:1": "READ_ONLY_VERIFICATION",
        }
        current["file_manifest"] = [
            {"path": f"src/file_{index}.ts", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(13)
        ]

        sliced = deterministic_phase_slicing(current, "major-1", {"major-blueprint"})

        self.assertIsNotNone(sliced)
        result, _ = sliced
        phases = [phase for phase in result["phases"] if phase["id"].startswith("major-1")]
        self.assertEqual(3, len(phases))
        self.assertTrue(all(phase["execution_mode"] == "IMPLEMENTATION" for phase in phases[:-1]))
        self.assertTrue(all(phase["verify"] == [] and "verification_command" not in phase
                            and "verification_targets" not in phase and "verification_executor" not in phase
                            for phase in phases[:-1]))
        self.assertTrue(all("acceptance_contract" not in phase for phase in phases[:-1]))
        self.assertTrue(all("playwright" not in phase.get("required_mcp", [])
                            and "playwright" not in phase.get("selected_mcp", []) for phase in phases[:-1]))
        self.assertTrue(all("host_project_test_defect" not in phase
                            and "host_current_command_failure" not in phase for phase in phases[:-1]))
        self.assertEqual(["The complete production build succeeds"], phases[-1]["verify"])
        self.assertEqual(["npm", "run", "build"], phases[-1]["verification_command"])
        self.assertEqual(["package.json", "src/App.tsx"], phases[-1]["verification_targets"])
        self.assertEqual("READ_ONLY_VERIFICATION", phases[-1]["verification_executor"])
        self.assertEqual("IMPLEMENTATION_AND_VERIFICATION", phases[-1]["execution_mode"])
        self.assertEqual(["FRONTEND_STACK", "MARKETING_STRUCTURE"], phases[-1]["acceptance_contract"])
        self.assertIn("playwright", phases[-1]["required_mcp"])
        self.assertIn("playwright", phases[-1]["selected_mcp"])
        self.assertEqual({phase["id"] for phase in phases},
                         set(next(p for p in result["phases"] if p["id"] == "major-2")["dependencies"]))
        self.assertEqual("IMPLEMENTER", phases[0]["criterion_executors"]["DONE:1"])
        self.assertEqual("IMPLEMENTER", phases[-1]["criterion_executors"]["DONE:1"])
        self.assertEqual("READ_ONLY_VERIFICATION", phases[-1]["criterion_executors"]["VERIFY:1"])

    def test_initial_bounder_fails_closed_when_phase_cap_cannot_fit_split(self):
        current = self.current_plan()
        current["phases"][1]["done"] = [f"Requirement {index}" for index in range(13)]
        current["file_manifest"] = [
            {"path": f"src/file_{index}.ts", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(13)
        ]

        bounded, changes = bound_oversized_plan_phases(current, max_total_phase_count=4)

        self.assertEqual(current, bounded)
        self.assertEqual(1, len(changes))
        self.assertEqual(
            "phase_slicing_blocked:major-1:host_phase_limit_exceeded(required=5,max=4)",
            changes[0])

    def test_initial_bounder_splits_large_read_only_verification_phase(self):
        current = self.current_plan()
        current["phases"][2]["done"] = ["The requested interactions are checked"]
        current["phases"][2]["verify"] = [f"Browser check {index} passes" for index in range(7)]
        current["file_manifest"] = [
            {"path": f"src/file_{index}.ts", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(7)
        ]

        bounded, changes = bound_oversized_plan_phases(current)

        self.assertIn("deterministic_phase_slicing", changes)
        verification_slices = [phase for phase in bounded["phases"]
                               if phase.get("execution_mode") == "VERIFICATION_ONLY"]
        self.assertGreaterEqual(len(verification_slices), 2)
        self.assertTrue(all(len(phase["done"]) + len(phase["verify"]) <= 6
                            for phase in verification_slices))
        self.assertEqual(["The requested interactions are checked"],
                         [criterion for phase in verification_slices for criterion in phase["done"]
                          if criterion == "The requested interactions are checked"])
        self.assertCountEqual([f"Browser check {index} passes" for index in range(7)],
                              [criterion for phase in verification_slices for criterion in phase["verify"]])
        self.assertEqual([], api.validate_plan(bounded, bounded["original_goal"]))

    def test_initial_bounder_keeps_verify_criteria_on_every_generated_verification_slice(self):
        current = self.current_plan()
        current["phases"][2]["done"] = [f"Read-only criterion {index} is satisfied" for index in range(13)]
        current["phases"][2]["verify"] = [f"Browser criterion {index} passes" for index in range(3)]

        bounded, changes = bound_oversized_plan_phases(current)

        self.assertIn("deterministic_verification_slicing", changes)
        verification_slices = [phase for phase in bounded["phases"]
                               if phase.get("execution_mode") == "VERIFICATION_ONLY"]
        self.assertEqual(3, len(verification_slices))
        self.assertTrue(all(phase.get("verify") for phase in verification_slices))
        self.assertCountEqual(current["phases"][2]["done"],
                              [criterion for phase in verification_slices for criterion in phase["done"]])
        self.assertCountEqual(current["phases"][2]["verify"],
                              [criterion for phase in verification_slices for criterion in phase["verify"]])
        self.assertEqual([], api.validate_plan(bounded, bounded["original_goal"]))

    def test_initial_bounder_counts_synthetic_done_slots_against_verification_slice_capacity(self):
        current = self.current_plan()
        current["phases"][2]["done"] = ["The requested interactions are checked"]
        current["phases"][2]["verify"] = [f"Browser check {index} passes" for index in range(20)]

        bounded, changes = bound_oversized_plan_phases(current)

        self.assertIn("deterministic_verification_slicing", changes)
        verification_slices = [phase for phase in bounded["phases"]
                               if phase.get("execution_mode") == "VERIFICATION_ONLY"]
        self.assertEqual(4, len(verification_slices))
        self.assertTrue(all(len(phase["done"]) + len(phase["verify"]) <= 6
                            for phase in verification_slices))
        self.assertCountEqual(current["phases"][2]["verify"],
                              [criterion for phase in verification_slices for criterion in phase["verify"]])

    def test_sparse_verify_criteria_are_reused_without_inheriting_implementer_executor(self):
        current = self.current_plan()
        verification = current["phases"][2]
        verification["done"] = [f"Read-only criterion {index} is satisfied" for index in range(13)]
        verification["verify"] = ["The production build succeeds"]
        verification["criterion_executors"] = {
            "DONE:1": "IMPLEMENTER",
            "VERIFY:1": "READ_ONLY_VERIFICATION",
        }

        bounded, changes = bound_oversized_plan_phases(current)

        self.assertIn("deterministic_verification_slicing", changes)
        verification_slices = [phase for phase in bounded["phases"]
                               if phase.get("execution_mode") == "VERIFICATION_ONLY"]
        self.assertEqual(3, len(verification_slices))
        self.assertTrue(all(phase["verify"] == ["The production build succeeds"]
                            for phase in verification_slices))
        self.assertTrue(all(phase.get("criterion_executors", {}).get("VERIFY:1")
                            == "READ_ONLY_VERIFICATION" for phase in verification_slices))

    def test_initial_bounder_removes_implementation_mcps_from_split_verification_phases(self):
        current = self.current_plan()
        verification = current["phases"][2]
        verification["done"] = ["The requested interactions are checked"]
        verification["verify"] = [f"Browser check {index} passes" for index in range(7)]
        verification["required_mcp"] = ["animejs", "playwright"]
        verification["selected_mcp"] = ["shadcn-ui", "playwright"]

        bounded, changes = bound_oversized_plan_phases(current)

        self.assertIn("deterministic_verification_slicing", changes)
        verification_slices = [phase for phase in bounded["phases"]
                               if phase.get("execution_mode") == "VERIFICATION_ONLY"]
        self.assertGreaterEqual(len(verification_slices), 2)
        self.assertTrue(all(phase.get("required_mcp") == ["playwright"]
                            for phase in verification_slices))
        self.assertTrue(all(phase.get("selected_mcp") == ["playwright"]
                            for phase in verification_slices))
        self.assertEqual([], api.validate_plan(
            bounded, bounded["original_goal"], max_phase_count=api.MAX_HOST_EXECUTION_PHASES))

    def test_implementation_split_sanitizes_only_its_generated_verifier_mcps(self):
        current = self.current_plan()
        implementation = current["phases"][1]
        implementation["done"] = [f"Requirement {index} is implemented" for index in range(13)]
        implementation["verify"] = [f"Global verification {index} passes" for index in range(6)]
        implementation["required_mcp"] = ["animejs", "playwright"]
        implementation["selected_mcp"] = ["shadcn", "playwright"]
        current["file_manifest"] = [
            {"path": f"src/file_{index}.tsx", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(13)
        ]

        bounded, changes = bound_oversized_plan_phases(current)

        self.assertIn("deterministic_phase_slicing", changes)
        implementation_slices = [phase for phase in bounded["phases"]
                                 if phase.get("id", "").startswith("major-1")
                                 and phase.get("execution_mode") != "VERIFICATION_ONLY"]
        verification_slices = [phase for phase in bounded["phases"]
                               if phase.get("id", "").startswith("major-1-verification")
                               and phase.get("execution_mode") == "VERIFICATION_ONLY"]
        self.assertTrue(implementation_slices)
        self.assertTrue(verification_slices)
        self.assertTrue(all(phase.get("required_mcp") == ["animejs"]
                            and phase.get("selected_mcp") == ["shadcn"]
                            for phase in implementation_slices))
        self.assertTrue(all(phase.get("required_mcp") == ["playwright"]
                            and phase.get("selected_mcp") == ["playwright"]
                            for phase in verification_slices))
        self.assertEqual([], api.validate_plan(bounded, bounded["original_goal"]))

    def test_host_phase_limit_also_bounds_the_derived_task_graph(self):
        plan = {
            "schema_version": 1,
            "original_goal": "Implement bounded host task slices",
            "scope": {"allowed": ["src/"], "forbidden": []},
            "assumptions": [], "max_retries_per_phase": 2,
            "requires_user_approval": True,
            "phases": [{
                "id": f"slice-{index}", "goal": f"Implement approved slice {index}",
                "status": "pending", "done": [f"Approved slice {index} is complete"],
                "verify": [], "dependencies": [] if index == 0 else [f"slice-{index - 1}"],
                "risks": [], "execution_mode": "IMPLEMENTATION",
            } for index in range(16)],
        }
        plan = api.derive_task_graph(plan)

        self.assertIn("phases must contain 1 to 8 entries", api.validate_plan(plan, plan["original_goal"]))
        self.assertEqual([], api.validate_plan(
            plan, plan["original_goal"], max_phase_count=api.MAX_HOST_EXECUTION_PHASES))

    def test_initial_bounder_moves_verification_after_mutation_when_done_slots_are_full(self):
        current = self.current_plan()
        current["phases"][1]["done"] = ["Implementation is complete"]
        current["phases"][1]["verify"] = [f"Global check {index}" for index in range(6)]
        current["file_manifest"] = [
            {"path": f"src/file_{index}.ts", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(13)
        ]

        bounded, changes = bound_oversized_plan_phases(current)

        self.assertIn("deterministic_phase_slicing", changes)
        mutation_slices = [phase for phase in bounded["phases"]
                           if phase["id"].startswith("major-1")
                           and phase.get("execution_mode") != "VERIFICATION_ONLY"]
        verification_slices = [phase for phase in bounded["phases"]
                               if phase.get("execution_mode") == "VERIFICATION_ONLY"]
        self.assertGreaterEqual(len(mutation_slices), 2)
        self.assertGreaterEqual(len(verification_slices), 2)
        self.assertTrue(all(phase["done"] for phase in mutation_slices + verification_slices))
        last_mutation_index = max(bounded["phases"].index(phase) for phase in mutation_slices)
        self.assertTrue(all(bounded["phases"].index(phase) > last_mutation_index
                            for phase in verification_slices))
        self.assertEqual([], api.validate_plan(bounded, bounded["original_goal"]))

    def test_planner_pass_status_does_not_suppress_initial_host_slicing(self):
        current = self.current_plan()
        current["phases"][1]["status"] = "pass"
        current["phases"][1]["done"] = [f"Requirement {index}" for index in range(13)]

        bounded, changes = bound_oversized_plan_phases(current)

        self.assertIn("deterministic_phase_slicing", changes)
        slices = [phase for phase in bounded["phases"] if phase["id"].startswith("major-1")]
        self.assertGreater(len(slices), 1)
        self.assertTrue(all(phase["status"] == "pending" for phase in bounded["phases"]))


class ArtifactCoverageAliasTests(unittest.TestCase):
    def test_generated_build_outputs_are_not_mutable_artifacts(self):
        plan = {
            "phases": [{"id": "build", "goal": "Build app", "done": [
                "npm run build succeeds and generates dist/index.html"], "verify": [],
                "execution_mode": "IMPLEMENTATION"}],
            "file_manifest": [{"path": "src/App.tsx", "action": "modify", "owner_phase": "build"}],
        }
        artifacts = concrete_artifact_contract(plan)["artifacts"]
        self.assertIn("src/App.tsx", artifacts)
        self.assertNotIn("dist/index.html", artifacts)

    def test_prose_aliases_resolve_only_to_unique_approved_manifest_paths(self):
        plan = {
            "phases": [{"id": "build", "goal": "Edit index.css and lib/motion.ts",
                        "done": ["motion.ts contains the animation helpers"], "verify": [],
                        "execution_mode": "IMPLEMENTATION"}],
            "file_manifest": [
                {"path": "src/index.css", "action": "modify", "owner_phase": "build"},
                {"path": "src/lib/motion.ts", "action": "modify", "owner_phase": "build"},
            ],
        }
        artifacts = concrete_artifact_contract(plan)["artifacts"]
        self.assertEqual({"src/index.css", "src/lib/motion.ts"}, set(artifacts))

    def test_ambiguous_prose_alias_does_not_choose_between_manifest_paths(self):
        plan = {
            "phases": [{"id": "build", "goal": "Update motion.ts", "done": [], "verify": [],
                        "execution_mode": "IMPLEMENTATION"}],
            "file_manifest": [
                {"path": "src/lib/motion.ts", "action": "modify", "owner_phase": "build"},
                {"path": "src/other/motion.ts", "action": "modify", "owner_phase": "build"},
            ],
        }
        artifacts = concrete_artifact_contract(plan)["artifacts"]
        self.assertIn("motion.ts", artifacts)
        self.assertIn("src/lib/motion.ts", artifacts)
        self.assertIn("src/other/motion.ts", artifacts)


class PhaseSplitPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temp.name)
        self.old_db = api.db
        api.db = Database(str(self.root / "phase-slicing.sqlite"))
        api.db.initialize()
        api.db.create_project("phase-slicing", str(self.root), time.time(), "phase-slicing")
        api.db.create_conversation("phase-slicing", time.time(), "phase-slicing", "phase-slicing")

    def tearDown(self):
        api.db = self.old_db
        self.temp.cleanup()

    def test_ineffective_replan_resume_queues_host_split_without_model_recovery_call(self):
        current = RecoveryPhaseSplitNormalizationTests().current_plan()
        current["phases"][1]["done"] = [f"Deliverable {index}" for index in range(13)]
        current["file_manifest"] = [
            {"path": f"src/file_{index}.ts", "action": "modify", "role": "implementation",
             "required": True, "owner_phase": "major-1"}
            for index in range(13)
        ]
        api.db.create_coding_task("ineffective-large", "phase-slicing", current["original_goal"],
                                  "WAITING_FOR_USER", "NONE", current, time.time())
        api.db.update_coding_task(
            "ineffective-large", current_phase_id="major-1",
            pending_authorization={"type": "REPLAN_INEFFECTIVE", "state": "PENDING"},
            pending_user_confirmation=1, recovery_reason="REPLAN_INEFFECTIVE")

        with patch.object(api._coding_scheduler_wake, "set") as wake:
            api._resume_coding_task_control(api.db.coding_task("ineffective-large"), source="TEST")

        saved = api.db.coding_task("ineffective-large")
        self.assertEqual("QUEUED", saved["status"])
        self.assertEqual(0, saved["task_total_recovery_model_calls"])
        self.assertNotIn("ineffective_replan_retry_requested", saved["resume_state"])
        self.assertEqual("major-1", saved["resume_state"]["ineffective_replan_split_requested"]["phase_id"])
        self.assertTrue(wake.called)

    def test_large_lean_planning_schema_allows_seven_bounded_phases(self):
        goal = "Build a multi-section website and run its production build."
        for mode in ("LEAN", "DELEGATED_DESIGN"):
            with self.subTest(mode=mode):
                task_id = f"large-phase-schema-{mode.lower()}"
                api.db.create_coding_task(task_id, "phase-slicing", goal, "PLANNING", "NONE", None, time.time())
                requirements = {"planning_policy": {"mode": mode},
                                "execution_policy": {"task_size": "LARGE"}}
                with patch.object(api, "_model_text", return_value=None) as model:
                    api._generate_plan(task_id, goal, "Plan the supplied specification.", "QWEN_PLANNING",
                                       requirements=requirements)
                schema = model.call_args.kwargs["structured_schema"]
                phase_schema = schema["properties"]["phases"]
                self.assertEqual(7, phase_schema["maxItems"])
                self.assertEqual(6, phase_schema["items"]["properties"]["done"]["maxItems"])
                self.assertIn("bounded implementation batches", model.call_args.args[4][1]["content"])

    def test_overloaded_phase_is_sliced_on_host_without_planner_call(self):
        goal = "Build the requested website, run npm test, and verify that the implementation works."
        plan = {
            "schema_version": 1, "original_goal": goal,
            "scope": {"allowed": ["src/App.tsx", "src/sections.tsx"], "forbidden": []},
            "assumptions": [], "max_retries_per_phase": 2, "requires_user_approval": True,
            "phases": [
                {"id": "major-blueprint", "kind": "ORCHESTRATOR_BLUEPRINT", "status": "pass",
                 "goal": "Persist repository context and blueprint", "done": ["Blueprint is persisted"],
                 "verify": [], "dependencies": [], "risks": [], "execution_mode": "IMPLEMENTATION"},
                {"id": "major-1", "status": "pending", "goal": "Implement the entire website",
                 "done": [f"Deliverable {i} exists" for i in range(12)],
                 "verify": [f"Check {i} passes" for i in range(5)], "dependencies": ["major-blueprint"],
                 "risks": [], "execution_mode": "IMPLEMENTATION_AND_VERIFICATION"},
                {"id": "major-2", "status": "pending", "goal": "Verify website behavior",
                 "done": ["The requested interactions are checked"],
                 "verify": ["The rendered site has all sections"],
                 "dependencies": ["major-1"], "risks": [], "execution_mode": "VERIFICATION_ONLY"},
            ],
            "file_manifest": [
                {"path": "src/App.tsx", "action": "modify", "role": "app", "required": True,
                 "owner_phase": "major-1"},
                {"path": "src/sections.tsx", "action": "modify", "role": "sections", "required": True,
                 "owner_phase": "major-1"},
            ],
        }
        task_id = "large-phase-replan"
        api.db.create_coding_task(task_id, "phase-slicing", goal, "QUEUED", "NONE", plan, time.time())
        api.db.update_coding_task(task_id, retry_count=2, max_retries=2)
        api.db.update_coding_task(task_id, requirements={
            "planning_policy": {"mode": "LEAN"}, "execution_policy": {"task_size": "MEDIUM"},
            "verification_requested": True,
        })
        diagnosis = {"unmet_done": ["Deliverable 10 exists", "Deliverable 11 exists"],
                     "unmet_verify": [f"Check {i} passes" for i in range(5)]}
        with patch.object(api, "_generate_plan", side_effect=AssertionError("host slicing must not call a planner")):
            outcome = api._replan_task(task_id, api.db.coding_task(task_id), {"major-blueprint"},
                                       "retry limit reached", diagnosis)
        self.assertEqual("Coding Task was replanned and requeued.", outcome)
        persisted = api.db.coding_task(task_id)["plan"]
        slices = [phase for phase in persisted["phases"] if phase["id"].startswith("major-1")]
        self.assertEqual(3, len(slices))
        self.assertTrue(all(len(phase["done"]) + len(phase["verify"]) <= 6 for phase in slices))
        self.assertTrue({f"Deliverable {i} exists" for i in range(12)} <=
                        {item for phase in slices for item in phase["done"]})
        self.assertEqual({f"Check {i} passes" for i in range(5)},
                         {item for phase in slices for item in phase["verify"]})

    def test_output_budget_failure_enables_split_before_retry_exhaustion(self):
        goal = "Build the requested website, run npm test, and verify that the implementation works."
        plan = RecoveryPhaseSplitNormalizationTests().current_plan()
        plan["phases"][1]["done"] = [f"Deliverable {i} exists" for i in range(7)]
        plan["file_manifest"] = [
            {"path": f"src/part-{i}.tsx", "action": "modify", "role": f"part {i}",
             "required": True, "owner_phase": "major-1"}
            for i in range(7)
        ]
        task_id = "output-budget-phase-replan"
        api.db.create_coding_task(task_id, "phase-slicing", goal, "QUEUED", "NONE", plan, time.time())
        api.db.update_coding_task(task_id, current_phase_id="major-1", retry_count=0, max_retries=2,
                                  requirements={"planning_policy": {"mode": "LEAN"},
                                                "execution_policy": {"task_size": "SMALL"}})
        with patch.object(api, "_generate_plan", side_effect=AssertionError("output-limit splitting must be host-side")):
            outcome = api._replan_task(task_id, api.db.coding_task(task_id), {"major-blueprint"},
                                       "provider output_limit reached", {"unmet_done": ["Deliverable 6 exists"]})
        self.assertEqual("Coding Task was replanned and requeued.", outcome)
        persisted = api.db.coding_task(task_id)["plan"]
        slices = [phase for phase in persisted["phases"] if phase["id"].startswith("major-1")]
        self.assertEqual(2, len(slices))
        self.assertTrue(all(len(phase["done"]) + len(phase["verify"]) <= 6 for phase in slices))

    def test_authorized_large_replan_persists_split_and_restores_completed_blueprint(self):
        goal = "Build the requested website, run npm test, and verify that the implementation works."
        current = RecoveryPhaseSplitNormalizationTests().current_plan()
        current["phases"][1]["done"] = [f"Deliverable {i} exists" for i in range(10)]
        current["phases"][1]["verify"] = [f"Check {i} passes" for i in range(2)]
        current["file_manifest"] = [
            {"path": f"src/part-{i}.tsx", "action": "modify", "role": f"part {i}",
             "required": True, "owner_phase": "major-1"}
            for i in range(8)
        ]
        task_id = "large-phase-split-apply"
        api.db.create_coding_task(task_id, "phase-slicing", goal, "QUEUED", "NONE", current, time.time())
        api.db.update_coding_task(task_id, retry_count=2, max_retries=2, current_phase_id="major-1",
                                  requirements={"planning_policy": {"mode": "LEAN"},
                                                "execution_policy": {"task_size": "LARGE"},
                                                "verification_requested": True})
        first_half, second_half = current["phases"][1]["done"][:4], current["phases"][1]["done"][4:]
        first_checks = current["phases"][1]["verify"]
        second_checks = []
        replacement = {
            "schema_version": 1, "original_goal": goal,
            "scope": current["scope"], "assumptions": [],
            "phases": [
                {"id": "slice-implementation-a", "status": "pending", "goal": "Implement first deliverable batch",
                 "done": first_half, "verify": first_checks, "dependencies": [], "risks": [],
                 "execution_mode": "IMPLEMENTATION"},
                {"id": "slice-implementation-b", "status": "pending", "goal": "Implement second deliverable batch",
                 "done": second_half, "verify": second_checks, "dependencies": ["slice-implementation-a"],
                 "risks": [], "execution_mode": "IMPLEMENTATION_AND_VERIFICATION"},
                {"id": "major-2", "status": "pending", "goal": "MODEL REWROTE THE UNRELATED VERIFICATION PHASE",
                 "done": ["The requested interactions are checked"],
                 "verify": ["The rendered site has all sections"],
                 "dependencies": [], "risks": [],
                 "execution_mode": "VERIFICATION_ONLY"},
            ],
            "file_manifest": [
                {**entry, "owner_phase": "slice-implementation-a" if index < 4 else "slice-implementation-b"}
                for index, entry in enumerate(current["file_manifest"])
            ],
        }
        with patch.object(api, "_generate_plan", side_effect=AssertionError("output-limit recovery must slice on host")):
            outcome = api._replan_task(task_id, api.db.coding_task(task_id), {"major-blueprint"},
                                       "retry limit reached",
                                       {"unmet_done": ["Deliverable 8 exists", "Deliverable 9 exists"],
                                        "unmet_verify": [f"Check {i} passes" for i in range(2)]})

        persisted = api.db.coding_task(task_id)["plan"]
        self.assertEqual("Coding Task was replanned and requeued.", outcome)
        self.assertEqual({"major-blueprint", "major-1", "major-1-recovery-2", "major-2"},
                         {phase["id"] for phase in persisted["phases"]})
        self.assertEqual("pass", next(phase for phase in persisted["phases"]
                                       if phase["id"] == "major-blueprint")["status"])
        downstream = next(phase for phase in persisted["phases"] if phase["id"] == "major-2")
        self.assertEqual("Verify the rendered site", downstream["goal"])
        self.assertEqual({"major-1", "major-1-recovery-2"}, set(downstream["dependencies"]))
        self.assertEqual(set(current["phases"][1]["done"]),
                         {item for phase in persisted["phases"]
                          if phase["id"] in {"major-1", "major-1-recovery-2"}
                          for item in phase["done"]})
        self.assertEqual(set(current["phases"][1]["verify"]),
                         {item for phase in persisted["phases"]
                          if phase["id"] in {"major-1", "major-1-recovery-2"}
                          for item in phase["verify"]})


if __name__ == "__main__":
    unittest.main()
