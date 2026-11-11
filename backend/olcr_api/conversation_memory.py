from __future__ import annotations

from datetime import datetime, timezone
from heapq import nlargest
import json
import math
import re
from threading import Lock, Thread
import time
from typing import Any

from .context_policy import relevance, terms


_MAINTENANCE_JOB = Lock()


class ConversationMemory:
    """Search compact historical excerpts and a bounded window of recent turns."""

    SOURCE_KIND = "conversation_turn"
    SUMMARY_KIND = "conversation_summary"
    INDEX_VERSION = "conversation-turn-v1"
    SUMMARY_VERSION = "conversation-extract-v1"
    MIN_RELEVANCE_SCORE = 0.55
    SUMMARY_TURNS = 8
    SUMMARY_IDLE_SECONDS = 24 * 60 * 60
    HISTORY_MAINTENANCE_INTERVAL = 30
    RECENT_CANDIDATES = 32
    DETAIL_CHUNKS = 2
    _DETAIL_REQUEST = re.compile(r"原文|逐語|正確|一字一句|そのまま引用|\b(?:exact|verbatim|quot(?:e|ation))\b", re.I)

    def __init__(self, db: Any, provider: Any, model: str, limit: int = 4,
                 min_relevance: float = .55, budget: int = 6000, cross_project: bool = False):
        if (type(min_relevance) not in (int, float) or not math.isfinite(min_relevance)
                or not 0 <= min_relevance <= 1):
            raise ValueError("min_relevance must be finite and between 0 and 1")
        self.cross_project = cross_project
        self.db, self.provider, self.model, self.limit = db, provider, model, limit
        self.MIN_RELEVANCE_SCORE, self.budget = min_relevance, budget
        self._history_due = 0.0

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            return -1.0
        denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
        return sum(x * y for x, y in zip(left, right)) / denominator if denominator else 0.0

    @staticmethod
    def _excerpt(text: str, limit: int) -> str:
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        head = limit * 2 // 3
        return text[:head] + " … " + text[-(limit - head - 3):]

    @classmethod
    def _summarize(cls, turns: list[dict[str, Any]]) -> str:
        excerpts = dict.fromkeys(
            f"User: {cls._excerpt(turn['user_message'], 110)}\n"
            f"Assistant: {cls._excerpt(turn['assistant_message'], 155)}"
            for turn in turns
        )
        return "\n\n".join(excerpts)

    def _embed(self, text: str) -> list[float]:
        vector = self.provider.embed([text], self.model)[0]
        if not vector or not all(math.isfinite(x) for x in vector) or not any(vector):
            raise ValueError("invalid conversation memory embedding")
        return vector

    def _index_turn(self, turn: dict[str, Any]) -> bool:
        vector = self._embed(turn["user_message"] + "\n" + turn["assistant_message"])
        return self.db.save_memory_embedding(turn, self.model, vector, turn["source_at"],
                                             self.INDEX_VERSION, self.SUMMARY_VERSION)

    def index_completed_turn(self, conversation_id: str) -> None:
        if self.model and self.provider is not None:
            for turn in self.db.unindexed_memory_turns(self.model, self.INDEX_VERSION, self.SUMMARY_VERSION, 1,
                                                        conversation_id=conversation_id):
                self._index_turn(turn)

    def backfill(self, limit: int = 8, project_id: str | None = None) -> int:
        if not self.model or self.provider is None or limit <= 0:
            return 0
        added = 0
        for turn in self.db.unindexed_memory_turns(self.model, self.INDEX_VERSION, self.SUMMARY_VERSION, limit,
                                                    project_id=project_id):
            try:
                added += int(self._index_turn(turn))
            except Exception:
                break
        return added

    def compact(self, conversation_id: str | None = None) -> int:
        """Replace up to eight derived turn records with one summary; leave source messages intact."""
        if not self.model or self.provider is None:
            return 0
        turns = self.db.memory_summary_batch(self.model, self.SUMMARY_VERSION, self.SUMMARY_TURNS,
                                             time.time() - self.SUMMARY_IDLE_SECONDS, conversation_id)
        if not turns or (conversation_id is not None and len(turns) < self.SUMMARY_TURNS):
            return 0
        text = self._summarize(turns)
        vector = self._embed(text)
        return int(self.db.save_memory_summary(turns, text, self.model, self.SUMMARY_VERSION, vector,
                                               self.INDEX_VERSION))

    def maintain(self, limit: int = 8) -> dict[str, Any]:
        if not _MAINTENANCE_JOB.acquire(blocking=False):
            return {"indexed": 0, "summarized": 0, "status": "busy"}
        try:
            return self._maintain_history(limit)
        finally:
            _MAINTENANCE_JOB.release()

    def _maintain_history(self, limit: int) -> dict[str, Any]:
        try:
            return {"summarized": self.compact(), "indexed": self.backfill(limit)}
        except Exception as exc:
            return {"indexed": 0, "summarized": 0, "status": "degraded", "error": type(exc).__name__}

    def schedule_maintenance(self, conversation_id: str) -> None:
        if self.model and self.provider is not None:
            _BACKGROUND_MEMORY.schedule(self, conversation_id)

    def _maintain_after_chat(self, conversation_id: str) -> None:
        self.compact(conversation_id)
        self.index_completed_turn(conversation_id)
        if time.monotonic() >= self._history_due:
            self._history_due = time.monotonic() + self.HISTORY_MAINTENANCE_INTERVAL
            self._maintain_history(8)

    @staticmethod
    def _turn_text(row: dict[str, Any]) -> str:
        return f"User: {row['user_message']}\nAssistant: {row['assistant_message']}"

    @staticmethod
    def _focused_excerpt(text: str, query: str, limit: int) -> str:
        if len(text) <= limit or limit < 8:
            return text[:limit]
        width = limit - 4  # Leave room to mark clipping on either side.
        folded = text.lower()
        end_start = len(text) - width
        starts = {0, end_start}
        for token in sorted(terms(query), key=lambda term: (-len(term), term))[:32]:
            for position in (folded.find(token), folded.rfind(token)):
                if position >= 0:
                    starts.add(min(end_start, max(0, position - width // 4)))
        start = max(starts, key=lambda offset: (relevance(query, text[offset:offset + width]), -offset))
        return ("… " if start else "") + text[start:start + width] + (" …" if start < end_start else "")

    @staticmethod
    def _rank_key(candidate):
        score, row, _ = candidate
        return score, row["source_at"]

    def search(self, query: str, exclude_conversation: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        if not self.model or self.provider is None or self.budget <= 0 or self.limit <= 0:
            return []
        try:
            vector = self._embed(query)
        except Exception:
            vector = None
        scope = (project_id, self.cross_project, exclude_conversation)
        dimension = len(vector) if vector is not None else None

        def semantic(row):
            try:
                return self._cosine(vector, json.loads(row["vector_json"]))
            except (KeyError, ValueError, TypeError, OverflowError):
                return -1.0

        def ranked(candidate_rows, scorer, kind):
            for row in candidate_rows:
                score = scorer(row)
                if math.isfinite(score):
                    yield score, row, kind

        summary_rows = self.db.memory_summary_rows(self.model, self.SUMMARY_VERSION, dimension, *scope)
        summaries = nlargest(max(self.limit, self.DETAIL_CHUNKS), ranked(
            summary_rows, semantic if vector is not None else lambda row: relevance(query, row["text"]),
            self.SUMMARY_KIND), key=self._rank_key)

        # Recent turns can supersede what a summary recorded. Their candidate
        # pool is capped before any text or vectors leave SQLite.
        if vector is not None:
            recent = ranked(self.db.recent_memory_embeddings(self.model, self.INDEX_VERSION, self.SUMMARY_VERSION,
                            dimension, *scope, self.RECENT_CANDIDATES), semantic, self.SOURCE_KIND)
        else:
            recent = ranked(self.db.recent_memory_turns(self.model, self.SUMMARY_VERSION, *scope, self.RECENT_CANDIDATES),
                            lambda row: relevance(query, row["user_message"]), self.SOURCE_KIND)

        def accepted(candidates):
            return [item for item in candidates if item[0] >= self.MIN_RELEVANCE_SCORE]

        best = sorted(accepted(summaries) + accepted(recent), key=self._rank_key, reverse=True)
        details = []
        wants_detail = bool(self._DETAIL_REQUEST.search(query))
        detail_query = self._DETAIL_REQUEST.sub("", query)
        if not best or wants_detail:
            for _, summary, _ in summaries[:self.DETAIL_CHUNKS]:
                turns = self.db.memory_detail_turns(summary["conversation_id"], summary["first_ordinal"],
                                                    summary["last_ordinal"], self.SUMMARY_TURNS, *scope)
                for turn in turns:
                    excerpt = self._focused_excerpt(self._turn_text(turn), detail_query, min(3000, self.budget))
                    score = relevance(detail_query, excerpt)
                    if score >= self.MIN_RELEVANCE_SCORE:
                        details.append((score, turn | {"detail": True}, self.SOURCE_KIND))
        if details:
            best = sorted(details, key=self._rank_key, reverse=True) + [
                item for item in best
                if item[2] != self.SUMMARY_KIND or not any(
                    detail["conversation_id"] == item[1]["conversation_id"]
                    and item[1]["first_ordinal"] <= detail["ordinal"] <= item[1]["last_ordinal"]
                    for _, detail, _ in details)
            ]

        results, used = [], 0
        for score, row, kind in best:
            room = min(3000, self.budget - used)
            if room <= 0:
                break
            item = {"source_kind": kind, "conversation_id": row["conversation_id"], "score": score,
                    "source_at": row["source_at"]}
            if kind == self.SUMMARY_KIND:
                date = datetime.fromtimestamp(row["source_at"], timezone.utc).date().isoformat()
                header = f"Past conversation excerpts (through {date} UTC)\n"
                text = header + self._focused_excerpt(row["text"], detail_query, max(0, room - len(header)))
                item.update(first_ordinal=row["first_ordinal"], last_ordinal=row["last_ordinal"])
            else:
                text = self._turn_text(row)
                if row.get("detail") or wants_detail:
                    text = self._focused_excerpt(text, detail_query, room)
                    if row.get("detail") and relevance(detail_query, text) < self.MIN_RELEVANCE_SCORE:
                        continue
                item["ordinal"] = row.get("ordinal", row.get("turn_ordinal"))
            text = text[:room]
            if not text:
                break
            results.append(item | {"text": text})
            used += len(text)
            if len(results) >= self.limit:
                break
        return results


class _BackgroundMemory:
    """One bounded queue per backend, even when settings rebuild the indexer."""

    MAX_PENDING = 32

    def __init__(self):
        self.lock = Lock()
        self.pending: dict[tuple[str, str], ConversationMemory] = {}
        self.running = False

    def schedule(self, memory: ConversationMemory, conversation_id: str) -> None:
        key = (memory.db.path, conversation_id)
        with self.lock:
            self.pending.pop(key, None)
            self.pending[key] = memory
            if len(self.pending) > self.MAX_PENDING:
                self.pending.pop(next(iter(self.pending)))
            if self.running:
                return
            self.running = True
            try:
                Thread(target=self._drain, name="olcr-conversation-memory", daemon=True).start()
            except Exception:
                self.running = False

    def _drain(self) -> None:
        while True:
            with self.lock:
                if not self.pending:
                    self.running = False
                    return
                key = next(iter(self.pending))
                memory = self.pending.pop(key)
            with _MAINTENANCE_JOB:
                try:
                    memory._maintain_after_chat(key[1])
                except Exception:
                    pass  # The transcript is already saved; maintenance can retry it later.


_BACKGROUND_MEMORY = _BackgroundMemory()
