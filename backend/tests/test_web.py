import unittest
import json
import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.retrieval import RetrievalRouter, FileRetriever, FTSRetriever, DisabledVectorStore
from olcr_api.runtime import Runtime

from olcr_api.web import (_SearchParser, _Text, _PinnedHTTPConnection, _PinnedHTTPSConnection,
                          _PinnedHTTPSHandler,
                          _classify_search_html, brave_search, tavily_search, validate_url,
                          requested_result_count, fetch, _safe_opener, _decode_response_text)


class WebSafetyTests(unittest.TestCase):
    def test_wikipedia_recovery_uses_subject_without_search_provider_hints(self):
        from olcr_api.models import TaskState
        with tempfile.TemporaryDirectory() as root:
            db = Database(str(Path(root) / "wiki.db")); db.initialize()
            settings = Settings(allowed_roots=(root,), db_path=db.path, main_model="mock",
                                web_mode="auto", web_provider="brave").validated()
            retrieval = RetrievalRouter(FileRetriever((root,)), FTSRetriever(db), DisabledVectorStore(), False)
            runtime = Runtime(settings, db, retrieval, type("M", (), {"generate": lambda *a, **k: {"text": ""}})())
            wiki = {"data": {"title": "大谷翔平", "extract": "大谷翔平は岩手県出身の野球選手です。",
                             "page_url": "https://ja.wikipedia.org/wiki/Ohtani"}}
            with patch("olcr_api.runtime.brave_search", return_value=[]), \
                 patch("olcr_api.runtime.provider_key", return_value="configured"), \
                 patch("olcr_api.external_tools.execute", return_value=wiki) as execute:
                task, answer = runtime.execute("大谷翔平について教えて", api_miss_fallback=True,
                    wikipedia_fallback_allowed=True, external_search_query="大谷翔平について教えて Wikimedia official source")
            self.assertEqual({"query": "大谷翔平", "language": "ja", "limit": 1}, execute.call_args.args[1])
            self.assertEqual(TaskState.COMPLETED, task.state)
            self.assertIn("岩手県", answer)
            self.assertNotIn("一般知識", answer)

    def test_retrieved_sources_survive_brain_failure_in_both_composition_paths(self):
        from olcr_api.models import TaskState
        from olcr_api.ollama import ModelFailure
        for recovery in (False, True):
            with self.subTest(api_miss_fallback=recovery), tempfile.TemporaryDirectory() as root:
                db = Database(str(Path(root) / "recovery.db")); db.initialize()
                settings = Settings(allowed_roots=(root,), db_path=db.path, main_model="mock",
                                    web_mode="auto", web_provider="brave").validated()
                retrieval = RetrievalRouter(FileRetriever((root,)), FTSRetriever(db), DisabledVectorStore(), False)
                model = type("Unavailable", (), {"generate": lambda *a, **k: None})()
                runtime = Runtime(settings, db, retrieval, model)
                source = {"url": "https://example.com/profile", "final_url": "https://example.com/profile",
                          "title": "Verified profile", "text": "Shohei Ohtani is a Japanese baseball player."}
                with patch("olcr_api.runtime.brave_search", return_value=[source]), \
                     patch("olcr_api.runtime.web_fetch", return_value=dict(source)), \
                     patch("olcr_api.runtime.provider_key", return_value="configured"), \
                     patch.object(runtime, "_generate_brain", side_effect=ModelFailure("timeout", "model timed out")):
                    task, answer = runtime.execute("大谷翔平について教えて", api_miss_fallback=recovery)
                self.assertEqual(TaskState.COMPLETED, task.state)
                self.assertIsNone(task.error)
                self.assertIn("Japanese baseball player", answer)
                self.assertIn("https://example.com/profile", answer)
                self.assertNotIn("一般知識", answer)
                self.assertTrue(any("composition_fallback" in item for item in task.selected_context))

    def test_readable_html_extracts_metadata_main_content_and_safe_links(self):
        parser = _Text("https://example.com/base")
        parser.feed("""<html><head><title> Project  Update </title><meta name="description" content="Summary"></head>
        <body><nav>Navigation noise</nav><main><h1>Release</h1><p>Body <a href="/docs">guide</a>
        <a href="javascript:alert(1)">unsafe</a></p><script>secret noise</script></main></body></html>""")
        result = parser.result()
        self.assertEqual("Project Update", result["title"])
        self.assertEqual("Summary", result["description"])
        self.assertIn("# Release", result["clean_markdown"])
        self.assertIn("[guide](https://example.com/docs)", result["clean_markdown"])
        self.assertNotIn("Navigation noise", result["clean_markdown"])
        self.assertNotIn("secret noise", result["clean_markdown"])
        self.assertNotIn("javascript:", result["clean_markdown"])

    def test_readable_html_omits_hidden_content_and_reads_social_description(self):
        parser = _Text("https://example.com/")
        parser.feed("""<html><head><meta property="og:description" content="Useful summary"></head>
        <body><main><p>Visible text</p><input hidden><img hidden><div hidden>Hidden attribute</div>
        <div aria-hidden="true"><p>Aria hidden parent <span>nested secret</span></p></div>
        <p style="display: none !important">Display hidden</p>
        <p style="color: red; visibility: collapse">Visibility hidden</p>
        <p>Still visible</p></main></body></html>""")
        result = parser.result()
        self.assertEqual("Useful summary", result["description"])
        self.assertIn("Visible text", result["clean_markdown"])
        self.assertIn("Still visible", result["clean_markdown"])
        for hidden in ("Hidden attribute", "Aria hidden parent", "nested secret", "Display hidden", "Visibility hidden"):
            self.assertNotIn(hidden, result["clean_markdown"])

        malformed = _Text("https://example.com/")
        malformed.feed("<meta property><div class><p aria-hidden>Still readable</p>")
        self.assertIn("Still readable", malformed.result()["clean_markdown"])

    def test_readable_html_fallback_omits_site_chrome_but_keeps_article_header(self):
        parser = _Text("https://example.com/")
        parser.feed("""<body><header>Site navigation</header><article>
        <header><h1>Article title</h1><p>By Ada</p></header>
        <p>Readable article body.</p><aside>Related links</aside></article>
        <footer>Copyright</footer></body>""")
        text = parser.result()["clean_markdown"]
        self.assertIn("# Article title", text)
        self.assertIn("By Ada", text)
        self.assertIn("Readable article body.", text)
        for chrome in ("Site navigation", "Related links", "Copyright"):
            self.assertNotIn(chrome, text)

    def test_readable_html_without_semantic_landmarks_omits_site_navigation_and_footer(self):
        parser = _Text("https://example.com/")
        parser.feed("""<html><body><header>Site navigation</header>
        <div class="site-nav">Home Search Account</div>
        <div class="content"><h1>Article</h1><p>Main information for the reader.</p></div>
        <footer>Legal links</footer></body></html>""")
        text = parser.result()["clean_markdown"]
        self.assertIn("Main information for the reader.", text)
        self.assertNotIn("Site navigation", text)
        self.assertNotIn("Home Search Account", text)
        self.assertNotIn("Legal links", text)

    def test_readable_html_keeps_title_as_metadata_not_body_content(self):
        parser = _Text("https://example.com/")
        parser.feed("""<html><head><title>Repeated page title</title></head>
        <body><div><h1>Article heading</h1><p>Article body.</p></div></body></html>""")
        result = parser.result()
        self.assertEqual("Repeated page title", result["title"])
        self.assertIn("Article body.", result["clean_markdown"])
        self.assertNotIn("Repeated page title", result["clean_markdown"])

    def test_fetch_keeps_legacy_text_and_returns_web_read_envelope(self):
        from email.message import Message
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        response = type("Response", (), {
            "status": 200, "headers": headers,
            "read": lambda self, limit: b"<title>Example</title><main><p>Hello OLCR</p></main>",
            "geturl": lambda self: "https://example.com/",
            "__enter__": lambda self: self, "__exit__": lambda self, *args: None,
        })()
        with patch("olcr_api.web.validate_url", side_effect=lambda url: url), patch("olcr_api.web.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = fetch("https://example.com/")
        self.assertEqual("Example", result["title"])
        self.assertEqual("Hello OLCR", result["text"].strip())
        self.assertEqual(result["text"], result["clean_markdown"])
        self.assertEqual("https://example.com/", result["final_url"])
        self.assertIn("timestamp", result)

    def test_fetch_decodes_gzip_and_deflate_responses(self):
        from email.message import Message
        import zlib

        document=b"<title>Encoded page</title><main><p>Readable compressed content</p></main>"
        for encoding, body in (("gzip", gzip.compress(document)),
                               ("deflate", zlib.compress(document))):
            with self.subTest(encoding=encoding):
                headers=Message()
                headers["Content-Type"]="text/html; charset=utf-8"
                headers["Content-Encoding"]=encoding
                response=type("Response", (), {
                    "status":200,"headers":headers,
                    "read":lambda self, limit, body=body:body,
                    "geturl":lambda self:"https://example.com/",
                    "__enter__":lambda self:self,"__exit__":lambda self,*args:None,
                })()
                with patch("olcr_api.web.validate_url",side_effect=lambda url:url), \
                     patch("olcr_api.web.build_opener") as opener:
                    opener.return_value.open.return_value=response
                    result=fetch("https://example.com/")
                self.assertEqual("Encoded page",result["title"])
                self.assertEqual("Readable compressed content",result["text"].strip())

    def test_web_text_decoder_handles_japanese_legacy_charsets_and_bad_utf8_headers(self):
        expected = "大谷翔平の最新情報"
        for encoding, content_type in (
            ("shift_jis", "text/html; charset=Shift_JIS"),
            ("euc_jp", "text/html; charset=EUC-JP"),
        ):
            with self.subTest(encoding=encoding):
                text, detected = _decode_response_text(expected.encode(encoding), content_type)
                self.assertEqual(expected, text)
                self.assertEqual(encoding, detected)

        html = f'<meta charset="Shift_JIS"><main>{expected}</main>'.encode("shift_jis")
        text, detected = _decode_response_text(html, "text/html")
        self.assertEqual("shift_jis", detected)
        self.assertIn(expected, text)

        # Some publishers incorrectly advertise UTF-8 while emitting Shift_JIS.
        text, detected = _decode_response_text(expected.encode("shift_jis"), "text/html; charset=utf-8")
        self.assertEqual(expected, text)
        self.assertEqual("shift_jis", detected)

    def test_fetch_rejects_unsupported_encoding_and_compression_bombs(self):
        from email.message import Message

        def response_for(encoding, body):
            headers=Message()
            headers["Content-Type"]="text/html; charset=utf-8"
            headers["Content-Encoding"]=encoding
            return type("Response", (), {
                "status":200,"headers":headers,
                "read":lambda self, limit:body,
                "geturl":lambda self:"https://example.com/",
                "__enter__":lambda self:self,"__exit__":lambda self,*args:None,
            })()

        with patch("olcr_api.web.validate_url",side_effect=lambda url:url), \
             patch("olcr_api.web.build_opener") as opener:
            opener.return_value.open.return_value=response_for("br",b"encoded")
            with self.assertRaisesRegex(ValueError,"UNSUPPORTED_CONTENT_ENCODING"):
                fetch("https://example.com/")

        bomb=gzip.compress(b"A"*1024)
        with patch("olcr_api.web.MAX_BYTES",64), \
             patch("olcr_api.web.validate_url",side_effect=lambda url:url), \
             patch("olcr_api.web.build_opener") as opener:
            opener.return_value.open.return_value=response_for("gzip",bomb)
            with self.assertRaisesRegex(ValueError,"RESPONSE_TOO_LARGE"):
                fetch("https://example.com/")
    def test_requested_result_count_is_a_default_not_a_ceiling(self):
        self.assertEqual(10,requested_result_count("Search latest news"))
        self.assertEqual(24,requested_result_count("最新ニュースを24件検索して"))
        self.assertEqual(40,requested_result_count("show top 40 results"))
    def test_negative_web_instruction_prevents_explicit_and_freshness_search(self):
        request = "Do not search the web for the latest version; answer from local context only."
        self.assertTrue(Runtime._web_search_prohibited(request))
        with tempfile.TemporaryDirectory(dir="/private/tmp") as root:
            db = Database(str(Path(root) / "no-web.sqlite")); db.initialize()
            settings = Settings(allowed_roots=(root,), db_path=str(Path(root) / "no-web.sqlite"),
                                main_model="mock", web_mode="auto", web_provider="brave").validated()
            retrieval = RetrievalRouter(FileRetriever((root,)), FTSRetriever(db), DisabledVectorStore(), False, None, None)
            model = type("M", (), {"generate": lambda *args, **kwargs: {"text": "I will answer from local context only.", "latency_ms": 0}})()
            runtime = Runtime(settings, db, retrieval, model)
            with patch("olcr_api.runtime.web_search", side_effect=AssertionError("prohibited Web search was attempted")) as search:
                task, response = runtime.execute(
                    request,
                    core_context="[ACTIVE_SKILL name=find-skills]\nSummarize local instructions only.\n[/ACTIVE_SKILL]",
                )
            self.assertEqual("completed", task.state.value)
            self.assertEqual("I will answer from local context only.", response)
            search.assert_not_called()

    def test_runtime_suppress_web_prevents_auto_search_for_local_only_request(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as root:
            db = Database(str(Path(root) / "local-only.sqlite")); db.initialize()
            settings = Settings(allowed_roots=(root,), db_path=str(Path(root) / "local-only.sqlite"),
                               main_model="mock", web_mode="auto", web_provider="brave").validated()
            retrieval = RetrievalRouter(FileRetriever((root,)), FTSRetriever(db), DisabledVectorStore(), False, None, None)
            model = type("M", (), {"generate": lambda *args, **kwargs: {"text": "Only local sources."}})()
            runtime = Runtime(settings, db, retrieval, model)
            with patch("olcr_api.runtime.web_search", side_effect=AssertionError("web search must be suppressed")) as search:
                _, response = runtime.execute("Search local project documents only; do not call external APIs.", suppress_web=True)
            self.assertEqual("none", json.loads(response)["retrieval_method"])
            search.assert_not_called()

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

    def test_web_composition_has_stable_format_and_cleans_brain_fragments(self):
        sources=[
            {"source_id":"web-1","title":"大谷 翔平 - スポーツナビ","text":"本文"},
            {"source_id":"web-2","title":"J SPORTS【公式】","text":"本文"},
        ]
        rendered=Runtime._render_web_composition([
            {"result_id":"web-1","summary":" MLBトップ   日程・結果 日本人選手 順位表 個人成績 ニュース \n 2025年度成績は打率.282、本塁打55。 https://example.com/nope"},
            {"result_id":"web-2","summary":"プロフィール 生年月日 1994年7月5日。 身長193cm。"},
        ],sources,japanese=True)
        self.assertTrue(rendered.startswith("検索で確認できた資料\n\n1. **大谷 翔平 - スポーツナビ**"))
        self.assertIn("2. **J SPORTS【公式】**",rendered)
        self.assertIn("2025年度成績は打率.282、本塁打55。",rendered)
        self.assertNotIn("日程・結果",rendered)
        self.assertNotIn("https://",rendered)

    def test_web_composition_falls_back_to_clean_page_excerpt(self):
        sources=[{"source_id":"web-1","title":"大谷翔平","text":"MLBトップ 日程・結果 日本人選手 順位表 個人成績 チーム 動画 ニュース\n大谷翔平 プロフィール\n2025年度成績は打率.282、本塁打55、打点102。"}]
        rendered=Runtime._render_web_composition([{"result_id":"web-1","summary":""}],sources,japanese=True)
        self.assertIn("2025年度成績は打率.282、本塁打55、打点102。",rendered)
        self.assertNotIn("MLBトップ",rendered)
        self.assertNotIn("\n   \n",rendered)

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

    def test_brave_explicit_result_count_paginates_past_old_ten_item_cap(self):
        class Response:
            status=200
            def __init__(self, body): self.body=body
            def read(self, n): return self.body
            def __enter__(self): return self
            def __exit__(self,*a): pass
        first=[{"title":f"R{i}","url":f"https://example.com/{i}","description":"x"} for i in range(20)]
        second=[{"title":f"R{i}","url":f"https://example.com/{i}","description":"x"} for i in range(20,25)]
        def open_request(req, timeout):
            params=parse_qs(urlparse(req.full_url).query)
            body={"web":{"results":first if params["offset"]==["0"] else second},
                  "query":{"more_results_available":params["offset"]==["0"]}}
            return Response(json.dumps(body).encode())
        with patch.dict("os.environ", {"OLCR_WEB_SEARCH_API_KEY":"secret"}), patch("olcr_api.web.build_opener") as opener:
            opener.return_value.open.side_effect=open_request
            rows=brave_search("query",25)
        self.assertEqual(25,len(rows))
        self.assertEqual(2,opener.return_value.open.call_count)
        self.assertEqual(["20","20"],[parse_qs(urlparse(call.args[0].full_url).query)["count"][0] for call in opener.return_value.open.call_args_list])

    def test_tavily_limit_over_provider_max_fails_explicitly_without_network(self):
        with patch.dict("os.environ", {"OLCR_WEB_TAVILY_API_KEY":"secret"}), patch("olcr_api.web.build_opener") as opener:
            with self.assertRaisesRegex(ValueError,"PROVIDER_RESULT_LIMIT_EXCEEDED:20"):
                tavily_search("query",21)
            opener.assert_not_called()
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

    def test_url_credentials_and_mixed_public_private_dns_are_blocked(self):
        with self.assertRaisesRegex(ValueError, "URL_CREDENTIALS_REJECTED"):
            validate_url("https://user:pass@example.com/")
        with patch("olcr_api.web.socket.getaddrinfo", return_value=[
                (0, 0, 0, "", ("93.184.216.34", 0)),
                (0, 0, 0, "", ("169.254.169.254", 0)),
        ]):
            with self.assertRaisesRegex(ValueError, "PRIVATE_ADDRESS_REJECTED"):
                validate_url("https://example.com/")

    def test_pinned_http_connection_dials_only_the_validated_ip(self):
        sock = type("Socket", (), {
            "settimeout": lambda self, timeout: None,
            "connect": lambda self, address: setattr(self, "connected_address", address),
            "close": lambda self: None,
        })()
        with patch("olcr_api.web.socket.socket", return_value=sock):
            connection = _PinnedHTTPConnection("example.com", addresses=(("93.184.216.34", 80),))
            connection.connect()
        self.assertEqual(("93.184.216.34", 80), sock.connected_address)
        self.assertIs(connection.sock, sock)

    def test_pinned_https_keeps_original_hostname_for_tls_sni(self):
        sock = type("Socket", (), {
            "settimeout": lambda self, timeout: None,
            "connect": lambda self, address: setattr(self, "connected_address", address),
            "close": lambda self: None,
        })()
        context = type("TLSContext", (), {
            "wrap_socket": lambda self, raw, server_hostname: (setattr(self, "server_hostname", server_hostname) or raw),
        })()
        with patch("olcr_api.web.socket.socket", return_value=sock):
            connection = _PinnedHTTPSConnection("example.com", addresses=(("93.184.216.34", 443),), context=context)
            connection.connect()
        self.assertEqual(("93.184.216.34", 443), sock.connected_address)
        self.assertEqual("example.com", context.server_hostname)

    def test_pinned_https_handler_supports_python_without_check_hostname_attribute(self):
        import ssl
        from urllib.request import Request

        handler = _PinnedHTTPSHandler(context=ssl.create_default_context())
        context = handler._context
        # Python 3.14's HTTPSHandler no longer retains this legacy attribute.
        if hasattr(handler, "_check_hostname"):
            del handler._check_hostname
        observed = {}

        def do_open(connection_factory, request, **kwargs):
            observed.update(kwargs)
            connection_factory(request.host, context=kwargs["context"],
                               check_hostname=kwargs["check_hostname"])
            return "opened"

        with patch("olcr_api.web._resolve_public_addresses", return_value=(("93.184.216.34", 443),)), \
             patch.object(handler, "do_open", side_effect=do_open):
            result = handler.https_open(Request("https://example.com/"))

        self.assertEqual("opened", result)
        self.assertIs(context, observed["context"])
        self.assertIsNone(observed["check_hostname"])
        self.assertTrue(context.check_hostname)

    def test_safe_opener_fetches_via_pinned_socket_and_blocks_private_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302); self.send_header("Location", "http://169.254.169.254/latest/meta-data/"); self.end_headers()
                    return
                body=b"controlled fixture"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *_args): pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        port=server.server_address[1]
        def resolve(url):
            if urlparse(url).hostname=="example.test":
                return (("127.0.0.1",port),)
            raise ValueError("PRIVATE_ADDRESS_REJECTED")
        try:
            with patch("olcr_api.web._resolve_public_addresses",side_effect=resolve):
                with _safe_opener().open("http://example.test:%d/"%port,timeout=2) as response:
                    self.assertEqual(b"controlled fixture",response.read())
                with self.assertRaisesRegex(ValueError,"PRIVATE_ADDRESS_REJECTED"):
                    _safe_opener().open("http://example.test:%d/redirect"%port,timeout=2)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_search_results_are_structured_candidates(self):
        parser = _SearchParser(); parser.feed('<a class="result__a" href="https://example.com">Official result</a>')
        self.assertEqual({"title", "url", "snippet", "rank"}, set(parser.results[0]))
        self.assertEqual("https://example.com", parser.results[0]["url"])

    def test_response_classification_distinguishes_interstitial_and_mismatch(self):
        self.assertEqual("INTERSTITIAL", _classify_search_html("<title>Challenge</title><p>captcha</p>", 0)[0])
        self.assertEqual("PARSER_MISMATCH", _classify_search_html("<title>Results</title><a href='https://example.com'>x</a>", 0)[0])
        self.assertEqual("RESULT_PAGE", _classify_search_html("<a class='result__a' href='https://example.com'>x</a>", 1)[0])


if __name__ == "__main__": unittest.main()
