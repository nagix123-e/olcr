"""Run the existing OLCR backend as a loopback-only background-service target."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import secrets
import socket
import sys


def main():
    from olcr_api.config import default_db_path
    parser = argparse.ArgumentParser(description='PC-owned OLCR runtime for a service supervisor; no phone listener')
    parser.add_argument('--db', default=default_db_path())
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    workspace = Path(args.workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        parser.error('workspace must be a directory')
    db = Path(args.db).expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    with open(db.parent / 'mobile-launch.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('a mobile host already owns this profile')
        descriptor = db.parent / 'mobile-host.json'
        if descriptor.exists():
            import json
            try:
                pid = json.loads(descriptor.read_text())['pid']
                os.kill(pid, 0)
            except (OSError, ValueError, KeyError):
                pass
            else:
                parser.error('OLCR already owns this profile; attach olcr-acp to that host')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = os.environ | {'OLCR_DB_PATH': str(db), 'OLCR_ALLOWED_ROOTS': str(workspace),
            'OLCR_BACKEND_PORT': str(port), 'OLCR_GUI_SESSION_TOKEN': secrets.token_urlsafe(32)}
        # A supervisor (launchd/Happier setup) owns this command's lifetime.
        # Replace the launcher so the supervisor's signals reach the backend
        # itself instead of leaving an orphaned uvicorn child behind.
        os.execvpe(sys.executable, [sys.executable, '-m', 'uvicorn', 'olcr_api.app:app',
                                  '--host', '127.0.0.1', '--port', str(port)], env)


if __name__ == '__main__':
    main()
