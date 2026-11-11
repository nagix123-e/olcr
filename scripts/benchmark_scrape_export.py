#!/usr/bin/env python3
"""Measure real Scrape Studio StreamingResponse exports over temporary SQLite rows."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
FORMATS = ("csv", "json", "jsonl")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, int(len(ordered) * fraction + 0.999999) - 1)], 3)


async def _consume(response, output_format: str, expected_rows: int) -> dict:
    first_byte_ms = None
    start = time.perf_counter()
    byte_count = 0
    row_count = 0
    chunk_count = 0
    digest = hashlib.sha256()
    prefix = bytearray()
    json_decoder = json.JSONDecoder()
    json_pending = ""
    json_started = False
    async for chunk in response.body_iterator:
        raw = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
        if first_byte_ms is None:
            first_byte_ms = (time.perf_counter() - start) * 1000
        chunk_count += 1
        byte_count += len(raw)
        digest.update(raw)
        if len(prefix) < 4096:
            prefix.extend(raw[:4096 - len(prefix)])
        if output_format == "jsonl":
            row_count += raw.count(b"\n")
        elif output_format == "csv":
            row_count += raw.count(b"\n")
        else:
            json_pending += raw.decode("utf-8")
            if not json_started:
                opening = json_pending.lstrip()
                if opening.startswith("["):
                    json_pending = opening[1:]
                    json_started = True
            while json_started:
                json_pending = json_pending.lstrip()
                if json_pending.startswith(","):
                    json_pending = json_pending[1:]
                    continue
                if json_pending.startswith("]") or not json_pending:
                    break
                try:
                    _item, end = json_decoder.raw_decode(json_pending)
                except json.JSONDecodeError:
                    break
                row_count += 1
                json_pending = json_pending[end:]
    total_ms = (time.perf_counter() - start) * 1000
    if output_format == "csv":
        row_count -= 1  # Exclude the header.
    if row_count != expected_rows:
        raise RuntimeError(f"export row count mismatch: expected {expected_rows}, got {row_count}")
    marker = b"'=DANGEROUS-000000" if output_format == "csv" else b"=DANGEROUS-000000"
    if marker not in prefix:
        raise RuntimeError(f"export did not preserve expected sample/formula policy for {output_format}")
    return {
        "rows": row_count,
        "bytes": byte_count,
        "chunks": chunk_count,
        "first_byte_ms": round(first_byte_ms or 0.0, 3),
        "total_ms": round(total_ms, 3),
        "throughput_mib_s": round(byte_count / (1024 * 1024) / (total_ms / 1000), 3) if total_ms else 0.0,
        "sha256": digest.hexdigest(),
    }


def _consume_child(db_path: str, run_id: str, output_format: str, expected_rows: int,
                   measure_memory: bool) -> dict:
    os.environ["OLCR_DB_PATH"] = db_path
    os.environ["OLCR_APP_SUPPORT"] = str(Path(db_path).parent / "app-support")
    os.environ["OLCR_VECTOR_ENABLED"] = "false"
    os.environ["OLCR_ALLOWED_ROOTS"] = str(REPO_ROOT)
    sys.path.insert(0, str(BACKEND_ROOT))
    from olcr_api import app as app_module

    if measure_memory:
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
    response = app_module.export_web_scrape_run(run_id)
    media_type = response.media_type
    expected_media = {
        "csv": "text/csv; charset=utf-8",
        "json": "application/json",
        "jsonl": "application/x-ndjson",
    }[output_format]
    if media_type != expected_media:
        raise RuntimeError(f"unexpected media type: {media_type!r}")
    measured = asyncio.run(_consume(response, output_format, expected_rows))
    if measure_memory:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        measured["python_peak_incremental_bytes"] = max(0, peak - before)
    # This process is fresh for each sample; ru_maxrss is therefore comparable
    # across formats and row counts, though it includes Python/app import overhead.
    peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux and most other Unix platforms report KiB.
    measured["process_peak_rss_bytes"] = peak_rss if sys.platform == "darwin" else peak_rss * 1024
    measured["media_type"] = media_type
    return measured


def _seed_database(db_path: Path, row_counts: list[int]) -> dict:
    sys.path.insert(0, str(BACKEND_ROOT))
    from olcr_api.db import Database
    from olcr_api.recipes import ScrapeRecipeInput

    database = Database(str(db_path))
    database.initialize()
    runs: dict[str, dict] = {}
    now = time.time()
    definitions = []
    for count in row_counts:
        for output_format in FORMATS:
            run_id = f"p23-{count}-{output_format}-{uuid.uuid4().hex[:8]}"
            recipe_id = f"recipe-{run_id}"
            recipe = ScrapeRecipeInput.model_validate({
                "name": f"P23 {output_format} {count}",
                "start_url": "https://example.test/records",
                "record_selector": "article.record",
                "fields": {
                    "name": {"selector": ".name", "extract": "text"},
                    "group": {"selector": ".group", "extract": "text"},
                    "score": {"selector": ".score", "extract": "number"},
                },
                "output_format": output_format,
                "max_pages": 1,
                "max_records": count,
            }).to_record(recipe_id)
            database.save_web_recipe(recipe)
            database.create_web_scrape_run(run_id, recipe)
            definitions.append((count, output_format, run_id))
    with database.connect() as connection:
        for count, output_format, run_id in definitions:
                rows = []
                for index in range(count):
                    name = f"=DANGEROUS-{index:06d}" if index == 0 else f"Organization {index:06d}"
                    row = {"name": name, "group": f"Sector {index % 17:02d}", "score": index % 1000}
                    rows.append((run_id, index, 1, None,
                                 json.dumps(row, ensure_ascii=False, separators=(",", ":")), now))
                    if len(rows) >= 2048:
                        connection.executemany(
                            "INSERT INTO web_scrape_rows(run_id,ordinal,page_number,dedupe_key,row_json,created_at) VALUES(?,?,?,?,?,?)",
                            rows,
                        )
                        rows.clear()
                if rows:
                    connection.executemany(
                        "INSERT INTO web_scrape_rows(run_id,ordinal,page_number,dedupe_key,row_json,created_at) VALUES(?,?,?,?,?,?)",
                        rows,
                    )
                connection.execute(
                    "UPDATE web_scrape_runs SET status='COMPLETED',page_cursor=1,pages_completed=1,record_count=? WHERE id=?",
                    (count, run_id),
                )
                runs[f"{count}:{output_format}"] = {"run_id": run_id, "rows": count}
    return runs


def run(row_counts: list[int], iterations: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="olcr-p23-export-") as temporary:
        db_path = Path(temporary) / "p23-export.db"
        runs = _seed_database(db_path, row_counts)
        samples = {}
        for key, record in runs.items():
            count_text, output_format = key.split(":", 1)
            args = [sys.executable, str(Path(__file__).resolve()), "--consume",
                    str(db_path), record["run_id"], output_format, count_text]
            timed_values = []
            memory_values = []
            for _ in range(iterations):
                for mode, target in (("timing", timed_values), ("memory", memory_values)):
                    completed = subprocess.run([*args, mode], cwd=REPO_ROOT, text=True,
                                               capture_output=True, check=True, timeout=60)
                    target.append(json.loads(completed.stdout))
            reference = timed_values[0]
            if any(value["rows"] != record["rows"] or value["sha256"] != reference["sha256"]
                   for value in timed_values + memory_values):
                raise RuntimeError(f"nondeterministic or incomplete output for {key}")
            total_ms = [value["total_ms"] for value in timed_values]
            first_byte_ms = [value["first_byte_ms"] for value in timed_values]
            throughput = [value["throughput_mib_s"] for value in timed_values]
            samples[key] = {
                "rows": record["rows"],
                "bytes": reference["bytes"],
                "chunks": reference["chunks"],
                "media_type": reference["media_type"],
                "sha256": reference["sha256"],
                "iterations": iterations,
                "first_byte_ms_p50": round(statistics.median(first_byte_ms), 3),
                "first_byte_ms_p95": _percentile(first_byte_ms, .95),
                "total_ms_p50": round(statistics.median(total_ms), 3),
                "total_ms_p95": _percentile(total_ms, .95),
                "throughput_mib_s_p50": round(statistics.median(throughput), 3),
                "python_peak_incremental_bytes_max": max(value["python_peak_incremental_bytes"] for value in memory_values),
                "process_peak_rss_bytes_max": max(
                    value["process_peak_rss_bytes"]
                    for value in timed_values + memory_values
                ),
                "formula_policy_sample": "verified",
            }
        return {
            "benchmark": "scrape_studio_p23_streaming_export_v1",
            "scope": "direct iteration of the real FastAPI export endpoint's StreamingResponse body iterator over temporary SQLite rows",
            "date": time.strftime("%Y-%m-%d", time.gmtime()),
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "sqlite": __import__("sqlite3").sqlite_version,
                "iterations_per_format_and_size": iterations,
                "batch_size": 256,
                "output_chunk_rows": 64,
            },
            "row_counts": row_counts,
            "model_calls": 0,
            "temporary_database_only": True,
            "samples": samples,
            "limits": [
                "local synthetic data only; does not establish disk/network client behavior",
                "process peak RSS includes app import/runtime baseline; compare fresh-process runs at increasing output sizes",
                "no logged-in sites, live browser, desktop capture, or model inference",
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", default=[10_000, 100_000])
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--consume", nargs=5, metavar=("DB", "RUN_ID", "FORMAT", "ROWS", "MODE"))
    args = parser.parse_args()
    if args.consume:
        db_path, run_id, output_format, count, mode = args.consume
        if output_format not in FORMATS:
            parser.error("invalid output format")
        if mode not in {"timing", "memory"}:
            parser.error("invalid measurement mode")
        print(json.dumps(_consume_child(db_path, run_id, output_format, int(count),
                                        mode == "memory"), sort_keys=True))
        return
    if args.iterations < 3 or args.iterations > 20:
        parser.error("--iterations must be in [3, 20]")
    if any(count < 1000 or count > 1_000_000 for count in args.rows):
        parser.error("--rows values must be in [1000, 1000000]")
    print(json.dumps(run(args.rows, args.iterations), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
