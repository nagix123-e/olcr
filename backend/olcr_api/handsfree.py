"""Opt-in local hands-free runtime. No acquisition or ML imports on normal boot."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
import uuid
import wave
from urllib.request import urlopen

PACK_VERSION = '1'
PACK_REQUIREMENTS = ('local-wake==0.1.2', 'mediapipe==0.10.21',
                     'opencv-contrib-python==4.11.0.86', 'numpy==1.26.4',
                     'librosa==0.11.0', 'onnxruntime==1.20.1')
GESTURE_MODEL = 'https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task'
GESTURE_SHA256 = '97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482'


def validate_audio(encoded: str, max_seconds: float = 4) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > 180_000:
        raise ValueError('HANDSFREE_AUDIO_INVALID')
    try:
        raw = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(raw)) as audio:
            duration = audio.getnframes() / audio.getframerate()
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or audio.getframerate() != 16000 or not 0.1 <= duration <= max_seconds
                    or audio.getcomptype() != 'NONE'):
                raise ValueError()
            if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
                raise ValueError()
    except Exception as exc:
        raise ValueError('HANDSFREE_AUDIO_INVALID') from exc
    return raw


class HandsFreeManager:
    def __init__(self, root: Path):
        self.root = Path(root) / 'handsfree'
        self.lock = threading.RLock()
        self.worker_lock = threading.RLock()
        self.worker = None
        self.installer = None
        self.session = None
        self.cancel = threading.Event()
        self.last_seen = 0.
        self.job = {'phase': 'off', 'downloaded_bytes': 0, 'total_bytes': None}

    @property
    def python(self):
        return self.root / 'runtime' / 'bin' / 'python'

    def installed(self):
        try:
            marker = json.loads((self.root / 'complete.json').read_text())
            return (marker['version'] == PACK_VERSION and self.python.is_file()
                    and hashlib.sha256((self.root / 'gesture_recognizer.task').read_bytes()).hexdigest() == GESTURE_SHA256)
        except (OSError, ValueError, KeyError):
            return False

    def profile(self):
        try:
            return json.loads((self.root / 'profile.json').read_text())
        except (OSError, ValueError):
            return {'label': 'OLCR', 'count': 0, 'threshold': 0.1}

    def status(self, session=None):
        with self.lock:
            if self.session and time.monotonic() - self.last_seen > 20:
                self.stop()
            if session and session == self.session:
                self.last_seen = time.monotonic()
            return {**self.job, 'installed': self.installed(), 'enabled': bool(self.session),
                    'session': self.session, 'profile': self.profile()}

    def enable(self, language='ja'):
        if language not in {'ja', 'en'}: raise ValueError('HANDSFREE_LANGUAGE_INVALID')
        with self.lock:
            if self.session:
                return self.status(self.session)
            if self.installer and self.installer.is_alive():
                raise ValueError('HANDSFREE_PREPARATION_STOPPING')
            self.session = uuid.uuid4().hex
            self.cancel = threading.Event()
            self.last_seen = time.monotonic()
            self.job = {'phase': 'preparing', 'downloaded_bytes': 0, 'total_bytes': None}
            self.installer = threading.Thread(target=self._prepare, args=(language,), daemon=True)
            self.installer.start()
            threading.Thread(target=self._watch, args=(self.session,), daemon=True).start()
            return self.status(self.session)

    def require(self, session):
        with self.lock:
            if not self.session or session != self.session or self.cancel.is_set():
                raise ValueError('HANDSFREE_OFF')
            if time.monotonic() - self.last_seen > 20:
                self.stop()
                raise ValueError('HANDSFREE_SESSION_EXPIRED')
            self.last_seen = time.monotonic()

    def _watch(self, session):
        while session == self.session:
            time.sleep(1)
            if time.monotonic() - self.last_seen > 20:
                self.stop(session)

    def stop(self, session=None):
        with self.lock:
            if session and session != self.session:
                return
            self.cancel.set()
            self.session = None
            self.job['phase'] = 'off'
            worker, self.worker = self.worker, None
            if worker and worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)

    def _run(self, args):
        env = dict(os.environ)
        if 'pip' in args:
            env.update(PIP_CONFIG_FILE=os.devnull, PIP_INDEX_URL='https://pypi.org/simple',
                       PIP_EXTRA_INDEX_URL='', PIP_TRUSTED_HOST='', PIP_DISABLE_PIP_VERSION_CHECK='1')
        with self.lock:
            if self.cancel.is_set(): raise RuntimeError('HANDSFREE_CANCELLED')
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        while process.poll() is None:
            if self.cancel.wait(.2):
                process.kill(); process.wait()
                raise RuntimeError('HANDSFREE_CANCELLED')
        if process.returncode:
            raise RuntimeError('HANDSFREE_RUNTIME_INSTALL_FAILED')

    def _fetch(self, url, target, expected=None):
        if self.cancel.is_set(): raise RuntimeError('HANDSFREE_CANCELLED')
        digest = hashlib.sha256()
        temp = target.with_suffix(target.suffix + '.partial')
        try:
            with urlopen(url, timeout=30) as source, temp.open('wb') as output:
                while chunk := source.read(256 * 1024):
                    if self.cancel.is_set():
                        raise RuntimeError('HANDSFREE_CANCELLED')
                    output.write(chunk); digest.update(chunk)
                    with self.lock:
                        self.job['downloaded_bytes'] += len(chunk)
            if expected and digest.hexdigest() != expected:
                raise RuntimeError('HANDSFREE_ASSET_HASH_MISMATCH')
            temp.replace(target)
            return digest.hexdigest()
        finally:
            temp.unlink(missing_ok=True)

    def _prepare(self, language):
        try:
            if not self.installed():
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._run([sys.executable, '-m', 'venv', str(self.root / 'runtime')])
                # Reuse the application's installed torch/audio runtime without changing it.
                site = next((self.root / 'runtime/lib').glob('python*/site-packages'))
                paths = [path for path in sys.path if path.endswith('site-packages')]
                (site / 'olcr-runtime.pth').write_text('\n'.join(paths) + '\n')
                report_path = self.root / 'resolution.json'
                self._run([str(self.python), '-m', 'pip', 'install', '--dry-run',
                           '--only-binary=:all:', '--report', str(report_path), *PACK_REQUIREMENTS])
                report = json.loads(report_path.read_text())
                wheelhouse = self.root / 'wheels'; wheelhouse.mkdir(exist_ok=True)
                wheels = []
                for index, item in enumerate(report['install']):
                    info = item['download_info']
                    # pip's resolver obtains these artifact hashes from the package index.
                    expected = info['archive_info']['hashes']['sha256']
                    url = info['url']
                    if not url.startswith('https://files.pythonhosted.org/') or not url.endswith('.whl'):
                        raise RuntimeError('HANDSFREE_ARTIFACT_INVALID')
                    path = wheelhouse / url.rsplit('/', 1)[-1]
                    with self.lock: self.job.update(phase='downloading', package=index + 1, packages=len(report['install']))
                    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        self._fetch(url, path, expected)
                    wheels.append(str(path))
                with self.lock: self.job['phase'] = 'installing'
                if wheels:
                    self._run([str(self.python), '-m', 'pip', 'install', '--no-index', '--no-deps', *wheels])
                model_hash = self._fetch(GESTURE_MODEL, self.root / 'gesture_recognizer.task', GESTURE_SHA256)
                (self.root / 'assets.json').write_text(json.dumps({'gesture_sha256': model_hash}))
            if self.cancel.is_set():
                return
            with self.lock: self.job['phase'] = 'preparing_audio'
            self._prepare_audio(language)
            with self.worker_lock:
                env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
                with self.lock:
                    if self.cancel.is_set(): return
                    self.worker = subprocess.Popen([str(self.python), str(Path(__file__).with_name('handsfree_worker.py')), str(self.root)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
                self._call({'op': 'init'})
            (self.root / 'complete.json').write_text(json.dumps({'version': PACK_VERSION}))
            with self.lock:
                if not self.cancel.is_set(): self.job['phase'] = 'ready'
        except Exception as exc:
            with self.lock:
                worker, self.worker = self.worker, None
                if worker and worker.poll() is None:
                    worker.kill(); worker.wait(timeout=5)
                if not self.cancel.is_set():
                    error = str(exc)
                    self.job.update(phase='error', error=error if error.startswith('HANDSFREE_') else 'HANDSFREE_PREPARATION_FAILED')

    def _prepare_audio(self, language):
        self._run([sys.executable, '-c',
                   'import sys; sys.path.insert(0,sys.argv[1]); '
                   'from olcr_api.audio_transcription import audio_model_status,prepare_audio_model; '
                   'language=sys.argv[2]; '
                   'audio_model_status(language)["downloaded"] or prepare_audio_model(language)',
                   str(Path(__file__).resolve().parents[1]), language])

    def _call(self, value):
        process = self.worker
        if not process or process.poll() is not None:
            raise ValueError('HANDSFREE_WORKER_UNAVAILABLE')
        with self.worker_lock:
            process.stdin.write(json.dumps(value) + '\n'); process.stdin.flush()
            if not select.select([process.stdout], [], [], 60)[0]:
                self.stop(); raise RuntimeError('HANDSFREE_WORKER_TIMEOUT')
            result = json.loads(process.stdout.readline())
            if 'error' in result: raise ValueError(result['error'])
            return result

    def request(self, value):
        self.require(value.get('session'))
        if self.job['phase'] != 'ready': raise ValueError('HANDSFREE_NOT_READY')
        op = value.get('op')
        if op not in {'enroll', 'wake', 'gesture', 'reset', 'delete'}:
            raise ValueError('HANDSFREE_REQUEST_INVALID')
        if op in {'enroll', 'wake'}: validate_audio(value.get('audio'))
        if op == 'enroll' and (not isinstance(value.get('label'), str) or not 1 <= len(value['label'].strip()) <= 40):
            raise ValueError('HANDSFREE_LABEL_INVALID')
        if op == 'gesture':
            if not isinstance(value.get('image'), str) or len(value['image']) > 600_000:
                raise ValueError('HANDSFREE_IMAGE_INVALID')
        return self._call(value)
