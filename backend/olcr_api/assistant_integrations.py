"""Calendar account access and official YouTube playback for chat actions.

OAuth secrets live in memory and the desktop Keychain, never in the settings DB.
The loopback OAuth listener and media tickets are independent of GUI credentials.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx


class ActionError(ValueError):
    pass


CALENDAR_HANDLE = 'OLCR_GOOGLE_CALENDAR_CREDENTIAL'
YOUTUBE_HANDLE = 'OLCR_YOUTUBE_API_KEY'
SCOPES = 'https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.calendarlist.readonly'


class GoogleCalendar:
    def __init__(self, client=None):
        self.http = client or httpx.Client(timeout=20, follow_redirects=False)
        self.lock = threading.RLock()
        self.credentials = {}
        try:
            stored = json.loads(os.environ.get(CALENDAR_HANDLE, '{}'))
            if isinstance(stored, dict):
                self.credentials = {k: str(v) for k, v in stored.items()
                                    if k in {'client_id', 'client_secret', 'refresh_token'} and v}
        except ValueError:
            pass
        self.credentials.setdefault('client_id', os.environ.get('OLCR_GOOGLE_CALENDAR_CLIENT_ID', ''))
        self.credentials.setdefault('client_secret', os.environ.get('OLCR_GOOGLE_CALENDAR_CLIENT_SECRET', ''))
        self.access_token = ''
        self.expires_at = 0
        self.generation = 0
        self.saved_generation = 0
        self.attempt = None
        self.reauth_required = False
        self.network_guard = lambda: None

    def status(self):
        with self.lock:
            attempt = self.attempt
            if attempt and attempt['status'] == 'pending' and time.monotonic() > attempt['deadline']:
                self._finish('expired')
            return {'connected': bool(self.credentials.get('refresh_token')) and not self.reauth_required,
                    'client_configured': bool(self.credentials.get('client_id')),
                    'reauth_required': self.reauth_required,
                    'credential_pending': self.generation > self.saved_generation,
                    'attempt': ({k: attempt[k] for k in ('id', 'status', 'authorization_url', 'error') if k in attempt}
                                if attempt else None)}

    def configure(self, client_id: str, client_secret: str = ''):
        client_id = client_id.strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{8,250}\.apps\.googleusercontent\.com', client_id):
            raise ActionError('CALENDAR_CLIENT_ID_INVALID')
        if len(client_secret) > 1000 or any(ord(c) < 32 for c in client_secret):
            raise ActionError('CALENDAR_CLIENT_SECRET_INVALID')
        with self.lock:
            if self.attempt and self.attempt['status'] in {'pending', 'exchanging'}:
                self._finish('cancelled')
            if self.credentials.get('client_id') != client_id:
                self.disconnect()
                self.credentials = {'client_id': client_id}
            if client_secret:
                self.credentials['client_secret'] = client_secret.strip()
            self.generation += 1
        return self.status()

    def export(self):
        with self.lock:
            return {'handle': CALENDAR_HANDLE, 'generation': self.generation,
                    'value': json.dumps(self.credentials, separators=(',', ':'))}

    def acknowledge(self, generation: int):
        with self.lock:
            if generation == self.generation:
                self.saved_generation = generation
        return self.status()

    def disconnect(self):
        with self.lock:
            if self.attempt and self.attempt['status'] in {'pending', 'exchanging'}:
                self._finish('cancelled')
            self.credentials.pop('refresh_token', None)
            self.access_token, self.expires_at = '', 0
            self.reauth_required = False
            self.generation += 1
        return self.status()

    def _finish(self, status, error=''):
        if self.attempt:
            self.attempt['status'] = status
            self.attempt['error'] = error
            self.attempt['stop'].set()

    def start(self, language='ja'):
        with self.lock:
            if not self.credentials.get('client_id'):
                raise ActionError('CALENDAR_CLIENT_REQUIRED')
            if self.attempt and self.attempt['status'] in {'pending', 'exchanging'} and time.monotonic() < self.attempt['deadline']:
                return self.status()
            if self.attempt:
                self.attempt['stop'].set()
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            attempt = {'id': secrets.token_urlsafe(24), 'state': secrets.token_urlsafe(32),
                       'verifier': verifier, 'status': 'pending', 'deadline': time.monotonic() + 300,
                       'stop': threading.Event(), 'language': language}
            owner = self

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # OAuth codes and state must not reach access logs.

                def do_GET(self):
                    parsed = urlsplit(self.path)
                    query = parse_qs(parsed.query)
                    valid = (parsed.path == '/oauth/callback'
                             and secrets.compare_digest(query.get('state', [''])[0], attempt['state']))
                    if not valid:
                        self.send_error(400)
                        return
                    success = owner.complete(attempt, query.get('code', [''])[0], bool(query.get('error')))
                    message = ('接続しました。OLCRに戻ってください。' if success else '接続できませんでした。OLCRから再試行してください。') if language == 'ja' else ('Connected. Return to OLCR.' if success else 'Connection failed. Retry from OLCR.')
                    content = ('<!doctype html><meta charset="utf-8"><title>OLCR</title><p>' + message + '</p>').encode()
                    self.send_response(200 if success else 400)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

            server = ThreadingHTTPServer(('127.0.0.1', 0), Callback)
            server.daemon_threads = True
            server.timeout = 0.5
            attempt['redirect_uri'] = f'http://127.0.0.1:{server.server_port}/oauth/callback'
            attempt['authorization_url'] = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
                'client_id': self.credentials['client_id'], 'redirect_uri': attempt['redirect_uri'],
                'response_type': 'code', 'scope': SCOPES, 'state': attempt['state'],
                'code_challenge': challenge, 'code_challenge_method': 'S256',
                'access_type': 'offline', 'prompt': 'consent'})
            self.attempt = attempt

            def listen():
                try:
                    while not attempt['stop'].is_set() and time.monotonic() < attempt['deadline']:
                        server.handle_request()
                    with self.lock:
                        if self.attempt is attempt and attempt['status'] == 'pending':
                            self._finish('expired')
                finally:
                    server.server_close()
            threading.Thread(target=listen, name='olcr-calendar-oauth', daemon=True).start()
        return self.status()

    def complete(self, attempt, code, denied=False):
        with self.lock:
            if (self.attempt is not attempt or attempt['status'] != 'pending'
                    or time.monotonic() > attempt['deadline'] or not code or denied):
                if self.attempt is attempt:
                    self._finish('failed', 'CALENDAR_AUTH_CANCELLED')
                return False
            # Claim this callback once. Cancel/disconnect still invalidates it while HTTP is in flight.
            attempt['status'] = 'exchanging'
            client = dict(self.credentials)
        try:
            self.network_guard()
            response = self.http.post('https://oauth2.googleapis.com/token', data={
                'client_id': client['client_id'], 'client_secret': client.get('client_secret', ''),
                'code': code, 'redirect_uri': attempt['redirect_uri'],
                'grant_type': 'authorization_code', 'code_verifier': attempt['verifier']})
            token = response.json()
            if response.status_code != 200 or not token.get('refresh_token') or not token.get('access_token'):
                raise ActionError('CALENDAR_AUTH_FAILED')
            with self.lock:
                if self.attempt is not attempt or attempt['status'] != 'exchanging':
                    return False
                self.credentials['refresh_token'] = token['refresh_token']
                self.access_token = token['access_token']
                self.expires_at = time.time() + min(float(token.get('expires_in', 3600)), 86400)
                self.reauth_required = False
                self.generation += 1
                self._finish('connected')
            return True
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            with self.lock:
                if self.attempt is attempt and attempt['status'] == 'exchanging':
                    self._finish('failed', 'CALENDAR_AUTH_FAILED')
            return False

    def _access(self):
        with self.lock:
            if not self.credentials.get('refresh_token') or self.reauth_required:
                raise ActionError('CALENDAR_NOT_CONNECTED')
            if self.access_token and time.time() < self.expires_at - 60:
                return self.access_token
            try:
                response = self.http.post('https://oauth2.googleapis.com/token', data={
                    'client_id': self.credentials['client_id'], 'client_secret': self.credentials.get('client_secret', ''),
                    'refresh_token': self.credentials['refresh_token'], 'grant_type': 'refresh_token'})
                token = response.json()
                if response.status_code != 200 or not token.get('access_token'):
                    if token.get('error') == 'invalid_grant':
                        self.reauth_required = True
                    raise ActionError('CALENDAR_RECONNECT_REQUIRED')
                self.access_token = token['access_token']
                if token.get('refresh_token') and token['refresh_token'] != self.credentials.get('refresh_token'):
                    self.credentials['refresh_token'] = token['refresh_token']
                    self.generation += 1
                self.expires_at = time.time() + min(float(token.get('expires_in', 3600)), 86400)
                return self.access_token
            except ActionError:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise ActionError('CALENDAR_NETWORK_FAILED') from exc

    def request(self, method, path, *, params=None, payload=None, etag=None):
        self.network_guard()
        headers = {'Authorization': 'Bearer ' + self._access()}
        if etag:
            headers['If-Match'] = etag
        try:
            response = self.http.request(method, 'https://www.googleapis.com/calendar/v3/' + path,
                                         params=params, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ActionError('CALENDAR_NETWORK_FAILED') from exc
        if response.status_code == 412:
            raise ActionError('CALENDAR_EVENT_CHANGED')
        if response.status_code == 401:
            self.access_token, self.expires_at = '', 0
            raise ActionError('CALENDAR_RECONNECT_REQUIRED')
        if response.status_code == 403:
            raise ActionError('CALENDAR_PERMISSION_DENIED')
        if response.status_code == 404:
            raise ActionError('CALENDAR_EVENT_NOT_FOUND')
        if response.status_code >= 400:
            raise ActionError('CALENDAR_REQUEST_FAILED')
        try:
            data = response.json() if response.content else {}
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except ValueError as exc:
            raise ActionError('CALENDAR_REQUEST_FAILED') from exc

    @staticmethod
    def event_path(calendar_id, event_id=''):
        if not calendar_id or len(calendar_id) > 512 or (event_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}', event_id)):
            raise ActionError('CALENDAR_EVENT_INVALID')
        path = 'calendars/' + quote(calendar_id, safe='') + '/events'
        return path + ('/' + event_id if event_id else '')

    def calendars(self):
        result = self.request('GET', 'users/me/calendarList', params={'maxResults': 100})
        return [{'id': e['id'], 'title': str(e.get('summary', e['id']))[:200],
                 'writable': e.get('accessRole') in {'owner', 'writer'},
                 'primary': bool(e.get('primary'))} for e in result.get('items', []) if e.get('id')]

    def events(self, calendar_id, start, end, query=''):
        if start >= end or (end - start).total_seconds() > 366 * 86400:
            raise ActionError('CALENDAR_RANGE_INVALID')
        params = {'timeMin': start.isoformat(), 'timeMax': end.isoformat(), 'singleEvents': 'true',
                  'orderBy': 'startTime', 'maxResults': 100,
                  'fields': 'items(id,summary,start,end,etag,htmlLink,recurringEventId),nextPageToken'}
        if query:
            params['q'] = query[:200]
        result = self.request('GET', self.event_path(calendar_id), params=params)
        return {'events': [self.public_event(e) for e in result.get('items', []) if e.get('status') != 'cancelled'],
                'more': bool(result.get('nextPageToken'))}

    @staticmethod
    def public_event(e):
        return {k: e.get(k) for k in ('id', 'summary', 'start', 'end', 'etag', 'htmlLink', 'recurringEventId')}


def video_id(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or '').lower()
    value = ''
    if host in {'youtu.be', 'www.youtu.be'}:
        value = parsed.path.strip('/')
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'www.youtube-nocookie.com'}:
        value = parse_qs(parsed.query).get('v', [''])[0]
        if parsed.path.startswith(('/shorts/', '/embed/', '/live/')):
            value = parsed.path.split('/')[2]
    return value if re.fullmatch(r'[A-Za-z0-9_-]{11}', value) else None


def search_youtube(query: str, key: str = '', client=None, *, order='relevance'):
    direct = next((ident for url in re.findall(r'https?://[^\s<>]+', query)
                   if (ident := video_id(url.rstrip('。、.,)')))), None)
    if direct:
        return [{'id': direct, 'title': 'YouTube', 'url': 'https://www.youtube.com/watch?v=' + direct}]
    query = query.strip()[:300]
    if not query:
        raise ActionError('YOUTUBE_QUERY_REQUIRED')
    try:
        # Legacy key arguments remain accepted, but are not needed or sent.
        # Playback still uses the official player; no media is downloaded.
        from .response_language import response_language
        response = (client or httpx).get('https://www.youtube.com/results',
            params={'search_query': query, 'hl': response_language(), **({'sp':'CAI='} if order=='date' else {})},
            headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': response_language()}, timeout=20)
        if response.status_code != 200 or len(response.content) > 5_000_000:
            raise ActionError('YOUTUBE_SEARCH_FAILED')
        data = youtube_search_results(response.text)
    except ActionError:
        raise
    except (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ActionError('YOUTUBE_SEARCH_FAILED') from exc
    found, seen = [], set()
    for item in data:
        ident = item.get('id')
        if ident and re.fullmatch(r'[A-Za-z0-9_-]{11}', ident) and ident not in seen:
            seen.add(ident)
            found.append({**item, 'url': 'https://www.youtube.com/watch?v=' + ident})
    if not found:
        raise ActionError('YOUTUBE_NO_RESULTS')
    return found


def youtube_search_results(document: str):
    """Parse a bounded public search snapshot; never execute page JavaScript."""
    decoder = json.JSONDecoder()
    packet = None
    for match in re.finditer(r'(?:var\s+ytInitialData|window\["ytInitialData"\]|ytInitialData)\s*=\s*', document):
        try:
            packet, _ = decoder.raw_decode(document[match.end():].lstrip())
            break
        except ValueError:
            continue
    if not isinstance(packet, dict):
        raise ActionError('YOUTUBE_SEARCH_FAILED')
    pending, data, visited = [packet], [], 0
    def label(value):
        if not isinstance(value, dict): return ''
        return str(value.get('simpleText') or ''.join(str(run.get('text', '')) for run in value.get('runs', []) if isinstance(run, dict)))[:300]
    while pending and visited < 10_000 and len(data) < 20:
        value = pending.pop(); visited += 1
        if isinstance(value, list):
            pending.extend(reversed(value))
        elif isinstance(value, dict):
            video = value.get('videoRenderer')
            if isinstance(video, dict):
                data.append({'id': video.get('videoId'), 'title': label(video.get('title')) or 'YouTube',
                             'channel': label(video.get('ownerText') or video.get('shortBylineText'))})
            else:
                pending.extend(reversed(list(value.values())))
    return data[:5]


class PlayerTickets:
    def __init__(self):
        self.lock = threading.Lock()
        self.tickets = {}

    def issue(self, ident):
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', ident):
            raise ActionError('YOUTUBE_VIDEO_INVALID')
        with self.lock:
            self.tickets = {k: v for k, v in self.tickets.items() if v[1] > time.monotonic()}
            if len(self.tickets) >= 128:
                self.tickets.pop(next(iter(self.tickets)))
            ticket = secrets.token_urlsafe(32)
            self.tickets[ticket] = (ident, time.monotonic() + 3600)
            return ticket

    def render(self, ticket):
        with self.lock:
            value = self.tickets.get(ticket)
        if not value or value[1] < time.monotonic():
            raise ActionError('YOUTUBE_PLAYER_EXPIRED')
        ident = value[0]
        nonce = secrets.token_urlsafe(18)
        # This loopback HTTP document gives YouTube an HTTP origin and Referer,
        # including when OLCR itself uses the tauri:// custom protocol.
        script = '''const ticket=TICKET;let player;
const send=(state,error)=>parent.postMessage({olcrPlayer:ticket,state,error},'*');
// Keep fullscreen delegation on the actual cross-origin iframe. Creating the
// element ourselves avoids relying on the IFrame API's replacement attributes.
const embed=document.createElement('iframe');embed.id='player';
embed.src='https://www.youtube-nocookie.com/embed/'+VIDEO+'?enablejsapi=1&playsinline=1&controls=1&fs=1&origin='+encodeURIComponent(location.origin);
embed.allow='autoplay; encrypted-media; picture-in-picture; fullscreen';
embed.setAttribute('allowfullscreen','');embed.setAttribute('webkitallowfullscreen','');
embed.setAttribute('title','YouTube');embed.style.border='0';
document.getElementById('player').replaceWith(embed);
window.onYouTubeIframeAPIReady=()=>{player=new YT.Player('player',{host:'https://www.youtube-nocookie.com',width:'100%',height:'100%',videoId:VIDEO,playerVars:{playsinline:1,controls:1,fs:1,origin:location.origin},events:{onReady:()=>send('ready'),onStateChange:e=>send(({'-1':'stopped',0:'ended',1:'playing',2:'paused',3:'buffering',5:'cued'})[e.data]||'unstarted'),onError:e=>send('error',e.data),onAutoplayBlocked:()=>send('blocked')}})};
addEventListener('message',e=>{if(e.source!==parent||e.data?.ticket!==ticket||!player)return;const cmd=e.data.command;if(cmd==='play')player.playVideo();if(cmd==='pause')player.pauseVideo();if(cmd==='stop')player.stopVideo()});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&player?.pauseVideo)player.pauseVideo()});
'''.replace('TICKET', json.dumps(ticket)).replace('VIDEO', json.dumps(ident))
        page = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="strict-origin-when-cross-origin"><title>YouTube · OLCR</title><style nonce="{nonce}">html,body,#player{{width:100%;height:100%;margin:0;background:#000}}</style></head><body><div id="player"></div><script nonce="{nonce}">{script}</script><script src="https://www.youtube.com/iframe_api"></script></body></html>'''
        policy = (f"default-src 'none'; script-src 'nonce-{nonce}' https://www.youtube.com https://s.ytimg.com; "
                  f"style-src 'nonce-{nonce}'; frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
                  "connect-src https://www.youtube.com https://www.youtube-nocookie.com; img-src https: data:; "
                  "frame-ancestors tauri://localhost http://tauri.localhost http://127.0.0.1:* http://localhost:5173")
        return page, policy
