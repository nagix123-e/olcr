import json
import unittest
from unittest.mock import patch

import olcr_api.app as app
from olcr_api.ollama import OllamaProvider
from olcr_api.coding_telemetry import CodingTelemetry
from olcr_api.coding_benchmark import finalize_live_artifact, prepare_fixture
import tempfile
from pathlib import Path


class _Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.value).encode()


class RuntimeObservabilityTests(unittest.TestCase):
    def test_coding_request_payload_is_unchanged_and_options_are_recorded(self):
        captured = {}
        def fake_urlopen(req, timeout):
            captured.update(json.loads(req.data.decode()))
            return _Response({"message": {"content": "ok"}, "prompt_eval_count": 2, "eval_count": 3})
        provider = OllamaProvider("http://ollama")
        with patch("olcr_api.ollama.request.urlopen", side_effect=fake_urlopen):
            result = provider.generate([{"role": "user", "content": "x"}], "qwen3.5:9b", think=False,
                                       format={"type": "object"})
        self.assertEqual({"model", "messages", "stream", "think", "format"}, set(captured))
        self.assertNotIn("options", captured)
        self.assertNotIn("keep_alive", captured)
        self.assertEqual("OMITTED", result["request_options"]["num_ctx"])
        self.assertFalse(result["request_options"]["think"])
        self.assertEqual("EXPLICIT_JSON_SCHEMA", result["request_options"]["format"])

    def test_runtime_snapshot_is_allowlisted_and_truthful(self):
        snapshot = OllamaProvider.summarize_runtime_observation(
            version={"version": "0.32.15"},
            ps={"models": [{"name": "qwen3.5:9b", "context_length": 32768, "size": 10, "size_vram": 9,
                            "expires_at": "never", "details": {"format": "gguf", "quantization_level": "Q4_K_M", "parameter_size": "9.7B"}}]},
            show={"details": {"format": "gguf", "quantization_level": "Q4_K_M", "parameter_size": "9.7B"},
                  "model_info": {"qwen35.context_length": 262144},
                  "parameters": "temperature 1\ntop_p 0.95\ntop_k 20"},
            target_model="qwen3.5:9b")
        self.assertEqual("0.32.15", snapshot["ollama_version"])
        self.assertTrue(snapshot["target_loaded"])
        self.assertEqual(32768, snapshot["model_context_active"])
        self.assertEqual(262144, snapshot["model_metadata"]["context_capacity"])
        self.assertEqual(0.95, snapshot["model_metadata"]["parameter_defaults"]["top_p"])
        self.assertEqual("UNKNOWN", snapshot["daemon_keep_alive"])
        self.assertNotIn("environment", snapshot)

    def test_telemetry_keeps_request_options_and_provenance_additively(self):
        telemetry = CodingTelemetry("task", "qwen3.5:9b")
        call = telemetry.add_call(
            phase_id="p1", role="PLANNER", model="qwen3.5:9b",
            messages=[{"role": "user", "content": "x"}],
            result={"prompt_tokens": 1, "completion_tokens": 1,
                    "request_options": {"num_ctx": "OMITTED"}},
            model_provenance={"effective_model": "qwen3.5:9b", "source": "DEFAULT_MAIN_MODEL"},
            started=1, finished=1.1, structured=True, thinking=False, success=True)
        record = telemetry.finish("PASS")
        self.assertEqual("OMITTED", call["request_options"]["num_ctx"])
        self.assertEqual("DEFAULT_MAIN_MODEL", record["model_provenance"]["PLANNER"]["source"])
        self.assertEqual("YES", record["request_options_observed"])

    def test_legacy_application_setting_provenance_explains_effective_model(self):
        with patch.object(app.db, "load_application_settings", return_value={"main_model": "qwen3:14b"}), \
             patch.object(app.settings, "main_model", "qwen3.5:9b"):
            value = app._coding_model_provenance("qwen3.5:9b", "PLANNER")
        self.assertEqual("qwen3.5:9b", value["effective_model"])
        self.assertEqual("APPLICATION_SETTINGS_MAIN_MODEL_LEGACY_NORMALIZED_TO_DEFAULT_MAIN_MODEL", value["source"])
        self.assertEqual("qwen3:14b", value["application_main_model"])

    def test_runtime_endpoint_falls_back_to_unknown_without_provider_support(self):
        class Fake:
            pass
        with patch.object(app.runtime, "model", Fake()):
            value = app.runtime_observability()
        self.assertEqual("UNKNOWN", value["status"])
        self.assertEqual("UNKNOWN", value["processor_placement"])
        self.assertIn("model_provenance", value)

    def test_live_artifact_preserves_runtime_observation_additively(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            contract = prepare_fixture("small", directory)
            artifact = finalize_live_artifact(
                directory, contract=contract, task_id="task", telemetry={"model_name": "qwen3.5:9b", "model_call_count": 1, "task_finished_at": 2},
                task_status="COMPLETED", verify_status="PASS", changed_files=["src/math.py"],
                runtime_model_observation={"status": "OK", "target_model": "qwen3.5:9b"})
            self.assertEqual("OK", artifact["runtime_model_observation"]["status"])
            Path(contract["root"]).exists() and __import__("shutil").rmtree(contract["root"], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
