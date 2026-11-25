"""Local PC control surface for a trusted daemon/ACP process, never a phone HTTP API."""
from __future__ import annotations

import base64
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import fcntl

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool

from .mobile_pairing import HappierPairing, PairingError
from .mobile_runs import MobileRuns

CAPABILITIES = {
    'protocol': 'acp-v1', 'text_prompt': True, 'history_load': True,
    'mobile_library_protocol': 2,
    'durable_event_cursor': True, 'cancel': True, 'permission_request': True,
    'resume_after_host_restart': False, 'individual_phone_revocation': False,
    'mobile_transport': True, 'model_config_write': False, 'elicitation': False,
    'coding_plan_approval_form': False, 'rich_blocks': 'olcr_extension',
    'reason': 'Text sessions use the Happier daemon. Structured forms and native LFM TTS are not available.',
}


class BridgeAccess:
    def __init__(self, profile, port=None):
        Path(profile).mkdir(parents=True, exist_ok=True)
        self.lock = open(Path(profile) / 'mobile-host.lock', 'a')
        os.chmod(self.lock.name, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('OLCR_PROFILE_ALREADY_OWNED_BY_ANOTHER_BACKEND') from None
        self.token = secrets.token_urlsafe(32)
        self.path = Path(profile) / 'mobile-host.json'
        if port:
            descriptor = {'api_version': 1, 'pid': os.getpid(), 'base_url': f'http://127.0.0.1:{int(port)}/api/mobile/bridge',
                          'token': self.token}
            fd, temp = tempfile.mkstemp(prefix='.mobile-host-', dir=profile)
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(descriptor, file)
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)

    def close(self):
        if self.lock.closed:
            return
        try:
            if self.path.is_file() and json.loads(self.path.read_text()).get('token') == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def permits(self, request):
        return (request.url.path.startswith('/api/mobile/bridge/')
                and request.client is not None and request.client.host in {'127.0.0.1', '::1', 'testclient'}
                and not request.headers.get('origin')
                and hmac.compare_digest(request.headers.get('X-OLCR-Bridge', ''), self.token))


class RunInput(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=100_000)
    language: str = Field(default='ja', pattern='^(ja|en)$')


class PairInput(BaseModel):
    relay_url: str = Field(default='https://api.happier.dev', max_length=2048)
    consent: bool = False
    workspace: str | None = Field(default=None, max_length=4096)


class ApprovalInput(BaseModel):
    code: str = Field(min_length=1, max_length=20)


class ProjectRenameInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ConversationRenameInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class MobilePinInput(BaseModel):
    pinned: StrictBool


class YouTubeSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    language: str = Field(default='ja', pattern='^(ja|en)$')


