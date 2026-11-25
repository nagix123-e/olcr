"""Role-aware model invocation boundary."""
from __future__ import annotations
from .permissions import capability_enabled, full_access

import json
import os
from pathlib import Path
import atexit
import selectors
import subprocess
import tempfile
import threading
import time
from typing import Any

from .config import Settings
from .model_registry import ModelBinding, ModelRegistry, ModelRegistryError, default_registry
from .model_telemetry import model_telemetry
from .ollama import ModelFailure, ModelProvider, OllamaProvider, model_timeout_seconds, inference_is_unbounded
from .generation_control import current_generation, check_cancelled, GenerationCancelled


ROLE_TOKENS = {f"role:{role}": role for role in (
    "semantic_embedding", "typed_local_decisions", "planner_replanner",
    "short_coding_fix_debug", "long_implementation", "gui_grounding",
    "general_image", "normal_chat")}

PLANNER_REPLANNER_MAX_TOKENS = 2048
SHORT_CODING_FIX_MAX_TOKENS = 4096
MIN_OUTPUT_RETRY_TOKENS = 16_384
MAX_OUTPUT_RETRY_TOKENS = 32_000
MAX_OUTPUT_BUDGET_RETRIES = 2


class RoleAwareModelProvider(ModelProvider):
    """Resolve every production call through the canonical manifest.

    Bonsai and the two MLX perception models cross a dedicated subprocess
    boundary so their publisher-specific runtimes cannot be replaced by a
    generic MLX loader.  Ollama models remain in their native provider.
    """
    handles_context_budget = True
    handles_thinking_policy = True
    def __init__(self, settings: Settings, registry: ModelRegistry | None = None):
        self.brain_models = None
        self.settings = settings
        self.registry = registry or ModelRegistry(settings.model_manifest_path)
        self.ollama = OllamaProvider(settings.ollama_endpoint)
        self.repo_root = Path(__file__).resolve().parents[2]
        self._vision_worker_lock = threading.Lock()
        self._vision_worker: subprocess.Popen[str] | None = None
        self._vision_runtime_id: str | None = None
        self._vision_lease = None
        atexit.register(self.close)

    def _binding(self, model: str, *, vision: bool = False) -> ModelBinding:
        token = str(model or "").strip()
        role = ROLE_TOKENS.get(token)
        if role is None and token.startswith('role:'):
            raise ModelFailure('configuration', 'Unknown model role: ' + token)
        if role is None:
            entries = self.registry.entries()
            normal = self.registry.binding("normal_chat")
            if token in {normal.model_id, normal.deployment_model, normal.deployment_repo}:
                role = "normal_chat"
            for candidate_role in ROLE_TOKENS.values():
                if role is not None:
                    break
                candidate = self.registry.binding(candidate_role)
                if token in {candidate.model_id, candidate.deployment_model, candidate.deployment_repo}:
                    role = candidate_role
                    break
        if role is None:
            lower = token.lower()
            if any(marker in lower for marker in ("qwen3.5", "qwen3:14b", "lfm", "rerank")):
                raise ModelFailure("retired_model", f"retired production model rejected: {token}")
            # User-selected local Ollama models remain supported only for the
            # generic normal-chat compatibility setting. They are not used by
            # a role-bound architecture path.
            return ModelBinding("custom_ollama", token, token, "CUSTOM", "UNKNOWN", token,
                                "UNKNOWN", "Ollama", "Ollama model store", True,
                                str(self.registry.manifest_path), self.registry.manifest_sha256)
        try:
            return self.registry.binding(role)
        except ModelRegistryError as exc:
            raise ModelFailure("configuration", str(exc)) from exc

    @staticmethod
    def _runtime_asset(binding: ModelBinding) -> str | None:
        if binding.model_id == "ternary_bonsai_2_27b":
            return "ternary_bonsai_2_27b"
        if binding.model_id.startswith("mlx:"):
            return binding.model_id.removeprefix("mlx:")
        if binding.deployment_model == "ornith-ai/Ornith-1.5-9B-MLX-8bit":
            return "ornith_1_5_9b_8bit"
        return None

    def _runtime_binding(self, asset_id: str | None):
        if not asset_id:
            return None, None
        from .model_runtime_manager import get_runtime_manager
        manager = get_runtime_manager()
        if manager is None:
            raise ModelFailure("configuration", "Model runtime manager is unavailable")
        binding = manager.resolve(asset_id)
        if not binding.ready or not binding.python_path:
            raise ModelFailure("mlx_runtime", "This model's runtime needs preparation in Settings")
        return manager, binding

    @staticmethod
    def _mlx_worker_environment() -> dict[str, str]:
        """Keep the MLX interpreter isolated from the backend's Python path.

        Desktop launches may set PYTHONPATH so the backend can run from a base
        interpreter. Forwarding that path into the separate MLX venv can load
        incompatible packages (for example Python 3.12 transformers inside a
        Python 3.11 worker), breaking Brain generation before provider routing.
        """
        worker_env = os.environ.copy()
        worker_env.pop("PYTHONPATH", None)
        worker_env.pop("PYTHONHOME", None)
        return worker_env

    def _run_mlx(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(payload.pop("runtime_asset_id", "") or "") or None
        manager, runtime_binding = self._runtime_binding(asset_id)
        python = (Path(runtime_binding.python_path) if runtime_binding else
                  Path(os.environ.get("OLCR_MLX_PYTHON") or self.repo_root / ".venv-mlx" / "bin" / "python"))
        if not python.is_file():
            raise ModelFailure("configuration", f"MLX runtime unavailable: {python}")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            payload_path = Path(handle.name)
        live_path=payload_path.with_suffix('.live.jsonl')
        live_stop=threading.Event();live_thread=None
        lease = manager.worker_lease(runtime_binding) if manager and runtime_binding else None
        try:
            if lease:
                lease.__enter__()
            command = [str(python), str(self.repo_root / "backend" / "olcr_api" / "mlx_worker.py"),
                       "--operation", operation, "--payload", str(payload_path)]
            started = time.perf_counter()
            worker_env = self._mlx_worker_environment()
            if runtime_binding:
                worker_env['OLCR_MLX_RUNTIME_PROFILE_ID'] = runtime_binding.profile_id
                worker_env['OLCR_MLX_RUNTIME_ID'] = runtime_binding.runtime_id
            worker_env['OLCR_MLX_PROGRESS_PATH'] = str(Path(self.settings.db_path).resolve().parent / 'model-progress.jsonl')
            from .model_progress import model_progress_active,model_progress
            if model_progress_active():
                from contextvars import copy_context
                worker_env['OLCR_MLX_LIVE_PATH']=str(live_path)
                context=copy_context()
                def follow():
                    position=0;pending=''
                    while True:
                        try:
                            with live_path.open(encoding='utf8') as source:
                                source.seek(position);pending+=source.read();position=source.tell()
                            lines=pending.split('\n');pending=lines.pop()
                            for line in lines:
                                try:
                                    data=json.loads(line)
                                    if data.get('state') in {'reasoning','worker_progress'}:model_progress(**data)
                                except (ValueError,TypeError):pass
                        except FileNotFoundError:pass
                        if live_stop.wait(.1):
                            # One last drain captures the final flushed worker log.
                            if not getattr(follow,'drained',False):follow.drained=True;continue
                            return
                live_thread=threading.Thread(target=lambda:context.run(follow),daemon=True);live_thread.start()
            generation = current_generation()
            check_cancelled()
            if generation is None:
                completed = subprocess.run(command, cwd=str(self.repo_root), capture_output=True, text=True,
                                           timeout=model_timeout_seconds(750), check=False, env=worker_env)
            else:
                process = subprocess.Popen(command, cwd=str(self.repo_root), stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, env=worker_env)
                def stop_worker():
                    if process.poll() is not None:
                        return
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        return
                    def reap():
                        try:
                            process.wait(timeout=.5)
                        except subprocess.TimeoutExpired:
                            try: process.kill()
                            except ProcessLookupError: pass
                    threading.Thread(target=reap, daemon=True).start()
                try:
                    with generation.interrupt(stop_worker):
                        stdout, stderr = process.communicate(timeout=model_timeout_seconds(750))
                    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                finally:
                    stop_worker()
                    process.wait()
            if completed.returncode != 0:
                raise ModelFailure("mlx_runtime", (completed.stderr or completed.stdout or "MLX invocation failed")[-2000:])
            try:
                value = json.loads(completed.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise ModelFailure("mlx_response", "MLX runtime returned invalid JSON") from exc
            if manager and runtime_binding and runtime_binding.legacy and asset_id:
                try:
                    manager.commit_binding(asset_id, runtime_binding)
                except (ValueError, OSError):
                    # A successful generation remains usable; persistence is
                    # retried on the next request if the legacy profile changed.
                    pass
            value["latency_ms"] = (time.perf_counter() - started) * 1000
            value["model_runtime"] = "dedicated_mlx"
            return value
        except subprocess.TimeoutExpired as exc:
            raw_progress = exc.stderr
            if isinstance(raw_progress, bytes):
                raw_progress = raw_progress.decode("utf-8", errors="replace")
            progress = [line.strip() for line in str(raw_progress or "").splitlines()
                        if line.startswith("OLCR_MLX_PROGRESS=")]
            progress_summary = " | ".join(progress[-8:]) or "OLCR_MLX_PROGRESS=UNAVAILABLE"
            print(f"MLX_RUNTIME_TIMEOUT=YES OPERATION={operation} WORKER_PROGRESS={progress_summary}",
                  file=__import__("sys").stderr, flush=True)
            raise ModelFailure("timeout", f"dedicated MLX runtime timed out; {progress_summary}") from exc
        finally:
            if lease:
                lease.__exit__(*__import__('sys').exc_info())
            live_stop.set()
            if live_thread:live_thread.join(timeout=2)
            live_path.unlink(missing_ok=True)
            try:
                payload_path.unlink()
            except OSError:
                pass

    def _start_vision_worker(self, python: Path, worker_env: dict[str, str]) -> subprocess.Popen[str]:
        command = [str(python), str(self.repo_root / "backend" / "olcr_api" / "mlx_worker.py"),
                   "--serve-vision"]
        return subprocess.Popen(command, cwd=str(self.repo_root), env=worker_env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)

    def _run_mlx_vision(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Reuse one dedicated MLX process so perception does not reload weights per frame."""
        asset_id = str(payload.pop("runtime_asset_id", "") or "") or None
        manager, runtime_binding = self._runtime_binding(asset_id)
        python = (Path(runtime_binding.python_path) if runtime_binding else
                  Path(os.environ.get("OLCR_MLX_PYTHON") or self.repo_root / ".venv-mlx" / "bin" / "python"))
        if not python.is_file():
            raise ModelFailure("configuration", f"MLX runtime unavailable: {python}")
        worker_env = self._mlx_worker_environment()
        if runtime_binding:
            worker_env['OLCR_MLX_RUNTIME_PROFILE_ID'] = runtime_binding.profile_id
            worker_env['OLCR_MLX_RUNTIME_ID'] = runtime_binding.runtime_id
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            payload_path = Path(handle.name)
        try:
            with self._vision_worker_lock:
                worker = self._vision_worker
                runtime_id = (runtime_binding.runtime_id + ':' + str(python)) if runtime_binding else str(python)
                if worker is not None and self._vision_runtime_id != runtime_id:
                    self._stop_vision_worker_locked()
                    worker = None
                if worker is None or worker.poll() is not None:
                    if manager and runtime_binding:
                        self._vision_lease = manager.worker_lease(runtime_binding)
                        self._vision_lease.__enter__()
                    try:
                        worker = self._start_vision_worker(python, worker_env)
                    except BaseException:
                        lease, self._vision_lease = self._vision_lease, None
                        self._vision_runtime_id = None
                        if lease:
                            lease.__exit__(*__import__('sys').exc_info())
                        raise
                    self._vision_worker = worker
                    self._vision_runtime_id = runtime_id
                if worker.stdin is None or worker.stdout is None:
                    raise ModelFailure("mlx_runtime", "dedicated MLX vision worker pipes unavailable")
                try:
                    started = time.perf_counter()
                    worker.stdin.write(str(payload_path) + "\n")
                    worker.stdin.flush()
                    with selectors.DefaultSelector() as selector:
                        selector.register(worker.stdout, selectors.EVENT_READ)
                        readable = selector.select(model_timeout_seconds(750))
                    if not readable:
                        raise subprocess.TimeoutExpired(worker.args, 750)
                    raw = worker.stdout.readline()
                    if not raw:
                        raise ModelFailure("mlx_runtime", "dedicated MLX vision worker exited without a response")
                    value = json.loads(raw)
                except subprocess.TimeoutExpired as exc:
                    self._stop_vision_worker_locked()
                    raise ModelFailure("timeout", "dedicated MLX vision worker timed out") from exc
                except (OSError, ValueError) as exc:
                    self._stop_vision_worker_locked()
                    raise ModelFailure("mlx_response", "dedicated MLX vision worker returned invalid JSON") from exc
                if not isinstance(value, dict):
                    self._stop_vision_worker_locked()
                    raise ModelFailure("mlx_response", "dedicated MLX vision worker returned an invalid response")
                if value.get("error"):
                    raise ModelFailure("mlx_runtime", str(value["error"])[:2000])
                if not isinstance(value.get("text"), str):
                    raise ModelFailure("mlx_response", "dedicated MLX vision worker response omitted text")
                value["latency_ms"] = (time.perf_counter() - started) * 1000
                value["model_runtime"] = "dedicated_mlx_persistent"
                return value
        finally:
            try:
                payload_path.unlink()
            except OSError:
                pass

    def _stop_vision_worker_locked(self) -> None:
        worker, self._vision_worker = self._vision_worker, None
        lease, self._vision_lease = self._vision_lease, None
        self._vision_runtime_id = None
        if worker is None:
            if lease:
                lease.__exit__(None, None, None)
            return
        if worker.stdin:
            try:
                worker.stdin.close()
            except OSError:
                pass
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=3)
        for pipe in (worker.stdout, worker.stderr):
            if pipe:
                pipe.close()
        if lease:
            lease.__exit__(None, None, None)

    def close(self) -> None:
        """Stop the optional visual worker during backend shutdown."""
        self.release_gui_grounding_worker()

    def release_gui_grounding_worker(self) -> None:
        """Release UI-Venus weights after explicit screen-perception stop."""
        lock = getattr(self, "_vision_worker_lock", None)
        if lock is None:
            return
        with lock:
            self._stop_vision_worker_locked()

    @staticmethod
    def _runtime_kind(binding: ModelBinding) -> str:
        runtime = binding.runtime.lower()
        return "mlx" if "mlx" in runtime else "ollama" if "ollama" in runtime else runtime

    def context_budget(self, model: str) -> int:
        from .context_policy import context_budget
        binding = self._binding(model)
        selected = ('ollama:' + binding.deployment_model if binding.role == 'custom_ollama' else
                    self.brain_models.selected_model_for_role(binding.role) if self.brain_models is not None else "")
        return context_budget(self.settings, selected, model, binding.model_id, binding.deployment_model)

    def generate(self, messages: list[dict[str, str]], model: str, stream: bool = False,
                 think: bool | None = None, format: Any | None = None,
                 max_tokens: int | None = None, _fallback_allowed: bool = True,
                 thinking_text: str | None = None, force_no_thinking: bool = False,
                 _output_limit_retry_count: int = 0,
                 _recovery_enabled: bool = True) -> Any:
        check_cancelled()
        requested_think=think
        from .response_language import with_language_instruction
        messages = with_language_instruction(messages)
        source_messages = messages
        binding = self._binding(model)
        role = binding.role
        compatibility_chat = self.brain_models is not None and role == 'custom_ollama'
        # Stream iterators may be consumed after brain_scope has exited or on
        # another worker thread. The initial model and fallback sequence must
        # both come from the same invocation-owned configuration snapshot.
        from .brain_models import active_configuration, brain_scope
        request_configuration = json.loads(json.dumps(active_configuration(
            self.brain_models.configuration() if self.brain_models is not None else None)))
        original_selected_model = None
        resolution_failure = None
        if self.brain_models is not None:
            with brain_scope(request_configuration):
                original_selected_model = ('ollama:' + binding.deployment_model if compatibility_chat else
                                           self.brain_models.selected_model_for_role(role))
                state=self.brain_models.preflight(original_selected_model,capability_enabled(self.settings,'external_access_enabled'))
                if state['state']=='unavailable':
                    resolution_failure=ModelFailure('preflight_unavailable','Model unavailable before invocation')
                    resolution_failure.availability=state
                else:
                    try:
                        binding = self.brain_models.resolve(binding, self.registry)
                    except ModelFailure as failure:
                        resolution_failure = failure
        runtime = 'preflight_skipped' if resolution_failure and resolution_failure.category=='preflight_unavailable' else self._runtime_kind(binding)
        from .context_policy import fit_messages, context_budget
        budget = context_budget(self.settings, original_selected_model, model, binding.model_id, binding.deployment_model)
        request_text = next((str(item.get("content") or "").split("\n[TOOL_RESULT_UNTRUSTED]", 1)[0]
                             for item in reversed(messages) if item.get("role") == "user"), "")
        from .thinking import decide_thinking, configured_thinking_mode
        decision = decide_thinking(configured_thinking_mode(request_configuration or {}, role),
                                   thinking_text if thinking_text is not None else request_text,
                                   requested=think, structured=format is not None, role=role)
        if force_no_thinking:
            decision = {**decision, 'enabled': False, 'reason': 'internal_verification_contract'}
        if runtime in {'mlx', 'ollama'}:
            think = decision['enabled']
            if think and format is not None and isinstance(max_tokens,int):
                max_tokens=max(max_tokens,4096)
        else:
            decision = {'mode': 'provider', 'enabled': False if force_no_thinking else None,
                        'reason': 'internal_verification_contract' if force_no_thinking else 'provider_effort'}
        messages = fit_messages(messages, request_text, budget, self.settings.rag_min_relevance)
        context_report = None
        local_controls = None
        response_reserve = None
        local_effective_output_budget = None
        local_output_ceiling = None
        if runtime in {'mlx', 'ollama'}:
            local_controls = {key: getattr(self.settings, key) for key in
                              ('yarn_enabled', 'yarn_target_tokens', 'local_context_tokens', 'context_auto_compress')}
            window = self.settings.yarn_target_tokens if self.settings.yarn_enabled and runtime == 'mlx' else self.settings.local_context_tokens
            default_reserve = 8192 if inference_is_unbounded() else (2048 if runtime == 'ollama' else 4096)
            requested_reserve = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else default_reserve
            if self.settings.context_auto_compress:
                local_output_ceiling = max(1, window // 2)
                local_effective_output_budget = min(requested_reserve, local_output_ceiling)
                from .context_extension import compress_messages
                from .model_progress import model_progress
                # Leave prompt space even at the smallest supported window.
                # MLX later adjusts the response limit using its real tokenizer.
                response_reserve = local_effective_output_budget
                compression_started = False
                def report_compression_started():
                    nonlocal compression_started
                    compression_started = True
                    model_progress(state='context_compression', phase='started', method='extractive',
                                   window_tokens=window)
                try:
                    messages, context_report = compress_messages(
                        messages, window, response_reserve,
                        on_compression_start=report_compression_started)
                except Exception:
                    if compression_started:
                        model_progress(state='context_compression', phase='failed', method='extractive')
                    raise
                if compression_started:
                    model_progress(state='context_compression',
                                   phase='completed' if context_report['compressed'] else 'not_needed',
                                   method='extractive',
                                   estimated_tokens_before=context_report['estimated_tokens_before'],
                                   estimated_tokens_after=context_report['estimated_tokens_after'],
                                   window_tokens=window)
            elif isinstance(max_tokens, int) and max_tokens > 0:
                # Without automatic context management, the local runtime's
                # configured output request is already the safe ceiling OLCR
                # can prove; do not silently increase it on retry.
                local_output_ceiling = max_tokens
                local_effective_output_budget = max_tokens
        print(f"MODEL_ROLE_BINDING_REQUESTED={model} MODEL_ROLE_RESOLVED={role} "
              f"MODEL_DEPLOYMENT_EXPECTED={binding.deployment_model} MODEL_RUNTIME_EXPECTED={runtime}",
              file=__import__("sys").stderr, flush=True)
        model_telemetry.record(role=role, model=binding.deployment_model, runtime=runtime,
                               revision=binding.deployment_revision, stage="GENERATE")
        from .model_traffic_logs import begin_model_traffic, uses_internet
        traffic_call = begin_model_traffic(
            role=role, model=binding.deployment_model, runtime=runtime,
            request_body={"messages": messages, "stream": stream, "think": think,
                          "format": format, "max_tokens": max_tokens,
                          "effective_max_tokens": local_effective_output_budget if local_effective_output_budget is not None else max_tokens,
                          "context": local_controls, "context_compression": context_report,
                          "response_reserve": response_reserve},
            record=not binding.model_id.startswith("api:"),
            internet=(uses_internet(self.settings.ollama_endpoint)
                      if runtime == "ollama" else False))
        traffic_response = None
        from .model_progress import model_progress
        if resolution_failure and resolution_failure.category=='preflight_unavailable':
            state=resolution_failure.availability
            invocation_id=model_progress(state='model_skipped',role=role,model=original_selected_model,reason=state['reason'],retry_at=state.get('retry_at'))
        else:
            invocation_id=model_progress(state='request_started',role=role,model=binding.deployment_model,runtime=runtime)
        effective_output_budget = (local_effective_output_budget
            if local_effective_output_budget is not None else max_tokens)
        if (effective_output_budget is not None and not
                (resolution_failure and resolution_failure.category == 'preflight_unavailable')):
            model_progress(state='worker_progress',
                event='output_budget_resolved' if runtime in {'mlx', 'ollama'} else 'output_budget_requested',
                model=binding.deployment_model, runtime=runtime, requested_max_tokens=max_tokens,
                effective_max_tokens=effective_output_budget,
                reason=('local context window caps the requested per-call output budget'
                        if max_tokens is not None and effective_output_budget < max_tokens
                        else 'per-call output budget prepared before provider invocation; remote provider cap is unconfirmed'))
        coding_plan = inference_is_unbounded() and isinstance(format, dict) and 'phases' in format.get('properties', {})
        coding_operations = inference_is_unbounded() and isinstance(format, dict) and bool(
            {'operations','edits'} & format.get('properties', {}).keys())

        def try_fallback(failure: ModelFailure) -> Any:
            traffic_call.finish(status="error", response_body=traffic_response,
                                error_code=failure.category)
            from .model_progress import model_progress
            if failure.category!='preflight_unavailable':model_progress(state='request_failed',role=role,model=binding.deployment_model,runtime=runtime,failure=failure.category)
            check_cancelled()
            if self.brain_models is not None:self.brain_models.note_local_result(original_selected_model,failure)
            if not _fallback_allowed or self.brain_models is None:
                raise failure
            from .brain_models import active_configuration, brain_scope, ROLE_SLOTS
            config = request_configuration or active_configuration(self.brain_models.configuration())
            slot = ROLE_SLOTS.get('normal_chat' if compatibility_chat else role)
            # Start at the active role selection, then visit each configured
            # fallback once, preserving the user's order. A model selected
            # for another role is still a valid fallback for this invocation.
            choices = list(dict.fromkeys(candidate for candidate in
                (config.get('fallback_models') or []) if candidate != original_selected_model))
            if not choices or slot is None:
                raise failure
            errors = [failure]
            previous_failure=failure
            previous_model=original_selected_model or binding.model_id
            for candidate in choices:
                model_progress(state='fallback',model=candidate,previous_model=previous_model,
                    reason=getattr(previous_failure,'availability',{}).get('reason',
                        getattr(previous_failure,'fallback_reason',previous_failure.category)))
                attempted = dict(config)
                attempted['selections'] = dict(config.get('selections') or {})
                target_slot = 'brain' if config.get('mode') == 'unified' else slot
                attempted['selections'][target_slot] = candidate
                attempted['efforts'] = dict(config.get('efforts') or {})
                from .thinking import configured_thinking_mode
                attempted['efforts'][target_slot] = (configured_thinking_mode(config, role)
                    if candidate.startswith(('mlx:', 'ollama:')) else None)
                try:
                    with brain_scope(attempted):
                        fallback_result = self.generate(source_messages, 'role:normal_chat' if compatibility_chat else model,
                                                        stream, requested_think, format, max_tokens, False,
                                                        thinking_text=thinking_text,
                                                        force_no_thinking=force_no_thinking)
                    if isinstance(fallback_result, dict):
                        fallback_result['fallback_from'] = original_selected_model or binding.model_id
                        fallback_result['fallback_model_id'] = candidate
                        if not str(fallback_result.get('text') or '').strip():
                            raise ModelFailure('invalid_response', 'Fallback model returned no text')
                        return fallback_result
                    fallback_frames = list(fallback_result)
                    if not any(isinstance(frame,dict) and str(frame.get('text') or '').strip() for frame in fallback_frames):
                        raise ModelFailure('invalid_response', 'Fallback model returned no text')
                    first = next((frame for frame in fallback_frames if isinstance(frame,dict)), None)
                    if first is not None:
                        first['fallback_from'] = original_selected_model or binding.model_id
                        first['fallback_model_id'] = candidate
                    return iter(fallback_frames)
                except ModelFailure as next_failure:
                    errors.append(next_failure)
                    previous_failure=next_failure
                    previous_model=candidate
            detail = str(errors[-1]) if errors[-1].category == 'model_concurrency_limit' else 'All configured fallback models are unavailable'
            raise ModelFailure(errors[-1].category, detail) from errors[-1]

        def can_reduce_reasoning(failure: ModelFailure) -> tuple[bool, bool]:
            if (force_no_thinking or stream or format is None
                    or failure.category not in {'reasoning_budget', 'output_limit'}):
                return False, False
            local_retry = runtime in {'mlx', 'ollama'} and think is True
            api_retry = False
            if binding.model_id.startswith('api:') and self.brain_models is not None:
                _, provider_name, selected_model = binding.model_id.split(':', 2)
                metadata = self.brain_models.providers.model_metadata(provider_name, selected_model)
                from .brain_providers import reasoning_profile
                profile = reasoning_profile(provider_name, selected_model, (metadata or {}).get('base_url'))
                api_retry = any(level in {'off', 'none', 'minimal', 'low'}
                                for level in profile.get('effort_levels', []))
            return local_retry or api_retry, api_retry

        def same_model_attempt(*, budget: int | None, reduced_reasoning: bool) -> dict[str, Any]:
            retry_think = False if reduced_reasoning else requested_think
            with brain_scope(request_configuration):
                retried = self.generate(
                    source_messages, model, False, retry_think, format, budget, False,
                    thinking_text=thinking_text,
                    force_no_thinking=force_no_thinking or reduced_reasoning,
                    _output_limit_retry_count=_output_limit_retry_count,
                    _recovery_enabled=False,
                )
            if not isinstance(retried, dict) or not str(retried.get('text') or '').strip():
                raise ModelFailure('invalid_response', 'Same-model retry returned no text')
            return retried

        def next_output_budget(current_budget: int | None) -> int | None:
            if runtime in {'mlx', 'ollama'} and local_output_ceiling is None:
                return None
            ceiling = min(MAX_OUTPUT_RETRY_TOKENS, local_output_ceiling) if local_output_ceiling else MAX_OUTPUT_RETRY_TOKENS
            if current_budget is not None and current_budget >= ceiling:
                return None
            retry_budget = min(ceiling, max(MIN_OUTPUT_RETRY_TOKENS, current_budget * 2)) if current_budget else ceiling
            return retry_budget if current_budget is None or retry_budget > current_budget else None

        def recover_failure(failure: ModelFailure) -> Any:
            """Run bounded same-model recovery before considering provider fallback.

            Recovery attempts disable nested recovery so each output-limit and
            reasoning failure is handled once by this state machine.
            """
            if not _recovery_enabled:
                if failure.category == 'output_limit':
                    raise failure
                return try_fallback(failure)

            current_failure = failure
            reduced_reasoning = force_no_thinking
            can_retry_reasoning, api_reasoning_retry = can_reduce_reasoning(current_failure)
            if can_retry_reasoning:
                traffic_call.finish(status="error", response_body=traffic_response,
                                    error_code=current_failure.category)
                model_progress(state='worker_progress', event='structured_no_thinking_retry',
                    model=binding.deployment_model, runtime=runtime, failure=current_failure.category,
                    reason='retrying structured output on the same model with reduced reasoning effort')
                print(f"STRUCTURED_OUTPUT_NO_THINK_RETRY=YES MODEL={binding.deployment_model} "
                      f"FAILURE={current_failure.category}", file=__import__('sys').stderr, flush=True)
                try:
                    retried = same_model_attempt(budget=max_tokens, reduced_reasoning=True)
                except ModelFailure as retry_failure:
                    if (api_reasoning_retry and retry_failure.category in {'provider_rejected', 'configuration'}):
                        # The endpoint declined the optional effort override;
                        # keep the original error and continue its normal path.
                        pass
                    elif retry_failure.category == 'output_limit':
                        current_failure = retry_failure
                        reduced_reasoning = True
                    else:
                        return try_fallback(retry_failure)
                else:
                    retried['structured_no_thinking_retry'] = {'after_failure': current_failure.category}
                    return retried

            if current_failure.category == 'output_limit' and not stream:
                current_budget = max_tokens if type(max_tokens) is int and max_tokens > 0 else None
                if local_effective_output_budget is not None:
                    current_budget = local_effective_output_budget
                retries_used = max(0, int(_output_limit_retry_count or 0))
                while retries_used < MAX_OUTPUT_BUDGET_RETRIES:
                    retry_budget = next_output_budget(current_budget)
                    if retry_budget is None:
                        break
                    attempt = retries_used + 1
                    model_progress(state='worker_progress', event='output_budget_retry',
                        model=original_selected_model or binding.deployment_model, runtime=runtime,
                        previous_max_tokens=current_budget, retry_max_tokens=retry_budget,
                        attempt=attempt, max_attempts=MAX_OUTPUT_BUDGET_RETRIES,
                        reason='retrying truncated response on the same selected model with a larger per-request output budget')
                    print(f"OUTPUT_BUDGET_RETRY=YES MODEL={original_selected_model or binding.deployment_model} "
                          f"PREVIOUS_MAX_TOKENS={current_budget or 'provider_default'} RETRY_MAX_TOKENS={retry_budget} "
                          f"ATTEMPT={attempt}/{MAX_OUTPUT_BUDGET_RETRIES}", file=__import__('sys').stderr, flush=True)
                    try:
                        retried = same_model_attempt(budget=retry_budget, reduced_reasoning=reduced_reasoning)
                    except ModelFailure as retry_failure:
                        if retry_failure.category == 'output_limit':
                            current_failure = retry_failure
                            current_budget = retry_budget
                            retries_used += 1
                            continue
                        return try_fallback(retry_failure)
                    retried['output_budget_retry'] = {
                        'previous_max_tokens': current_budget,
                        'effective_max_tokens': retry_budget,
                        'attempt': attempt,
                    }
                    return retried
            if current_failure.category == 'output_limit':
                try:
                    return try_fallback(current_failure)
                except ModelFailure as fallback_failure:
                    if fallback_failure is current_failure:
                        raise
                    raise current_failure from fallback_failure
            return try_fallback(current_failure)

        try:
          if resolution_failure is not None:
            raise resolution_failure
          if runtime == 'ollama' and self.settings.yarn_enabled:
            raise ModelFailure('yarn_unsupported', 'YaRN requires an MLX text model. Ollama does not expose runtime YaRN controls; select MLX or disable YaRN.')
          if binding.model_id == "ternary_bonsai_2_27b":
            role_budget = (PLANNER_REPLANNER_MAX_TOKENS if role == "planner_replanner"
                           else SHORT_CODING_FIX_MAX_TOKENS)
            if coding_plan:
                role_budget = max(role_budget, 8192)
            if think and format is not None:
                role_budget=max(role_budget,4096)
            if coding_operations and isinstance(max_tokens,int) and max_tokens>0:
                # Runtime sized this complete atomic source operation batch.
                # The short-role default must not silently truncate its JSON
                # below that request. Keep the same finite implementation cap.
                role_budget = max(role_budget,min(16384,max_tokens))
            token_budget = min(role_budget, max_tokens) if isinstance(max_tokens, int) and max_tokens > 0 else role_budget
            result = self._run_mlx("bonsai", {"runtime_asset_id": "ternary_bonsai_2_27b",
                                              "messages": messages, "max_tokens": token_budget, "format": format, "context": local_controls, "think": think,
                                              "bounded_reasoning": inference_is_unbounded() and isinstance(format,dict)})
          elif binding.model_id == "mlx:ornith_1_5_9b_8bit":
            token_budget = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else (8192 if coding_plan else 2048)
            result = self._run_mlx("ornith", {"runtime_asset_id": "ornith_1_5_9b_8bit",
                                               "messages": messages, "max_tokens": token_budget,
                                               "think": think, "format": format, "context": local_controls,
                                               "bounded_reasoning": inference_is_unbounded() and isinstance(format,dict)})
          elif binding.model_id.removeprefix('mlx:').startswith('ternary_bonsai_2_27b_') and runtime == 'mlx':
            role_budget = (PLANNER_REPLANNER_MAX_TOKENS if role == 'planner_replanner'
                           else SHORT_CODING_FIX_MAX_TOKENS)
            if coding_plan:
                role_budget = max(role_budget, 8192)
            if think and format is not None:
                role_budget = max(role_budget, 4096)
            if coding_operations and isinstance(max_tokens, int) and max_tokens > 0:
                role_budget = max(role_budget, min(16384, max_tokens))
            token_budget = min(role_budget, max_tokens) if isinstance(max_tokens, int) and max_tokens > 0 else role_budget
            result = self._run_mlx('bonsai_vlm', {'runtime_asset_id': self._runtime_asset(binding),
                'model_path': binding.local_path,
                'model_id': binding.model_id, 'messages': messages, 'max_tokens': token_budget,
                'think': think, 'format': format, 'context': local_controls})
          elif runtime == "ollama":
            if context_report is not None and context_report['over_budget']:
                raise ModelFailure('context_window_exceeded',
                    f"CONTEXT_WINDOW_EXCEEDED: estimated_prompt={context_report['estimated_tokens_after']}, "
                    f"response={response_reserve}, window={self.settings.local_context_tokens}. "
                    "Current input and required instructions were preserved; increase the context window or shorten the input.")
            ollama_options = ({'options': {'num_ctx': self.settings.local_context_tokens, 'num_predict': response_reserve}}
                              if self.settings.context_auto_compress else {})
            if not self.settings.context_auto_compress and isinstance(max_tokens, int) and max_tokens > 0:
                ollama_options = {'options': {'num_predict': max_tokens}}
            result = self.ollama.generate(messages, binding.deployment_model, stream=stream, think=think, format=format, **ollama_options)
          elif runtime == 'mlx':
            token_budget = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else (8192 if coding_plan else 2048)
            result = self._run_mlx('generic', {'runtime_asset_id': self._runtime_asset(binding),
                'model_path': binding.local_path,
                'model_id': binding.model_id, 'messages': messages, 'max_tokens': token_budget,
                'think': think, 'format': format, 'context': local_controls})
          elif self.brain_models is not None and binding.model_id.startswith("api:"):
            _, provider, selected_model = binding.model_id.split(":", 2)
            effort = self.brain_models.effort_for_role(role)
            allow_unadvertised_effort = False
            from .brain_providers import reasoning_profile
            metadata=self.brain_models.providers.model_metadata(provider,selected_model)
            profile = reasoning_profile(provider, selected_model, (metadata or {}).get('base_url'))
            if force_no_thinking:
                effort = next((level for level in ('off', 'none', 'minimal', 'low')
                               if level in profile.get('effort_levels', [])), None)
            if profile.get('effort_transport') in {'google_thinking_toggle','anthropic_thinking_toggle'}:
                decision=decide_thinking(effort or 'auto',thinking_text if thinking_text is not None else request_text,role=role)
                effort='on' if decision['enabled'] else 'off'
            result = self.brain_models.providers.generate(provider, selected_model, messages, format,
                                                          capability_enabled(self.settings, "external_access_enabled"), effort=effort,
                                                          role=role,
                                                          allow_unadvertised_effort=allow_unadvertised_effort,
                                                          **({'max_tokens':max_tokens} if type(max_tokens) is int and max_tokens>0 else {}),
                                                          **({"quick":True} if self.brain_models.quick_for_model(binding.model_id) else {}))
            from .model_progress import current_model_invocation
            invocation_id=current_model_invocation() or invocation_id
          else:
            raise ModelFailure("configuration", f"unsupported generation runtime for role {role}: {binding.runtime}")
        except ModelFailure as failure:
            traffic_call.finish(status="error", response_body=traffic_response,
                                error_code=failure.category)
            return recover_failure(failure)
        except (OSError, TimeoutError, ValueError) as failure:
            traffic_call.finish(status="error", response_body=traffic_response,
                                error_code=type(failure).__name__)
            return try_fallback(ModelFailure('provider_unavailable', 'Selected model did not return a complete response'))
        except BaseException as failure:
            traffic_call.finish(status="cancelled" if isinstance(failure, (GeneratorExit, GenerationCancelled)) else "error",
                                response_body=traffic_response,
                                error_code=getattr(failure, "category", type(failure).__name__))
            raise
        traffic_response = result
        def annotate(value: dict[str, Any]) -> dict[str, Any]:
            actual_model = str(value.get("model") or binding.deployment_model)
            if actual_model != binding.deployment_model:
                print(f"MODEL_ROLE_BINDING_MISMATCH=YES MODEL_ROLE_RESOLVED={role} "
                      f"MODEL_DEPLOYMENT_EXPECTED={binding.deployment_model} MODEL_DEPLOYMENT_ACTUAL={actual_model}",
                      file=__import__("sys").stderr, flush=True)
                raise ModelFailure("model_mismatch",
                                   f"Model role {role} resolved to {binding.deployment_model} "
                                   f"but provider returned {actual_model}")
            if context_report is not None:
                value["context_compression"] = context_report
            if effective_output_budget is not None:
                value['output_budget'] = {
                    'requested_max_tokens': max_tokens,
                    'effective_max_tokens': effective_output_budget,
                    'context_limited': bool(max_tokens is not None and effective_output_budget < max_tokens),
                }
            value["model"] = actual_model
            value["model_role"] = role
            value["model_revision"] = binding.deployment_revision
            value["model_provenance"] = binding.public_dict()
            effective_thinking=value.get('thinking_decision')
            value['thinking_decision'] = ({**decision,**{key:effective_thinking[key]
                for key in ('enabled','reason') if key in effective_thinking}}
                if runtime not in {'mlx','ollama'} and isinstance(effective_thinking,dict) else decision)
            raw=value.get('reasoning')
            if isinstance(raw,dict) and raw.get('text') and not (stream and runtime=='ollama'):
                from .model_progress import model_progress
                model_progress(invocation_id=invocation_id,state='reasoning',text=raw['text'],kind=raw.get('kind','trace'),complete=raw.get('complete',True),model=actual_model,snapshot=True)
            if not stream or value.get('done'):
                if self.brain_models is not None:self.brain_models.note_local_result(original_selected_model)
                from .model_progress import model_progress
                model_progress(invocation_id=invocation_id,state='request_completed',model=actual_model,runtime=runtime)
            return value
        if stream:
            # The dedicated Bonsai runtime is request-scoped and returns a
            # completed response.  Ollama streams are wrapped so every frame
            # still carries actual model provenance at the invocation boundary.
            if isinstance(result, dict):
                if not str(result.get('text') or '').strip():
                    return try_fallback(ModelFailure('invalid_response', 'Selected model returned no text'))
                try:
                    annotated = annotate(result)
                    traffic_call.finish(status="success", response_body=result)
                    return iter([annotated])
                except ModelFailure as failure:
                    return try_fallback(failure)
            try:
                iterator = iter(result) if not isinstance(result, (str, bytes)) else None
            except TypeError:
                iterator = None
            if iterator is None:
                return try_fallback(ModelFailure('invalid_response', 'Selected model returned an invalid stream'))
            def frames():
                saw_text = False
                traffic_frames = []
                finished = False
                # A failed attempt must not leak partial text into the next
                # model's answer. Preserve live streaming when no fallback is set.
                buffered = []
                buffer_attempt = bool(_fallback_allowed and request_configuration
                                      and request_configuration.get('fallback_models'))
                try:
                    for frame in iterator:
                        traffic_frames.append(frame)
                        if isinstance(frame, dict):
                            frame = annotate(frame)
                            saw_text = saw_text or bool(str(frame.get('text') or '').strip())
                        if buffer_attempt:
                            buffered.append(frame)
                        else:
                            yield frame
                    if not saw_text:
                        raise ModelFailure('invalid_response', 'Selected model returned no text')
                    traffic_call.finish(status="success", response_body=traffic_frames)
                    finished = True
                    yield from buffered
                except ModelFailure as failure:
                    traffic_call.finish(status="error", response_body=traffic_frames,
                                        error_code=failure.category)
                    finished = True
                    if saw_text and not buffer_attempt:
                        raise
                    yield from try_fallback(failure)
                except (OSError, TimeoutError, ValueError) as error:
                    failure = ModelFailure('provider_unavailable', 'Selected model did not return a complete response')
                    traffic_call.finish(status="error", response_body=traffic_frames,
                                        error_code=type(error).__name__)
                    finished = True
                    if saw_text and not buffer_attempt:
                        raise failure from error
                    yield from try_fallback(failure)
                except GeneratorExit:
                    traffic_call.finish(status="cancelled", response_body=traffic_frames,
                                        error_code="stream_cancelled")
                    finished = True
                    raise
                except BaseException as error:
                    traffic_call.finish(status="cancelled" if isinstance(error, GenerationCancelled) else "error",
                                        response_body=traffic_frames,
                                        error_code=type(error).__name__)
                    finished = True
                    raise
                finally:
                    if not finished:
                        traffic_call.finish(status="cancelled", response_body=traffic_frames,
                                            error_code="stream_cancelled")
                    if hasattr(result, 'close'):
                        result.close()
            return frames()
        if isinstance(result, dict):
            if not str(result.get('text') or '').strip():
                if result.get('finish_reason') in {'length','max_tokens'}:
                    failure = ModelFailure('output_limit',
                        'Model exhausted its output budget before returning the required answer or JSON')
                    traffic_call.finish(status="error", response_body=result,
                                        error_code=failure.category)
                    return recover_failure(failure)
                return try_fallback(ModelFailure('invalid_response', 'Selected model returned no text'))
            if (format is not None and result.get('finish_reason') in {'length', 'max_tokens'}
                    and runtime in {'mlx', 'ollama'}):
                try:
                    json.loads(str(result.get('text') or ''))
                except (TypeError, ValueError):
                    failure = ModelFailure('output_limit',
                        'Model exhausted its output budget before completing structured JSON')
                    traffic_call.finish(status="error", response_body=result,
                                        error_code=failure.category)
                    return recover_failure(failure)
            try:
                annotated = annotate(result)
                traffic_call.finish(status="success", response_body=result)
                return annotated
            except ModelFailure as failure:
                return try_fallback(failure)
        return try_fallback(ModelFailure('invalid_response', 'Selected model returned an invalid response'))

    def runtime_observation(self, model: str) -> dict[str, Any]:
        binding = self._binding(model)
        choices = getattr(self, 'purpose_models', None)
        if choices and binding.role in {'general_image', 'gui_grounding'}:
            choice = choices.choice(binding.role)
            if choice['protocol'] != 'preset':
                return {'status': 'CONFIGURED', 'target_model': choice['model'], 'model_runtime': choice['protocol'],
                        'loaded_models': [], 'loaded_state': 'UNKNOWN', 'model_provenance': {'role': binding.role, 'model_id': choice['model'], 'runtime': choice['protocol']}}
        if self.brain_models is not None:
            binding = self.brain_models.resolve(binding, self.registry)
        if self._runtime_kind(binding) == "ollama":
            observation = self.ollama.runtime_observation(binding.deployment_model)
        elif self._runtime_kind(binding) == "mlx":
            observation = {"status": "OK", "target_model": binding.model_id,
                           "model_runtime": "dedicated_mlx", "loaded_models": []}
        else:
            observation = {"status": "CONFIGURED" if binding.ready else "CREDENTIAL_REQUIRED",
                           "target_model": binding.deployment_model, "model_runtime": binding.runtime,
                           "loaded_models": [], "loaded_state": "UNKNOWN"}
        observation["model_provenance"] = binding.public_dict()
        return observation

    def vision(self, image_bytes: bytes, mime_type: str, prompt: str,
               model: str = "role:gui_grounding") -> Any:
        binding = self._binding(model, vision=True)
        choices = getattr(self, 'purpose_models', None)
        if choices and binding.role in {'general_image', 'gui_grounding'}:
            config = choices.choice(binding.role)
            if config['protocol'] != 'preset':
                try:
                    result = choices.vision(binding.role, image_bytes, mime_type, prompt, config)
                    model_telemetry.record(role=binding.role, model=config['model'], runtime=config['protocol'], revision='USER_CONFIGURED', stage='VISION')
                    result['model_provenance'] = {'role': binding.role, 'model_id': config['model'], 'runtime': config['protocol'], 'deployment_repo': config.get('base_url', config.get('provider')), 'ready': True}
                    return result
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    raise ModelFailure('invalid_response', 'Selected Vision model returned an invalid response') from exc
        runtime = self._runtime_kind(binding)
        from .model_traffic_logs import begin_model_traffic, uses_internet
        traffic_call = begin_model_traffic(
            role=binding.role, model=binding.deployment_model, runtime=runtime,
            request_body={"image": image_bytes, "mime_type": mime_type, "prompt": prompt},
            internet=(uses_internet(self.settings.ollama_endpoint)
                      if runtime == "ollama" else False))
        model_telemetry.record(role=binding.role, model=binding.deployment_model, runtime=runtime,
                               revision=binding.deployment_revision, stage="VISION")
        try:
            if runtime == "mlx":
                suffix = ".jpg" if mime_type in {"image/jpeg", "image/jpg"} else ".png"
                with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as handle:
                    handle.write(image_bytes)
                    image_path = Path(handle.name)
                try:
                    vision_model_id = binding.model_id.removeprefix('mlx:')
                    vision_request = {"runtime_asset_id": vision_model_id, "model": vision_model_id, "image": str(image_path),
                                      "prompt": prompt, "max_tokens": 512}
                    result = (self._run_mlx_vision(vision_request) if binding.role == "gui_grounding"
                              else self._run_mlx("vision", vision_request))
                finally:
                    try:
                        image_path.unlink()
                    except OSError:
                        pass
            else:
                result = self.ollama.vision(image_bytes, mime_type, prompt, binding.deployment_model)
            if isinstance(result, dict):
                result["model"] = binding.deployment_model
                result["model_role"] = binding.role
                result["model_revision"] = binding.deployment_revision
                result["model_provenance"] = binding.public_dict()
            traffic_call.finish(status="success", response_body=result)
            return result
        except BaseException as failure:
            traffic_call.finish(status="cancelled" if isinstance(failure, GenerationCancelled) else "error",
                                error_code=getattr(failure, "category", type(failure).__name__))
            raise


def validate_runtime_candidate(settings: Settings, entry: dict[str, Any],
                               model_path: Path, runtime_binding) -> bool:
    """Boundedly load a downloaded artifact before switching its runtime binding."""
    import base64
    import struct

    from .model_runtime_manager import get_runtime_manager
    from .model_runtime_profiles import asset_identity_for_entry, runtime_profile_for_asset

    manager = get_runtime_manager()
    if manager is None or not runtime_binding.python_path:
        return False
    identity = asset_identity_for_entry(entry)
    profile = runtime_profile_for_asset(identity.asset_id, identity.revision)
    if not Path(model_path).is_dir():
        return False
    if profile.loader_id == 'plamo-translate-cli':
        try:
            from .plamo_translation import translate
            with manager.worker_lease(runtime_binding):
                return bool(translate('Hello', 'en', 'ja', Path(model_path), timeout=240,
                                      python_path=Path(runtime_binding.python_path)).strip())
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return False

    worker_env = RoleAwareModelProvider._mlx_worker_environment()
    worker_env.update(
        OLCR_MODEL_ASSETS_DIR=str(Path(settings.db_path).expanduser().parent / 'models'),
        OLCR_MLX_RUNTIME_PROFILE_ID=runtime_binding.profile_id,
        OLCR_MLX_RUNTIME_ID=runtime_binding.runtime_id,
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
    )
    if profile.loader_id in {'mlx-audio', 'mlx-whisper'}:
        request_payload = {
            'operation': 'transcribe_whisper' if profile.loader_id == 'mlx-whisper' else 'transcribe',
            'language': 'ja',
            'path': str(model_path),
            'model': identity.asset_id,
            'sample_rate': 16000,
            'pcm': base64.b64encode(struct.pack('<' + 'f' * 8192, *([0.0] * 8192))).decode('ascii'),
        }
        if profile.loader_id == 'mlx-whisper':
            request_payload['language'] = 'en'
        worker = Path(__file__).with_name('audio_mlx_worker.py')
        try:
            with manager.worker_lease(runtime_binding):
                completed = subprocess.run(
                    [runtime_binding.python_path, str(worker)], input=json.dumps(request_payload) + '\n',
                    cwd=str(Path(__file__).resolve().parents[2]), capture_output=True,
                    text=True, timeout=300, check=False, env=worker_env)
            if completed.returncode:
                return False
            events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            return any(event.get('type') == 'done' for event in events)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    worker = Path(__file__).with_name('mlx_worker.py')
    payload: dict[str, Any] = {
        'loader_id': profile.loader_id,
        'asset_id': identity.asset_id,
        'model_path': str(model_path),
    }
    if profile.loader_id == 'mlx-vlm' and not identity.asset_id.startswith('ternary_bonsai_2_27b_'):
        try:
            from .purpose_models import vision_probe_image
            with tempfile.NamedTemporaryFile('wb', suffix='.png', delete=False) as image_file:
                image_file.write(vision_probe_image())
                image_path = Path(image_file.name)
            payload['image'] = str(image_path)
        except (ImportError, OSError):
            return False
    else:
        image_path = None
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.json', encoding='utf-8', delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False)
            payload_file = Path(handle.name)
        try:
            with manager.worker_lease(runtime_binding):
                completed = subprocess.run(
                    [runtime_binding.python_path, str(worker), '--operation', 'validate',
                     '--payload', str(payload_file)], cwd=str(Path(__file__).resolve().parents[2]),
                    capture_output=True, text=True, timeout=300, check=False, env=worker_env)
            if completed.returncode:
                return False
            value = json.loads(completed.stdout.strip().splitlines()[-1])
            return value.get('valid') is True
        finally:
            payload_file.unlink(missing_ok=True)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False
    finally:
        if image_path is not None:
            image_path.unlink(missing_ok=True)
