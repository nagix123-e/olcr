"""Application permission choices, independent of routing and execution contracts."""
from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
import hashlib
import secrets
import threading
import time
from typing import Literal

PermissionLevel = Literal['confirm', 'automatic', 'full']
LEVELS = ('confirm', 'automatic', 'full')
request_approved: ContextVar[bool] = ContextVar('olcr_permission_request_approved', default=False)


def level(settings) -> str:
    return getattr(settings, 'permission_level', 'automatic')


def full_access(settings) -> bool:
    return level(settings) == 'full'


def capability_enabled(settings, name: str) -> bool:
    return full_access(settings) or bool(getattr(settings, name))


def requires_request_approval(method: str, path: str) -> bool:
    """Only execution entrypoints. Polling, Stop and configuration stay usable.

    A chat request can autonomously choose network/tools, so confirm mode
    approves the entire request before dispatch, not after it has side effects.
    """
    if method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return False
    if path.startswith(('/api/commerce-sessions/', '/api/agent-tools', '/api/browser-forms/')):
        return method == 'POST'
    if path.startswith('/api/connectors/'):
        return method == 'POST' and path.endswith('/execute')
    if path.startswith('/api/symptom-consultations/'):
        return method == 'POST'
    if path in {'/api/chat', '/api/chat/stream', '/api/mobile/bridge/runs', '/api/agents/builder/chat', '/api/commands', '/api/web/search', '/api/web/fetch', '/api/files/index'}:
        return True
    if path.startswith('/api/assistant/'):
        return path.endswith('/confirm') or path == '/api/assistant/media/player'
    if '/git/' in path:
        return path.endswith(('/prepare', '/approve'))
    if path.endswith('/verify'):
        return False
    return path.startswith(('/api/tools/', '/api/web/browser/', '/api/web/recipes/', '/api/scrape/', '/api/computer/actions/',
                            '/api/computer/goals', '/api/local-e2e/')) and not path.endswith(('/cancel', '/stop')) and method == 'POST'


@dataclass
class Grant:
    fingerprint: str
    expires_at: float
    approved: bool = False


class RequestApprovals:
    """Single-use, short-lived grants bound to the exact method, URL and body."""
    def __init__(self):
        self._lock = threading.Lock()
        self._grants: dict[str, Grant] = {}

    @staticmethod
    def fingerprint(method: str, url: str, body: bytes) -> str:
        return hashlib.sha256(method.encode() + b'\0' + url.encode() + b'\0' + body).hexdigest()

    def issue(self, fingerprint: str) -> str:
        with self._lock:
            now = time.monotonic()
            self._grants = {k: v for k, v in self._grants.items() if v.expires_at > now}
            if len(self._grants) >= 512:
                self._grants.pop(next(iter(self._grants)))
            token = secrets.token_urlsafe(32)
            self._grants[token] = Grant(fingerprint, now + 300)
            return token

    def approve(self, token: str, approved: bool) -> bool:
        with self._lock:
            grant = self._grants.get(token)
            if not grant or grant.expires_at <= time.monotonic():
                self._grants.pop(token, None)
                return False
            if not approved:
                self._grants.pop(token, None)
            else:
                grant.approved = True
            return True

    def consume(self, token: str, fingerprint: str) -> bool:
        with self._lock:
            grant = self._grants.get(token)
            if not grant or not grant.approved or grant.expires_at <= time.monotonic() or grant.fingerprint != fingerprint:
                return False
            del self._grants[token]
            return True

    def clear(self):
        with self._lock:
            self._grants.clear()
