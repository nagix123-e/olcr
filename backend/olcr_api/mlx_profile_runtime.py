"""Pinned text-only MLX runtime for models selected by the landing profiles."""
from __future__ import annotations

from functools import lru_cache
import os
import platform
import subprocess


MLX_VERSION = '0.32.0'
MLX_LM_VERSION = '0.32.0'
LLGUIDANCE_VERSION = '1.8.0'


def _python(asset_id='ornith_1_5_9b_8bit'):
    from .model_runtime_manager import get_runtime_manager
    manager = get_runtime_manager()
    if manager is not None:
        binding = manager.resolve(asset_id)
        if binding.ready and binding.python_path:
            from pathlib import Path
            return Path(binding.python_path)
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    return Path(os.environ.get('OLCR_MLX_PYTHON') or root / '.venv-mlx/bin/python')


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


def available(python_path=None) -> bool:
    python = python_path or _python()
    return _available(str(python), platform.system(), platform.machine())


def ensure() -> None:
    if available():
        return
    raise RuntimeError('MLX_PROFILE_RUNTIME_PREPARATION_REQUIRED')
