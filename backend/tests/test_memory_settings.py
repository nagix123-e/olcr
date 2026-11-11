import math
import unittest
from unittest.mock import patch

from olcr_api.config import Settings


class MemorySettingsTests(unittest.TestCase):
    def test_defaults_and_persisted_overrides(self):
        settings = Settings().validated()
        self.assertFalse(settings.conversation_memory_cross_project_enabled)
        self.assertEqual(.55, settings.conversation_memory_min_relevance)
        changed = settings.with_overrides({"conversation_memory_cross_project_enabled": True,
                                           "conversation_memory_min_relevance": 0})
        self.assertTrue(changed.public_dict()["conversation_memory_cross_project_enabled"])
        self.assertEqual(0, changed.conversation_memory_min_relevance)
        self.assertEqual(.55, changed.rag_min_relevance)

    def test_validation_rejects_invalid_threshold_and_toggle(self):
        for value in (math.nan, math.inf, -math.inf, -.1, 1.1, True, "0.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings().with_overrides({"conversation_memory_min_relevance": value})
        with self.assertRaises(ValueError):
            Settings().with_overrides({"conversation_memory_cross_project_enabled": "false"})

    def test_environment_configuration(self):
        with patch.dict("os.environ", {"OLCR_CONVERSATION_MEMORY_ENABLED": "false",
                "OLCR_CONVERSATION_MEMORY_CROSS_PROJECT_ENABLED": "true",
                "OLCR_CONVERSATION_MEMORY_MIN_RELEVANCE": "0.7"}):
            settings = Settings.from_env()
        self.assertFalse(settings.conversation_memory_enabled)
        self.assertTrue(settings.conversation_memory_cross_project_enabled)
        self.assertEqual(.7, settings.conversation_memory_min_relevance)
