from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import time
import uuid


class Route(str, Enum):
    DIRECT = "DIRECT"
    RETRIEVAL = "RETRIEVAL"
    PROCEDURE = "PROCEDURE"
    NEURAL = "NEURAL"


class Risk(str, Enum):
    SAFE = "SAFE"
    CONFIRM = "CONFIRM"
    DENY_DEFAULT = "DENY_DEFAULT"


class TaskState(str, Enum):
    CREATED = "created"
    ROUTING = "routing"
    SEARCHING = "searching"
    EXECUTING = "executing"
    GENERATING = "generating"
    WAITING = "waiting_for_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.DENIED, TaskState.CANCELLED}
TRANSITIONS = {
    TaskState.CREATED: {TaskState.ROUTING},
    TaskState.ROUTING: {TaskState.SEARCHING, TaskState.EXECUTING, TaskState.GENERATING, TaskState.WAITING, TaskState.DENIED, TaskState.FAILED},
    TaskState.SEARCHING: {TaskState.GENERATING, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.EXECUTING: {TaskState.COMPLETED, TaskState.FAILED, TaskState.WAITING, TaskState.CANCELLED},
    TaskState.GENERATING: {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.WAITING: {TaskState.EXECUTING, TaskState.CANCELLED, TaskState.FAILED},
}


@dataclass
class Task:
    raw_request: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    route: Optional[Route] = None
    state: TaskState = TaskState.CREATED
    authorization_state: str = "not_required"
    selected_context: list[dict[str, Any]] = field(default_factory=list)
    tool_executions: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: Optional[str] = None
    reason_category: Optional[str] = None

    def transition(self, state: TaskState) -> None:
        if state not in TRANSITIONS.get(self.state, set()):
            raise ValueError(f"invalid task transition: {self.state.value} -> {state.value}")
        self.state = state
        self.updated_at = time.time()


@dataclass(frozen=True)
class SearchResult:
    source: str
    snippet: str
    score: float = 1.0
    line: Optional[int] = None
    method: str = "unknown"
