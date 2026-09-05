import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.retrieval import RetrievalRouter, FileRetriever, FTSRetriever, DisabledVectorStore
from olcr_api.runtime import Runtime

from olcr_api.web import _SearchParser, _classify_search_html, brave_search, tavily_search, validate_url


class WebSafetyTests(unittest.TestCase):
    def test_brain_urls_are_suppressed_but_body_remains(self):
        body, detected=Runtime._suppress_brain_urls("Answer. https://updatify.com/ollama**\n[Source](https://fabricated.example/)")
        self.assertTrue(detected); self.assertNotIn("http", body); self.assertIn("Answer.", body)

    def test_freshness_reads_titles_and_compares_base_versions(self):
        sources=[{"title":"Ollama Latest Version (v0.33.2) + Version History","text":"older v0.33.1"}]
        self.assertEqual("CONFLICT", Runtime._freshness_check("v0.33.1-33151 is latest", sources)[0])

    def test_freshness_scopes_out_dependency_versions(self):
        sources=[{"title":"Ollama release v0.33.3","text":"Ollama v0.33.3 released. MLX-C updated to v0.5.0."}]
        self.assertEqual("PASS", Runtime._freshness_check_scoped("Ollama v0.33.3 is latest", sources, target="Ollama")[0])

    def test_numeric_semver_max_is_not_lexical(self):
        sources=[{"title":"Ollama v0.33.3","text":"Ollama release v0.33.3 and v0.5.0"}]
        self.assertEqual("PASS", Runtime._freshness_check_scoped("Ollama v0.33.3 is latest", sources, target="Ollama")[0])

    def test_target_extraction_for_japanese_request(self):
        self.assertEqual("Ollama", Runtime._freshness_target("今日時点のOllamaの最新リリースについてWebで確認"))

    def test_source_fragment_is_removed_with_url_suppression(self):
        body, detected=Runtime._suppress_brain_source_fragments("Answer\n- タイトル: Ollama Version History\n- URL:\n")
        self.assertTrue(detected); self.assertNotIn("タイトル", body); self.assertNotIn("URL:", body)

    def test_title_only_japanese_source_heading_is_removed(self):
        body, detected=Runtime._suppress_brain_source_fragments("Ollamaの最新リリースはv0.33.3です。\n参照したWebページのタイトル:\nOllama release notes - Product release notes & Product release notes")
        self.assertTrue(detected); self.assertIn("v0.33.3", body); self.assertNotIn("Ollama release notes", body)

    def test_rendered_sources_use_atomic_fetched_provenance(self):
        sources=[{"source_id":"web-1","title":"Example release notes","final_url":"https://example.com/releases","fetch_success":True}]
        rendered=Runtime._render_web_sources(sources)
        self.assertIn("Example release notes", rendered); self.assertIn("https://example.com/releases", rendered)
        self.assertNotIn("**", rendered)

    def test_unfetched_or_malformed_source_is_not_rendered(self):
        sources=[{"title":"A","final_url":"https://a.example/","fetch_success":False},{"title":"B","final_url":"https://b.example/**","fetch_success":True}]
        self.assertEqual("", Runtime._render_web_sources(sources))

    def test_freshness_prefers_stable_over_same_base_prerelease(self):
        sources=[{"text":"v0.33.3 Latest"},{"text":"v0.33.3-rc0 Release candidate"}]
        self.assertEqual("CONFLICT", Runtime._freshness_check("v0.33.3-rc0 is latest", sources)[0])
        self.assertEqual("PASS", Runtime._freshness_check("v0.33.3 is latest", sources)[0])

    def test_brave_missing_key_fails_without_request(self):
        with patch.dict("os.environ", {}, clear=True), patch("olcr_api.web.build_opener") as opener:
            with self.assertRaisesRegex(RuntimeError, "WEB_SEARCH_PROVIDER_NOT_READY"): brave_search("latest release")
            opener.assert_not_called()

    def test_tavily_uses_only_tavily_key(self):
        with patch.dict("os.environ", {"OLCR_WEB_SEARCH_API_KEY":"legacy"}, clear=True), patch("olcr_api.web.build_opener") as opener:
            with self.assertRaisesRegex(RuntimeError, "WEB_SEARCH_PROVIDER_NOT_READY"): tavily_search("latest release")
            opener.assert_not_called()

    def test_brave_response_is_bounded_structured_records(self):
        class Response:
            status=200
            def read(self, n): return b'{"web":{"results":[{"title":"A","url":"https://example.com","description":"x"}]}}'
            def __enter__(self): return self
            def __exit__(self,*a): pass
        with patch.dict("os.environ", {"OLCR_WEB_SEARCH_API_KEY":"secret"}), patch("olcr_api.web.build_opener") as opener:
            opener.return_value.open.return_value=Response()
            rows=brave_search("latest release")
        self.assertEqual("brave", rows[0]["provider"]); self.assertNotIn("secret", str(rows))
    def test_zero_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(str(Path(root)/"x.db")); db.initialize()
            settings=Settings(allowed_roots=(root,), db_path=str(Path(root)/"x.db"), main_model="mock", web_mode="auto").validated()
            router=RetrievalRouter(FileRetriever((root,)), FTSRetriever(db), DisabledVectorStore(), False, None, None)
            runtime=Runtime(settings, db, router, type("M", (), {"generate":lambda *a,**k:{"text":"https://example.com"}})())
            with patch("olcr_api.runtime.web_search", return_value=[]):
                _, answer=runtime.execute("今日の最新リリースをWebで確認してください")
            self.assertIn("確認できませんでした", answer)
            self.assertNotIn("example.com", answer)

    def test_public_http_url_requires_public_resolution(self):
        with patch("olcr_api.web.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
            self.assertEqual("https://example.com", validate_url("https://example.com"))

    def test_private_and_non_http_urls_are_blocked(self):
        with self.assertRaises(ValueError): validate_url("file:///tmp/x")
        with patch("olcr_api.web.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 0))]):
            with self.assertRaises(ValueError): validate_url("http://localhost/")

    def test_search_results_are_structured_candidates(self):
        parser = _SearchParser(); parser.feed('<a class="result__a" href="https://example.com">Official result</a>')
        self.assertEqual({"title", "url", "snippet", "rank"}, set(parser.results[0]))
        self.assertEqual("https://example.com", parser.results[0]["url"])

    def test_response_classification_distinguishes_interstitial_and_mismatch(self):
        self.assertEqual("INTERSTITIAL", _classify_search_html("<title>Challenge</title><p>captcha</p>", 0)[0])
        self.assertEqual("PARSER_MISMATCH", _classify_search_html("<title>Results</title><a href='https://example.com'>x</a>", 0)[0])
        self.assertEqual("RESULT_PAGE", _classify_search_html("<a class='result__a' href='https://example.com'>x</a>", 1)[0])


if __name__ == "__main__": unittest.main()
