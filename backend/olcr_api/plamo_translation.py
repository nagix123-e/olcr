"""Run PFN's local PLaMo translation server with an explicitly selected checkpoint."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from functools import lru_cache

from .translation_languages import plamo_source_language_tag, normalize_language_code
from .text_processing import TextPhase, split_text_into_phases


CLI_VERSION = '1.2.0'
PHASE_RESTART_COOLDOWN_SECONDS = 3
PHASE_MAX_OUTPUT_TOKENS = 8_192
REPETITION_PENALTY = '1.05'
REPETITION_CONTEXT_SIZE = '128'
MAX_PHASE_RECOVERY_DEPTH = 2
MIN_PHASE_RECOVERY_CHARS = 1_000
_MODEL_LOCK = threading.Lock()
_MCP_TRANSLATION_SCRIPT = r'''
import asyncio
import json
import sys
from plamo_translate.clients.translate import MCPClient

payload = json.load(sys.stdin)

async def translate_all():
    outputs = []
    for item in payload["items"]:
        source = item["source"]
        source_line = f"input lang={source}\n" if source else "input\n"
        messages = [
            {"role": "user", "content": source_line + item["text"]},
            {"role": "user", "content": f"output lang={item['target']}\n"},
        ]
        result = []
        async for chunk in MCPClient(stream=False).translate(messages):
            result.append(chunk)
        outputs.append("".join(result))
    return outputs

sys.stdout.write(json.dumps(asyncio.run(translate_all()), ensure_ascii=False))
'''


def runtime_python(asset_id: str = 'plamo_3_translate_2b') -> Path:
    from .model_runtime_manager import get_runtime_manager
    manager = get_runtime_manager()
    if manager is not None:
        binding = manager.resolve(asset_id)
        if binding.ready and binding.python_path:
            return Path(binding.python_path).expanduser()
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


def ensure_runtime(python: Path | None = None) -> None:
    python = python or runtime_python()
    if runtime_available(str(python)):
        return
    raise RuntimeError('PLAMO_TRANSLATION_RUNTIME_PREPARATION_REQUIRED')


def _cli_failure_code(stdout: str, stderr: str) -> str:
    # Inspect output only for a small set of failure classes. Never return or
    # persist CLI output because it can contain fragments of the user's text.
    detail = f"{stderr}\n{stdout}".casefold()
    if re.search(r"out of memory|memoryerror|failed to allocate|allocation failed|not enough memory|resource_exhausted", detail):
        return 'MODEL_TRANSLATION_MEMORY_ERROR'
    if re.search(r"checkpoint is .*requested|unsupported precision|quantized weights cannot recover", detail):
        return 'MODEL_TRANSLATION_CHECKPOINT_MISMATCH'
    if re.search(r"translation reached plamo_max_tokens before eos", detail):
        return 'MODEL_TRANSLATION_OUTPUT_LIMIT'
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
              timeout: float = 1200, python_path: Path | str | None = None) -> str:
    """Translate one text through the pinned local MCP server."""
    return translate_many([text], source, target, checkpoint, timeout, python_path)[0]


def translate_many(texts: list[str] | tuple[str, ...], source: str, target: str,
                   checkpoint: Path | str, timeout: float = 1200,
                   python_path: Path | str | None = None,
                   on_progress=None) -> list[str]:
    """Translate ordered phases, stopping and restarting the model between phases."""
    if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError('MODEL_TRANSLATION_INPUT_INVALID')
    python = Path(python_path).expanduser() if python_path else runtime_python()
    executable = cli_path(python)
    if not runtime_available(str(python)):
        raise ValueError('MODEL_PRESET_RUNTIME_UNAVAILABLE')
    model = Path(checkpoint)
    if not (model / 'config.json').is_file():
        raise ValueError('MODEL_PRESET_NOT_DOWNLOADED')
    # PLaMo's automatic input-language tag only covers English and Japanese.
    # Detect the entire request once so every restarted phase receives the
    # same explicit source language rather than a blank `input` tag.
    source_code = plamo_source_language_tag(source, "\n".join(texts))
    target_code = normalize_language_code(target)
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                       TOKENIZERS_PARALLELISM='false', NO_COLOR='1',
                       PLAMO_TRANSLATE_CLI_MODEL_NAME=str(model),
                       PLAMO_TRANSLATE_CLI_PRECISION='4bit')
    # PLaMo defaults to 32,768 output tokens. A degenerate translation can
    # consume the entire 1,200-second phase timeout before the non-streaming
    # MCP client returns. Keep each 5,000-character phase bounded, and force
    # the empirically validated anti-repetition settings so inherited launchd
    # or shell values cannot silently disable them.
    environment.update(
        PLAMO_MAX_TOKENS=str(PHASE_MAX_OUTPUT_TOKENS),
        PLAMO_TRANSLATE_CLI_REPETITION_PENALTY=REPETITION_PENALTY,
        PLAMO_TRANSLATE_CLI_REPETITION_CONTEXT_SIZE=REPETITION_CONTEXT_SIZE,
    )

    def report_progress(state: str, index: int, output: str | None = None) -> None:
        if not on_progress:
            return
        # A completed phase is persisted by the caller before the next phase
        # starts, so failures in that durable write must stop this run.
        on_progress(state, index, len(texts), output)

    try:
        # Keep the task and its saved phase results alive, while releasing the
        # local model process and its MLX memory between phases.
        with _MODEL_LOCK:
            phase_timeout = max(0.1, timeout)
            translated = []
            report_progress('source_detected', 1, source_code)
            for index, text in enumerate(texts, start=1):
                pending = [(TextPhase(text), 0)]
                phase_outputs = []
                while pending:
                    phase, recovery_depth = pending.pop(0)
                    try:
                        output = _translate_segment(
                            phase.text, index, len(texts), python, executable, model,
                            phase_environment=environment,
                            timeout=phase_timeout,
                            source_code=source_code,
                            target_code=target_code,
                            report_progress=report_progress,
                        )
                    except ValueError as exc:
                        if (str(exc) != 'MODEL_TRANSLATION_OUTPUT_LIMIT'
                                or recovery_depth >= MAX_PHASE_RECOVERY_DEPTH
                                or len(phase.text) <= MIN_PHASE_RECOVERY_CHARS):
                            raise
                        children = split_text_into_phases(
                            phase.text, max_chars=max(1, len(phase.text) // 2))
                        if len(children) < 2:
                            raise
                        children[-1] = TextPhase(
                            children[-1].text,
                            children[-1].separator_after + phase.separator_after,
                        )
                        report_progress(
                            'phase_split_retry', index,
                            f"{len(phase.text):,}文字を{len(children)}分割して再試行します。",
                        )
                        pending[0:0] = [(child, recovery_depth + 1) for child in children]
                        report_progress('phase_cooldown', index)
                        time.sleep(PHASE_RESTART_COOLDOWN_SECONDS)
                        continue

                    phase_outputs.append(output + phase.separator_after)
                    if pending:
                        report_progress('phase_cooldown', index)
                        time.sleep(PHASE_RESTART_COOLDOWN_SECONDS)

                combined_phase_output = ''.join(phase_outputs).strip()
                translated.append(combined_phase_output)
                report_progress('phase_completed', index, combined_phase_output)
                if index < len(texts):
                    report_progress('phase_cooldown', index)
                    time.sleep(PHASE_RESTART_COOLDOWN_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('MODEL_TRANSLATION_TIMEOUT') from exc
    except OSError as exc:
        raise ValueError('MODEL_PRESET_RUNTIME_UNAVAILABLE') from exc
    return translated


def _translate_segment(text: str, index: int, total: int, python: Path,
                       executable: Path, model: Path, *, phase_environment: dict[str, str],
                       timeout: float, source_code: str, target_code: str,
                       report_progress) -> str:
    """Run one bounded generation and always stop its isolated model server."""
    with tempfile.TemporaryDirectory(prefix='olcr-plamo-translation-') as temporary:
        segment_environment = phase_environment.copy()
        segment_environment['TMPDIR'] = temporary
        startup_deadline = time.monotonic() + timeout
        report_progress('model_starting', index)
        server = subprocess.Popen(
            [str(executable), 'server', '--backend-type', 'mlx',
             '--model', str(model), '--precision', '4bit'],
            env=segment_environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            config_path = Path(temporary) / 'plamo-translate-config.json'
            _wait_for_local_server(server, config_path, startup_deadline)
            report_progress('model_ready', index)
            phase_deadline = time.monotonic() + timeout
            report_progress('phase_started', index)
            payload = json.dumps({
                'items': [{'source': source_code, 'target': target_code, 'text': text}],
            }, ensure_ascii=False)
            remaining = phase_deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('MODEL_TRANSLATION_TIMEOUT')
            try:
                result = subprocess.run(
                    [str(python), '-c', _MCP_TRANSLATION_SCRIPT],
                    input=payload, text=True, encoding='utf-8',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=segment_environment, timeout=remaining, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError('MODEL_TRANSLATION_TIMEOUT') from exc
            if result.returncode != 0:
                code = _cli_failure_code(result.stdout, result.stderr)
                _report_cli_failure(code, result.returncode)
                raise ValueError(code)
            try:
                phase_result = json.loads(result.stdout)
            except (TypeError, ValueError):
                phase_result = None
            if (not isinstance(phase_result, list) or len(phase_result) != 1
                    or not isinstance(phase_result[0], str) or not phase_result[0].strip()):
                code = _cli_failure_code(result.stdout, result.stderr)
                _report_cli_failure(code, result.returncode)
                raise ValueError(code)
            return phase_result[0].strip()
        finally:
            try:
                report_progress('model_stopping', index)
            finally:
                _stop_server(server)


def _wait_for_local_server(server: subprocess.Popen, config_path: Path,
                           deadline: float) -> None:
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise ValueError('MODEL_TRANSLATION_RUNTIME_FAILED')
        try:
            config = json.loads(config_path.read_text(encoding='utf-8'))
            port = int(config['port'])
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            time.sleep(0.1)
    raise ValueError('MODEL_TRANSLATION_TIMEOUT')


def _stop_server(server: subprocess.Popen) -> None:
    if server.poll() is not None:
        return
    try:
        server.terminate()
    except ProcessLookupError:
        return
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            server.kill()
        except ProcessLookupError:
            pass
        server.wait()
