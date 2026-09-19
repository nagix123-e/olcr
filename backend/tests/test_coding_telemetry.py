import json
import tempfile
import unittest
from pathlib import Path

from olcr_api.coding_benchmark import (BENCHMARK_VERSION, baseline_eligibility, benchmark_contract, classify_live_failure,
                                       compare_results, finalize_live_artifact, is_valid_baseline, refinalize_live_artifact,
                                       prepare_fixture, run_case, run_live_case, verify_fixture, write_incomplete_artifact,
                                       _prune_retained_runs)
from olcr_api.coding_telemetry import CodingTelemetry, prompt_sections


class CodingTelemetryTests(unittest.TestCase):
    def test_deterministic_final_report_is_not_model_time(self):
        telemetry = CodingTelemetry("task", "qwen3.5:9b")
        telemetry.record_final_report_format(1.25)
        record = telemetry.finish("PASS")
        self.assertEqual("DETERMINISTIC", record["final_report_generation_mode"])
        self.assertEqual(1.25, record["final_report_format_ms"])
        self.assertEqual(0, record["final_report_model_call_count"])
        self.assertEqual(0.0, record["final_report_model_ms"])

    def test_final_report_and_deterministic_verification_have_separate_roles(self):
        telemetry = CodingTelemetry("task", "qwen3.5:9b")
        telemetry.record_verification(mode="DETERMINISTIC_COMMAND", evidence_source="READ_ONLY_COMMAND", elapsed_ms=12.5)
        telemetry.add_call(
            phase_id="p1", role="FINAL_REPORT", model="qwen3.5:9b",
            messages=[{"role": "user", "content": "report"}],
            result={"prompt_tokens": 20, "completion_tokens": 5, "load_duration_ms": 2,
                    "prompt_eval_duration": 3_000_000, "eval_duration": 4_000_000},
            started=1, finished=1.1, structured=False, thinking=False, success=True,
        )
        record = telemetry.finish("PASS")
        self.assertEqual("DETERMINISTIC_COMMAND", record["verification_mode"])
        self.assertEqual("READ_ONLY_COMMAND", record["verify_evidence_source"])
        self.assertEqual(12.5, record["deterministic_verify_ms"])
        self.assertEqual(1, record["final_report_model_call_count"])
        self.assertEqual(0, record["semantic_verification_model_call_count"])
        self.assertEqual(0, record["verification_model_call_count"])
        self.assertEqual(20, record["final_report_input_tokens"])
        self.assertEqual(5, record["final_report_output_tokens"])
        self.assertEqual(2, record["final_report_load_ms"])
        self.assertEqual(3, record["final_report_prompt_eval_ms"])
        self.assertEqual(4, record["final_report_decode_ms"])
        self.assertEqual(record["final_report_model_ms"], record["final_report_total_model_ms"])

    def test_semantic_verification_model_is_not_final_report(self):
        telemetry = CodingTelemetry("task")
        telemetry.add_call(phase_id="p1", role="SEMANTIC_VERIFICATION", model="m",
                           messages=[{"role": "user", "content": "verify"}],
                           result={"prompt_tokens": 1, "completion_tokens": 1},
                           started=1, finished=1.1, structured=False, thinking=False, success=True)
        record = telemetry.finish("PASS")
        self.assertEqual(1, record["semantic_verification_model_call_count"])
        self.assertEqual(1, record["verification_model_call_count"])
        self.assertEqual(0, record["final_report_model_call_count"])

    def test_role_model_timings_reconcile_with_aggregate(self):
        telemetry = CodingTelemetry("task")
        for role, started in (("PLANNER", 1), ("IMPLEMENTER", 2), ("FINAL_REPORT", 3)):
            telemetry.add_call(phase_id="p1", role=role, model="m",
                               messages=[{"role": "user", "content": role}],
                               result={"prompt_tokens": 1, "completion_tokens": 1},
                               started=started, finished=started + 0.1,
                               structured=False, thinking=False, success=True)
        record = telemetry.finish("PASS")
        role_total = record["planner_model_ms"] + record["implementer_model_ms"] + record["final_report_model_ms"]
        self.assertAlmostEqual(record["model_active_ms"], role_total)

    def test_lifecycle_and_authoritative_tokens(self):
        telemetry=CodingTelemetry("task", "qwen3.5:9b")
        telemetry.add_call(phase_id="p1", role="PLANNER", model="qwen3.5:9b", messages=[{"role":"system","content":"stable"},{"role":"user","content":"goal"}], result={"prompt_tokens":10,"completion_tokens":4,"load_duration_ms":2}, started=1, finished=1.1, structured=True, thinking=False, success=True)
        record=telemetry.finish("PASS")
        self.assertEqual(1, record["model_call_count"]); self.assertEqual(10, record["input_tokens_total"]); self.assertEqual(4, record["output_tokens_total"])
        self.assertEqual("PASS", record["verify_status"])
        self.assertNotIn("verification_status", record)

    def test_missing_provider_metrics_are_unknown(self):
        telemetry=CodingTelemetry("task")
        telemetry.add_call(phase_id="p1", role="IMPLEMENTER", model="unknown", messages=[{"role":"user","content":"x"}], result={}, started=1, finished=1.1, structured=False, thinking=False, success=True)
        record=telemetry.finish()
        self.assertEqual("UNKNOWN", record["input_tokens_total"]); self.assertEqual("UNKNOWN", record["output_tokens_total"])

    def test_stable_prefix_and_dynamic_suffix(self):
        messages=[{"role":"system","content":"stable instructions"},{"role":"user","content":"first"}]
        telemetry=CodingTelemetry("task")
        first=telemetry.add_call(phase_id="p1", role="PLANNER", model="m", messages=messages, result={"prompt_tokens":1,"completion_tokens":1}, started=1, finished=1.1, structured=False, thinking=False, success=True)
        second=telemetry.add_call(phase_id="p1", role="IMPLEMENTER", model="m", messages=[{"role":"system","content":"stable instructions"},{"role":"user","content":"second"}], result={"prompt_tokens":1,"completion_tokens":1}, started=2, finished=2.1, structured=False, thinking=False, success=True)
        self.assertEqual(first["stable_prefix_fingerprint"], second["stable_prefix_fingerprint"]); self.assertTrue(second["prefix_fingerprint_match"])
        self.assertNotIn("first", json.dumps(second))

    def test_prompt_sections_are_size_labeled_bytes(self):
        sections=prompt_sections([{"role":"system","content":"abc"},{"role":"user","content":"日本語"}])
        self.assertEqual(3, sections["system_context"]["bytes"]); self.assertEqual(len("日本語".encode()), sections["dynamic_context"]["bytes"])

    def test_benchmark_fixture_isolated_and_repeatable(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            first=run_case("small", directory); second=run_case("small", directory)
            self.assertTrue(first["telemetry"]["fixture_root_not_persisted"]); self.assertEqual(first["case"], second["case"]); self.assertEqual(2, len(list(Path(directory).glob("*.json"))))

    def test_small_fixture_is_target_only_and_initially_fails(self):
        contract = prepare_fixture("small", "/private/tmp")
        try:
            self.assertEqual(BENCHMARK_VERSION, "coding-v2")
            self.assertEqual(["src/math.py", "tests/test_math.py"], contract["files"])
            self.assertEqual(["src/math.py"], contract["target_files"])
            self.assertEqual("FAIL", contract["initial_verify_status"])
            self.assertIn("test_add", contract["initial_verify_output"])
        finally:
            import shutil; shutil.rmtree(contract["root"], ignore_errors=True)

    def test_small_request_hash_and_fixture_hash_are_stable(self):
        first = prepare_fixture("small", "/private/tmp"); second = prepare_fixture("small", "/private/tmp")
        try:
            self.assertEqual(first["fixture_hash"], second["fixture_hash"])
            self.assertEqual(first["request_hash"], second["request_hash"])
        finally:
            import shutil; shutil.rmtree(first["root"], ignore_errors=True); shutil.rmtree(second["root"], ignore_errors=True)

    def test_incomplete_artifact_is_not_a_baseline(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            contract = prepare_fixture("small", directory)
            artifact = write_incomplete_artifact(directory, contract=contract, task_id="task", reason="TIMEOUT")
            self.assertEqual("INCOMPLETE", artifact["benchmark_result"])
            self.assertFalse(artifact["partial_metrics_available"])
            import shutil; shutil.rmtree(contract["root"], ignore_errors=True)

    def test_incompatible_results_are_not_comparable(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            left, right = run_case("small", directory), run_case("medium", directory)
            self.assertFalse(compare_results(left, right)["comparable"])

    def test_benchmark_comparison_reports_numeric_deltas(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            left=run_case("small", directory); right=run_case("medium", directory)
            delta=compare_results(left, right)
            self.assertIn("total_task_ms", delta["deltas"])

    def test_contract_declares_deterministic_forbidden_operations(self):
        contract = benchmark_contract("small")
        self.assertEqual("small", contract["benchmark_case"])
        self.assertEqual(["src/math.py"], contract["allowed_mutation"])
        self.assertEqual("FORBIDDEN", contract["network"])
        fixture = prepare_fixture("small", "/private/tmp")
        try:
            self.assertEqual(contract["request_hash"], fixture["request_hash"])
        finally:
            import shutil; shutil.rmtree(fixture["root"], ignore_errors=True)

    def test_live_artifact_requires_real_done_verify_and_model_call(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fixture = prepare_fixture("small", directory)
            telemetry = {"task_id": "t", "model_name": "qwen3.5:9b", "model_call_count": 1, "task_finished_at": 2}
            artifact = finalize_live_artifact(directory, contract=fixture, task_id="t", telemetry=telemetry,
                                              task_status="COMPLETED", verify_status="PASS",
                                              changed_files=["src/math.py"])
            self.assertEqual("PASS", artifact["benchmark_result"])
            self.assertTrue(is_valid_baseline(artifact))
            import shutil; shutil.rmtree(fixture["root"], ignore_errors=True)

    def test_live_artifact_rejects_fixture_only_or_out_of_scope(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fixture = prepare_fixture("small", directory)
            artifact = finalize_live_artifact(directory, contract=fixture, task_id="t",
                                              telemetry={"model_name": "UNKNOWN", "model_call_count": 0, "task_finished_at": 2},
                                              task_status="COMPLETED", verify_status="PASS",
                                              changed_files=["tests/test_math.py"])
            self.assertEqual("INCOMPLETE", artifact["benchmark_result"])
            self.assertFalse(is_valid_baseline(artifact))
            import shutil; shutil.rmtree(fixture["root"], ignore_errors=True)

    def test_failure_classification_is_stable(self):
        result = classify_live_failure(task_status="RESUMABLE", verification_status="FAIL",
                                       done_satisfied=True, verify_satisfied=False,
                                       replan_reason="retry limit reached")
        self.assertEqual("VERIFY", result["failure_stage"])
        self.assertEqual("VERIFY_UNSATISFIED", result["root_cause_category"])

    def test_live_timeout_retains_workspace_metadata(self):
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(self.value).encode()
        values = iter([{"id": "project"}, {"id": "conversation"}, {"coding_task_id": "task-live"}])
        def fake_urlopen(request, timeout=30): return Response(next(values))
        with tempfile.TemporaryDirectory(dir="/private/tmp") as workspace, tempfile.TemporaryDirectory(dir="/private/tmp") as output:
            from unittest.mock import patch
            with patch("olcr_api.coding_benchmark.urlrequest.urlopen", side_effect=fake_urlopen):
                result = run_live_case("small", api_base="http://example/api", workspace=workspace, output_dir=output, timeout_seconds=0)
            self.assertEqual("INCOMPLETE", result["benchmark_result"])
            self.assertTrue(Path(result["workspace_path"]).exists())
            self.assertTrue((Path(result["workspace_path"]) / ".benchmark_metadata.json").exists())
            persisted = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
            self.assertEqual("LIVE_ORCHESTRATOR", persisted["benchmark_execution_mode"])
            self.assertEqual(result["workspace_path"], persisted["workspace_path"])

    def test_terminal_artifact_is_written_before_cleanup(self):
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(self.value).encode()
        values = iter([{"id": "project"}, {"id": "conversation"}, {"coding_task_id": "task-terminal"},
                       {"status": "COMPLETED", "activity": "NONE", "final_report": {"text": "report"}},
                       {"verify_status": "PASS", "model_call_count": 1, "model_name": "qwen3.5:9b", "task_finished_at": 2,
                        "final_report_generation_mode": "DETERMINISTIC"},
                       {"reports": []}])
        def fake_urlopen(request, timeout=30): return Response(next(values))
        with tempfile.TemporaryDirectory(dir="/private/tmp") as workspace, tempfile.TemporaryDirectory(dir="/private/tmp") as output:
            from unittest.mock import patch
            with patch("olcr_api.coding_benchmark.urlrequest.urlopen", side_effect=fake_urlopen):
                result = run_live_case("small", api_base="http://example/api", workspace=workspace, output_dir=output)
            self.assertEqual("PASS", result["benchmark_result"])
            self.assertFalse(Path(result["workspace_path"]).exists())
            self.assertTrue(Path(result["result_path"]).exists())

    def test_baseline_missing_verify_status_has_explicit_reason(self):
        eligible, reasons = baseline_eligibility(benchmark_execution_mode="LIVE_ORCHESTRATOR", task_status="COMPLETED",
                                                  verify_status=None, telemetry={"task_finished_at": 2, "model_name": "qwen3.5:9b", "model_call_count": 1})
        self.assertFalse(eligible); self.assertIn("VERIFY_STATUS_MISSING", reasons)

    def test_baseline_fail_verify_is_not_eligible(self):
        eligible, reasons = baseline_eligibility(benchmark_execution_mode="LIVE_ORCHESTRATOR", task_status="COMPLETED",
                                                  verify_status="FAIL", telemetry={"task_finished_at": 2, "model_name": "qwen3.5:9b", "model_call_count": 1})
        self.assertFalse(eligible); self.assertIn("VERIFY_NOT_PASS", reasons)

    def test_completed_and_done_are_successful_terminal_statuses(self):
        for status in ("COMPLETED", "DONE"):
            eligible, reasons = baseline_eligibility(benchmark_execution_mode="LIVE_ORCHESTRATOR", task_status=status,
                                                      verify_status="PASS", telemetry={"task_finished_at": 2, "model_name": "qwen3.5:9b", "model_call_count": 1})
            self.assertTrue(eligible, reasons)

    def test_fixture_only_no_model_and_unfinished_telemetry_are_ineligible(self):
        for mode, calls, finished, expected in (("FIXTURE_ONLY", 1, 2, "NOT_LIVE_ORCHESTRATOR"),
                                                  ("LIVE_ORCHESTRATOR", 0, 2, "NO_REAL_MODEL_CALL"),
                                                  ("LIVE_ORCHESTRATOR", 1, None, "TELEMETRY_NOT_FINISHED")):
            eligible, reasons = baseline_eligibility(benchmark_execution_mode=mode, task_status="COMPLETED", verify_status="PASS",
                                                      telemetry={"task_finished_at": finished, "model_name": "qwen3.5:9b", "model_call_count": calls})
            self.assertFalse(eligible); self.assertIn(expected, reasons)

    def test_legacy_verification_status_is_read_only_compatibility(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fixture = prepare_fixture("small", directory)
            artifact = finalize_live_artifact(directory, contract=fixture, task_id="legacy", telemetry={"task_finished_at": 2, "model_name": "qwen3.5:9b", "model_call_count": 1}, task_status="COMPLETED", verification_status="PASS")
            legacy = dict(artifact); legacy.pop("verify_status", None); legacy["verification_status"] = "PASS"
            self.assertTrue(is_valid_baseline(legacy))
            import shutil; shutil.rmtree(fixture["root"], ignore_errors=True)

    def test_comparison_exposes_canonical_verify_status(self):
        left = {"benchmark_version": BENCHMARK_VERSION, "fixture_hash": "f", "request_hash": "r", "telemetry": {"verify_status": "PASS"}}
        right = {"benchmark_version": BENCHMARK_VERSION, "fixture_hash": "f", "request_hash": "r", "telemetry": {"verify_status": "PASS"}}
        result = compare_results(left, right)
        self.assertEqual("PASS", result["baseline_verify_status"]); self.assertEqual("PASS", result["candidate_verify_status"])

    def test_comparison_exposes_role_separated_metrics_and_reads_legacy_records(self):
        left = {"benchmark_version": BENCHMARK_VERSION, "fixture_hash": "f", "request_hash": "r",
                "telemetry": {"verify_status": "PASS", "calls": [{"role": "VERIFICATION"}], "final_report_model_ms": 0}}
        right = {"benchmark_version": BENCHMARK_VERSION, "fixture_hash": "f", "request_hash": "r",
                 "telemetry": {"verify_status": "PASS", "verification_mode": "DETERMINISTIC_COMMAND",
                                "verify_evidence_source": "READ_ONLY_COMMAND", "planner_model_ms": 1,
                                "implementer_model_ms": 2, "final_report_model_ms": 3,
                                "deterministic_verify_ms": 4, "final_report_model_call_count": 1,
                                "semantic_verification_model_call_count": 0}}
        result = compare_results(left, right)
        self.assertEqual(3, result["deltas"]["final_report_model_ms"])
        self.assertEqual("DETERMINISTIC_COMMAND", result["candidate_verification_mode"])
        self.assertEqual("YES", result["historical_role_ambiguous"])

    def test_existing_artifact_refinalization_is_read_only(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            source = Path(directory) / "old.json"
            source.write_text(json.dumps({"benchmark_version": BENCHMARK_VERSION, "case": "small", "task_id": "persisted",
                                          "benchmark_execution": "LIVE", "task_final_status": "COMPLETED", "fixture_hash": "f",
                                          "request_hash": "r", "initial_verify_status": "FAIL", "telemetry": {
                                              "verify_status": "PASS", "task_finished_at": 2, "model_name": "qwen3.5:9b", "model_call_count": 1}}))
            result = refinalize_live_artifact(source, directory)
            self.assertTrue(result["baseline_eligible"]); self.assertEqual(1, result["telemetry"]["model_call_count"])
            self.assertTrue(source.exists())

    def test_incomplete_retention_is_bounded(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / ".olcr-benchmark"
            for index in range(4):
                run_dir = root / f"run-{index}" / "fixture"
                run_dir.mkdir(parents=True)
                (run_dir / ".benchmark_metadata.json").write_text(json.dumps({
                    "created_at": index, "lifecycle_state": "RETAINED_INCOMPLETE"
                }), encoding="utf-8")
            _prune_retained_runs(root, keep=2)
            self.assertEqual(2, len(list(root.iterdir())))


if __name__ == "__main__": unittest.main()
