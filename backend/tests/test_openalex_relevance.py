import unittest
from unittest.mock import patch

from olcr_api import external_tools


def work(identifier, title, **extra):
    return {
        "id": identifier,
        "title": title,
        "publication_year": extra.pop("publication_year", 2017),
        "authorships": [],
        "primary_location": {},
        "open_access": {},
        **extra,
    }


class OpenAlexRelevanceTests(unittest.TestCase):
    def test_title_request_compiler_preserves_only_title_and_marks_title_lookup(self):
        tool_id, arguments = external_tools.route("attention is all you need の論文情報を調べて")
        self.assertEqual("research.openalex", tool_id)
        self.assertEqual("attention is all you need", arguments["query"])
        self.assertTrue(arguments["title_like"])

    def test_title_query_selects_exact_match_from_ranked_pool(self):
        payload = {"results": [
            work("W1", "Unrelated ranking result"),
            work("W2", "A survey of transformer models"),
            work("W3", "Attention Is All You Need"),
        ]}
        with patch.object(external_tools, "_request", return_value=payload):
            result = external_tools.research("attention is all you need", title_like=True)
        data = result["data"]
        self.assertEqual("DATA", data["semantic_status"])
        self.assertEqual("Attention Is All You Need", data["selected_title"])
        self.assertEqual("EXACT", data["works"][0]["title_match"])
        self.assertEqual("TITLE_EXACT_MATCH", data["selection_reason"])

    def test_title_normalization_handles_case_punctuation_and_unicode(self):
        payload = {"results": [work("W1", "Attention—Is All You Need!")]}
        with patch.object(external_tools, "_request", return_value=payload):
            result = external_tools.research("ＡＴＴＥＮＴＩＯＮ is all you need", title_like=True)
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("EXACT", result["data"]["works"][0]["title_match"])

    def test_title_lookup_returns_no_relevant_result_for_unrelated_rows(self):
        payload = {"results": [work("W1", "A completely unrelated paper")]}
        with patch.object(external_tools, "_request", return_value=payload):
            result = external_tools.research("attention is all you need", title_like=True)
        self.assertEqual("NO_RELEVANT_RESULT", result["data"]["semantic_status"])
        self.assertEqual([], result["data"]["works"])

    def test_title_lookup_uses_bounded_fallback_when_first_search_has_no_match(self):
        responses = [
            {"results": [work("W1", "Unrelated result")]},
            {"results": [work("W2", "Attention Is All You Need")]},
        ]
        with patch.object(external_tools, "_request", side_effect=responses) as request:
            result = external_tools.research("attention is all you need", title_like=True)
        self.assertEqual("TITLE_NORMALIZED", result["data"]["query_mode"])
        self.assertEqual(2, request.call_count)
        self.assertEqual("W2", result["data"]["works"][0]["openalex_id"])

    def test_doi_lookup_prefers_provider_doi_evidence(self):
        doi = "https://doi.org/10.48550/arXiv.1706.03762"
        payload = {"id": "https://openalex.org/W2741809807", "title": "Attention Is All You Need", "doi": doi,
                   "authorships": [], "primary_location": {}, "open_access": {}}
        with patch.object(external_tools, "_request", return_value=payload):
            result = external_tools.research(doi=doi)
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("DOI", result["data"]["works"][0]["title_match"])
        self.assertEqual("DOI_MATCH", result["data"]["selection_reason"])

    def test_broad_topic_search_keeps_search_results(self):
        payload = {"results": [work("W1", "Paper", relevance_score=0.8)]}
        with patch.object(external_tools, "_request", return_value=payload):
            result = external_tools.research("RAG")
        self.assertEqual("DATA", result["data"]["semantic_status"])
        self.assertEqual("W1", result["data"]["works"][0]["openalex_id"])

    def test_malformed_provider_payload_is_empty_without_invented_work(self):
        with patch.object(external_tools, "_request", return_value={"unexpected": "shape"}):
            result = external_tools.research("attention is all you need", title_like=True)
        self.assertEqual("NO_RELEVANT_RESULT", result["data"]["semantic_status"])
        self.assertIsNone(result["data"]["selected_title"])


if __name__ == "__main__":
    unittest.main()
