import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import PropertyMock, patch

_tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
os.environ["OLCR_DB_PATH"] = str(Path(_tmp.name) / "wikipedia-routing.sqlite")
os.environ["OLCR_ALLOWED_ROOTS"] = _tmp.name

from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api import external_tools
from olcr_api.db import Database
from olcr_api.config import Settings


def wikipedia_result(title="Anthropic"):
    return {"tool_id":"knowledge.wikimedia", "provider":"Wikimedia", "fetched_at":"2026-09-16T00:00:00Z",
            "data":{"title":title, "description":"AI safety company", "extract":"Anthropic is an artificial intelligence company.",
                    "page_url":"https://en.wikipedia.org/wiki/Anthropic", "language":"en"},
            "sources":[{"title":title,"provider":"Wikipedia","canonical_url":"https://en.wikipedia.org/wiki/Anthropic"}]}


def wikipedia_malformed_result():
    return {"tool_id":"knowledge.wikimedia", "provider":"Wikimedia", "fetched_at":"2026-09-16T00:00:00Z",
            "data":{"title":"", "extract":"", "page_url":"", "language":"en"},
            "sources":[]}


class WikipediaChatRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.old_db, self.old_settings = api.db, api.settings
        api.db = Database(str(Path(self.tmp.name) / "test.sqlite")); api.db.initialize()
        api.rebuild(Settings(db_path=str(Path(self.tmp.name) / "test.sqlite"), allowed_roots=(self.tmp.name,), external_access_enabled=True))
        api.db.create_project("project", self.tmp.name, time.time(), "project")
        api.db.create_conversation("conversation", time.time(), "conversation", "project")
        self.client = TestClient(api.app)

    def tearDown(self):
        api.db, api.settings = self.old_db, self.old_settings
        api.rebuild(self.old_settings)
        self.tmp.cleanup()

    def _chat(self, message, stream=False):
        endpoint = "/api/chat/stream" if stream else "/api/chat"
        response = self.client.post(endpoint, json={"message":message,"project_id":"project","conversation_id":"conversation"})
        return response

    def _fallback_model(self, answer=None):
        stack = ExitStack()
        stack.enter_context(patch('olcr_api.runtime.brave_search', return_value=[]))
        stack.enter_context(patch('olcr_api.runtime.tavily_search', return_value=[]))
        stack.enter_context(patch('olcr_api.runtime.web_search', return_value=[]))
        stack.enter_context(patch('olcr_api.external_tools.execute', side_effect=api.ExternalToolError('PROVIDER_UNAVAILABLE')))
        stack.enter_context(patch.object(api.runtime.model, 'generate',
            return_value={'text': answer or '大谷翔平は、投手と打者の二刀流で知られる日本の野球選手です。',
                          'model_role': 'planner_replanner'}))
        return stack

    def test_embedding_digest_failure_and_exhausted_sources_still_answer_both_chat_apis(self):
        from types import SimpleNamespace
        from olcr_api.semantic import EmbeddingFailure
        for stream in (False, True):
            api._API_ROUTER_EMBEDDING_CACHE.clear()
            with self.subTest(stream=stream), self._fallback_model(), \
                 patch.object(api, 'vectors', SimpleNamespace(state='ready')), \
                 patch.object(api._api_embedding_provider, 'embed', side_effect=EmbeddingFailure('digest_mismatch', 'digest mismatch')) as embedding, \
                 patch.object(api.laya_provider.local, 'loaded', False), \
                 patch.object(api, 'execute_external_tool', side_effect=api.ExternalToolError('PROVIDER_UNAVAILABLE')) as source, \
                 patch.object(api.runtime.model, 'generate', wraps=api.runtime.model.generate) as model:
                response = self._chat('大谷翔平の経歴を調べて。その要点も説明して', stream=stream)
                self.assertEqual(200, response.status_code, response.text)
                if stream:
                    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                    answer = ''.join(event.get('text', '') for event in events if event['type'] == 'chunk')
                    task = next(event['task'] for event in events if event['type'] == 'done')
                else:
                    answer, task = response.json()['response'], response.json()['task']
                self.assertIn('二刀流', answer)
                self.assertIn('一般知識', answer)
                self.assertNotIn('代替ソースを確認しました', answer)
                self.assertNotIn('digest_mismatch', answer)
                self.assertEqual('completed', task['state'])
                self.assertTrue(any('inference_fallback' in row for row in task['selected_context']))
                embedding.assert_called()
                source.assert_called_once()
                self.assertEqual('knowledge.wikimedia', source.call_args.args[0])
                model.assert_called_once()
                self.assertEqual(api.settings.planner_model, model.call_args.args[1])
                self.assertNotIn('format', model.call_args.kwargs)
                self.assertIn('do not invent', str(model.call_args.args[0]))

    def test_source_intent_and_subject_normalization(self):
        cases = {
            "search on wikipedia about anthropic": "anthropic",
            "search wikipedia for anthropic": "anthropic",
            "look up Anthropic on Wikipedia": "Anthropic",
            "anthropic wikipedia search": "anthropic",
            "WikipediaでAnthropicについて検索して": "Anthropic",
        }
        for message, query in cases.items():
            with self.subTest(message=message):
                tool, arguments = external_tools.route(message)
                self.assertEqual("knowledge.wikimedia", tool)
                self.assertEqual(query.casefold(), arguments["query"].casefold())

    def test_ohtani_real_adapter_returns_verified_article_through_both_chat_endpoints(self):
        page = {"query": {"pages": {"42": {"pageid": 42, "title": "大谷翔平",
                "extract": "大谷翔平は岩手県出身で、投手と打者の二刀流で知られる日本の野球選手です。"}}}}
        for stream in (False, True):
            with self.subTest(stream=stream), \
                 patch.object(api, "route_external_tool", return_value=None), \
                 patch.object(api, "router_decision", return_value=None), \
                 patch.object(external_tools, "_request", return_value=page) as request, \
                 patch.object(api.runtime.model, "generate", side_effect=AssertionError("Article evidence must remain usable without Brain")):
                response = self._chat("大谷翔平について教えて", stream=stream)
            self.assertEqual(200, response.status_code, response.text)
            if stream:
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                answer = ''.join(event.get('text', '') for event in events if event['type'] == 'chunk')
                task = next(event['task'] for event in events if event['type'] == 'done')
            else:
                answer, task = response.json()['response'], response.json()['task']
            self.assertIn("岩手県", answer)
            self.assertIn("ja.wikipedia.org", answer)
            self.assertNotIn("一般知識", answer)
            self.assertEqual("completed", task['state'])
            self.assertEqual("EXTERNAL_API", task['route'])
            request.assert_called_once()

    def test_general_product_research_uses_knowledge_on_both_transports(self):
        page = {"query": {"pages": {"43": {"pageid": 43, "title": "ママレモン",
                "extract": "ママレモンは台所用洗剤のブランドです。"}}}}
        for stream in (False, True):
            with self.subTest(stream=stream), \
                 patch.object(external_tools, "_request", return_value=page) as request, \
                 patch.object(api.laya_provider, "choice", side_effect=AssertionError("No specialist classification for an open topic")), \
                 patch.object(api.runtime.model, "generate", side_effect=AssertionError("Use verified article evidence")):
                response = self._chat("ママレモンについて調べて", stream=stream)
            self.assertEqual(200, response.status_code, response.text)
            self.assertIn("台所用洗剤", response.text)
            self.assertNotIn("化合物名", response.text)
            request.assert_called_once()

    def test_legacy_api_data_survives_composer_timeout_without_source_fallback(self):
        from olcr_api.models import Task, TaskState
        failed = Task("Tokyo weather")
        failed.transition(TaskState.ROUTING); failed.transition(TaskState.GENERATING)
        failed.error = "model timed out"; failed.transition(TaskState.FAILED)
        result = {"tool_id": "weather.open_meteo", "provider": "Open-Meteo", "fetched_at": "2026-10-19",
                  "data": {"location": {"name": "Tokyo"}, "weather": {"current": {"temperature_2m": 21}}},
                  "sources": [{"title": "Open-Meteo", "provider": "Open-Meteo", "canonical_url": "https://open-meteo.com/"}]}
        with patch.object(api.runtime, "compose_tool_result", return_value=(failed, "")):
            task, answer = api.compose_external_result("Tokyo weather", result)
        self.assertEqual(TaskState.COMPLETED, task.state)
        self.assertIsNone(task.error)
        self.assertIn("Tokyo", answer)
        self.assertIn("Open-Meteo", answer)
        self.assertTrue(any("composition_fallback" in row for row in task.selected_context))

    def test_exact_gui_path_executes_wikipedia_and_persists_user_facing_response(self):
        with patch.object(api, "execute_external_tool", return_value=wikipedia_result()) as execute, \
             patch.object(api, "router_decision", side_effect=AssertionError("explicit Wikipedia must bypass API Router")):
            response = self._chat("search on wikipedia about anthropic")
        self.assertEqual(200, response.status_code)
        body=response.json()
        self.assertIn("Anthropic", body["response"])
        self.assertIn("Wikipedia", body["response"])
        self.assertNotIn("retrieval_method", body["response"])
        self.assertNotIn("normalizer_diagnostics", body["response"])
        self.assertEqual("anthropic", execute.call_args.args[1]["query"])
        messages=api.db.conversation("conversation")["messages"]
        self.assertEqual(body["response"], messages[-1]["content"])

    def test_selected_provider_failure_falls_back_to_search_then_wikipedia(self):
        prompt = "Show the latest public earthquake activity in Japan"
        fallback_task = api.Task(prompt)
        fallback_task.transition(api.TaskState.ROUTING)
        fallback_task.transition(api.TaskState.GENERATING)
        fallback_task.transition(api.TaskState.COMPLETED)
        with patch.object(api, "route_external_tool", return_value=("earth.earthquake", {"query": "Japan"})), \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "external_tool_credential_setup", return_value=None), \
             patch.object(api, "compile_provider_arguments", return_value={"query": "Japan"}), \
             patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("PROVIDER_TIMEOUT")), \
             patch.object(api, "runtime") as runtime:
            runtime.execute.return_value = (fallback_task, "Fallback evidence from Wikipedia")
            response = self._chat(prompt)
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertIn("Fallback evidence from Wikipedia", body["response"])
        self.assertIn("代替ソースを確認しました", body["response"])
        runtime.execute.assert_called_once_with(
            prompt, suppress_web=False, api_miss_fallback=True,
            wikipedia_fallback_allowed=True,
            external_search_query=prompt + ' USGS Earthquake API official source',
        )
        self.assertTrue(any(item.get("tool") == "earth.earthquake"
                            and item.get("error") == "PROVIDER_TIMEOUT"
                            for item in fallback_task.tool_executions))

    def test_selected_provider_empty_result_uses_the_same_grouped_fallback(self):
        prompt = "Find recent court decisions about online platforms"
        fallback_task = api.Task(prompt)
        fallback_task.transition(api.TaskState.ROUTING)
        fallback_task.transition(api.TaskState.GENERATING)
        fallback_task.transition(api.TaskState.COMPLETED)
        empty_result = {"tool_id": "law.us_courts", "provider": "CourtListener",
                        "data": {"semantic_status": "EMPTY", "items": [], "item_count": 0},
                        "sources": []}
        with patch.object(api, "route_external_tool", return_value=("law.us_courts", {"query": "online platforms"})), \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "external_tool_credential_setup", return_value=None), \
             patch.object(api, "compile_provider_arguments", return_value={"query": "online platforms"}), \
             patch.object(api, "execute_external_tool", return_value=empty_result), \
             patch.object(api, "runtime") as runtime:
            runtime.execute.return_value = (fallback_task, "Search fallback evidence")
            response = self._chat(prompt)
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("Search fallback evidence", response.json()["response"])
        runtime.execute.assert_called_once_with(
            prompt, suppress_web=False, api_miss_fallback=True,
            wikipedia_fallback_allowed=True,
            external_search_query=prompt + ' CourtListener official source',
        )

    def test_unmatched_natural_knowledge_question_falls_back_to_wikipedia(self):
        prompt = "Tell me about the history of the Silk Road"
        with patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "execute_external_tool", return_value=wikipedia_result("Silk Road")) as execute, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("Wikipedia is the no-search fallback")):
            response = self._chat(prompt)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("knowledge.wikimedia", execute.call_args.args[0])
        self.assertEqual("the Silk Road", execute.call_args.args[1]["query"])
        self.assertIn("Wikipedia", response.json()["response"])
        self.assertEqual("api_miss_wikipedia_fallback",
                         response.json()["task"]["selected_context"][-1]["api_routing"]["retrieval"])

    def test_wabi_sabi_explanation_uses_wikipedia_when_no_knowledge_api_matches(self):
        prompt = "日本のわびさびって、結局どういう考え方?"
        with patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "execute_external_tool", return_value=wikipedia_result("Wabi-sabi")) as execute:
            response = self._chat(prompt)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("knowledge.wikimedia", execute.call_args.args[0])
        self.assertIn("わびさび", execute.call_args.args[1]["query"])
        self.assertIn("Wikipedia", response.json()["response"])

    def test_wabi_sabi_question_is_normalized_to_the_subject(self):
        self.assertEqual("日本のわびさび", external_tools.normalize_wiki_arguments({
            "query": "日本のわびさびって、結局どういう考え方?",
        })["query"])

    def test_wikipedia_discards_broad_search_hit_when_subject_matches_later_result(self):
        search = {"query": {"search": [
            {"title": "日本の百貨店", "snippet": "日本における百貨店の歴史と小売業について。"},
            {"title": "侘び寂び", "snippet": "日本の伝統的な美意識である侘び寂びについて解説。"},
        ]}}
        page = {"query": {"pages": {"42": {
            "pageid": 42, "title": "侘び寂び", "description": "日本の美意識",
            "extract": "不完全さや移ろいの中に美を見いだす日本の美意識。",
        }}}}
        missing = {"query": {"pages": {"-1": {"title": "日本のわびさび", "missing": ""}}}}
        with patch.object(external_tools, "_request", side_effect=[missing, search, page]):
            result = external_tools.wiki("日本のわびさび", language="ja")
        self.assertEqual("侘び寂び", result["data"]["title"])
        self.assertIn("美意識", result["data"]["extract"])
        self.assertEqual("侘び寂び", result["sources"][0]["title"])

    def test_streaming_wabi_sabi_uses_the_same_wikipedia_fallback(self):
        prompt = "日本のわびさびって、結局どういう考え方?"
        with patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "execute_external_tool", return_value=wikipedia_result("Wabi-sabi")) as execute:
            response = self._chat(prompt, stream=True)
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("Wikipedia", response.text)
        self.assertEqual("knowledge.wikimedia", execute.call_args.args[0])
        self.assertIn("わびさび", execute.call_args.args[1]["query"])

    def test_laya_no_match_uses_wikipedia_instead_of_nearest_unrelated_api(self):
        class FakeVectors:
            state = "ready"

        prompt = "シルクロードの歴史を要約して、主な時代と交易への影響を教えて。"

        def embed(texts, _model):
            return [[1.0, 0.0] if index == 0 or "humanitarian.unhcr" in text else [0.0, 1.0]
                    for index, text in enumerate(texts)]

        choice_candidates = []

        def choose(*, question, language, candidates):
            choice_candidates.extend(candidates)
            return {"tool_id": "__no_matching_api__", "confidence": 0.0}

        with patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api, "vectors", FakeVectors()), \
             patch.object(api, "_api_embedding_provider") as embedding, \
             patch.object(type(api.laya_provider), "loaded", new_callable=PropertyMock, return_value=True), \
             patch.object(api.laya_provider, "choice", side_effect=choose), \
             patch.object(api, "execute_external_tool", return_value=wikipedia_result("Silk Road")) as execute:
            embedding.embed.side_effect = embed
            response = self._chat(prompt)

        self.assertEqual(200, response.status_code, response.text)
        self.assertLessEqual(len(choice_candidates), 8)
        self.assertEqual("__no_matching_api__", choice_candidates[-1]["tool_id"])
        self.assertEqual("knowledge.wikimedia", execute.call_args.args[0])
        self.assertIn("Wikipedia", response.json()["response"])
        self.assertEqual("api_miss_wikipedia_fallback",
                         response.json()["task"]["selected_context"][-1]["api_routing"]["retrieval"])

    def test_wikipedia_fallback_still_obeys_global_external_access_gate(self):
        api.settings.external_access_enabled = False
        with patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api, "router_decision", return_value=None), \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("disabled external access must not call Wikipedia")) as execute:
            response = self._chat("Tell me about the history of the Silk Road")
        self.assertEqual(200, response.status_code)
        self.assertIn('外部アクセスはOFF', response.json()['response'])
        self.assertEqual('EXTERNAL_ACCESS_REQUIRED', response.json()['task']['error'])
        execute.assert_not_called()

    def test_chat_provider_failure_returns_and_persists_route_evidence(self):
        fallback_task = api.Task("weather in Tokyo")
        fallback_task.transition(api.TaskState.ROUTING)
        fallback_task.transition(api.TaskState.GENERATING)
        fallback_task.transition(api.TaskState.COMPLETED)
        with patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("PROVIDER_RATE_LIMITED")), \
             patch.object(api.runtime, "execute", return_value=(fallback_task, "Fallback weather evidence")):
            response = self._chat("weather in Tokyo")

        self.assertEqual(200, response.status_code)
        body = response.json()
        task = body["task"]
        self.assertEqual("EXTERNAL_API", task["route"])
        self.assertEqual("completed", task["state"])
        self.assertIn({"api_routing": {
            "retrieval": "deterministic", "candidate_ids": ["weather.open_meteo"],
            "laya_called": False, "selected": "weather.open_meteo",
        }}, task["selected_context"])
        self.assertIn("Fallback weather evidence", body["response"])
        self.assertTrue(any(entry.get("direct_provider_failure_fallback", {}).get("error") == "PROVIDER_RATE_LIMITED"
                            for entry in task["selected_context"]))
        with api.db.connect() as conn:
            stored = conn.execute(
                "SELECT route,error,selected_context_json FROM tasks WHERE id=?", (task["id"],)
            ).fetchone()
            execution = conn.execute(
                "SELECT tool_name,status,error FROM tool_executions WHERE task_id=?", (task["id"],)
            ).fetchone()
        self.assertEqual("EXTERNAL_API", stored["route"])
        self.assertEqual(task["selected_context"], json.loads(stored["selected_context_json"]))
        self.assertEqual(("weather.open_meteo", "failed", "PROVIDER_RATE_LIMITED"), tuple(execution))

    def test_stream_provider_failure_returns_and_persists_route_evidence(self):
        fallback_task = api.Task("weather in Tokyo")
        fallback_task.transition(api.TaskState.ROUTING)
        fallback_task.transition(api.TaskState.GENERATING)
        fallback_task.transition(api.TaskState.COMPLETED)
        with patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("PROVIDER_RATE_LIMITED")), \
             patch.object(api.runtime, "execute", return_value=(fallback_task, "Fallback weather evidence")):
            response = self._chat("weather in Tokyo", stream=True)

        self.assertEqual(200, response.status_code)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        done = next(event for event in events if event.get("type") == "done")
        task = done["task"]
        self.assertEqual("EXTERNAL_API", task["route"])
        self.assertEqual("completed", task["state"])
        self.assertIn("Fallback weather evidence", next(event["text"] for event in events if event.get("type") == "chunk"))
        self.assertIn({"api_routing": {
            "retrieval": "deterministic", "candidate_ids": ["weather.open_meteo"],
            "laya_called": False, "selected": "weather.open_meteo",
        }}, task["selected_context"])
        with api.db.connect() as conn:
            stored = conn.execute(
                "SELECT route,error,selected_context_json FROM tasks WHERE id=?", (task["id"],)
            ).fetchone()
            execution = conn.execute(
                "SELECT tool_name,status,error FROM tool_executions WHERE task_id=?", (task["id"],)
            ).fetchone()
        self.assertEqual("EXTERNAL_API", stored["route"])
        self.assertEqual(task["selected_context"], json.loads(stored["selected_context_json"]))
        self.assertEqual(("weather.open_meteo", "failed", "PROVIDER_RATE_LIMITED"), tuple(execution))

    def test_stream_external_access_gate_is_explicit_and_never_executes_provider(self):
        api.rebuild(Settings(db_path=str(Path(self.tmp.name) / "test.sqlite"),
                            allowed_roots=(self.tmp.name,), external_access_enabled=False))
        with patch.object(api, "execute_external_tool", side_effect=AssertionError("external request must be gated")) as execute:
            response = self._chat("weather in Tokyo", stream=True)

        self.assertEqual(200, response.status_code)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        chunk = next(event for event in events if event.get("type") == "chunk")
        done = next(event for event in events if event.get("type") == "done")
        self.assertIn("外部アクセスはOFF", chunk["text"])
        self.assertIn("接続していません", chunk["text"])
        self.assertEqual("EXTERNAL_ACCESS_REQUIRED", done["task"]["error"])
        execute.assert_not_called()

    def test_streaming_path_does_not_use_local_retrieval_or_emit_diagnostics(self):
        with patch.object(api, "execute_external_tool", return_value=wikipedia_result()) as execute, \
             patch.object(api.retrieval, "retrieve", side_effect=AssertionError("local retrieval must not run")):
            response=self._chat("search on wikipedia about anthropic", stream=True)
        self.assertEqual(200, response.status_code)
        wire=response.text
        self.assertIn("Anthropic", wire)
        self.assertNotIn("retrieval_method", wire)
        self.assertNotIn("normalizer_diagnostics", wire)
        self.assertEqual("anthropic", execute.call_args.args[1]["query"])

    def test_streaming_path_uses_shared_laya_api_router_for_unresolved_provider_intent(self):
        prompt = "Rosneftが現在の制裁リストに掲載されているか、利用可能なAPIで確認して。"
        # GUI projects commonly have the Coding Task gate enabled. External
        # intent must still reach the shared router rather than being rejected
        # as an ambiguous coding scope before API selection runs.
        api.settings = api.settings.with_overrides({"task_manager_enabled": True})
        external_tools._RUNTIME_CREDENTIALS.pop("sanctions.opensanctions", None)
        with self._fallback_model('現在の制裁指定状況は未確認です。'), \
             patch.object(api, "route_external_tool", return_value=None), \
             patch.object(api, "router_decision", return_value=("sanctions.opensanctions", {"query": "Rosneft"})) as decide, \
             patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("CONFIG_REQUIRED")) as execute, \
             patch.dict(os.environ, {"OLCR_OPENSANCTIONS_API_KEY": ""}):
            response = self._chat(prompt, stream=True)

        self.assertEqual(200, response.status_code)
        credential_prompt = "このAPIを利用するには、プロバイダーの認証情報を下の専用欄に設定してください。"
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertIn(credential_prompt, "".join(event.get("text", "") for event in events))
        self.assertEqual("credential_setup", next(event["block"]["type"] for event in events if event.get("type") == "block"))
        done = next(event for event in events if event.get("type") == "done")
        self.assertEqual('completed', done['task']['state'])
        self.assertTrue(any('inference_fallback' in row for row in done['task']['selected_context']))
        decide.assert_called_once_with(prompt)
        execute.assert_not_called()
        messages = api.db.conversation("conversation")["messages"]
        self.assertEqual(1, sum(message["role"] == "user" and message["content"] == prompt for message in messages))
        assistant = next(message for message in messages if message["role"] == "assistant")
        self.assertIn(credential_prompt, assistant["content"])
        self.assertEqual("credential_setup", assistant["blocks"][0]["type"])

    def test_streaming_path_honors_explicit_no_tool_execution_request(self):
        prompt = "Find API candidates for a Python sum question, but do not execute any tools or APIs."
        with patch.object(api, "route_external_tool", side_effect=AssertionError("prohibited tool routing ran")) as route, \
             patch.object(api, "router_decision", side_effect=AssertionError("prohibited API router ran")) as decide, \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("prohibited provider executed")) as execute, \
             patch.object(api.runtime, "execute", return_value=(api.Task(prompt), "No tool was executed.")) as run:
            response = self._chat(prompt, stream=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("No tool was executed.", response.text)
        route.assert_not_called()
        decide.assert_not_called()
        execute.assert_not_called()
        self.assertTrue(run.call_args.kwargs["suppress_web"])

    def test_streaming_g_acceptance_prompt_never_routes_or_executes_external_candidates(self):
        prompt = (
            "Use EmbeddingGemma for repo/document, API/tool, and Skill candidate retrieval only. "
            "Do not execute anything."
        )
        candidate_result = {"response": "Candidates retrieved; nothing was executed.", "candidate_retrieval": {
            "document_candidates": [], "api_candidates": [], "skill_candidates": [],
            "embedding_status": {"documents": "PASS", "api": "PASS", "skills": "PASS"}}}
        with patch.object(api, "_candidate_retrieval_only_response", return_value=candidate_result) as retrieve, \
             patch.object(api, "route_external_tool", side_effect=AssertionError("G prohibits provider routing")) as route, \
             patch.object(api, "router_decision", side_effect=AssertionError("G prohibits API selection")) as decide, \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("G prohibits provider execution")) as execute, \
             patch.object(api, "_automatic_skill_selection", side_effect=AssertionError("G prohibits loading a Skill")) as skill, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("G prohibits normal model execution")) as run:
            response = self._chat(prompt, stream=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Candidates retrieved; nothing was executed.", response.text)
        self.assertIn('"response_owner": "CANDIDATE_RETRIEVAL_ONLY"', response.text)
        retrieve.assert_called_once()
        self.assertEqual(prompt, retrieve.call_args.args[0])
        route.assert_not_called()
        decide.assert_not_called()
        execute.assert_not_called()
        skill.assert_not_called()
        run.assert_not_called()

    def test_canonical_g_local_source_research_records_all_candidate_retrieval_without_execution(self):
        prompt = (
            "This is not a coding task. Do not modify files. Search for the function that retrieves Skill "
            "candidates and summarize its ranking stages. Use only this local repository and cite exact "
            "relative paths; no web or external services."
        )
        candidate_audit = {
            "document_candidates": [{"source": "backend/olcr_api/app.py", "line_start": 5711}],
            "api_candidates": [{"tool_id": "knowledge.wikimedia"}],
            "skill_candidates": [{"name": "find-skills"}],
            "embedding_status": {"documents": "PASS", "api": "PASS", "skills": "PASS"},
        }
        task = api.Task(prompt)
        task.route = api.Route.RETRIEVAL
        with patch.object(api, "_retrieve_candidates_only", return_value=candidate_audit) as retrieve, \
             patch.object(api, "_automatic_skill_selection", side_effect=AssertionError("source inspection must not load/select a Skill")) as skill, \
             patch.object(api, "route_external_tool", side_effect=AssertionError("external provider routing is prohibited")) as route, \
             patch.object(api, "router_decision", side_effect=AssertionError("external API selection is prohibited")) as decide, \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("external provider execution is prohibited")) as execute, \
             patch.object(api.runtime, "execute", return_value=(task, "Grounded local summary.")) as run:
            response = self._chat(prompt, stream=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Grounded local summary.", response.text)
        self.assertIn('"response_owner": "LOCAL_SOURCE_RESEARCH"', response.text)
        retrieve.assert_called_once_with(prompt, workspace_root=self.tmp.name)
        run.assert_called_once()
        self.assertTrue(run.call_args.kwargs["suppress_web"])
        self.assertEqual(self.tmp.name, run.call_args.kwargs["workspace_root"])
        self.assertIn({"source_inspection_candidate_audit": candidate_audit}, task.selected_context)
        skill.assert_not_called()
        route.assert_not_called()
        decide.assert_not_called()
        execute.assert_not_called()

    def test_workspace_candidate_audit_preserves_safe_embedding_failure_category(self):
        source = Path(self.tmp.name) / "local-source.py"
        source.write_text("def local_source():\n    return 'local evidence'\n", encoding="utf-8")
        with patch.object(api._api_embedding_provider, "embed",
                          side_effect=api.EmbeddingFailure("provider_error", "private provider detail")):
            candidates, status, files_read = api._retrieve_workspace_candidates(
                "summarize local_source", self.tmp.name)

        self.assertEqual([], candidates)
        self.assertEqual("DEGRADED_EmbeddingFailure_provider_error", status)
        self.assertEqual(1, files_read)

    def test_workspace_candidate_audit_batches_realistic_document_embeddings(self):
        source = Path(self.tmp.name) / "many-lines.md"
        source.write_text("\n".join(f"chunk-{index} " + ("evidence " * 130) for index in range(40)),
                          encoding="utf-8")
        calls = []

        def embed(texts, model):
            calls.append(len(texts))
            return [[1.0, 0.0] for _ in texts]

        with patch.object(api._api_embedding_provider, "embed", side_effect=embed):
            candidates, status, files_read = api._retrieve_workspace_candidates(
                "summarize local evidence", self.tmp.name)

        self.assertEqual("PASS", status)
        self.assertEqual(1, files_read)
        self.assertTrue(candidates)
        self.assertEqual([1, 32, 8], calls)

    def test_chat_local_only_search_prohibition_reaches_runtime_web_guard(self):
        prompt = "This is not a coding task. Do not modify code. Search local project documents only; do not call external APIs."
        with patch.object(api, "route_external_tool", side_effect=AssertionError("prohibited tool routing ran")) as route, \
             patch.object(api, "router_decision", side_effect=AssertionError("prohibited API router ran")) as decide, \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("prohibited provider executed")) as execute, \
             patch.object(api.runtime, "execute", return_value=(api.Task(prompt), "Local retrieval only.")) as run:
            response = self._chat(prompt)
        self.assertEqual(200, response.status_code)
        self.assertEqual("Local retrieval only.", response.json()["response"])
        route.assert_not_called()
        decide.assert_not_called()
        execute.assert_not_called()
        self.assertTrue(run.call_args.kwargs["suppress_web"])

    def test_chat_g_candidate_only_request_bypasses_ambiguous_coding_gate(self):
        prompt = "Use EmbeddingGemma for repo/document, API/tool, and Skill candidate retrieval only. Do not execute anything."
        with patch.object(api, "_candidate_retrieval_only_response", return_value={
                 "response": "Candidate-only result.", "candidate_retrieval": {"api_candidates": []}}) as retrieve, \
             patch.object(api, "router_decision", side_effect=AssertionError("candidate listing must not select a provider")) as decide, \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("candidate listing must not execute a provider")) as execute, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("candidate listing must not invoke a model")) as run:
            response = self._chat(prompt)
        self.assertEqual(200, response.status_code)
        self.assertEqual("Candidate-only result.", response.json()["response"])
        self.assertEqual([], api.db.coding_tasks("conversation"))
        retrieve.assert_called_once()
        self.assertEqual(prompt, retrieve.call_args.args[0])
        decide.assert_not_called()
        execute.assert_not_called()
        run.assert_not_called()

    def test_japanese_candidate_search_only_request_never_executes_external_provider(self):
        prompt = (
            "EmbeddingGemmaを使って、このプロジェクトのリポジトリ／文書、API／ツール、Skillの候補検索だけをしてください。"
            "候補を表示し、API・ツール・Skillの実行や読み込みは行わないでください。ファイル変更も禁止です。"
        )
        candidate_result = {"response": "候補検索のみを完了しました。実行や読み込みはしていません。",
                            "candidate_retrieval": {"document_candidates": [], "api_candidates": [],
                                                     "skill_candidates": [], "embedding_status": {}}}
        with patch.object(api, "_candidate_retrieval_only_response", return_value=candidate_result) as retrieve, \
             patch.object(api, "route_external_tool", side_effect=AssertionError("候補検索のみでprovider routingは禁止")) as route, \
             patch.object(api, "router_decision", side_effect=AssertionError("候補検索のみでAPI選択は禁止")) as decide, \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("候補検索のみでprovider実行は禁止")) as execute, \
             patch.object(api, "_automatic_skill_selection", side_effect=AssertionError("Skill読込は禁止")) as skill, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("通常モデル実行は禁止")) as run:
            response = self._chat(prompt)

        self.assertEqual(200, response.status_code)
        self.assertEqual(candidate_result["response"], response.json()["response"])
        self.assertEqual([], api.db.coding_tasks("conversation"))
        retrieve.assert_called_once()
        self.assertEqual(prompt, retrieve.call_args.args[0])
        route.assert_not_called()
        decide.assert_not_called()
        execute.assert_not_called()
        skill.assert_not_called()
        run.assert_not_called()

    def test_streaming_japanese_candidate_search_only_request_keeps_execution_prohibited(self):
        prompt = (
            "EmbeddingGemmaを使って、このプロジェクトのリポジトリ／文書、API／ツール、Skillの候補検索だけをしてください。"
            "候補を表示し、API・ツール・Skillの実行や読み込みは行わないでください。ファイル変更も禁止です。"
        )
        candidate_result = {"response": "候補検索のみ。実行していません。", "candidate_retrieval": {
            "document_candidates": [], "api_candidates": [], "skill_candidates": [],
            "embedding_status": {"documents": "PASS", "api": "PASS", "skills": "PASS"}}}
        with patch.object(api, "_candidate_retrieval_only_response", return_value=candidate_result) as retrieve, \
             patch.object(api, "route_external_tool", side_effect=AssertionError("候補検索のみでprovider routingは禁止")) as route, \
             patch.object(api, "router_decision", side_effect=AssertionError("候補検索のみでAPI選択は禁止")) as decide, \
             patch.object(api, "execute_external_tool", side_effect=AssertionError("候補検索のみでprovider実行は禁止")) as execute, \
             patch.object(api, "_automatic_skill_selection", side_effect=AssertionError("Skill読込は禁止")) as skill, \
             patch.object(api.runtime, "execute", side_effect=AssertionError("通常モデル実行は禁止")) as run:
            response = self._chat(prompt, stream=True)

        self.assertEqual(200, response.status_code)
        self.assertIn(candidate_result["response"], response.text)
        self.assertIn('"response_owner": "CANDIDATE_RETRIEVAL_ONLY"', response.text)
        self.assertEqual([], api.db.coding_tasks("conversation"))
        retrieve.assert_called_once()
        self.assertEqual(prompt, retrieve.call_args.args[0])
        route.assert_not_called()
        decide.assert_not_called()
        execute.assert_not_called()
        skill.assert_not_called()
        run.assert_not_called()

    def test_wikipedia_zero_results_are_user_facing(self):
        with self._fallback_model(), patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("WIKIMEDIA_NOT_FOUND")):
            response=self._chat("search wikipedia for anthropic")
        self.assertEqual(200, response.status_code)
        self.assertIn("一般知識", response.json()["response"])
        self.assertNotIn("WIKIMEDIA_NOT_FOUND", response.json()["response"])
        self.assertNotIn("retrieval", response.json()["response"].lower())

    def test_wikipedia_provider_failure_is_user_facing(self):
        with self._fallback_model(), patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("PROVIDER_UNAVAILABLE")):
            response=self._chat("search wikipedia for anthropic")
        self.assertEqual(200, response.status_code)
        self.assertIn("一般知識", response.json()["response"])
        self.assertNotIn("PROVIDER_UNAVAILABLE", response.json()["response"])
        self.assertNotIn("retrieval", response.json()["response"].lower())

    def test_wikipedia_malformed_payload_is_not_reported_as_success(self):
        with self._fallback_model(), patch.object(api, "execute_external_tool", return_value=wikipedia_malformed_result()):
            response=self._chat("search wikipedia for anthropic")
        self.assertEqual(200, response.status_code)
        self.assertIn('一般知識', response.json()['response'])
        self.assertEqual([], response.json()['sources'])
        self.assertNotIn("retrieval_method", response.json()["response"])

    def test_wikipedia_failure_does_not_persist_diagnostics(self):
        with self._fallback_model(), patch.object(api, "execute_external_tool", side_effect=api.ExternalToolError("WIKIMEDIA_NOT_FOUND")):
            response=self._chat("search wikipedia for anthropic")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("WIKIMEDIA_NOT_FOUND", response.json()["response"])
        self.assertNotIn("retrieval", response.json()["response"].lower())
        self.assertEqual(response.json()["response"], api.db.conversation("conversation")["messages"][-1]["content"])

    def test_generic_and_local_routes_are_not_forced_to_wikipedia(self):
        generic=external_tools.route("search the web for Anthropic")
        self.assertTrue(generic is None or generic[0] != "knowledge.wikimedia")
        self.assertIsNone(external_tools.route("search for local-file-name"))
