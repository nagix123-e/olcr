"""Git chat/API adapters and the coding-task completion handoff."""
import json
import shlex
import time
import uuid
import threading
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .git_workspace import GitError, GitWorkspace, coding_git_intent
from .task_workspace import WorkspaceError
from .response_language import response_language
from .permissions import full_access, level


class GitPreparation(BaseModel):
    action: Literal['stage', 'commit', 'push', 'branch', 'switch_branch', 'pull_request']
    paths: list[str] = Field(default_factory=list, max_length=500)
    message: str = Field(default='', max_length=5000)
    remote: str = Field(default='origin', max_length=100)
    branch: str | None = Field(default=None, max_length=255)
    task_id: str | None = None
    title: str = Field(default="", max_length=240)
    body: str = Field(default="", max_length=60_000)
    base: str = Field(default="main", min_length=1, max_length=255)
    draft: bool = True


class GitHubPolicy(BaseModel):
    execution_mode: Literal['review', 'autonomous']


class GitIntegration:
    def __init__(self, get_db, get_settings, workspace_resolver=None, idle_check=None):
        self.workspace_resolver = workspace_resolver
        self.idle_check = idle_check
        self.get_db = get_db
        from .github_connection import GitHubConnection
        self.github = GitHubConnection(get_db)
        self.get_settings = get_settings
        self._db = None
        self._service = None
        self._service_lock = threading.RLock()
        self.router = APIRouter(prefix='/api')
        self.router.add_api_route('/github/connection', self.connection_status, methods=['GET'])
        self.router.add_api_route('/github/connect', self.connect_github, methods=['POST'])
        self.router.add_api_route('/github/connect/{attempt_id}', self.poll_github, methods=['GET'])
        self.router.add_api_route('/github/connect/{attempt_id}', self.cancel_github, methods=['DELETE'])
        self.router.add_api_route('/github/policy', self.github_policy, methods=['PUT'])
        self.router.add_api_route('/conversations/{conversation_id}/git/operations', self.list_operations, methods=['GET'])
        self.router.add_api_route('/conversations/{conversation_id}/git/status', self.status, methods=['GET'])
        self.router.add_api_route('/conversations/{conversation_id}/git/diff', self.diff, methods=['GET'])
        self.router.add_api_route('/conversations/{conversation_id}/git/pull-requests', self.pull_requests, methods=['GET'])
        self.router.add_api_route('/conversations/{conversation_id}/git/pull-requests/{number}', self.pull_request, methods=['GET'])
        self.router.add_api_route('/conversations/{conversation_id}/git/log', self.log, methods=['GET'])
        self.router.add_api_route('/conversations/{conversation_id}/git/prepare', self.prepare, methods=['POST'])
        self.router.add_api_route('/conversations/{conversation_id}/git/operations/{operation_id}/approve', self.approve, methods=['POST'])
        self.router.add_api_route('/conversations/{conversation_id}/git/operations/{operation_id}/reject', self.reject, methods=['POST'])

    @property
    def service(self):
        with self._service_lock:
            db = self.get_db()
            if db is not self._db:
                self._service = GitWorkspace(db, self.workspace_resolver, self.idle_check, self.github.transport, self.github.identity)
                self._db = db
            return self._service

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (GitError, WorkspaceError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc) if isinstance(exc, GitError) else 'GIT_WORKSPACE_NOT_FOUND') from None

    def connection_status(self):
        return self._call(self.github.account)

    def connect_github(self):
        return self._call(self.github.start)

    def poll_github(self, attempt_id: str):
        return self._call(self.github.poll, attempt_id)

    def cancel_github(self, attempt_id: str):
        return self.github.expire(attempt_id, cancel=True)

    def github_policy(self, value: GitHubPolicy):
        return self._call(self.github.policy, value.execution_mode)

    def network_access(self):
        return bool(full_access(self.get_settings()) or self.get_settings().external_access_enabled or self.github.linked())

    def finish(self, conversation_id, operation):
        if not full_access(self.get_settings()) and (level(self.get_settings()) == "confirm" or not self.github.automatic()):
            return operation
        completed = self.approve(conversation_id, operation['id'])
        if completed.get('next_operation_id'):
            completed['following_operation'] = self.approve(conversation_id, completed['next_operation_id'])
        return completed

    def list_operations(self, conversation_id: str, task_id: str | None = None):
        db = self.get_db()
        conversation = db.conversation(conversation_id)
        if not conversation: raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        project = db.project(conversation.get('project_id')) if conversation.get('project_id') else None
        operations = self.service.operations(conversation_id)
        if task_id:
            operations = [op for op in operations if op.get('task_id') == task_id]
        return {'available': bool(project and project.get('workspace_path')), 'operations': operations}

    def status(self, conversation_id: str, task_id: str | None = None):
        return self._call(self.service.status, conversation_id, task_id)

    def diff(self, conversation_id: str, task_id: str | None = None):
        return self._call(self.service.diff, conversation_id, task_id)

    def log(self, conversation_id: str, task_id: str | None = None):
        root = self._call(self.service.root, conversation_id, task_id)
        return {'log': self._call(self.service._run, root, ['log', '-30', '--format=%h %ad %s', '--date=short'])}

    def pull_requests(self, conversation_id: str, task_id: str | None = None, remote: str = 'origin'):
        from .github_review import GitHubReview
        root = self._call(self.service.root, conversation_id, task_id)
        return self._call(GitHubReview(self.service).list, root, remote, self.network_access)

    def pull_request(self, conversation_id: str, number: int, task_id: str | None = None, remote: str = 'origin'):
        from .github_review import GitHubReview
        root = self._call(self.service.root, conversation_id, task_id)
        return self._call(GitHubReview(self.service).view, root, remote, number, self.network_access)

    def prepare(self, conversation_id: str, value: GitPreparation):
        # Commit approval is local. Push approval also checks External Access
        # at execution, so switching it OFF immediately revokes network use.
        result = self._call(self.service.prepare, conversation_id, value.action,
                            paths=value.paths, message=value.message, remote=value.remote, branch=value.branch, task_id=value.task_id,
                            title=value.title, body=value.body, base=value.base, draft=value.draft,
                            external_access=self.network_access)
        result = self.finish(conversation_id, result) if full_access(self.get_settings()) else result
        self._message(conversation_id, result)
        return result

    def _message(self, conversation_id, op):
        text = self.summary(op['action'], op)
        self.get_db().add_message(conversation_id, 'assistant', text, time.time(), str(uuid.uuid4()))

    def summary(self, action, result):
        ja = response_language() == 'ja'
        if action in ('status', 'connect'):
            lines = [f"Git · {result['workspace']}", f"{'ブランチ' if ja else 'Branch'}: {result['branch'] or 'detached HEAD'}",
                     f"HEAD: {result['head'] or '(none)'}"]
            lines += [f"- {row['status']} {row['path']}" for row in result['files']]
            lines += [f"- {remote['name']}: {remote['url']}" for remote in result['remotes']]
            lines.append('チャット下のGit / GitHubパネルで差分・認証手順を確認できます。' if ja else 'Use the Git / GitHub panel below the chat to review diffs and authentication setup.')
            return '\n'.join(lines)
        if action in ('prs', 'pr') and 'action' not in result:
            return json.dumps(result, ensure_ascii=False, indent=2)
        if action in ('diff', 'log'):
            value = result.get('log', '') if action == 'log' else ('Worktree\n' + result['working_diff'] + '\nIndex\n' + result['staged_diff'] + '\nUntracked\n' + '\n'.join(result['untracked_files']))
            import re
            fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', value)), default=0))
            return f'{fence}\n{value}\n{fence}'
        text = f"Git {result['action']}: {result['status']} · {result['id']}"
        if result.get('commit'): text += f"\nCommit: {result['commit']}"
        if result.get('pushed_commit'): text += f"\nPushed: {result['pushed_commit']} → {result['remote_url']} / {result['destination_branch']}"
        if result.get('pr_url'): text += '\nPull request: ' + result['pr_url']
        following = result.get('following_operation')
        if following:
            text += '\nGit push: ' + following['status']
            if following.get('pushed_commit'):
                text += '\nPushed: ' + following['pushed_commit'] + ' → ' + following['remote_url']
            if following.get('error'): text += '\n' + following['error']
        if result.get('push_preparation_error'): text += '\n' + result['push_preparation_error']
        if result.get('error'): text += '\n' + result['error']
        if result['status'] == 'PENDING':
            text += '\n' + ('Git / GitHubパネルで内容を確認し、承認して実行してください。' if ja else 'Review the contents in the Git / GitHub panel, then approve to execute.')
        return text

    def approve(self, conversation_id: str, operation_id: str):
        result = self._call(self.service.execute, operation_id, conversation_id,
                            external_access=self.network_access)
        result = self.finish(conversation_id, result) if full_access(self.get_settings()) else result
        self._message(conversation_id, result)
        return result

    def reject(self, conversation_id: str, operation_id: str):
        result = self._call(self.service.reject, operation_id, conversation_id)
        result = self.finish(conversation_id, result) if full_access(self.get_settings()) else result
        self._message(conversation_id, result)
        return result

    def command(self, conversation_id, action, argument):
        if action in ('status', 'connect'): return self.status(conversation_id)
        if action == 'diff': return self.diff(conversation_id)
        if action == 'log': return self.log(conversation_id)
        if action in ('stage', 'commit', 'commit_push'):
            if not argument.strip():
                state = self.status(conversation_id)
                paths = [row['path'] for row in state['files']]
                operation = self._call(self.service.prepare, conversation_id, 'stage' if action == 'stage' else 'commit',
                    paths=paths, message='Update ' + ', '.join(paths[:6]), push_after=action == 'commit_push',
                    external_access=self.network_access)
                return self.finish(conversation_id, operation)
            try:
                payload = json.loads(argument)
                if not isinstance(payload, dict) or set(payload) - {'paths', 'message'}:
                    raise ValueError()
                if 'paths' not in payload: payload['paths'] = [row['path'] for row in self.status(conversation_id)['files']]
                if 'message' not in payload: payload['message'] = 'Update ' + ', '.join(payload['paths'][:6])
                value = GitPreparation(action='stage' if action == 'stage' else 'commit', **payload)
            except (ValueError, TypeError):
                raise HTTPException(422, 'Use Git panel to select files and enter a message, or /git commit {"paths":["file.py"],"message":"Fix bug"}.') from None
            return self.finish(conversation_id, self.prepare(conversation_id, value))
        if action == 'push':
            try: parts = shlex.split(argument)
            except ValueError: raise HTTPException(422, 'GIT_REMOTE_BRANCH_INVALID') from None
            if len(parts) > 2: raise HTTPException(422, 'GIT_REMOTE_BRANCH_INVALID')
            return self.finish(conversation_id, self.prepare(conversation_id, GitPreparation(action='push', remote=parts[0] if parts else 'origin', branch=parts[1] if len(parts) == 2 else None)))
        if action == 'switch':
            return self.prepare(conversation_id, GitPreparation(action='switch_branch', branch=argument.strip()))
        if action == 'branch':
            return self.prepare(conversation_id, GitPreparation(action='branch', branch=argument.strip()))
        if action == 'prs':
            return self.pull_requests(conversation_id)
        if action == 'pr':
            if argument.strip().isdigit(): return self.pull_request(conversation_id, int(argument.strip()))
            try:
                payload = json.loads(argument)
                value = GitPreparation(action='pull_request', **payload)
            except (ValueError, TypeError):
                raise HTTPException(422, 'Use /git pr <number> or /git pr {"title":"...","body":"...","base":"main","draft":true}') from None
            return self.prepare(conversation_id, value)
        if action in ('approve', 'reject'):
            if len(argument.split()) != 1: raise HTTPException(422, 'GIT_OPERATION_ID_REQUIRED')
            return (self.approve if action == 'approve' else self.reject)(conversation_id, argument)
        raise HTTPException(422, 'GIT_COMMAND_UNSUPPORTED')

    def capture_task_baseline(self, task):
        if not coding_git_intent(str(task.get('original_goal') or ''), self.github.automatic()): return
        try:
            root = self.service.root(task['conversation_id'], task['id'])
            payload = {'workspace': str(root), 'dirty_paths': [r['path'] for r in self.service._files(root)]}
            with self.get_db().connect() as conn:
                conn.execute("INSERT INTO git_task_baselines VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET payload_json=excluded.payload_json WHERE json_extract(git_task_baselines.payload_json,'$.workspace') != json_extract(excluded.payload_json,'$.workspace')", (task['id'], json.dumps(payload)))
        except (GitError, WorkspaceError, OSError, sqlite3.Error):
            # A missing baseline disables automatic file selection at handoff.
            pass

    def completed_task(self, task):
        """Prepare task-owned changes and apply the user's execution policy."""
        intent = coding_git_intent(str(task.get('original_goal') or ''), self.github.automatic())
        if not intent: return
        forbidden = set((task.get('requirements') or {}).get('forbidden_capabilities') or [])
        if forbidden & {'network', 'web', 'external_access', 'external_tools'}:
            intent['push'] = False
            if not intent['commit'] and not intent.get('stage'): return
        conversation_id = task['conversation_id']
        task_id = task['id']
        if self.service.task_operations(task_id):
            return
        try:
            if intent['commit'] or intent.get('stage'):
                root = self.service.root(conversation_id, task_id)
                allowed = set()
                plan = task.get('plan') or {}
                artifact = plan.get('implementation_plan_artifact') or {}
                for entry in artifact.get('file_manifest') or plan.get('file_manifest') or []:
                    value = entry.get('path') if isinstance(entry, dict) else entry
                    if isinstance(value, str):
                        p = Path(value)
                        if p.is_absolute():
                            try: value = p.relative_to(root).as_posix()
                            except ValueError: continue
                        allowed.add(value)
                changed = set()
                for row in self.get_db().coding_phase_reports(task_id):
                    for value in (row.get('structured_report') or {}).get('changed_files') or []:
                        if isinstance(value, dict): value = value.get('path')
                        if isinstance(value, str):
                            p = Path(value)
                            if p.is_absolute():
                                try: value = p.relative_to(root).as_posix()
                                except ValueError: continue
                            if value in allowed: changed.add(value)
                dirty = {row['path'] for row in self.service._files(root)}
                with self.get_db().connect() as conn:
                    row = conn.execute('SELECT payload_json FROM git_task_baselines WHERE task_id=?', (task_id,)).fetchone()
                baseline = json.loads(row['payload_json']) if row else None
                selected = sorted((changed & dirty) - set(baseline['dirty_paths'])) if baseline and baseline['workspace'] == str(root) else []
                # If provenance is missing, the UI asks the user to select files.
                # Never infer that every dirty file belongs to this task.
                if not selected: raise GitError('Select the task files in the Git panel; no verified changed-file provenance is available.')
                op = self.service.prepare(conversation_id, 'commit' if intent['commit'] else 'stage', paths=selected,
                                          message='Complete coding task: ' + str(task['original_goal']).splitlines()[0][:160],
                                          task_id=task_id, push_after=intent['push'])
            else:
                op = self.service.prepare(conversation_id, 'push', task_id=task_id,
                                          external_access=self.network_access)
            op = self.finish(conversation_id, op)
            self._message(conversation_id, op)
        except (GitError, OSError) as exc:
            self.get_db().add_message(conversation_id, 'assistant', 'Git操作の準備: ' + str(exc), time.time(), str(uuid.uuid4()))
