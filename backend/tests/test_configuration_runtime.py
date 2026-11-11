from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olcr_api.config import DEFAULT_MAIN_MODEL, MODEL_REQUEST_TIMEOUT_SECONDS, Settings
from olcr_api.ollama import OllamaProvider
from olcr_api.semantic import OllamaEmbeddingProvider, OllamaIntentNormalizer, OllamaSemanticRelationEvaluator


class ConfigurationRuntimeTests(unittest.TestCase):
    def test_web_search_defaults_to_auto_brave_and_explicit_off_is_preserved(self):
        defaults = Settings.from_env().with_overrides({})
        self.assertEqual("auto", defaults.web_mode)
        self.assertEqual("brave", defaults.web_provider)

        opted_out = defaults.with_overrides({"web_mode": "off"})
        self.assertEqual("off", opted_out.web_mode)
        self.assertEqual("brave", opted_out.web_provider)

    def test_chat_controlled_capabilities_are_on_by_default(self):
        settings = Settings.from_env()
        self.assertTrue(settings.web_scraping_enabled)
        self.assertTrue(settings.local_e2e_enabled)
        self.assertTrue(settings.computer_use_enabled)
        self.assertTrue(settings.external_access_enabled)
        self.assertTrue(settings.task_manager_enabled)

    def test_environment_model_wins_when_persisted_model_is_empty(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "qwen3.6:latest"}, clear=False):
            settings = Settings.from_env().with_overrides({"main_model": ""})
        self.assertEqual(settings.main_model, "qwen3.6:latest")

    def test_nonempty_persisted_model_overrides_environment(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "qwen3.6:latest"}, clear=False):
            settings = Settings.from_env().with_overrides({"main_model": "another-valid-model"})
        self.assertEqual(settings.main_model, "another-valid-model")

    def test_packaged_default_and_app_support_database(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OLCR_APP_SUPPORT": directory, "OLCR_DB_PATH": ""}, clear=False):
            settings = Settings.from_env()
        self.assertEqual(settings.main_model, DEFAULT_MAIN_MODEL)
        self.assertEqual(settings.db_path, str(Path(directory) / "olcr.db"))

    def test_computer_use_scope_is_normalized_and_rejects_invalid_bundle_ids(self):
        settings = Settings().with_overrides({"computer_use_allowed_bundle_ids": [" com.example.game ", "com.example.game"]})
        self.assertEqual(settings.computer_use_allowed_bundle_ids, ("com.example.game",))
        self.assertEqual(settings.public_dict()["computer_use_allowed_bundle_ids"], ["com.example.game"])
        with self.assertRaisesRegex(ValueError, "invalid bundle ID"):
            Settings().with_overrides({"computer_use_allowed_bundle_ids": ["bad\nbundle"]})

    def test_all_normal_model_providers_use_shared_timeout(self):
        self.assertEqual(OllamaProvider("http://127.0.0.1:11434").timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(OllamaEmbeddingProvider("http://127.0.0.1:11434").timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(OllamaIntentNormalizer("http://127.0.0.1:11434", "judge").timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(OllamaSemanticRelationEvaluator("http://127.0.0.1:11434", "judge").timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(MODEL_REQUEST_TIMEOUT_SECONDS, 750)
