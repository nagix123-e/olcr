"""Pinned text-only MLX runtime for models selected by the landing profiles."""
from __future__ import annotations

from functools import lru_cache
import os
import platform
import subprocess


MLX_VERSION = '0.32.0'
MLX_LM_VERSION = '0.32.0'
LLGUIDANCE_VERSION = '1.8.0'


def _python():
    from .audio_mlx import _python as audio_python
    return audio_python()


@lru_cache(maxsize=4)
def _available(python_path: str, system: str, machine: str) -> bool:
    if system != 'Darwin' or machine != 'arm64' or not os.path.isfile(python_path):
        return False
    env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    try:
        result = subprocess.run([python_path, '-c',
            'from importlib.metadata import version; '
            f'assert version("mlx") == "{MLX_VERSION}"; '
            f'assert version("mlx-lm") == "{MLX_LM_VERSION}"; '
            f'assert version("llguidance") == "{LLGUIDANCE_VERSION}"; '
            'import mlx, mlx_lm, llguidance, llguidance.hf'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def available() -> bool:
    return _available(str(_python()), platform.system(), platform.machine())


def ensure() -> None:
    if available():
        return
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('MLX_PROFILE_RUNTIME_UNAVAILABLE')
    python = _python()
    if not python.is_file():
        raise RuntimeError('MLX_RUNTIME_UNAVAILABLE')
    env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    result = subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
        '--no-deps', f'mlx=={MLX_VERSION}', f'mlx-lm=={MLX_LM_VERSION}',
        f'llguidance=={LLGUIDANCE_VERSION}'], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
    _available.cache_clear()
    if result.returncode or not available():
        raise RuntimeError('MLX_PROFILE_RUNTIME_UNAVAILABLE')
