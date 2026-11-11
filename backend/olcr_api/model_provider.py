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
MAX_STRUCTURED_OUTPUT_RETRY_TOKENS = 32768


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

    def _mlx_python(self) -> Path:
        value = os.environ.get("OLCR_MLX_PYTHON")
        python = Path(value).expanduser() if value else self.repo_root / ".venv-mlx" / "bin" / "python"
        if not python.is_file():
            raise ModelFailure("configuration", f"MLX runtime unavailable: {python}")
        return python

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
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            payload_path = Path(handle.name)
        live_path=payload_path.with_suffix('.live.jsonl')
        live_stop=threading.Event();live_thread=None
        try:
            command = [str(self._mlx_python()), str(self.repo_root / "backend" / "olcr_api" / "mlx_worker.py"),
                       "--operation", operation, "--payload", str(payload_path)]
            started = time.perf_counter()
            worker_env = self._mlx_worker_environment()
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
            live_stop.set()
            if live_thread:live_thread.join(timeout=2)
            live_path.unlink(missing_ok=True)
            try:
                payload_path.unlink()
            except OSError:
                pass

    def _start_vision_worker(self) -> subprocess.Popen[str]:
        command = [str(self._mlx_python()), str(self.repo_root / "backend" / "olcr_api" / "mlx_worker.py"),
                   "--serve-vision"]
        return subprocess.Popen(command, cwd=str(self.repo_root), env=self._mlx_worker_environment(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)

    def _run_mlx_vision(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Reuse one dedicated MLX process so perception does not reload weights per frame."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            payload_path = Path(handle.name)
        try:
            with self._vision_worker_lock:
                worker = self._vision_worker
                if worker is None or worker.poll() is not None:
                    worker = self._start_vision_worker()
                    self._vision_worker = worker
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
        if worker is None:
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
                 _output_limit_retry: bool = False) -> Any:
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
        if runtime in {'mlx', 'ollama'}:
            local_controls = {key: getattr(self.settings, key) for key in
                              ('yarn_enabled', 'yarn_target_tokens', 'local_context_tokens', 'context_auto_compress')}
            if self.settings.context_auto_compress:
                from .context_extension import compress_messages
                window = self.settings.yarn_target_tokens if self.settings.yarn_enabled and runtime == 'mlx' else self.settings.local_context_tokens
                default_reserve = 8192 if inference_is_unbounded() else (2048 if runtime == 'ollama' else 4096)
                requested_reserve = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else default_reserve
                # Leave prompt space even at the smallest supported window.
                # MLX later adjusts the response limit using its real tokenizer.
                response_reserve = min(requested_reserve, window // 2)
                messages, context_report = compress_messages(messages, window, response_reserve)
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

        def retry_structured_without_thinking(failure: ModelFailure) -> Any | None:
            if (force_no_thinking or stream or format is None or think is not True
                    or runtime not in {'mlx', 'ollama'}
                    or failure.category not in {'reasoning_budget', 'output_limit'}):
                return None
            traffic_call.finish(status="error", response_body=traffic_response,
                                error_code=failure.category)
            model_progress(state='worker_progress', event='structured_no_thinking_retry',
                           model=binding.deployment_model, failure=failure.category,
                           reason='retrying structured output on the same local model with thinking disabled')
            print(f"STRUCTURED_OUTPUT_NO_THINK_RETRY=YES MODEL={binding.deployment_model} "
                  f"FAILURE={failure.category}", file=__import__('sys').stderr, flush=True)
            try:
                retried = self.generate(source_messages, model, False, False, format, max_tokens, False,
                                        thinking_text=thinking_text, force_no_thinking=True)
            except ModelFailure as retry_failure:
                if retry_failure.category in {'reasoning_budget', 'output_limit'}:
                    raise
                return try_fallback(retry_failure)
            if isinstance(retried, dict) and str(retried.get('text') or '').strip():
                retried['structured_no_thinking_retry'] = {'after_failure': failure.category}
                return retried
            return try_fallback(ModelFailure('invalid_response',
                'Same-model no-thinking retry returned no structured output'))

        def retry_structured_with_larger_output_budget(failure: ModelFailure) -> Any | None:
            if (_output_limit_retry or stream or format is None
                    or failure.category != 'output_limit'
                    or runtime in {'mlx', 'ollama'}
                    or type(max_tokens) is not int or max_tokens <= 0
                    or max_tokens >= MAX_STRUCTURED_OUTPUT_RETRY_TOKENS):
                return None
            retry_budget=min(MAX_STRUCTURED_OUTPUT_RETRY_TOKENS,max_tokens*2)
            if retry_budget <= max_tokens:
                return None
            model_progress(state='worker_progress',event='structured_output_budget_retry',
                model=original_selected_model or binding.deployment_model,
                previous_max_tokens=max_tokens,retry_max_tokens=retry_budget,
                reason='retrying truncated structured output on the same selected model')
            print(f"STRUCTURED_OUTPUT_BUDGET_RETRY=YES MODEL={original_selected_model or binding.deployment_model} "
                  f"PREVIOUS_MAX_TOKENS={max_tokens} RETRY_MAX_TOKENS={retry_budget}",
                  file=__import__('sys').stderr,flush=True)
            try:
                retried=self.generate(source_messages,model,False,requested_think,format,retry_budget,False,
                    thinking_text=thinking_text,force_no_thinking=force_no_thinking,
                    _output_limit_retry=True)
            except ModelFailure as retry_failure:
                # A second truncation means the selected endpoint's structured
                # response budget is exhausted. Don't fan the same request out
                # to unrelated providers; only fail over if the retry reveals
                # a genuine connection/availability failure.
                if retry_failure.category=='output_limit':
                    raise
                return try_fallback(retry_failure)
            if isinstance(retried,dict) and str(retried.get('text') or '').strip():
                return retried
            raise ModelFailure('invalid_response','Same-model larger-budget retry returned no structured output')

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
            result = self._run_mlx("bonsai", {"messages": messages, "max_tokens": token_budget, "format": format, "context": local_controls, "think": think,
                                              "bounded_reasoning": inference_is_unbounded() and isinstance(format,dict)})
          elif binding.model_id == "mlx:ornith_1_5_9b_8bit":
            token_budget = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else (8192 if coding_plan else 2048)
            result = self._run_mlx("ornith", {"messages": messages, "max_tokens": token_budget,
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
            result = self._run_mlx('bonsai_vlm', {'model_path': binding.local_path,
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
            result = self._run_mlx('generic', {'model_path': binding.local_path,
                'model_id': binding.model_id, 'messages': messages, 'max_tokens': token_budget,
                'think': think, 'format': format, 'context': local_controls})
          elif self.brain_models is not None and binding.model_id.startswith("api:"):
            _, provider, selected_model = binding.model_id.split(":", 2)
            effort = self.brain_models.effort_for_role(role)
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
                                                          **({'max_tokens':max_tokens} if type(max_tokens) is int and max_tokens>0 else {}),
                                                          **({"quick":True} if self.brain_models.quick_for_model(binding.model_id) else {}))
            from .model_progress import current_model_invocation
            invocation_id=current_model_invocation() or invocation_id
          else:
            raise ModelFailure("configuration", f"unsupported generation runtime for role {role}: {binding.runtime}")
        except ModelFailure as failure:
            traffic_call.finish(status="error", response_body=traffic_response,
                                error_code=failure.category)
            retried = retry_structured_without_thinking(failure)
            if retried is not None:
                return retried
            retried=retry_structured_with_larger_output_budget(failure)
            if retried is not None:
                return retried
            if failure.category=='output_limit':
                raise
            return try_fallback(failure)
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
                    retried = retry_structured_without_thinking(failure)
                    if retried is not None:
                        return retried
                    return try_fallback(failure)
                return try_fallback(ModelFailure('invalid_response', 'Selected model returned no text'))
            if (format is not None and result.get('finish_reason') in {'length', 'max_tokens'}
                    and runtime in {'mlx', 'ollama'}):
                try:
                    json.loads(str(result.get('text') or ''))
                except (TypeError, ValueError):
                    failure = ModelFailure('output_limit',
                        'Model exhausted its output budget before completing structured JSON')
                    retried = retry_structured_without_thinking(failure)
                    if retried is not None:
                        return retried
                    return try_fallback(failure)
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
                    vision_request = {"model": binding.model_id, "image": str(image_path),
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
