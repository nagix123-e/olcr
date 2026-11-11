"""Acquire optional local engines from pinned upstream distributions on first use.

Only small recipes ship with OLCR. Installs use private staging directories,
hash verification, process locks and atomic rename; diagnostics never install.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import urllib.request
import zipfile

RECIPES = Path(__file__).with_name('runtime_recipes')
NAMES = ('laya', 'agents', 'node-tools', 'browser', 'serena')
NODE = {
    'agents': ('24.18.0', 'e1a97e14c99c803e96c7339403282ea05a499c32f8d83defe9ef5ec66f979ed1'),
    'node-tools': ('20.20.2', '466e05f3477c20dfb723054dfebffe55bc74660ee77f612166fca121dacb65b6'),
}
BROWSER_URL = 'https://cdn.playwright.dev/builds/cft/153.0.8010.12/mac-arm64/chrome-headless-shell-mac-arm64.zip'
BROWSER_SHA = '89d80a6d26ccd0ccfd51e22d9e1297283862af2b0cd91dce07459b35ca0059f2'
BROWSER_PATH = 'chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell'
SQLITE_URL = 'https://github.com/WiseLibs/better-sqlite3/releases/download/v12.11.1/better-sqlite3-v12.11.1-node-v137-darwin-arm64.tar.gz'
SQLITE_SHA = '6eaefc8a9c088fea873365f7db2c0f89be6ea021ede62f6a71832f5130826a93'

class SetupError(ValueError):
    pass

def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): result.update(chunk)
    return result.hexdigest()

def extract_tar(source, destination):
    with tarfile.open(source) as archive:
        if sum(entry.size for entry in archive.getmembers()) > 2_000_000_000:
            raise SetupError('追加環境の展開サイズが上限を超えました。')
        archive.extractall(destination, filter='data')

def extract_zip(source, destination):
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if sum(entry.file_size for entry in entries) > 1_000_000_000:
            raise SetupError('ブラウザの展開サイズが上限を超えました。')
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                raise SetupError('追加環境のアーカイブが不正です。')
        for entry in entries:
            archive.extract(entry, destination)
            if not entry.is_dir():
                (destination / entry.filename).chmod(0o700 if (entry.external_attr >> 16) & 0o111 else 0o600)

class OptionalRuntimes:
    def __init__(self, root=None, recipes=RECIPES):
        self._root_override = Path(root) if root is not None else None
        self.recipes = Path(recipes)
        self.lock = threading.RLock()
        self.states = {}
        self.workers = {}

    @property
    def root(self):
        if self._root_override is not None: return self._root_override
        support = Path(os.environ.get('OLCR_APP_SUPPORT', Path.home() / 'Library/Application Support/OLCR'))
        if os.environ.get('OLCR_DB_PATH') and not os.environ.get('OLCR_APP_SUPPORT'):
            support = Path(os.environ['OLCR_DB_PATH']).parent
        return Path(os.environ.get('OLCR_COMPONENTS_ROOT', support / 'components'))

    def identity(self, name):
        if name not in NAMES: raise SetupError('追加環境の名前が不正です。')
        digest = hashlib.sha256(('olcr-optional-v1:' + name + BROWSER_SHA + SQLITE_SHA + str(NODE)).encode())
        source = self.recipes / ('laya.json' if name == 'laya' else name)
        files = [source] if source.is_file() else sorted(p for p in source.rglob('*') if p.is_file())
        for path in files:
            digest.update(path.name.encode()); digest.update(path.read_bytes())
        return digest.hexdigest()[:20]

    def _cache_digests(self, name):
        values = set()
        if name in NODE:
            values.add(NODE[name][1])
        if name == 'agents': values.add(SQLITE_SHA)
        if name == 'browser': values.add(BROWSER_SHA)
        try:
            if name == 'laya':
                values.update(item['sha256'] for item in json.loads((self.recipes / 'laya.json').read_text())['files'])
            elif name == 'serena':
                metadata = json.loads((self.recipes / 'serena' / 'artifacts.json').read_text())
                values.add(metadata['python']['sha256'])
                values.update(item['sha256'] for item in metadata.get('bootstrap', []))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return values

    def _remove_download_cache(self, name):
        cache_root = self.root / 'downloads'
        if cache_root.is_symlink() or not cache_root.is_dir(): return
        retained = set().union(*(self._cache_digests(other) for other in NAMES if other != name))
        for digest in self._cache_digests(name) - retained:
            cached = cache_root / digest
            if cached.is_file(): cached.unlink(missing_ok=True)
            (cache_root / f'{digest}.{name}.part').unlink(missing_ok=True)
        package_npm_cache = self.root / 'npm-cache' / name
        if package_npm_cache.is_dir() and not package_npm_cache.is_symlink():
            shutil.rmtree(package_npm_cache)
        if name in {'agents', 'node-tools'}:
            shared_users = {'agents', 'node-tools'} - {name}
            other_active = any(self.workers.get(other) and self.workers[other].is_alive() for other in shared_users)
            other_installed = any(self.installed(other) for other in shared_users)
            legacy_npm_cache = self.root / 'npm-cache'
            if not other_active and not other_installed and legacy_npm_cache.is_dir() and not legacy_npm_cache.is_symlink():
                shutil.rmtree(legacy_npm_cache)

    def path(self, name):
        return self.root / name / self.identity(name)

    def has_downloads(self, name):
        package_root = self.path(name).parent
        if package_root.exists(): return True
        cache_root = self.root / 'downloads'
        retained = set().union(*(self._cache_digests(other) for other in NAMES if other != name))
        if cache_root.is_dir() and not cache_root.is_symlink() and any(
                (cache_root / digest).is_file() for digest in self._cache_digests(name) - retained):
            return True
        package_cache = self.root / 'npm-cache' / name
        return package_cache.is_dir() and not package_cache.is_symlink()

    def installed(self, name):
        root = self.path(name)
        try:
            marker = json.loads((root / '.olcr-component.json').read_text())
            return marker.get('identity') == self.identity(name) and self.valid(name, root)
        except (OSError, ValueError): return False

    def valid(self, name, root):
        if name == 'laya': return all((root / 'multilingual' / item['path']).is_file() and (root / 'multilingual' / item['path']).stat().st_size > 0 for item in json.loads((self.recipes / 'laya.json').read_text())['files'])
        if name == 'browser': return (root / BROWSER_PATH).is_file() and os.access(root / BROWSER_PATH, os.X_OK)
        if name == 'serena':
            return (os.access(root / 'python/bin/python3', os.X_OK) and (root / 'runtime-manifest.json').is_file()
                    and (root / 'python/lib/python3.11/site-packages/serena/cli.py').is_file())
        if not os.access(root / 'node/bin/node', os.X_OK): return False
        if name == 'agents':
            return all((root / path).is_file() for path in ['node/bin/node','runner.mjs','node_modules/@librechat/agents/package.json','node_modules/better-sqlite3/build/Release/better_sqlite3.node'])
        return all((root / path).is_file() for path in ['node/bin/node','node_modules/@playwright/mcp/cli.js','node_modules/shadcn/dist/index.js','servers/animejs-reference/server.js','servers/devdocs/server.mjs'])

    def update(self, name, state, message='', **extra):
        with self.lock: self.states[name] = {'name': name, 'state': state, 'message': message, **extra}

    def status(self, name):
        with self.lock: state = dict(self.states.get(name, {'name': name, 'state': 'not_installed', 'message': ''}))
        state['removable'] = self.has_downloads(name)
        if state['state'] not in {'downloading','installing','removing','error'} and self.installed(name):
            state.update(state='ready', message='')
        elif state['state'] == 'ready':
            state.update(state='not_installed', message='')
        return state

    def prepare(self, name):
        self.identity(name)
        with self.lock:
            if not self.workers.get(name) or not self.workers[name].is_alive():
                self.update(name, 'installing', '追加環境を準備中…')
                def work():
                    try: self.ensure(name)
                    except Exception: pass  # ensure records a user-facing error
                self.workers[name] = threading.Thread(target=work, name='olcr-setup-' + name, daemon=True)
                self.workers[name].start()
        return self.status(name)

    def remove(self, name):
        self.identity(name)
        with self.lock:
            worker = self.workers.get(name)
            if worker and worker.is_alive():
                raise SetupError('追加環境の準備または削除が進行中です。完了後にもう一度お試しください。')
            package_root = self.path(name).parent
            if not self.has_downloads(name):
                self.update(name, 'not_installed')
                return self.status(name)
            self.update(name, 'removing', '追加環境を削除中…')

            def work():
                try:
                    # Keep this lock outside package_root so it remains stable
                    # while the whole versioned component directory is removed.
                    lock_path = self.root / ('.' + name + '.install.lock')
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    with lock_path.open('a') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        if package_root.exists():
                            shutil.rmtree(package_root)
                        self._remove_download_cache(name)
                    self.update(name, 'not_installed')
                except Exception:
                    self.update(name, 'error', '追加環境を削除できませんでした。')

            self.workers[name] = threading.Thread(target=work, name='olcr-remove-' + name, daemon=True)
            self.workers[name].start()
        return self.status(name)

    def download(self, name, url, digest, target, maximum=1_000_000_000):
        target.parent.mkdir(parents=True, exist_ok=True)
        cache = self.root / 'downloads' / digest
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.is_file() and file_hash(cache) == digest:
            shutil.copyfile(cache, target); return
        partial = cache.with_name(cache.name + '.' + name + '.part')
        try:
            request = urllib.request.Request(url, headers={'User-Agent':'OLCR-Component-Setup/1'})
            with urllib.request.urlopen(request, timeout=60) as response, partial.open('wb') as stream:
                total = 0; hasher = hashlib.sha256()
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > maximum: raise SetupError('追加環境の取得サイズが上限を超えました。')
                    stream.write(chunk); hasher.update(chunk)
                    self.update(name, 'downloading', f'ダウンロード中… {total // 1_000_000} MB', downloaded_bytes=total)
            if hasher.hexdigest() != digest: raise SetupError('追加環境の検証に失敗しました。再試行してください。')
            partial.replace(cache); shutil.copyfile(cache, target)
        finally: partial.unlink(missing_ok=True)

    def ensure(self, name):
        target = self.path(name)
        if self.installed(name):
            self.update(name, 'ready'); return target
        # This release's native recipes target macOS Apple Silicon.
        if name != 'laya' and (platform.system() != 'Darwin' or platform.machine() != 'arm64'):
            self.update(name, 'error', 'この追加環境はmacOSのApple Silicon版に対応しています。')
            raise SetupError(self.status(name)['message'])
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.parent.parent / ('.' + name + '.install.lock')
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.installed(name):
                self.update(name, 'ready'); return target
            try:
                if shutil.disk_usage(target.parent).free < 2_000_000_000:
                    raise SetupError('追加環境の準備には2GB以上の空き容量が必要です。')
                with tempfile.TemporaryDirectory(prefix='.setup-', dir=target.parent) as temporary:
                    stage = Path(temporary)
                    self.update(name, 'installing', '追加環境を準備中…')
                    getattr(self, '_install_' + name.replace('-', '_'))(stage)
                    if not self.valid(name, stage): raise SetupError('追加環境のファイルが不足しています。')
                    (stage / '.olcr-component.json').write_text(json.dumps({'identity': self.identity(name)}))
                    if target.exists(): shutil.rmtree(target)
                    stage.rename(target)
                self.update(name, 'ready')
                return target
            except Exception as exc:
                message = str(exc) if isinstance(exc, SetupError) else '追加環境を取得できませんでした。ネットワーク接続を確認して再試行してください。'
                self.update(name, 'error', message)
                raise SetupError(message) from exc

    def _install_laya(self, stage):
        metadata = json.loads((self.recipes / 'laya.json').read_text())
        for item in metadata['files']:
            self.download('laya', f"https://huggingface.co/{metadata['repo']}/resolve/{metadata['revision']}/{item['path']}",
                          item['sha256'], stage / 'multilingual' / item['path'], maximum=item['bytes'] + 1024)
        (stage / 'olcr-laya-cache.json').write_text(json.dumps({'model_revision':metadata['revision'],'multilingual':True}))

    def _node_packages(self, name, stage):
        version, digest = NODE[name]
        archive = stage / 'node.tar.gz'
        self.download(name, f'https://nodejs.org/dist/v{version}/node-v{version}-darwin-arm64.tar.gz', digest, archive)
        extract_tar(archive, stage); archive.unlink()
        (stage / f'node-v{version}-darwin-arm64').rename(stage / 'node')
        source = self.recipes / name
        for filename in ('package.json','package-lock.json'): shutil.copy2(source / filename, stage / filename)
        # npm verifies every tarball against the shipped lock. No install scripts,
        # global npm settings, host Node, compiler or developer tools are used.
        environment = {key: value for key,value in os.environ.items() if key in {'HOME','TMPDIR','LANG','LC_ALL','TZ'}}
        (stage / '.npm-user').touch(); (stage / '.npm-global').touch()
        environment.update(PATH=str(stage / 'node/bin') + ':/usr/bin:/bin',
                           npm_config_userconfig=str(stage / '.npm-user'), npm_config_globalconfig=str(stage / '.npm-global'),
                           npm_config_cache=str(self.root / 'npm-cache' / name), npm_config_registry='https://registry.npmjs.org',
                           PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1')
        self.update(name, 'installing', '必要なパッケージを取得中…')
        result = subprocess.run([str(stage / 'node/bin/node'), str(stage / 'node/lib/node_modules/npm/bin/npm-cli.js'),
                                 'ci','--omit=dev','--ignore-scripts','--no-audit','--no-fund'],
                                cwd=stage, env=environment, capture_output=True, timeout=900)
        if result.returncode: raise SetupError('追加パッケージを取得できませんでした。再試行してください。')

    def _install_agents(self, stage):
        self._node_packages('agents', stage)
        # The npm node package normally runs an installer. Bind it to the
        # already verified private Node instead, without executing that script.
        node_bin = stage / 'node_modules/node/bin'; node_bin.mkdir(parents=True, exist_ok=True)
        (node_bin / 'node').symlink_to('../../../node/bin/node')
        archive = stage / 'sqlite.tar.gz'
        self.download('agents', SQLITE_URL, SQLITE_SHA, archive)
        extract_tar(archive, stage / 'node_modules/better-sqlite3'); archive.unlink()
        shutil.copy2(self.recipes / 'agents/runner.mjs', stage / 'runner.mjs')
        (stage / 'runtime-manifest.json').write_text(json.dumps({'engine_version':'4.0.4','node':{'version':'v24.18.0'},'entrypoint':'runner.mjs'}))

    def _install_node_tools(self, stage):
        self._node_packages('node-tools', stage)
        shutil.copytree(self.recipes / 'node-tools/animejs-reference', stage / 'servers/animejs-reference')
        shutil.copytree(self.recipes / 'node-tools/devdocs', stage / 'servers/devdocs')
        (stage / 'runtime-manifest.json').write_text(json.dumps({'entrypoints':{
            'devdocs':['servers/devdocs/server.mjs'],
            'shadcn':['node_modules/shadcn/dist/index.js','mcp'],
            'animejs':['servers/animejs-reference/server.js'],
            'playwright':['node_modules/@playwright/mcp/cli.js','--headless','--isolated']}}))

    def _install_serena(self, stage):
        recipe = self.recipes / 'serena'
        metadata = json.loads((recipe / 'artifacts.json').read_text())
        archive = stage / 'python.tar.gz'
        self.download('serena', metadata['python']['source'], metadata['python']['sha256'], archive)
        extract_tar(archive, stage); archive.unlink()
        python = stage / 'python/bin/python3'
        environment = {key:value for key,value in os.environ.items() if key in {'HOME','TMPDIR','LANG','LC_ALL','TZ'}}
        environment.update(PATH=str(python.parent) + ':/usr/bin:/bin', PIP_CONFIG_FILE=os.devnull)
        bootstrap = stage / 'bootstrap'; bootstrap.mkdir()
        wheels = []
        for item in metadata['bootstrap']:
            target = bootstrap / item['filename']
            self.download('serena', item['url'], item['sha256'], target)
            wheels.append(str(target))
        commands = [
            [str(python), '-m', 'pip', 'install', '--no-index', '--no-deps', *wheels],
            [str(python), '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
             '--require-hashes', '--no-build-isolation', '-r', str(recipe / 'requirements-lock.txt')],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=stage, env=environment, capture_output=True, timeout=900)
            if result.returncode:
                raise SetupError('Serenaの追加環境を取得できませんでした。ネットワーク接続を確認して再試行してください。')
        shutil.rmtree(bootstrap)
        (stage / 'runtime-manifest.json').write_text(json.dumps({
            'serena_version':metadata['serena']['version'], 'entrypoint':metadata['serena']['entrypoint'],
            'python':metadata['python'], 'lock_sha256':file_hash(recipe / 'requirements-lock.txt')}))

    def _install_browser(self, stage):
        archive = stage / 'browser.zip'
        self.download('browser', BROWSER_URL, BROWSER_SHA, archive)
        destination = stage / 'chromium_headless_shell-1243'; destination.mkdir()
        extract_zip(archive, destination); archive.unlink()

runtime = OptionalRuntimes()
