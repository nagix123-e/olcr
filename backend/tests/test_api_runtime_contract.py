import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from types import ModuleType

from olcr_api.api_acceptance import MANIFEST_SOURCE_COUNT, validate_acceptance_fixture
from olcr_api.laya_runtime import LayaDecisionProvider, LayaRuntimeError, LAYA_PACKAGE_VERSION, preflight


class ApiRuntimeContractTests(unittest.TestCase):
    def test_laya_preflight_never_claims_ready_without_pinned_runtime(self):
        result = preflight(revision="UNPINNED")
        self.assertFalse(result["ready"])
        self.assertEqual("LAYA_PREFLIGHT_INCOMPLETE", result["reason"])
        self.assertTrue(result["request_path_download_forbidden"])

    def test_acceptance_fixture_is_release_gate_data(self):
        result = validate_acceptance_fixture()
        self.assertEqual(MANIFEST_SOURCE_COUNT, result["source_count"])
        self.assertEqual(MANIFEST_SOURCE_COUNT, result["unique_tool_ids"])
        self.assertFalse(result["skip_is_pass"])

    def test_laya_provider_fails_closed_before_startup_preflight(self):
        provider = LayaDecisionProvider(multilingual_model="m", english_model="e", revision="r")
        with self.assertRaisesRegex(LayaRuntimeError, "must be loaded"):
            provider.choice(question="q", candidates=[{"tool_id": "x"}])

    def test_laya_preflight_exposes_pinned_lock_and_cache_gate(self):
        with TemporaryDirectory() as empty_cache, patch.dict("os.environ", {
                "OLCR_LAYA_CACHE": empty_cache, "OLCR_LAYA_CHECKPOINTS_READY": "false"}):
            result = preflight()
        self.assertEqual("laya", result["package"])
        self.assertEqual(LAYA_PACKAGE_VERSION, result["package_version_required"])
        self.assertTrue(result["lockfile_valid"])
        self.assertFalse(result["checkpoints_cached"])
        self.assertEqual("052592a15d198d9ad47da779604259b10b47b7aa", result["model_revisions"]["multilingual"])
        self.assertEqual({"multilingual"}, set(result["model_revisions"]))
        self.assertFalse(result["english_checkpoint_ready"])
        self.assertFalse(result["ready"])

    def test_english_choice_without_staged_checkpoint_uses_local_multilingual_model(self):
        provider = LayaDecisionProvider(multilingual_model="multi-hub", english_model="english-hub", revision="multi-rev")
        router = Mock()
        router.predict.return_value = {"answers": {"api": {"choice": "candidate", "confidence": 0.9}}}
        laya_module = ModuleType("laya")
        router_factory = Mock(return_value=router)
        laya_module.Router = router_factory
        with patch("olcr_api.laya_runtime.preflight", return_value={"ready": True, "health": "READY", "cache_root": "/cache", "english_checkpoint_ready": False}), \
             patch("olcr_api.laya_runtime._checkpoint_dir", return_value=Path("/cache/multilingual")), \
             patch.dict("sys.modules", {"laya": laya_module}):
            provider.load()
            models = router_factory.call_args.kwargs["models"]
            self.assertEqual({"multilingual"}, set(models))
            self.assertEqual("/cache/multilingual", models["multilingual"])
            self.assertEqual(["multilingual"], router.preload.call_args.args[0])
            result = provider.choice(question="English request", candidates=[{"tool_id": "candidate"}], language="english")
        self.assertEqual("candidate", result["tool_id"])
        self.assertEqual("multi-hub", provider.last_trace["laya_checkpoint"])

    def test_staged_english_checkpoint_is_never_loaded(self):
        provider = LayaDecisionProvider(multilingual_model="multi-hub", english_model="english-hub", revision="multi-rev")
        router = Mock()
        router.predict.return_value = {"answers": {"api": {"choice": "candidate", "confidence": 0.9}}}
        laya_module = ModuleType("laya")
        router_factory = Mock(return_value=router)
        laya_module.Router = router_factory
        with patch("olcr_api.laya_runtime.preflight", return_value={"ready": True, "health": "READY", "cache_root": "/cache", "english_checkpoint_ready": True}), \
             patch("olcr_api.laya_runtime._checkpoint_dir", return_value=Path("/cache/multilingual")), \
             patch.dict("sys.modules", {"laya": laya_module}), \
             patch("olcr_api.laya_runtime.model_telemetry.record") as record:
            provider.load()
            provider.choice(question="Which API?", candidates=[{"tool_id": "candidate"}], language="english")
        self.assertEqual(["multilingual"], router.preload.call_args.args[0])
        self.assertEqual("multi-hub", record.call_args.kwargs["model"])
        self.assertEqual("multi-rev", record.call_args.kwargs["revision"])
        self.assertEqual("ja", router.predict.call_args.kwargs["lang"])

    def test_preflight_ignores_retired_english_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for checkpoint in ("english", "multilingual"):
                target = root / checkpoint
                (target / "tokenizer").mkdir(parents=True)
                for filename in ("rl_agent_config.json", "model.safetensors", "tokenizer/tokenizer.json"):
                    (target / filename).touch()
            marker = {
                "model_revision": "052592a15d198d9ad47da779604259b10b47b7aa",
                "model_revisions": {
                    "english": "1c5edc17a7acd8701df6fc341c0d179f1c62c982",
                    "multilingual": "052592a15d198d9ad47da779604259b10b47b7aa",
                },
                "multilingual": True,
                "english": True,
                "english_revision": "1c5edc17a7acd8701df6fc341c0d179f1c62c982",
            }
            (root / "olcr-laya-cache.json").write_text(json.dumps(marker), encoding="utf-8")
            with patch.dict("os.environ", {"OLCR_LAYA_CACHE": directory, "OLCR_LAYA_CHECKPOINTS_READY": "true"}):
                with patch("olcr_api.laya_runtime.importlib.util.find_spec", return_value=object()), \
                     patch("olcr_api.laya_runtime.metadata.version", return_value=LAYA_PACKAGE_VERSION):
                    status = preflight()
                    self.assertTrue(status["ready"])
                    self.assertFalse(status["english_checkpoint_ready"])
                    marker["english_revision"] = "wrong-revision"
                    (root / "olcr-laya-cache.json").write_text(json.dumps(marker), encoding="utf-8")
                    mismatched = preflight()
        self.assertTrue(mismatched["ready"], "optional English cache must not degrade multilingual startup")
        self.assertFalse(mismatched["english_checkpoint_ready"], "English must fail closed on a revision mismatch")


if __name__ == "__main__":
    unittest.main()
