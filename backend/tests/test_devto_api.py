import unittest
from unittest.mock import patch

import olcr_api.app as app
from olcr_api import external_tools as tools


class DevtoApiTests(unittest.TestCase):
    def test_english_cardinality_and_subject_survive_the_full_route_compile_chain(self):
        for prompt, expected_tool, expected_query in (
            ("Find three recent DEV.to articles about React state management. Show titles and source links.",
             "knowledge.devto", "React state management"),
            ("Find 3 Hacker News stories about Rust programming. Show source links.",
             "news.hackernews", "Rust programming"),
        ):
            with self.subTest(prompt=prompt):
                tool_id, arguments = tools.route(prompt)
                result = tools.compile_provider_arguments(tool_id, prompt, arguments)
                self.assertEqual(expected_tool, tool_id)
                self.assertEqual(expected_query, result["query"])
                self.assertEqual(3, result["limit"])

    def test_devto_is_a_production_public_search_provider(self):
        self.assertIn("knowledge.devto", tools.RUNTIME_REGISTRY)
        self.assertEqual("search_results", tools._RESPONSE_KIND_BY_TOOL["knowledge.devto"])

    def test_compiler_removes_prompt_boilerplate_and_preserves_limit(self):
        result = tools.compile_provider_arguments(
            "knowledge.devto", "Find 8 recent DEV Community articles about Python async patterns, show author and tags", {}
        )
        self.assertEqual(8, result["limit"])
        self.assertEqual("Python async patterns", result["query"])

    def test_public_search_maps_evidence_and_uses_forems_documented_endpoint(self):
        article = {
            "id": 42, "title": "Async Python", "path": "/author/async-python",
            "user": {"name": "Ada", "username": "ada"}, "published_at": "2026-09-01T00:00:00Z",
            "description": "Async Python guide",
            "tag_list": "python,async", "positive_reactions_count": 12, "comments_count": 3,
            "reading_time_minutes": 4,
        }
        with patch.object(tools, "_request", return_value=[article]) as request:
            result = tools.execute("knowledge.devto", {"query": "Python async", "limit": 7})
        host, path, params, headers = request.call_args_list[0].args
        self.assertEqual(("dev.to", "/api/articles/search"), (host, path))
        self.assertEqual({"q": "Python async", "per_page": 7}, params)
        self.assertEqual("application/vnd.forem.api-v1+json", headers["Accept"])
        self.assertEqual("public_unauthenticated", result["data"]["authentication"])
        self.assertEqual("Ada", result["data"]["items"][0]["author"])
        self.assertEqual(["python", "async"], result["data"]["items"][0]["tags"])
        self.assertEqual("https://dev.to/author/async-python", result["data"]["items"][0]["url"])

    def test_unrelated_http_200_feed_recovers_by_public_tag_and_excludes_wrong_topic(self):
        wrong = {"id": 1, "title": "AI estate", "tag_list": ["ai"], "url": "https://dev.to/u/wrong"}
        right = {"id": 2, "title": "React state management with Zustand", "tag_list": ["react"], "url": "https://dev.to/u/right"}
        with patch.object(tools, "_request", side_effect=[[wrong], [wrong, right]]) as request:
            result = tools.execute("knowledge.devto", {"query": "React state management", "limit": 3})
        self.assertEqual([2], [row["id"] for row in result["data"]["items"]])
        self.assertEqual("/api/articles", request.call_args.args[1])
        self.assertEqual("react", request.call_args.args[2]["tag"])

    def test_unrelated_search_results_fail_instead_of_becoming_false_evidence(self):
        with patch.object(tools, "_request", return_value=[{"id": 1, "title": "Unrelated news"}]):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_NO_RELEVANT_RESULT"):
                tools.execute("knowledge.devto", {"query": "React state management", "limit": 3})

    def test_empty_and_malformed_payloads_are_not_reported_as_success(self):
        with patch.object(tools, "_request", return_value=[]):
            result = tools.execute("knowledge.devto", {"query": "rare topic", "limit": 4})
        self.assertEqual("no_data", result["status"])
        with patch.object(tools, "_request", return_value={"articles": []}):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools.execute("knowledge.devto", {"query": "Python", "limit": 4})

    def test_summary_keeps_author_tags_metrics_and_source_link(self):
        result = {
            "tool_id": "knowledge.devto", "provider": "DEV Community (DEV.to)",
            "data": {"items": [{"title": "Async Python", "author": "Ada", "published_at": "2026-09-01",
                                   "tags": ["python", "async"], "description": "A useful guide",
                                   "positive_reactions_count": 12, "comments_count": 3,
                                   "url": "https://dev.to/ada/async-python"}]},
        }
        summary = app._new_provider_summary(result)
        for expected in ("Async Python", "Ada", "python, async", "reactions 12", "https://dev.to/ada/async-python"):
            self.assertIn(expected, summary)

    def test_analysis_candidates_follow_language_and_explicit_source(self):
        self.assertEqual(["knowledge.qiita"], app._analysis_candidates_for_domain(
            "最近のPython技術記事を分析して", []
        ))
        self.assertEqual(["knowledge.devto", "news.hackernews"], app._analysis_candidates_for_domain(
            "Analyze recent Python developer articles", []
        ))
        self.assertEqual(["news.hackernews"], app._analysis_candidates_for_domain(
            "Analyze Hacker News discussions about Python", []
        ))
        self.assertEqual(["knowledge.devto"], app._analysis_candidates_for_domain(
            "DEV.toでPythonの記事を分析して", []
        ))


if __name__ == "__main__":
    unittest.main()
