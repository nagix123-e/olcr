"""Build OLCR's verified Graphify snapshot with the Graphify Python runtime.

No LLM extraction, model downloads, or modification of source files.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--include", action="append", required=True,
                        help="Explicit source path relative to workspace (repeatable)")
    args = parser.parse_args()
    graphify_version = version("graphifyy")
    if graphify_version != "0.9.73":
        parser.error(f"Validated Graphify version is 0.9.73, installed: {graphify_version}")
    from graphify.extract import collect_files, extract
    from graphify.build import build_from_json
    from graphify.export import to_json
    root = args.workspace.resolve(strict=True)
    files = set()
    for include in args.include:
        target = (root / include).resolve(strict=True)
        target.relative_to(root)
        files.update(collect_files(target, root=root))
    paths = sorted(files)
    if not paths:
        parser.error("No supported source files in explicit include paths")
    if len(paths) > 4096:
        parser.error("Source file count exceeds 4096; narrow --include")
    def hashes():
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    before = hashes()
    # Extraction caches stay task-owned and disappear on completion/failure.
    with tempfile.TemporaryDirectory(prefix="olcr-graphify-build-") as cache:
        extracted = extract(paths, cache_root=Path(cache), root=root, parallel=False)
        graph = build_from_json(extracted, directed=True, root=root)
        if hashes() != before:
            raise RuntimeError("Sources changed during extraction; previous snapshot preserved")
        graph.graph["olcr"] = {"contract": "olcr-graphify-v1", "workspace_root": str(root),
                               "source_hashes": before, "graphify_version": graphify_version,
                               "extraction": "ast_only", "includes": args.include}
        destination = root / ".olcr/graphify"
        destination.resolve(strict=False).relative_to(root)
        destination.mkdir(parents=True, exist_ok=True)
        destination.resolve(strict=True).relative_to(root)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=destination) as staging:
            output = Path(staging) / "graph.json"
            if not to_json(graph, {}, str(output)):
                raise RuntimeError("Graphify export refused")
            if output.stat().st_size > 32 * 1024 * 1024:
                raise RuntimeError("Snapshot exceeds runtime size limit")
            if hashes() != before:
                raise RuntimeError("Sources changed during export; previous snapshot preserved")
            output.replace(destination / "graph.json")
    print(json.dumps({"path": str(destination / "graph.json"), "files": len(paths),
                      "nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
                      "graphify_version": graphify_version}))


if __name__ == "__main__":
    main()
