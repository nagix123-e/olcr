from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import re
import time
import unicodedata
import uuid
from typing import Any, Callable
from .auth import AuthorizationPolicy
from .tools import Tool


def routine_goal_signature(question: str) -> str:
    """Stable exact-intent key for repeated, explicitly reviewed routine goals."""
    normalized = unicodedata.normalize("NFKC", str(question or "")).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip().strip("。.．.!！?？")
    if not normalized or len(normalized) > 1000:
        raise ValueError("routine goal is empty or outside the bounded matcher")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Procedure:
    id: str
    version: str
    name: str
    required_inputs: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]
    applicability: str
    validated: bool
    environment_signature: str = ""
    preconditions: tuple[dict[str, Any], ...] = ()
    status: str = "ACTIVE"
    captured_trace: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    review_history: tuple[dict[str, Any], ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {"id":self.id,"version":self.version,"name":self.name,
                "required_inputs":list(self.required_inputs),"steps":list(self.steps),
                "applicability":self.applicability,"validated":self.validated,
                "environment_signature":self.environment_signature,
                "preconditions":list(self.preconditions),"status":self.status,
                "captured_trace":self.captured_trace,"review":self.review,
                "review_history":list(self.review_history)}

    @classmethod
    def from_record(cls, value: dict[str, Any]) -> "Procedure":
        review_history = value.get("review_history")
        if not isinstance(review_history, list):
            review_history = []
        return cls(str(value["id"]),str(value["version"]),str(value["name"]),
                   tuple(str(item) for item in value.get("required_inputs",[])),
                   tuple(item for item in value.get("steps",[]) if isinstance(item,dict)),
                   str(value.get("applicability") or ""),bool(value.get("validated")),
                   str(value.get("environment_signature") or ""),
                   tuple(item for item in value.get("preconditions",[]) if isinstance(item,dict)),
                   str(value.get("status") or "ACTIVE"),
                   value.get("captured_trace") if isinstance(value.get("captured_trace"),dict) else None,
                   value.get("review") if isinstance(value.get("review"),dict) else None,
                   tuple(item for item in review_history if isinstance(item, dict))[-16:])


class ProcedureStore:
    """Adapter to the existing SQLite procedures table."""
    def __init__(self, database: Any):self.database=database
    def save(self, procedure: Procedure) -> None:self.database.save_procedure(procedure.to_record())
    def get(self, procedure_id: str) -> Procedure | None:
        record=self.database.procedure(procedure_id)
        return Procedure.from_record(record) if record else None
    def list(self, *, active_only: bool = True) -> list[Procedure]:
        return [Procedure.from_record(item) for item in self.database.procedures(active_only=active_only)]
    def record_run(self, procedure: Procedure, success: bool, observations: list[dict[str, Any]]) -> None:
        self.database.record_procedure_run(procedure.id,success,observations)

    def capture_verified_trace(self, trace: dict[str, Any]) -> Procedure:
        """Store a bounded, non-executable trace in the existing Procedure table."""
        verification = trace.get("verification") if isinstance(trace, dict) else None
        action = trace.get("action") if isinstance(trace, dict) else None
        if not isinstance(verification, dict) or verification.get("result") != "CONFIRMED":
            raise ValueError("only confirmed traces can be captured")
        if not isinstance(action, dict) or action.get("kind") not in {"open_disclosure", "open_find"}:
            raise ValueError("action is not eligible for trace capture")
        if len(json.dumps(trace, ensure_ascii=False).encode("utf-8")) > 16_384:
            raise ValueError("trace exceeds size limit")
        captured = [item for item in self.database.procedures(active_only=False)
                    if item.get("status") == "CAPTURED"]
        if len(captured) >= 64:
            raise ValueError("captured trace capacity reached")
        procedure = Procedure(
            id=f"routine.trace.{uuid.uuid4()}", version="1", name=f"Captured {action['kind']}",
            required_inputs=(), steps=(), applicability=f"Captured verified {action['kind']} action",
            validated=False, environment_signature="", preconditions=(), status="CAPTURED",
            captured_trace={**trace, "captured_at": time.time()},
        )
        self.save(procedure)
        return procedure

    def compile_candidate(self, trace_ids: tuple[str, ...] | list[str]) -> Procedure:
        """Compile repeated identical, confirmed safe traces into a non-runnable draft."""
        ids = tuple(dict.fromkeys(str(item) for item in trace_ids if str(item)))
        if len(ids) < 2 or len(ids) > 8:
            raise ValueError("two to eight distinct verified traces are required")
        existing_records = self.database.procedures(active_only=False)
        existing_candidates = [item for item in existing_records
                               if item.get("status") in {"CANDIDATE", "SUSPECT"}]
        if len(existing_candidates) >= 64:
            raise ValueError("routine candidate capacity reached")
        if any(set((item.get("captured_trace") or {}).get("source_trace_ids", [])) == set(ids)
               for item in existing_records
               if isinstance(item.get("captured_trace"), dict)):
            raise ValueError("source traces already compiled")
        source_records = [self.get(item) for item in ids]
        if any(item is None or item.status != "CAPTURED" or item.captured_trace is None
               for item in source_records):
            raise ValueError("source trace is unavailable or already compiled")
        traces = [item.captured_trace for item in source_records if item and item.captured_trace]
        first = traces[0]
        action = first.get("action")
        context = first.get("context")
        effects = first.get("expected_effects")
        actions = first.get("actions")
        preconditions = first.get("preconditions")
        if (not isinstance(action, dict) or action.get("kind") not in {"open_disclosure", "open_find"}
                or not isinstance(context, dict) or not isinstance(preconditions, list) or len(preconditions) != 1
                or not isinstance(effects, list) or len(effects) != 1
                or not isinstance(actions, list) or len(actions) != 1
                or not isinstance(actions[0], dict)
                or actions[0].get("kind") != action.get("kind")):
            raise ValueError("trace is outside the bounded routine compiler")
        route = actions[0]
        if route.get("mechanism") != action.get("mechanism"):
            raise ValueError("trace is outside the bounded routine compiler")
        if action["kind"] == "open_disclosure":
            expected_mechanism = "native_structured" if context.get("source") == "DOM" else "accessibility"
            if route.get("mechanism") != expected_mechanism:
                raise ValueError("trace is outside the bounded routine compiler")
        else:
            shortcut = route.get("shortcut")
            if (context.get("source") != "AX" or route.get("mechanism") != "keyboard_shortcut"
                    or not isinstance(shortcut, dict) or shortcut.get("provenance") != "application_menu"
                    or not isinstance(shortcut.get("key_code"), int)
                    or isinstance(shortcut.get("key_code"), bool)
                    or not 0 <= shortcut["key_code"] <= 127
                    or not isinstance(shortcut.get("modifiers"), int)
                    or isinstance(shortcut.get("modifiers"), bool)
                    or not 0 <= shortcut["modifiers"] <= 15
                    or not isinstance(shortcut.get("key"), str) or not shortcut["key"]):
                raise ValueError("trace is outside the bounded routine compiler")
        if context.get("source") == "AX":
            bundle_id = context.get("application_bundle_id")
            if not isinstance(bundle_id, str) or not bundle_id.strip() or len(bundle_id) > 255:
                raise ValueError("trace is outside the bounded routine compiler")
            # PID and AX session IDs protect an in-flight action, but change on
            # relaunch. The stable bundle ID is the application component of
            # the routine signature; fresh PID/session checks stay in the
            # action plan and are never relaxed by routine matching.
            context_key = {"source": "AX", "application_bundle_id": bundle_id,
                           "coordinate_space": context.get("coordinate_space")}
        elif context.get("source") == "DOM":
            context_key = {"source": "DOM", "session_id": context.get("session_id"),
                           "coordinate_space": context.get("coordinate_space")}
        else:
            raise ValueError("trace is outside the bounded routine compiler")
        target = preconditions[0]
        for trace in traces:
            verification = trace.get("verification")
            trace_context = trace.get("context")
            if (not isinstance(verification, dict) or verification.get("result") != "CONFIRMED"
                    or trace.get("goal_signature") != first.get("goal_signature")
                    or trace.get("action") != action or trace.get("expected_effects") != effects
                    or trace.get("actions") != actions or trace.get("preconditions") != preconditions
                    or not isinstance(trace_context, dict)
                    or any(trace_context.get(key) != value for key, value in context_key.items())):
                raise ValueError("traces do not establish the same bounded action")
        effect = effects[0]
        tool = f"olcr.gui.{action['kind']}"
        # Keep the bounded route details in the declarative step. In particular,
        # Find depends on app-menu provenance for its shortcut; dropping that
        # evidence would turn the candidate into an incomplete action recipe.
        step = {"tool": tool, "input": {"target": target},
                "declarative_action": actions[0], "expected_effect": effect}
        environment_signature = hashlib.sha256(json.dumps(
            context_key, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        scope = "application" if context_key["source"] == "AX" else "session"
        candidate_trace = {"compiler_version": 1, "source_trace_ids": list(ids),
                           "source_trace_count": len(ids), "goal_signature": first["goal_signature"],
                           "goal_signature_version": first.get("goal_signature_version"),
                           "context": context_key, "action": action, "actions": actions,
                           "expected_effects": effects,
                           "verification_count": len(traces),
                           "verification_provenance": [trace.get("provenance", {}) for trace in traces]}
        procedure = Procedure(
            id=f"routine.candidate.{uuid.uuid4()}", version="1",
            name=f"Candidate {action['kind']}", required_inputs=(), steps=(step,),
            applicability=f"Same {context.get('source')} {scope} and observed target only",
            validated=False, environment_signature=environment_signature,
            preconditions=tuple(preconditions), status="CANDIDATE", captured_trace=candidate_trace)
        self.save(procedure)
        return procedure

    def review_candidate(self, candidate_id: str, decision: str) -> Procedure:
        """Persist an explicit human review without making a candidate runnable."""
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("review decision must be APPROVED or REJECTED")
        candidate = self.get(candidate_id)
        if (candidate is None or candidate.status != "CANDIDATE"
                or candidate.captured_trace is None
                or not isinstance(candidate.captured_trace.get("source_trace_ids"), list)):
            raise ValueError("routine candidate is unavailable for review")
        review = {"decision": decision, "reviewed_at": time.time(),
                  "review_scope": "local_gui_session"}
        review_history = (*candidate.review_history, review)[-16:]
        # Approval is a review outcome only. Promotion requires a registered,
        # verified executor path and is deliberately a separate transition.
        reviewed = Procedure(**{**candidate.__dict__, "review": review,
                                "review_history": review_history,
                                "validated": False, "status": "CANDIDATE"})
        self.save(reviewed)
        return reviewed

    def match_candidates(self, question: str, world_record: dict[str, Any], *,
                         now: float | None = None, max_age_seconds: float = 30.0) -> list[dict[str, Any]]:
        """Find approved candidates whose exact goal, environment and precondition match fresh World State.

        This produces a read-only match proposal. It never marks a Procedure active
        and never dispatches its declarative action.
        """
        goal_signature = routine_goal_signature(question)
        current_time = time.time() if now is None else float(now)
        if not isinstance(world_record, dict) or not isinstance(world_record.get("state"), dict):
            return []
        state = world_record["state"]
        observed_at = state.get("observed_at")
        if (not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool)
                or not (0 <= current_time - float(observed_at) <= max_age_seconds)
                or observed_at > current_time + 2):
            return []
        source = str(state.get("source") or "").upper()
        if source not in {"AX", "DOM"}:
            return []
        coordinate_space = state.get("coordinate_space")
        active_window = state.get("active_window") if isinstance(state.get("active_window"), dict) else {}
        context_key = ({"source": "AX", "application_bundle_id": active_window.get("application_bundle_id"),
                        "coordinate_space": coordinate_space} if source == "AX" else
                       {"source": "DOM", "session_id": world_record.get("session_id"),
                        "coordinate_space": coordinate_space})
        environment_signature = hashlib.sha256(json.dumps(
            context_key, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        elements = state.get("ui_elements")
        if not isinstance(elements, list):
            return []
        matches = []
        for record in self.database.procedures(active_only=False):
            if record.get("status") != "CANDIDATE" or record.get("validated") is True:
                continue
            candidate = self.get(str(record.get("id") or ""))
            evidence = candidate.captured_trace if candidate else None
            action = evidence.get("action") if isinstance(evidence, dict) else None
            actions = evidence.get("actions") if isinstance(evidence, dict) else None
            if (candidate is None or not isinstance(evidence, dict)
                    or candidate.environment_signature != environment_signature
                    or evidence.get("goal_signature") != goal_signature
                    or evidence.get("goal_signature_version") != 1
                    or not isinstance(candidate.review, dict)
                    or candidate.review.get("decision") != "APPROVED"
                    or candidate.status != "CANDIDATE" or candidate.validated
                    or not isinstance(action, dict)
                    or action.get("kind") not in {"open_disclosure", "open_find"}
                    or not isinstance(actions, list) or len(actions) != 1
                    or not isinstance(actions[0], dict)
                    or actions[0].get("kind") != action.get("kind")
                    or len(candidate.preconditions) != 1):
                continue
            expected = candidate.preconditions[0]
            if (not expected or any(key not in {"role", "subrole", "label", "enabled", "expanded", "focused", "selected"}
                                    for key in expected)
                    or any(not isinstance(value, (str, int, float, bool, type(None)))
                           for value in expected.values())):
                continue
            observed = [element for element in elements if isinstance(element, dict)
                        and all(element.get(key) == value for key, value in expected.items())]
            if len(observed) != 1:
                continue
            element_id = observed[0].get("id", observed[0].get("element_id"))
            if not isinstance(element_id, str) or not element_id:
                continue
            matches.append({"procedure_id": candidate.id, "name": candidate.name,
                            "action": action["kind"], "mechanism": actions[0].get("mechanism"),
                            "element_id": element_id, "observation_id": state.get("observation_id"),
                            "session_id": world_record.get("session_id"),
                            "observed_at": observed_at, "validated": False,
                            "executable": False})
        return matches


class ProcedureVerificationError(RuntimeError):
    def __init__(self, reason: str, observations: list[dict[str, Any]]):
        super().__init__(reason)
        self.reason = reason
        self.observations = observations


class ProcedureRunner:
    def __init__(self, tools: dict[str, Tool], policy: AuthorizationPolicy, *,
                 environment_signature: Callable[[], str] | None = None,
                 precondition_check: Callable[[tuple[dict[str, Any], ...], dict[str, Any]], bool] | None = None,
                 effect_verifier: Callable[[dict[str, Any], Any, dict[str, Any]], bool | dict[str, Any]] | None = None,
                 run_recorder: Callable[[Procedure, bool, list[dict[str, Any]]], None] | None = None):
        self.tools, self.policy = tools, policy
        self.environment_signature = environment_signature
        self.precondition_check = precondition_check
        self.effect_verifier = effect_verifier
        self.run_recorder = run_recorder

    @staticmethod
    def _verification_result(verification: Any) -> str:
        if isinstance(verification, dict):
            if "result" in verification:
                return str(verification.get("result") or "UNKNOWN")
            return "CONFIRMED" if verification.get("verified") is True else "NOT_CONFIRMED"
        if isinstance(verification, bool):
            return "CONFIRMED" if verification else "NOT_CONFIRMED"
        return "INCONCLUSIVE"

    def record_staged_outcome(self, procedure: Procedure, verification: dict[str, Any], *,
                              execution_attempted: bool) -> bool:
        """Finalize a GUI step dispatched by the native executor and verified by its API route.

        GUI actions must stay staged through the frontend/native permission boundary. This
        method lets that path use the existing ProcedureRunner recorder without dispatching
        a second action or treating a plan as execution.
        """
        if (not self.run_recorder or execution_attempted is not True
                or procedure.status != "CANDIDATE" or procedure.validated
                or not isinstance(procedure.review, dict)
                or procedure.review.get("decision") != "APPROVED"
                or len(procedure.steps) != 1):
            return False
        step = procedure.steps[0]
        if (step.get("tool") not in {"olcr.gui.open_disclosure", "olcr.gui.open_find"}
                or not isinstance(step.get("expected_effect"), dict)):
            return False
        result = self._verification_result(verification)
        if result == "CONFIRMED":
            success = True
        elif result == "NOT_CONFIRMED":
            success = False
        else:
            # Ambiguous or unknown results do not count as a routine run.
            return False
        self.run_recorder(procedure, success, [{
            "tool": step["tool"], "verification": {"result": result},
        }])
        return True

    def run(self, procedure: Procedure, parameters: dict[str, Any], approved: bool = False) -> list[dict[str, Any]]:
        if not procedure.validated or procedure.status != "ACTIVE":
            raise ValueError("procedure is not active and validated")
        missing = set(procedure.required_inputs) - set(parameters)
        if missing: raise ValueError(f"missing inputs: {sorted(missing)}")
        if procedure.environment_signature:
            if not self.environment_signature:
                raise ProcedureVerificationError("ENVIRONMENT_SIGNATURE_UNAVAILABLE", [])
            if self.environment_signature() != procedure.environment_signature:
                if self.run_recorder:self.run_recorder(procedure,False,[])
                raise ProcedureVerificationError("ENVIRONMENT_SIGNATURE_MISMATCH", [])
        if procedure.preconditions:
            if not self.precondition_check:
                raise ProcedureVerificationError("PRECONDITION_CHECKER_UNAVAILABLE", [])
            if not self.precondition_check(procedure.preconditions, parameters):
                if self.run_recorder:self.run_recorder(procedure,False,[])
                raise ProcedureVerificationError("PRECONDITION_MISMATCH", [])
        if any(step.get("expected_effect") for step in procedure.steps) and not self.effect_verifier:
            raise ProcedureVerificationError("EFFECT_VERIFIER_UNAVAILABLE", [])
        observations = []
        try:
            for step in procedure.steps:
                tool = self.tools.get(step["tool"])
                if not tool: raise ValueError(f"unknown tool: {step['tool']}")
                decision = self.policy.decide(tool.risk, approved)
                if not decision.allowed: raise PermissionError(decision.state)
                bound = {key: parameters[value[1:]] if isinstance(value, str) and value.startswith("$") else value for key, value in step["input"].items()}
                output, latency = tool.run(bound)
                observation = {"tool": tool.name, "input": bound, "output": output, "latency_ms": latency}
                effect = step.get("expected_effect")
                if effect:
                    verification = self.effect_verifier(effect, output, parameters)
                    result = self._verification_result(verification)
                    confirmed = result == "CONFIRMED"
                    observation["verification"] = (verification if isinstance(verification, dict)
                                                    else {"result": result})
                    if not confirmed:
                        observations.append(observation)
                        raise ProcedureVerificationError("EXPECTED_EFFECT_NOT_CONFIRMED", observations)
                observations.append(observation)
        except Exception:
            if self.run_recorder:self.run_recorder(procedure,False,observations)
            raise
        if self.run_recorder:self.run_recorder(procedure,True,observations)
        return observations


LOWERCASE_PROCEDURE = Procedure("builtin.lowercase", "1.0", "Lowercase text", ("text",), ({"tool": "lowercase", "input": {"text": "$text"}},), "text is supplied", True)
