"""Pinned native MLX-VLM runtime for Bonsai 2 quantization profiles."""
from __future__ import annotations

import os
import platform
import subprocess
from functools import lru_cache


MLX_LM_VERSION = '0.32.0'
MLX_VLM_VERSION = '0.7.8'
LLGUIDANCE_VERSION = '1.8.0'


def _python(asset_id='ternary_bonsai_2_27b_6bit'):
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


def available(python_path=None) -> bool:
    python = python_path or _python()
    return _available(str(python), platform.system(), platform.machine())


def ensure() -> None:
    if available():
        return
    raise RuntimeError('MLX_VLM_RUNTIME_PREPARATION_REQUIRED')
