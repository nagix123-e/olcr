"""Browser sign-in via GitHub CLI; credentials never cross the OLCR API."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from .git_workspace import GitError


def github_cli():
    return shutil.which('gh') or next((str(p) for p in
        (Path('/opt/homebrew/bin/gh'), Path('/usr/local/bin/gh')) if p.is_file()), None)


class GitHubConnection:
    def __init__(self, get_db):
        self.get_db = get_db
        self.lock = threading.RLock()
        self.attempts = {}
        self.cached = None
        self.cached_at = 0

    def preference(self, key, value=None):
        with self.get_db().connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS github_preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            if value is not None:
                db.execute('INSERT OR REPLACE INTO github_preferences VALUES(?,?)', (key, json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM github_preferences WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def automatic(self):
        return self.preference('execution_mode') == 'autonomous'

    def linked(self):
        return bool(self.preference('account'))

    def identity(self):
        login = self.preference('account')
        return ({'name': login, 'email': self.preference('commit_email') or login + '@users.noreply.github.com'}
                if login else None)

    def env(self):
        env = {key: value for key, value in os.environ.items() if key in
               {'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'XDG_CONFIG_HOME', 'GH_CONFIG_DIR'}}
        env.update(GH_HOST='github.com', GH_PROMPT_DISABLED='1', GH_BROWSER='/usr/bin/true',
                   BROWSER='/usr/bin/true', LC_ALL='C', NO_COLOR='1')
        return env

    def run(self, args):
        binary = github_cli()
        if not binary:
            raise GitError('GITHUB_CLI_NOT_INSTALLED')
        try:
            result = subprocess.run([binary, *args], env=self.env(), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired):
            raise GitError('GITHUB_CONNECTION_UNAVAILABLE') from None
        if result.returncode:
            raise GitError('GITHUB_SIGN_IN_REQUIRED')
        return result.stdout

    def account(self, refresh=False):
        if not github_cli():
            return {'installed': False, 'connected': False, 'execution_mode': 'review'}
        with self.lock:
            if refresh or self.cached is None or time.monotonic()-self.cached_at > 60:
                try:
                    user = json.loads(self.run(['api', 'user', '--hostname', 'github.com', '--jq',
                                               '{login: .login, id: .id}']))
                    if not isinstance(user.get('login'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', user['login']):
                        raise ValueError()
                    # Persist only public identity; gh owns all credential storage.
                    self.cached = {'installed': True, 'connected': True, 'account': user['login'],
                                   'url': 'https://github.com/' + user['login']}
                    self.preference('account', user['login'])
                    prefix = str(user['id']) + '+' if isinstance(user.get('id'), int) else ''
                    self.preference('commit_email', prefix + user['login'] + '@users.noreply.github.com')
                except (GitError, ValueError, TypeError):
                    self.cached = {'installed': True, 'connected': False}
                    self.preference('account', '')
                self.cached_at = time.monotonic()
            return {**self.cached, 'execution_mode': self.preference('execution_mode') or 'review'}

    def policy(self, mode):
        if mode not in {'review', 'autonomous'}:
            raise GitError('GITHUB_EXECUTION_MODE_INVALID')
        self.preference('execution_mode', mode)
        return {'execution_mode': mode}

    def start(self):
        binary = github_cli()
        if not binary:
            raise GitError('GITHUB_CLI_NOT_INSTALLED')
        with self.lock:
            for value in self.attempts.values():
                if value['status'] == 'pending':
                    return self.public(value)
            identity = str(uuid.uuid4())
            value = {'id': identity, 'status': 'pending', 'code': '',
                     'url': 'https://github.com/login/device', 'deadline': time.monotonic()+900}
            self.attempts = {identity: value}
            try:
                process = subprocess.Popen([binary, 'auth', 'login', '--hostname', 'github.com',
                                            '--git-protocol', 'https', '--web'], env=self.env(),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, bufsize=1)
            except OSError:
                value['status'] = 'failed'
                raise GitError('GITHUB_CONNECTION_UNAVAILABLE') from None
            value['process'] = process
            threading.Thread(target=self.complete, args=(value,), daemon=True).start()
            timer = threading.Timer(900, lambda: self.expire(identity))
            timer.daemon = True
            timer.start()
            return self.public(value)

    @staticmethod
    def public(value):
        return {key: value[key] for key in ('id', 'status', 'code', 'url') if key in value}

    def complete(self, value):
        try:
            for line in value['process'].stdout:
                code = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', line)
                if code:
                    with self.lock:
                        value['code'] = code.group(1)
            if value['process'].wait() != 0:
                raise GitError('GITHUB_SIGN_IN_FAILED')
            with self.lock:
                if value['status'] != 'pending':
                    return
            self.run(['auth', 'setup-git', '--hostname', 'github.com'])
            connected = self.account(refresh=True)
            if not connected['connected']:
                raise GitError('GITHUB_SIGN_IN_FAILED')
            if self.preference('execution_mode') is None:
                self.policy('autonomous')
            with self.lock:
                value['status'] = 'connected'
        except (GitError, OSError, ValueError):
            with self.lock:
                if value['status'] == 'pending':
                    value['status'] = 'failed'
        finally:
            value['process'].stdout.close()

    def expire(self, identity, cancel=False):
        with self.lock:
            value = self.attempts.get(identity)
            if value and value['status'] == 'pending' and (cancel or time.monotonic() >= value['deadline']):
                value['status'] = 'cancelled' if cancel else 'expired'
                if value['process'].poll() is None:
                    value['process'].terminate()
        return {'status': 'cancelled' if cancel else 'expired'}

    def poll(self, identity):
        self.expire(identity)
        with self.lock:
            value = self.attempts.get(identity)
            if not value:
                raise GitError('GITHUB_SIGN_IN_SESSION_NOT_FOUND')
            return self.public(value)

    def transport(self, url):
        if self.linked() and not url.startswith('https://'):
            repo = re.split(r'github\.com[:/]', url, maxsplit=1)[1]
            return 'https://github.com/' + repo
        return url
