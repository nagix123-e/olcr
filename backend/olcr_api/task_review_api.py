"""HTTP controls for local coding task review and execution preferences."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .git_workspace import GitError
from .task_workspace import WorkspaceError


class WorkspacePreference(BaseModel):
    mode: Literal['direct', 'worktree']


class ReviewRevision(BaseModel):
    revision: str = Field(min_length=1, max_length=64)


class RevertFile(ReviewRevision):
    path: str = Field(min_length=1, max_length=4096)


class ApplyFiles(ReviewRevision):
    paths: list[str] = Field(min_length=1, max_length=500)


class SteeringInput(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    plan_revision: int = Field(ge=0)


class InstructionUpdate(BaseModel):
    action: Literal['edit', 'cancel', 'send_now', 'retry']
    expected_message: str = Field(min_length=1, max_length=20_000)
    expected_status: Literal['WAITING', 'QUEUED', 'NEEDS_REVIEW']
    message: str | None = Field(default=None, min_length=1, max_length=20_000)


class PlanApproval(BaseModel):
    plan_revision: int = Field(ge=0)


class WorkspaceSetup(BaseModel):
    action: Literal['direct', 'initialize', 'retry']


class TaskReviewAPI:
    def __init__(self, integration, wake):
        self.integration = integration
        self.wake = wake
        self.router = APIRouter(prefix='/api')
        routes = [
            ('/projects/{project_id}/coding-workspace', self.preference, ['GET']),
            ('/projects/{project_id}/coding-workspace', self.set_preference, ['PUT']),
            ('/conversations/{conversation_id}/coding-work', self.list_work, ['GET']),
            ('/projects/{project_id}/coding-work', self.project_work, ['GET']),
            ('/coding-tasks/{task_id}/changes', self.changes, ['GET']),
            ('/coding-tasks/{task_id}/review', self.review, ['GET']),
            ('/coding-tasks/{task_id}/changes/revert', self.revert, ['POST']),
            ('/coding-tasks/{task_id}/changes/apply', self.apply, ['POST']),
            ('/coding-tasks/{task_id}/steer', self.steer, ['POST']),
            ('/coding-tasks/{task_id}/instructions', self.instructions, ['GET']),
            ('/coding-tasks/{task_id}/instructions/{instruction_id}', self.update_instruction, ['PATCH']),
            ('/coding-tasks/{task_id}/plan/approve', self.approve_plan, ['POST']),
            ('/coding-tasks/{task_id}/workspace/setup', self.setup_workspace, ['POST']),
            ('/coding-tasks/{task_id}/worktree/remove', self.remove, ['POST']),
            ('/coding-tasks/{task_id}/worktree/restore', self.restore, ['POST']),
        ]
        for path, endpoint, methods in routes:
            self.router.add_api_route(path, endpoint, methods=methods)

    @property
    def service(self):
        return self.integration.service

    def call(self, method, *args):
        try:
            return method(*args)
        except (WorkspaceError, GitError, OSError) as exc:
            raise HTTPException(404 if str(exc) in {'TASK_NOT_FOUND', 'PROJECT_NOT_FOUND'} else 409, str(exc)) from None

    def preference(self, project_id: str):
        return self.call(self.service.preference, project_id)

    def set_preference(self, project_id: str, value: WorkspacePreference):
        return self.call(self.service.preference, project_id, value.mode)

    def list_work(self, conversation_id: str):
        if not self.service.db.conversation(conversation_id):
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        return {'tasks': [{**t, 'workspace': self.service.metadata(t['id'])} for t in self.service.db.coding_tasks(conversation_id)]}

    def changes(self, task_id: str):
        return self.call(self.service.changes, task_id)

    def project_work(self, project_id: str):
        self.call(self.service.preference, project_id)
        with self.service.db.connect() as conn:
            rows = [dict(row) for row in conn.execute('''
                SELECT ct.id,ct.conversation_id,ct.original_goal,ct.status,ct.current_phase_id,ct.updated_at
                FROM coding_tasks ct JOIN conversations c ON c.id=ct.conversation_id
                WHERE c.project_id=? AND ct.archived=0 ORDER BY ct.updated_at DESC LIMIT 100
            ''', (project_id,))]
        return {'tasks': [{**row, 'workspace': self.service.metadata(row['id'])} for row in rows]}

    def review(self, task_id: str):
        return {**self.changes(task_id), 'steering': self.call(self.service.steering, task_id),
                'reports': self.service.db.coding_phase_reports(task_id)}

    def revert(self, task_id: str, value: RevertFile):
        return self.call(self.service.revert, task_id, value.path, value.revision)

    def apply(self, task_id: str, value: ApplyFiles):
        return self.call(self.service.apply, task_id, value.revision, value.paths)

    def steer(self, task_id: str, value: SteeringInput):
        rows = self.call(self.service.steer, task_id, value.message, value.plan_revision)
        task = self.service.task(task_id)
        if task['status'] in {'RESUMABLE', 'BLOCKED', 'WAITING_FOR_USER'} and not task.get('pending_authorization'):
            self.service.db.enqueue_coding_task(task_id)
            self.wake()
        return {'steering': rows}

    def instructions(self, task_id: str):
        return {'steering': self.call(self.service.steering, task_id)}

    def update_instruction(self, task_id: str, instruction_id: str, value: InstructionUpdate):
        row = self.call(self.service.update_instruction, task_id, instruction_id, value.action,
                        value.expected_message, value.expected_status, value.message)
        if value.action in {'send_now', 'retry'}:
            task = self.service.task(task_id)
            if task['status'] in {'RESUMABLE', 'BLOCKED', 'WAITING_FOR_USER'} and not task.get('pending_authorization'):
                self.service.db.enqueue_coding_task(task_id)
            self.wake()
        return row

    def approve_plan(self, task_id: str, value: PlanApproval):
        self.call(self.service.task, task_id)
        try:
            result = self.service.db.approve_coding_plan(task_id, value.plan_revision)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        self.wake()
        return result

    def remove(self, task_id: str):
        return self.call(self.service.archive_worktree, task_id)

    def setup_workspace(self, task_id: str, value: WorkspaceSetup):
        result = self.call(self.service.setup, task_id, value.action)
        self.wake()
        return result

    def restore(self, task_id: str):
        return self.call(self.service.restore_worktree, task_id)
