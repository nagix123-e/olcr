"""Isolated, persistent MLX audio process with cancellation-safe NDJSON IPC."""
from __future__ import annotations

import atexit
from functools import lru_cache
import json
import os
from pathlib import Path
import platform
import queue
import subprocess
import threading


def _python() -> Path:
    root = Path(__file__).resolve().parents[2]
    return Path(os.environ.get('OLCR_AUDIO_MLX_PYTHON') or os.environ.get('OLCR_MLX_PYTHON')
                or root / '.venv-mlx/bin/python').expanduser()


def _environment():
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(key, None)
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
    # Keep float32 vocoder GEMMs consistent across full/chunk matrix shapes.
    # This switch doesn't disable the BF16 main model's native acceleration.
    env['MLX_ENABLE_TF32'] = '0'
    return env


@lru_cache(maxsize=4)
def _installed(python: str) -> bool:
    try:
        result = subprocess.run([python, '-c',
            'from importlib.metadata import version; '
            'assert version("mlx-audio") == "0.4.6"; '
            'from mlx_audio.sts.models.lfm_audio import LFM2AudioModel, LFM2AudioProcessor'],
            env=_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def enabled() -> bool:
    engine = os.environ.get('OLCR_AUDIO_ENGINE', 'auto').lower()
    if engine not in ('auto', 'torch', 'mlx'):
        raise RuntimeError('AUDIO_ENGINE_INVALID')
    if engine == 'torch':
        return False
    available = (platform.system() == 'Darwin' and platform.machine() == 'arm64'
                 and _python().is_file() and _installed(str(_python())))
    if engine == 'mlx' and not available:
        raise RuntimeError('AUDIO_MLX_RUNTIME_UNAVAILABLE')
    return available


@lru_cache(maxsize=4)
def _whisper_installed(python: str) -> bool:
    try:
        result = subprocess.run([python, '-c',
            'from importlib.metadata import version; '
            'assert version("mlx-whisper") == "0.4.3"; import mlx_whisper'],
            env=_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def whisper_enabled() -> bool:
    return (platform.system() == 'Darwin' and platform.machine() == 'arm64'
            and _python().is_file() and _whisper_installed(str(_python())))


def ensure_whisper_runtime() -> None:
    if whisper_enabled():
        return
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('AUDIO_WHISPER_RUNTIME_UNAVAILABLE')
    python = _python()
    if not python.is_file():
        raise RuntimeError('AUDIO_MLX_RUNTIME_UNAVAILABLE')
    result = subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                             '--no-deps', 'mlx-whisper==0.4.3'],
                            env=_environment(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    _whisper_installed.cache_clear()
    if result.returncode or not whisper_enabled():
        raise RuntimeError('AUDIO_WHISPER_RUNTIME_UNAVAILABLE')


class AudioWorker:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()

    def _start(self):
        self.close()
        worker = Path(__file__).with_name('audio_mlx_worker.py')
        process = subprocess.Popen([str(_python()), '-u', str(worker)], env=_environment(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   text=True, encoding='utf-8', bufsize=1)
        messages = queue.Queue()
        def read():
            try:
                for line in process.stdout:
                    messages.put(line)
            except (OSError, ValueError):
                pass  # Cancellation closes this process's pipe.
            finally:
                messages.put(None)
        threading.Thread(target=read, name='olcr-audio-mlx-output', daemon=True).start()
        self.process, self.messages = process, messages

    def stream(self, request, cancelled=None):
        cancelled = cancelled or threading.Event()
        while not self.lock.acquire(timeout=0.1):
            if cancelled.is_set():
                return
        completed = False
        try:
            if cancelled.is_set():
                completed = True
                return
            if self.process is None or self.process.poll() is not None:
                self._start()
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
            while not cancelled.is_set():
                try:
                    line = self.messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    raise RuntimeError('AUDIO_MLX_WORKER_EXITED')
                event = json.loads(line)
                if event.get('type') == 'error':
                    raise RuntimeError(event.get('error', 'AUDIO_MLX_RUNTIME_FAILED'))
                if event.get('type') == 'done':
                    completed = True
                yield event
                if completed:
                    break
        finally:
            # Killing ONLY our own worker interrupts Metal work and discards
            # its protocol state. A later request cannot receive stale audio.
            if not completed:
                self.close()
            self.lock.release()


worker = AudioWorker()
atexit.register(worker.close)


def stream(operation, language, path, model_id, cancelled=None, **payload):
    yield from worker.stream({'operation': operation, 'language': language,
                              'path': str(path), 'model': model_id, **payload}, cancelled)
