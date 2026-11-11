from __future__ import annotations

from dataclasses import dataclass
from .models import Risk


@dataclass(frozen=True)
class Decision:
    allowed: bool
    state: str
    reason: str


class AuthorizationPolicy:
    def __init__(self, get_level=lambda: "automatic"):
        self.get_level = get_level

    def decide(self, risk: Risk, approved: bool = False) -> Decision:
        if self.get_level() == "full":
            return Decision(True, "authorized", "full_access")
        if risk is Risk.SAFE:
            return Decision(True, "authorized", "safe_operation")
        if risk is Risk.CONFIRM:
            return Decision(approved, "authorized" if approved else "waiting_for_confirmation", "explicit_approval_required")
        return Decision(False, "blocked", "deny_default")

