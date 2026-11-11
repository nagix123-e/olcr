"""Own the complete ConvertX environment. No system Docker/Brew commands are used.

Explicit enable installs checksum-pinned Lima and boots a private, mount-free VM using
Apple Virtualization.framework. The pinned upstream OCI image contains all of
ConvertX's converters. Downloads and the VM persist between OLCR launches.
"""
from __future__ import annotations

import atexit
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request

import httpx

LIMA_VERSION = '2.0.3'
LIMA_HASHES = {
    'arm64': '22aee997df59e4fd448041b2d1214e48bd8eaf705d2d48a4307d65c1b179dc97',
    'x86_64': '0806bcb83a08411e9d878b43b2c4203f1556fe14f9f8ba1e5f0d5d9a3c2c0bd8',
}
UBUNTU_DATE = '20261017'
UBUNTU_HASHES = {
    'arm64': '50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e',
    'x86_64': '04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94',
}
IMAGE = 'ghcr.io/c4illin/convertx@sha256:8590a21a16adb8514a8be55b74ad0112bf58ba772328a53f7b40528fcf8fa013'


class RuntimeSetupError(RuntimeError):
    pass


def verified_download(url: str, destination: Path, digest: str, progress=lambda text: None,
                     cancelled: threading.Event | None = None):
    """Never execute an incomplete or unverified download. Retry starts cleanly."""
    if destination.is_file() and file_hash(destination) == digest:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.part')
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'OLCR-Conversion-Runtime/1'})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open('wb') as output:
            total = int(response.headers.get('Content-Length', 0))
            read = 0
            while chunk := response.read(1024 * 1024):
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeSetupError('変換環境のダウンロードを中止しました。')
                read += len(chunk)
                if read > 3_000_000_000:
                    raise RuntimeSetupError('変換環境の取得サイズが上限を超えました。')
                output.write(chunk)
                progress(f'変換環境を取得中… {read // 1_000_000} MB' +
                         (f' / {total // 1_000_000} MB' if total else ''))
        if file_hash(temporary) != digest:
            raise RuntimeSetupError('変換環境の検証に失敗しました。再試行してください。')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def vm_config(image: Path, arch: str, digest: str, port: int) -> str:
    # JSON is a YAML subset. No inherited templates or host home-directory mounts.
    return json.dumps({
        'vmType': 'vz', 'arch': 'aarch64' if arch == 'arm64' else 'x86_64',
        'cpus': 2, 'memory': '3GiB', 'disk': '24GiB', 'mounts': [],
        # Host macOS UID 501 is a system UID on Ubuntu; use a normal Linux UID
        # so logind starts the rootless containerd user session reliably.
        'user': {'name': 'olcr', 'uid': 1000, 'home': '/home/olcr', 'shell': '/bin/bash'},
        'images': [{'location': str(image), 'arch': 'aarch64' if arch == 'arm64' else 'x86_64',
                    'digest': 'sha256:' + digest}],
        'containerd': {'system': False, 'user': True},
        'portForwards': [{'guestPort': 3000, 'hostPort': port, 'hostIP': '127.0.0.1'},
                         {'guestPortRange': [1, 65535], 'proto': 'any', 'ignore': True}],
    }, indent=2)


