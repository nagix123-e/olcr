"""Local LFM2.5 Audio transcription for short voice dictation clips."""
from __future__ import annotations

import base64
import binascii
import io
import json
import threading
import wave
import os
from contextlib import contextmanager
from pathlib import Path

_MODELS = {
    "en": "LiquidAI/LFM2.5-Audio-1.5B",
    "ja": "LiquidAI/LFM2.5-Audio-1.5B-JP",
}
_model_lock = threading.Lock()
_download_lock = threading.Lock()
_loaded: tuple[str, object, object] | None = None
MAX_AUDIO_BYTES = 12 * 1024 * 1024
MAX_AUDIO_SECONDS = 120


@contextmanager
def _model_access(cancelled=None):
    while not _model_lock.acquire(timeout=0.1):
        if cancelled is not None and cancelled.is_set():
            yield False
            return
    try:
        yield cancelled is None or not cancelled.is_set()
    finally:
        _model_lock.release()


def _cache_dir() -> str:
    # Keep ASR independent of import order and Laya's HF_HOME mutation.
    if os.environ.get("OLCR_AUDIO_MODEL_CACHE"):
        return os.environ["OLCR_AUDIO_MODEL_CACHE"]
    if os.environ.get("OLCR_LAYA_CACHE"):
        return str(Path(os.environ["OLCR_LAYA_CACHE"]) / "hub")
    return str(Path(__file__).resolve().parents[2] / "models" / "laya" / "hub")


def _snapshot(model_id: str) -> Path:
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    from huggingface_hub.constants import HF_HUB_CACHE
    candidates = [_cache_dir(), str(Path(__file__).resolve().parents[2] / "models/laya/hub"), HF_HUB_CACHE]
    for cache in dict.fromkeys(candidates):
        try:
            path = Path(snapshot_download(repo_id=model_id, local_files_only=True, cache_dir=cache))
        except LocalEntryNotFoundError:
            continue
        # A snapshot directory alone can exist after an interrupted download.
        if all((path / name).is_file() for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "model.safetensors")):
            return path
    raise RuntimeError("AUDIO_MODEL_NOT_DOWNLOADED")


def _require_audio_runtime() -> None:
    """Fail before a multi-gigabyte download if this app cannot run ASR."""
    from .audio_mlx import enabled
    if enabled():
        return
    import sys

    if sys.version_info < (3, 12):
        raise RuntimeError("AUDIO_RUNTIME_REQUIRES_PYTHON_3_12")
    try:
        import liquid_audio  # noqa: F401
        import torch  # noqa: F401
        import torchaudio  # noqa: F401
    except Exception as exc:
        raise RuntimeError("AUDIO_RUNTIME_DEPENDENCIES_UNAVAILABLE") from exc


def audio_model_status(language: str, model_id: str | None = None) -> dict[str, str | bool]:
    """Report whether the full model snapshot is already available locally."""
    if language not in _MODELS:
        raise ValueError("AUDIO_LANGUAGE_UNSUPPORTED")
    if model_id and model_id.startswith('whisper_'):
        from .model_profiles import _data
        from .model_assets import asset_path
        descriptor = _data()['models'].get(model_id)
        if not descriptor:
            raise ValueError('AUDIO_MODEL_UNSUPPORTED')
        from . import audio_mlx
        runtime_available = audio_mlx.whisper_enabled()
        path = asset_path(descriptor['relative'])
        downloaded = (runtime_available and (path / '.olcr-complete.json').is_file()
                      and (path / 'config.json').is_file()
                      and any((path / name).is_file() for name in ('weights.npz', 'weights.safetensors', 'model.safetensors')))
        return {'language': language, 'model': model_id, 'downloaded': downloaded,
                'runtime_available': runtime_available}
    _require_audio_runtime()
    model_id = _MODELS[language]
    try:
        _snapshot(model_id)
    except RuntimeError:
        return {"language": language, "model": model_id, "downloaded": False}
    return {"language": language, "model": model_id, "downloaded": True}


