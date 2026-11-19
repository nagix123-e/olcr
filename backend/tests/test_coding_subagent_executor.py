import json
import threading
import time
import unittest
from dataclasses import asdict

from olcr_api.coding_subagent_executor import (
    CodingSubagentExecutor, CodingSubagentFileContext, CodingSubagentRequest,
)
from olcr_api.generation_control import GenerationCancelled, check_cancelled


def binding(runtime="brain-provider:openai", model="api:openai:gpt-coder"):
    return {
        "requested_model": "role:long_implementation", "model_id": model,
        "provider_id": "openai", "runtime_id": runtime, "model_revision": None,
        "brain_mode": "split", "selected_slot": "coder",
        "implementation_role": "long_implementation", "fallback_policy": "NONE",
    }


def request(worker_id="worker-1", role="implementation_worker", model_binding=None,
            files=None):
    return CodingSubagentRequest(
        task_id="task-1", phase_id=worker_id, plan_revision=3, worker_id=worker_id,
        role_id=role, model_binding=model_binding or binding(),
        original_goal="Implement a scoped feature", phase_goal="Implement module",
        acceptance_criteria=("The module works",),
        files=tuple(files or [CodingSubagentFileContext(
            "backend/module.py", False, None, None, None)]),
    )


def response(*, mutations=None, summary="Scoped implementation prepared"):
    return {"text": json.dumps({"summary": summary, "findings": [],
                                "proposed_mutations": mutations or []}),
            "prompt_tokens": 20, "completion_tokens": 10, "latency_ms": 4.5,
            "reasoning": {"text": "never persisted"}}


class CodingSubagentExecutorTests(unittest.TestCase):
    def test_pins_every_worker_to_same_model_and_disables_fallback(self):
        calls = []

        def generate(messages, model, **kwargs):
            calls.append((model, kwargs))
            return response()

        results = CodingSubagentExecutor(generate).run_group([
            request("worker-1"), request("worker-2", model_binding=binding()),
        ])
        self.assertEqual(2, len(results))
        self.assertEqual(["role:long_implementation"] * 2, [call[0] for call in calls])
        self.assertTrue(all(call[1]["_fallback_allowed"] is False for call in calls))
        self.assertTrue(all(call[1]["max_tokens"] == 4096 for call in calls))
        self.assertEqual({"input_tokens": 20, "output_tokens": 10, "latency_ms": 4.5},
                         results[0].usage)

    def test_group_rejects_model_drift_and_more_than_three_workers(self):
        calls = []
        executor = CodingSubagentExecutor(lambda *args, **kwargs: calls.append(args) or response())
        with self.assertRaisesRegex(ValueError, "model binding mismatch"):
            executor.run_group([request("worker-1"), request(
                "worker-2", model_binding=binding(model="api:openai:other"))])
        with self.assertRaisesRegex(ValueError, "one to three"):
            executor.run_group([request(f"worker-{index}") for index in range(4)])
        self.assertEqual([], calls)

    def test_read_only_role_cannot_return_mutations(self):
        mutation = {"path": "backend/module.py", "operation": "create",
                    "expected_sha256": None, "expected_mode": None, "content": "print('x')"}
        executor = CodingSubagentExecutor(lambda *args, **kwargs: response(mutations=[mutation]))
        result = executor.run_group([request("worker-1", role="repo_explorer")])[0]
        self.assertEqual("INVALID_RESULT", result.error_category)
        self.assertIsNone(result.summary)

    def test_mutations_must_match_assigned_scope_and_preimage(self):
        file = CodingSubagentFileContext("backend/module.py", True, "a" * 64, 0o644, "old")
        valid = {"path": "backend/module.py", "operation": "modify",
                 "expected_sha256": "a" * 64, "expected_mode": 0o644, "content": "new"}
        result = CodingSubagentExecutor(
            lambda *args, **kwargs: response(mutations=[valid])).run_group([
                request(files=[file])])[0]
        self.assertIsNone(result.error_category)
        self.assertEqual("new", result.proposed_mutations[0].content)

        outside = {**valid, "path": "backend/other.py"}
        result = CodingSubagentExecutor(
            lambda *args, **kwargs: response(mutations=[outside])).run_group([
                request(files=[file])])[0]
        self.assertEqual("INVALID_RESULT", result.error_category)

        stale = {**valid, "expected_sha256": "b" * 64}
        result = CodingSubagentExecutor(
            lambda *args, **kwargs: response(mutations=[stale])).run_group([
                request(files=[file])])[0]
        self.assertEqual("INVALID_RESULT", result.error_category)

    def test_local_model_worker_calls_are_serialized(self):
        lock = threading.Lock()
        active = 0
        maximum = 0

        def generate(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return response()

        local = binding(runtime="mlx", model="mlx:coder")
        results = CodingSubagentExecutor(generate).run_group([
            request(f"worker-{index}", model_binding=local) for index in range(3)
        ])
        self.assertEqual(1, maximum)
        self.assertEqual({None}, {item.error_category for item in results})

    def test_remote_workers_use_bounded_parallel_calls(self):
        lock = threading.Lock()
        active = 0
        maximum = 0
        barrier = threading.Barrier(3)

        def generate(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            barrier.wait(timeout=1)
            time.sleep(0.02)
            with lock:
                active -= 1
            return response()

        results = CodingSubagentExecutor(generate).run_group([
            request(f"worker-{index}") for index in range(3)
        ])
        self.assertEqual(3, maximum)
        self.assertTrue(all(result.error_category is None for result in results))

    def test_parent_cancellation_stops_running_worker_generations(self):
        entered = threading.Event()
        executor = None

        def generate(*args, **kwargs):
            entered.set()
            while True:
                check_cancelled()
                time.sleep(0.005)

        executor = CodingSubagentExecutor(generate)
        results = []
        thread = threading.Thread(target=lambda: results.extend(
            executor.run_group([request()])))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(1, executor.cancel_task("task-1"))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual("CANCELLED", results[0].error_category)

    def test_worker_timeout_is_recorded_and_cancels_provider_context(self):
        entered = threading.Event()

        def generate(*args, **kwargs):
            entered.set()
            while True:
                check_cancelled()
                time.sleep(0.005)

        results = CodingSubagentExecutor(generate, worker_timeout_seconds=0.03).run_group([
            request()])
        self.assertTrue(entered.is_set())
        self.assertEqual("TIMEOUT", results[0].error_category)

    def test_partial_worker_failure_keeps_other_results(self):
        def generate(messages, model, **kwargs):
            if "worker-2" in messages[-1]["content"]:
                raise RuntimeError("provider secret must not escape")
            return response()

        results = CodingSubagentExecutor(generate).run_group([
            request("worker-1"), request("worker-2")])
        self.assertEqual([None, "MODEL_ERROR"], [item.error_category for item in results])
        self.assertEqual("Scoped implementation prepared", results[0].summary)
        self.assertNotIn("provider secret", str(asdict(results[1])))


if __name__ == "__main__":
    unittest.main()
