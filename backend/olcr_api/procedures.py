from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from .auth import AuthorizationPolicy
from .tools import Tool


@dataclass(frozen=True)
class Procedure:
    id: str
    version: str
    name: str
    required_inputs: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]
    applicability: str
    validated: bool


class ProcedureRunner:
    def __init__(self, tools: dict[str, Tool], policy: AuthorizationPolicy): self.tools, self.policy = tools, policy
    def run(self, procedure: Procedure, parameters: dict[str, Any], approved: bool = False) -> list[dict[str, Any]]:
        if not procedure.validated: raise ValueError("procedure is not validated")
        missing = set(procedure.required_inputs) - set(parameters)
        if missing: raise ValueError(f"missing inputs: {sorted(missing)}")
        observations = []
        for step in procedure.steps:
            tool = self.tools.get(step["tool"])
            if not tool: raise ValueError(f"unknown tool: {step['tool']}")
            decision = self.policy.decide(tool.risk, approved)
            if not decision.allowed: raise PermissionError(decision.state)
            bound = {key: parameters[value[1:]] if isinstance(value, str) and value.startswith("$") else value for key, value in step["input"].items()}
            output, latency = tool.run(bound)
            observations.append({"tool": tool.name, "input": bound, "output": output, "latency_ms": latency})
        return observations


LOWERCASE_PROCEDURE = Procedure("builtin.lowercase", "1.0", "Lowercase text", ("text",), ({"tool": "lowercase", "input": {"text": "$text"}},), "text is supplied", True)