def prepare_audio_model(language: str, model_id: str | None = None) -> dict[str, str | bool]:
    """Download and cache the requested model after explicit user consent."""
    if language not in _MODELS:
        raise ValueError("AUDIO_LANGUAGE_UNSUPPORTED")
    if model_id and model_id.startswith('whisper_'):
        from .model_profiles import _data
        from . import audio_mlx
        from huggingface_hub import snapshot_download
        descriptor = _data()['models'].get(model_id)
        if not descriptor:
            raise ValueError('AUDIO_MODEL_UNSUPPORTED')
        audio_mlx.ensure_whisper_runtime()
        root = Path(os.environ.get('OLCR_MODEL_ASSETS_DIR', str(Path(__file__).resolve().parents[2] / 'models')))
        target = root / descriptor['relative']
        with _download_lock:
            target.mkdir(parents=True, exist_ok=True)
            snapshot_download(repo_id=descriptor['repo'], revision=descriptor['revision'], local_dir=target)
            if not (target / 'config.json').is_file() or not any(
                    (target / name).is_file() for name in ('weights.npz', 'weights.safetensors', 'model.safetensors')):
                raise RuntimeError('AUDIO_MODEL_DOWNLOAD_INCOMPLETE')
            from .model_runtime_profiles import asset_identity_for_entry, make_asset_record, write_asset_record
            identity = asset_identity_for_entry({'id': model_id, 'repo': descriptor['repo'],
                                                 'revision': descriptor['revision']})
            write_asset_record(target, make_asset_record(target, identity))
        return {'language': language, 'model': model_id, 'downloaded': True}
    _require_audio_runtime()
    from huggingface_hub import snapshot_download

    model_id = _MODELS[language]
    with _download_lock:
        snapshot_download(repo_id=model_id, cache_dir=_cache_dir())
        snapshot = _snapshot(model_id)
        decoder = snapshot / 'audio_detokenizer'
        if not all((decoder / name).is_file() for name in ('config.json', 'model.safetensors')):
            raise RuntimeError('AUDIO_MODEL_DOWNLOAD_INCOMPLETE')
        from .model_runtime_profiles import (
            asset_identity_for_entry, make_asset_record, read_asset_record,
            SCHEMA_VERSION, write_asset_record,
        )
        identity = asset_identity_for_entry({'id': 'transcription_' + language, 'repo': model_id})
        existing = read_asset_record(snapshot, identity)
        if existing is None or existing.schema_version != SCHEMA_VERSION:
            write_asset_record(snapshot, make_asset_record(snapshot, identity))
    return {"language": language, "model": model_id, "downloaded": True}


def _load_model(language: str):
    """Caller holds _model_lock; ASR and TTS share a single model instance."""
    global _loaded
    import torch
    model_id = _MODELS[language]
    if _loaded is None or _loaded[0] != model_id:
        if _loaded is not None:
            del _loaded
            _loaded = None
            if hasattr(torch, "mps") and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        from liquid_audio import LFM2AudioModel, LFM2AudioProcessor
        device = "mps" if hasattr(torch, "mps") and torch.backends.mps.is_available() else "cpu"
        model_path = _snapshot(model_id)
        processor = LFM2AudioProcessor.from_pretrained(Path(model_path), device="cpu").eval()
        # `from_pretrained` defaults to CUDA and loads the checkpoint there.
        # Moving to MPS after that is too late on Apple Silicon, where CUDA
        # is unavailable. Load the weights directly onto the selected device.
        model = LFM2AudioModel.from_pretrained(Path(model_path), device=device).eval()
        _loaded = (model_id, model, processor)
    _, model, processor = _loaded
    return model_id, model, processor