class ConversionRuntime:
    def __init__(self, root: Path | None = None):
        support = Path(os.environ.get('OLCR_APP_SUPPORT', Path.home() / 'Library/Application Support/OLCR'))
        self.root = root or support / 'cx'
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.stopper: threading.Thread | None = None
        self.remover: threading.Thread | None = None
        self.asset_downloads = 0
        self.state = {'state': 'off', 'message': '', 'url': '', 'error': ''}
        self.enabled = False
        self.last_used = time.monotonic()
        self.closed = threading.Event()
        self.closed.set()

    def configure(self, enabled: bool, *, prepare=False):
        with self.lock:
            if enabled and self.remover and self.remover.is_alive():
                raise RuntimeSetupError('変換環境の削除中です。完了後にもう一度お試しください。')
            self.enabled = enabled
            if enabled:
                if self.stopper and self.stopper.is_alive():
                    return dict(self.state)
                if not self.worker or not self.worker.is_alive():
                    self.closed.clear()
                    if self.state['state'] == 'off':
                        self.update('idle', '')
                return self.prepare() if prepare else dict(self.state)
            self.closed.set()
            self.update('stopping', '停止中…', url='')
            if not self.stopper or not self.stopper.is_alive():
                self.stopper = threading.Thread(target=lambda: self.close(restart=True), name='olcr-convertx-stop', daemon=True)
                self.stopper.start()
        return dict(self.state)

    def check_enabled(self):
        if not self.enabled or self.closed.is_set():
            raise RuntimeSetupError('ConvertはOFFです。設定でONにしてください。')

    def progress(self, text):
        self.check_enabled()
        self.update('preparing', text)

    @property
    def binary(self):
        return self.root / f'lima-{LIMA_VERSION}' / 'bin/limactl'

    def environment(self):
        return {**os.environ, 'LIMA_HOME': str(self.root / 'vm'), 'LIMA_CACHE_HOME': str(self.root / 'cache'),
                'PATH': str(self.binary.parent) + os.pathsep + os.environ.get('PATH', '')}

    def status(self):
        with self.lock:
            result = dict(self.state)
        if result['state'] == 'ready' and not self.healthy(result['url']):
            self.update('idle', '変換環境を再開します。', url='')
            return dict(self.state)
        return result

    def downloads_status(self):
        with self.lock:
            state = dict(self.state)
            removing = bool(self.remover and self.remover.is_alive())
            enabled = self.enabled
        downloaded = not self.root.is_symlink() and self.root.is_dir() and any(self.root.iterdir())
        return {'downloaded': downloaded, 'enabled': enabled,
                'has_conversion_data': (self.root / 'vm' / 'cx').exists(),
                'state': 'removing' if removing else state['state'],
                'message': 'ダウンロードデータを削除中…' if removing else state.get('message', '')}

    def remove_downloads(self):
        with self.lock:
            if self.enabled:
                raise RuntimeSetupError('先に設定でConvertをOFFにしてください。')
            if ((self.worker and self.worker.is_alive())
                    or (self.stopper and self.stopper.is_alive()) or self.asset_downloads):
                raise RuntimeSetupError('変換環境の停止または準備が完了してから削除してください。')
            if self.remover and self.remover.is_alive():
                return self.downloads_status()
            if not self.root.exists():
                return self.downloads_status()
            self.update('removing', '変換環境を削除中…', url='')
            self.remover = threading.Thread(target=self._remove_downloads,
                                            name='olcr-convertx-remove', daemon=True)
            self.remover.start()
        return self.downloads_status()

    def _remove_downloads(self):
        try:
            if self.root.is_symlink():
                raise RuntimeSetupError('変換環境の保存先を安全に確認できません。')
            if self.root.exists():
                with (self.root / 'setup.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    self.stop_vm()
                    shutil.rmtree(self.root)
            self.update('off', '', url='', error='')
        except Exception as exc:
            message = str(exc) if isinstance(exc, RuntimeSetupError) else '変換環境のデータを削除できませんでした。'
            self.update('failed', message, error=message, url='')

    def update(self, state, message, **extra):
        with self.lock:
            self.state.update(state=state, message=message, **extra)

    @staticmethod
    def healthy(url):
        if not url:
            return False
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                return client.get(url + '/healthcheck').status_code == 200
        except httpx.HTTPError:
            return False

    def prepare(self):
        with self.lock:
            self.check_enabled()
            self.last_used = time.monotonic()
            if self.state['state'] == 'ready' and self.healthy(self.state['url']):
                return dict(self.state)
            if not self.worker or not self.worker.is_alive():
                self.closed.clear()
                self.update('preparing', '初回の変換環境を準備中…', error='')
                self.worker = threading.Thread(target=self._prepare, name='olcr-convertx-setup', daemon=True)
                self.worker.start()
            return dict(self.state)

    def ensure(self, timeout=1800):
        self.prepare()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_enabled()
            state = self.status()
            if state['state'] == 'ready':
                self.last_used = time.monotonic()
                return state['url']
            if state['state'] == 'failed':
                raise RuntimeSetupError(state['error'])
            time.sleep(0.3)
        raise RuntimeSetupError('変換環境の準備がタイムアウトしました。再試行してください。')

    def command(self, *args, timeout=900, allow_failure=False):
        # Keep diagnostic output private; never interpolate filenames into shell code.
        with (self.root / 'runtime.log').open('ab') as log:
            process = subprocess.Popen([str(self.binary), '--tty=false', *args], env=self.environment(),
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if args[0] != 'stop':
                        self.check_enabled()
                    if time.monotonic() >= deadline:
                        raise RuntimeSetupError('変換環境の準備がタイムアウトしました。')
                    time.sleep(0.2)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        if process.returncode and not allow_failure:
            raise RuntimeSetupError('変換環境の起動に失敗しました。再試行してください。')
        return process.returncode == 0

    def _prepare(self):
        try:
            arch = platform.machine()
            if platform.system() != 'Darwin' or arch not in LIMA_HASHES:
                raise RuntimeSetupError('自動変換環境はmacOSのApple Silicon／Intelに対応しています。')
            if tuple(map(int, platform.mac_ver()[0].split('.')[:2])) < (13, 0):
                raise RuntimeSetupError('自動変換にはmacOS 13以降が必要です。')
            self.root.mkdir(parents=True, exist_ok=True)
            self.root.chmod(0o700)
            with (self.root / 'setup.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.check_enabled()
                self._install(arch)
                self.check_enabled()
                self._start(arch)
            self.check_enabled()
            self.update('ready', '変換環境の準備ができました。', error='')
        except Exception as exc:
            message = str(exc) if isinstance(exc, RuntimeSetupError) else '変換環境を準備できませんでした。通信状態と空き容量を確認して再試行してください。'
            self.update('failed' if self.enabled else 'off', message if self.enabled else '',
                        error=message if self.enabled else '', url='')
            self.stop_vm()

    def _install(self, arch):
        self.update('preparing', '変換環境を検証中…')
        if not (self.root / 'vm/cx/lima.yaml').exists() and shutil.disk_usage(self.root).free < 12_000_000_000:
            raise RuntimeSetupError('変換環境の準備には12 GB以上の空き容量が必要です。')
        archive = self.root / f'lima-{LIMA_VERSION}-{arch}.tar.gz'
        marker = self.binary.parent.parent / 'olcr-verified.json'
        if not marker.is_file() or not self.binary.is_file():
            verified_download(f'https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}/lima-{LIMA_VERSION}-Darwin-{arch}.tar.gz',
                              archive, LIMA_HASHES[arch], self.progress)
            self.check_enabled()
            target = self.binary.parent.parent
            with tempfile.TemporaryDirectory(dir=self.root, prefix='install-') as temporary:
                with tarfile.open(archive) as bundle:
                    bundle.extractall(temporary, filter='data')
                (Path(temporary) / 'olcr-verified.json').write_text(json.dumps({'sha256': LIMA_HASHES[arch]}))
                if target.exists():
                    shutil.rmtree(target)
                Path(temporary).rename(target)
                # TemporaryDirectory now points to a moved directory; cleanup is harmless.
        suffix = 'arm64' if arch == 'arm64' else 'amd64'
        image = self.root / f'ubuntu-{UBUNTU_DATE}-{arch}.img'
        if not (self.root / 'vm/cx/lima.yaml').exists():
            verified_download(f'https://cloud-images.ubuntu.com/releases/noble/release-{UBUNTU_DATE}/ubuntu-24.04-server-cloudimg-{suffix}.img',
                              image, UBUNTU_HASHES[arch], self.progress)

    def download_assets(self, cancelled: threading.Event | None = None):
        """Cache ConvertX's verified host files without enabling or starting it."""
        with self.lock:
            if self.remover and self.remover.is_alive():
                raise RuntimeSetupError('変換環境の削除中です。完了後にもう一度お試しください。')
            self.asset_downloads += 1
        try:
            return self._download_assets(cancelled)
        finally:
            with self.lock:
                self.asset_downloads = max(0, self.asset_downloads - 1)

    def _download_assets(self, cancelled: threading.Event | None = None):
        arch = platform.machine()
        if platform.system() != 'Darwin' or arch not in LIMA_HASHES:
            raise RuntimeSetupError('自動変換環境はmacOSのApple Silicon／Intelに対応しています。')
        if tuple(map(int, platform.mac_ver()[0].split('.')[:2])) < (13, 0):
            raise RuntimeSetupError('自動変換にはmacOS 13以降が必要です。')
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if shutil.disk_usage(self.root).free < 12_000_000_000:
            raise RuntimeSetupError('変換環境の取得には12 GB以上の空き容量が必要です。')
        def progress(message):
            self.update('downloading', message)
        with (self.root / 'setup.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if cancelled is not None and cancelled.is_set():
                raise RuntimeSetupError('変換環境のダウンロードを中止しました。')
            archive = self.root / f'lima-{LIMA_VERSION}-{arch}.tar.gz'
            marker = self.binary.parent.parent / 'olcr-verified.json'
            if not marker.is_file() or not self.binary.is_file():
                verified_download(f'https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}/lima-{LIMA_VERSION}-Darwin-{arch}.tar.gz',
                                  archive, LIMA_HASHES[arch], progress, cancelled)
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeSetupError('変換環境のダウンロードを中止しました。')
                target = self.binary.parent.parent
                with tempfile.TemporaryDirectory(dir=self.root, prefix='install-') as temporary:
                    with tarfile.open(archive) as bundle:
                        bundle.extractall(temporary, filter='data')
                    (Path(temporary) / 'olcr-verified.json').write_text(json.dumps({'sha256': LIMA_HASHES[arch]}))
                    if target.exists():
                        shutil.rmtree(target)
                    Path(temporary).rename(target)
            suffix = 'arm64' if arch == 'arm64' else 'amd64'
            image = self.root / f'ubuntu-{UBUNTU_DATE}-{arch}.img'
            if not (self.root / 'vm/cx/lima.yaml').exists() and not (image.is_file() and file_hash(image) == UBUNTU_HASHES[arch]):
                verified_download(f'https://cloud-images.ubuntu.com/releases/noble/release-{UBUNTU_DATE}/ubuntu-24.04-server-cloudimg-{suffix}.img',
                                  image, UBUNTU_HASHES[arch], progress, cancelled)
        if cancelled is not None and cancelled.is_set():
            raise RuntimeSetupError('変換環境のダウンロードを中止しました。')
        self.update('downloaded', '変換環境のファイルを取得しました。ConvertはOFFのままです。')
        return {'downloaded': True, 'enabled': self.enabled}

    def _start(self, arch):
        configuration = self.root / 'service.json'
        cached_image = None
        if configuration.is_file():
            previous = json.loads(configuration.read_text())
            port = previous['port']
            cached_image = previous.get('image')
        else:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            configuration.write_text(json.dumps({'port': port, 'image': IMAGE}))
        url = f'http://127.0.0.1:{port}'
        self.update('preparing', '変換環境を起動中…', url=url)
        if not (self.root / 'vm/cx/lima.yaml').exists():
            template = self.root / 'cx.yaml'
            template.write_text(vm_config(self.root / f'ubuntu-{UBUNTU_DATE}-{arch}.img', arch, UBUNTU_HASHES[arch], port))
            self.command('start', '--name=cx', '--timeout=15m', str(template), timeout=1000)
        else:
            self.command('start', '--timeout=15m', 'cx', timeout=1000)
        self.update('preparing', '変換ツール一式を取得中… 初回は数分かかります。')
        # Restart the owned container directly. nerdctl's image-inspect lookup
        # by multi-platform digest is not reliable after a VM restart.
        reused = cached_image == IMAGE and self.command('shell', 'cx', 'nerdctl', 'start', 'olcr-convertx', allow_failure=True)
        if not reused:
            self.command('shell', 'cx', 'nerdctl', 'pull', IMAGE, timeout=1200)
            # The VM and named container belong exclusively to this OLCR profile.
            self.command('shell', 'cx', 'nerdctl', 'rm', '-f', 'olcr-convertx', allow_failure=True)
            self.command('shell', 'cx', 'nerdctl', 'run', '-d', '--name', 'olcr-convertx',
                         '-p', '127.0.0.1:3000:3000', '-e', 'ALLOW_UNAUTHENTICATED=true', '-e', 'HTTP_ALLOWED=true',
                         '-e', 'ACCOUNT_REGISTRATION=false', '-e', 'AUTO_DELETE_EVERY_N_HOURS=1',
                         '-e', 'MAX_CONVERT_PROCESS=1', '-v', 'olcr-convertx-data:/app/data', IMAGE)
            configuration.write_text(json.dumps({'port': port, 'image': IMAGE}))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.check_enabled()
            if self.healthy(url):
                return
            time.sleep(0.5)
        raise RuntimeSetupError('変換サービスが起動しませんでした。再試行してください。')

    def stop_vm(self):
        instance = self.root / 'vm/cx'
        if self.binary.is_file() and any((instance / name).exists() for name in ('ha.pid', 'vz.pid')):
            try:
                self.command('stop', 'cx', timeout=30)
            except (RuntimeSetupError, OSError, subprocess.TimeoutExpired):
                try:
                    self.command('stop', '--force', 'cx', timeout=10)
                except (RuntimeSetupError, OSError, subprocess.TimeoutExpired):
                    pass

    def close(self, *, restart=False):
        if not restart:
            self.enabled = False
        self.closed.set()
        worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=65)
        self.stop_vm()
        self.update('off' if not self.enabled else 'idle', '', url='')
        if restart and self.enabled:
            with self.lock:
                self.closed.clear()
                self.prepare()


runtime = ConversionRuntime()
atexit.register(runtime.close)
