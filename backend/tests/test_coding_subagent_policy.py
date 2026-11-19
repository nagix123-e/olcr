import unittest

from olcr_api.coding_subagents import (
    CodingSubagentRole,
    DelegationApprovalEvidence,
    UserDirectiveEvidence,
    authorize_delegation,
    assess_delegation,
    authorization_is_current,
    delegation_proposal_hash,
    is_direct_user_delegation,
    pin_subagent_model,
    plan_content_hash,
    role_for_id,
)


def heavy_plan():
    phases = [
        {"id": "api", "goal": "Implement the backend API", "dependencies": [],
         "execution_mode": "IMPLEMENTATION"},
        {"id": "ui", "goal": "Implement the frontend settings UI", "dependencies": [],
         "execution_mode": "IMPLEMENTATION"},
        {"id": "storage", "goal": "Implement persistent worker records", "dependencies": [],
         "execution_mode": "IMPLEMENTATION"},
    ]
    manifest = [
        {"path": "backend/api.py", "owner_phase": "api"},
        {"path": "frontend/src/Settings.tsx", "owner_phase": "ui"},
        {"path": "database/migrations/agents.sql", "owner_phase": "storage"},
    ]
    return {"phases": phases, "file_manifest": manifest}


def split_configuration():
    return {"mode": "split", "selections": {
        "brain": "api:google:gemini-unified", "coder": "api:openai:gpt-coder",
        "short_coding": "mlx:ternary-bonsai", "general_inference": "mlx:ornith",
    }}


