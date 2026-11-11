import unittest
from unittest.mock import MagicMock, patch

import olcr_api.app as app
from olcr_api import external_tools as tools


def _article(index=1):
    return {
        "id": f"article-{index}", "title": f"Python async guide {index}",
        "url": f"https://qiita.com/example/items/{index}",
        "user": {"id": "qiita-user", "name": "Qiita User"},
        "tags": [{"name": "Python"}, {"name": "asyncio"}],
        "created_at": "2026-01-01T00:00:00+09:00",
        "updated_at": "2026-01-02T00:00:00+09:00",
        "likes_count": 12, "comments_count": 2, "body": "A concise article body.",
    }


class QiitaApiTests(unittest.TestCase):
    def test_qiita_allows_bounded_multimegabyte_search_results_only_for_its_host(self):
        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.url = "https://qiita.com/api/v2/items"
        response.content = b" " * (2 * tools.MAX_BYTES)
        response.json.return_value = [_article()]
        with patch("olcr_api.external_tools.httpx.Client") as client_factory:
            client_factory.return_value.__enter__.return_value.get.return_value = response
            self.assertEqual([_article()], tools._request("qiita.com", "/api/v2/items", {"query": "Rust"}))
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools._request("example.com", "/items", {})

    def test_fuzzy_japanese_technical_article_request_extracts_rust_error_handling(self):
        prompt = "最近の日本語技術記事で、Rustのエラー処理について複数の記事を探し、よく使われる方法を教えて。"
        tool_id, arguments = tools.route(prompt)
        compiled = tools.compile_provider_arguments(tool_id, prompt, arguments)
        self.assertEqual("knowledge.qiita", tool_id)
        self.assertEqual("tag:rust エラー処理", compiled["query"])

    def test_explicit_and_providerless_programming_article_intents_route_to_qiita(self):
        cases = (
            ("QiitaでPythonの非同期処理の記事を5件探して", "tag:python 非同期処理", 5),
            ("Reactの実装記事を3件探して", "React", 3),
            ("Pythonの使い方の記事を5件探して", "Python", 5),
        )
        for prompt, expected_query, expected_limit in cases:
            with self.subTest(prompt=prompt):
                tool_id, arguments = tools.route(prompt)
                self.assertEqual("knowledge.qiita", tool_id)
                self.assertEqual(expected_query, arguments["query"])
                self.assertEqual(expected_limit, arguments["limit"])

    def test_english_community_articles_route_to_devto_while_hn_is_explicit(self):
        cases = (
            ("Find recent Python async articles and show the author", "knowledge.devto"),
            ("Search DEV Community posts about React state management", "knowledge.devto"),
            ("Show the latest Hacker News stories about Python", "news.hackernews"),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(expected, tools.route(prompt)[0])

    def test_acceptance_prompt_compiles_to_topic_not_display_constraints(self):
        prompt = "QiitaからPythonやReactの実装・使い方に関する技術記事を探して、タイトル、投稿者、タグ、更新日、本文の要点、記事リンクを示して"
        compiled = tools.compile_provider_arguments("knowledge.qiita", prompt, {})
        self.assertEqual("PythonやReactの実装・使い方に関する技術記事", compiled["query"])

    def test_public_search_maps_article_evidence_and_needs_no_token(self):
        with patch.object(tools, "_request", return_value=[_article()]) as request:
            result = tools.execute("knowledge.qiita", {"query": "Python async", "limit": 5})
        self.assertEqual("ok", result["status"])
        self.assertEqual("public_unauthenticated", result["data"]["authentication"])
        self.assertEqual("Qiita User", result["data"]["items"][0]["user"])
        self.assertEqual(["Python", "asyncio"], result["data"]["items"][0]["tags"])
        self.assertEqual("https://qiita.com/api/v2/docs", result["source"]["url"])
        host, path, params = request.call_args.args[:3]
        self.assertEqual(("qiita.com", "/api/v2/items"), (host, path))
        self.assertEqual({"query": "Python async", "page": 1, "per_page": 10}, params)

    def test_pagination_keeps_page_size_stable_and_respects_requested_limit(self):
        calls = []

        def response(host, path, params, headers=None):
            calls.append(dict(params))
            count = 100 if params["page"] == 1 else 10
            offset = (params["page"] - 1) * 100
            return [_article(offset + index) for index in range(count)]

        with patch.object(tools, "_request", side_effect=response):
            result = tools.execute("knowledge.qiita", {"query": "Python", "limit": 102})
        self.assertEqual([10, 10], [call["per_page"] for call in calls])
        self.assertEqual([1, 2], [call["page"] for call in calls])
        self.assertEqual(102, len(result["data"]["items"]))

    def test_empty_and_malformed_provider_payloads_are_distinguished(self):
        with patch.object(tools, "_request", return_value=[]):
            empty = tools.execute("knowledge.qiita", {"query": "no such topic", "limit": 3})
        self.assertEqual("no_data", empty["status"])
        self.assertEqual("EMPTY", empty["data"]["semantic_status"])
        with patch.object(tools, "_request", return_value={"items": []}):
            with self.assertRaisesRegex(tools.ExternalToolError, "PROVIDER_BAD_RESPONSE"):
                tools.execute("knowledge.qiita", {"query": "Python", "limit": 3})

    def test_analysis_summary_preserves_titles_authors_tags_excerpt_and_links(self):
        with patch.object(tools, "_request", return_value=[_article()]):
            result = tools.execute("knowledge.qiita", {"query": "Python", "limit": 1})
        summary = app._new_provider_summary(result)
        for expected in ("Python async guide", "Qiita User", "Python, asyncio", "本文抜粋", "https://qiita.com/example/items/1"):
            self.assertIn(expected, summary)

    def test_framework_topic_search_filters_irrelevant_body_mentions(self):
        relevant = _article()
        relevant.update({"id": "react-state", "title": "Reactの状態管理をRedux Toolkitで整理する",
                         "tags": [{"name": "React"}, {"name": "状態管理"}],
                         "body": "React applications can use Redux Toolkit for state management."})
        unrelated = _article(2)
        unrelated.update({"id": "aws-react-mention", "title": "CloudWatchでAWS Lambdaを監視する",
                          "tags": [{"name": "AWS"}, {"name": "CloudWatch"}],
                          "body": "Reactの状態管理にも少し触れます。"})
        broad_react = _article(3)
        broad_react.update({"id": "react-theme", "title": "Reactでテーマをカスタマイズする",
                            "tags": [{"name": "React"}, {"name": "WordPress"}],
                            "body": "A theme customization guide. It briefly mentions Redux state management."})
        with patch.object(tools, "_request", return_value=[unrelated, broad_react, relevant]):
            result = tools.execute("knowledge.qiita", {"query": "tag:react 状態管理", "limit": 3})
        self.assertEqual(["react-state"], [item["id"] for item in result["data"]["items"]])
        self.assertEqual(1, result["data"]["item_count"])

    def test_fuzzy_framework_topic_uses_one_large_candidate_page_before_filtering(self):
        irrelevant = _article(1)
        irrelevant.update({"title": "Reactテーマ設定", "tags": [{"name": "React"}],
                           "body": "Reactの状態管理にも触れます"})
        relevant = []
        for index in range(3):
            item = _article(index + 2)
            item.update({"title": f"React 状態管理ガイド {index}",
                         "tags": [{"name": "React"}, {"name": "状態管理"}]})
            relevant.append(item)
        payload = [irrelevant] * 97 + relevant
        with patch.object(tools, "_request", return_value=payload) as request:
            result = tools.execute("knowledge.qiita", {"query": "tag:react 状態管理", "limit": 3})
        self.assertEqual(1, request.call_count)
        self.assertEqual(10, request.call_args.args[2]["per_page"])
        self.assertEqual(3, result["data"]["item_count"])


if __name__ == "__main__":
    unittest.main()
