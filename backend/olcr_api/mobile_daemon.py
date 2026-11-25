"""Provision the dedicated OLCR identity for the pinned Happier daemon."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from nacl.secret import SecretBox
from .mobile_pairing import PairingError

HAPPIER_VERSION = '0.2.20'


def cli_path():
    override = os.environ.get('OLCR_HAPPIER_CLI')
    if override:
        return shutil.which(override)
    source_cli = Path(__file__).resolve().parents[2]/'.tooling'/'happier'/'node_modules'/'.bin'/'happier'
    paths = (source_cli, Path.home()/'.happier'/'bin'/'happier', Path('/opt/homebrew/bin/happier'), Path('/usr/local/bin/happier'))
    return shutil.which('happier') or next((str(path) for path in paths if path.is_file() and os.access(path, os.X_OK)), None)


def private_file(path, value, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            os.fchmod(file.fileno(), mode)
            file.write(value)
        os.replace(filename, path)
    finally:
        Path(filename).unlink(missing_ok=True)


def account_settings_box(seed):
    # Happier accountScopedCipher.ts / keyDerivation.ts, legacy recovery identity.
    root = hmac.digest(b'Happy EnCoder Master Seed', seed, 'sha512')
    content = hmac.digest(root[32:], b'\0content', 'sha512')[:32]
    machine_key = hashlib.sha512(content).digest()[:32]
    return SecretBox(hmac.digest(machine_key, b'happier:account_scoped:account_settings:v1', 'sha512')[:32])


async def register_adapter(service, entry):
    box = account_settings_box(service.seed)
    for _ in range(3):
        current = await service._request('GET', '/v2/account/settings')
        content = current['content']
        settings = {}
        if content is not None:
            if content.get('t') != 'encrypted':
                raise PairingError('HAPPIER_ENCRYPTION_REQUIRED')
            try:
                data = base64.b64decode(content['c'], validate=True)
                settings = json.loads(box.decrypt(data[2:]) if data.startswith(b'\xa1\x01')
                                      else SecretBox(service.seed).decrypt(data))
                if not isinstance(settings, dict):
                    raise ValueError('invalid settings')
            except Exception:
                raise PairingError('HAPPIER_SETTINGS_DECRYPT_FAILED') from None
        catalog = settings.get('acpCatalogSettingsV1', {'v': 2, 'backends': []})
        if not isinstance(catalog, dict) or catalog.get('v') != 2 or not isinstance(catalog.get('backends'), list):
            raise PairingError('HAPPIER_CATALOG_UNSUPPORTED')
        backends = catalog['backends']
        backend_id = entry.get('id')
        if not isinstance(backend_id, str) or not backend_id:
            raise PairingError('HAPPIER_CATALOG_BACKEND_INVALID')
        old = next((item for item in backends if item.get('id') == backend_id), None)
        if old and all(old.get(key) == value for key, value in entry.items()):
            return
        now = int(time.time()*1000)
        catalog['backends'] = [item for item in backends if item.get('id') != backend_id] + [
            entry | {'createdAt': old.get('createdAt', now) if old else now, 'updatedAt': now}]
        settings['acpCatalogSettingsV1'] = catalog
        ciphertext = base64.b64encode(b'\xa1\x01' + bytes(box.encrypt(json.dumps(settings).encode()))).decode()
        result = await service._request('POST', '/v2/account/settings', json={
            'content': {'t': 'encrypted', 'c': ciphertext}, 'expectedVersion': current['version']})
        if result.get('success') is True:
            return
        if result.get('error') != 'version-mismatch':
            break
    raise PairingError('HAPPIER_CATALOG_UPDATE_FAILED')


async def prepare_daemon(service, profile, host_file, workspace):
    cli = cli_path()
    if not cli:
        raise PairingError('HAPPIER_CLI_NOT_INSTALLED')
    home = Path(profile)/'happier-mobile'/hashlib.sha256(service.relay.encode()).hexdigest()[:16]
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    env = {key: os.environ[key] for key in ('HOME','PATH','TMPDIR','LANG','LC_ALL','USER') if key in os.environ}
    env['PATH'] = os.pathsep.join((env.get('PATH', os.defpath), '/opt/homebrew/bin', '/usr/local/bin'))
    env.update(HAPPIER_HOME_DIR=str(home), HAPPIER_ACTIVE_SERVER_ID='olcr',
               HAPPIER_SERVER_URL=service.relay, HAPPIER_ENCRYPTION_REQUIREMENT='require_e2ee')

    async def run(*args):
        try:
            proc = await asyncio.create_subprocess_exec(cli,*args,env=env,cwd=workspace,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            try:
                output,_ = await asyncio.wait_for(proc.communicate(), 25)
            except (TimeoutError, asyncio.CancelledError):
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            if proc.returncode:
                raise PairingError('HAPPIER_DAEMON_START_FAILED')
            return output.decode(errors='replace')
        except (OSError,TimeoutError):
            raise PairingError('HAPPIER_CLI_UNAVAILABLE') from None

    if not re.search(r'(?<![\d.])'+re.escape(HAPPIER_VERSION)+r'(?![\d.])', await run('--version')):
        raise PairingError('HAPPIER_CLI_VERSION_MISMATCH')

    # CLI expects its own credential file; never export the OLCR GUI token.
    private_file(home/'servers'/'olcr'/'access.key', json.dumps({
        'token':service.token,'secret':base64.b64encode(service.seed).decode()}))
    # Quote the local Python/package paths without exposing secrets to the ACP child.
    import shlex
    wrapper = home/'olcr-acp'
    backend = str(Path(__file__).resolve().parent.parent)
    private_file(wrapper, '#!/bin/sh\nexport PYTHONPATH='+shlex.quote(backend)+'\nexec '+shlex.join(
        [sys.executable,'-m','olcr_cli.acp','--host-file',str(host_file)])+' "$@"\n', 0o700)
    await register_adapter(service, {
        'id':'olcr','name':'olcr','title':'OLCR','command':str(wrapper),'args':[],
        'capabilities':{'supportsLoadSession':True},
    })
    await run('daemon','start')
