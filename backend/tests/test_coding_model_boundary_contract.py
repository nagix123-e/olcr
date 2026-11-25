"""Deterministic tests for the Coding Task model-call boundary."""
from __future__ import annotations

import io
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.ollama import ModelFailure


STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class RecordingProvider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def generate(self, messages, model, think=None, format=None, max_tokens=None):
        self.calls.append({
            "messages": messages,
            "model": model,
            "think": think,
            "format": format,
            "max_tokens": max_tokens,
        })
        return self.result


class MissingFormatProvider:
    def __init__(self):
        self.calls = 0

    def generate(self, messages, model, think=None, max_tokens=None):
        self.calls += 1
        return {"text": '{"answer":"ok"}'}


class MissingMaxTokensProvider:
    def __init__(self):
        self.calls = 0

    def generate(self, messages, model, think=None, format=None):
        self.calls += 1
        return {"text": '{"answer":"ok"}'}


class CodingModelBoundaryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "coding.sqlite"))
        self.db.initialize()
        self.db.create_conversation("conversation", time.time(), "boundary", None)
        self.db.create_coding_task(
            "boundary-task", "conversation", "Return structured JSON.",
            "RUNNING", "VERIFICATION_CONTRACT", None, time.time(),
        )

    def invoke(self, provider, *, schema=STRUCTURED_SCHEMA):
        with patch.object(api, "db", self.db), \
             patch.object(api, "runtime", SimpleNamespace(model=provider)), \
             patch.object(api, "_coding_telemetry", {}), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return api._model_text("boundary-task", "RUNNING", "VERIFICATION_CONTRACT",
                                   "role:short_coding_fix_debug",
                                   [{"role": "user", "content": "Return one JSON object."}],
                                   structured_schema=schema)

    def assert_invalid_response(self, result):
        provider = RecordingProvider(result)
        with self.assertRaises(ModelFailure) as caught:
            self.invoke(provider)
        self.assertEqual("invalid_response", caught.exception.category)

    def test_structured_call_rejects_non_dict_provider_results_as_typed_failures(self):
        for raw in (None, "{\"answer\":\"ok\"}", [], 7):
            with self.subTest(raw_type=type(raw).__name__):
                self.assert_invalid_response(raw)

    def test_structured_call_rejects_blank_or_malformed_json_as_typed_failures(self):
        for raw in ({}, {"text": None}, {"text": ""}, {"text": " \n\t "},
                    {"text": "{"}, {"text": "not JSON"}, {"text": "[]"}):
            with self.subTest(raw=repr(raw)):
                self.assert_invalid_response(raw)

    def test_structured_call_fails_before_call_when_provider_lacks_format_capability(self):
        provider = MissingFormatProvider()
        with self.assertRaises(ModelFailure) as caught:
            self.invoke(provider)
        self.assertEqual("unsupported", caught.exception.category)
        self.assertIn("format", str(caught.exception).lower())
        self.assertEqual(0, provider.calls)

    def test_structured_call_fails_before_call_when_provider_lacks_max_tokens_capability(self):
        provider = MissingMaxTokensProvider()
        with self.assertRaises(ModelFailure) as caught:
            self.invoke(provider)
        self.assertEqual("unsupported", caught.exception.category)
        self.assertIn("max_tokens", str(caught.exception).lower())
        self.assertEqual(0, provider.calls)

    def test_structured_call_passes_both_required_capabilities_to_supported_provider(self):
        provider = RecordingProvider({"text": '{"answer":"ok"}'})

        result = self.invoke(provider)

        self.assertEqual('{"answer":"ok"}', result)
        self.assertEqual(1, len(provider.calls))
        self.assertEqual(STRUCTURED_SCHEMA, provider.calls[0]["format"])
        self.assertEqual(8192, provider.calls[0]["max_tokens"])


if __name__ == "__main__":
    unittest.main()
