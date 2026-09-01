from __future__ import annotations

from dataclasses import dataclass
from .models import Risk


@dataclass(frozen=True)
class Decision:
    allowed: bool
    state: str
    reason: str


class AuthorizationPolicy:
    def decide(self, risk: Risk, approved: bool = False) -> Decision:
        if risk is Risk.SAFE:
            return Decision(True, "authorized", "safe_operation")
        if risk is Risk.CONFIRM:
            return Decision(approved, "authorized" if approved else "waiting_for_confirmation", "explicit_approval_required")
        return Decision(False, "blocked", "deny_default")

