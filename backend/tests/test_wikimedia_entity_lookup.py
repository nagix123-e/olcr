"""Regression cases from the real Japanese Wikipedia API failure."""
import unittest
from unittest.mock import patch

from olcr_api import external_tools as tools


def page(title, extract="投手と打者の二刀流で知られる野球選手。"):
    return {"query": {"pages": {"1": {"pageid": 1, "title": title, "extract": extract}}}}


MISSING = {"query": {"pages": {"-1": {"title": "missing", "missing": ""}}}}


class WikimediaEntityLookupTests(unittest.TestCase):
    def test_exact_title_is_resolved_before_full_text_search(self):
        with patch.object(tools, "_request", return_value=page("大谷翔平")) as request:
            result = tools.wiki(**tools.normalize_wiki_arguments({"query": "大谷翔平について教えて"}))
        self.assertEqual("大谷翔平", result["data"]["title"])
        self.assertIn("二刀流", result["data"]["extract"])
        request.assert_called_once()
        self.assertEqual("大谷翔平", request.call_args.args[2]["titles"])
        self.assertEqual(1, request.call_args.args[2]["redirects"])
        self.assertEqual("DATA", result["data"]["semantic_status"])

    def test_redirect_uses_resolved_title_and_source_url(self):
        with patch.object(tools, "_request", return_value=page("侘び寂び")):
            result = tools.wiki("わびさび")
        self.assertEqual("侘び寂び", result["data"]["title"])
        self.assertEqual(result["data"]["page_url"], result["sources"][0]["canonical_url"])
        self.assertNotIn("わびさび", result["data"]["page_url"])

    def test_search_snippet_markup_does_not_split_entity_matching(self):
        search = {"query": {"search": [
            {"title": "無関係な記事", "snippet": "関係のない説明"},
            {"title": "日本の野球選手", "snippet": '<span class="searchmatch">大谷</span><span class="searchmatch">翔平</span>の紹介'},
        ]}}
        with patch.object(tools, "_request", side_effect=[MISSING, search, page("日本の野球選手")]):
            result = tools.wiki("大谷翔平")
        self.assertEqual("日本の野球選手", result["data"]["title"])

    def test_empty_extract_and_missing_page_are_not_successful_evidence(self):
        search = {"query": {"search": [{"title": "大谷翔平", "snippet": "大谷翔平"}]}}
        for unavailable in (MISSING, page("大谷翔平", "")):
            with self.subTest(unavailable=unavailable), patch.object(
                    tools, "_request", side_effect=[unavailable, search, unavailable]):
                with self.assertRaisesRegex(tools.ExternalToolError, "WIKIMEDIA_NOT_FOUND"):
                    tools.wiki("大谷翔平")

    def test_mediawiki_error_is_classified_as_bad_response(self):
        with patch.object(tools, "_request", return_value={"error": {"code": "maxlag"}}):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools.wiki("大谷翔平")


if __name__ == "__main__":
    unittest.main()
