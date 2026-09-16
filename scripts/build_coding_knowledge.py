#!/usr/bin/env python3
"""Build OLCR's immutable Coding Knowledge artifact from reviewed inputs only."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from olcr_api.knowledge import EXPECTED_EMBEDDING_MODEL, build_production_index, canonical_hash, validate_source_manifest
from olcr_api.semantic import OllamaEmbeddingProvider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, default=ROOT / "packaging" / "coding-knowledge" / "sources.json")
    parser.add_argument("--records", type=Path, default=ROOT / "packaging" / "coding-knowledge" / "records.json")
    parser.add_argument("--output", type=Path, default=ROOT / "packaging" / "coding-knowledge" / "coding-knowledge-index.json")
    parser.add_argument("--endpoint", default=os.environ.get("OLLAMA_ENDPOINT", "http://127.0.0.1:11434"))
    parser.add_argument("--target-olcr-version", default="0.6.0")
    args = parser.parse_args()
    source_manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    seeds = json.loads(args.records.read_text(encoding="utf-8"))
    validate_source_manifest(source_manifest)
    provider = OllamaEmbeddingProvider(args.endpoint)
    identity = provider.model_identity(EXPECTED_EMBEDDING_MODEL)
    metadata = build_production_index(
        source_manifest=source_manifest,
        seeds=seeds,
        provider=provider,
        output=args.output,
        model_identity=identity,
        created_at=seeds["generated_at"],
        target_olcr_version=args.target_olcr_version,
    )
    inventory = {
        "knowledge_index_version": metadata["knowledge_index_version"],
        "source_manifest_hash": metadata["source_manifest_hash"],
        "approved_sources": [source for source in source_manifest["sources"] if source["review_status"] == "APPROVED"],
    }
    inventory_path = args.output.with_name("source-provenance-inventory.json")
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"KNOWLEDGE_INDEX": str(args.output), "EMBEDDING_MODEL": EXPECTED_EMBEDDING_MODEL, "EMBEDDING_IDENTITY": identity, "SOURCE_MANIFEST_HASH": canonical_hash(source_manifest)}, sort_keys=True))

if __name__ == "__main__":
    main()
