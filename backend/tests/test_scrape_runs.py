import tempfile
import unittest
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from olcr_api.db import Database
from olcr_api.recipes import RecipeNavigation, ScrapeRecipeInput
from olcr_api.scrape_runs import ScrapeRunManager


class FakeBrowserSessions:
    def __init__(self, pages, next_urls=None):
        self.pages = pages
        self.next_urls = next_urls or {}
        self.visited = []
        self.closed = []
        self.actions = []
        self.current_page = 1
        self.current_url = ""
        self.history = []
        self.detail_data = {}
        self.snapshot_states = []
        self.snapshots = 0
        self.extraction_started_after_snapshot = False
        self.operations = []
        self.page_load_waits = 0
        self.opened_profiles = []

    def open(self, profile_id=None, **kwargs):
        self.opened_profiles.append(profile_id)
        return {"status": "READY", "session_id": "run-session"}
    def navigate(self, session_id, url):
        self.operations.append(("navigate", url))
        self.visited.append(url)
        if self.current_url:
            self.history.append(self.current_url)
        self.current_url = url
        self.current_page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        return {"status": "AVAILABLE", "url": url}
    def navigate_back(self, session_id):
        if self.history:
            self.current_url = self.history.pop()
        return {"status":"AVAILABLE", "url":self.current_url}
    def click_recipe_load_more(self, session_id, selector):
        self.actions.append(("load_more", selector)); self.current_page += 1
        return {"status":"AVAILABLE", "url":self.visited[-1]}
    def scroll_for_recipe_page(self, session_id):
        self.actions.append(("infinite_scroll", None)); self.current_page += 1
        return {"status":"AVAILABLE", "url":self.visited[-1]}
    def extract_recipe(self, session_id, record_selector, fields, *, limit=100):
        self.operations.append(("extract", record_selector))
        self.extraction_started_after_snapshot = self.snapshots > 0
        if record_selector == "body":
            row = self.detail_data.get(self.current_url)
            return {"status":"AVAILABLE", "rows":[row] if row else [],
                    "record_count":1 if row else 0,
                    "field_match_counts":{name:1 if row and name in row else 0 for name in fields}}
        page = self.current_page
        rows = self.pages.get(page, [])[:limit]
        return {"status": "AVAILABLE", "rows": rows, "record_count": len(rows), "truncated": False,
                "field_match_counts": {name: sum(name in row for row in rows) for name in fields}}
    def snapshot(self, session_id):
        self.snapshots += 1
        if self.snapshot_states:
            content = self.snapshot_states.pop(0)
        else:
            content = f"page-{self.current_page}"
        return {"status": "AVAILABLE", "url": self.current_url,
                "title": f"Page {self.current_page}", "clean_markdown": content}
    def picker_evidence(self, session_id, kind):
        return {"status": "AVAILABLE", "candidates": []}
    def wait_for_page_load(self, session_id):
        self.page_load_waits += 1
        return {"status": "AVAILABLE", "ready": True}
    def extract_next_link(self, session_id, selector):
        return {"status": "AVAILABLE", "url": self.next_urls.get(self.visited[-1])}
    def close(self, session_id): self.closed.append(session_id); return True


class ScrapeRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "runs.db"))
        self.db.initialize()
        record = ScrapeRecipeInput.model_validate({
            "name": "Directory", "start_url": "https://example.com/people?sort=name",
            "record_selector": "tbody > tr",
            "fields": {"name": {"selector": "td.name", "extract": "text"},
                       "group": {"selector": "td.group", "extract": "text"}},
            "navigation": {"type": "url_parameter", "page_parameter": "page"},
            "deduplicate_keys": ["name"], "max_pages": 2, "max_records": 10,
        }).to_record("recipe-1")
        self.db.save_web_recipe(record)
        self.recipe = self.db.set_web_recipe_status("recipe-1", "VALIDATED", {"record_count": 1, "field_match_counts": {"name": 1, "group": 1}})

    def tearDown(self): self.tmp.cleanup()

    def test_deterministic_pagination_checkpoints_dedupes_and_streams(self):
        browser = FakeBrowserSessions({
            1: [{"name": "Acme", "group": "Research"}, {"name": "Beta", "group": "Design"}],
            2: [{"name": "Beta", "group": "Design"}, {"name": "Gamma", "group": "Energy"}],
        })
        run = self.db.create_web_scrape_run("run-1", self.recipe)
        manager = ScrapeRunManager(self.db, browser)
        manager.execute(run["id"])
        result = self.db.web_scrape_run("run-1")
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual("MAX_PAGES", result["stop_reason"])
        self.assertEqual((2, 2, 3), (result["page_cursor"], result["pages_completed"], result["record_count"]))
        self.assertEqual(["1", "2"], [parse_qs(urlsplit(url).query)["page"][0] for url in browser.visited])
        self.assertEqual(["Acme", "Beta", "Gamma"], [row["name"] for row in self.db.stream_web_scrape_rows("run-1")])
        self.assertEqual({
            "pages_visited": 2, "records_discovered": 4, "duplicates_removed": 1,
            "dom_extractions": 2, "network_extractions": 0,
            "detail_pages_visited": 0, "detail_extractions": 0,
            "selector_recoveries": 0, "records_written": 3,
        }, {key: result["metrics"][key] for key in (
            "pages_visited", "records_discovered", "duplicates_removed", "dom_extractions",
            "network_extractions", "detail_pages_visited", "detail_extractions",
            "selector_recoveries", "records_written",
        )})
        self.assertGreaterEqual(result["metrics"]["elapsed_ms"], 0)
        self.assertGreaterEqual(result["metrics"]["records_per_second"], 0)
        self.assertEqual(["run-session"], browser.closed)
        self.assertGreaterEqual(browser.snapshots, 2)
        self.assertTrue(browser.extraction_started_after_snapshot)
        self.assertEqual([None], browser.opened_profiles)

    def test_url_parameter_pagination_preserves_selected_start_page(self):
        self.assertEqual("https://example.com/list?query=shops&page=4",
                         ScrapeRunManager._page_url("https://example.com/list?query=shops&page=4", "page", 1))
        self.assertEqual("https://example.com/list?query=shops&page=5",
                         ScrapeRunManager._page_url("https://example.com/list?query=shops&page=4", "page", 2))

    def test_run_reopens_the_recipe_persistent_profile(self):
        recipe = ScrapeRecipeInput.from_record(self.recipe).model_copy(
            update={"browser_profile_id": "account-one"})
        record = recipe.to_record("recipe-profile")
        self.db.save_web_recipe(record)
        validated = self.db.set_web_recipe_status("recipe-profile", "VALIDATED", {
            "record_count": 1, "field_match_counts": {"name": 1, "group": 1}})
        browser = FakeBrowserSessions({1: [{"name": "Acme", "group": "Research"}]})
        run = self.db.create_web_scrape_run("run-profile", validated)
        ScrapeRunManager(self.db, browser).execute(run["id"])
        self.assertEqual(["account-one"], browser.opened_profiles)
        result = self.db.web_scrape_run("run-profile")
        self.assertEqual("COMPLETED", result["status"], result)

    def test_selector_recovery_snapshot_reopens_failed_url_parameter_page_transiently(self):
        recipe = ScrapeRecipeInput.from_record(self.recipe).model_copy(
            update={"browser_profile_id": "account-one"})
        record = recipe.to_record("recipe-recovery-preview")
        self.db.save_web_recipe(record)
        validated = self.db.set_web_recipe_status("recipe-recovery-preview", "VALIDATED", {
            "record_count": 1, "field_match_counts": {"name": 1, "group": 1}})
        run = self.db.create_web_scrape_run("run-recovery-preview", validated)
        self.db.update_web_scrape_run(run["id"], "RUNNING")
        self.db.set_web_scrape_recovery(run["id"], {
            "page_number": 2, "reason": "NO_ROWS", "original_selector": "tbody > tr",
            "unmatched_fields": [], "candidates": [],
        })
        browser = FakeBrowserSessions({})
        manager = ScrapeRunManager(self.db, browser)

        preview = manager.recovery_snapshot(run["id"])

        self.assertEqual("AVAILABLE", preview["status"])
        self.assertEqual(2, preview["page_number"])
        self.assertEqual("https://example.com/people", preview["url"])
        self.assertEqual("page-2", preview["clean_markdown"])
        self.assertFalse(preview["persisted"])
        self.assertEqual(["account-one"], browser.opened_profiles)
        self.assertEqual(["https://example.com/people?sort=name&page=2"], browser.visited)
        self.assertEqual(["run-session"], browser.closed)

    def test_selector_recovery_snapshot_replays_load_more_to_failed_page(self):
        base = ScrapeRecipeInput.from_record(self.recipe)
        recipe = base.model_copy(update={
            "navigation": RecipeNavigation(type="load_more", selector="button.more"),
            "max_pages": 3,
        })
        self.db.save_web_recipe(recipe.to_record("recipe-load-more-recovery"))
        validated = self.db.set_web_recipe_status("recipe-load-more-recovery", "VALIDATED", {
            "record_count": 1, "field_match_counts": {"name": 1, "group": 1}})
        run = self.db.create_web_scrape_run("run-load-more-recovery", validated)
        self.db.update_web_scrape_run(run["id"], "RUNNING")
        self.db.set_web_scrape_recovery(run["id"], {
            "page_number": 3, "reason": "NO_ROWS", "original_selector": "tbody > tr",
            "unmatched_fields": [], "candidates": [],
        })
        browser = FakeBrowserSessions({})
        manager = ScrapeRunManager(self.db, browser)

        preview = manager.recovery_snapshot(run["id"])

        self.assertEqual("AVAILABLE", preview["status"])
        self.assertEqual(3, preview["page_number"])
        self.assertEqual("page-3", preview["clean_markdown"])
        self.assertEqual([("load_more", "button.more")] * 2, browser.actions)
        self.assertEqual(["run-session"], browser.closed)

    def test_selector_recovery_snapshot_uses_checkpointed_same_origin_next_url_transiently(self):
        base = ScrapeRecipeInput.from_record(self.recipe)
        recipe = base.model_copy(update={
            "navigation": RecipeNavigation(type="next", selector="a.next"),
            "max_pages": 2,
        })
        self.db.save_web_recipe(recipe.to_record("recipe-next-recovery"))
        validated = self.db.set_web_recipe_status("recipe-next-recovery", "VALIDATED", {
            "record_count": 1, "field_match_counts": {"name": 1, "group": 1}})
        run = self.db.create_web_scrape_run("run-next-recovery", validated)
        self.db.update_web_scrape_run(run["id"], "RUNNING")
        checkpointed_next_url = "https://example.com/people?page=2&view=compact#results"
        self.db.checkpoint_web_scrape_page(run["id"], 1, [], "page-one-hash", checkpointed_next_url)
        self.db.set_web_scrape_recovery(run["id"], {
            "page_number": 2, "reason": "NO_ROWS", "original_selector": "tbody > tr",
            "unmatched_fields": [], "candidates": [],
        })
        browser = FakeBrowserSessions({})
        manager = ScrapeRunManager(self.db, browser)

        preview = manager.recovery_snapshot(run["id"])

        self.assertEqual("https://example.com/people", preview["url"])
        self.assertEqual([checkpointed_next_url], browser.visited)
        self.assertNotIn("view=compact", preview["url"])
        self.assertNotIn("results", preview["url"])
        self.assertFalse(preview["persisted"])

    def test_network_json_recipe_uses_existing_preview_validation_run_and_checkpoint_lifecycle(self):
        record = ScrapeRecipeInput.model_validate({
            "name": "JSON directory", "start_url": "https://example.com/directory",
            "record_selector": "body", "fields": {}, "source": "network_json",
            "network_json": {"endpoint_path": "/api/items", "records_pointer": "/items",
                "fields": {"name": {"pointer": "/name"}, "id": {"pointer": "/id"}}},
            "navigation": {"type": "none"}, "max_records": 5,
            "deduplicate_keys": ["id"], "output_format": "jsonl",
        }).to_record("json-recipe")
        self.db.save_web_recipe(record)
        validated = self.db.set_web_recipe_status("json-recipe", "VALIDATED", {"record_count": 2})

        class JsonBrowser(FakeBrowserSessions):
            def __init__(self):
                super().__init__({1: []})
                self.json_sources = []
            def extract_network_json_recipe(self, session_id, source, *, limit=100):
                self.json_sources.append(source)
                rows = [{"name":"Ada", "id":1}, {"name":"Lin", "id":2}][:limit]
                return {"status":"AVAILABLE", "rows":rows, "record_count":2,
                        "field_match_counts":{"name":2,"id":2}, "truncated":False}

        browser = JsonBrowser()
        run = self.db.create_web_scrape_run("json-run", validated)
        manager = ScrapeRunManager(self.db, browser)
        manager.execute(run["id"])
        completed = self.db.web_scrape_run("json-run")
        self.assertEqual("COMPLETED", completed["status"])
        self.assertEqual(2, completed["record_count"])
        self.assertEqual((1, 2, 0, 1), (
            completed["metrics"]["pages_visited"], completed["metrics"]["records_discovered"],
            completed["metrics"]["dom_extractions"], completed["metrics"]["network_extractions"],
        ))
        self.assertEqual(["Ada", "Lin"], [row["name"] for row in self.db.stream_web_scrape_rows("json-run")])
        self.assertEqual("/api/items", browser.json_sources[0]["endpoint_path"])
        self.assertEqual("jsonl", completed["recipe"]["output_format"])

    def test_runtime_selector_recovery_requires_revalidation_and_retries_failed_page(self):
        class RecoveryBrowser(FakeBrowserSessions):
            def extract_recipe(self, session_id, record_selector, fields, *, limit=100):
                if record_selector == "tbody > tr":
                    return {"status": "AVAILABLE", "rows": [], "record_count": 0,
                            "field_match_counts": {name: 0 for name in fields}}
                return super().extract_recipe(session_id, record_selector, fields, limit=limit)

            def picker_evidence(self, session_id, kind):
                return {"status": "AVAILABLE", "candidates": [{
                    "kind": "row", "tag": "tr", "selector_candidates": ["table.people tr"],
                    "dom_path": "table.people > tbody > tr:nth-of-type(1)",
                }]}

            def probe_recipe_selectors(self, session_id, candidates, fields):
                return {"status": "AVAILABLE", "results": [{
                    "candidate_id": item["candidate_id"], "selector": item["selector"],
                    "record_count": 2, "field_match_counts": {name: 2 for name in fields},
                } for item in candidates]}

        browser = RecoveryBrowser({
            1: [{"name": "Acme", "group": "Research"}],
            2: [{"name": "Beta", "group": "Design"}],
        })
        run = self.db.create_web_scrape_run("run-selector-recovery", self.recipe)
        manager = ScrapeRunManager(self.db, browser)
        manager.execute(run["id"])

        failed = self.db.web_scrape_run(run["id"])
        self.assertEqual(("FAILED", "SELECTOR_RECOVERY"), (failed["status"], failed["stop_reason"]))
        self.assertEqual(1, failed["metrics"]["selector_recoveries"])
        self.assertEqual((1, 1, 0), (
            failed["metrics"]["pages_visited"], failed["metrics"]["dom_extractions"],
            failed["metrics"]["records_discovered"],
        ))
        self.assertEqual(0, failed["pages_completed"])
        self.assertEqual(1, failed["recovery"]["page_number"])
        self.assertEqual("tbody > tr", failed["recovery"]["original_selector"])
        self.assertEqual("table.people tr", failed["recovery"]["candidates"][0]["selector"])
        with self.assertRaisesRegex(ValueError, "RECIPE_RECOVERY_REVALIDATION_REQUIRED"):
            manager.start(run["id"])

        draft = ScrapeRecipeInput.model_validate({
            "name": self.recipe["name"], "start_url": self.recipe["start"]["url"],
            "record_selector": "table.people tr", "fields": self.recipe["fields"],
            "navigation": self.recipe["navigation"], "deduplicate_keys": self.recipe["deduplicate_keys"],
            "max_pages": self.recipe["max_pages"], "max_records": self.recipe["max_records"],
        }).to_record("recipe-1")
        self.db.save_web_recipe(draft)
        with self.assertRaisesRegex(ValueError, "RECIPE_RECOVERY_REVALIDATION_REQUIRED"):
            manager.start(run["id"])
        self.db.set_web_recipe_status(
            "recipe-1", "VALIDATED", {"record_count": 1, "field_match_counts": {"name": 1, "group": 1}}
        )
        manager.start(run["id"])
        deadline = time.time() + 2
        while time.time() < deadline:
            current = self.db.web_scrape_run(run["id"])
            if current["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(0.01)
        completed = self.db.web_scrape_run(run["id"])
        self.assertEqual("COMPLETED", completed["status"])
        self.assertEqual((2, 2), (completed["page_cursor"], completed["record_count"]))
        self.assertEqual({}, completed["recovery"])
        self.assertEqual(["Acme", "Beta"], [row["name"] for row in self.db.stream_web_scrape_rows(run["id"])])
        self.assertEqual(["1", "1", "2"], [parse_qs(urlsplit(url).query)["page"][0] for url in browser.visited])

    def test_wait_strategy_short_delay_is_applied_before_extraction(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name": "Short wait", "start_url": "https://example.com/people",
            "record_selector": "article.person", "fields": {"name": {"selector": "h2"}},
            "wait_strategy": "short_delay", "max_pages": 1,
        }).to_record("recipe-short-wait")
        self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status("recipe-short-wait", "VALIDATED", {"record_count": 1})
        browser = FakeBrowserSessions({1: [{"name": "Acme"}]})
        run = self.db.create_web_scrape_run("run-short-wait", validated)
        manager = ScrapeRunManager(self.db, browser)
        started = time.monotonic()
        manager.execute(run["id"])
        elapsed = time.monotonic() - started
        self.assertEqual("COMPLETED", self.db.web_scrape_run(run["id"])["status"])
        self.assertGreaterEqual(elapsed, 0.20)
        self.assertEqual(["navigate", "extract"], [operation[0] for operation in browser.operations])

    def test_wait_strategy_page_load_uses_document_readiness(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name": "Page load wait", "start_url": "https://example.com/people",
            "record_selector": "article.person", "fields": {"name": {"selector": "h2"}},
            "wait_strategy": "page_load", "max_pages": 1,
        }).to_record("recipe-page-load-wait")
        self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status("recipe-page-load-wait", "VALIDATED", {"record_count": 1})
        browser = FakeBrowserSessions({1: [{"name": "Acme"}]})
        run = self.db.create_web_scrape_run("run-page-load-wait", validated)
        ScrapeRunManager(self.db, browser).execute(run["id"])
        self.assertEqual("COMPLETED", self.db.web_scrape_run(run["id"])["status"])
        self.assertEqual(1, browser.page_load_waits)

    def test_dom_stability_waits_for_two_matching_snapshots(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name": "Stable wait", "start_url": "https://example.com/people",
            "record_selector": "article.person", "fields": {"name": {"selector": "h2"}},
            "wait_strategy": "dom_stable", "max_pages": 1,
        }).to_record("recipe-stable-wait")
        self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status("recipe-stable-wait", "VALIDATED", {"record_count": 1})
        browser = FakeBrowserSessions({1: [{"name": "Acme"}]})
        browser.snapshot_states = ["loading", "loaded", "loaded"]
        run = self.db.create_web_scrape_run("run-stable-wait", validated)
        manager = ScrapeRunManager(self.db, browser)
        manager.execute(run["id"])
        self.assertEqual("COMPLETED", self.db.web_scrape_run(run["id"])["status"])
        self.assertEqual(3, browser.snapshots)
        self.assertTrue(browser.extraction_started_after_snapshot)

    def test_no_new_deduplicated_rows_stops_with_checkpoint(self):
        browser = FakeBrowserSessions({
            1: [{"name": "Acme", "group": "Research"}],
            2: [{"name": "Acme", "group": "Updated"}],
        })
        self.db.create_web_scrape_run("run-2", self.recipe)
        manager = ScrapeRunManager(self.db, browser)
        manager.execute("run-2")
        result = self.db.web_scrape_run("run-2")
        self.assertEqual(("COMPLETED", "NO_NEW_RECORDS"), (result["status"], result["stop_reason"]))
        self.assertEqual((2, 1), (result["page_cursor"], result["record_count"]))

    def test_same_origin_next_links_are_followed_and_checkpointed(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name": "Next pages", "start_url": "https://example.com/people",
            "record_selector": "article.person",
            "fields": {"name": {"selector": "h2", "extract": "text"}},
            "navigation": {"type": "next", "selector": "a.next"},
            "deduplicate_keys": ["name"], "max_pages": 3,
        }).to_record("recipe-next")
        self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status("recipe-next", "VALIDATED", {"record_count": 1, "field_match_counts": {"name": 1}})
        page_one = "https://example.com/people"
        page_two = "https://example.com/people?page=2"
        browser = FakeBrowserSessions(
            {1: [{"name": "Acme"}], 2: [{"name": "Beta"}]},
            {page_one: page_two, page_two: None},
        )
        run = self.db.create_web_scrape_run("run-next", validated)
        manager = ScrapeRunManager(self.db, browser)
        manager.execute(run["id"])
        result = self.db.web_scrape_run("run-next")
        self.assertEqual(("COMPLETED", "NO_NEXT_LINK"), (result["status"], result["stop_reason"]))
        self.assertEqual([page_one, page_two], browser.visited)
        self.assertEqual(["Acme", "Beta"], [row["name"] for row in self.db.stream_web_scrape_rows("run-next")])

    def test_load_more_and_infinite_scroll_advance_with_page_and_record_caps(self):
        for navigation in ({"type":"load_more","selector":"button.more"}, {"type":"infinite_scroll"}):
            recipe = ScrapeRecipeInput.model_validate({
                "name":"Dynamic pages", "start_url":"https://example.com/people", "record_selector":"article.person",
                "fields":{"name":{"selector":"h2"}}, "navigation":navigation,
                "deduplicate_keys":["name"], "max_pages":2, "max_records":10,
            }).to_record("recipe-" + navigation["type"])
            validated = self.db.save_web_recipe(recipe)
            validated = self.db.set_web_recipe_status(validated["id"], "VALIDATED", {"record_count":1,"field_match_counts":{"name":1}})
            browser = FakeBrowserSessions({1:[{"name":"Acme"}],2:[{"name":"Beta"}]})
            run_id = "run-" + navigation["type"]
            self.db.create_web_scrape_run(run_id, validated)
            ScrapeRunManager(self.db, browser).execute(run_id)
            result = self.db.web_scrape_run(run_id)
            with self.subTest(navigation=navigation):
                self.assertEqual(("COMPLETED","MAX_PAGES"),(result["status"],result["stop_reason"]))
                self.assertEqual(2,result["pages_completed"])
                self.assertEqual(["Acme","Beta"],[row["name"] for row in self.db.stream_web_scrape_rows(run_id)])
                self.assertEqual(1,len(browser.actions))

    def test_interrupted_load_more_run_replays_bounded_session_state(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name":"Recover dynamic pages", "start_url":"https://example.com/people", "record_selector":"article.person",
            "fields":{"name":{"selector":"h2"}}, "navigation":{"type":"load_more","selector":"button.more"},
            "deduplicate_keys":["name"], "max_pages":3,
        }).to_record("recipe-load-more-recovery")
        validated = self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status(validated["id"], "VALIDATED", {"record_count":1,"field_match_counts":{"name":1}})
        browser = FakeBrowserSessions({1:[{"name":"Acme"}],2:[{"name":"Beta"}],3:[{"name":"Gamma"}]})
        run_id = "run-load-more-recovery"
        self.db.create_web_scrape_run(run_id, validated)
        self.db.update_web_scrape_run(run_id, "RUNNING")
        self.db.checkpoint_web_scrape_page(run_id, 1, [("acme", {"name":"Acme"})], "page-1")
        self.db.update_web_scrape_run(run_id, "INTERRUPTED", error="PROCESS_RESTARTED")
        ScrapeRunManager(self.db, browser).execute(run_id)
        result = self.db.web_scrape_run(run_id)
        self.assertEqual(("COMPLETED","MAX_PAGES"),(result["status"],result["stop_reason"]))
        self.assertEqual(["Acme","Beta","Gamma"],[row["name"] for row in self.db.stream_web_scrape_rows(run_id)])
        self.assertEqual([("load_more","button.more"),("load_more","button.more")],browser.actions)

    def test_detail_pages_merge_same_origin_fields_and_return_to_list(self):
        detail_url = "https://example.com/person/1"
        recipe = ScrapeRecipeInput.model_validate({
            "name":"Detail pages", "start_url":"https://example.com/people", "record_selector":"article.person",
            "fields":{"name":{"selector":"h2"},"profile":{"selector":"a.profile","extract":"href"}},
            "detail":{"link_field":"profile","fields":{"city":{"selector":".city"}}},
            "navigation":{"type":"none"}, "deduplicate_keys":["profile"], "max_pages":1,
        }).to_record("recipe-details")
        validated = self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status(validated["id"], "VALIDATED", {"record_count":1,"field_match_counts":{"name":1,"profile":1}})
        browser = FakeBrowserSessions({1:[{"name":"Acme","profile":detail_url}]})
        browser.detail_data[detail_url] = {"city":"Tokyo"}
        self.db.create_web_scrape_run("run-details", validated)
        ScrapeRunManager(self.db, browser).execute("run-details")
        result = self.db.web_scrape_run("run-details")
        self.assertEqual(("COMPLETED","MAX_PAGES"),(result["status"],result["stop_reason"]))
        self.assertEqual([{"name":"Acme","profile":detail_url,"city":"Tokyo"}],list(self.db.stream_web_scrape_rows("run-details")))
        self.assertEqual("https://example.com/people",browser.current_url)
        self.assertEqual(4, browser.snapshots)  # stable list page and detail page
        self.assertEqual((1, 1, 1, 1), (
            result["metrics"]["pages_visited"], result["metrics"]["dom_extractions"],
            result["metrics"]["detail_pages_visited"], result["metrics"]["detail_extractions"],
        ))

    def test_detail_page_rejects_cross_origin_link_before_navigation(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name":"Reject foreign detail", "start_url":"https://example.com/people", "record_selector":"article.person",
            "fields":{"name":{"selector":"h2"},"profile":{"selector":"a.profile","extract":"href"}},
            "detail":{"link_field":"profile","fields":{"city":{"selector":".city"}}},
            "navigation":{"type":"none"}, "max_pages":1,
        }).to_record("recipe-foreign-details")
        validated = self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status(validated["id"], "VALIDATED", {"record_count":1,"field_match_counts":{"name":1,"profile":1}})
        browser = FakeBrowserSessions({1:[{"name":"Acme","profile":"https://attacker.example/person/1"}]})
        self.db.create_web_scrape_run("run-foreign-details", validated)
        ScrapeRunManager(self.db, browser).execute("run-foreign-details")
        result = self.db.web_scrape_run("run-foreign-details")
        self.assertEqual(("FAILED","DETAIL_LINK_ORIGIN_CHANGED"),(result["status"],result["error"]))
        self.assertEqual(["https://example.com/people"],browser.visited)

    def test_next_link_cannot_change_origin(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name": "Next pages", "start_url": "https://example.com/people",
            "record_selector": "article.person", "fields": {"name": {"selector": "h2"}},
            "navigation": {"type": "next", "selector": "a.next"}, "max_pages": 2,
        }).to_record("recipe-other-origin")
        self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status("recipe-other-origin", "VALIDATED", {"record_count": 1, "field_match_counts": {"name": 1}})
        browser = FakeBrowserSessions({1: [{"name": "Acme"}]}, {"https://example.com/people": "https://attacker.example/next"})
        self.db.create_web_scrape_run("run-other-origin", validated)
        ScrapeRunManager(self.db, browser).execute("run-other-origin")
        result = self.db.web_scrape_run("run-other-origin")
        self.assertEqual(("FAILED", "NEXT_LINK_ORIGIN_CHANGED"), (result["status"], result["error"]))
        self.assertEqual(["https://example.com/people"], browser.visited)

    def test_only_validated_recipes_can_start_runs(self):
        self.db.set_web_recipe_status("recipe-1", "DRAFT")
        manager = ScrapeRunManager(self.db, FakeBrowserSessions({}))
        with self.assertRaisesRegex(ValueError, "RECIPE_NOT_VALIDATED"):
            manager.create("recipe-1")

    def test_chat_run_reuses_its_attached_browser_session(self):
        class AttachedBrowser(FakeBrowserSessions):
            def snapshot(self, session_id):
                self.snapshots += 1
                return {"status": "AVAILABLE", "url": self.current_url or
                        "https://example.com/people?sort=name", "clean_markdown": "Directory"}

        browser = AttachedBrowser({1: [{"name": "Acme", "group": "Research"}]})
        manager = ScrapeRunManager(self.db, browser)
        run = manager.create("recipe-1", session_id="chat-session")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = self.db.web_scrape_run(run["id"])
            if run["status"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.01)
        self.assertEqual("COMPLETED", run["status"])
        self.assertEqual([], browser.opened_profiles)
        self.assertEqual([], browser.closed)
        self.assertEqual("AVAILABLE", browser.snapshot("chat-session")["status"])
        self.assertEqual(2, len(browser.visited))
        self.assertEqual(["1", "2"], [parse_qs(urlsplit(url).query)["page"][0]
                                     for url in browser.visited])

    def test_pause_checkpoints_current_page_and_resume_continues_from_next_page(self):
        self.db.create_web_scrape_run("run-pause", self.recipe)
        manager = ScrapeRunManager(self.db, FakeBrowserSessions({}))
        delegate = FakeBrowserSessions({
            1: [{"name": "Acme", "group": "Research"}],
            2: [{"name": "Beta", "group": "Design"}],
        })
        class PauseAfterFirstPage:
            def open(self): return delegate.open()
            def navigate(self, session_id, url):
                result = delegate.navigate(session_id, url)
                if "page=1" in url: manager.pause("run-pause")
                return result
            def snapshot(self, session_id): return delegate.snapshot(session_id)
            def extract_recipe(self, *args, **kwargs): return delegate.extract_recipe(*args, **kwargs)
            def close(self, session_id): return delegate.close(session_id)
        manager.browser_sessions = PauseAfterFirstPage()
        manager.execute("run-pause")
        paused = self.db.web_scrape_run("run-pause")
        self.assertEqual(("PAUSED", 1, 1), (paused["status"], paused["page_cursor"], paused["record_count"]))
        manager.browser_sessions = delegate
        manager.start("run-pause")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.db.web_scrape_run("run-pause")
            if result["status"] in {"COMPLETED", "FAILED"}: break
            time.sleep(0.01)
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(["1", "2"], [parse_qs(urlsplit(url).query)["page"][0] for url in delegate.visited])
        self.assertEqual(["Acme", "Beta"], [row["name"] for row in self.db.stream_web_scrape_rows("run-pause")])

    def test_stop_request_commits_the_current_page_then_stops(self):
        self.db.create_web_scrape_run("run-stop", self.recipe)
        manager = ScrapeRunManager(self.db, FakeBrowserSessions({}))
        delegate = FakeBrowserSessions({1: [{"name": "Acme", "group": "Research"}]})
        class StopAfterFirstNavigation:
            def open(self): return delegate.open()
            def navigate(self, session_id, url):
                result = delegate.navigate(session_id, url)
                manager.stop("run-stop")
                return result
            def snapshot(self, session_id): return delegate.snapshot(session_id)
            def extract_recipe(self, *args, **kwargs): return delegate.extract_recipe(*args, **kwargs)
            def close(self, session_id): return delegate.close(session_id)
        manager.browser_sessions = StopAfterFirstNavigation()
        manager.start("run-stop")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.db.web_scrape_run("run-stop")
            if result["status"] in {"STOPPED", "FAILED", "COMPLETED"}: break
            time.sleep(0.01)
        self.assertEqual(("STOPPED", "USER_STOP", 1, 1),
                         (result["status"], result["stop_reason"], result["page_cursor"], result["record_count"]))

    def test_next_link_checkpoint_resumes_without_revisiting_completed_page(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name": "Resumable next pages", "start_url": "https://example.com/people",
            "record_selector": "article.person", "fields": {"name": {"selector": "h2"}},
            "navigation": {"type": "next", "selector": "a.next"},
            "deduplicate_keys": ["name"], "max_pages": 3,
        }).to_record("recipe-next-resume")
        self.db.save_web_recipe(recipe)
        validated = self.db.set_web_recipe_status("recipe-next-resume", "VALIDATED", {"record_count": 1, "field_match_counts": {"name": 1}})
        page_one, page_two = "https://example.com/people", "https://example.com/people?page=2"
        delegate = FakeBrowserSessions({1: [{"name": "Acme"}], 2: [{"name": "Beta"}]}, {page_one: page_two})
        manager = ScrapeRunManager(self.db, delegate)
        self.db.create_web_scrape_run("run-next-resume", validated)

        class PauseOnFirstPage:
            def open(self): return delegate.open()
            def navigate(self, session_id, url):
                result = delegate.navigate(session_id, url)
                if url == page_one: manager.pause("run-next-resume")
                return result
            def snapshot(self, session_id): return delegate.snapshot(session_id)
            def extract_recipe(self, *args, **kwargs): return delegate.extract_recipe(*args, **kwargs)
            def extract_next_link(self, *args, **kwargs): return delegate.extract_next_link(*args, **kwargs)
            def close(self, session_id): return delegate.close(session_id)

        manager.browser_sessions = PauseOnFirstPage()
        manager.execute("run-next-resume")
        paused = self.db.web_scrape_run("run-next-resume")
        self.assertEqual(("PAUSED", 1, page_two), (paused["status"], paused["page_cursor"], paused["next_url"]))
        manager.browser_sessions = delegate
        manager.start("run-next-resume")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.db.web_scrape_run("run-next-resume")
            if result["status"] in {"COMPLETED", "FAILED"}: break
            time.sleep(0.01)
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual([page_one, page_two], delegate.visited)
        self.assertEqual(["Acme", "Beta"], [row["name"] for row in self.db.stream_web_scrape_rows("run-next-resume")])


if __name__ == "__main__":
    unittest.main()
