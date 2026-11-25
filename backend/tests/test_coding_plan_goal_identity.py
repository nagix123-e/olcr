"""Regression contract: long task identity belongs to the host, not the planner."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.db import Database


class CodingPlanGoalIdentityTests(unittest.TestCase):
    def test_long_goal_is_host_owned_and_injected_before_plan_validation(self):
        prefix = "Fix a typo in the sample text while preserving its existing behavior. "
        goal = prefix + ("x" * (16_000 - len(prefix)))
        self.assertEqual(16_000, len(goal))

        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            db = Database(Path(directory) / "coding.sqlite")
            db.initialize()
            db.create_conversation("plan-goal", time.time(), "plan-goal")
            task_id = "long-goal-plan"
            db.create_coding_task(task_id, "plan-goal", goal, "PLANNING", "QWEN_PLANNING",
                                  None, time.time())

            # This is otherwise a valid minimal plan. The host supplies the
            # canonical task identity, so the model response intentionally
            # omits `original_goal`.
            model_plan = {
                "schema_version": 1,
                "scope": {"allowed": ["src"], "forbidden": []},
                "assumptions": [],
                "phases": [{
                    "id": "p1",
                    "goal": "Correct the sample text while preserving behavior.",
                    "status": "pending",
                    "done": ["The requested typo is corrected."],
                    "verify": [],
                    "dependencies": [],
                    "risks": [],
                    "execution_mode": "IMPLEMENTATION",
                }],
                "max_retries_per_phase": 2,
                "requires_user_approval": True,
            }
            schemas = []
            validated_plans = []

            def fake_model_text(*_args, **kwargs):
                schema = kwargs["structured_schema"]
                schemas.append(json.loads(json.dumps(schema)))
                return json.dumps(model_plan)

            real_validate_plan = api.validate_plan

            def observe_validation(value, task_goal):
                validated_plans.append(json.loads(json.dumps(value)))
                return real_validate_plan(value, task_goal)

            with patch.object(api, "db", db), \
                 patch.object(api, "_model_text", side_effect=fake_model_text) as model_call, \
                 patch.object(api, "validate_plan", side_effect=observe_validation):
                parsed, _raw = api._generate_plan(task_id, goal, "Plan the requested correction.",
                                                  "QWEN_PLANNING")

            violations = []
            if not schemas:
                violations.append("planner schema was not captured")
            else:
                schema = schemas[0]
                identity_property = (schema.get("properties") or {}).get("original_goal") or {}
                if identity_property.get("const") == goal:
                    violations.append("the 16k task goal is embedded as original_goal.const")
                if "original_goal" in (schema.get("required") or []):
                    violations.append("original_goal is still required model output")
                if goal in json.dumps(schema, ensure_ascii=False):
                    violations.append("the exact 16k task goal appears in the model schema")
            if not validated_plans or validated_plans[0].get("original_goal") != goal:
                violations.append("the host did not inject the exact goal before initial validation")
            if parsed is None or parsed.get("original_goal") != goal:
                violations.append("a valid plan omitting original_goal did not validate after host injection")
            if model_call.call_count != 1:
                violations.append(f"planner was called {model_call.call_count} times instead of once")

            self.assertEqual([], violations, "; ".join(violations))

    def test_long_goal_stays_host_owned_during_model_schema_repair(self):
        prefix = "Fix a typo in the sample text while preserving its existing behavior. "
        goal = prefix + ("x" * (16_000 - len(prefix)))
        self.assertEqual(16_000, len(goal))

        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            db = Database(Path(directory) / "coding.sqlite")
            db.initialize()
            db.create_conversation("plan-repair-goal", time.time(), "plan-repair-goal")
            task_id = "long-goal-plan-repair"
            db.create_coding_task(task_id, "plan-repair-goal", goal, "PLANNING", "QWEN_PLANNING",
                                  None, time.time())

            model_plan = {
                "schema_version": 1,
                "scope": {"allowed": ["src"], "forbidden": []},
                "assumptions": [],
                "phases": [{
                    "id": "p1",
                    "goal": "Correct the sample text while preserving behavior.",
                    "status": "pending",
                    "done": ["The requested typo is corrected."],
                    "verify": [],
                    "dependencies": [],
                    "risks": [],
                    "execution_mode": "IMPLEMENTATION",
                }],
                "max_retries_per_phase": 2,
                "requires_user_approval": True,
            }
            model_responses = ["not JSON", json.dumps(model_plan)]
            model_schemas = []
            validation_observations = []
            model_call_count = 0

            def fake_model_text(*_args, **kwargs):
                nonlocal model_call_count
                model_call_count += 1
                schema = kwargs["structured_schema"]
                model_schemas.append(json.loads(json.dumps(schema)))
                return model_responses.pop(0)

            real_validate_plan = api.validate_plan

            def observe_validation(value, task_goal):
                validation_observations.append(
                    (model_call_count, json.loads(json.dumps(value)) if isinstance(value, dict) else value)
                )
                return real_validate_plan(value, task_goal)

            with patch.object(api, "db", db), \
                 patch.object(api, "_model_text", side_effect=fake_model_text) as model_call, \
                 patch.object(api, "validate_plan", side_effect=observe_validation):
                parsed, _raw = api._generate_plan(task_id, goal, "Plan the requested correction.",
                                                  "QWEN_PLANNING")

            violations = []
            if model_call.call_count != 2:
                violations.append(f"planner was called {model_call.call_count} times instead of twice")
            if len(model_schemas) != 2:
                violations.append(f"captured {len(model_schemas)} schemas instead of two")
            for index, schema in enumerate(model_schemas, 1):
                identity_property = (schema.get("properties") or {}).get("original_goal") or {}
                if identity_property.get("const") == goal:
                    violations.append(f"repair call {index} embeds the 16k goal as original_goal.const")
                if "original_goal" in (schema.get("required") or []):
                    violations.append(f"repair call {index} requires original_goal model output")
                if goal in json.dumps(schema, ensure_ascii=False):
                    violations.append(f"repair call {index} schema contains the exact 16k goal")

            repair_validation = [
                value for call_index, value in validation_observations
                if call_index == 2 and isinstance(value, dict)
            ]
            if not repair_validation or repair_validation[0].get("original_goal") != goal:
                violations.append("host did not inject the exact goal before validating the repaired plan")
            if parsed is None or parsed.get("original_goal") != goal:
                violations.append("schema repair did not produce a valid host-identified plan")
            if any(goal in response for response in ["not JSON", json.dumps(model_plan)]):
                violations.append("the model response unexpectedly contains the long task goal")

            self.assertEqual([], violations, "; ".join(violations))


if __name__ == "__main__":
    unittest.main()
