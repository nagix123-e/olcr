"""Pinned native MLX-VLM runtime for Bonsai 2 quantization profiles."""
from __future__ import annotations

import os
import platform
import subprocess
from functools import lru_cache


MLX_LM_VERSION = '0.32.0'
MLX_VLM_VERSION = '0.7.8'
LLGUIDANCE_VERSION = '1.8.0'


def _python():
    from .audio_mlx import _python as audio_python
    return audio_python()


@lru_cache(maxsize=4)
def _available(python_path: str, system: str, machine: str) -> bool:
    if system != 'Darwin' or machine != 'arm64' or not os.path.isfile(python_path):
        return False
    try:
        result = subprocess.run([python_path, '-c',
            'from importlib.metadata import version; '
            f'assert version("mlx") == "{MLX_LM_VERSION}"; '
            f'assert version("mlx-lm") == "{MLX_LM_VERSION}"; '
            f'assert version("mlx-vlm") == "{MLX_VLM_VERSION}"; '
            f'assert version("llguidance") == "{LLGUIDANCE_VERSION}"; '
            'import mlx_vlm, llguidance, llguidance.hf; '
            'from mlx_vlm.structured import build_json_schema_logits_processor'],
            env={key:value for key,value in os.environ.items() if key not in {'PYTHONPATH','PYTHONHOME'}},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def available() -> bool:
    return _available(str(_python()), platform.system(), platform.machine())


def ensure() -> None:
    if available():
        return
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('MLX_VLM_RUNTIME_UNAVAILABLE')
    python = _python()
    if not python.is_file():
        raise RuntimeError('MLX_RUNTIME_UNAVAILABLE')
    env = {key:value for key,value in os.environ.items() if key not in {'PYTHONPATH','PYTHONHOME'}}
    result = subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
        '--no-deps', f'mlx=={MLX_LM_VERSION}', f'mlx-lm=={MLX_LM_VERSION}', f'mlx-vlm=={MLX_VLM_VERSION}',
        f'llguidance=={LLGUIDANCE_VERSION}'],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=900)
    _available.cache_clear()
    if result.returncode or not available():
        raise RuntimeError('MLX_VLM_RUNTIME_UNAVAILABLE')
