"""Regression tests for bounded structured-output retries across providers."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olcr_api.brain_models import BrainModels
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.model_provider import MAX_OUTPUT_BUDGET_RETRIES, RoleAwareModelProvider
from olcr_api.ollama import ModelFailure


class ModelProviderRetryContractTests(unittest.TestCase):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(external_access_enabled=True)
        self.db = Database(Path(self.temp.name) / "providers.db")
        self.db.initialize()
        self.brain_models = BrainModels(self.db, self.settings)
        self.brain_models.providers.register(
            "openai",
            "api_key",
            {
                "base_url": "https://api.openai.com/v1",
                "api_key": "synthetic-test-key",
                "model_ids": "gpt-6-luna",
            },
        )
        self.provider = RoleAwareModelProvider(self.settings)
        self.provider.brain_models = self.brain_models
        self.configuration = {
            "mode": "split",
            "selections": {
                "brain": "ollama:unused",
                "coder": "api:openai:gpt-6-luna",
                "short_coding": "ollama:unused",
                "general_inference": "ollama:unused",
            },
            "efforts": {"brain": None, "coder": "high", "short_coding": None,
                        "general_inference": None},
            "fallback_models": [],
            "quick_models": {},
        }

    def tearDown(self):
        self.provider.close()
        self.temp.cleanup()

    def _generate_structured(self, max_tokens=8192):
        with patch.object(self.brain_models, "configuration", return_value=self.configuration):
            return self.provider.generate(
                [{"role": "user", "content": "Return the answer in the required JSON schema."}],
                "role:long_implementation",
                think=True,
                format=self.schema,
                max_tokens=max_tokens,
            )

    def _output_limit(self):
        return ModelFailure("output_limit", "synthetic output truncation")

    def test_structured_output_limit_retries_reduced_effort_then_larger_budget(self):
        with patch.object(
            self.brain_models.providers,
            "generate",
            side_effect=[
                self._output_limit(),
                self._output_limit(),
                {"text": '{"answer":"complete"}', "finish_reason": "stop"},
            ],
        ) as remote_generate:
            result = self._generate_structured()

        self.assertEqual({"answer": "complete"}, json.loads(result["text"]))
        self.assertEqual(3, remote_generate.call_count)
        calls = remote_generate.call_args_list
        self.assertEqual([8192, 8192, 16384], [call.kwargs["max_tokens"] for call in calls])
        self.assertEqual(["high", calls[1].kwargs["effort"], calls[1].kwargs["effort"]],
                         [call.kwargs["effort"] for call in calls])
        self.assertIn(calls[1].kwargs["effort"], {"off", "none", "minimal", "low"})
        self.assertTrue(all(call.args[1] == "gpt-6-luna" for call in calls))
        self.assertTrue(all(call.args[3] == self.schema for call in calls))

    def test_repeated_truncation_is_bounded_to_the_configured_budget_retries(self):
        attempt_count = 2 + MAX_OUTPUT_BUDGET_RETRIES
        with patch.object(
            self.brain_models.providers,
            "generate",
            side_effect=[self._output_limit() for _ in range(attempt_count)],
        ) as remote_generate:
            with self.assertRaises(ModelFailure) as error:
                self._generate_structured()

        self.assertEqual("output_limit", error.exception.category)
        self.assertEqual(attempt_count, remote_generate.call_count)
        calls = remote_generate.call_args_list
        self.assertEqual([8192, 8192, 16384, 32000],
                         [call.kwargs["max_tokens"] for call in calls])
        self.assertTrue(all(call.args[3] == self.schema for call in calls))
        self.assertTrue(all(call.args[1] == "gpt-6-luna" for call in calls))

    def test_output_limit_exhaustion_uses_one_distinct_configured_provider_fallback(self):
        fallback_model = "claude-sonnet-4-20250514"
        self.brain_models.providers.register(
            "anthropic",
            "api_key",
            {
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "synthetic-fallback-key",
                "model_ids": fallback_model,
            },
        )
        self.configuration["fallback_models"] = [f"api:anthropic:{fallback_model}"]
        with patch.object(
            self.brain_models.providers,
            "generate",
            side_effect=[
                self._output_limit(),
                self._output_limit(),
                self._output_limit(),
                self._output_limit(),
                {"text": '{"answer":"fallback complete"}', "model": fallback_model},
            ],
        ) as remote_generate:
            result = self._generate_structured()

        self.assertEqual({"answer": "fallback complete"}, json.loads(result["text"]))
        calls = remote_generate.call_args_list
        self.assertEqual(2 + MAX_OUTPUT_BUDGET_RETRIES + 1, len(calls))
        self.assertEqual(
            [("openai", "gpt-6-luna")] * (2 + MAX_OUTPUT_BUDGET_RETRIES)
            + [("anthropic", fallback_model)],
            [(call.args[0], call.args[1]) for call in calls],
        )
        self.assertEqual([8192, 8192, 16384, 32000, 8192],
                         [call.kwargs["max_tokens"] for call in calls])
        self.assertTrue(all(call.args[3] == self.schema for call in calls))
        self.assertEqual("api:openai:gpt-6-luna", result["fallback_from"])
        self.assertEqual(f"api:anthropic:{fallback_model}", result["fallback_model_id"])
        self.assertEqual(f"api:anthropic:{fallback_model}", result["model_provenance"]["model_id"])

    def test_output_limit_remains_typed_when_configured_model_fallback_fails(self):
        fallback_model = "claude-sonnet-4-20250514"
        self.brain_models.providers.register(
            "anthropic",
            "api_key",
            {
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "synthetic-fallback-key",
                "model_ids": fallback_model,
            },
        )
        self.configuration["fallback_models"] = [f"api:anthropic:{fallback_model}"]
        fallback_failure = ModelFailure("provider_unavailable", "synthetic fallback failure")
        with patch.object(
            self.brain_models.providers,
            "generate",
            side_effect=[
                self._output_limit(),
                self._output_limit(),
                self._output_limit(),
                self._output_limit(),
                fallback_failure,
            ],
        ) as remote_generate:
            with self.assertRaises(ModelFailure) as caught:
                self._generate_structured()

        self.assertEqual("output_limit", caught.exception.category)
        self.assertEqual("provider_unavailable", caught.exception.__cause__.category)
        self.assertIn("All configured fallback models are unavailable", str(caught.exception.__cause__))
        self.assertEqual(
            [("openai", "gpt-6-luna")] * (2 + MAX_OUTPUT_BUDGET_RETRIES)
            + [("anthropic", fallback_model)],
            [(call.args[0], call.args[1]) for call in remote_generate.call_args_list],
        )

    def test_remote_output_budget_progress_reports_a_request_not_a_resolved_cap(self):
        with patch.object(
            self.brain_models.providers,
            "generate",
            return_value={"text": '{"answer":"complete"}', "finish_reason": "stop"},
        ), patch("olcr_api.model_progress.model_progress") as progress:
            self._generate_structured()

        budget_events = [call.kwargs for call in progress.call_args_list
                         if call.kwargs.get("event") in {"output_budget_requested", "output_budget_resolved"}]
        self.assertEqual(1, len(budget_events))
        self.assertEqual("output_budget_requested", budget_events[0]["event"])
        self.assertEqual(8192, budget_events[0]["requested_max_tokens"])
        self.assertNotIn(budget_events[0].get("runtime"), {"mlx", "ollama"})

    def test_preflight_skipped_model_does_not_report_an_api_output_budget(self):
        with patch.object(
            self.brain_models,
            "preflight",
            return_value={"state": "unavailable", "reason": "rate_limited"},
        ), patch("olcr_api.model_progress.model_progress") as progress:
            with self.assertRaises(ModelFailure):
                self._generate_structured()

        events = [call.kwargs for call in progress.call_args_list]
        self.assertTrue(any(event.get("state") == "model_skipped" for event in events))
        self.assertFalse(any(event.get("event") in {
            "output_budget_requested", "output_budget_resolved",
        } for event in events))

    def test_local_explicit_output_budget_respects_context_compression_cap(self):
        settings = Settings(context_auto_compress=True, local_context_tokens=4096)
        provider = RoleAwareModelProvider(settings)
        try:
            with patch.object(provider.ollama, "generate", return_value={"text": "{}"}) as local_generate:
                result=provider.generate(
                    [{"role": "user", "content": "hello"}],
                    "custom-local-test",
                    think=False,
                    format=self.schema,
                    max_tokens=8192,
                )

            options = local_generate.call_args.kwargs["options"]
            self.assertEqual(4096, options["num_ctx"])
            self.assertEqual(2048, options["num_predict"])
            self.assertEqual({
                "requested_max_tokens": 8192,
                "effective_max_tokens": 2048,
                "context_limited": True,
            }, result["output_budget"])
        finally:
            provider.close()


if __name__ == "__main__":
    unittest.main()
