"""Local PC control surface for a trusted daemon/ACP process, never a phone HTTP API."""
from __future__ import annotations

import base64
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import fcntl

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .mobile_pairing import HappierPairing, PairingError
from .mobile_runs import MobileRuns

CAPABILITIES = {
    'protocol': 'acp-v1', 'text_prompt': True, 'history_load': True,
    'durable_event_cursor': True, 'cancel': True, 'permission_request': True,
    'resume_after_host_restart': False, 'individual_phone_revocation': False,
    'mobile_transport': False, 'model_config_write': False, 'elicitation': False,
    'coding_plan_approval_form': False, 'rich_blocks': 'olcr_extension',
    'reason': 'Phone transport and native LFM TTS belong to the next integration stage.',
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


class ApprovalInput(BaseModel):
    code: str = Field(min_length=1, max_length=20)


def install_mobile_api(app, *, db, settings, access, source, approve_request):
    runs = MobileRuns(db.path, source)
    pairings = {}
    current = {'relay': None}

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
        import shutil
        return {'capabilities': CAPABILITIES, 'bridge_descriptor_ready': access.path.is_file(),
                'happier_cli_available': bool(shutil.which('happier')),
                'relay': current['relay'], 'pairing_active': bool(current['relay'] and active_pairing().session)}

    @control.post('/pairing')
    async def pair(value: PairInput):
        if not value.consent:
            raise HTTPException(422, 'PAIRING_CONSENT_REQUIRED')
        try:
            service = pairings.get(value.relay_url)
            if not service:
                service = HappierPairing(value.relay_url, str(Path(db.path).resolve()))
                pairings[value.relay_url] = service
            result = await service.start()
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

    @api.get('/conversations/{identifier}')
    def history(identifier: str):
        active = runs.active(identifier)
        conversation = db.conversation(identifier)
        if not conversation:
            raise HTTPException(404, 'CONVERSATION_NOT_FOUND')
        if active:
            conversation['messages'] = [message for message in conversation.get('messages', [])
                if message.get('role') != 'assistant' or message.get('created_at', 0) < active['created_at']]
        return conversation | {'active_run': active}

    @api.get('/conversations/{identifier}/active-run')
    def active_run(identifier: str):
        return {'run': runs.active(identifier)}

    @api.post('/sessions')
    async def session(value: dict):
        if not isinstance(value.get('cwd'), str) or not Path(value['cwd']).is_absolute():
            raise HTTPException(422, 'ABSOLUTE_WORKSPACE_REQUIRED')
        cwd = Path(str(value.get('cwd', ''))).expanduser().resolve()
        roots = [Path(root).resolve() for root in settings().allowed_roots]
        if not cwd.is_dir() or not any(cwd == root or cwd.is_relative_to(root) for root in roots):
            raise HTTPException(403, 'WORKSPACE_NOT_AUTHORIZED')
        if value.get('mcpServers'):
            raise HTTPException(422, 'EXTERNAL_MCP_CONFIG_UNSUPPORTED')
        import time
        import uuid
        identifier = str(uuid.uuid4())
        db.create_conversation('Mobile conversation', time.time(), identifier, None)
        # Use OLCR's existing workspace setter, never just trust an ACP cwd.
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
