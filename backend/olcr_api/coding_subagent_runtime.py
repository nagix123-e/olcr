"""Host-owned coordination for one approved Coding Task delegation group."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time
import uuid
from typing import Any, Mapping

from .coding_subagent_executor import (
    CodingSubagentExecutor, CodingSubagentFileContext, CodingSubagentRequest,
    CodingSubagentResult,
)
from .coding_subagents import (
    CodingSubagentModelBinding, UserDirectiveEvidence,
    assess_delegation, authorize_delegation, delegation_proposal_hash,
    pin_subagent_model, select_independent_workstreams,
)
from .task_workspace import WorkspaceError


@dataclass(frozen=True)
class CodingSubagentCoordination:
    status: str
    reason: str
    proposal: Mapping[str, Any] | None = None
    results: tuple[CodingSubagentResult, ...] = ()


class CodingSubagentCoordinator:
    """Apply policy and persist lifecycle before calling the bounded executor."""

    def __init__(self, db, workspaces, executor: CodingSubagentExecutor):
        self.db = db
        self.workspaces = workspaces
        self.executor = executor

    @staticmethod
    def _worker_records(assessment, selected, plan, role_ids):
        records = []
        for stream in selected:
            role_id = "implementation_worker"
            worker_id = "subagent-" + uuid.uuid4().hex
            records.append({
                "worker_id": worker_id, "phase_id": stream.phase_id,
                "role_id": role_id, "prompt_version": "2026-11-v1",
                "assigned_paths": list(stream.assigned_paths),
            })
        return records

    def _requests(self, task: Mapping[str, Any], plan: Mapping[str, Any],
                  proposal: Mapping[str, Any]) -> tuple[CodingSubagentRequest, ...]:
        phases = {str(item.get("id")): item for item in plan.get("phases", [])
                  if isinstance(item, Mapping)}
        binding = proposal.get("model_binding")
        if not isinstance(binding, Mapping):
            raise ValueError("invalid persisted Coding Task model binding")
        requests = []
        for worker in proposal.get("workers", []):
            phase_id = str(worker.get("phase_id") or "")
            phase = phases.get(phase_id)
            if phase is None:
                raise WorkspaceError("STALE_PLAN")
            rows = self.workspaces.read_coding_subagent_files(
                task["id"], phase_id, int(proposal["plan_revision"]),
                list(worker.get("assigned_paths") or []))
            files = tuple(CodingSubagentFileContext(
                path=item["path"], exists=bool(item["exists"]), sha256=item.get("sha256"),
                mode=item.get("mode"), content=item.get("content")) for item in rows)
            criteria = tuple(str(value)[:500] for value in
                             [*(phase.get("done") or []), *(phase.get("verify") or [])]
                             if isinstance(value, str))
            requests.append(CodingSubagentRequest(
                task_id=str(task["id"]), phase_id=phase_id,
                plan_revision=int(proposal["plan_revision"]), worker_id=str(worker["id"]),
                role_id=str(worker["role_id"]), model_binding=dict(binding),
                original_goal=str(task.get("original_goal") or "")[:20_000],
                phase_goal=str(phase.get("goal") or "")[:8_000],
                acceptance_criteria=criteria, files=files,
            ))
        return tuple(requests)

    def _create_proposal(self, task: Mapping[str, Any], phase: Mapping[str, Any],
                         plan: Mapping[str, Any], assessment, binding: Mapping[str, Any],
                         selected, direct_evidence: UserDirectiveEvidence | None,
                         now: float) -> dict[str, Any]:
        role_ids = ["implementation_worker"]
        model_binding = CodingSubagentModelBinding(**dict(binding))
        proposal_hash = delegation_proposal_hash(assessment, model_binding, tuple(role_ids), selected)
        records = self._worker_records(assessment, selected, plan, role_ids)
        authorization = None
        if direct_evidence is not None:
            minted = authorize_delegation(
                assessment, task_id=str(task["id"]), model_binding=model_binding,
                user_directive=direct_evidence, role_ids=tuple(role_ids), workstreams=tuple(selected))
            authorization = {
                "task_id": minted.task_id, "source": minted.source,
                "source_id": minted.source_id, "plan_revision": minted.plan_revision,
                "plan_hash": minted.plan_hash, "proposal_hash": proposal_hash,
                "model_binding": dict(binding), "role_ids": list(minted.role_ids),
                "workstream_scopes": [
                    {"phase_id": item.phase_id, "assigned_paths": list(item.assigned_paths)}
                    for item in selected
                ],
            }
        return self.db.create_coding_subagent_proposal(
            "subagent-proposal-" + uuid.uuid4().hex, task_id=str(task["id"]),
            phase_id=str(phase["id"]), plan_revision=assessment.plan_revision,
            plan_hash=assessment.plan_hash, assessment_hash=assessment.assessment_hash,
            proposal_hash=proposal_hash, model_binding=dict(binding), role_ids=role_ids,
            workers=records, now=now, setting_enabled=True, authorization=authorization)

    def coordinate(self, task: Mapping[str, Any], phase: Mapping[str, Any], *,
                   plan: Mapping[str, Any], plan_revision: int, effective_model: str,
                   brain_configuration: Mapping[str, Any], setting_enabled: bool,
                   direct_evidence: UserDirectiveEvidence | None = None,
                   completed_phase_ids: set[str] | frozenset[str] | None = None,
                   now: float | None = None) -> CodingSubagentCoordination:
        if type(setting_enabled) is not bool or not setting_enabled:
            return CodingSubagentCoordination("SKIPPED", "SETTING_OFF")
        if phase.get("execution_mode") == "VERIFICATION_ONLY":
            return CodingSubagentCoordination("SKIPPED", "VERIFICATION_ONLY")
        task_id = str(task.get("id") or "")
        assessment = assess_delegation(plan, task_id=task_id,
            plan_revision=plan_revision, setting_enabled=True)
        if not assessment.eligible:
            return CodingSubagentCoordination("SKIPPED", assessment.reason)
        completed = set(completed_phase_ids or ())
        ready = {str(item.get("id")) for item in plan.get("phases", [])
                 if isinstance(item, Mapping)
                 and isinstance(item.get("id"), str)
                 and all(dependency in completed for dependency in item.get("dependencies", []))}
        # The parent owns the currently dispatched phase. Children receive only
        # other ready workstreams, so neither parent and child nor siblings race
        # on the same phase or bypass an unfinished prerequisite.
        ready.discard(str(phase.get("id") or ""))
        selected = select_independent_workstreams(assessment, eligible_phase_ids=ready)
        if len(selected) < 2:
            return CodingSubagentCoordination("SKIPPED", "NO_INDEPENDENT_WORKER_GROUP")
        try:
            binding = asdict(pin_subagent_model(effective_model, brain_configuration))
        except (KeyError, TypeError, ValueError):
            return CodingSubagentCoordination("SKIPPED", "CODING_MODEL_UNAVAILABLE")
        timestamp = time.time() if now is None else float(now)
        role_ids = ["implementation_worker"]
        model_binding = CodingSubagentModelBinding(**binding)
        proposal_hash = delegation_proposal_hash(assessment, model_binding, tuple(role_ids), selected)

        considered = self.db.get_coding_subagent_proposal_for_revision(task_id, plan_revision)
        if (considered and considered.get("plan_hash") == assessment.plan_hash
                and considered.get("phase_id") != str(phase["id"])):
            return CodingSubagentCoordination("SKIPPED", "PLAN_DELEGATION_ALREADY_CONSIDERED", considered)

        active = self.db.get_active_coding_subagent_proposal(
            task_id, plan_revision, str(phase["id"]))
        proposal = None
        if active and active["plan_hash"] != assessment.plan_hash:
            active = None
        if active and active["status"] == "AWAITING_USER_APPROVAL":
            if active["model_binding"] != binding:
                self.db.invalidate_pending_coding_subagent_model(
                    task_id, plan_revision, assessment.plan_hash, binding, timestamp)
                active = None
            elif direct_evidence is None:
                return CodingSubagentCoordination("AWAITING_USER_APPROVAL", "USER_APPROVAL_REQUIRED", active)
            else:
                proposal = self._create_proposal(task, phase, plan, assessment, binding,
                    selected, direct_evidence, timestamp)
                active = None
        else:
            proposal = None
        if active and active["status"] in {"AUTHORIZED", "QUEUED"}:
            proposal = active
        elif active and active["status"] == "RUNNING":
            return CodingSubagentCoordination("IN_PROGRESS", "WORKERS_ALREADY_RUNNING", active)
        elif active:
            return CodingSubagentCoordination("TERMINAL", active["status"], active)
        elif active is None and proposal is None:
            proposal = self._create_proposal(task, phase, plan, assessment, binding, selected,
                direct_evidence, timestamp)
        if proposal["status"] == "AWAITING_USER_APPROVAL":
            return CodingSubagentCoordination("AWAITING_USER_APPROVAL", "USER_APPROVAL_REQUIRED", proposal)
        if proposal["status"] not in {"AUTHORIZED", "QUEUED"}:
            return CodingSubagentCoordination("TERMINAL", proposal["status"], proposal)

        if proposal["status"] == "AUTHORIZED":
            proposal = self.db.transition_coding_subagent_proposal(
                proposal["id"], expected_status="AUTHORIZED", new_status="QUEUED", now=timestamp)
            for worker in proposal["workers"]:
                self.db.transition_coding_subagent_run(worker["id"], expected_status="AUTHORIZED",
                    new_status="QUEUED", now=timestamp)
        proposal = self.db.transition_coding_subagent_proposal(
            proposal["id"], expected_status="QUEUED", new_status="RUNNING", now=timestamp)
        for worker in proposal["workers"]:
            self.db.transition_coding_subagent_run(worker["id"], expected_status="QUEUED",
                new_status="RUNNING", now=timestamp)

        try:
            requests = self._requests(task, plan, proposal)
            results = self.executor.run_group(requests)
        except GenerationCancelled:
            raise
        except Exception as exc:
            category = "SCOPE_REJECTED" if isinstance(exc, WorkspaceError) else "MODEL_ERROR"
            for worker in proposal["workers"]:
                try:
                    self.db.transition_coding_subagent_run(worker["id"], expected_status="RUNNING",
                        new_status="FAILED", now=time.time(), error_category=category)
                except ValueError:
                    pass
            try:
                self.db.transition_coding_subagent_proposal(proposal["id"], expected_status="RUNNING",
                    new_status="FAILED", now=time.time())
            except ValueError:
                pass
            return CodingSubagentCoordination("FAILED", category, proposal)

        persisted_group = self.db.get_coding_subagent_proposal(proposal["id"], task_id)
        if not self.executor.enabled or (persisted_group and persisted_group.get("status") == "CANCELLED"):
            # A global setting-off cancels children but leaves the parent task
            # alive. Continue through its ordinary single-agent implementation.
            return CodingSubagentCoordination("SKIPPED", "SETTING_OFF_DURING_RUN", persisted_group)

        persisted_results = []
        all_succeeded = True
        for worker, result in zip(proposal["workers"], results, strict=True):
            current_group = self.db.get_coding_subagent_proposal(proposal["id"], task_id) or {}
            if current_group.get("status") == "CANCELLED" or not self.executor.enabled:
                return CodingSubagentCoordination("SKIPPED", "SETTING_OFF_DURING_APPLY",
                                                  current_group, tuple(persisted_results))
            error_category = result.error_category
            summary = result.summary
            if error_category is None:
                try:
                    mutations = [asdict(item) for item in result.proposed_mutations]
                    if mutations:
                        self.workspaces.apply_coding_subagent_mutations(
                            task_id, worker["id"], mutations, now=time.time())
                except WorkspaceError as exc:
                    reason = str(exc)
                    error_category = ("SCOPE_REJECTED" if reason.startswith(("SCOPE", "ROLE"))
                                      else "CONFLICT" if reason in {"PREIMAGE_CHANGED", "STALE_PLAN"}
                                      else "INVALID_RESULT")
                    summary = None
            if error_category is None:
                self.db.transition_coding_subagent_run(worker["id"], expected_status="RUNNING",
                    new_status="SUCCEEDED", now=time.time(), summary=summary,
                    usage=dict(result.usage or {}))
            else:
                all_succeeded = False
                target_status = "CANCELLED" if error_category == "CANCELLED" else (
                    "TIMED_OUT" if error_category == "TIMEOUT" else "FAILED")
                self.db.transition_coding_subagent_run(worker["id"], expected_status="RUNNING",
                    new_status=target_status, now=time.time(), usage=dict(result.usage or {}),
                    error_category=error_category)
            persisted_results.append(CodingSubagentResult(
                worker_id=worker["id"], summary=summary,
                findings=result.findings if error_category is None else (),
                proposed_mutations=result.proposed_mutations if error_category is None else (),
                usage=result.usage, error_category=error_category))
        final_status = "SUCCEEDED" if all_succeeded else "FAILED"
        current = self.db.get_coding_subagent_proposal(proposal["id"], task_id) or proposal
        if current.get("status") == "CANCELLED" or not self.executor.enabled:
            return CodingSubagentCoordination("SKIPPED", "SETTING_OFF_DURING_APPLY", current,
                                              tuple(persisted_results))
        self.db.transition_coding_subagent_proposal(current["id"], expected_status="RUNNING",
            new_status=final_status, now=time.time())
        final = self.db.get_coding_subagent_proposal(proposal["id"], task_id)
        return CodingSubagentCoordination(final_status, "WORKERS_FINISHED", final,
                                          tuple(persisted_results))
