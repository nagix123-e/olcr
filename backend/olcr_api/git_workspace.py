"""Reviewed, project-scoped Git operations; no model-generated shell commands."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager


GENERATED_DIRECTORIES = frozenset({'.git', 'node_modules', '.venv', '.venv312', '.venv-mlx', '__pycache__', 'target', 'dist', 'graphify-out'})


class GitError(ValueError):
    pass


def network_allowed(access):
    return bool(access() if callable(access) else access)


def git_intent(text: str) -> dict | None:
    """Only standalone commands: ordinary coding requests keep their own route."""
    value = text.strip().rstrip('。.!！').strip()
    value = re.sub(r'^please\s+|\s+please$', '', value, flags=re.I)
    value = value.replace('しておいて', 'して').replace('してください', 'して')
    value = re.sub(r'^(?:今の変更|この変更|今回の変更|変更分|これ)を', '変更を', value)
    value = value.replace('全部', 'すべて')
    if re.fullmatch(r'github(?:に|と)?(?:接続して|連携して)|connect (?:to )?github', value, re.I):
        return {'action': 'connect'}
    if re.fullmatch(r'(?:git|github|ギット)(?:の)?(?:状態|接続状態)(?:を)?(?:見せて|確認して|表示して)?|(?:show|check) git status', value, re.I):
        return {'action': 'status'}
    if re.fullmatch(r'(?:git|ギット)(?:の)?差分(?:を)?(?:見せて|確認して|表示して)?|(?:show|check) git diff', value, re.I):
        return {'action': 'diff'}
    named = re.fullmatch(r'([A-Za-z0-9_./-]+)を(ステージ|コミット)して', value, re.I)
    if named:
        return {'action': 'stage' if named[2] == 'ステージ' else 'commit', 'paths': [named[1]]}
    message = re.fullmatch(r'(?:メッセージ)?[「\"](.+?)[」\"]でコミットして', value)
    if message:
        return {'action': 'commit', 'message': message[1]}
    if re.fullmatch(r'(?:変更(?:を)?|すべて(?:の変更)?(?:を)?)?(?:コミットして(?:から|、)?プッシュして)|commit(?: the)? changes and push', value, re.I):
        return {'action': 'commit_push'}
    if re.fullmatch(r'(?:変更(?:を)?|すべて(?:の変更)?(?:を)?)?(?:ステージして|ステージングして)|stage(?: all| the| my)?(?: changes)?', value, re.I):
        return {'action': 'stage'}
    if re.fullmatch(r'(?:変更(?:を)?|すべて(?:の変更)?(?:を)?)?コミットして|commit(?: the| my| all)?(?: changes| it| this)?', value, re.I):
        return {'action': 'commit'}
    if re.fullmatch(r'(?:github(?:に|へ)?|git(?:を)?|変更を)?(?:プッシュして|push(?: (?:the |my |all )?changes)?(?: to github)?)', value, re.I):
        return {'action': 'push'}
    return None


def coding_git_intent(text: str, autonomous: bool = False) -> dict | None:
    """Resolve requested Git actions, including an opt-in autonomous policy."""
    if re.search(r'Git(?:Hub)?操作(?:を|は)?(?:しない|禁止)|do not (?:use )?git', text, re.I):
        return None
    stage = bool(re.search(r'ステージ(?:して|だけ)|\bstage (?:changes|only)', text, re.I))
    commit = bool(re.search(r'(?:コミット(?:して|まで|する|もして)|\bcommit\s+(?:the\s+)?(?:changes|it|this)|\band\s+commit\s*$|\bcommit(?=\s+and\s+push(?:\s+to\s+\S+)?\s*[.!]?$))', text, re.I))
    push = bool(re.search(r'(?:プッシュ(?:して|まで|する|もして)|\bpush\s+(?:the\s+)?(?:changes|it|this|to)|\band\s+push\s*$)', text, re.I))
    explicit_push = push
    commit, push = commit or autonomous, push or autonomous
    for english, japanese in (('commit', 'コミット'), ('push', 'プッシュ')):
        negative = re.search(rf'(?:do not|don.t|no)\s+(?:git\s*)?{english}|{japanese}(?:を)?(?:しない|禁止|不要)', text, re.I)
        if negative:
            if english == 'commit': commit = False
            else: push = False
    if re.search(r'ステージだけ|stage only', text, re.I):
        commit = push = False
    if autonomous and not commit and not explicit_push:
        push = False
    result = {'commit': commit, 'push': push}
    if stage: result['stage'] = True
    return result if commit or push or stage else None


def safe_error(value: str) -> str:
    value = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[redacted]@', value)
    value = re.sub(r'(?i)(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)', '[redacted]', value)
    return value[:3000]


def github_url(value: str) -> str:
    if not re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', value):
        raise GitError('GIT_REMOTE_MUST_BE_GITHUB_WITHOUT_EMBEDDED_CREDENTIALS')
    return value


class GitWorkspace:
    def __init__(self, db, workspace_resolver=None, idle_check=None, credential_transport=None, identity_provider=None):
        self.db = db
        self.credential_transport = credential_transport
        self.identity_provider = identity_provider
        self.workspace_resolver = workspace_resolver
        self.idle_check = idle_check
        self.git = shutil.which('git')
        self._lock = threading.RLock()
        with db.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS git_operations(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, task_id TEXT, payload_json TEXT NOT NULL, created_at REAL NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS git_task_baselines(task_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)')
            # A process may have died after Git mutated the repository. Never
            # replay such a transaction, even after a normal app restart.
            rows = conn.execute('SELECT id,payload_json FROM git_operations').fetchall()
            for row in rows:
                payload = json.loads(row['payload_json'])
                if payload.get('status') == 'RUNNING':
                    payload.update(status='INTERRUPTED', error='Inspect repository state before preparing another operation.')
                    conn.execute('UPDATE git_operations SET payload_json=? WHERE id=?', (json.dumps(payload), row['id']))

    def _env(self, extra=None):
        names = ('PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSH_AUTH_SOCK', 'XDG_CONFIG_HOME', 'APPDATA', 'SYSTEMROOT')
        env = {key: os.environ[key] for key in names if key in os.environ}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_LITERAL_PATHSPECS='1', GH_PROMPT_DISABLED='1')
        if shutil.which('ssh'):
            env['GIT_SSH_COMMAND'] = 'ssh -oBatchMode=yes'
        env.update(extra or {})
        return env

    def _run(self, root: Path, args: list[str], *, env=None, ok=(0,), timeout=30) -> str:
        if not self.git:
            raise GitError('GIT_NOT_INSTALLED')
        try:
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                proc = subprocess.run([self.git, '--no-pager', '-c', 'color.ui=false', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args],
                                      env=self._env(env), stdin=subprocess.DEVNULL, stdout=output,
                                      stderr=errors, timeout=timeout)
                if output.tell() > 4_000_000: raise GitError('GIT_OUTPUT_TOO_LARGE_SELECT_FEWER_FILES')
                output.seek(0); stdout = output.read()
                errors.seek(0); stderr = errors.read(12_000)
        except subprocess.TimeoutExpired:
            raise GitError('GIT_TIMEOUT_CHECK_STATE_BEFORE_RETRY') from None
        except OSError:
            raise GitError('GIT_EXECUTABLE_UNAVAILABLE') from None
        if proc.returncode not in ok:
            raise GitError(safe_error(stderr.decode('utf-8', 'replace')) or 'GIT_COMMAND_FAILED')
        return stdout.decode('utf-8', 'surrogateescape')

    def root(self, conversation_id: str, task_id=None) -> Path:
        conversation = self.db.conversation(conversation_id)
        if not conversation:
            raise GitError('CONVERSATION_NOT_FOUND')
        project = self.db.project(conversation.get('project_id')) if conversation.get('project_id') else None
        if not project or project.get('archived') or not project.get('workspace_path'):
            raise GitError('GIT_PROJECT_WORKSPACE_REQUIRED')
        value = self.workspace_resolver(conversation_id, task_id) if self.workspace_resolver else None
        root = Path(value or project['workspace_path']).expanduser().resolve(strict=True)
        actual = self._run(root, ['rev-parse', '--show-toplevel']).strip()
        if Path(actual).resolve() != root:
            raise GitError('GIT_WORKSPACE_MUST_BE_REPOSITORY_ROOT')
        return root

    def _branch(self, root):
        return self._run(root, ['symbolic-ref', '--quiet', '--short', 'HEAD'], ok=(0, 1)).strip()

    def _head(self, root):
        return self._run(root, ['rev-parse', '--verify', 'HEAD'], ok=(0, 128)).strip()

    def _files(self, root, extra_paths=()):
        raw = self._run(root, ['status', '--porcelain=v1', '-z', '--untracked-files=no'])
        pieces = iter(raw.split('\0'))
        rows = []
        for part in pieces:
            if not part:
                continue
            row = {'status': part[:2], 'path': part[3:]}
            if 'R' in part[:2] or 'C' in part[:2]:
                row['original_path'] = next(pieces, '')
            rows.append(row)
        excludes = ['--exclude=**/' + name + '/**' for name in sorted(GENERATED_DIRECTORIES) if name != '.git']
        untracked = self._run(root, ['ls-files', '--others', '--exclude-standard', '-z', *excludes]).split('\0')
        if extra_paths:
            untracked += self._run(root, ['ls-files', '--others', '--exclude-standard', '-z', '--', *extra_paths]).split('\0')
        rows.extend({'status': '??', 'path': path} for path in sorted(set(untracked)) if path)
        return rows

    def _paths(self, root, paths):
        if not isinstance(paths, list) or not paths or len(paths) > 500:
            raise GitError('GIT_EXPLICIT_FILE_SELECTION_REQUIRED')
        changed = {row['path'] for row in self._files(root)}
        selected = []
        for value in paths:
            if not isinstance(value, str) or not value or '\0' in value or '\n' in value or '\r' in value:
                raise GitError('GIT_FILE_PATH_INVALID')
            p = PurePosixPath(value)
            if p.is_absolute() or '..' in p.parts or '.git' in p.parts:
                raise GitError('GIT_FILE_OUTSIDE_WORKSPACE')
            # Permit committing a symlink itself, never traversing its target.
            target = root / value
            if not target.parent.resolve().is_relative_to(root):
                raise GitError('GIT_FILE_OUTSIDE_WORKSPACE')
            if target.is_dir() and not target.is_symlink():
                raise GitError('GIT_SELECT_FILES_NOT_DIRECTORIES')
            if value not in changed and not self._run(root, ['ls-files', '--others', '--exclude-standard', '-z', '--', value]).strip('\0'):
                raise GitError('GIT_SELECTED_FILE_HAS_NO_CHANGE')
            if value not in selected:
                selected.append(value)
        rows = self._files(root)
        if any(row['status'] in ('UU', 'AA', 'DD', 'AU', 'UA', 'DU', 'UD') for row in rows):
            raise GitError('GIT_RESOLVE_CONFLICTS_FIRST')
        # Renames require both endpoints in one reviewed selection.
        for row in rows:
            if row['path'] in selected and row.get('original_path') and row['original_path'] not in selected:
                selected.append(row['original_path'])
        return selected

    def status(self, conversation_id, task_id=None):
        root = self.root(conversation_id, task_id)
        remotes = []
        for name in self._run(root, ['remote']).splitlines():
            urls = self._run(root, ['remote', 'get-url', '--push', '--all', name]).splitlines()
            valid = len(urls) == 1
            try:
                if valid: github_url(urls[0])
            except GitError:
                valid = False
            remotes.append({'name': name, 'url': urls[0] if valid else '[unsupported or multiple destinations]', 'github': valid})
        helper = self._run(root, ['config', '--get', 'credential.helper'], ok=(0, 1)).strip()
        explicit_paths = []
        if task_id:
            task = self.db.coding_task(task_id) or {}
            plan = task.get('plan') or {}
            for entry in (plan.get('implementation_plan_artifact') or {}).get('file_manifest') or plan.get('file_manifest') or []:
                path = entry.get('path') if isinstance(entry, dict) else entry
                if isinstance(path, str) and not Path(path).is_absolute() and '..' not in PurePosixPath(path).parts and '.git' not in PurePosixPath(path).parts:
                    explicit_paths.append(path)
            explicit_paths = explicit_paths[:500]
        return {'workspace': str(root), 'branch': self._branch(root), 'head': self._head(root), 'files': self._files(root, explicit_paths), 'remotes': remotes,
                'credential_helper_configured': bool(helper), 'github_cli_installed': bool(shutil.which('gh')),
                'authentication': 'SYSTEM_GIT_CREDENTIALS; connection verified on push',
                'setup_command': 'gh auth login --hostname github.com && gh auth setup-git',
                'setup_url': 'https://cli.github.com/manual/gh_auth_login',
                'branches': self._run(root, ['for-each-ref', '--format=%(refname:short)', 'refs/heads/']).splitlines()}

    def diff(self, conversation_id, task_id=None):
        root = self.root(conversation_id, task_id)
        return {'working_diff': self._run(root, ['diff', '--no-ext-diff', '--no-textconv', '--', '.']),
                'staged_diff': self._run(root, ['diff', '--cached', '--no-ext-diff', '--no-textconv', '--', '.']),
                'untracked_files': [r['path'] for r in self._files(root) if r['status'] == '??']}

    def _snapshot(self, root, paths=()):
        digest = hashlib.sha256()
        for value in (str(root), self._head(root), self._branch(root),
                      self._run(root, ['diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv']),
                      self._run(root, ['status', '--porcelain=v1', '-z', '--untracked-files=normal'])):
            digest.update(value.encode('utf-8', 'surrogateescape'))
        for path in paths:
            target = root / path
            digest.update(path.encode('utf-8', 'surrogateescape'))
            if target.is_symlink():
                digest.update(os.readlink(target).encode('utf-8', 'surrogateescape'))
            elif target.is_file():
                with target.open('rb') as stream:
                    while chunk := stream.read(1024 * 1024): digest.update(chunk)
            else:
                digest.update(b'DELETED')
        return digest.hexdigest()

    @contextmanager
    def _index(self, root, paths):
        with tempfile.TemporaryDirectory(prefix='olcr-git-review-') as temporary:
            env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
            self._run(root, ['read-tree', 'HEAD'] if self._head(root) else ['read-tree', '--empty'], env=env)
            self._run(root, ['add', '--', *paths], env=env)
            yield env

    def _commit_diff(self, root, paths):
        with self._index(root, paths) as env:
            diff = self._run(root, ['diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv'], env=env)
        if not diff:
            raise GitError('GIT_NO_CONTENT_CHANGE_TO_COMMIT')
        return diff

    def _push_target(self, root, remote, branch):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}', remote):
            raise GitError('GIT_REMOTE_NAME_INVALID')
        if not remote or remote not in self._run(root, ['remote']).splitlines():
            raise GitError('GIT_REMOTE_NOT_CONFIGURED')
        if not branch or branch.startswith('-') or self._run(root, ['check-ref-format', '--branch', branch], ok=(0, 128)).strip() != branch:
            raise GitError('GIT_BRANCH_INVALID')
        urls = self._run(root, ['remote', 'get-url', '--push', '--all', remote]).splitlines()
        if len(urls) != 1:
            raise GitError('GIT_MULTIPLE_PUSH_DESTINATIONS_UNSUPPORTED')
        url = github_url(urls[0])
        rewrites = self._run(root, ['config', '--get-regexp', r'^url\..*\.(insteadof|pushinsteadof)$'], ok=(0, 1))
        if rewrites:
            raise GitError('GIT_URL_REWRITES_REQUIRE_REMOVAL_BEFORE_REVIEWED_PUSH')
        return self.credential_transport(url) if self.credential_transport else url

    def _save(self, payload, new=False):
        with self.db.connect() as conn:
            if new:
                conn.execute('INSERT INTO git_operations VALUES(?,?,?,?,?)', (payload['id'], payload['conversation_id'], payload.get('task_id'), json.dumps(payload, ensure_ascii=True), payload['created_at']))
            else:
                conn.execute('UPDATE git_operations SET payload_json=? WHERE id=?', (json.dumps(payload, ensure_ascii=True), payload['id']))
        return payload

    def operation(self, operation_id, conversation_id):
        with self.db.connect() as conn:
            row = conn.execute('SELECT payload_json FROM git_operations WHERE id=? AND conversation_id=?', (operation_id, conversation_id)).fetchone()
        if not row: raise GitError('GIT_OPERATION_NOT_FOUND')
        return json.loads(row['payload_json'])

    def operations(self, conversation_id):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT payload_json FROM git_operations WHERE conversation_id=? AND (json_extract(payload_json,'$.status') IN ('PENDING','RUNNING') OR id IN (SELECT id FROM git_operations WHERE conversation_id=? ORDER BY created_at DESC LIMIT 20)) ORDER BY created_at DESC", (conversation_id, conversation_id)).fetchall()
        return [json.loads(row['payload_json']) for row in rows]

    def task_operations(self, task_id):
        with self.db.connect() as conn:
            rows = conn.execute('SELECT payload_json FROM git_operations WHERE task_id=?', (task_id,)).fetchall()
        return [json.loads(row['payload_json']) for row in rows]

    def _require_idle(self, conversation_id, task_id=None):
        root = self.root(conversation_id, task_id)
        with self.db.connect() as conn:
            active = conn.execute("SELECT id,conversation_id FROM coding_tasks WHERE status IN ('QUEUED','RUNNING','PLANNING','FINAL_REPORTING','QWEN_REPLANNING') AND archived=0").fetchall()
        for task in active:
            try:
                if self.root(task['conversation_id'], task['id']) == root:
                    raise GitError('GIT_WAIT_FOR_CODING_TASK_TO_FINISH')
            except (ValueError, OSError) as exc:
                if str(exc) == 'GIT_WAIT_FOR_CODING_TASK_TO_FINISH': raise

    def prepare(self, conversation_id, action, *, paths=None, message='', remote='origin', branch=None, task_id=None, push_after=False, external_access=False, title='', body='', base='main', draft=True):
        with self._lock:
            root = self.root(conversation_id, task_id)
            self._require_idle(conversation_id, task_id)
            current_branch = self._branch(root)
            if not current_branch: raise GitError('GIT_DETACHED_HEAD_SELECT_BRANCH_FIRST')
            operation = {'id': str(uuid.uuid4()), 'conversation_id': conversation_id, 'task_id': task_id,
                         'workspace': str(root), 'action': action, 'status': 'PENDING', 'created_at': time.time(),
                         'branch': current_branch, 'head': self._head(root), 'hooks': 'DISABLED_FOR_REVIEWED_OPERATION'}
            if action in ('commit', 'stage'):
                if action == 'commit' and (not message.strip() or len(message) > 5000 or '\0' in message):
                    raise GitError('GIT_COMMIT_MESSAGE_REQUIRED')
                selected = self._paths(root, paths)
                before = self._snapshot(root, selected)
                diff = self._commit_diff(root, selected)
                if before != self._snapshot(root, selected): raise GitError('GIT_STATE_CHANGED_DURING_REVIEW')
                operation.update(paths=selected, message=message.strip(), diff=diff, snapshot=before, push_after=push_after)
            elif action == 'push':
                if not network_allowed(external_access): raise GitError('EXTERNAL_ACCESS_REQUIRED')
                if not operation['head']: raise GitError('GIT_COMMIT_BEFORE_PUSH')
                branch = branch or current_branch
                url = self._push_target(root, remote, branch)
                if not network_allowed(external_access): raise GitError('EXTERNAL_ACCESS_REQUIRED')
                remote_head = self._remote_head(root, url, branch)
                range_spec = operation['head']
                scope = 'ALL_LOCAL_HISTORY_NEW_REMOTE_BRANCH'
                if remote_head:
                    exists = bool(self._run(root, ['rev-parse', '--verify', remote_head + '^{commit}'], ok=(0, 128)).strip())
                    if exists:
                        range_spec = f"{remote_head}..{operation['head']}"
                        scope = 'COMMITS_SINCE_REMOTE_HEAD'
                    else:
                        scope = 'ALL_LOCAL_HISTORY_REMOTE_OBJECT_NOT_PRESENT'
                operation.update(remote=remote, destination_branch=branch, remote_url=url, remote_head=remote_head,
                                 snapshot=self._snapshot(root), commit_scope=scope)
                operation['commits'] = self._run(root, ['log', '--format=%h %s', range_spec]).splitlines()
            elif action == 'switch_branch':
                if not branch or branch.startswith('-') or self._run(root, ['check-ref-format', '--branch', branch], ok=(0,128)).strip() != branch:
                    raise GitError('GIT_BRANCH_INVALID')
                if self._files(root): raise GitError('GIT_COMMIT_OR_REVERT_BEFORE_BRANCH_SWITCH')
                target_head = self._run(root, ['rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}'], ok=(0,128)).strip()
                if not target_head: raise GitError('GIT_BRANCH_NOT_FOUND')
                operation.update(target_branch=branch, target_head=target_head, snapshot=self._snapshot(root),
                    diff=self._run(root, ['diff', '--no-ext-diff', '--no-textconv', operation['head'] + '..' + target_head]))
            elif action == 'branch':
                if not branch or branch.startswith('-') or self._run(root, ['check-ref-format', '--branch', branch], ok=(0,128)).strip() != branch:
                    raise GitError('GIT_BRANCH_INVALID')
                if self._files(root): raise GitError('GIT_COMMIT_OR_REVERT_BEFORE_BRANCH_SWITCH')
                if self._run(root, ['rev-parse', '--verify', 'refs/heads/' + branch], ok=(0,128)).strip():
                    raise GitError('GIT_BRANCH_ALREADY_EXISTS')
                operation.update(new_branch=branch, snapshot=self._snapshot(root))
            elif action == 'pull_request':
                from .github_review import GitHubReview
                github = GitHubReview(self)
                operation.update(github.prepare(root, remote, current_branch, base, title, body, draft, external_access))
                operation['snapshot'] = self._snapshot(root)
            else: raise GitError('GIT_OPERATION_UNSUPPORTED')
            return self._save(operation, new=True)

    def _remote_head(self, root, url, branch):
        lines = self._run(root, ['ls-remote', '--refs', url, f'refs/heads/{branch}']).splitlines()
        if not lines: return ''
        parts = lines[0].split('\t')
        if len(parts) != 2 or parts[1] != f'refs/heads/{branch}' or not re.fullmatch('[0-9a-f]{40,64}', parts[0]):
            raise GitError('GIT_REMOTE_RESPONSE_INVALID')
        return parts[0]

    def execute(self, operation_id, conversation_id, *, external_access=False):
        with self._lock:
            op = self.operation(operation_id, conversation_id)
            if op['status'] != 'PENDING': raise GitError('GIT_OPERATION_ALREADY_HANDLED')
            if time.time() - op['created_at'] > 3600: raise GitError('GIT_APPROVAL_EXPIRED_PREPARE_AGAIN')
            root = self.root(conversation_id, op.get('task_id'))
            self._require_idle(conversation_id, op.get('task_id'))
            if self.idle_check: self.idle_check(root)
            if str(root) != op['workspace'] or self._snapshot(root, op.get('paths', [])) != op['snapshot']:
                op.update(status='STALE', error='GIT_STATE_CHANGED_PREPARE_AGAIN'); return self._save(op)
            if op['action'] == 'switch_branch' and self._run(root, ['rev-parse', '--verify', 'refs/heads/' + op['target_branch'] + '^{commit}'], ok=(0,128)).strip() != op['target_head']:
                op.update(status='STALE', error='GIT_TARGET_BRANCH_CHANGED_PREPARE_AGAIN'); return self._save(op)
            if op['action'] == 'push':
                if not network_allowed(external_access): raise GitError('EXTERNAL_ACCESS_REQUIRED')
                if self._push_target(root, op['remote'], op['destination_branch']) != op['remote_url']:
                    op.update(status='STALE', error='GIT_DESTINATION_CHANGED_PREPARE_AGAIN'); return self._save(op)
                if not network_allowed(external_access): raise GitError('EXTERNAL_ACCESS_REQUIRED')
                if self._remote_head(root, op['remote_url'], op['destination_branch']) != op['remote_head']:
                    op.update(status='STALE', error='GIT_REMOTE_BRANCH_CHANGED_PREPARE_AGAIN'); return self._save(op)
            op.update(status='RUNNING', approved_at=time.time()); self._save(op)
            try:
                if op['action'] == 'stage':
                    self._run(root, ['add', '--', *op['paths']])
                elif op['action'] == 'commit':
                    # Commit the exact frozen tree shown in the review. Reading
                    # the worktree again with commit --only would reopen a race.
                    with self._index(root, op['paths']) as env:
                        diff = self._run(root, ['diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv'], env=env)
                        if diff != op['diff'] or self._snapshot(root, op['paths']) != op['snapshot']:
                            raise GitError('GIT_DIFF_CHANGED_PREPARE_AGAIN')
                        identity = self.identity_provider() if self.identity_provider else None
                        if identity:
                            if not self._run(root, ['config', '--get', 'user.name'], ok=(0,1)).strip():
                                env.update(GIT_AUTHOR_NAME=identity['name'], GIT_COMMITTER_NAME=identity['name'])
                            if not self._run(root, ['config', '--get', 'user.email'], ok=(0,1)).strip():
                                email = identity['email']
                                env.update(GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
                        self._run(root, ['commit', '-m', op['message']], env=env, timeout=60)
                        op.update(commit=self._head(root)); self._save(op)
                    # Reconcile only committed paths in the normal index,
                    # retaining unrelated staged entries and all worktree bytes.
                    self._run(root, ['reset', '-q', op['commit'], '--', *op['paths']])
                elif op['action'] == 'switch_branch':
                    self._run(root, ['switch', op['target_branch']])
                    op['switched_branch'] = op['target_branch']
                elif op['action'] == 'branch':
                    self._run(root, ['switch', '-c', op['new_branch']])
                    op['created_branch'] = op['new_branch']
                elif op['action'] == 'pull_request':
                    from .github_review import GitHubReview
                    op.update(GitHubReview(self).create(root, op, external_access))
                else:
                    if not network_allowed(external_access): raise GitError('EXTERNAL_ACCESS_REQUIRED')
                    output = self._run(root, ['-c', 'push.followTags=false', 'push', '--porcelain', op['remote_url'], f"{op['head']}:refs/heads/{op['destination_branch']}"], timeout=90)
                    op.update(output=safe_error(output), pushed_commit=op['head'])
                op.update(status='SUCCEEDED', completed_at=time.time()); self._save(op)
            except GitError as exc:
                op.update(status='SUCCEEDED_WITH_WARNING' if op.get('commit') else 'FAILED', error=str(exc), completed_at=time.time()); self._save(op)
                return op
            if op.get('push_after'):
                try:
                    following = self.prepare(conversation_id, 'push', task_id=op.get('task_id'), external_access=external_access)
                    op['next_operation_id'] = following['id']; self._save(op)
                except GitError as exc:
                    op['push_preparation_error'] = str(exc); self._save(op)
            return op

    def reject(self, operation_id, conversation_id):
        with self._lock:
            op = self.operation(operation_id, conversation_id)
            if op['status'] != 'PENDING': raise GitError('GIT_OPERATION_ALREADY_HANDLED')
            op.update(status='REJECTED'); return self._save(op)
