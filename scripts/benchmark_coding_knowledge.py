#!/usr/bin/env python3
"""Run deterministic retrieval checks against a completed production index."""
from __future__ import annotations
import argparse, json, os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from olcr_api.knowledge import CodingKnowledge, EXPECTED_EMBEDDING_MODEL
from olcr_api.semantic import OllamaEmbeddingProvider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, default=ROOT / "packaging" / "coding-knowledge" / "coding-knowledge-index.json")
    parser.add_argument("--benchmark", type=Path, default=ROOT / "packaging" / "coding-knowledge" / "benchmark.json")
    parser.add_argument("--endpoint", default=os.environ.get("OLLAMA_ENDPOINT", "http://127.0.0.1:11434"))
    args = parser.parse_args()
    knowledge = CodingKnowledge(args.index)
    if not knowledge.index.available:
        raise SystemExit("KNOWLEDGE_BENCHMARK=BLOCKED_INDEX_UNAVAILABLE")
    provider = OllamaEmbeddingProvider(args.endpoint)
    identity = provider.model_identity(EXPECTED_EMBEDDING_MODEL)
    if not knowledge.index.compatible(EXPECTED_EMBEDDING_MODEL, identity):
        raise SystemExit("KNOWLEDGE_BENCHMARK=BLOCKED_MODEL_MISMATCH")
    queries = json.loads(args.benchmark.read_text(encoding="utf-8"))["queries"]
    results = []
    for item in queries:
        stacks = {item["expected_stack"]}
        vectors = provider.embed([item["query"]], EXPECTED_EMBEDDING_MODEL)
        rows = knowledge.index.search(vectors[0], domains={item["expected_domain"]}, stacks=stacks, versions=item.get("version_context", {}), limit=5)
        text = " ".join(row["content"] for row in rows).lower()
        required = all(str(term).lower() in text for term in item.get("required_concepts", []))
        contaminated = any(row.get("stack") in set(item.get("forbidden_stacks", [])) for row in rows)
        results.append({"query_id": item["query_id"], "expected_domain_hit": bool(rows and all(row["domain"] == item["expected_domain"] for row in rows)), "expected_stack_hit": bool(rows and all(row["stack"] == item["expected_stack"] for row in rows)), "required_concept_hit": required, "cross_stack_contamination": contaminated, "top_k": len(rows)})
    passed = sum(1 for row in results if row["expected_domain_hit"] and row["expected_stack_hit"] and row["required_concept_hit"] and not row["cross_stack_contamination"])
    print(json.dumps({"BENCHMARK_QUERY_COUNT": len(results), "BENCHMARK_PASS_COUNT": passed, "BENCHMARK_FAIL_COUNT": len(results) - passed, "EMBEDDING_DIGEST": identity, "RESULTS": results}, ensure_ascii=False, sort_keys=True))
    if passed != len(results): raise SystemExit(2)


if __name__ == "__main__":
    main()
