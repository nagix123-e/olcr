"""Typed phase boundary for model hand-offs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    IMPLEMENTATION = "IMPLEMENTATION"
    VERIFICATION_ONLY = "VERIFICATION_ONLY"
    TOOL_ONLY = "TOOL_ONLY"
    RETRIEVAL_ONLY = "RETRIEVAL_ONLY"
    VISION_ONLY = "VISION_ONLY"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    REPORT_ONLY = "REPORT_ONLY"
    FINISH = "FINISH"


@dataclass
class InvocationBoundary:
    task_id: str
    phase_id: str = ""
    state: str = "IDLE"
    events: list[str] = field(default_factory=list)

    def stop(self) -> None:
        self.state = "STOPPED"
        self.events.append("STOP")

    def commit_evidence(self) -> None:
        if self.state != "STOPPED":
            raise RuntimeError("INVOCATION_BOUNDARY_EVIDENCE_BEFORE_STOP")
        self.state = "EVIDENCE_COMMITTED"
        self.events.append("EVIDENCE_COMMITTED")

    def checkpoint(self) -> None:
        if self.state != "EVIDENCE_COMMITTED":
            raise RuntimeError("INVOCATION_BOUNDARY_CHECKPOINT_ORDER")
        self.state = "CHECKPOINT_SAVED"
        self.events.append("CHECKPOINT_SAVED")

    def finalize_telemetry(self) -> None:
        if self.state != "CHECKPOINT_SAVED":
            raise RuntimeError("INVOCATION_BOUNDARY_TELEMETRY_ORDER")
        self.state = "TELEMETRY_FINALIZED"
        self.events.append("TELEMETRY_FINALIZED")

    def next_invocation(self, task_id: str, phase_id: str) -> None:
        if task_id != self.task_id:
            raise RuntimeError("INVOCATION_BOUNDARY_TASK_ID_CHANGED")
        if self.state != "TELEMETRY_FINALIZED":
            raise RuntimeError("INVOCATION_BOUNDARY_NEXT_BEFORE_FINALIZE")
        self.phase_id = phase_id
        self.state = "NEXT_INVOCATION"
        self.events.append("NEXT_INVOCATION")

    def public_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "phase_id": self.phase_id,
                "state": self.state, "events": list(self.events),
                "invariants_pass": self.events == ["STOP", "EVIDENCE_COMMITTED",
                                                    "CHECKPOINT_SAVED", "TELEMETRY_FINALIZED",
                                                    "NEXT_INVOCATION"]}
