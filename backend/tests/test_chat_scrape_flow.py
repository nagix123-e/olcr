import os
from pathlib import Path
import json
import shlex
import socket
import sys
import threading
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

_profile = tempfile.TemporaryDirectory()
os.environ["OLCR_DB_PATH"] = str(Path(_profile.name) / "chat-scrape.sqlite")
os.environ["OLCR_ALLOWED_ROOTS"] = _profile.name

try:
    from fastapi.testclient import TestClient
    import olcr_api.app as api
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "FastAPI test dependencies not installed")
class ChatScrapeFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        self.original_settings = api.settings
        self.original_sessions = api.browser_sessions
        self.original_run_sessions = api.scrape_runs.browser_sessions
        self.original_local_e2e = api.local_e2e
        self.conversation_ids = []

    def tearDown(self):
        api.settings = self.original_settings
        api.browser_sessions = self.original_sessions
        api.scrape_runs.browser_sessions = self.original_run_sessions
        api.local_e2e = self.original_local_e2e
        for conversation_id in self.conversation_ids:
            with api.db.connect() as connection:
                connection.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
                connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    def test_chat_list_recipes_paginate_by_default_but_honor_single_page_request(self):
        recipe = api.ScrapeRecipeInput(name="Directory", start_url="https://example.test/catalog",
            record_selector="auto", tool="list").to_record("recipe-pagination-default")
        with patch.object(api.browser_sessions, "list", return_value=[{
            "session_id": "pagination-session", "url": recipe["start"]["url"], "profile_id": None,
        }]), patch.object(api, "draft_auto_web_recipe", return_value={"recipe": recipe}) as draft, \
             patch.object(api.db, "save_web_recipe", side_effect=lambda value: value):
            api.draft_web_recipe_from_text(api.BrowserRecipeDraftFromTextInput(
                session_id="pagination-session", instruction="Scrape facility names and addresses"))
            self.assertTrue(draft.call_args.args[0].auto_paginate)
            self.assertEqual(100, draft.call_args.args[0].max_pages)

            api.draft_web_recipe_from_text(api.BrowserRecipeDraftFromTextInput(
                session_id="pagination-session", instruction="Scrape facility names on this page only"))
            self.assertFalse(draft.call_args.args[0].auto_paginate)

    def test_chat_scrape_keeps_current_search_and_page_query(self):
        with patch.object(api.browser_sessions, "list", return_value=[{
            "session_id": "query-session", "url": "https://example.test/catalog?q=shops&page=4#results",
            "profile_id": "profile-1",
        }]), patch.object(api, "draft_auto_web_recipe", side_effect=api.HTTPException(422, "no repeating rows")), \
             patch.object(api.browser_sessions, "picker_evidence", return_value={"status": "AVAILABLE", "candidates": [
                 {"kind": "row", "tag": "article", "text": "Shop", "dom_path": "main > article",
                  "selector_candidates": ["article"], "relative_selector": ""},
                 {"kind": "field", "tag": "a", "text": "Shop", "dom_path": "article > a",
                  "selector_candidates": ["a"], "relative_selector": "a", "attributes": {"href": "/shop"}},
             ]}), patch.object(api.runtime.model, "generate", return_value={"text": json.dumps({
                 "name": "Directory", "record_candidate_index": 0,
                 "fields": [{"name": "name", "candidate_index": 1, "extract": "text"}],
                 "max_pages": 10, "max_records": 1000,
             })}), patch.object(api.db, "save_web_recipe", side_effect=lambda value: value) as save_recipe:
            api.draft_web_recipe_from_text(api.BrowserRecipeDraftFromTextInput(
                session_id="query-session", instruction="Scrape the list"))
        saved_recipe = save_recipe.call_args.args[0]
        self.assertEqual("https://example.test/catalog?q=shops&page=4", saved_recipe["start"]["url"])

    def test_recipe_preview_reuses_already_open_page_instead_of_navigating_again(self):
        recipe = api.ScrapeRecipeInput(
            name="Current page", start_url="https://example.test/catalog?q=shops",
            record_selector="body", tool="page",
        ).to_record("recipe-current-page")
        evidence = {"status": "AVAILABLE", "rows": [{"title": "Catalog"}], "record_count": 1,
                    "field_match_counts": {}}
        with patch.object(api.db, "web_recipe", return_value=recipe), \
             patch.object(api.browser_sessions, "list", return_value=[{
                 "session_id": "already-open", "url": recipe["start"]["url"],
                 "profile_id": None,
             }]), \
             patch.object(api.browser_sessions, "snapshot", return_value={
                 "status": "AVAILABLE", "url": recipe["start"]["url"],
                 "clean_markdown": "Catalog is already loaded",
             }) as snapshot, \
             patch.object(api.browser_sessions, "navigate", side_effect=AssertionError(
                 "the current URL should not be requested a second time")), \
             patch.object(api.browser_sessions, "extract_recipe", return_value=evidence), \
             patch.object(api.db, "set_web_recipe_status", return_value=recipe):
            result_recipe, result = api._preview_web_recipe(
                "recipe-current-page", api.BrowserRecipePreviewInput(
                    session_id="already-open", limit=5), 5)
        self.assertEqual("recipe-current-page", result_recipe["id"])
        self.assertEqual(evidence, result)
        snapshot.assert_called_once_with("already-open")

    def test_managed_browser_screenshot_requires_gui_auth_and_valid_session_id(self):
        class Sessions:
            def screenshot(self, session_id):
                self.session_id = session_id
                return {"status": "AVAILABLE", "mime_type": "image/png", "data": "cG5n",
                        "url": "http://127.0.0.1:5173/"}

        sessions = Sessions()
        api.browser_sessions = sessions
        session_id = "d36df8b7-09d9-47aa-97d5-2af7da661934"
        with patch("olcr_api.app._gui_session_token", "chat-surface-token"):
            self.assertEqual(401, self.client.get(f"/api/web/browser/sessions/{session_id}/screenshot").status_code)
            result = self.client.get(f"/api/web/browser/sessions/{session_id}/screenshot",
                                     headers={"X-OLCR-Session": "chat-surface-token"})
            invalid = self.client.get("/api/web/browser/sessions/not-a-session/screenshot",
                                      headers={"X-OLCR-Session": "chat-surface-token"})
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(session_id, sessions.session_id)
        self.assertEqual("image/png", result.json()["mime_type"])
        self.assertEqual(422, invalid.status_code)

    def test_explicit_chat_request_validates_and_starts_existing_recipe_run(self):
        calls = []

        class Sessions:
            def open(self):
                calls.append(("open",))
                return {"status": "READY", "session_id": "chat-session-1"}

            def navigate(self, session_id, url):
                calls.append(("navigate", session_id, url))
                return {"status": "AVAILABLE", "url": url, "title": "Catalog"}

            def close(self, session_id):
                calls.append(("close", session_id))
                return True

        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})
        api.browser_sessions = Sessions()
        recipe = {"id": "recipe-chat-1", "name": "Catalog records"}
        validation = {"status": "VALIDATED", "rows": [{"name": "Example"}], "record_count": 1,
                      "field_match_counts": {"name": 1}}
        run = {"id": "run-chat-1", "status": "RUNNING", "pages_completed": 0, "record_count": 0}
        with patch("olcr_api.app._gui_session_token", ""), \
             patch("olcr_api.app.draft_web_recipe_from_text", return_value=recipe), \
             patch("olcr_api.app._validate_web_recipe", return_value=validation) as validate, \
             patch.object(api.scrape_runs, "create", return_value=run) as create_run:
            result = self.client.post("/api/chat", json={
                "message": "Scrape this URL: https://example.test/catalog, collect product names and prices",
                "message_id": "chat-scrape-message-1",
            })
        self.assertEqual(200, result.status_code, result.text)
        payload = result.json()
        self.conversation_ids.append(payload["conversation_id"])
        self.assertEqual("open", calls[0][0])
        self.assertEqual("navigate", calls[1][0])
        block = payload["blocks"][0]
        self.assertEqual("scrape_task", block["type"])
        self.assertEqual("chat-session-1", block["session_id"])
        self.assertEqual("https://example.test/catalog", block["url"])
        self.assertIn("product names and prices", block["instruction"])
        self.assertEqual("recipe-chat-1", block["recipe_id"])
        self.assertEqual("run-chat-1", block["run_id"])
        self.assertEqual("RUNNING", block["status"])
        validate.assert_called_once_with("recipe-chat-1", api.BrowserRecipePreviewInput(session_id="chat-session-1", limit=5))
        create_run.assert_called_once_with("recipe-chat-1", session_id="chat-session-1")
        stored = api.db.conversation(payload["conversation_id"])
        assistant = next(message for message in stored["messages"] if message["role"] == "assistant")
        self.assertEqual(block, assistant["blocks"][0])

        # Retrying the same source message returns its task instead of opening another session/run.
        with patch("olcr_api.app._gui_session_token", ""):
            retry = self.client.post("/api/chat", json={
                "message": "Scrape this URL: https://example.test/catalog, collect product names and prices",
                "message_id": "chat-scrape-message-1", "conversation_id": payload["conversation_id"],
            })
        self.assertEqual(200, retry.status_code, retry.text)
        self.assertEqual(block, retry.json()["blocks"][0])
        self.assertEqual(1, len([call for call in calls if call[0] == "open"]))
        create_run.assert_called_once()

    def test_chat_pause_control_routes_to_existing_scrape_run(self):
        conversation_id = api.db.create_conversation("scrape controls", 1.0,
            "chat-scrape-control-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        api.db.add_message(conversation_id, "assistant", "started", 2.0, "chat-scrape-control-start",
            blocks=[{"type": "scrape_task", "session_id": "session-control", "url": "https://example.test",
                     "instruction": "collect titles", "run_id": "run-control", "status": "RUNNING"}])
        run = {"id": "run-control", "status": "PAUSE_REQUESTED", "pages_completed": 2, "record_count": 15}
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.scrape_runs, "get", return_value=run), \
             patch.object(api.scrape_runs, "pause", return_value=run) as pause:
            result = self.client.post("/api/chat", json={
                "message": "Pause scraping", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-scrape-control-pause",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertIn("scrape_run_id", result.json(), result.text)
        self.assertEqual("run-control", result.json()["scrape_run_id"])
        self.assertEqual("PAUSE_REQUESTED", result.json()["scrape_status"])
        pause.assert_called_once_with("run-control")

    def test_chat_scrape_control_lists_multiple_runs_and_selects_numbered_run(self):
        conversation_id = api.db.create_conversation("numbered scrape controls", 1.0,
            "chat-scrape-numbered-control-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        api.db.add_message(conversation_id, "assistant", "started", 2.0, "chat-scrape-numbered-control-start",
            blocks=[
                {"type": "scrape_task", "url": "https://alpha.example.test/catalog", "run_id": "run-alpha", "status": "RUNNING"},
                {"type": "scrape_task", "url": "https://beta.example.test/catalog", "run_id": "run-beta", "status": "RUNNING"},
            ])
        runs = {
            "run-alpha": {"id": "run-alpha", "status": "RUNNING", "pages_completed": 1, "record_count": 4},
            "run-beta": {"id": "run-beta", "status": "RUNNING", "pages_completed": 2, "record_count": 8},
        }
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.scrape_runs, "get", side_effect=lambda run_id: runs[run_id]), \
             patch.object(api.scrape_runs, "pause", return_value={**runs["run-beta"], "status": "PAUSE_REQUESTED"}) as pause:
            ambiguous = self.client.post("/api/chat", json={
                "message": "Pause scraping", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-scrape-numbered-ambiguous",
            })
            selected = self.client.post("/api/chat", json={
                "message": "Pause scraping #2", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-scrape-numbered-select-two",
            })
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.scrape_runs, "get", side_effect=lambda run_id: runs[run_id]), \
             patch.object(api.db, "stream_web_scrape_rows", return_value=iter([{"title": "Beta result"}])) as stream_rows:
            results = self.client.post("/api/chat", json={
                "message": "show results #2", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-scrape-numbered-results-two",
            })
        self.assertEqual(200, ambiguous.status_code, ambiguous.text)
        self.assertIn("#1 alpha.example.test（RUNNING）", ambiguous.json()["response"])
        self.assertIn("#2 beta.example.test（RUNNING）", ambiguous.json()["response"])
        self.assertNotIn("scrape_run_id", ambiguous.json())
        self.assertEqual(200, selected.status_code, selected.text)
        self.assertEqual("run-beta", selected.json()["scrape_run_id"])
        self.assertEqual("PAUSE_REQUESTED", selected.json()["scrape_status"])
        pause.assert_called_once_with("run-beta")
        self.assertEqual(200, results.status_code, results.text)
        self.assertEqual("run-beta", results.json()["scrape_run_id"])
        stream_rows.assert_called_once_with("run-beta", batch_size=20)

    def test_chat_clarification_reuses_pending_browser_and_starts_validated_recipe(self):
        conversation_id = api.db.create_conversation("chat scrape clarification", 1.0,
            "chat-scrape-clarification-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        calls = []

        class Sessions:
            def open(self):
                calls.append(("open",))
                return {"status": "READY", "session_id": "session-pending"}

            def navigate(self, session_id, url):
                calls.append(("navigate", session_id, url))
                return {"status": "AVAILABLE", "url": url, "title": "Catalog"}

            def snapshot(self, session_id):
                calls.append(("snapshot", session_id))
                return {"status": "AVAILABLE", "url": "https://example.test/catalog", "title": "Catalog"}

        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})
        api.browser_sessions = Sessions()
        recipe = {"id": "recipe-clarified", "name": "Catalog fields"}
        run = {"id": "run-clarified", "status": "RUNNING"}
        with patch("olcr_api.app._gui_session_token", ""), \
             patch("olcr_api.app.draft_web_recipe_from_text", side_effect=[
                 api.HTTPException(422, "RECIPE_DRAFT_NOT_GROUNDED_IN_VISIBLE_EVIDENCE"), recipe]) as draft, \
             patch("olcr_api.app._validate_web_recipe", return_value={"status": "VALIDATED"}) as validate, \
             patch.object(api.scrape_runs, "create", return_value=run) as create_run:
            first = self.client.post("/api/chat", json={
                "message": "Scrape this URL: https://example.test/catalog, collect the main records",
                "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(),
                "message_id": "chat-scrape-initial-clarification",
            })
            self.assertEqual(200, first.status_code, first.text)
            pending = first.json()["blocks"][0]
            self.assertTrue(pending["needs_clarification"])
            self.assertEqual("NEEDS_CLARIFICATION", pending["status"])
            self.assertIn("チャットで具体的", first.json()["response"])
            result = self.client.post("/api/chat", json={
                "message": "Scrape https://example.test/catalog and collect product name and price",
                "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(),
                "message_id": "chat-scrape-clarification-answer",
            })
        self.assertEqual(200, result.status_code, result.text)
        block = result.json()["blocks"][0]
        self.assertEqual("session-pending", block["session_id"])
        self.assertEqual("run-clarified", block["run_id"])
        self.assertFalse(block["needs_clarification"])
        self.assertIn("product name and price", block["instruction"])
        self.assertEqual([("open",), ("navigate", "session-pending", "https://example.test/catalog"),
                          ("snapshot", "session-pending")], calls)
        self.assertEqual(2, draft.call_count)
        draft_instruction = draft.call_args_list[1].args[0].instruction
        self.assertIn("product name and price", draft_instruction)
        self.assertIn("takes precedence", draft_instruction)
        validate.assert_called_once_with("recipe-clarified", api.BrowserRecipePreviewInput(
            session_id="session-pending", limit=5))
        create_run.assert_called_once_with("recipe-clarified", session_id="session-pending")

    def test_chat_can_stop_a_pending_scrape_before_a_run_exists(self):
        conversation_id = api.db.create_conversation("pending scrape stop", 1.0,
            "chat-scrape-pending-stop-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        pending = {"type": "scrape_task", "session_id": "session-to-close",
                   "url": "https://example.test/catalog", "instruction": "Collect records",
                   "status": "NEEDS_CLARIFICATION", "needs_clarification": True}
        api.db.add_message(conversation_id, "assistant", "Which fields?", 2.0,
                           "chat-scrape-pending-stop", blocks=[pending])

        class Sessions:
            def close(self, session_id):
                self.session_id = session_id
                return True

        sessions = Sessions()
        api.browser_sessions = sessions
        with patch("olcr_api.app._gui_session_token", ""):
            result = self.client.post("/api/chat", json={
                "message": "Stop scraping", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-scrape-pending-stop-request",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual("session-to-close", sessions.session_id)
        self.assertEqual("STOPPED", result.json()["blocks"][0]["status"])
        self.assertFalse(result.json()["blocks"][0]["needs_clarification"])

    def test_chat_can_reveal_more_public_page_evidence_before_recipe_validation(self):
        conversation_id = api.db.create_conversation("pending scrape page action", 1.0,
            "chat-scrape-page-action-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        pending = {"type": "scrape_task", "session_id": "session-page-action",
                   "url": "https://example.test/catalog", "instruction": "Collect product names",
                   "status": "NEEDS_CLARIFICATION", "needs_clarification": True}
        api.db.add_message(conversation_id, "assistant", "Which fields?", 2.0,
                           "chat-scrape-page-action-start", blocks=[pending])

        class Sessions:
            def click_scrape_readonly_control(self, session_id, label):
                self.call = (session_id, label)
                return {"status": "AVAILABLE", "url": "https://example.test/catalog?page=2",
                        "title": "Catalog page 2", "clean_markdown": "- heading: More products"}

        sessions = Sessions()
        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})
        api.browser_sessions = sessions
        with patch("olcr_api.app._gui_session_token", ""):
            result = self.client.post("/api/chat", json={
                "message": 'click the button "Load more"',
                "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(),
                "message_id": "chat-scrape-page-action-click",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(("session-page-action", "Load more"), sessions.call)
        block = result.json()["blocks"][0]
        self.assertEqual("https://example.test/catalog?page=2", block["url"])
        self.assertEqual("- heading: More products", block["snapshot"])
        self.assertTrue(block["needs_clarification"])
        self.assertNotIn("run_id", block)

    def test_chat_can_infer_one_safe_public_next_page_link_from_the_goal(self):
        import json

        conversation_id = api.db.create_conversation("goal-directed scrape navigation", 1.0,
            "chat-scrape-goal-navigation-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        pending = {"type": "scrape_task", "session_id": "session-goal-navigation",
                   "url": "https://example.test/catalog", "instruction": "Collect product names",
                   "status": "NEEDS_CLARIFICATION", "needs_clarification": True}
        api.db.add_message(conversation_id, "assistant", "Which fields?", 2.0,
                           "chat-scrape-goal-navigation-start", blocks=[pending])

        class Sessions:
            def scrape_readonly_control_candidates(self, session_id):
                self.evidence_call = session_id
                return {"status": "AVAILABLE", "url": "https://example.test/catalog",
                        "truncated": False, "candidates": [
                            {"label": "Next", "kind": "navigation", "tag": "a"},
                            {"label": "Load more", "kind": "control", "tag": "button"},
                        ]}

            def click_scrape_readonly_control(self, session_id, label):
                self.click_call = (session_id, label)
                return {"status": "AVAILABLE", "url": "https://example.test/catalog?page=2",
                        "title": "Catalog page 2", "clean_markdown": "- heading: Page two"}

        sessions = Sessions()
        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})
        api.browser_sessions = sessions
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.runtime.model, "generate", return_value={
                 "text": json.dumps({"decision": "execute", "target_label": "Next"})}) as generate:
            result = self.client.post("/api/chat", json={
                "message": "go to the next page",
                "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(),
                "message_id": "chat-scrape-goal-navigation-request",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual("session-goal-navigation", sessions.evidence_call)
        self.assertEqual(("session-goal-navigation", "Next"), sessions.click_call)
        generate.assert_called_once()
        self.assertEqual(api.settings.main_model, generate.call_args.args[1])
        block = result.json()["blocks"][0]
        self.assertEqual("https://example.test/catalog?page=2", block["url"])
        self.assertEqual("- heading: Page two", block["snapshot"])
        self.assertTrue(block["needs_clarification"])
        self.assertNotIn("run_id", block)

    def test_chat_does_not_dispatch_ambiguous_model_selected_scrape_link(self):
        import json

        conversation_id = api.db.create_conversation("ambiguous goal-directed scrape navigation", 1.0,
            "chat-scrape-goal-navigation-ambiguous-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        api.db.add_message(conversation_id, "assistant", "Which fields?", 2.0,
            "chat-scrape-goal-navigation-ambiguous-start", blocks=[{
                "type": "scrape_task", "session_id": "session-goal-navigation-ambiguous",
                "url": "https://example.test/catalog", "instruction": "Collect products",
                "status": "NEEDS_CLARIFICATION", "needs_clarification": True,
            }])

        class Sessions:
            def scrape_readonly_control_candidates(self, _session_id):
                return {"status": "AVAILABLE", "url": "https://example.test/catalog",
                        "truncated": False, "candidates": [
                            {"label": "Next", "kind": "navigation", "tag": "a"},
                            {"label": "Next", "kind": "navigation", "tag": "a"},
                        ]}
            def click_scrape_readonly_control(self, *_args):
                raise AssertionError("duplicate labels must be clarified before click dispatch")

        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})
        api.browser_sessions = Sessions()
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.runtime.model, "generate", return_value={
                 "text": json.dumps({"decision": "execute", "target_label": "Next"})}):
            result = self.client.post("/api/chat", json={
                "message": "go to the next page",
                "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(),
                "message_id": "chat-scrape-goal-navigation-ambiguous-request",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertIn("一意に", result.json()["response"])
        self.assertEqual("scrape_task", result.json()["blocks"][0]["type"])

    def test_chat_does_not_guess_between_pending_scrape_sessions(self):
        conversation_id = api.db.create_conversation("ambiguous pending scrapes", 1.0,
            "chat-scrape-page-action-ambiguous-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        for index, session_id in enumerate(("session-one", "session-two")):
            api.db.add_message(conversation_id, "assistant", "Which fields?", 2.0 + index,
                f"chat-scrape-page-action-pending-{index}", blocks=[{
                    "type": "scrape_task", "session_id": session_id,
                    "url": "https://example.test/catalog", "instruction": "Collect products",
                    "status": "NEEDS_CLARIFICATION", "needs_clarification": True,
                }])

        class Sessions:
            def click_scrape_readonly_control(self, *_args):
                raise AssertionError("ambiguous task target must not dispatch page interaction")

        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})
        api.browser_sessions = Sessions()
        with patch("olcr_api.app._gui_session_token", ""):
            result = self.client.post("/api/chat", json={
                "message": 'click the button "Load more"',
                "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(),
                "message_id": "chat-scrape-page-action-ambiguous-click",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertIn("複数のスクレイプタスク", result.json()["response"])

    def test_chat_clarification_respects_scraping_disabled_after_task_creation(self):
        conversation_id = api.db.create_conversation("disabled pending scrape", 1.0,
            "chat-scrape-disabled-pending-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        pending = {"type": "scrape_task", "session_id": "session-disabled",
                   "url": "https://example.test/catalog", "instruction": "Collect records",
                   "status": "NEEDS_CLARIFICATION", "needs_clarification": True}
        api.db.add_message(conversation_id, "assistant", "Which fields?", 2.0,
                           "chat-scrape-disabled-pending", blocks=[pending])

        class Sessions:
            def snapshot(self, _session_id):
                raise AssertionError("disabled capability must not inspect the browser")

            def open(self):
                raise AssertionError("disabled capability must not open a browser")

        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": False})
        api.browser_sessions = Sessions()
        with patch("olcr_api.app._gui_session_token", ""), \
             patch("olcr_api.app.draft_web_recipe_from_text") as draft:
            result = self.client.post("/api/chat", json={
                "message": "Collect only product names",
                "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(),
                "message_id": "chat-scrape-disabled-pending-reply",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertIn("OFF", result.json()["response"])
        self.assertNotIn("blocks", result.json())
        draft.assert_not_called()

    def test_chat_results_returns_bounded_table_from_existing_scrape_run(self):
        conversation_id = api.db.create_conversation("scrape result controls", 1.0,
            "chat-scrape-results-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        api.db.add_message(conversation_id, "assistant", "started", 2.0, "chat-scrape-results-start",
            blocks=[{"type": "scrape_task", "session_id": "session-results", "url": "https://example.test/catalog?token=hidden",
                     "instruction": "collect names", "run_id": "run-results", "status": "COMPLETED"}])
        run = {"id": "run-results", "status": "COMPLETED", "pages_completed": 1, "record_count": 23}
        rows = [{"name": f"Item {index}", "rank": index} for index in range(23)]
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.scrape_runs, "get", return_value=run), \
             patch.object(api.db, "stream_web_scrape_rows", return_value=iter(rows)):
            result = self.client.post("/api/chat", json={
                "message": "show results", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-scrape-results-query",
            })
        self.assertEqual(200, result.status_code, result.text)
        payload = result.json()
        self.assertEqual("run-results", payload["scrape_run_id"])
        table = payload["blocks"][0]
        self.assertEqual("table", table["type"])
        self.assertEqual(20, len(table["rows"]))
        self.assertEqual("Item 0", table["rows"][0]["name"])
        self.assertEqual("example.test", table["source_label"])
        self.assertNotIn("token", table["source_label"])
        self.assertIn("23 件", payload["response"])

    def test_streaming_chat_results_uses_scrape_control_route(self):
        conversation_id = api.db.create_conversation("stream scrape result controls", 1.0,
            "chat-scrape-stream-results-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        api.db.add_message(conversation_id, "assistant", "started", 2.0, "chat-scrape-stream-results-start",
            blocks=[{"type": "scrape_task", "session_id": "session-stream-results",
                     "url": "https://example.test/catalog", "instruction": "collect names",
                     "run_id": "run-stream-results", "status": "COMPLETED"}])
        run = {"id": "run-stream-results", "status": "COMPLETED", "pages_completed": 1, "record_count": 1}
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.scrape_runs, "get", return_value=run), \
             patch.object(api.db, "stream_web_scrape_rows", return_value=iter([{"name": "Item 0"}])):
            result = self.client.post("/api/chat/stream", json={
                "message": "show results", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-scrape-stream-results-query",
            })
        self.assertEqual(200, result.status_code, result.text)
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
        self.assertEqual("SCRAPE_CONTROL", events[-1]["response_owner"])
        table = next(event["block"] for event in events if event["type"] == "block")
        self.assertEqual("table", table["type"])
        self.assertEqual("Item 0", table["rows"][0]["name"])

    def test_chat_export_returns_authenticated_download_block_for_existing_scrape_run(self):
        conversation_id = api.db.create_conversation("scrape export controls", 1.0,
            "chat-scrape-export-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        api.db.add_message(conversation_id, "assistant", "started", 2.0, "chat-scrape-export-start",
            blocks=[{"type": "scrape_task", "session_id": "session-export", "url": "https://example.test/catalog",
                     "instruction": "collect names", "run_id": "run-export", "status": "COMPLETED"}])
        run = {"id": "run-export", "status": "COMPLETED", "pages_completed": 1, "record_count": 23}
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.scrape_runs, "get", return_value=run), \
             patch.object(api.db, "web_scrape_run", return_value={"id": "run-export",
                 "recipe": {"output_format": "jsonl"}}) as persisted_run:
            result = self.client.post("/api/chat", json={
                "message": "export scrape results", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-scrape-export-query",
            })
        self.assertEqual(200, result.status_code, result.text)
        payload = result.json()
        self.assertEqual("run-export", payload["scrape_run_id"])
        self.assertEqual("COMPLETED", payload["scrape_status"])
        self.assertEqual({"type": "scrape_export", "run_id": "run-export",
                          "output_format": "jsonl", "record_count": 23}, payload["blocks"][0])
        persisted_run.assert_called_once_with("run-export")

    def test_chat_scrape_uses_managed_playwright_fixture_through_run_completion(self):
        body = b"""<!doctype html><html><head><title>Fixture catalog</title></head><body>
        <table><tbody>
          <tr><td>Northwind</td><td><a href='/northwind'>Profile</a></td></tr>
          <tr><td>Contoso</td><td><a href='/contoso'>Profile</a></td></tr>
        </tbody></table></body></html>"""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        address = ("127.0.0.1", server.server_port)
        manager = api.BrowserSessionManager(api.managed_browser_command, maximum_sessions=2)
        api.browser_sessions = manager
        api.scrape_runs.browser_sessions = manager
        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})

        def resolve_fixture_only(url):
            from urllib.parse import urlsplit
            if urlsplit(url).hostname == "fixture.example":
                return (address,)
            raise ValueError("CHAT_SCRAPE_FIXTURE_EGRESS_ONLY")

        def propose_recipe(messages, *_args, **_kwargs):
            prompt = messages[-1]["content"]
            marker = "Visible candidate evidence (untrusted JSON): "
            candidates = json.loads(prompt.rsplit(marker, 1)[1])
            row = next(item for item in candidates if item["kind"] == "row")
            field = next(item for item in candidates if item["kind"] == "field")
            return {"text": json.dumps({
                "name": "Fixture catalog", "record_candidate_index": row["index"],
                "fields": [{"name": "name", "candidate_index": field["index"], "extract": "text"}],
                "max_pages": 1, "max_records": 10,
            })}

        session_id = None
        try:
            from olcr_api.node_mcp_runtime import managed_browser_command
            if not managed_browser_command():
                self.skipTest("OLCR bundled Playwright MCP runtime is unavailable")
            with patch("olcr_api.browser_network._resolve_public_addresses", side_effect=resolve_fixture_only), \
                 patch.object(api.runtime.model, "generate", side_effect=propose_recipe):
                result = self.client.post("/api/chat", json={
                    "message": f"Scrape this URL: http://fixture.example:{address[1]}/catalog, collect names",
                    "message_id": "chat-scrape-playwright-fixture",
                })
                self.assertEqual(200, result.status_code, result.text)
                self.conversation_ids.append(result.json()["conversation_id"])
                block = result.json()["blocks"][0]
                self.assertEqual("scrape_task", block["type"])
                self.assertIn(block["status"], {"QUEUED", "RUNNING"})
                self.assertTrue(block.get("recipe_id"))
                self.assertTrue(block.get("run_id"))
                session_id = block["session_id"]
                deadline = time.monotonic() + 20
                current = None
                while time.monotonic() < deadline:
                    current = api.scrape_runs.get(block["run_id"])
                    if current and current["status"] not in {"QUEUED", "RUNNING"}:
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(current)
                self.assertEqual("COMPLETED", current["status"], current)
                self.assertEqual(2, current["record_count"])
                rows = list(api.db.stream_web_scrape_rows(block["run_id"]))
                self.assertEqual(["Northwind", "Contoso"], [row["name"] for row in rows])
        finally:
            if session_id:
                manager.close(session_id)
            manager.close_all()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_chat_scrape_automatically_follows_next_page_and_deduplicates_rows(self):
        pages = {
            "/catalog": """<main><section>
              <article><a href='/spot/a'>Alpha Mall</a><p>東京都港区1-1</p></article>
              <article><a href='/spot/b'>Beta Mall</a><p>東京都港区2-2</p></article>
              <article><a href='/spot/d'>Delta Mall</a><p>東京都港区3-3</p></article>
            </section><nav><a rel='next' href='/catalog?page=2'>Next page</a></nav></main>""",
            "/catalog?page=2": """<main><section>
              <article><a href='/spot/b'>Beta Mall</a><p>東京都港区2-2</p></article>
              <article><a href='/spot/c'>Gamma Mall</a><p>東京都港区4-4</p></article>
            </section></main>""",
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = ("<!doctype html><title>Fixture directory</title>" +
                        pages.get(self.path, "<main>End of results</main>")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args): pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        address = ("127.0.0.1", server.server_port)
        manager = api.BrowserSessionManager(api.managed_browser_command, maximum_sessions=2)
        api.browser_sessions = manager
        api.scrape_runs.browser_sessions = manager
        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})

        def resolve_fixture_only(url):
            from urllib.parse import urlsplit
            if urlsplit(url).hostname == "fixture.example": return (address,)
            raise ValueError("CHAT_SCRAPE_FIXTURE_EGRESS_ONLY")

        try:
            from olcr_api.node_mcp_runtime import managed_browser_command
            if not managed_browser_command(): self.skipTest("OLCR bundled Playwright MCP runtime is unavailable")
            with patch("olcr_api.browser_network._resolve_public_addresses", side_effect=resolve_fixture_only), \
                 patch.object(api.runtime.model, "generate", side_effect=AssertionError("stable list extraction should not require a model")):
                result = self.client.post("/api/chat", json={
                    "message": f"Scrape the facility names and addresses from http://fixture.example:{address[1]}/catalog",
                    "message_id": "chat-scrape-auto-pagination",
                })
                self.assertEqual(200, result.status_code, result.text)
                self.conversation_ids.append(result.json()["conversation_id"])
                block = result.json()["blocks"][0]
                deadline = time.monotonic() + 30
                current = None
                while time.monotonic() < deadline:
                    current = api.scrape_runs.get(block["run_id"])
                    if current and current["status"] not in {"QUEUED", "RUNNING"}: break
                    time.sleep(0.1)
                self.assertIsNotNone(current)
                self.assertEqual("COMPLETED", current["status"], current)
                self.assertEqual("NO_NEXT_LINK", current["stop_reason"])
                self.assertEqual((2, 4), (current["pages_completed"], current["record_count"]))
                rows = list(api.db.stream_web_scrape_rows(block["run_id"]))
                self.assertEqual(["Alpha Mall", "Beta Mall", "Delta Mall", "Gamma Mall"],
                                 [row["facility_name"] for row in rows])
                self.assertEqual(1, current["metrics"]["duplicates_removed"])
                self.assertEqual(2, current["metrics"]["pages_visited"])
        finally:
            manager.close_all()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_natural_multi_url_email_scrape_without_model_and_table_exports(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                email = "one@example.com" if self.path == "/one" else "two@example.com"
                body = f"<html><title>Contacts</title><body><main>{email}</main></body></html>".encode()
                self.send_response(200); self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *_args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        address = ("127.0.0.1", server.server_port)
        manager = api.BrowserSessionManager(api.managed_browser_command, maximum_sessions=2)
        api.browser_sessions = manager; api.scrape_runs.browser_sessions = manager
        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})
        def resolve_fixture(url):
            from urllib.parse import urlsplit
            if urlsplit(url).hostname == "fixture.example": return (address,)
            raise ValueError("FIXTURE_ONLY")
        try:
            if not api.managed_browser_command(): self.skipTest("prepared browser runtime unavailable")
            with patch("olcr_api.browser_network._resolve_public_addresses", side_effect=resolve_fixture), \
                 patch.object(api.runtime.model, "generate", side_effect=AssertionError("automatic extraction must not call a model")) as model:
                base = f"http://fixture.example:{address[1]}"
                result = self.client.post("/api/chat", json={"message": f"Extract emails from {base}/one and {base}/two", "message_id": "auto-email-multi-url"})
                self.assertEqual(200, result.status_code, result.text)
                self.conversation_ids.append(result.json()["conversation_id"])
                block = result.json()["blocks"][0]; run_id = block["run_id"]
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    run = api.scrape_runs.get(run_id)
                    if run["status"] not in {"QUEUED", "RUNNING"}: break
                    time.sleep(.1)
                self.assertEqual("COMPLETED", run["status"], run)
                self.assertEqual(2, run["record_count"])
                self.assertEqual({"one@example.com", "two@example.com"}, {r["email"] for r in api.db.stream_web_scrape_rows(run_id)})
                model.assert_not_called()
                table = self.client.get(f"/api/web/scrape/runs/{run_id}/table?search=two").json()
                self.assertEqual(1, table["total"])
                renamed = self.client.post(f"/api/web/scrape/runs/{run_id}/table", json={"operation": "rename", "column": "email", "target": "address"})
                self.assertEqual(200, renamed.status_code, renamed.text)
                csv_export = self.client.get(f"/api/web/scrape/runs/{run_id}/export?format=csv")
                self.assertIn("page_url,address", csv_export.text)
                json_export = self.client.get(f"/api/web/scrape/runs/{run_id}/export?format=json")
                self.assertEqual(2, len(json_export.json()))
                excel = self.client.get(f"/api/web/scrape/runs/{run_id}/export?format=xlsx")
                self.assertTrue(excel.content.startswith(b"PK"))
                exported = self.client.get(f"/api/web/recipes/{block['recipe_id']}/download")
                imported = self.client.post("/api/web/recipes/import", json=exported.json())
                self.assertEqual(200, imported.status_code, imported.text)
                self.assertEqual("DRAFT", imported.json()["status"])
                self.assertEqual("emails", imported.json()["tool"])
        finally:
            manager.close_all(); server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_disabled_chat_scraping_does_not_start_browser(self):
        class Sessions:
            def open(self):
                raise AssertionError("browser must stay closed when the capability is off")

        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": False})
        api.browser_sessions = Sessions()
        with patch("olcr_api.app._gui_session_token", ""):
            result = self.client.post("/api/chat", json={
                "message": "Scrape this URL: https://example.test/catalog",
                "message_id": "chat-scrape-disabled-1",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.conversation_ids.append(result.json()["conversation_id"])
        self.assertIn("OFF", result.json()["response"])
        self.assertNotIn("blocks", result.json())

    def test_local_e2e_chat_launch_uses_selected_project_and_is_idempotent(self):
        calls = []
        workspace = Path(_profile.name) / "sample-game"
        workspace.mkdir(exist_ok=True)

        class LocalRuns:
            def start(self, selected_workspace, allowed_roots, *, expected_origin=None,
                      requested_script=None, requested_command=None):
                calls.append(("start", selected_workspace, expected_origin, requested_script,
                              requested_command))
                return {"id": "local-run-1", "origin": "http://127.0.0.1:5173",
                        "owned_process": True, "status": "READY"}

            def get(self, _run_id):
                return {"id": "local-run-1", "status": "READY", "owned": True}

        class Sessions:
            def open_local(self, origin):
                calls.append(("open_local", origin))
                return {"status": "READY", "session_id": "local-session-1"}

            def navigate(self, session_id, url):
                calls.append(("navigate", session_id, url))
                return {"status": "AVAILABLE", "url": url, "title": "Game",
                        "clean_markdown": "button Start game"}

            def close(self, session_id):
                calls.append(("close", session_id))
                return True

        api.settings = self.original_settings.with_overrides({"local_e2e_enabled": True})
        api.local_e2e = LocalRuns()
        api.browser_sessions = Sessions()
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"}):
            result = self.client.post("/api/chat", json={
                "message": ("Start local E2E with command `python3 server.py --host 127.0.0.1 --port 5173` "
                            "at http://127.0.0.1:5173"),
                "message_id": "chat-local-e2e-start-1",
            })
        self.assertEqual(200, result.status_code, result.text)
        payload = result.json()
        self.conversation_ids.append(payload["conversation_id"])
        self.assertEqual("start", calls[0][0])
        self.assertEqual(str(workspace), calls[0][1])
        self.assertEqual("http://127.0.0.1:5173", calls[0][2])
        self.assertEqual("python3 server.py --host 127.0.0.1 --port 5173", calls[0][4])
        self.assertEqual(("open_local", "http://127.0.0.1:5173"), calls[1])
        block = payload["blocks"][0]
        self.assertEqual("local_e2e_task", block["type"])
        self.assertEqual("READY", block["status"])
        self.assertEqual("button Start game", block["snapshot"])

        with patch("olcr_api.app._gui_session_token", ""):
            retry = self.client.post("/api/chat", json={
                "message": ("Start local E2E with command `python3 server.py --host 127.0.0.1 --port 5173` "
                            "at http://127.0.0.1:5173"),
                "message_id": "chat-local-e2e-start-1", "conversation_id": payload["conversation_id"],
            })
        self.assertEqual(200, retry.status_code, retry.text)
        self.assertEqual(block, retry.json()["blocks"][0])
        self.assertEqual(1, len([item for item in calls if item[0] == "start"]))

    def test_chat_persists_native_computer_use_session_event_and_enforces_default_off(self):
        with patch("olcr_api.app._gui_session_token", ""):
            blocked = self.client.post("/api/chat", json={
                "message": "start computer use", "message_id": "chat-cu-off",
                "computer_use_event": {"action": "start", "status": "BLOCKED", "reason": "COMPUTER_USE_DISABLED"},
            })
        self.assertEqual(200, blocked.status_code, blocked.text)
        self.conversation_ids.append(blocked.json()["conversation_id"])
        self.assertIn("OFF", blocked.json()["response"])
        self.assertEqual("COMPUTER_USE_DISABLED", blocked.json()["blocks"][0]["reason"])

        api.settings = self.original_settings.with_overrides({"computer_use_enabled": True,
            "computer_use_allowed_bundle_ids": ["com.example.game"]})
        with patch("olcr_api.app._gui_session_token", ""):
            started = self.client.post("/api/chat", json={
                "message": "computer use ONの状態で実E2Eを開始して", "message_id": "chat-cu-start",
                "computer_use_event": {"action": "start", "status": "RUNNING",
                    "capture_session_id": "capture-chat-1", "application_pid": 77,
                    "application_bundle_id": "com.example.game"},
            })
        self.assertEqual(200, started.status_code, started.text)
        self.conversation_ids.append(started.json()["conversation_id"])
        block = started.json()["blocks"][0]
        self.assertEqual("computer_use_task", block["type"])
        self.assertEqual("RUNNING", block["status"])
        self.assertIn("画面キャプチャを開始しました", started.json()["response"])
        self.assertIn("操作を指示してください", started.json()["response"])
        stored = api.db.conversation(started.json()["conversation_id"])
        assistant = next(message for message in stored["messages"] if message["role"] == "assistant")
        self.assertEqual(block, assistant["blocks"][0])

        followup = self.client.post("/api/chat", json={
            "message": "Start GUI E2E with Computer Use for example game, then start it",
            "message_id": "chat-cu-start-with-action",
            "computer_use_event": {"action": "start", "status": "RUNNING",
                "capture_session_id": "capture-chat-followup", "application_pid": 79,
                "application_bundle_id": "com.example.game", "followup_action": True},
        })
        self.assertEqual(200, followup.status_code, followup.text)
        self.conversation_ids.append(followup.json()["conversation_id"])
        self.assertIn("このチャットで依頼された操作を続けます", followup.json()["response"])
        self.assertNotIn("操作を指示してください", followup.json()["response"])
        self.assertTrue(followup.json()["blocks"][0]["followup_action"])

        with patch("olcr_api.app._gui_session_token", ""):
            out_of_scope = self.client.post("/api/chat", json={
                "message": "start computer use on Calculator", "message_id": f"{started.json()['conversation_id']}:out-of-scope",
                "conversation_id": started.json()["conversation_id"],
                "computer_use_event": {"action": "start", "status": "RUNNING",
                    "capture_session_id": "capture-chat-2", "application_pid": 78,
                    "application_bundle_id": "com.apple.Calculator"},
            })
        self.assertEqual(200, out_of_scope.status_code, out_of_scope.text)
        # Explicit chat targeting replaces the retired per-app allowlist gate.
        self.assertEqual("com.apple.Calculator", out_of_scope.json()["blocks"][0]["application_bundle_id"])

        with patch("olcr_api.app._gui_session_token", ""):
            retry = self.client.post("/api/chat", json={
                "message": "start computer use", "message_id": "chat-cu-start",
                "conversation_id": started.json()["conversation_id"],
                "computer_use_event": {"action": "start", "status": "RUNNING",
                    "capture_session_id": "capture-chat-1", "application_pid": 77,
                    "application_bundle_id": "com.example.game"},
            })
        self.assertEqual(200, retry.status_code, retry.text)
        self.assertEqual(block, retry.json()["blocks"][0])

    def test_local_e2e_chat_control_clicks_only_one_named_visible_target(self):
        workspace = Path(_profile.name) / "sample-game-control"
        workspace.mkdir(exist_ok=True)
        conversation_id = api.db.create_conversation("local E2E controls", 1.0,
            "chat-local-e2e-control-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        api.db.add_message(conversation_id, "assistant", "started", 2.0, "chat-local-e2e-control-start",
            blocks=[{"type": "local_e2e_task", "session_id": "local-session-control",
                     "run_id": "local-run-control", "origin": "http://127.0.0.1:5173",
                     "url": "http://127.0.0.1:5173/", "status": "READY", "owned_process": True}])

        class LocalRuns:
            def get(self, run_id):
                return {"id": run_id, "status": "READY", "owned": True}

            def stop(self, run_id):
                return {"id": run_id, "status": "STOPPED", "owned_process": True, "stopped": True}

        class Sessions:
            def click_local_control(self, session_id, label):
                self.clicked = (session_id, label)
                return {"status": "AVAILABLE", "url": "http://127.0.0.1:5173/", "page": "button Started"}

            def type_local_field(self, session_id, label, value):
                self.typed = (session_id, label, value)
                return {"status": "AVAILABLE", "url": "http://127.0.0.1:5173/"}

            def snapshot(self, _session_id):
                return {"status": "AVAILABLE", "clean_markdown": "button Started"}

            def close(self, session_id):
                self.closed = session_id
                return True

        runs = LocalRuns()
        sessions = Sessions()
        api.local_e2e = runs
        api.browser_sessions = sessions
        api.settings = self.original_settings.with_overrides({"local_e2e_enabled": True})
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"}):
            clicked = self.client.post("/api/chat", json={"message": "「Start game」ボタンをクリック",
                "conversation_id": conversation_id, "project_id": api.db.default_project_id(),
                "message_id": "chat-local-e2e-click"})
        self.assertEqual(200, clicked.status_code, clicked.text)
        self.assertEqual(("local-session-control", "Start game"), sessions.clicked)
        self.assertIn("Started", clicked.json()["blocks"][0]["snapshot"])

        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"}):
            typed = self.client.post("/api/chat", json={"message": "「Player name」に「Alice」を入力して",
                "conversation_id": conversation_id, "project_id": api.db.default_project_id(),
                "message_id": "chat-local-e2e-type"})
        self.assertEqual(200, typed.status_code, typed.text)
        self.assertEqual(("local-session-control", "Player name", "Alice"), sessions.typed)
        self.assertIn("Player name", typed.json()["response"])

        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"}):
            stopped = self.client.post("/api/chat", json={"message": "stop local e2e",
                "conversation_id": conversation_id, "project_id": api.db.default_project_id(),
                "message_id": "chat-local-e2e-stop"})
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual("local-session-control", sessions.closed)
        self.assertEqual("STOPPED", stopped.json()["blocks"][0]["status"])

    def test_local_e2e_chat_executes_bounded_action_sequence_and_stops_on_first_failure(self):
        workspace = Path(_profile.name) / "sample-game-sequence"
        workspace.mkdir(exist_ok=True)
        conversation_id = api.db.create_conversation("local E2E sequence", 1.0,
            "chat-local-e2e-sequence-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)
        api.db.add_message(conversation_id, "assistant", "started", 2.0, "chat-local-e2e-sequence-start",
            blocks=[{"type": "local_e2e_task", "session_id": "local-session-sequence",
                     "run_id": "local-run-sequence", "origin": "http://127.0.0.1:5173",
                     "url": "http://127.0.0.1:5173/", "status": "READY", "owned_process": True}])

        class LocalRuns:
            def get(self, run_id):
                return {"id": run_id, "status": "READY", "owned": True}

        class Sessions:
            def __init__(self):
                self.calls = []

            def click_local_control(self, session_id, label):
                self.calls.append(("click", session_id, label))
                if label == "Missing target":
                    return {"status": "BLOCKED", "error": "LOCAL_E2E_CLICK_TARGET_NOT_UNIQUE"}
                return {"status": "AVAILABLE", "url": "http://127.0.0.1:5173/", "page": label}

            def type_local_field(self, session_id, label, value):
                self.calls.append(("type", session_id, label, value))
                return {"status": "AVAILABLE", "url": "http://127.0.0.1:5173/"}

            def navigate_local_path(self, session_id, path):
                self.calls.append(("navigate", session_id, path))
                return {"status": "AVAILABLE", "url": f"http://127.0.0.1:5173{path}"}

            def check_local_text(self, session_id, text, *, wait=False, timeout_seconds=10.0):
                self.calls.append(("wait_for_text" if wait else "assert_text", session_id, text))
                return {"status": "AVAILABLE", "verified": True, "url": "http://127.0.0.1:5173/"}

            def snapshot(self, _session_id):
                return {"status": "AVAILABLE", "clean_markdown": "latest visible state"}

        sessions = Sessions()
        api.local_e2e = LocalRuns()
        api.browser_sessions = sessions
        api.settings = self.original_settings.with_overrides({"local_e2e_enabled": True})
        chat_context = patch("olcr_api.app._gui_session_token", ""), patch.object(
            api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"})
        with chat_context[0], chat_context[1]:
            result = self.client.post("/api/chat", json={
                "message": 'type "Alice" into "Player name"; click "Start game"; then click "Continue"',
                "conversation_id": conversation_id, "project_id": api.db.default_project_id(),
                "message_id": "chat-local-e2e-sequence-success",
            })
        self.assertEqual(200, result.status_code, result.text)
        block = result.json()["blocks"][0]
        self.assertEqual(["VALUE_VERIFIED", "DISPATCHED", "DISPATCHED"],
                         [step["status"] for step in block["actions"]])
        self.assertEqual("latest visible state", block["snapshot"])
        self.assertEqual([
            ("type", "local-session-sequence", "Player name", "Alice"),
            ("click", "local-session-sequence", "Start game"),
            ("click", "local-session-sequence", "Continue"),
        ], sessions.calls)

        with chat_context[0], chat_context[1]:
            verified_flow = self.client.post("/api/chat", json={
                "message": 'navigate to "/game/level-2"; wait for text "Level 2"; assert text "Ready"',
                "conversation_id": conversation_id, "project_id": api.db.default_project_id(),
                "message_id": "chat-local-e2e-sequence-verify",
            })
        self.assertEqual(200, verified_flow.status_code, verified_flow.text)
        verified_block = verified_flow.json()["blocks"][0]
        self.assertEqual(["NAVIGATION_VERIFIED", "POSTCONDITION_OBSERVED", "POSTCONDITION_OBSERVED"],
                         [step["status"] for step in verified_block["actions"]])
        self.assertEqual([
            ("navigate", "local-session-sequence", "/game/level-2"),
            ("wait_for_text", "local-session-sequence", "Level 2"),
            ("assert_text", "local-session-sequence", "Ready"),
        ], sessions.calls[3:])

        with chat_context[0], chat_context[1]:
            partial = self.client.post("/api/chat", json={
                "message": 'click "Start game"; click "Missing target"; click "Continue"',
                "conversation_id": conversation_id, "project_id": api.db.default_project_id(),
                "message_id": "chat-local-e2e-sequence-partial",
            })
        self.assertEqual(200, partial.status_code, partial.text)
        partial_block = partial.json()["blocks"][0]
        self.assertEqual(["Start game"], [step["label"] for step in partial_block["actions"]])
        self.assertIn("1/3", partial.json()["response"])
        self.assertEqual(8, len(sessions.calls))

    def test_local_e2e_chat_attaches_to_explicit_loopback_without_a_manifest(self):
        workspace = Path(_profile.name) / "sample-game-attach"
        workspace.mkdir(exist_ok=True)
        conversation_id = api.db.create_conversation("local E2E attach", 1.0,
            "chat-local-e2e-attach-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)

        class LocalRuns:
            def attach(self, selected_workspace, allowed_roots, origin):
                self.attached = (selected_workspace, tuple(allowed_roots), origin)
                return {"id": "local-attach-1", "origin": origin, "status": "READY", "owned_process": False}

        class Sessions:
            def open_local(self, origin):
                self.origin = origin
                return {"status": "READY", "session_id": "local-session-attach"}

            def navigate(self, session_id, url):
                self.navigation = (session_id, url)
                return {"status": "AVAILABLE", "url": url, "title": "Local game", "clean_markdown": "button Start"}

        runs = LocalRuns()
        sessions = Sessions()
        api.local_e2e = runs
        api.browser_sessions = sessions
        api.settings = self.original_settings.with_overrides({"local_e2e_enabled": True})
        origin = "http://127.0.0.1:5173/game?level=1"
        canonical_origin = "http://127.0.0.1:5173"
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"}):
            result = self.client.post("/api/chat", json={
                "message": f"Attach local E2E to {origin}", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-local-e2e-attach",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(canonical_origin, runs.attached[2])
        self.assertEqual(canonical_origin, sessions.origin)
        self.assertEqual(("local-session-attach", origin), sessions.navigation)
        self.assertEqual(origin, result.json()["blocks"][0]["url"])
        self.assertFalse(result.json()["blocks"][0]["owned_process"])
        self.assertEqual("READY", result.json()["blocks"][0]["status"])

    def test_local_e2e_attach_without_origin_asks_for_target_instead_of_resolving_start_script(self):
        workspace = Path(_profile.name) / "sample-game-attach-needs-origin"
        workspace.mkdir(exist_ok=True)
        (workspace / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
        conversation_id = api.db.create_conversation("local E2E attach target", 1.0,
            "chat-local-e2e-attach-needs-origin", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)

        class LocalRuns:
            def attach(self, *_args):
                raise AssertionError("attach must wait for an explicit origin")

        api.local_e2e = LocalRuns()
        api.settings = self.original_settings.with_overrides({"local_e2e_enabled": True})
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"}):
            result = self.client.post("/api/chat", json={
                "message": "Attach local E2E", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-local-e2e-attach-no-origin",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.assertIn("loopback URL", result.json()["response"])

    def test_local_e2e_script_clarification_reply_starts_the_selected_script(self):
        workspace = Path(_profile.name) / "sample-game-ambiguous"
        workspace.mkdir(exist_ok=True)
        conversation_id = api.db.create_conversation("local E2E script choice", 1.0,
            "chat-local-e2e-script-choice-conversation", api.db.default_project_id())
        self.conversation_ids.append(conversation_id)

        class LocalRuns:
            def start(self, _workspace, _allowed_roots, *, expected_origin=None,
                      requested_script=None, requested_command=None):
                if requested_script is None:
                    raise ValueError("LOCAL_E2E_SCRIPT_AMBIGUOUS: dev, start")
                self.started = (expected_origin, requested_script)
                return {"id": "local-script-choice-1", "origin": expected_origin,
                        "owned_process": True, "status": "READY"}

        class Sessions:
            def open_local(self, origin):
                return {"status": "READY", "session_id": "local-session-script-choice"}

            def navigate(self, _session_id, url):
                return {"status": "AVAILABLE", "url": url, "title": "Game", "clean_markdown": "canvas"}

        runs = LocalRuns()
        api.local_e2e = runs
        api.browser_sessions = Sessions()
        api.settings = self.original_settings.with_overrides({"local_e2e_enabled": True})
        origin = "http://127.0.0.1:4173"
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"}):
            question = self.client.post("/api/chat", json={
                "message": f"Start local E2E at {origin}", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-local-e2e-script-question",
            })
        self.assertEqual(200, question.status_code, question.text)
        self.assertIn("dev, start", question.json()["response"])
        with patch("olcr_api.app._gui_session_token", ""), \
             patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Game"}):
            selected = self.client.post("/api/chat", json={
                "message": "dev", "conversation_id": conversation_id,
                "project_id": api.db.default_project_id(), "message_id": "chat-local-e2e-script-answer",
            })
        self.assertEqual(200, selected.status_code, selected.text)
        self.assertEqual((origin, "dev"), runs.started)
        self.assertEqual("local_e2e_task", selected.json()["blocks"][0]["type"])

    def test_chat_local_e2e_natural_language_plan_starts_browser_actions_and_stops(self):
        from olcr_api.browser_network import BrowserSessionManager
        from olcr_api.local_e2e import LocalE2EManager
        from olcr_api.node_mcp_runtime import managed_browser_command
        if not managed_browser_command():
            self.skipTest("OLCR bundled Playwright MCP runtime is unavailable")
        workspace = Path(_profile.name) / "chat-real-local-e2e"
        (workspace / ".olcr").mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        server = workspace / "server.py"
        server.write_text(
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "import argparse\n"
            "p=argparse.ArgumentParser(); p.add_argument('--host'); p.add_argument('--port', type=int); a=p.parse_args()\n"
            "class H(BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  b=b\"<!doctype html><title>Chat game</title><label for='player'>Player name</label><input id='player' name='player'><button type='button' onclick=\\\"document.querySelector('#result').textContent='Started: '+document.querySelector('#player').value;document.querySelector('#continue').disabled=false\\\">Start game</button><button id='continue' type='button' disabled onclick=\\\"document.querySelector('#result').textContent += ' - Continued'\\\">Continue</button><p id='result'></p>\"; self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)\n"
            " def log_message(self,*args): pass\n"
            "HTTPServer((a.host,a.port),H).serve_forever()\n", encoding="utf-8")
        manager = LocalE2EManager(startup_timeout=8)
        sessions = BrowserSessionManager(api.managed_browser_command, maximum_sessions=1)
        api.local_e2e = manager
        api.browser_sessions = sessions
        api.settings = self.original_settings.with_overrides({
            "local_e2e_enabled": True,
            "allowed_roots": [*self.original_settings.allowed_roots, str(workspace)],
        })
        try:
            with patch("olcr_api.app._gui_session_token", ""), \
                 patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Local game"}):
                launched = self.client.post("/api/chat", json={
                    "message": (f"Start local E2E with command `{shlex.quote(sys.executable)} "
                                f"{shlex.quote(str(server))} "
                                f"--host 127.0.0.1 --port {port}` at {origin}"),
                    "message_id": "chat-local-e2e-real-start"})
            self.assertEqual(200, launched.status_code, launched.text)
            self.conversation_ids.append(launched.json()["conversation_id"])
            block = launched.json()["blocks"][0]
            self.assertEqual("READY", block["status"])
            self.assertEqual(origin, block["origin"])
            self.assertIn("Start game", block["snapshot"])
            frame = sessions.screenshot(block["session_id"])
            self.assertEqual("AVAILABLE", frame["status"], frame)
            self.assertEqual("image/jpeg", frame["mime_type"])
            self.assertTrue(frame["data"])

            with patch("olcr_api.app._gui_session_token", ""), \
                 patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Local game"}), \
                 patch.object(api.runtime.model, "generate", return_value={
                     "text": json.dumps({"decision": "not_action", "clarification": "", "actions": []})
                 }) as rejected_generate:
                rejected = self.client.post("/api/chat", json={
                    "message": ('Please enter Alice as the player name, start the game, and verify '
                                '"Started: Alice".'),
                    "conversation_id": launched.json()["conversation_id"],
                    "project_id": api.db.default_project_id(), "message_id": "chat-local-e2e-planner-reject"})
            self.assertEqual(200, rejected.status_code, rejected.text)
            self.assertIn("安全なE2E操作に変換できませんでした", rejected.json()["response"])
            self.assertNotIn("LOCAL_E2E_PROCESS_EXITED_BEFORE_READY", rejected.json()["response"])
            self.assertEqual(1, rejected_generate.call_count,
                             "a rejected action plan must not fall through into ordinary chat generation")

            natural_plan = {"decision": "execute", "clarification": "", "actions": [
                {"kind": "type", "label": "Player name", "value": "Alice"},
                {"kind": "click", "label": "Start game"},
                {"kind": "click", "label": "Continue"},
                {"kind": "wait_for_text", "text": "Started: Alice - Continued"},
                {"kind": "assert_text", "text": "Continued"},
            ]}
            with patch("olcr_api.app._gui_session_token", ""), \
                 patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Local game"}), \
                 patch.object(api.runtime.model, "generate", return_value={"text": json.dumps(natural_plan)}) as model_generate:
                operated = self.client.post("/api/chat", json={
                    "message": ('Please enter Alice as the player name, start the game and continue, wait until '
                                '"Started: Alice - Continued" appears, then verify "Continued".'),
                    "conversation_id": launched.json()["conversation_id"],
                    "project_id": api.db.default_project_id(), "message_id": "chat-local-e2e-natural-sequence"})
            self.assertEqual(200, operated.status_code, operated.text)
            self.assertEqual(1, model_generate.call_count)
            operated_block = operated.json()["blocks"][0]
            self.assertEqual(["VALUE_VERIFIED", "DISPATCHED", "DISPATCHED",
                              "POSTCONDITION_OBSERVED", "POSTCONDITION_OBSERVED"],
                             [step["status"] for step in operated_block["actions"]])
            self.assertIn("Started: Alice - Continued", operated_block["snapshot"])

            # The user can state the goal without prescribing its exact UI text.
            # The model derives a concise postcondition, which is checked after
            # the click against a fresh accessibility snapshot.
            sessions.navigate(operated_block["session_id"], origin + "/")
            inferred_plan = {"decision": "execute", "clarification": "", "actions": [
                {"kind": "click", "label": "Start game"},
                {"kind": "assert_text", "text": "Started:"},
            ]}
            with patch("olcr_api.app._gui_session_token", ""), \
                 patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Local game"}), \
                 patch.object(api.runtime.model, "generate", return_value={"text": json.dumps(inferred_plan)}) as inferred_generate:
                inferred = self.client.post("/api/chat", json={
                    "message": "Start the game and confirm that it begins.",
                    "conversation_id": launched.json()["conversation_id"],
                    "project_id": api.db.default_project_id(), "message_id": "chat-local-e2e-inferred-outcome"})
            self.assertEqual(200, inferred.status_code, inferred.text)
            self.assertEqual(1, inferred_generate.call_count)
            inferred_block = inferred.json()["blocks"][0]
            self.assertEqual(["DISPATCHED", "POSTCONDITION_OBSERVED"],
                             [step["status"] for step in inferred_block["actions"]])
            self.assertIn("Started:", inferred_block["snapshot"])

            with patch("olcr_api.app._gui_session_token", ""), \
                 patch.object(api.db, "project", return_value={"workspace_path": str(workspace), "name": "Local game"}):
                stopped = self.client.post("/api/chat", json={
                    "message": "stop local E2E", "conversation_id": launched.json()["conversation_id"],
                    "project_id": api.db.default_project_id(), "message_id": "chat-local-e2e-real-stop"})
            self.assertEqual(200, stopped.status_code, stopped.text)
            self.assertEqual("STOPPED", stopped.json()["blocks"][0]["status"])
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.2)
        finally:
            manager.close_all()
            sessions.close_all()

    def test_blocked_destination_closes_new_session(self):
        calls = []

        class Sessions:
            def open(self): return {"status": "READY", "session_id": "blocked-session"}
            def navigate(self, session_id, url): return {"status": "BLOCKED", "error": "PRIVATE_ADDRESS"}
            def close(self, session_id): calls.append(session_id); return True

        api.settings = self.original_settings.with_overrides({"web_scraping_enabled": True})
        api.browser_sessions = Sessions()
        with patch("olcr_api.app._gui_session_token", ""):
            result = self.client.post("/api/chat", json={
                "message": "Scrape https://example.test/private",
                "message_id": "chat-scrape-blocked-1",
            })
        self.assertEqual(200, result.status_code, result.text)
        self.conversation_ids.append(result.json()["conversation_id"])
        self.assertEqual(["blocked-session"], calls)
        self.assertNotIn("blocks", result.json())


if __name__ == "__main__":
    unittest.main()