def _stream_transcribe_audio_impl(audio_base64: str, language: str, cancelled: threading.Event | None = None,
                                  model_id: str | None = None):
    """Decode a bounded PCM WAV payload and run the selected local model."""
    if language not in _MODELS:
        raise ValueError("AUDIO_LANGUAGE_UNSUPPORTED")
    if cancelled is not None and cancelled.is_set():
        return
    _require_audio_runtime()
    if len(audio_base64) > (MAX_AUDIO_BYTES * 4 // 3 + 16):
        raise ValueError("AUDIO_CLIP_TOO_LARGE")
    try:
        raw = base64.b64decode(audio_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("AUDIO_PAYLOAD_INVALID") from exc
    if not raw or len(raw) > MAX_AUDIO_BYTES:
        raise ValueError("AUDIO_CLIP_TOO_LARGE")

    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
            if channels not in (1, 2) or width != 2 or not 8_000 <= sample_rate <= 96_000:
                raise ValueError("AUDIO_FORMAT_UNSUPPORTED")
            if frames <= 0 or frames / sample_rate > MAX_AUDIO_SECONDS:
                raise ValueError("AUDIO_CLIP_DURATION_INVALID")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * channels * width:
                raise ValueError("AUDIO_WAV_INVALID")
    except (wave.Error, EOFError) as exc:
        raise ValueError("AUDIO_WAV_INVALID") from exc

    import numpy as np
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    model_id = model_id or _MODELS[language]
    if model_id.startswith('whisper_'):
        from .model_profiles import _data
        from .model_assets import asset_path
        from . import audio_mlx
        descriptor = _data()['models'].get(model_id)
        if not descriptor:
            raise ValueError('AUDIO_MODEL_UNSUPPORTED')
        if not audio_mlx.whisper_enabled():
            raise RuntimeError('AUDIO_WHISPER_RUNTIME_UNAVAILABLE')
        path = asset_path(descriptor['relative'])
        if not (path / '.olcr-complete.json').is_file():
            raise RuntimeError('AUDIO_MODEL_NOT_DOWNLOADED')
        yield from audio_mlx.stream('transcribe_whisper', language, path, model_id, cancelled,
                                    pcm=base64.b64encode(samples.astype('<f4').tobytes()).decode('ascii'),
                                    sample_rate=sample_rate)
        return
    from . import audio_mlx
    if audio_mlx.enabled():
        yield from audio_mlx.stream('transcribe', language, _snapshot(_MODELS[language]),
                                    _MODELS[language], cancelled,
                                    pcm=base64.b64encode(samples.astype('<f4').tobytes()).decode('ascii'),
                                    sample_rate=sample_rate)
        return
    import torch
    audio = torch.from_numpy(samples.copy()).unsqueeze(0)

    with _model_access(cancelled) as active:
        if not active:
            return
        model_id, model, processor = _load_model(language)

        from liquid_audio import ChatState
        chat = ChatState(processor)
        chat.new_turn("system")
        chat.add_text("Perform ASR in japanese." if language == "ja" else "Perform ASR.")
        chat.end_turn()
        chat.new_turn("user")
        chat.add_audio(audio, sample_rate)
        chat.end_turn()
        chat.new_turn("assistant")

        tokens: list[int] = []
        inputs = {key: value.to(next(model.parameters()).device) for key, value in dict(chat).items()}
        previous = ""
        with torch.inference_mode():
            for token in model.generate_sequential(**inputs, max_new_tokens=512):
                if cancelled is not None and cancelled.is_set():
                    return
                if token.numel() == 1:
                    tokens.extend(token.reshape(-1).tolist())
                    # Decode the whole prefix: Japanese characters can span tokens.
                    text = processor.text.decode(tokens, skip_special_tokens=True).strip()
                    if text != previous and not text.endswith("\ufffd"):
                        previous = text
                        yield {"type": "text", "text": text, "model": model_id}
        yield {"type": "done", "text": previous, "model": model_id}


def _record_audio_events(events, traffic_call, cancelled):
    response_events = []
    finished = False
    try:
        for event in events:
            response_events.append(event)
            yield event
        is_cancelled = cancelled is not None and cancelled.is_set()
        traffic_call.finish(status='cancelled' if is_cancelled else 'success',
                            response_body=response_events,
                            error_code='stream_cancelled' if is_cancelled else None)
        finished = True
    except GeneratorExit:
        traffic_call.finish(status='cancelled', response_body=response_events,
                            error_code='stream_cancelled')
        finished = True
        raise
    except BaseException as exc:
        from .generation_control import GenerationCancelled
        traffic_call.finish(status='cancelled' if isinstance(exc, GenerationCancelled) else 'error',
                            response_body=response_events,
                            error_code=getattr(exc, 'category', type(exc).__name__))
        finished = True
        raise
    finally:
        if not finished:
            traffic_call.finish(status='cancelled', response_body=response_events,
                                error_code='stream_cancelled')


def stream_transcribe_audio(audio_base64: str, language: str, cancelled: threading.Event | None = None,
                            model_id: str | None = None):
    """Run local ASR and persist its request and streamed response in the global ledger."""
    if language not in _MODELS:
        raise ValueError("AUDIO_LANGUAGE_UNSUPPORTED")
    model_id = model_id or _MODELS[language]
    from . import audio_mlx
    from .model_traffic_logs import begin_model_traffic
    traffic_call = begin_model_traffic(
        role='transcription_' + language, model=model_id,
        runtime='mlx-whisper' if model_id.startswith('whisper_') else ('mlx-audio' if audio_mlx.enabled() else 'torch-audio'),
        request_body={'audio_base64': audio_base64, 'language': language})
    yield from _record_audio_events(_stream_transcribe_audio_impl(audio_base64, language, cancelled, model_id),
                                    traffic_call, cancelled)


def transcribe_audio(audio_base64: str, language: str, model_id: str | None = None) -> dict[str, str]:
    result = {"text": "", "model": model_id or _MODELS.get(language, "")}
    for event in stream_transcribe_audio(audio_base64, language, model_id=model_id):
        result = {"text": event["text"], "model": event["model"]}
    return result
