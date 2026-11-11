"""Calendar access through the bundled, signed macOS EventKit helper."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

from .assistant_integrations import ActionError, GoogleCalendar


class AppleCalendar:
    provider = 'apple'
    native = True

    def __init__(self, db, runner=None):
        self.db, self.runner = db, runner
        self.network_guard = lambda: None
        with db.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS assistant_native_calendar_connection '
                        '(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL)')

    def helper(self):
        value = os.environ.get('OLCR_MACOS_CALENDAR_HELPER')
        candidate = Path(value) if value else Path(__file__).resolve().parents[2] / 'desktop/src-tauri/binaries/olcr-macos-calendar'
        return candidate if sys.platform == 'darwin' and candidate.is_file() else None

    def _call(self, operation, **fields):
        if self.runner:
            value = self.runner({'operation': operation, **fields})
        else:
            helper = self.helper()
            if not helper:
                raise ActionError('MACOS_CALENDAR_UNAVAILABLE')
            try:
                result = subprocess.run([str(helper)], input=json.dumps({'operation': operation, **fields}),
                    capture_output=True, text=True, timeout=130 if operation == 'connect' else 30, check=False)
                value = json.loads(result.stdout)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                # Mutation may have reached EventKit; the host marks the action
                # uncertain rather than blindly replaying it after a lost reply.
                raise ActionError('CALENDAR_NETWORK_FAILED') from exc
        if not isinstance(value, dict):
            raise ActionError('CALENDAR_REQUEST_FAILED')
        code = value.get('error')
        if code:
            if operation == 'connect' and code == 'CALENDAR_PERMISSION_DENIED':
                code = 'CALENDAR_ACCESS_DENIED'
            raise ActionError(code if isinstance(code, str) and re.fullmatch(r'[A-Z_]+', code)
                              else 'CALENDAR_REQUEST_FAILED')
        return value

    def status(self):
        try:
            value = self._call('status')
        except ActionError as exc:
            value = {'available': False, 'authorized': False, 'permission': 'unavailable', 'error': str(exc)}
        with self.db.connect() as con:
            row = con.execute('SELECT enabled FROM assistant_native_calendar_connection WHERE id=1').fetchone()
        return {**value, 'provider': 'apple', 'native': True,
                'connected': bool(row and row[0] and value.get('authorized')),
                'client_configured': True, 'credential_pending': False, 'reauth_required': False}

    def icon(self):
        """Read the current Calendar app icon from macOS without requesting calendar access."""
        return self._call('icon')

    def start(self, language='ja'):
        value = self._call('connect')
        if not value.get('authorized'):
            raise ActionError('CALENDAR_ACCESS_DENIED')
        with self.db.connect() as con:
            con.execute('INSERT INTO assistant_native_calendar_connection VALUES(1,1) '
                        'ON CONFLICT(id) DO UPDATE SET enabled=1')
        return self.status()

    def disconnect(self):
        with self.db.connect() as con:
            con.execute('INSERT INTO assistant_native_calendar_connection VALUES(1,0) '
                        'ON CONFLICT(id) DO UPDATE SET enabled=0')
        return self.status()

    def configure(self, *args):
        raise ActionError('CALENDAR_NATIVE_SETUP')

    def export(self):
        raise ActionError('CALENDAR_NATIVE_SETUP')

    def acknowledge(self, generation):
        return self.status()

    def _connected(self):
        if not self.status()['connected']:
            raise ActionError('CALENDAR_NOT_CONNECTED')

    def calendars(self):
        self._connected()
        return self._call('calendars').get('calendars', [])

    def events(self, calendar_id, start, end, query=''):
        self._connected()
        return self._call('events', calendar_id=calendar_id, start=start.isoformat(), end=end.isoformat(), query=query)

    event_path = staticmethod(GoogleCalendar.event_path)

    def request(self, method, path, *, params=None, payload=None, etag=None):
        self._connected()
        match = re.fullmatch(r'calendars/([^/]+)/events(?:/([A-Za-z0-9_-]{1,1024}))?', path)
        if not match or method not in {'GET', 'POST', 'PATCH', 'DELETE'}:
            raise ActionError('CALENDAR_EVENT_INVALID')
        operation = {'GET': 'get', 'POST': 'create', 'PATCH': 'update', 'DELETE': 'delete'}[method]
        with self.db.connect() as con:
            row = con.execute('SELECT value_json FROM assistant_preferences WHERE id=1').fetchone()
        zone = json.loads(row[0]).get('time_zone', 'UTC') if row else 'UTC'
        return self._call(operation, calendar_id=unquote(match[1]), event_id=match[2] or '',
                          payload=payload, etag=etag, time_zone=zone)

    @staticmethod
    def public_event(event):
        return {key: event.get(key) for key in ('id', 'summary', 'start', 'end', 'etag', 'provider')}
