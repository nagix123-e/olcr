"""Fake-transport coverage for Coding Task -> role-aware provider contracts."""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import olcr_api.app as api
from olcr_api.brain_models import BrainModels, INITIAL
from olcr_api.brain_providers import (
    CHATGPT_AUTHORIZATION_URL,
    CHATGPT_RESOURCE,
    CHATGPT_SCOPE,
    CHATGPT_TOKEN_URL,
    DEFAULT_BASE_URLS,
)
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.model_provider import RoleAwareModelProvider
from olcr_api.ollama import ModelFailure


PHASE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {"phases": {"type": "array", "items": {"type": "string"}}},
    "required": ["phases"],
    "additionalProperties": False,
}
OPERATIONS_SCHEMA = {
    "type": "object",
    "properties": {"operations": {"type": "array", "items": {"type": "string"}}},
    "required": ["operations"],
    "additionalProperties": False,
}
VERIFICATION_SCHEMA = {
    "type": "object",
    "properties": {"valid": {"type": "boolean"}},
    "required": ["valid"],
    "additionalProperties": False,
}

STAGES = (
    {
        "name": "plan",
        "status": "PLANNING",
        "activity": "QWEN_PLANNING",
        "model": "role:planner_replanner",
        "schema": PHASE_PLAN_SCHEMA,
        "text": '{"phases":["inspect"],"approved_scopes":["*"]}',
        "budget": 8192,
    },
    {
        "name": "implementation",
        "status": "RUNNING",
        "activity": "IMPLEMENTATION",
        "model": "role:long_implementation",
        "schema": OPERATIONS_SCHEMA,
        "text": '{"operations":[]}',
        "budget": 16384,
    },
    {
        "name": "verification",
        "status": "RUNNING",
        "activity": "VERIFICATION_CONTRACT",
        "model": "role:short_coding_fix_debug",
        "schema": VERIFICATION_SCHEMA,
        "text": '{"valid":true}',
        "budget": 8192,
    },
)


class CodingProviderStageMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(external_access_enabled=True, context_auto_compress=False)
        self.db = Database(Path(self.temp.name) / "coding-provider-matrix.sqlite")
        self.db.initialize()
        self.conversation_id = "matrix-conversation"
        self.task_id = "matrix-task"
        self.db.create_conversation("Provider matrix", time.time(), self.conversation_id)
        self.db.create_coding_task(
            self.task_id,
            self.conversation_id,
            "Exercise a structured coding model boundary without approving a plan.",
            "RUNNING",
            "NONE",
            None,
            time.time(),
        )
        self.brains = BrainModels(self.db, self.settings)
        self.providers = self.brains.providers
        self.models = self._register_api_models()
        self.provider = RoleAwareModelProvider(self.settings)
        self.provider.brain_models = self.brains
        self.addCleanup(self.provider.close)

    def _register_api_models(self):
        self.providers.register(
            "openai",
            "api_key",
            {
                "base_url": DEFAULT_BASE_URLS["openai"],
                "api_key": "synthetic-openai-key",
                "model_ids": "gpt-6-luna",
            },
            account_id=1,
        )
        self.providers.register(
            "openai",
            "api_key",
            {
                "base_url": "http://127.0.0.1:8765/v1",
                "api_key": "synthetic-compatible-key",
                "model_ids": "compat-fixture-model",
            },
            account_id=2,
        )
        self.providers.register(
            "google",
            "api_key",
            {
                "base_url": DEFAULT_BASE_URLS["google"],
                "api_key": "synthetic-google-key",
                "model_ids": "gemini-2.5-flash",
            },
        )
        self.providers.register(
            "anthropic",
            "api_key",
            {
                "base_url": DEFAULT_BASE_URLS["anthropic"],
                "api_key": "synthetic-anthropic-key",
                "model_ids": "claude-sonnet-4-20250514",
            },
        )
        # The OAuth connection shape is intentionally synthetic. Its only
        # transport, _responses_stream, is patched by each test below.
        oauth_model = "gpt-6-luna"
        self.providers.credentials["openai_oauth"] = {
            "access_token": "synthetic-access-token",
            "id_token": "synthetic-id-token",
            "client_id": "oaiapp_test",
            "scope": "chatgpt.tokens.use.direct",
            "expires_at": time.time() + 3600,
        }
        metadata = {
            "id": "openai_oauth",
            "account_id": 1,
            "base_url": CHATGPT_RESOURCE,
            "authorization_url": CHATGPT_AUTHORIZATION_URL,
            "token_url": CHATGPT_TOKEN_URL,
            "scope": CHATGPT_SCOPE,
            "client_id": "oaiapp_test",
            "model_ids": [oauth_model],
            "model_labels": {},
        }
        with self.db.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO brain_providers VALUES(?,?)",
                ("openai_oauth", json.dumps(metadata)),
            )
        return {
            "openai": ("api:openai:gpt-6-luna", "openai"),
            "openai-compatible": ("api:openai:compat-fixture-model", "openai-compatible"),
            "responses-oauth": ("api:openai_oauth:gpt-6-luna", "responses-oauth"),
            "google": ("api:google:gemini-2.5-flash", "google"),
            "anthropic": ("api:anthropic:claude-sonnet-4-20250514", "anthropic"),
        }

    def _save_configuration(self, selected_model: str):
        value = json.loads(json.dumps(INITIAL))
        value["selections"].update(
            {
                "general_inference": selected_model,
                "coder": selected_model,
                "short_coding": selected_model,
            }
        )
        with self.db.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO brain_configurations VALUES(?,?)",
                (self.conversation_id, json.dumps(value)),
            )

    def _invoke(self, stage, *, model=None):
        workspaces = SimpleNamespace(
            service=SimpleNamespace(instruction_context=lambda _task_id: "")
        )
        with patch.object(api, "db", self.db), \
             patch.object(api, "brain_models", self.brains), \
             patch.object(api, "runtime", SimpleNamespace(model=self.provider)), \
             patch.object(api, "task_workspaces", workspaces), \
             patch.object(api, "coding_debug_reference_context", return_value=""), \
             patch.object(api, "_coding_telemetry", {}), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return api._model_text(
                self.task_id,
                stage["status"],
                stage["activity"],
                model or stage["model"],
                [{"role": "user", "content": "Return the requested structured result."}],
                structured_schema=stage["schema"],
            )

    @staticmethod
    def _response_for(provider, text):
        if provider in {"openai", "openai-compatible"}:
            return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
        if provider == "google":
            return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}
        if provider == "anthropic":
            return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
        if provider == "responses-oauth":
            return None
        raise AssertionError(provider)

    @staticmethod
    def _assert_schema_contract(test, family, payload, schema):
        if family == "openai":
            native = (payload.get("response_format") or {}).get("json_schema", {}).get("schema")
            if native is not None:
                test.assertEqual(schema, native)
            else:
                test.assertIn(json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                              "\n".join(str(m.get("content", "")) for m in payload["messages"]))
        elif family == "google":
            native = (payload.get("generationConfig") or {}).get("responseJsonSchema")
            if native is not None:
                test.assertEqual(schema, native)
            else:
                test.assertIn(json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                              "\n".join(str(p.get("text", "")) for c in payload["contents"] for p in c.get("parts", [])))
        elif family == "anthropic":
            native = ((payload.get("output_config") or {}).get("format") or {}).get("schema")
            if native is not None:
                test.assertEqual(schema, native)
            else:
                prompt = str(payload.get("system", "")) + "\n" + "\n".join(
                    str(m.get("content", "")) for m in payload.get("messages", []))
                test.assertIn(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), prompt)
        elif family == "responses-oauth":
            native = ((payload.get("text") or {}).get("format") or {}).get("schema")
            if native is not None:
                test.assertEqual(schema, native)
            else:
                prompt = "\n".join(str(item.get("content", "")) for item in payload.get("input", []))
                test.assertIn(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), prompt)
        elif family == "openai-compatible":
            # Unknown compatible servers get the complete schema in-band,
            # without an unnegotiated protocol extension.
            test.assertNotIn("response_format", payload)
            instruction = "\n".join(str(message.get("content", "")) for message in payload["messages"])
            test.assertIn(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), instruction)
        else:
            raise AssertionError(family)

    @staticmethod
    def _assert_output_budget(test, family, payload, budget):
        if family == "openai":
            field = "max_completion_tokens"
        elif family == "openai-compatible":
            field = "max_tokens"
        elif family == "google":
            test.assertEqual(budget, payload["generationConfig"]["maxOutputTokens"])
            return
        elif family == "anthropic":
            field = "max_tokens"
        elif family == "responses-oauth":
            field = "max_output_tokens"
        else:
            raise AssertionError(family)
        test.assertEqual(budget, payload[field])

    def test_stage_and_provider_family_matrix_preserves_schema_and_output_budget(self):
        for family, (selected, transport_family) in self.models.items():
            self._save_configuration(selected)
            for stage in STAGES:
                with self.subTest(provider=family, stage=stage["name"]):
                    self.db.update_coding_task(
                        self.task_id, status="RUNNING", activity="NONE", plan=None,
                        approved_scopes=[],
                    )
                    response_text = stage["text"]
                    fake_response = self._response_for(transport_family, response_text)
                    with patch(
                        "olcr_api.brain_providers.call_json",
                        return_value=fake_response,
                    ) as request, patch.object(
                        self.providers,
                        "_responses_stream",
                        return_value=(response_text, {}),
                    ) as responses:
                        result = self._invoke(stage)

                    self.assertEqual(response_text, result)
                    if family == "responses-oauth":
                        responses.assert_called_once()
                        payload = responses.call_args.args[1]
                        self.assertEqual(0, request.call_count)
                    else:
                        request.assert_called_once()
                        payload = request.call_args.args[1]
                        self._assert_schema_contract(self, transport_family, payload, stage["schema"])
                        self._assert_output_budget(self, transport_family, payload, stage["budget"])
                    if family == "responses-oauth":
                        self._assert_schema_contract(self, transport_family, payload, stage["schema"])
                        self._assert_output_budget(self, transport_family, payload, stage["budget"])

                    # A model-proposed approval field is only untrusted plan
                    # content. Invoking the planning boundary does not persist
                    # a plan or grant mutation scope.
                    saved = self.db.coding_task(self.task_id)
                    self.assertIsNone(saved["plan"])
                    self.assertEqual([], saved["approved_scopes"])
                    self.assertEqual(stage["status"], saved["status"])

    def test_truncated_structured_plan_retries_with_a_bounded_larger_budget(self):
        selected, family = self.models["openai-compatible"]
        self._save_configuration(selected)
        stage = STAGES[0]
        truncated = {
            "choices": [
                {"message": {"content": '{"phases":['}, "finish_reason": "length"}
            ]
        }
        complete = self._response_for("openai-compatible", stage["text"])
        with patch("olcr_api.brain_providers.call_json", side_effect=[truncated, complete]) as request:
            result = self._invoke(stage)

        self.assertEqual(stage["text"], result)
        self.assertEqual(2, request.call_count)
        first, retry = [call.args[1] for call in request.call_args_list]
        self._assert_schema_contract(self, family, first, stage["schema"])
        self._assert_schema_contract(self, family, retry, stage["schema"])
        self.assertEqual(8192, first["max_tokens"])
        self.assertEqual(16384, retry["max_tokens"])

    def test_persistently_truncated_plan_exhausts_only_the_two_configured_retries(self):
        selected, family = self.models["openai-compatible"]
        self._save_configuration(selected)
        stage = STAGES[0]
        truncated = {
            "choices": [
                {"message": {"content": '{"phases":['}, "finish_reason": "length"}
            ]
        }
        with patch("olcr_api.brain_providers.call_json", return_value=truncated) as request:
            with self.assertRaises(ModelFailure) as caught:
                self._invoke(stage)

        self.assertEqual("output_limit", caught.exception.category)
        self.assertEqual(3, request.call_count)
        budgets = [call.args[1]["max_tokens"] for call in request.call_args_list]
        self.assertEqual([8192, 16384, 32000], budgets)
        for call in request.call_args_list:
            self._assert_schema_contract(self, family, call.args[1], stage["schema"])

    def test_malformed_provider_json_fails_as_a_typed_boundary_error_without_approving(self):
        selected, _family = self.models["openai"]
        self._save_configuration(selected)
        stage = STAGES[0]
        malformed = self._response_for("openai", '{"phases":[')
        with patch("olcr_api.brain_providers.call_json", return_value=malformed) as request:
            with self.assertRaises(ModelFailure) as caught:
                self._invoke(stage)

        self.assertEqual("invalid_response", caught.exception.category)
        request.assert_called_once()
        saved = self.db.coding_task(self.task_id)
        self.assertIsNone(saved["plan"])
        self.assertEqual([], saved["approved_scopes"])

    def test_local_ollama_uses_the_same_structured_boundary_and_output_cap(self):
        stage = STAGES[1]
        # A synthetic local model name takes the supported custom Ollama path.
        # Preflight is replaced so the deterministic test never probes a daemon.
        self.brains.preflight = lambda *_args, **_kwargs: {"state": "available", "reason": "test"}
        self._save_configuration(self.models["openai"][0])
        frame = {
            "model": "fixture-coder",
            "message": {"role": "assistant", "content": stage["text"]},
            "done": True,
            "done_reason": "stop",
            "eval_count": 4,
        }
        response = io.BytesIO((json.dumps(frame) + "\n").encode())
        response.headers = {}
        with patch("olcr_api.ollama.open_provider_response", return_value=response) as opened:
            result = self._invoke(stage, model="fixture-coder")

        self.assertEqual(stage["text"], result)
        request = opened.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(stage["schema"], body["format"])
        self.assertEqual(stage["budget"], body["options"]["num_predict"])
        self.assertEqual("http://127.0.0.1:11434/api/chat", request.full_url)

    def test_local_mlx_retries_reduced_reasoning_then_grows_within_context_budget(self):
        selected = "mlx:ornith_1_5_9b_8bit"
        self._save_configuration(selected)
        with self.db.connect() as con:
            row=con.execute(
                "SELECT configuration_json FROM brain_configurations WHERE scope=?",
                (self.conversation_id,),
            ).fetchone()
            value=json.loads(row[0])
            value["efforts"]["coder"]="on"
            con.execute("UPDATE brain_configurations SET configuration_json=? WHERE scope=?",
                        (json.dumps(value),self.conversation_id))

        stage=STAGES[1]
        self.settings.context_auto_compress=True
        self.settings.local_context_tokens=65536
        binding=replace(self.provider._binding("role:long_implementation"),
                        model_id=selected,runtime="mlx",deployment_model="ornith-1.5:9b")
        truncated={"text":'{"operations":[',"model":binding.deployment_model,
                   "finish_reason":"length"}
        complete={"text":stage["text"],"model":binding.deployment_model,
                  "finish_reason":"stop"}
        self.brains.preflight=lambda *_args,**_kwargs:{"state":"available","reason":"test"}
        with patch.object(self.provider,"_binding",return_value=binding), \
             patch.object(self.brains,"resolve",return_value=binding), \
             patch.object(self.provider,"_run_mlx",side_effect=[truncated,truncated,complete]) as worker:
            result=self._invoke(stage)

        self.assertEqual(stage["text"],result)
        self.assertEqual(3,worker.call_count)
        payloads=[call.args[1] for call in worker.call_args_list]
        self.assertEqual([16384,16384,32000],[payload["max_tokens"] for payload in payloads])
        self.assertEqual([True,False,False],[payload["think"] for payload in payloads])
        self.assertTrue(all(payload["format"]==stage["schema"] for payload in payloads))


if __name__ == "__main__":
    unittest.main()
