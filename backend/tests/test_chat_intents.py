import unittest

from olcr_api.chat_intents import (chat_scrape_clarification, chat_scrape_control, chat_scrape_control_target_number,
                                   chat_scrape_readonly_click, chat_scrape_readonly_navigation_request,
                                   chat_scrape_request,
                                   chat_local_e2e_request, chat_local_e2e_control, chat_local_e2e_click,
                                   chat_local_e2e_type, chat_local_e2e_action_sequence,
                                   chat_local_e2e_script_choice, chat_computer_use_intent,
                                   chat_local_e2e_action_request, validate_chat_local_e2e_plan)


class ChatScrapeIntentTests(unittest.TestCase):
    def test_resolves_only_short_explicit_scrape_controls(self):
        self.assertEqual("pause", chat_scrape_control("Pause scraping"))
        self.assertEqual("stop", chat_scrape_control("スクレイピングを停止して"))
        self.assertEqual("resume", chat_scrape_control("Resume the scrape"))
        self.assertEqual("status", chat_scrape_control("進捗を確認して"))
        self.assertEqual("results", chat_scrape_control("show results"))
        self.assertEqual("results", chat_scrape_control("スクレイピング結果を表示して"))
        self.assertEqual("export", chat_scrape_control("export scrape results"))
        self.assertEqual("export", chat_scrape_control("スクレイピング結果をダウンロードして"))
        self.assertEqual("export", chat_scrape_control("スクレイプ結果をエクスポート"))
        self.assertEqual("pause", chat_scrape_control("Pause scraping #2"))
        self.assertEqual("results", chat_scrape_control("show results #1"))
        self.assertEqual("stop", chat_scrape_control("スクレイピングを停止して #2"))
        self.assertEqual(2, chat_scrape_control_target_number("Pause scraping #2"))
        self.assertIsNone(chat_scrape_control_target_number("Pause scraping"))
        self.assertIsNone(chat_scrape_control("Pause scraping #0"))
        self.assertIsNone(chat_scrape_control("Do not stop scraping yet"))
        self.assertIsNone(chat_scrape_control("stop the music"))
        self.assertIsNone(chat_scrape_control("What are the scrape results?"))

    def test_resolves_explicit_readonly_scrape_control_label(self):
        self.assertEqual("Load more", chat_scrape_readonly_click('click the button "Load more"'))
        self.assertEqual("Next", chat_scrape_readonly_click('「Next」リンクをクリックして'))
        self.assertIsNone(chat_scrape_readonly_click("click a button"))
        self.assertEqual("Add to cart", chat_scrape_readonly_click('click "Add to cart"'))
        self.assertEqual("go to the next page", chat_scrape_readonly_navigation_request("go to the next page"))
        self.assertEqual("次のページへ進んで", chat_scrape_readonly_navigation_request("次のページへ進んで"))
        self.assertIsNone(chat_scrape_readonly_navigation_request("collect prices from this page"))
        self.assertIsNone(chat_scrape_readonly_navigation_request('click the button "Load more"'))

    def test_resolves_explicit_english_scrape_request_and_strips_url(self):
        result = chat_scrape_request("Please scrape https://example.test/catalog?page=1, collect product names and prices")
        self.assertEqual("https://example.test/catalog?page=1", result["url"])
        self.assertIn("product names and prices", result["instruction"])

    def test_resolves_explicit_japanese_scrape_request(self):
        result = chat_scrape_request("このページをスクレイピングして、組織名とプロフィールリンクを集めて https://example.test/directory")
        self.assertEqual("https://example.test/directory", result["url"])
        self.assertIn("組織名", result["instruction"])

    def test_defaults_to_visible_repeating_records_when_no_fields_are_named(self):
        result = chat_scrape_request("Scrape this URL: https://example.test/catalog")
        self.assertIn("main repeating records", result["instruction"])

    def test_does_not_route_plain_links_or_questions_about_scraping(self):
        self.assertIsNone(chat_scrape_request("Open https://example.test/catalog"))
        self.assertIsNone(chat_scrape_request("What is web scraping? https://example.test"))
        self.assertIsNone(chat_scrape_request("スクレイピングって何？ https://example.test"))

    def test_does_not_route_negated_or_ambiguous_targets(self):
        self.assertIsNone(chat_scrape_request("Don't scrape https://example.test"))
        self.assertEqual(["https://one.test", "https://two.test"], chat_scrape_request("https://one.test and https://two.test をスクレイピングして")["urls"])
        self.assertIsNone(chat_scrape_request("scrape https://user:secret@example.test/"))

    def test_resolves_explicit_local_e2e_chat_launch(self):
        self.assertEqual({"mode": "start", "origin": None, "script": None},
                         chat_local_e2e_request("computer use ONの状態で実E2Eを開始して"))
        self.assertEqual({"mode": "start", "origin": None, "script": None},
                         chat_local_e2e_request("Computer Useで自分のサイトのE2Eを開始して"))
        self.assertEqual({"mode": "start", "origin": None, "script": None},
                         chat_local_e2e_request("computer use ONの状態で実 Eto Eを開始して"))
        self.assertEqual({"mode": "start", "origin": "http://127.0.0.1:5173", "script": None},
                         chat_local_e2e_request("Start local E2E at http://127.0.0.1:5173"))
        self.assertEqual({"mode": "attach", "origin": "http://localhost:4173", "script": None},
                         chat_local_e2e_request("ローカルE2Eに接続して http://localhost:4173"))
        self.assertEqual({"mode": "start", "origin": None, "script": "dev"},
                         chat_local_e2e_request("Start local E2E using dev script"))
        self.assertEqual({"mode": "start", "origin": None, "script": "start"},
                         chat_local_e2e_request("Run local E2E with npm run start"))
        self.assertEqual({"mode": "start", "origin": "http://127.0.0.1:5173", "script": None,
                          "command": "python3 server.py --host 127.0.0.1 --port 5173"},
                         chat_local_e2e_request(
                             "Start local E2E with command `python3 server.py --host 127.0.0.1 --port 5173` "
                             "at http://127.0.0.1:5173"))
        self.assertIsNone(chat_local_e2e_request(
            "Start local E2E with command `python3 server.py\n--host 127.0.0.1 --port 5173`"))

    def test_local_e2e_controls_and_clicks_are_explicit(self):
        self.assertEqual("stop", chat_local_e2e_control("stop local e2e"))
        self.assertEqual("status", chat_local_e2e_control("ローカルE2Eの状態を確認して"))
        self.assertEqual("Start game", chat_local_e2e_click("「Start game」ボタンをクリック"))
        self.assertEqual("Start game", chat_local_e2e_click("「Start game」ボタンをクリックして"))
        self.assertEqual("Start game", chat_local_e2e_click("click the Start game button"))
        self.assertIsNone(chat_local_e2e_click("click a button"))
        self.assertIsNone(chat_local_e2e_request("Scrape http://127.0.0.1:5173"))
        self.assertIsNone(chat_local_e2e_request("Start E2E at https://example.test"))

    def test_local_e2e_script_choice_requires_a_matching_pending_chat_question(self):
        history = [
            {"role": "user", "content": "Start local E2E at http://localhost:4173"},
            {"role": "assistant", "content": "起動候補が複数あります（dev, start）。使うscriptをこのチャットで指定してください。"},
        ]
        self.assertEqual({"mode": "start", "origin": "http://localhost:4173", "script": "dev"},
                         chat_local_e2e_script_choice("dev", history))
        self.assertIsNone(chat_local_e2e_script_choice("serve", history))
        self.assertIsNone(chat_local_e2e_script_choice("dev", []))
        self.assertIsNone(chat_local_e2e_script_choice("dev", history + [
            {"role": "user", "content": "What is in this project?"},
            {"role": "assistant", "content": "The project contains a web application."},
        ]))

    def test_local_e2e_type_requires_quoted_value_and_accessible_label(self):
        self.assertEqual({"label": "Player name", "value": "Alice"},
                         chat_local_e2e_type("「Player name」に「Alice」を入力して"))
        self.assertEqual({"label": "Player name", "value": "Alice"},
                         chat_local_e2e_type('type "Alice" into "Player name"'))
        self.assertIsNone(chat_local_e2e_type("type Alice into a field"))
        self.assertIsNone(chat_local_e2e_type("「入力欄」に「x」を入力して"))

    def test_local_e2e_action_sequence_requires_bounded_explicit_actions(self):
        self.assertEqual([
            {"kind": "type", "label": "Player name", "value": "Alice"},
            {"kind": "click", "label": "Start game"},
            {"kind": "click", "label": "Continue"},
        ], chat_local_e2e_action_sequence(
            'type "Alice" into "Player name"; click "Start game"; then click "Continue"'))
        self.assertEqual([
            {"kind": "click", "label": "Start game"},
            {"kind": "type", "label": "Player name", "value": "Alice"},
        ], chat_local_e2e_action_sequence(
            '「Start game」ボタンをクリックしてから「Player name」に「Alice」を入力して'))
        self.assertEqual([
            {"kind": "type", "label": "Player name", "value": "Alice and Bob"},
            {"kind": "click", "label": "Start game"},
        ], chat_local_e2e_action_sequence(
            'type "Alice and Bob" into "Player name" and click "Start game"'))
        self.assertEqual([
            {"kind": "navigate", "path": "/game/level-2"},
            {"kind": "wait_for_text", "text": "Level 2"},
            {"kind": "assert_text", "text": "Ready"},
        ], chat_local_e2e_action_sequence(
            'navigate to "/game/level-2"; wait for text "Level 2"; assert text "Ready"'))
        self.assertEqual([{"kind": "navigate", "path": "/settings"}],
                         chat_local_e2e_action_sequence('「/settings」へ移動'))
        self.assertEqual([{"kind": "wait_for_text", "text": "Loaded"}],
                         chat_local_e2e_action_sequence('「Loaded」が表示されるまで待って'))
        self.assertIsNone(chat_local_e2e_action_sequence('navigate to "//evil.test/path"'))
        self.assertIsNone(chat_local_e2e_action_sequence('navigate to "https://example.test"'))
        self.assertIsNone(chat_local_e2e_action_sequence('click "Start game"; visit https://example.test'))
        self.assertIsNone(chat_local_e2e_action_sequence('; '.join('click "A"' for _ in range(7))))

    def test_detects_natural_language_e2e_action_requests(self):
        self.assertTrue(chat_local_e2e_action_request("Please enter Alice, start the game, and verify Started"))
        self.assertTrue(chat_local_e2e_action_request("ゲームを始めて、結果が出るまで進めて"))
        self.assertFalse(chat_local_e2e_action_request("What does the Start button do?"))
        self.assertFalse(chat_local_e2e_action_request("Scrape this page"))

    def test_validates_model_actions_against_user_values_and_current_visible_evidence(self):
        candidates = [
            {"kind": "field", "tag": "input", "text": "Player name"},
            {"kind": "control", "tag": "button", "text": "Start game"},
            {"kind": "navigation", "tag": "a", "text": "Level 2",
             "attributes": {"href": "http://127.0.0.1:5173/game/level-2"}},
        ]
        origin = "http://127.0.0.1:5173"
        plan = {"decision": "execute", "actions": [
            {"kind": "type", "label": "player name", "value": "Alice"},
            {"kind": "click", "label": "start game"},
            {"kind": "navigate", "path": "/game/level-2"},
            {"kind": "assert_text", "text": "Ready"},
        ]}
        validated = validate_chat_local_e2e_plan(plan,
            "Enter Alice, start the game, navigate to Level 2, and verify Ready", candidates, origin)
        self.assertEqual("Player name", validated["actions"][0]["label"])
        self.assertEqual("Start game", validated["actions"][1]["label"])
        self.assertEqual("execute", validated["decision"])
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PLAN_INVALID"):
            validate_chat_local_e2e_plan({"decision": "not_action", "actions": []},
                                         "Enter Alice and start the game", candidates, origin)
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PLAN_INPUT_VALUE_NOT_USER_PROVIDED"):
            validate_chat_local_e2e_plan({"decision": "execute", "actions": [
                {"kind": "type", "label": "Player name", "value": "Invented"},
            ]}, "Enter my name", candidates, origin)
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PLAN_EXPECTED_TEXT_NOT_USER_PROVIDED"):
            validate_chat_local_e2e_plan({"decision": "execute", "actions": [
                {"kind": "assert_text", "text": "Finished"},
            ]}, "Start the game and verify the result", candidates, origin)
        inferred_outcome = validate_chat_local_e2e_plan({"decision": "execute", "actions": [
            {"kind": "click", "label": "Start game"},
            {"kind": "assert_text", "text": "Game started"},
        ]}, "Start the game", candidates, origin)
        self.assertEqual({"kind": "assert_text", "text": "Game started"}, inferred_outcome["actions"][1])
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PLAN_INFERRED_OUTCOME_ALREADY_VISIBLE"):
            validate_chat_local_e2e_plan({"decision": "execute", "actions": [
                {"kind": "click", "label": "Start game"},
                {"kind": "assert_text", "text": "Game started"},
            ]}, "Start the game", candidates, origin, current_visible_text="Game started")
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PLAN_NAVIGATION_NOT_GROUNDED"):
            validate_chat_local_e2e_plan({"decision": "execute", "actions": [
                {"kind": "navigate", "path": "/secret"},
            ]}, "Open /secret", candidates, origin)
        candidates[2]["attributes"]["href"] = "https://evil.example/game/level-2"
        with self.assertRaisesRegex(ValueError, "LOCAL_E2E_PLAN_NAVIGATION_NOT_GROUNDED"):
            validate_chat_local_e2e_plan({"decision": "execute", "actions": [
                {"kind": "navigate", "path": "/game/level-2"},
            ]}, "Open the Level 2 page", candidates, origin)

    def test_native_computer_use_lifecycle_does_not_overlap_local_e2e(self):
        self.assertEqual("start", chat_computer_use_intent("start computer use"))
        self.assertEqual("start", chat_computer_use_intent("コンピュータユーズを開始して"))
        self.assertEqual("status", chat_computer_use_intent("computer use status"))
        self.assertEqual("stop", chat_computer_use_intent("画面操作を停止して"))
        self.assertEqual("start", chat_computer_use_intent("computer use ONの状態で実E2Eを開始して"))
        self.assertEqual("start", chat_computer_use_intent("Start a real GUI E2E with Computer Use for Calculator"))
        self.assertEqual("start", chat_computer_use_intent("Start an E2E with Computer Use"))
        self.assertEqual("start", chat_computer_use_intent(
            "Computer Useで実GUI E2Eを開始して。対象アプリは /tmp/FixtureGame.app です。"
        ))
        self.assertIsNone(chat_computer_use_intent("Run a website E2E with Computer Use"))
        self.assertIsNone(chat_computer_use_intent("Run a real GUI E2E with Computer Use on my website"))
        self.assertIsNone(chat_computer_use_intent("Computer Useで自分のサイトのE2Eを開始して"))
        self.assertIsNone(chat_computer_use_intent("Start local E2E with Computer Use"))
        self.assertIsNone(chat_computer_use_intent("Start Playwright E2E at http://127.0.0.1:5173"))
        self.assertIsNone(chat_computer_use_intent("scrape https://example.test with computer use"))


    def test_chat_scrape_clarification_targets_only_latest_pending_task(self):
        history = [{"role": "assistant", "blocks": [{
            "type": "scrape_task", "session_id": "session-1",
            "url": "https://example.test/catalog", "instruction": "Collect product data",
            "needs_clarification": True,
        }]}]
        result = chat_scrape_clarification("商品名と価格だけを集めて", history)
        self.assertEqual("session-1", result["session_id"])
        self.assertIn("商品名と価格だけを集めて", result["instruction"])
        self.assertIn("takes precedence", result["instruction"])

    def test_chat_scrape_clarification_reuses_pending_session_when_same_url_is_repeated(self):
        url = "https://example.test/catalog"
        history = [{"role": "assistant", "blocks": [{
            "type": "scrape_task", "session_id": "session-1", "url": url,
            "instruction": "Collect product data", "needs_clarification": True,
        }]}]
        result = chat_scrape_clarification(
            f"Scrape this page for name and address: {url}", history)
        self.assertEqual("session-1", result["session_id"])
        self.assertNotIn(url, result["instruction"])
        self.assertIn("name and address", result["instruction"])

    def test_chat_scrape_clarification_fails_closed_for_other_urls_or_completed_tasks(self):
        pending = {"type": "scrape_task", "session_id": "session-1",
                   "needs_clarification": True}
        completed = {**pending, "run_id": "run-1"}
        self.assertIsNone(chat_scrape_clarification("商品名だけにして", [{"blocks": [completed]}]))
        self.assertIsNone(chat_scrape_clarification("Scrape https://other.test for names", [{"blocks": [pending]}]))

    def test_newer_non_pending_task_supersedes_older_pending_scrape(self):
        history = [
            {"blocks": [{"type": "scrape_task", "session_id": "old", "needs_clarification": True}]},
            {"blocks": [{"type": "scrape_task", "session_id": "new", "run_id": "run-new",
                         "needs_clarification": False}]},
        ]
        self.assertIsNone(chat_scrape_clarification("Collect only the title", history))


if __name__ == "__main__":
    unittest.main()