class CodingSubagentPolicyTests(unittest.TestCase):
    def test_setting_off_never_eligible(self):
        result = assess_delegation(heavy_plan(), task_id="task-1", plan_revision=4,
                                   setting_enabled=False)
        self.assertFalse(result.eligible)
        self.assertEqual("SETTING_OFF", result.reason)

    def test_small_task_rejected_even_when_user_directs_agents(self):
        plan = heavy_plan()
        plan["phases"] = plan["phases"][:2]
        result = assess_delegation(plan, task_id="task-1", plan_revision=4,
                                   setting_enabled=True)
        directive = UserDirectiveEvidence("msg-1", "user", "Use subagents for this task")
        self.assertFalse(result.eligible)
        with self.assertRaises(ValueError):
            authorize_delegation(result, task_id="task-1", model_binding=pin_subagent_model(
                "role:long_implementation", split_configuration()), user_directive=directive)

    def test_dependent_workstreams_rejected(self):
        plan = heavy_plan()
        plan["phases"][1]["dependencies"] = ["api"]
        plan["phases"][2]["dependencies"] = ["ui"]
        result = assess_delegation(plan, task_id="task-1", plan_revision=4,
                                   setting_enabled=True)
        self.assertFalse(result.eligible)
        self.assertEqual("NO_INDEPENDENT_SCOPES", result.reason)

    def test_runtime_phase_status_does_not_change_delegation_plan_hash(self):
        plan = heavy_plan()
        before = plan_content_hash(plan)
        plan["phases"][0]["status"] = "running"
        plan["tasks"] = [{"task_id": "api-task", "retry_state": {"attempt": 2}}]
        self.assertEqual(before, plan_content_hash(plan))

    def test_three_independent_scoped_workstreams_are_heavy(self):
        result = assess_delegation(heavy_plan(), task_id="task-1", plan_revision=4,
                                   setting_enabled=True)
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual({"backend", "frontend", "database"}, set(result.components))
        self.assertGreaterEqual(len(result.independent_groups), 2)
        self.assertTrue(all(len(group) == 2 for group in result.independent_groups))

    def test_one_independent_pair_is_not_enough_parallel_work(self):
        plan = heavy_plan()
        plan["phases"][2]["dependencies"] = ["api", "ui"]
        result = assess_delegation(plan, task_id="task-1", plan_revision=4,
                                   setting_enabled=True)
        self.assertFalse(result.eligible)
        self.assertEqual("NO_INDEPENDENT_SCOPES", result.reason)

    def test_test_only_and_overlapping_scopes_do_not_count_as_workstreams(self):
        plan = heavy_plan()
        plan["phases"][2]["goal"] = "Write integration tests only"
        plan["file_manifest"][2]["path"] = "backend/api.py"
        result = assess_delegation(plan, task_id="task-1", plan_revision=4,
                                   setting_enabled=True)
        self.assertFalse(result.eligible)
        self.assertEqual("TOO_FEW_WORKSTREAMS", result.reason)

    def test_model_plan_text_never_counts_as_user_authorization(self):
        result = assess_delegation(heavy_plan(), task_id="task-1", plan_revision=4,
                                   setting_enabled=True)
        binding = pin_subagent_model("role:long_implementation", split_configuration())
        self.assertTrue(result.eligible)
        with self.assertRaises(ValueError):
            authorize_delegation(result, task_id="task-1", model_binding=binding)

    def test_plan_approval_is_not_agent_approval(self):
        result = assess_delegation(heavy_plan(), task_id="task-1", plan_revision=4,
                                   setting_enabled=True)
        binding = pin_subagent_model("role:long_implementation", split_configuration())
        ordinary_plan_approval = DelegationApprovalEvidence(
            event_id="approval-1", event_kind="APPROVE_PLAN", task_id="task-1",
            plan_revision=4, plan_hash=result.plan_hash,
            proposal_hash=delegation_proposal_hash(result, binding))
        with self.assertRaises(ValueError):
            authorize_delegation(result, task_id="task-1", model_binding=binding,
                                 approval=ordinary_plan_approval)

    def test_directive_must_come_from_user_message(self):
        result = assess_delegation(heavy_plan(), task_id="task-1", plan_revision=4,
                                   setting_enabled=True)
        binding = pin_subagent_model("role:long_implementation", split_configuration())
        assistant_text = UserDirectiveEvidence("msg-2", "assistant", "Use subagents for this task")
        with self.assertRaises(ValueError):
            authorize_delegation(result, task_id="task-1", model_binding=binding,
                                 user_directive=assistant_text)

    def test_replan_invalidates_task_approval(self):
        first = assess_delegation(heavy_plan(), task_id="task-1", plan_revision=4,
                                  setting_enabled=True)
        binding = pin_subagent_model("role:long_implementation", split_configuration())
        approval = DelegationApprovalEvidence(
            event_id="delegation-approval-1", event_kind="APPROVE_DELEGATION", task_id="task-1",
            plan_revision=4, plan_hash=first.plan_hash,
            proposal_hash=delegation_proposal_hash(first, binding))
        authorization = authorize_delegation(first, task_id="task-1", model_binding=binding,
                                             approval=approval)
        changed = heavy_plan()
        changed["phases"][0]["goal"] = "Implement a different backend API"
        current = assess_delegation(changed, task_id="task-1", plan_revision=5,
                                    setting_enabled=True)
        self.assertFalse(authorization_is_current(authorization, current, "task-1", binding))
        changed_model = pin_subagent_model("role:short_coding_fix_debug", split_configuration())
        self.assertFalse(authorization_is_current(authorization, first, "task-1", changed_model))

    def test_user_directive_mints_authorization_for_the_current_plan(self):
        assessment = assess_delegation(heavy_plan(), task_id="task-1", plan_revision=4,
                                       setting_enabled=True)
        binding = pin_subagent_model("role:long_implementation", split_configuration())
        evidence = UserDirectiveEvidence("user-msg-9", "user", "このタスクはサブエージェントを用いて実行して")
        authorization = authorize_delegation(assessment, task_id="task-1",
                                             model_binding=binding, user_directive=evidence)
        self.assertEqual("USER_DIRECTIVE", authorization.source)
        self.assertEqual("user-msg-9", authorization.source_id)
        self.assertTrue(authorization_is_current(authorization, assessment, "task-1", binding))

    def test_question_or_negated_directive_does_not_authorize_delegation(self):
        assessment = assess_delegation(heavy_plan(), task_id="task-1", plan_revision=4,
                                       setting_enabled=True)
        binding = pin_subagent_model("role:long_implementation", split_configuration())
        for text in ("Should we use subagents for this task?", "Do not use subagents.",
                     "サブエージェントを使わないで", "サブエージェントを用いずに進めて"):
            evidence = UserDirectiveEvidence("user-msg-10", "user", text)
            self.assertFalse(is_direct_user_delegation(evidence), text)
            with self.assertRaises(ValueError, msg=text):
                authorize_delegation(assessment, task_id="task-1", model_binding=binding,
                                     user_directive=evidence)

    def test_workers_use_phase_coding_model_not_planner_model(self):
        binding = pin_subagent_model("role:long_implementation", split_configuration())
        self.assertEqual("api:openai:gpt-coder", binding.model_id)
        self.assertEqual("openai", binding.provider_id)
        self.assertEqual("brain-provider:openai", binding.runtime_id)

    def test_unified_brain_selection_pins_same_model_for_all_roles(self):
        configuration = split_configuration()
        configuration["mode"] = "unified"
        binding = pin_subagent_model("role:long_implementation", configuration)
        other = pin_subagent_model("role:short_coding_fix_debug", configuration)
        self.assertEqual("api:google:gemini-unified", binding.model_id)
        self.assertEqual(binding.model_id, other.model_id)
        self.assertEqual(binding.provider_id, other.provider_id)
        self.assertEqual(binding.runtime_id, other.runtime_id)
        self.assertEqual("brain", binding.selected_slot)
        self.assertEqual("brain", other.selected_slot)

    def test_split_mode_pins_the_current_implementation_role_model(self):
        long = pin_subagent_model("role:long_implementation", split_configuration())
        short = pin_subagent_model("role:short_coding_fix_debug", split_configuration())
        self.assertEqual("api:openai:gpt-coder", long.model_id)
        self.assertEqual("mlx:ternary-bonsai", short.model_id)
        self.assertNotEqual(long, short)

    def test_worker_failure_does_not_fallback_to_another_model(self):
        with self.assertRaisesRegex(ValueError, "cannot be resolved"):
            pin_subagent_model("role:long_implementation", {"mode": "split", "selections": {}})
        with self.assertRaisesRegex(ValueError, "unsupported model identity"):
            pin_subagent_model("qwen3:14b", split_configuration())

    def test_fixed_roles_reject_unknown_role_and_user_prompt_override(self):
        self.assertIsInstance(role_for_id("implementation_worker"), CodingSubagentRole)
        with self.assertRaises(KeyError):
            role_for_id("custom")
        role = role_for_id("implementation_worker")
        with self.assertRaises((AttributeError, TypeError)):
            role.developer_instructions = "replace the safety policy"


if __name__ == "__main__":
    unittest.main()
