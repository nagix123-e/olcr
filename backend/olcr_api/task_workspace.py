"""Task-owned local workspaces, review snapshots, and durable steering messages.

The database is the authority for ownership. Files are read without following
symlinks, and review mutations use hashes from the displayed revision.
"""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import threading
import time
import uuid

from .git_workspace import GENERATED_DIRECTORIES, GitError, GitWorkspace

MAX_FILE_BYTES = 1_000_000
MAX_SNAPSHOT_BYTES = 32_000_000
MAX_FILES = 10_000
IGNORED = GENERATED_DIRECTORIES
ACTIVE = {'QUEUED', 'PLANNING', 'RUNNING', 'FINAL_REPORTING', 'QWEN_REPLANNING'}


def sensitive_path(path: str) -> bool:
    value = PurePosixPath(path)
    name = value.name.lower()
    return (bool(set(value.parts) & {'.aws', '.ssh'}) or name.startswith('.env')
            or name in {'credentials', 'credentials.json', 'secrets.json', 'secrets.toml', 'id_rsa', 'id_ed25519'}
            or name.endswith(('.pem', '.key', '.p12', '.pfx')))


class WorkspaceError(ValueError):
    pass


def digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


class TaskWorkspaces:
    def __init__(self, db):
        self.db = db
        self.git = GitWorkspace(db)
        self._lock = threading.RLock()
        self.instruction_lock = threading.RLock()
        self._roots: dict[str, threading.RLock] = {}
        self._running: dict[str, int] = {}
        with db.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS coding_workspace_preferences(project_id TEXT PRIMARY KEY, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coding_workspace_requests(task_id TEXT PRIMARY KEY REFERENCES coding_tasks(id) ON DELETE CASCADE, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coding_workspaces(task_id TEXT PRIMARY KEY REFERENCES coding_tasks(id) ON DELETE CASCADE, metadata_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coding_file_baselines(task_id TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE, path TEXT NOT NULL, state_json TEXT NOT NULL, contents BLOB, PRIMARY KEY(task_id,path));
                CREATE TABLE IF NOT EXISTS coding_file_checkpoints(task_id TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE, path TEXT NOT NULL, state_json TEXT NOT NULL, PRIMARY KEY(task_id,path));
                CREATE TABLE IF NOT EXISTS coding_source_applications(root TEXT NOT NULL, path TEXT NOT NULL, source_root TEXT NOT NULL, state_json TEXT NOT NULL, PRIMARY KEY(root,path));
                CREATE TABLE IF NOT EXISTS coding_steering(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES coding_tasks(id) ON DELETE CASCADE, message TEXT NOT NULL, received_revision INTEGER NOT NULL, applied_revision INTEGER, status TEXT NOT NULL, created_at REAL NOT NULL);
            ''')

    def task(self, task_id):
        task = self.db.coding_task(task_id)
        if not task:
            raise WorkspaceError('TASK_NOT_FOUND')
        return task

    def source(self, task_id):
        task = self.task(task_id)
        conversation = self.db.conversation(task['conversation_id']) or {}
        project = self.db.project(conversation.get('project_id')) or {}
        folder_id = (task.get('requirements') or {}).get('working_folder_id')
        if folder_id:
            from .working_folders import WorkingFolders
            try:
                folder = WorkingFolders(self.db).get(task['conversation_id'], folder_id)
            except ValueError as exc:
                raise WorkspaceError(str(exc)) from exc
            return Path(folder['root']), project
        value = project.get('workspace_path')
        if not value or project.get('archived'):
            raise WorkspaceError('TASK_PROJECT_WORKSPACE_REQUIRED')
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceError('TASK_WORKSPACE_MUST_BE_DIRECTORY')
        return root, project

    def preference(self, project_id, mode=None):
        if not self.db.project(project_id):
            raise WorkspaceError('PROJECT_NOT_FOUND')
        with self.db.connect() as conn:
            if mode is not None:
                if mode not in {'direct', 'worktree'}:
                    raise WorkspaceError('INVALID_WORKSPACE_MODE')
                conn.execute('INSERT INTO coding_workspace_preferences VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET mode=excluded.mode', (project_id, mode))
            row = conn.execute('SELECT mode FROM coding_workspace_preferences WHERE project_id=?', (project_id,)).fetchone()
        return {'mode': row[0] if row else 'direct'}

    def reserve(self, task_id):
        task = self.task(task_id)
        conversation = self.db.conversation(task['conversation_id']) or {}
        project = self.db.project(conversation.get('project_id'))
        from .working_folders import WorkingFolders
        folder = WorkingFolders(self.db).get(task['conversation_id'])
        if folder:
            requirements = dict(task.get('requirements') or {})
            requirements.setdefault('working_folder_id', folder['id'])
            self.db.update_coding_task(task_id, requirements=requirements)
        mode = self.preference(project['id'])['mode'] if project and not folder else 'direct'
        with self.db.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO coding_workspace_requests VALUES(?,?)', (task_id, mode))

    def requested_mode(self, task_id):
        with self.db.connect() as conn:
            row = conn.execute('SELECT mode FROM coding_workspace_requests WHERE task_id=?', (task_id,)).fetchone()
        # Legacy tasks retain their original workspace when resumed.
        return row[0] if row else 'direct'

    def preflight(self, task_id):
        """Check the selected workspace before spending a planning/model turn."""
        try:
            root, _ = self.source(task_id)
            if self.requested_mode(task_id) == 'worktree' and not self.metadata(task_id):
                try:
                    top = self.git._run(root, ['rev-parse', '--show-toplevel']).strip()
                except GitError as exc:
                    if 'not a git repository' in str(exc):
                        raise WorkspaceError('WORKTREE_REQUIRES_GIT_REPOSITORY') from None
                    raise
                if Path(top).resolve() != root:
                    raise WorkspaceError('WORKTREE_REQUIRES_REPOSITORY_ROOT')
                if not self.git._head(root):
                    raise WorkspaceError('WORKTREE_REQUIRES_INITIAL_COMMIT')
            return {'ready': True, 'mode': self.requested_mode(task_id)}
        except (WorkspaceError, GitError, OSError) as exc:
            return {'ready': False, 'mode': self.requested_mode(task_id), 'reason': str(exc)}

    def setup(self, task_id, action):
        task = self.task(task_id)
        if self.metadata(task_id) or task.get('archived') or task['status'] not in {'WAITING_FOR_USER', 'RESUMABLE'}:
            raise WorkspaceError('WORKSPACE_SETUP_NO_LONGER_AVAILABLE')
        pending = task.get('pending_authorization') or {}
        if pending.get('type') != 'WORKSPACE_SETUP' and not (task.get('recovery_reason') == 'SCHEDULER' and not task.get('plan')):
            raise WorkspaceError('WORKSPACE_SETUP_NO_LONGER_AVAILABLE')
        root, _ = self.source(task_id)
        with self.lock(root):
            self.assert_idle(root)
            if action == 'initialize':
                # Explicit setup authorizes one empty baseline commit only.
                # Never stage or commit pre-existing user files.
                if any(path.name not in {'.git', '.DS_Store'} for path in root.iterdir()) or (root / '.git').is_symlink():
                    raise WorkspaceError('GIT_INITIALIZATION_REQUIRES_EMPTY_FOLDER')
                try:
                    top = self.git._run(root, ['rev-parse', '--show-toplevel']).strip()
                except GitError as exc:
                    if 'not a git repository' not in str(exc): raise
                else:
                    if Path(top).resolve() != root or self.git._head(root):
                        raise WorkspaceError('GIT_INITIALIZATION_REQUIRES_EMPTY_REPOSITORY')
                    if self.git._run(root, ['ls-files', '-z']):
                        raise WorkspaceError('GIT_INITIALIZATION_REQUIRES_EMPTY_REPOSITORY')
                self.git._run(root, ['init', '-b', 'main'])
                self.git._run(root, ['-c', 'user.name=OLCR', '-c', 'user.email=olcr@localhost',
                    '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'Initialize OLCR workspace'])
            mode = 'direct' if action == 'direct' else 'worktree'
            with self.db.connect() as conn:
                conn.execute('INSERT INTO coding_workspace_requests VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET mode=excluded.mode', (task_id, mode))
            result = self.preflight(task_id)
            if not result['ready']: raise WorkspaceError(result['reason'])
        self.db.update_coding_task(task_id, pending_authorization=None, pending_user_confirmation=0,
            recovery_action='NONE', recovery_reason='NONE', activity='QWEN_PLANNING' if not task.get('plan') else 'NONE')
        return self.db.enqueue_coding_task(task_id)

    def previous_workspace(self, task_id):
        task = self.task(task_id)
        source, _ = self.source(task_id)
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT ct.id,w.metadata_json FROM coding_tasks ct
                JOIN coding_workspaces w ON w.task_id=ct.id
                WHERE ct.conversation_id=? AND ct.id!=? AND ct.status='COMPLETED'
                ORDER BY ct.updated_at DESC,ct.created_at DESC''', (task['conversation_id'], task_id)).fetchall()
        for row in rows:
            meta = json.loads(row['metadata_json'])
            if meta['mode'] == 'worktree' and meta['source_root'] == str(source):
                return row['id'], meta
        return None

    def planned_root(self, task_id):
        meta = self.metadata(task_id)
        if meta: return meta['root']
        if self.requested_mode(task_id) == 'worktree':
            previous = self.previous_workspace(task_id)
            return previous[1]['root'] if previous else 'isolated:' + task_id
        return str(self.source(task_id)[0])

    def metadata(self, task_id):
        self.task(task_id)
        with self.db.connect() as conn:
            row = conn.execute('SELECT metadata_json FROM coding_workspaces WHERE task_id=?', (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save_metadata(self, task_id, value):
        with self.db.connect() as conn:
            conn.execute('INSERT INTO coding_workspaces VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET metadata_json=excluded.metadata_json', (task_id, json.dumps(value)))

    @contextmanager
    def lock(self, root):
        with self._lock:
            lock = self._roots.setdefault(str(root), threading.RLock())
        with lock:
            yield

    def root_for(self, task_id, fallback=None):
        meta = self.metadata(task_id)
        if not meta:
            if (self.task(task_id).get('requirements') or {}).get('working_folder_id'):
                return str(self.source(task_id)[0])
            return fallback
        source, _ = self.source(task_id)
        if str(source) != meta['source_root']:
            raise WorkspaceError('PROJECT_WORKSPACE_CHANGED')
        if meta.get('removed'):
            raise WorkspaceError('WORKTREE_REMOVED_RESTORE_REQUIRED')
        root = Path(meta['root']).resolve(strict=True)
        if meta['mode'] == 'worktree':
            actual = self.git._run(root, ['rev-parse', '--git-common-dir']).strip()
            expected = self.git._run(source, ['rev-parse', '--git-common-dir']).strip()
            if (root / actual).resolve() != (source / expected).resolve():
                raise WorkspaceError('WORKTREE_OWNERSHIP_CHANGED')
        elif root != source:
            raise WorkspaceError('WORKSPACE_OWNERSHIP_CHANGED')
        return str(root)

    @contextmanager
    def executing(self, task_id):
        # Serialize at the execution boundary as well as in the scheduler.
        # Direct runners and same-chat worktree continuations share this path.
        with self.lock(self.planned_root(task_id)):
            self.prepare(task_id)
            root = self.root_for(task_id)
            with self.lock(root):
                with self._lock:
                    self._running[root] = self._running.get(root, 0) + 1
                try:
                    yield root
                finally:
                    with self._lock:
                        self._running[root] -= 1
                        if not self._running[root]: self._running.pop(root)

    def conversation_root(self, conversation_id, task_id=None):
        if task_id:
            if self.task(task_id)['conversation_id'] != conversation_id:
                raise WorkspaceError('TASK_CONVERSATION_MISMATCH')
        else:
            tasks = self.db.coding_tasks(conversation_id)
            task_id = next((t['id'] for t in tasks if not t.get('archived') and self.metadata(t['id'])), None)
        if task_id:
            return self.root_for(task_id)
        from .working_folders import WorkingFolders
        folder = WorkingFolders(self.db).get(conversation_id)
        return folder['root'] if folder else None

    def execution_plan(self, task_id, plan):
        meta = self.metadata(task_id)
        if not meta or meta['mode'] != 'worktree':
            return plan
        source, target = meta['source_root'], meta['root']
        def rebase(value, key=''):
            if key in {'original_goal', 'content', 'code', 'text'}:
                return value
            if isinstance(value, dict):
                return {k: rebase(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [rebase(item, key) for item in value]
            if isinstance(value, str):
                return target if value == source else value.replace(source + '/', target + '/')
            return value
        return rebase(plan)

    def prepare(self, task_id):
        # Acquire the workspace before any shared bookkeeping. Holding _lock
        # while capture waits for a running workspace deadlocks its release.
        with self.lock(self.planned_root(task_id)):
            existing = self.metadata(task_id)
            if existing:
                self.root_for(task_id)
                return existing
            source, project = self.source(task_id)
            mode = self.requested_mode(task_id)
            root = source
            meta = {'mode': mode, 'source_root': str(source), 'root': str(source), 'created_at': time.time(), 'branch': '', 'base_head': '', 'snapshot_complete': True}
            if mode == 'worktree':
                ready = self.preflight(task_id)
                if not ready['ready']: raise WorkspaceError(ready['reason'])
                if Path(self.git._run(source, ['rev-parse', '--show-toplevel']).strip()).resolve() != source:
                    raise WorkspaceError('WORKTREE_REQUIRES_REPOSITORY_ROOT')
                meta['base_head'] = self.git._head(source)
                if not meta['base_head']:
                    raise WorkspaceError('WORKTREE_REQUIRES_INITIAL_COMMIT')
                previous = self.previous_workspace(task_id)
                if previous:
                    previous_id, previous_meta = previous
                    if previous_meta.get('removed'): self.restore_worktree(previous_id)
                    root = Path(self.root_for(previous_id))
                    meta.update(root=str(root), branch=self.git._branch(root), base_head=self.git._head(root),
                                shared_from_task_id=previous_id, origin_task_id=previous_meta.get('origin_task_id', previous_id),
                                source_dirty_files=previous_meta.get('source_dirty_files', 0))
                else:
                    # A new chat gets a unique branch; never reset an existing ref.
                    identifier = uuid.uuid4().hex[:16]
                    branch = 'olcr/task-' + identifier
                    parent = Path(self.db.path).resolve().parent / 'coding-worktrees'
                    parent.mkdir(parents=True, exist_ok=True)
                    root = parent / identifier
                    source_dirty_files = len(self.git._files(source))
                    self.git._run(source, ['worktree', 'add', '-b', branch, str(root), meta['base_head']], timeout=60)
                    meta.update(root=str(root), branch=branch, origin_task_id=task_id, source_dirty_files=source_dirty_files)
            self._save_metadata(task_id, meta)
            try:
                self.capture(task_id)
            except WorkspaceError as exc:
                if str(exc) != 'WORKSPACE_TOO_LARGE_FOR_REVIEW':
                    raise
                meta.update(snapshot_complete=False, snapshot_error=str(exc))
                self._save_metadata(task_id, meta)
            return self.metadata(task_id)

    def _path(self, root, value):
        p = PurePosixPath(value)
        if (not value or p.is_absolute() or '..' in p.parts or '.git' in p.parts
                or '\x00' in value or '\n' in value or '\r' in value or p.as_posix() != value):
            raise WorkspaceError('FILE_OUTSIDE_TASK_WORKSPACE')
        target = root / value
        for item in (target, *target.parents):
            if item == root:
                break
            if item.is_symlink():
                raise WorkspaceError('SYMLINK_REVIEW_UNSUPPORTED')
        if not target.resolve().is_relative_to(root):
            raise WorkspaceError('FILE_OUTSIDE_TASK_WORKSPACE')
        return target

    def _read(self, root, value):
        path = self._path(root, value)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {'exists': False, 'hash': None, 'mode': None, 'reviewable': True}, None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceError('REVIEW_REQUIRES_REGULAR_FILE')
            if info.st_size > MAX_FILE_BYTES or sensitive_path(value):
                # Unreviewable assets need a change fingerprint, not a full
                # read of multi-gigabyte weights/videos on every UI refresh.
                fingerprint = digest(f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'.encode())
                return {'exists': True, 'hash': 'UNREVIEWABLE:' + fingerprint,
                        'mode': stat.S_IMODE(info.st_mode), 'reviewable': False,
                        'sensitive': sensitive_path(value), 'size': info.st_size}, None
            hasher = hashlib.sha256()
            content = bytearray()
            while chunk := stream.read(65_536):
                hasher.update(chunk)
                if info.st_size <= MAX_FILE_BYTES:
                    content.extend(chunk)
        supported = info.st_size <= MAX_FILE_BYTES
        return {'exists': True, 'hash': hasher.hexdigest(), 'mode': stat.S_IMODE(info.st_mode), 'reviewable': supported, 'size': info.st_size}, bytes(content) if supported else None

    def _files(self, root):
        found = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in IGNORED and not (Path(directory) / d).is_symlink()]
            for name in files:
                path = Path(directory) / name
                if name == '.git' or path.is_symlink():
                    continue
                found.append(path.relative_to(root).as_posix())
                if len(found) > MAX_FILES:
                    raise WorkspaceError('WORKSPACE_TOO_LARGE_FOR_REVIEW')
        return sorted(found)

    def capture(self, task_id):
        root = Path(self.root_for(task_id))
        meta = self.metadata(task_id)
        if meta.get('baseline_captured'):
            return
        total = 0
        with self.lock(root), self.db.connect() as conn:
            for path in self._files(root):
                state, contents = self._read(root, path)
                total += len(contents or b'')
                if total > MAX_SNAPSHOT_BYTES:
                    contents = None
                    state['reviewable'] = False
                conn.execute('INSERT OR IGNORE INTO coding_file_baselines VALUES(?,?,?,?)', (task_id, path, json.dumps(state), contents))
        meta.update(baseline_captured=True, snapshot_bytes=min(total, MAX_SNAPSHOT_BYTES), snapshot_complete=total <= MAX_SNAPSHOT_BYTES)
        self._save_metadata(task_id, meta)

    def _baseline(self, task_id):
        with self.db.connect() as conn:
            return {r['path']: (json.loads(r['state_json']), r['contents']) for r in conn.execute('SELECT * FROM coding_file_baselines WHERE task_id=?', (task_id,))}

    def capture_manifest(self, task_id, plan):
        """Record explicit paths before scaffolding, including ignored directories."""
        meta = self.metadata(task_id)
        if not meta or not meta.get('baseline_captured'):
            return
        root = Path(self.root_for(task_id))
        entries = (plan.get('implementation_plan_artifact') or {}).get('file_manifest') or plan.get('file_manifest') or []
        with self.lock(root), self.db.connect() as conn:
            total = conn.execute('SELECT COALESCE(SUM(length(contents)),0) FROM coding_file_baselines WHERE task_id=?', (task_id,)).fetchone()[0]
            for entry in entries:
                path = entry.get('path') if isinstance(entry, dict) else entry
                if not isinstance(path, str): continue
                if Path(path).is_absolute():
                    try: path = Path(path).relative_to(root).as_posix()
                    except ValueError: continue
                if conn.execute('SELECT 1 FROM coding_file_baselines WHERE task_id=? AND path=?', (task_id, path)).fetchone():
                    continue
                state, contents = self._read(root, path)
                if total + len(contents or b'') > MAX_SNAPSHOT_BYTES:
                    state['reviewable'] = False
                    contents = None
                total += len(contents or b'')
                conn.execute('INSERT INTO coding_file_baselines VALUES(?,?,?,?)', (task_id, path, json.dumps(state), contents))

    def _provenance(self, task_id, root):
        paths = {}
        task = self.task(task_id)
        plan = task.get('plan') or {}
        for entry in (plan.get('implementation_plan_artifact') or {}).get('file_manifest') or plan.get('file_manifest') or []:
            value = entry.get('path') if isinstance(entry, dict) else entry
            if isinstance(value, str):
                if Path(value).is_absolute():
                    try: value = Path(value).relative_to(root).as_posix()
                    except ValueError: continue
                paths[value] = []
        for row in self.db.coding_phase_reports(task_id):
            for entry in (row.get('structured_report') or {}).get('changed_files') or []:
                value = entry.get('path') if isinstance(entry, dict) else entry
                if isinstance(value, str):
                    if Path(value).is_absolute():
                        try: value = Path(value).relative_to(root).as_posix()
                        except ValueError: continue
                    paths.setdefault(value, []).append(row['phase_id'])
        return paths

    def checkpoint(self, task_id, paths=None):
        meta = self.metadata(task_id)
        if not meta or not meta.get('baseline_captured'):
            return
        root = Path(self.root_for(task_id))
        paths = paths if paths is not None else self._provenance(task_id, root)
        with self.lock(root), self.db.connect() as conn:
            for path in paths:
                evidence = path if isinstance(path, dict) else {}
                if isinstance(path, dict): path = path.get('path')
                if not isinstance(path, str): continue
                if Path(path).is_absolute():
                    try: path = Path(path).relative_to(root).as_posix()
                    except ValueError: continue
                try: state, _ = self._read(root, path)
                except WorkspaceError: continue
                if 'hash' in evidence:
                    state['hash'] = evidence['hash']
                conn.execute('INSERT INTO coding_file_checkpoints VALUES(?,?,?) ON CONFLICT(task_id,path) DO UPDATE SET state_json=excluded.state_json', (task_id, path, json.dumps(state)))

    def checkpoint_execution(self, task_id, operations, rolled_back=False):
        if rolled_back: return
        paths = []
        for operation in operations:
            if operation.get('status') != 'success': continue
            output = operation.get('output') or {}
            if operation.get('tool') == 'workspace_write' and output.get('path'):
                entry = {'path': output['path']}
                if output.get('post_apply_hash'): entry['hash'] = output['post_apply_hash']
                paths.append(entry)
            if operation.get('tool') == 'package_install' and output.get('exit_code') == 0:
                for path, state in (output.get('artifacts_after') or {}).items():
                    if isinstance(state, dict) and state.get('sha256'):
                        paths.append({'path': path, 'hash': state['sha256']})
        self.checkpoint(task_id, paths)

    def source_context(self, task_id):
        meta = self.metadata(task_id)
        origin = meta.get('origin_task_id', task_id)
        with self.db.connect() as conn:
            past = {}
            for row in conn.execute('''SELECT b.path,b.state_json FROM coding_file_checkpoints b
                JOIN coding_workspaces w ON w.task_id=b.task_id
                WHERE json_extract(w.metadata_json,'$.root')=? AND b.task_id!=?''', (meta['root'], task_id)):
                past.setdefault(row['path'], []).append(json.loads(row['state_json']))
            applied = {r['path']: json.loads(r['state_json']) for r in conn.execute(
                'SELECT * FROM coding_source_applications WHERE root=? AND source_root=?', (meta['root'], meta['source_root']))}
        return past, applied, self.metadata(origin) or meta

    def git_preimage(self, root, head, path):
        """The source integration base is the chat's initial Git tree, even
        when later tasks add paths to their individual review manifests."""
        value = self.git._run(root, ['ls-tree', '-z', head, '--', path]).split('\0')[0]
        if not value:
            return {'exists': False, 'hash': None, 'mode': None, 'reviewable': True}, None
        mode, kind, object_id = value.split('\t', 1)[0].split()
        state = {'exists': True, 'hash': 'UNREVIEWABLE:' + object_id, 'mode': int(mode, 8) & 0o777,
                 'reviewable': False, 'sensitive': sensitive_path(path)}
        if kind != 'blob' or mode == '120000' or sensitive_path(path):
            return state, None
        size = int(self.git._run(root, ['cat-file', '-s', object_id]).strip())
        if size > MAX_FILE_BYTES: return state, None
        contents = self.git._run(root, ['cat-file', 'blob', object_id]).encode('utf-8', 'surrogateescape')
        state.update(hash=digest(contents), reviewable=True, size=len(contents))
        return state, contents

    def changes(self, task_id):
        task = self.task(task_id)
        meta = self.metadata(task_id)
        if not meta or meta.get('removed') or not meta.get('baseline_captured'):
            return {'task_id': task_id, 'available': False, 'files': [], 'workspace': meta}
        root = Path(self.root_for(task_id))
        if meta['mode'] == 'worktree':
            current_branch = self.git._branch(root)
            if current_branch != meta['branch']:
                meta['branch'] = current_branch
                self._save_metadata(task_id, meta)
        baseline = self._baseline(task_id)
        provenance = self._provenance(task_id, root)
        source_meta = self.source_context(task_id)[2] if meta['mode'] == 'worktree' else meta
        with self.db.connect() as conn:
            checkpoints = {r['path']: json.loads(r['state_json']) for r in conn.execute('SELECT * FROM coding_file_checkpoints WHERE task_id=?', (task_id,))}
        # Isolated worktrees own all their changes; direct workspaces expose
        # only manifested/reported paths, never unrelated user modifications.
        paths = set(provenance) | set(checkpoints)
        if meta['mode'] == 'worktree':
            changed = self.git._run(root, ['diff', '--name-only', '-z', '--no-renames', '--no-ext-diff', '--no-textconv', source_meta['base_head'], '--'])
            paths.update(path for path in changed.split('\0') if path and not (set(PurePosixPath(path).parts) & IGNORED))
            paths.update(row['path'] for row in self.git._files(root) if not (set(PurePosixPath(row['path']).parts) & IGNORED))
        files = []
        with self.lock(root):
            for path in sorted(paths):
                before, old = baseline.get(path, ({'exists': False, 'hash': None, 'mode': None, 'reviewable': True}, None))
                try: after, new = self._read(root, path)
                except WorkspaceError as exc:
                    files.append({'path': path, 'status': 'UNSUPPORTED', 'error': str(exc), 'revertible': False})
                    continue
                source_before, source_old = self.git_preimage(root, source_meta['base_head'], path) if meta['mode'] == 'worktree' else (before, old)
                task_changed = (before['hash'], before['mode']) != (after['hash'], after['mode'])
                incoming_changed = (source_before['hash'], source_before['mode']) != (after['hash'], after['mode'])
                if not task_changed and not incoming_changed:
                    continue
                inherited = not task_changed and incoming_changed
                display_before, display_old = (source_before, source_old) if inherited else (before, old)
                reviewable = before['reviewable'] and after['reviewable']
                source_reviewable = bool(source_meta.get('baseline_captured') and source_before['reviewable'] and after['reviewable'])
                checkpoint = checkpoints.get(path)
                external_edit = bool(reviewable and checkpoint and (checkpoint['hash'], checkpoint['mode']) != (after['hash'], after['mode']))
                binary = (b'\x00' in (display_old or b'') or b'\x00' in (new or b''))
                patch = ''
                if display_before['reviewable'] and after['reviewable'] and not binary:
                    try:
                        patch = ''.join(difflib.unified_diff((display_old or b'').decode('utf-8').splitlines(True), (new or b'').decode('utf-8').splitlines(True), fromfile='a/' + path, tofile='b/' + path))
                    except UnicodeDecodeError:
                        binary = True
                source_diff = ''
                if source_reviewable and b'\x00' not in (source_old or b'') and b'\x00' not in (new or b''):
                    try:
                        source_diff = ''.join(difflib.unified_diff((source_old or b'').decode('utf-8').splitlines(True), (new or b'').decode('utf-8').splitlines(True), fromfile='source/' + path, tofile='worktree/' + path))[:500_000]
                    except UnicodeDecodeError: pass
                files.append({'path': path, 'status': 'ADDED' if not display_before['exists'] else 'DELETED' if not after['exists'] else 'MODIFIED',
                              'before_hash': before['hash'], 'after_hash': after['hash'], 'before_mode': before['mode'], 'after_mode': after['mode'],
                              'diff': patch[:500_000], 'truncated': len(patch) > 500_000, 'binary': binary, 'reviewable': reviewable,
                              'inherited': inherited, 'source_diff': source_diff, 'source_reviewable': source_reviewable,
                              'sensitive': bool(before.get('sensitive') or after.get('sensitive') or source_before.get('sensitive') or sensitive_path(path)),
                              'externally_modified': external_edit,
                              'revertible': task_changed and reviewable and not external_edit and checkpoint is not None and task['status'] not in ACTIVE, 'phase_ids': sorted(set(provenance.get(path, [])))})
        revision = digest(json.dumps([(f['path'], f.get('before_hash'), f.get('after_hash'), f.get('before_mode'), f.get('after_mode')) for f in files], sort_keys=True).encode())
        return {'task_id': task_id, 'available': True, 'workspace': meta, 'revision': revision, 'files': files}

    def _idle(self, task_id, root=None):
        task = self.task(task_id)
        if task['status'] in ACTIVE:
            raise WorkspaceError('WAIT_FOR_TASK_TO_PAUSE')
        root = root or self.root_for(task_id)
        with self._lock:
            if self._running.get(str(root)) or self._running.get(str(self.root_for(task_id))):
                raise WorkspaceError('WAIT_FOR_TASK_TO_PAUSE')
        with self.db.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM coding_tasks WHERE status IN ('QUEUED','PLANNING','RUNNING','FINAL_REPORTING','QWEN_REPLANNING') AND archived=0")]
        for other in ids:
            meta = self.metadata(other)
            try: other_root = meta['root'] if meta else self.planned_root(other)
            except (WorkspaceError, OSError): continue
            if other_root == str(root):
                raise WorkspaceError('WORKSPACE_HAS_ACTIVE_TASK')

    def assert_idle(self, root):
        with self._lock:
            if self._running.get(str(root)):
                raise GitError('GIT_WAIT_FOR_CODING_TASK_TO_FINISH')

    def _write(self, root, path, state, contents):
        target = self._path(root, path)
        if not state['exists']:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        self._path(root, path)
        fd, temporary = tempfile.mkstemp(prefix='.olcr-review-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(contents or b'')
            os.chmod(temporary, state['mode'])
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def revert(self, task_id, path, revision):
        root = Path(self.root_for(task_id))
        with self.lock(root):
            self._idle(task_id, root)
            changes = self.changes(task_id)
            if revision != changes['revision']:
                raise WorkspaceError('REVIEW_CHANGED_REFRESH_REQUIRED')
            row = next((f for f in changes['files'] if f['path'] == path), None)
            if not row or not row['revertible']:
                raise WorkspaceError('FILE_CANNOT_BE_REVERTED')
            state, contents = self._baseline(task_id).get(path, ({'exists': False}, None))
            current, _ = self._read(root, path)
            if current['hash'] != row['after_hash'] or current['mode'] != row['after_mode']:
                raise WorkspaceError('FILE_CHANGED_REFRESH_REQUIRED')
            self._write(root, path, state, contents)
            return self.changes(task_id)

    def apply(self, task_id, revision, paths):
        meta = self.metadata(task_id)
        if not meta or meta['mode'] != 'worktree':
            raise WorkspaceError('ISOLATED_WORKTREE_REQUIRED')
        root, source = Path(self.root_for(task_id)), Path(meta['source_root'])
        with self.lock(root), self.lock(source):
            self._idle(task_id, source)
            changes = self.changes(task_id)
            if revision != changes['revision']:
                raise WorkspaceError('REVIEW_CHANGED_REFRESH_REQUIRED')
            if not paths or len(paths) != len(set(paths)):
                raise WorkspaceError('SELECT_CHANGED_FILES')
            selected = [f for f in changes['files'] if f['path'] in paths]
            if len(selected) != len(paths) or any(not f.get('source_reviewable') for f in selected):
                raise WorkspaceError('SELECT_REVIEWABLE_FILES')
            baseline = self._baseline(task_id)
            past, applied, source_meta = self.source_context(task_id)
            writes = []
            resulting_states = []
            for row in selected:
                path = row['path']
                missing = ({'exists': False, 'hash': None, 'mode': None}, None)
                before, _ = baseline.get(path, missing)
                expected, _ = self.git_preimage(root, source_meta['base_head'], path)
                current, old = self._read(source, path)
                after, new = self._read(root, path)
                resulting_states.append((path, after))
                if (current['hash'], current['mode']) == (after['hash'], after['mode']):
                    continue  # A repeat request is a no-op.
                acceptable = [expected, before, *past.get(path, []), *([applied[path]] if path in applied else [])]
                if (current['hash'], current['mode']) not in {(state['hash'], state['mode']) for state in acceptable}:
                    raise WorkspaceError('SOURCE_CHANGED_CONFLICT:' + path)
                if after['hash'] != row['after_hash'] or after['mode'] != row['after_mode']:
                    raise WorkspaceError('FILE_CHANGED_REFRESH_REQUIRED')
                writes.append((path, current, old, after, new))
            completed = []
            try:
                for path, current, old, after, new in writes:
                    self._write(source, path, after, new)
                    completed.append((path, current, old, after))
                with self.db.connect() as conn:
                    conn.executemany('INSERT INTO coding_source_applications VALUES(?,?,?,?) ON CONFLICT(root,path) DO UPDATE SET source_root=excluded.source_root,state_json=excluded.state_json',
                        [(str(root), path, str(source), json.dumps(state)) for path, state in resulting_states])
            except Exception:
                conflicts = []
                for path, current, old, after in reversed(completed):
                    live, _ = self._read(source, path)
                    if (live['hash'], live['mode']) == (after['hash'], after['mode']):
                        self._write(source, path, current, old)
                    else:
                        conflicts.append(path)
                if conflicts: raise WorkspaceError('SOURCE_ROLLBACK_CONFLICT:' + ','.join(conflicts))
                raise
            return {'status': 'APPLIED', 'paths': paths, 'source_root': str(source)}

    def steering(self, task_id):
        self.task(task_id)
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM coding_steering WHERE task_id=? ORDER BY created_at,id', (task_id,))]

    def instruction_context(self, task_id):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT message FROM coding_steering WHERE task_id=? AND status IN ('APPLIED','APPLYING') ORDER BY created_at DESC,id DESC LIMIT 20", (task_id,)).fetchall()
        return '\n\n'.join(row[0] for row in reversed(rows))[-20_000:]

    def steer(self, task_id, message, revision, *, queued=False, message_id=None, persist_message=True):
        task = self.task(task_id)
        if task.get('archived') or task['status'] == 'COMPLETED':
            raise WorkspaceError('TASK_NO_LONGER_ACCEPTS_INSTRUCTIONS')
        if not message.strip() or len(message) > 20_000:
            raise WorkspaceError('INSTRUCTION_REQUIRED')
        if revision != task['plan_revision']:
            raise WorkspaceError('PLAN_CHANGED_REFRESH_REQUIRED')
        identifier = message_id or str(uuid.uuid4())
        with self.db.connect() as conn:
            if conn.execute('SELECT 1 FROM coding_steering WHERE id=? AND task_id=?', (identifier, task_id)).fetchone():
                return self.steering(task_id)
            inserted = conn.execute('''INSERT INTO coding_steering
                SELECT ?,id,?,plan_revision,NULL,?,? FROM coding_tasks
                WHERE id=? AND plan_revision=? AND status!='COMPLETED' AND archived=0''',
                (identifier, message.strip(), 'WAITING' if queued else 'QUEUED', time.time(), task_id, revision))
            if not inserted.rowcount:
                raise WorkspaceError('PLAN_CHANGED_REFRESH_REQUIRED')
        if persist_message: self.db.add_message(task['conversation_id'], 'user', message.strip(), time.time(), identifier)
        return self.steering(task_id)

    def update_instruction(self, task_id, identifier, action, expected_message, expected_status, message=None):
        """Mutate an unclaimed instruction, atomically against runner claims."""
        with self.instruction_lock, self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            task = conn.execute('SELECT * FROM coding_tasks WHERE id=?', (task_id,)).fetchone()
            if not task:
                raise WorkspaceError('TASK_NOT_FOUND')
            if task['archived'] or task['status'] == 'COMPLETED':
                raise WorkspaceError('TASK_NO_LONGER_ACCEPTS_INSTRUCTIONS')
            row = conn.execute('SELECT * FROM coding_steering WHERE id=? AND task_id=?', (identifier, task_id)).fetchone()
            if not row:
                raise WorkspaceError('INSTRUCTION_NOT_FOUND')
            allowed_statuses = ({'WAITING', 'QUEUED', 'NEEDS_REVIEW'}
                                if action in {'edit', 'cancel', 'retry'} else {'WAITING', 'QUEUED'})
            if row['status'] not in allowed_statuses:
                raise WorkspaceError('INSTRUCTION_ALREADY_CLAIMED')
            if row['message'] != expected_message or row['status'] != expected_status:
                raise WorkspaceError('INSTRUCTION_CHANGED_REFRESH_REQUIRED')
            if action == 'edit':
                if not message or not message.strip() or len(message) > 20_000:
                    raise WorkspaceError('INSTRUCTION_REQUIRED')
                conn.execute('UPDATE coding_steering SET message=? WHERE id=?', (message.strip(), identifier))
                conn.execute("UPDATE messages SET content=? WHERE id=? AND conversation_id=? AND role='user'", (message.strip(), identifier, task['conversation_id']))
            elif action == 'cancel':
                conn.execute("UPDATE coding_steering SET status='CANCELLED' WHERE id=?", (identifier,))
                conn.execute("DELETE FROM messages WHERE id=? AND conversation_id=? AND role='user'", (identifier, task['conversation_id']))
            elif action == 'send_now':
                conn.execute("UPDATE coding_steering SET status='QUEUED',received_revision=? WHERE id=?", (task['plan_revision'], identifier))
            elif action == 'retry' and row['status'] == 'NEEDS_REVIEW':
                # A failed apply remains inert across restart/resume. Only this
                # explicit user action can put it back on the next boundary.
                conn.execute("UPDATE coding_steering SET status='QUEUED',received_revision=? WHERE id=?", (task['plan_revision'], identifier))
            else:
                raise WorkspaceError('INVALID_INSTRUCTION_ACTION')
            if action in {'edit', 'cancel'}:
                conn.execute('DELETE FROM conversation_memory_embeddings WHERE conversation_id=?', (task['conversation_id'],))
            conn.execute('UPDATE conversations SET updated_at=? WHERE id=?', (time.time(), task['conversation_id']))
            return dict(conn.execute('SELECT * FROM coding_steering WHERE id=?', (identifier,)).fetchone())

    def take_steering(self, task_id):
        # Claim only queued instructions at a phase/planning boundary.
        # NEEDS_REVIEW is durable user work after an interrupted apply and
        # must never be replayed by a process restart or task resume.
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = [dict(r) for r in conn.execute("SELECT * FROM coding_steering WHERE task_id=? AND status='QUEUED' ORDER BY created_at,id", (task_id,))]
            conn.execute("UPDATE coding_steering SET status='APPLYING' WHERE task_id=? AND status='QUEUED'", (task_id,))
        return rows

    def finish_steering(self, rows, revision, applied):
        with self.db.connect() as conn:
            conn.executemany('UPDATE coding_steering SET status=?,applied_revision=? WHERE id=?', [('APPLIED' if applied else 'NEEDS_REVIEW', revision if applied else None, row['id']) for row in rows])

    def recover_steering(self, task_id=None):
        with self.db.connect() as conn:
            sql = "UPDATE coding_steering SET status='NEEDS_REVIEW' WHERE status='APPLYING'"
            conn.execute(sql + (" AND task_id=?" if task_id else ""), (task_id,) if task_id else ())

    def restore_worktree(self, task_id):
        meta = self.metadata(task_id)
        if not meta or meta['mode'] != 'worktree' or not meta.get('removed'):
            raise WorkspaceError('REMOVED_WORKTREE_REQUIRED')
        source, _ = self.source(task_id)
        if str(source) != meta['source_root']:
            raise WorkspaceError('PROJECT_WORKSPACE_CHANGED')
        self.git._run(source, ['worktree', 'add', meta['root'], meta['branch']], timeout=60)
        self._mark_worktree(meta['root'], meta['branch'], False)
        meta['removed'] = False
        return meta

    def _mark_worktree(self, root, branch, removed):
        with self.db.connect() as conn:
            rows = conn.execute('SELECT task_id,metadata_json FROM coding_workspaces').fetchall()
            for row in rows:
                meta = json.loads(row['metadata_json'])
                if meta['mode'] == 'worktree' and meta['root'] == root:
                    meta.update(branch=branch, removed=removed)
                    conn.execute('UPDATE coding_workspaces SET metadata_json=? WHERE task_id=?', (json.dumps(meta), row['task_id']))

    def archive_worktree(self, task_id):
        meta = self.metadata(task_id)
        if not meta or meta['mode'] != 'worktree':
            raise WorkspaceError('ISOLATED_WORKTREE_REQUIRED')
        root = Path(self.root_for(task_id))
        with self.lock(root):
            self._idle(task_id)
            with self.db.connect() as conn:
                projects = conn.execute('SELECT workspace_path FROM projects WHERE archived=0 AND workspace_path IS NOT NULL').fetchall()
            if any(Path(row[0]).expanduser().resolve() == root for row in projects):
                raise WorkspaceError('WORKTREE_USED_AS_PROJECT_WORKSPACE')
            if self.git._files(root):
                raise WorkspaceError('COMMIT_OR_REVERT_WORKTREE_BEFORE_REMOVAL')
            meta['branch'] = self.git._branch(root)
            self.git._run(Path(meta['source_root']), ['worktree', 'remove', str(root)])
            # Branch/commits remain recoverable; no automatic branch deletion.
            self._mark_worktree(meta['root'], meta['branch'], True)
            meta['removed'] = True
            return meta


class TaskWorkspaceIntegration:
    """Lazy service construction lets API tests and isolated profiles swap DBs."""
    def __init__(self, get_db):
        self.get_db = get_db
        self._db = None
        self._service = None
        self._lock = threading.RLock()

    @property
    def service(self):
        with self._lock:
            db = self.get_db()
            if db is not self._db:
                self._service = TaskWorkspaces(db)
                self._db = db
            return self._service
