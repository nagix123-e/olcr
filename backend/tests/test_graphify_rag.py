import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from olcr_api.graphify_rag import CONTRACT, NO_MATCH, GraphifyRAG, cosine
from olcr_api.retrieval import RetrievalRouter, FileRetriever, FTSRetriever, DisabledVectorStore
from olcr_api.runtime import ContextManager, Runtime
from olcr_api.config import Settings
from olcr_api.db import Database


class Embeddings:
    def __init__(self): self.calls = []; self.revision = "v1"
    def deployment_revision(self, model): return self.revision
    def embed(self, texts, model):
        self.calls.append((texts, model))
        return [[1., 0.] for _ in texts]


class GraphifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "alpha.py").write_text("def alpha():\n    return beta()\n")
        (self.root / "beta.py").write_text("def beta():\n    return 7\n")
        self.data = {"graph": {"olcr": {"contract": CONTRACT, "workspace_root": str(self.root),
                     "source_hashes": {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                                       for name in ("alpha.py", "beta.py")}}},
                     "nodes": [{"id": name, "label": name, "source_file": name+".py", "source_location": "L1"}
                               for name in ("alpha", "beta")],
                     "links": [{"source": "alpha", "target": "beta", "confidence": "EXTRACTED",
                                "relation": "calls", "source_file": "alpha.py"}]}
        self.write_graph()

    def write_graph(self):
        path = self.root / ".olcr/graphify/graph.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.data))

    def search(self, provider=None, query="alpha function"):
        provider = provider or GraphifyRAG()
        return provider.search(query, str(self.root))

    def test_verified_graph_traversal(self):
        rows, handled = self.search()
        self.assertTrue(handled)
        self.assertEqual([Path(row.source).name for row in rows], ["alpha.py", "beta.py"])
        self.assertTrue(all(row.graphify["source_verified"] for row in rows))

    def test_inferred_edge_not_traversed(self):
        self.data["links"][0]["confidence"] = "INFERRED"; self.write_graph()
        self.assertEqual(len(self.search()[0]), 1)

    def test_generic_function_word_does_not_outweigh_named_symbol(self):
        self.data["nodes"][0]["label"] = ".alpha()"
        self.data["nodes"].append({"id": "000-helper", "label": ".complete_function()", "source_file": "alpha.py", "source_location": "L2"})
        self.write_graph()
        rows, _ = self.search(query="Explain alpha function")
        self.assertEqual(rows[0].graphify["node_id"], "alpha")

    def test_stale_source_not_returned(self):
        (self.root / "alpha.py").write_text("def changed():\n    return 0\n")
        rows, handled = self.search()
        self.assertTrue(handled); self.assertEqual(rows, [])

    def test_unverified_old_graph_allows_legacy(self):
        self.data["graph"] = {}; self.write_graph()
        provider = GraphifyRAG()
        self.assertEqual(self.search(provider), ([], False))
        self.assertEqual(provider.telemetry["state"], "fallback")

    def test_dangling_edge_rejected(self):
        self.data["links"][0]["target"] = "missing"; self.write_graph()
        self.assertEqual(self.search(), ([], False))

    def test_corrupt_json_fallback(self):
        (self.root / ".olcr/graphify/graph.json").write_text("{")
        self.assertEqual(self.search(), ([], False))

    def test_escape_source_rejected(self):
        self.data["nodes"][0]["source_file"] = "../outside.py"; self.write_graph()
        self.assertEqual(self.search()[0], [])

    def test_disabled_makes_no_model_calls(self):
        embeddings = Embeddings(); provider = GraphifyRAG(embedding=embeddings, enabled=False)
        self.assertEqual(self.search(provider), ([], False)); self.assertEqual(embeddings.calls, [])

    def test_japanese_semantic_laya_selection(self):
        embeddings = Embeddings(); laya = Mock(loaded=True)
        laya.choice.return_value = {"tool_id": "alpha.py"}
        provider = GraphifyRAG(embedding=embeddings, laya=lambda: laya)
        rows, handled = self.search(provider, "この関数の処理を説明")
        self.assertTrue(handled); self.assertTrue(rows)
        self.assertEqual(provider.telemetry["embedding"], "pass")
        self.assertLessEqual(len(laya.choice.call_args.kwargs["candidates"]), 8)
        self.assertTrue(all(model == "embeddinggemma:latest" for _, model in embeddings.calls))

    def test_no_match_cannot_fall_back_to_legacy(self):
        laya = Mock(loaded=True); laya.choice.return_value = {"tool_id": NO_MATCH}
        graph = GraphifyRAG(laya=lambda: laya)
        files = Mock(); router = RetrievalRouter(files, Mock(), Mock(), False, graphify=graph)
        rows, method = router.retrieve("alpha", 3, workspace_root=str(self.root))
        self.assertEqual((rows, method), ([], "graphify")); files.search.assert_not_called()

    def test_invalid_laya_selection_abstains(self):
        laya = Mock(loaded=True); laya.choice.return_value = {"tool_id": "outside.py"}
        self.assertEqual(self.search(GraphifyRAG(laya=lambda: laya)), ([], True))

    def test_embeddings_cached_and_revision_invalidates(self):
        embeddings = Embeddings(); graph = GraphifyRAG(embedding=embeddings)
        self.search(graph); self.search(graph)
        self.assertEqual(len(embeddings.calls), 3)
        embeddings.revision = "v2"; self.search(graph)
        self.assertEqual(len(embeddings.calls), 5)

    def test_embedding_failure_keeps_verified_lexical(self):
        embedding = Mock(); embedding.embed.side_effect = RuntimeError("offline")
        graph = GraphifyRAG(embedding=embedding)
        self.assertTrue(self.search(graph)[0]); self.assertEqual(graph.telemetry["embedding"], "degraded")

    def test_identity_failure_does_not_disable_embedding(self):
        embeddings = Embeddings()
        embeddings.model_identity = Mock(side_effect=ValueError("no digest"))
        graph = GraphifyRAG(embedding=embeddings)
        self.assertTrue(self.search(graph)[0])
        self.assertEqual(graph.telemetry["embedding"], "pass")
        self.assertEqual(graph.telemetry["vector_cache"], "request_only")

    def test_semantic_only_requires_laya(self):
        self.assertEqual(self.search(GraphifyRAG(embedding=Embeddings()), "日本語で説明"), ([], True))

    def test_graph_context_retains_provenance_and_untrusted_boundary(self):
        rows, _ = self.search()
        messages, selected = ContextManager(8000).build("explain alpha function", rows)
        self.assertIn("graphify", selected[0])
        self.assertTrue(any("untrusted data" in row["content"] for row in messages))

    def test_invalid_vectors(self):
        for left, right in [([], []), ([1.], [1., 2.]), ([float("nan")], [1.]), ([0.], [1.])]:
            with self.assertRaises(ValueError): cosine(left, right)

    def runtime(self, graph):
        db = Database(str(self.root / "test.sqlite")); db.initialize()
        model = Mock(); model.generate.return_value = {"text": "verified answer", "latency_ms": 0}
        router = RetrievalRouter(FileRetriever([str(self.root)]), FTSRetriever(db), DisabledVectorStore(), False, graphify=graph)
        return Runtime(Settings(db_path=db.path, allowed_roots=(str(self.root),), main_model="mock").validated(), db, router, model), model

    def test_runtime_normal_question_uses_graph(self):
        runtime, model = self.runtime(GraphifyRAG())
        task, response = runtime.execute("Explain alpha function", workspace_root=str(self.root), suppress_web=True)
        self.assertEqual(response, "verified answer")
        self.assertTrue(any(row.get("method") == "graphify" for row in task.selected_context))
        self.assertTrue(any("Graphify is the authoritative" in row["content"] for row in model.generate.call_args.args[0]))

    def test_runtime_abstention_is_visible_to_generator(self):
        laya = Mock(loaded=True); laya.choice.return_value = {"tool_id": NO_MATCH}
        runtime, model = self.runtime(GraphifyRAG(laya=lambda: laya))
        runtime.execute("Explain alpha function", workspace_root=str(self.root), suppress_web=True)
        self.assertTrue(any("no verified relevant source" in row["content"] for row in model.generate.call_args.args[0]))

    def test_runtime_direct_bypasses_graph_and_models(self):
        graph = GraphifyRAG(embedding=Embeddings())
        runtime, model = self.runtime(graph)
        task, response = runtime.execute("calculate 2+3", workspace_root=str(self.root))
        self.assertEqual(task.route.value, "DIRECT")
        self.assertEqual(graph.embedding.calls, []); model.generate.assert_not_called()

    def test_project_root_mismatch_falls_back(self):
        self.data["graph"]["olcr"]["workspace_root"] = str(self.root / "other"); self.write_graph()
        self.assertEqual(self.search(), ([], False))


if __name__ == "__main__": unittest.main()
