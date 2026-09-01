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

    def test_all_normal_model_providers_use_shared_timeout(self):
        self.assertEqual(OllamaProvider("http://127.0.0.1:11434").timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(OllamaEmbeddingProvider("http://127.0.0.1:11434").timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(OllamaIntentNormalizer("http://127.0.0.1:11434", "judge").timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(OllamaSemanticRelationEvaluator("http://127.0.0.1:11434", "judge").timeout, MODEL_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(MODEL_REQUEST_TIMEOUT_SECONDS, 750)
