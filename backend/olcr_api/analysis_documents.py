"""Materialize linked public documents through the existing guarded Web fetch.

Catalog entries stay catalog entries when text cannot be acquired. Downloads
are bounded, retain their own provenance, and never execute source content.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .evidence_quality import subject_matches, subject_queries


def materialize_documents(message, results, fetch_page, maximum=4):
    output, queues, trace = [], [], []
    packets = []
    def copy_packet(original):
        result = {**original, "data": dict(original.get("data") or {}), "sources": list(original.get("sources") or [])}
        data = result["data"]
        rows = data.get("items") or []
        if data.get("render_nested_evidence") and rows and all(isinstance(row, dict) and isinstance(row.get("data"), dict) for row in rows):
            data["items"] = [copy_packet({**row, "tool_id": result.get("tool_id"), "provider": result.get("provider"),
                                         "sources": row.get("sources") or result["sources"]}) for row in rows]
        else:
            packets.append(result)
        return result
    for original in results:
        output.append(copy_packet(original))
    for result in packets:
        data = result["data"]
        tool = str(result.get("tool_id") or "")
        if not (tool.startswith(("law.", "politics.", "policy.")) or data.get("evidence_kind") == "document_catalog"):
            continue
        if data.get("evidence_kind") == "dataset_catalog":
            continue  # Landing-page prose is not the dataset's observations.
        rows = data.get("items")
        if not isinstance(rows, list):
            continue
        data["items"] = [dict(row) if isinstance(row, dict) else row for row in rows]
        candidates = []
        for row in data["items"]:
            if not isinstance(row, dict) or any(row.get(key) for key in ("text", "speech_excerpt", "body_excerpt", "abstract")):
                continue
            work = row.get("work")
            url = row.get("url") or row.get("document_url") or (work.get("value") if isinstance(work, dict) else None)
            try:
                valid_url = isinstance(url, str) and urlsplit(url).scheme in {"https", "http"}
            except ValueError:
                valid_url = False
            if not valid_url:
                continue
            candidates.append((result, row, url))
        if candidates:
            queues.append(candidates[:2])
    seen = set()
    while any(queues) and len(trace) < maximum:
        for queue in queues:
            if not queue or len(trace) >= maximum:
                continue
            result, row, url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            event = {"tool_id": result.get("tool_id"), "url": url}
            try:
                page = fetch_page(url)
                content_type = str(page.get("content_type") or (page.get("metadata") or {}).get("content_type") or "")
                text = str(page.get("clean_markdown") or page.get("text") or "").strip()
                if (not ("html" in content_type or content_type.startswith("text/plain"))
                        or len(text) < 240 or not subject_matches(message, text)
                        or re.search(r"enable javascript and cookies|verify you are human|access denied|captcha", text[:600], re.I)):
                    raise ValueError("DOCUMENT_TEXT_UNVERIFIED")
                positions = [text.casefold().find(term.casefold()) for term in subject_queries(message)
                             if text.casefold().find(term.casefold()) >= 0]
                offset = max(0, min(positions)-200) if positions else 0
                row.update(body_excerpt=text[offset:offset+3200], content_url=page.get("final_url") or url,
                           text_scope="public_document_excerpt", text_complete=False)
                result["sources"].append({"title": row.get("title") or page.get("title") or "Retrieved document",
                    "canonical_url": row["content_url"], "provider": result.get("provider"),
                    "retrieved_at": page.get("timestamp")})
                event.update(status="success", chars=len(row["body_excerpt"]), content_type=content_type)
            except Exception as exc:
                # Fetch failures cannot erase the catalog or another source.
                event.update(status="failed", error=type(exc).__name__)
            trace.append(event)
    for result in packets:
        data = result["data"]
        rows = data.get("items") or []
        expanded = [row for row in rows if isinstance(row, dict) and row.get("content_url")]
        if expanded:
            data["materialized_document_count"] = len(expanded)
            if len(expanded) == len(rows) and data.get("evidence_kind") == "document_catalog":
                data["evidence_kind"] = "document_excerpt"
            data["document_text_scope"] = "excerpts_only"
    return output, trace
