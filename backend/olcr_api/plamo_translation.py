"""Run PFN's local PLaMo translation CLI with an explicitly selected checkpoint."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import threading
from functools import lru_cache


CLI_VERSION = '1.2.0'
_MODEL_LOCK = threading.Lock()
LANGUAGES = {
    'ar': 'Arabic',
    'de': 'German',
    'en': 'English',
    'es': 'Spanish',
    'fr': 'French',
    'id': 'Indonesian',
    'it': 'Italian',
    'ja': 'Japanese',
    'ja-easy': 'Japanese(easy)',
    'ko': 'Korean',
    'nl': 'Dutch',
    'ru': 'Russian',
    'th': 'Thai',
    'vi': 'Vietnamese',
    'zh': 'Chinese',
    'zh-cn': 'Chinese',
    'zh-hans': 'Chinese',
    'zh-tw': 'Taiwanese',
    'zh-hant': 'Taiwanese',
    'zh-hk': 'Taiwanese',
    'zh-mo': 'Taiwanese',
}


def runtime_python() -> Path:
    root = Path(__file__).resolve().parents[2]
    value = os.environ.get('OLCR_MLX_PYTHON')
    return Path(value).expanduser() if value else root / '.venv-mlx' / 'bin' / 'python'


def cli_path(python: Path | None = None) -> Path:
    python = python or runtime_python()
    return python.parent / 'plamo-translate'


@lru_cache(maxsize=4)
def _runtime_available(python_text: str, system: str, machine: str) -> bool:
    python = Path(python_text)
    executable = cli_path(python)
    if (system != 'Darwin' or machine != 'arm64' or not python.is_file() or not executable.is_file()):
        return False
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    try:
        result = subprocess.run(
            [str(python), '-c',
             'from importlib.metadata import version; '
             f'assert version("plamo-translate") == "{CLI_VERSION}"'],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def runtime_available(python_text: str) -> bool:
    return _runtime_available(python_text, platform.system(), platform.machine())


def ensure_runtime() -> None:
    python = runtime_python()
    if runtime_available(str(python)):
        return
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('PLAMO_TRANSLATION_RUNTIME_UNAVAILABLE')
    if not python.is_file():
        raise RuntimeError('MLX_RUNTIME_UNAVAILABLE')
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    result = subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                             '--no-deps', f'plamo-translate=={CLI_VERSION}'],
                            env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
    _runtime_available.cache_clear()
    if result.returncode or not runtime_available(str(python)):
        raise RuntimeError('PLAMO_TRANSLATION_RUNTIME_UNAVAILABLE')


def _language_name(code: str) -> str:
    normalized = str(code or '').strip().replace('_', '-').lower()
    name = LANGUAGES.get(normalized) or LANGUAGES.get(normalized.split('-', 1)[0])
    if not name:
        raise ValueError('MODEL_LANGUAGE_UNSUPPORTED')
    return name


def _cli_failure_code(stdout: str, stderr: str) -> str:
    # Inspect output only for a small set of failure classes. Never return or
    # persist CLI output because it can contain fragments of the user's text.
    detail = f"{stderr}\n{stdout}".casefold()
    if re.search(r"out of memory|memoryerror|failed to allocate|allocation failed|not enough memory|resource_exhausted", detail):
        return 'MODEL_TRANSLATION_MEMORY_ERROR'
    if re.search(r"checkpoint is .*requested|unsupported precision|quantized weights cannot recover", detail):
        return 'MODEL_TRANSLATION_CHECKPOINT_MISMATCH'
    if re.search(r"timed out waiting for the translation server", detail):
        return 'MODEL_TRANSLATION_TIMEOUT'
    if re.search(r"translation server failed to start|error during server running|exited during model loading|could not find a suitable mcp server", detail):
        return 'MODEL_TRANSLATION_RUNTIME_FAILED'
    return 'MODEL_TRANSLATION_LOCAL_EXECUTION_FAILED'


def _report_cli_failure(code: str, exit_code: int) -> None:
    # Keep diagnostics useful without logging stdout, stderr, or the source text.
    print(f"PLAMO_TRANSLATION_CLI_FAILED category={code} exit_code={exit_code}",
          file=sys.stderr, flush=True)


def translate(text: str, source: str, target: str, checkpoint: Path | str,
              timeout: float = 300) -> str:
    """Translate through the pinned MLX CLI without putting user text in argv."""
    python = runtime_python()
    executable = cli_path(python)
    if not runtime_available(str(python)):
        raise ValueError('MODEL_PRESET_RUNTIME_UNAVAILABLE')
    model = Path(checkpoint)
    if not (model / 'config.json').is_file():
        raise ValueError('MODEL_PRESET_NOT_DOWNLOADED')
    source_name, target_name = _language_name(source), _language_name(target)
    command = [str(executable), '--backend-type', 'mlx', '--model', str(model),
               '--precision', '4bit', '--no-stream', '--from', source_name, '--to', target_name]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                       TOKENIZERS_PARALLELISM='false', NO_COLOR='1')
    try:
        # The CLI uses a temporary config path under TMPDIR. Isolating it per
        # request avoids collisions with another CLI session or translation.
        with tempfile.TemporaryDirectory(prefix='olcr-plamo-translation-') as temporary:
            environment['TMPDIR'] = temporary
            with _MODEL_LOCK:
                result = subprocess.run(
                    command, input=str(text), text=True, encoding='utf-8',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment, timeout=timeout, check=False,
                )
    except subprocess.TimeoutExpired as exc:
        raise ValueError('MODEL_TRANSLATION_TIMEOUT') from exc
    except OSError as exc:
        raise ValueError('MODEL_PRESET_RUNTIME_UNAVAILABLE') from exc
    if result.returncode != 0:
        code = _cli_failure_code(result.stdout, result.stderr)
        _report_cli_failure(code, result.returncode)
        raise ValueError(code)
    translated = result.stdout.strip()
    if not translated:
        code = _cli_failure_code(result.stdout, result.stderr)
        _report_cli_failure(code, result.returncode)
        raise ValueError(code)
    return translated
