import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from olcr_api.db import Database, SCHEMA_VERSION
from olcr_api.recipes import ScrapeRecipeInput, JsonRecipeSource, extract_json_recipe_rows


def sample_recipe(**overrides):
    value = {
        "name": "Public directory",
        "start_url": "https://example.com/directory?sort=name",
        "record_selector": "table.people > tbody > tr",
        "fields": {
            "name": {"selector": "td.name", "extract": "text"},
            "profile": {"selector": "a.profile", "extract": "href"},
        },
        "navigation": {"type": "url_parameter", "page_parameter": "page"},
        "deduplicate_keys": ["profile"],
        "max_pages": 25,
    }
    value.update(overrides)
    return value


class ScrapeRecipeValidationTests(unittest.TestCase):
    def test_valid_recipe_serializes_as_draft_without_auth_state(self):
        recipe = ScrapeRecipeInput.model_validate(sample_recipe()).to_record("recipe-1")
        self.assertEqual("DRAFT", recipe["status"])
        self.assertEqual("https://example.com/directory?sort=name", recipe["start"]["url"])
        self.assertEqual("href", recipe["fields"]["profile"]["extract"])
        self.assertNotIn("cookies", json.dumps(recipe).lower())
        self.assertNotIn("password", json.dumps(recipe).lower())

    def test_browser_profile_id_round_trips_without_auth_state(self):
        recipe = ScrapeRecipeInput.model_validate(sample_recipe(browser_profile_id="account-one"))
        record = recipe.to_record("recipe-profile")
        self.assertEqual("account-one", record["browser_profile_id"])
        self.assertEqual("account-one", ScrapeRecipeInput.from_record(record).browser_profile_id)
        self.assertNotIn("cookie", json.dumps(record).lower())
        for value in ("../escape", "Uppercase", "with space", "x" * 65):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ScrapeRecipeInput.model_validate(sample_recipe(browser_profile_id=value))

    def test_rejects_credentials_and_sensitive_query_parameters(self):
        for url in ("https://user:pass@example.com/list", "https://example.com/list?access_token=secret"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                ScrapeRecipeInput.model_validate(sample_recipe(start_url=url))

    def test_rejects_unbounded_or_non_css_selector_content(self):
        for selector in ("", "tr; alert(1)", "tr { color: red }", "a\nbody"):
            with self.subTest(selector=selector), self.assertRaises(ValidationError):
                ScrapeRecipeInput.model_validate(sample_recipe(record_selector=selector))

    def test_requires_consistent_navigation_and_deduplication_fields(self):
        cases = (
            {"navigation": {"type": "next"}},
            {"deduplicate_keys": ["unknown"]},
            {"fields": {"attr": {"selector": "a", "extract": "attribute"}}},
            {"fields": {"name;alert(1)": {"selector": "td", "extract": "text"}}},
        )
        for override in cases:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                ScrapeRecipeInput.model_validate(sample_recipe(**override))

    def test_detail_page_requires_an_existing_href_field_and_noncolliding_outputs(self):
        base = sample_recipe(navigation={"type":"none"}, deduplicate_keys=[])
        base["fields"] = {"profile": {"selector":"a.profile", "extract":"href"}}
        valid = ScrapeRecipeInput.model_validate({**base, "detail": {
            "link_field":"profile", "record_selector":"main", "fields":{"bio":{"selector":".bio"}},
        }})
        self.assertEqual("profile",valid.to_record("detail")["detail"]["link_field"])
        for detail in (
            {"link_field":"missing", "fields":{"bio":{"selector":".bio"}}},
            {"link_field":"profile", "fields":{"profile":{"selector":".bio"}}},
        ):
            with self.subTest(detail=detail), self.assertRaises(ValidationError):
                ScrapeRecipeInput.model_validate({**base, "detail":detail})

    def test_network_json_recipe_round_trips_only_path_and_explicit_pointers(self):
        recipe = ScrapeRecipeInput.model_validate({
            "name": "API directory", "start_url": "https://example.com/list",
            "record_selector": "body", "fields": {}, "source": "network_json",
            "network_json": {"endpoint_path": "/api/items", "records_pointer": "/data/items",
                "fields": {"name": {"pointer": "/display~1name"}, "id": {"pointer": "/id"}}},
        })
        record = recipe.to_record("json-1")
        restored = ScrapeRecipeInput.from_record(record)
        self.assertEqual("network_json", restored.source)
        self.assertEqual("/api/items", record["network_json"]["endpoint_path"])
        self.assertEqual("/display~1name", record["network_json"]["fields"]["name"]["pointer"])
        self.assertNotIn("response_body", json.dumps(record))
        self.assertEqual("DRAFT", record["status"])

    def test_network_json_recipe_rejects_query_paths_dom_fields_and_bad_pointer_escapes(self):
        base = {"name": "API directory", "start_url": "https://example.com/list",
                "record_selector": "body", "source": "network_json",
                "network_json": {"endpoint_path": "/api/items", "records_pointer": "",
                    "fields": {"name": {"pointer": "/name"}}}}
        invalid = (
            {**base, "network_json": {**base["network_json"], "endpoint_path": "/api/items?token=x"}},
            {**base, "network_json": {**base["network_json"], "fields": {"name": {"pointer": "/~2bad"}}}},
            {**base, "fields": {"name": {"selector": "h1"}}},
            {**base, "detail": {"link_field": "name", "fields": {"bio": {"selector": ".bio"}}}},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ScrapeRecipeInput.model_validate(payload)

    def test_network_json_pointer_projection_handles_escaped_keys_missing_and_null(self):
        source = JsonRecipeSource.model_validate({
            "endpoint_path": "/api/items", "records_pointer": "/data/items",
            "fields": {"name": {"pointer": "/display~1name"},
                       "nullable": {"pointer": "/nullable"},
                       "missing": {"pointer": "/absent"}},
        })
        evidence = extract_json_recipe_rows({"data": {"items": [
            {"display/name": "Ada", "nullable": None},
            {"display/name": "Lin"},
        ]}}, source, limit=1)
        self.assertEqual([{"name": "Ada", "nullable": None, "missing": None}], evidence["rows"])
        self.assertEqual(2, evidence["record_count"])
        self.assertTrue(evidence["truncated"])
        self.assertEqual({"name": 1, "nullable": 1, "missing": 0}, evidence["field_match_counts"])


class ScrapeRecipePersistenceTests(unittest.TestCase):
    def test_schema_v25_migrates_and_recipe_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "recipes.db")
            with sqlite3.connect(path) as raw:
                raw.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                raw.execute("INSERT INTO schema_version VALUES (25)")
            db = Database(path)
            db.initialize()
            record = ScrapeRecipeInput.model_validate(sample_recipe()).to_record("recipe-1")
            saved = db.save_web_recipe(record)
            self.assertEqual("recipe-1", saved["id"])
            self.assertEqual("DRAFT", db.web_recipe("recipe-1")["status"])
            self.assertEqual(["recipe-1"], [item["id"] for item in db.web_recipes()])
            updated = db.save_web_recipe({**record, "name": "Updated directory"})
            self.assertEqual("Updated directory", updated["name"])
            self.assertTrue(db.delete_web_recipe("recipe-1"))
            self.assertFalse(db.delete_web_recipe("recipe-1"))
            with db.connect() as migrated:
                self.assertEqual(SCHEMA_VERSION, migrated.execute("SELECT version FROM schema_version").fetchone()[0])
                self.assertIsNotNone(migrated.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_recipes'").fetchone())
                self.assertIsNotNone(migrated.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_world_states'").fetchone())
                self.assertIn("recovery_json", {row[1] for row in migrated.execute("PRAGMA table_info(web_scrape_runs)")})
                self.assertIn("metrics_json", {row[1] for row in migrated.execute("PRAGMA table_info(web_scrape_runs)")})


if __name__ == "__main__":
    unittest.main()
