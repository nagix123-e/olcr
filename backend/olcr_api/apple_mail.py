"""Local read and requested-send access to the user's macOS Mail account."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_BODY_CHARS = 200_000


class AppleMailConnector:
    def __init__(self, network_allowed=lambda: True, runner=None):
        self._network_allowed = network_allowed
        self.runner = runner

    def helper(self):
        value = os.environ.get('OLCR_MACOS_MAIL_HELPER')
        candidate = Path(value) if value else Path(__file__).resolve().parents[2] / 'desktop/src-tauri/binaries/olcr-macos-mail'
        return candidate if sys.platform == 'darwin' and candidate.is_file() else None

    def _call(self, operation, **fields):
        if self.runner:
            value = self.runner({'operation': operation, **fields})
        else:
            helper = self.helper()
            if not helper:
                if operation in {'status', 'icon'}:
                    return {'available': False, 'authorized': False, 'permission': 'unavailable'}
                raise ValueError('APPLE_MAIL_UNAVAILABLE')
            try:
                result = subprocess.run([str(helper)], input=json.dumps({'operation': operation, **fields}).encode('utf-8'),
                    capture_output=True, timeout=130 if operation in {'connect', 'send'} else 30, check=False)
                raw = result.stdout
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError('APPLE_MAIL_RESPONSE_TOO_LARGE')
                value = json.loads(raw)
            except subprocess.TimeoutExpired as exc:
                raise ValueError('APPLE_MAIL_REQUEST_TIMEOUT') from exc
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError('APPLE_MAIL_REQUEST_FAILED') from exc
        if not isinstance(value, dict):
            raise ValueError('APPLE_MAIL_INVALID_RESPONSE')
        code = value.get('error')
        if code:
            safe = code if isinstance(code, str) and re.fullmatch(r'[A-Z_]+', code) else 'APPLE_MAIL_REQUEST_FAILED'
            raise ValueError(safe)
        return value

    def status(self):
        result = self._call('status')
        if not result.get('available'):
            state = 'unavailable'
        elif result.get('authorized'):
            state = 'connected'
        else:
            state = {'denied':'permission_denied', 'not_running':'not_running', 'error':'permission_error'}.get(result.get('permission'),'permission_required')
        return {**result, 'state': state, 'provider': 'apple'}

    def connect(self):
        result = self._call('connect')
        if not result.get('authorized'):
            raise ValueError('APPLE_MAIL_AUTOMATION_PERMISSION_REQUIRED')
        return result

    def icon(self):
        return self._call('icon')

    def execute(self, action, arguments):
        if not self._network_allowed():
            raise ValueError('EXTERNAL_ACCESS_REQUIRED')
        if not isinstance(arguments, dict):
            raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        allowed = {'list': {'query', 'limit'}, 'get': {'id'}, 'send': {'to', 'cc', 'bcc', 'subject', 'body'}}
        if action not in allowed or set(arguments) - allowed[action]:
            raise ValueError('CONNECTOR_ACTION_UNSUPPORTED')
        if action == 'send':
            from .mail_message import prepare_mail
            mail = prepare_mail(arguments)
            result = self._call('send', **{key: mail[key] for key in ('to', 'cc', 'bcc', 'subject', 'body')})
            return {'sent': True, 'provider': 'apple_mail', 'recipient_count': mail['recipient_count'],
                    'message_id': result.get('message_id')}
        query = arguments.get('query', '')
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        if action == 'list':
            limit = arguments.get('limit', 10)
            if type(limit) is not int or not 1 <= limit <= 50:
                raise ValueError('CONNECTOR_ARGUMENT_INVALID')
            result = self._call('list', limit=50)
            messages = result.get('messages')
            if not isinstance(messages, list):
                raise ValueError('APPLE_MAIL_INVALID_RESPONSE')
            if query:
                needle = query.casefold()
                messages = [item for item in messages if needle in (str(item.get('subject', '')) + '\n' + str(item.get('sender', ''))).casefold()]
            return {'messages': messages[:limit], 'resultSizeEstimate': len(messages), 'mailbox': 'INBOX'}
        ident = arguments.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'\d{1,20}', ident):
            raise ValueError('CONNECTOR_ARGUMENT_INVALID')
        result = self._call('get', id=ident)
        content = result.get('content', '')
        if not isinstance(content, str):
            raise ValueError('APPLE_MAIL_INVALID_RESPONSE')
        result['content'] = content[:MAX_BODY_CHARS]
        result['content_truncated'] = len(content) > MAX_BODY_CHARS or result.get('content_truncated') is True
        return result