def install_mobile_api(app, *, db, settings, access, source, approve_request, assistant_service=None):
    runs = MobileRuns(db.path, source)
    pairings = {}
    current = {'relay': None}
    from .mobile_daemon import cli_path, prepare_daemon

    def workspaces():
        roots = [Path(root).resolve() for root in settings().allowed_roots]
        return [str(root) for root in roots if root.is_dir()]

    def pairing_workspace():
        roots = workspaces()
        if roots:
            return roots[0]
        # Pairing is to the OLCR instance. This private empty directory gives
        # ACP a stable cwd without granting access to an unapproved user folder.
        root = Path(db.path).resolve().parent / 'mobile-no-workspace'
        if root.is_symlink():
            raise HTTPException(409, 'MOBILE_WORKSPACE_UNAVAILABLE')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        return str(root.resolve())

    def conversation_workspace(conversation):
        """Return (authorized folder, attachable) for a persisted OLCR chat."""
        from .working_folders import WorkingFolders
        attachment = WorkingFolders(db).get(conversation['id'], validate=False)
        root = attachment.get('root') if attachment else None
        if not root and conversation.get('project_id'):
            project = db.project(conversation['project_id'])
            root = project.get('workspace_path') if project else None
        if not root:
            return None, True
        try:
            resolved = Path(root).expanduser().resolve(strict=True)
        except OSError:
            return None, False
        roots = [Path(item).resolve() for item in settings().allowed_roots]
        allowed = any(resolved == item or resolved.is_relative_to(item) for item in roots)
        return (str(resolved) if resolved.is_dir() else None), allowed and resolved.is_dir()


    def bridge(request: Request):
        if not access.permits(request):
            raise HTTPException(401, 'LOCAL_BRIDGE_AUTH_REQUIRED')

    def desktop(request: Request):
        token = os.environ.get('OLCR_GUI_SESSION_TOKEN', '')
        if (not token or not hmac.compare_digest(request.headers.get('X-OLCR-Session', ''), token)
                or not request.client or request.client.host not in {'127.0.0.1', '::1', 'testclient'}):
            raise HTTPException(401, 'DESKTOP_SESSION_REQUIRED')

    control = APIRouter(prefix='/api/mobile', dependencies=[Depends(desktop)])
    api = APIRouter(prefix='/api/mobile/bridge', dependencies=[Depends(bridge)])

    def active_pairing():
        service = pairings.get(current['relay'])
        if not service:
            raise HTTPException(409, 'NO_ACTIVE_PAIRING')
        return service

    @control.get('/status')
    def status():
        return {'capabilities': CAPABILITIES, 'bridge_descriptor_ready': access.path.is_file(),
                'happier_cli_available': bool(cli_path()), 'workspaces': workspaces(),
                'relay': current['relay'], 'pairing_active': bool(current['relay'] and active_pairing().session)}

    @control.post('/pairing')
    async def pair(value: PairInput):
        if not value.consent:
            raise HTTPException(422, 'PAIRING_CONSENT_REQUIRED')
        from urllib.parse import urlsplit
        relay = urlsplit(value.relay_url)
        if relay.scheme != 'https' or relay.path not in ('', '/'):
            raise HTTPException(422, 'MOBILE_RELAY_HTTPS_ORIGIN_REQUIRED')
        if not access.path.is_file():
            raise HTTPException(409, 'LOCAL_BRIDGE_UNAVAILABLE')
        if not cli_path():
            raise HTTPException(409, 'HAPPIER_CLI_NOT_INSTALLED')
        # Older clients may provide a workspace. New pairings identify the PC
        # first and use its default authorized root, or a private empty cwd.
        workspace = value.workspace or pairing_workspace()
        if not Path(workspace).is_absolute() or (workspace not in workspaces() and workspace != pairing_workspace()):
            raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
        try:
            service = pairings.get(value.relay_url)
            if not service:
                service = HappierPairing(value.relay_url, str(Path(db.path).resolve()))
                pairings[value.relay_url] = service
            async def prepare(active):
                await prepare_daemon(active, access.path.parent, access.path, workspace)
            result = await service.start(prepare=prepare, workspace=workspace)
            current['relay'] = value.relay_url
            import qrcode
            from qrcode.image.svg import SvgPathImage
            image = qrcode.make(result['url'], image_factory=SvgPathImage)
            buffer = io.BytesIO()
            image.save(buffer)
            return result | {'qr_data_url': 'data:image/svg+xml;base64,' + base64.b64encode(buffer.getvalue()).decode()}
        except PairingError as exc:
            raise HTTPException(409, str(exc)) from None

    @control.get('/pairing')
    async def pairing_status():
        try:
            return await active_pairing().status()
        except PairingError as exc:
            raise HTTPException(409, str(exc)) from None

    @control.post('/pairing/approve')
    async def pairing_approve(value: ApprovalInput):
        try:
            return await active_pairing().approve(value.code)
        except PairingError as exc:
            raise HTTPException(409, str(exc)) from None

    @control.delete('/pairing')
    async def pairing_cancel():
        try:
            return await active_pairing().cancel()
        except PairingError as exc:
            raise HTTPException(409, str(exc)) from None

    @api.get('/capabilities')
    def capabilities():
        return CAPABILITIES

    @api.post('/youtube/search')
    def youtube_search(value: YouTubeSearchInput):
        if assistant_service is None:
            raise HTTPException(503, 'YOUTUBE_SEARCH_UNAVAILABLE')
        from urllib.parse import urlencode
        from .assistant_actions import ActionError, youtube_request
        from .assistant_integrations import search_youtube
        service = assistant_service()
        try:
            service.network()
            query, order = youtube_request(value.query)
            candidates = search_youtube(query, service.youtube_key, **({'order': 'date'} if order == 'date' else {}))
        except ActionError as exc:
            code = str(exc)
            if code not in {'YOUTUBE_SEARCH_FAILED', 'YOUTUBE_NO_RESULTS'}:
                raise HTTPException(422, code) from None
            query, _ = youtube_request(value.query)
            return {'query': query, 'candidates': [],
                    'search_url': 'https://www.youtube.com/results?' + urlencode({'search_query': query}),
                    'error': code}
        candidates = [item for item in candidates[:10]
                      if isinstance(item, dict) and isinstance(item.get('id'), str)
                      and re.fullmatch(r'[A-Za-z0-9_-]{11}', item['id'])]
        return {'query': query, 'candidates': candidates}

    @api.get('/projects')
    def project_directory():
        roots = [Path(root).resolve() for root in settings().allowed_roots]
        result = []
        for project in db.projects():
            workspace = project.get('workspace_path')
            safe_workspace = None
            if workspace:
                try:
                    resolved = Path(workspace).expanduser().resolve(strict=True)
                    if resolved.is_dir() and any(resolved == root or resolved.is_relative_to(root) for root in roots):
                        safe_workspace = str(resolved)
                except OSError:
                    pass
            result.append({'id': project['id'], 'name': project['name'],
                           'workspace_path': safe_workspace,
                           'workspace_available': not workspace or safe_workspace is not None,
                           'updated_at': project.get('updated_at', 0),
                           'pinned': bool(project.get('pinned'))})
        return {'projects': result}

    @api.get('/conversations/{identifier}')
    def history(identifier: str):
        feed = runs.feed(identifier)
        active = feed['runs'][0] if feed['runs'] else None
        conversation = db.conversation(identifier)
        if not conversation:
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        workspace, available = conversation_workspace(conversation)
        if not available:
            raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
        # A run can start and finish while the conversation snapshot is being
        # read. Keep its output out of history so the cursor observer delivers
        # it once, and hand that observer the cursor captured before the read.
        just_started = runs.feed(identifier, after=feed['cursor'])['runs']
        observed_runs = list(just_started)
        if active:
            observed_runs.append(active)
        omit_assistant_after = min((row['created_at'] for row in observed_runs), default=None)
        pc_runs = [row for row in observed_runs if row.get('source') == 'pc']
        omit_user_after = min((row['created_at'] for row in pc_runs), default=None)
        if omit_assistant_after is not None:
            conversation['messages'] = [message for message in conversation.get('messages', [])
                if message.get('role') != 'assistant' or message.get('created_at', 0) < omit_assistant_after]
        if omit_user_after is not None:
            conversation['messages'] = [message for message in conversation.get('messages', [])
                if message.get('role') != 'user' or message.get('created_at', 0) < omit_user_after]
        return conversation | {'active_run': active, 'run_cursor': feed['cursor'], 'workspace': workspace}

    @api.get('/conversations')
    def conversation_directory(offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100),
                               project_id: str | None = None, projectless: bool = False):
        if project_id is not None and projectless:
            raise HTTPException(422, 'CONVERSATION_GROUP_INVALID')
        if project_id is not None and not db.project(project_id):
            raise HTTPException(404, 'PROJECT_NOT_FOUND')
        with db.connect() as conn:
            where = ' WHERE c.project_id=?' if project_id is not None else (' WHERE c.project_id IS NULL' if projectless else '')
            args = (project_id, limit + 1, offset) if project_id is not None else (limit + 1, offset)
            rows = conn.execute('''SELECT c.*, p.name AS project_name, p.workspace_path AS project_workspace
                FROM conversations c LEFT JOIN projects p ON p.id=c.project_id''' + where + '''
                ORDER BY c.pinned DESC, c.updated_at DESC, c.created_at DESC LIMIT ? OFFSET ?''', args).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        result = []
        for raw in rows:
            conversation = dict(raw)
            workspace, available = conversation_workspace(conversation)
            if not available:
                continue
            result.append({
                'id': conversation['id'], 'title': conversation.get('title') or 'OLCRの会話',
                'project_id': conversation.get('project_id'),
                'project_name': conversation.get('project_name'),
                'created_at': conversation.get('created_at'),
                'updated_at': conversation.get('updated_at'),
                'pinned': bool(conversation.get('pinned')),
                'workspace': workspace or pairing_workspace(),
            })
        return {'conversations': result, 'offset': offset, 'next_offset': offset + limit if has_more else None}

    @api.patch('/projects/{identifier}')
    def rename_project(identifier: str, value: ProjectRenameInput):
        name = value.name.strip()
        if not name:
            raise HTTPException(422, 'PROJECT_NAME_REQUIRED')
        result = db.update_project(identifier, name, None, None, time.time())
        if not result:
            raise HTTPException(404, 'PROJECT_NOT_FOUND')
        return result | {'pinned': bool(result.get('pinned'))}

    @api.put('/projects/{identifier}/pin')
    def pin_project(identifier: str, value: MobilePinInput):
        result = db.pin_project(identifier, value.pinned)
        if not result:
            raise HTTPException(404, 'PROJECT_NOT_FOUND')
        return result | {'pinned': bool(result.get('pinned'))}

    @api.delete('/projects/{identifier}')
    def delete_project(identifier: str):
        project = db.project(identifier)
        if not project:
            return {'deleted': True, 'project_id': identifier}
        active = [item['id'] for item in db.conversations(identifier) if runs.active(item['id'])]
        if active:
            raise HTTPException(409, 'PROJECT_HAS_ACTIVE_RUNS')
        if not db.delete_project(identifier):
            raise HTTPException(404, 'PROJECT_NOT_FOUND')
        return {'deleted': True, 'project_id': identifier}

    @api.patch('/conversations/{identifier}')
    def rename_conversation(identifier: str, value: ConversationRenameInput):
        conversation = db.conversation(identifier)
        if not conversation:
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        _, available = conversation_workspace(conversation)
        if not available:
            raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
        updated = db.rename_conversation(identifier, value.title, time.time())
        if not updated:
            raise HTTPException(422, 'CONVERSATION_TITLE_REQUIRED')
        return updated | {'pinned': bool(updated.get('pinned'))}

    @api.put('/conversations/{identifier}/pin')
    def pin_conversation(identifier: str, value: MobilePinInput):
        conversation = db.conversation(identifier)
        if not conversation:
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        _, available = conversation_workspace(conversation)
        if not available:
            raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
        updated = db.pin_conversation(identifier, value.pinned, conversation.get('project_id'))
        if not updated:
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        return updated | {'pinned': bool(updated.get('pinned'))}

    @api.delete('/conversations/{identifier}')
    def delete_conversation(identifier: str):
        conversation = db.conversation(identifier)
        if not conversation:
            return {'deleted': True, 'conversation_id': identifier}
        _, available = conversation_workspace(conversation)
        if not available:
            raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
        if runs.active(identifier):
            raise HTTPException(409, 'CONVERSATION_RUN_ACTIVE')
        with db.connect() as conn:
            deleted = conn.execute('DELETE FROM conversations WHERE id=?', (identifier,)).rowcount
        if not deleted:
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        db.delete_memory_for_conversation(identifier)
        return {'deleted': True, 'conversation_id': identifier}

    @api.get('/conversations/{identifier}/active-run')
    def active_run(identifier: str):
        return {'run': runs.active(identifier)}

    @api.get('/conversations/{identifier}/runs')
    def conversation_runs(identifier: str, after: int = Query(default=0, ge=0)):
        conversation = db.conversation(identifier)
        if not conversation:
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        _, available = conversation_workspace(conversation)
        if not available:
            raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
        return runs.feed(identifier, after=after)

    @api.post('/sessions')
    async def session(value: dict):
        if not isinstance(value.get('cwd'), str) or not Path(value['cwd']).is_absolute():
            raise HTTPException(422, 'ABSOLUTE_WORKSPACE_REQUIRED')
        cwd = Path(str(value.get('cwd', ''))).expanduser().resolve()
        # Match workspaces(): dead configured paths are not usable roots and
        # must not disable the private scratch cwd for projectless sessions.
        roots = [Path(root).resolve() for root in settings().allowed_roots
                 if Path(root).expanduser().is_dir()]
        no_workspace_root = Path(db.path).resolve().parent / 'mobile-no-workspace'
        no_workspace = not roots and cwd == no_workspace_root.resolve()
        authorized = any(cwd == root or cwd.is_relative_to(root) for root in roots)
        if not cwd.is_dir() or not (authorized or no_workspace):
            raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
        if value.get('mcpServers'):
            raise HTTPException(422, 'EXTERNAL_MCP_CONFIG_UNSUPPORTED')
        existing_id = value.get('existing_conversation_id')
        if existing_id is not None:
            if not isinstance(existing_id, str) or not existing_id:
                raise HTTPException(422, 'INVALID_CONVERSATION_ID')
            conversation = db.conversation(existing_id)
            if not conversation:
                raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
            workspace, available = conversation_workspace(conversation)
            if not available:
                raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
            expected = workspace or pairing_workspace()
            if cwd != Path(expected).resolve():
                raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
            return {'sessionId': existing_id}
        project_id = value.get('project_id')
        if project_id is not None:
            if not isinstance(project_id, str) or not project_id:
                raise HTTPException(422, 'INVALID_PROJECT_ID')
            project = db.project(project_id)
            if not project:
                raise HTTPException(404, 'PROJECT_NOT_FOUND')
            project_workspace = project.get('workspace_path')
            if project_workspace:
                try:
                    project_root = Path(project_workspace).expanduser().resolve(strict=True)
                except OSError:
                    raise HTTPException(403, 'PROJECT_WORKSPACE_NOT_AUTHORIZED') from None
                if (not project_root.is_dir() or not any(project_root == root or project_root.is_relative_to(root)
                        for root in roots) or cwd != project_root):
                    raise HTTPException(403, 'PROJECT_WORKSPACE_NOT_AUTHORIZED')
        import time
        import uuid
        identifier = str(uuid.uuid4())
        db.create_conversation('新しい会話', time.time(), identifier, project_id,
                               title_auto_pending=True)
        # A workspace-less PC pairing may create general conversations in its
        # private scratch cwd. Attach only directories already authorized by OLCR.
        if authorized:
            from .working_folders import WorkingFolders
            WorkingFolders(db).attach(identifier, {'directoryPath': str(cwd)})
        return {'sessionId': identifier}

    @api.post('/runs')
    async def start_run(value: RunInput):
        if not db.conversation(value.conversation_id):
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        try:
            return runs.start(value.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @api.get('/runs/{identifier}')
    def run(identifier: str, after: int = Query(default=0, ge=0)):
        try:
            return {'run': runs.get(identifier), 'events': runs.events(identifier, after)}
        except KeyError:
            raise HTTPException(404, 'RUN_NOT_FOUND') from None

    @api.post('/runs/{identifier}/cancel')
    def cancel(identifier: str):
        try:
            return runs.cancel(identifier)
        except KeyError:
            raise HTTPException(404, 'RUN_NOT_FOUND') from None

    @api.post('/permissions/{identifier}')
    def permission(identifier: str, value: dict):
        return approve_request(identifier, value.get('approved') is True)

    app.include_router(control)
    app.include_router(api)
    app.add_event_handler('shutdown', runs.close)
    app.add_event_handler('shutdown', access.close)
    return runs
