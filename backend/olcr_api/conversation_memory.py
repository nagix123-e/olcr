from __future__ import annotations

import math
import time
from typing import Any


class ConversationMemory:
    """Bounded, local retrieval over completed user/assistant turns."""
    SOURCE_KIND = "conversation_turn"
    INDEX_VERSION = "conversation-turn-v1"

    def __init__(self, db: Any, provider: Any, model: str, limit: int = 4):
        self.db, self.provider, self.model, self.limit = db, provider, model, limit

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            return -1.0
        denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
        return sum(x * y for x, y in zip(left, right)) / denominator if denominator else 0.0

    def index_completed_turn(self, conversation_id: str) -> None:
        if not self.model or self.provider is None:
            return
        turns = [x for x in self.db.completed_turns() if x["conversation_id"] == conversation_id]
        for turn in turns:
            text = turn["user_message"] + "\n" + turn["assistant_message"]
            vector = self.provider.embed([text], self.model)[0]
            self.db.save_memory_embedding(turn, self.model, vector, time.time(), self.INDEX_VERSION)

    def backfill(self, limit: int = 8) -> int:
        """Index a bounded batch of historical turns not yet represented."""
        if not self.model or self.provider is None:
            return 0
        turns = self.db.completed_turns()
        with self.db.connect() as conn:
            known = {
                (row["conversation_id"], row["turn_ordinal"]):
                (row["user_message"], row["assistant_message"])
                for row in conn.execute(
                    "SELECT conversation_id,turn_ordinal,user_message,assistant_message "
                    "FROM conversation_memory_embeddings WHERE model=? AND index_version=?",
                    (self.model, self.INDEX_VERSION),
                )
            }
        added = 0
        for turn in turns:
            key = (turn["conversation_id"], turn["ordinal"])
            current_content = (turn["user_message"], turn["assistant_message"])
            if known.get(key) == current_content:
                continue
            text = turn["user_message"] + "\n" + turn["assistant_message"]
            try:
                vector = self.provider.embed([text], self.model)[0]
                self.db.save_memory_embedding(turn, self.model, vector, time.time(), self.INDEX_VERSION)
                added += 1
            except Exception:
                break
            if added >= limit:
                break
        return added

    def search(self, query: str, exclude_conversation: str | None = None) -> list[dict[str, Any]]:
        if not self.model or self.provider is None:
            return []
        query_vector = self.provider.embed([query], self.model)[0]
        rows = self.db.memory_embeddings(self.model, len(query_vector), self.INDEX_VERSION)
        ranked = sorted(((self._cosine(query_vector, __import__("json").loads(row["vector_json"])), row) for row in rows), key=lambda x: x[0], reverse=True)
        result = []
        for score, row in ranked:
            if exclude_conversation and row["conversation_id"] == exclude_conversation:
                continue
            result.append({"source_kind": self.SOURCE_KIND, "conversation_id": row["conversation_id"], "score": score, "text": f"User: {row['user_message']}\nAssistant: {row['assistant_message']}"})
            if len(result) >= self.limit:
                break
        return result
