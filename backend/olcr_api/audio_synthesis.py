"""Language-selected LFM readout, streamed as mono 24 kHz PCM16."""
from __future__ import annotations

import threading

from . import audio_transcription as audio
from .audio_streaming import WaveformChunks, decoder_history, pcm_event


def synthesis_status(language: str):
    status = audio.audio_model_status(language)
    if status['downloaded']:
        path = audio._snapshot(audio._MODELS[language]) / 'audio_detokenizer'
        status['downloaded'] = all((path / name).is_file() for name in ('config.json', 'model.safetensors'))
    return status


def _decoder(processor):
    # liquid-audio's lazy property calls .cuda() unconditionally. Construct its
    # exact decoder on CPU instead: macOS has no CUDA, and ISTFT uses complex FFT.
    if processor._audio_detokenizer is None:
        from liquid_audio.detokenizer import LFM2AudioDetokenizer
        from transformers import Lfm2Config
        from safetensors.torch import load_file
        from pathlib import Path
        path = Path(processor.detokenizer_path)
        config = Lfm2Config.from_pretrained(path / 'config.json')
        config.layer_types = ['full_attention' if layer == 'sliding_attention' else layer for layer in config.layer_types]
        decoder = LFM2AudioDetokenizer(config).eval().to('cpu')
        decoder.load_state_dict(load_file(path / 'model.safetensors', device='cpu'))
        processor._audio_detokenizer = decoder
    return processor._audio_detokenizer


def _stream_synthesize_impl(text: str, language: str, cancelled: threading.Event):
    if language not in audio._MODELS or not isinstance(text, str) or not 0 < len(text.strip()) <= 180:
        raise ValueError('AUDIO_SYNTHESIS_REQUEST_INVALID')
    audio._require_audio_runtime()
    from . import audio_mlx
    if audio_mlx.enabled():
        yield from audio_mlx.stream('synthesize', language, audio._snapshot(audio._MODELS[language]),
                                    audio._MODELS[language], cancelled, text=text.strip())
        return
    import torch
    from liquid_audio import ChatState

    # Do not keep cancelled requests queued behind another recording/readout.
    while not audio._model_lock.acquire(timeout=0.1):
        if cancelled.is_set():
            return
    try:
        if cancelled.is_set():
            return
        model_id, model, processor = audio._load_model(language)
        decoder = _decoder(processor)
        chat = ChatState(processor)
        chat.new_turn('system')
        chat.add_text('Perform TTS in japanese.' if language == 'ja' else 'Perform TTS.')
        chat.end_turn()
        chat.new_turn('user')
        chat.add_text(text.strip())
        chat.end_turn()
        chat.new_turn('assistant')
        inputs = {key: value.to(next(model.parameters()).device) for key, value in dict(chat).items()}
        def decode(codes):
            return decoder(torch.stack(codes, 1).unsqueeze(0)).float().reshape(-1).numpy()
        config = decoder.lfm.config
        chunks = WaveformChunks(decode, decoder_history(config.layer_types,
            decoder.sliding_window_size, getattr(config, 'conv_L_cache', 3)))

        with torch.inference_mode():
            for token in model.generate_sequential(**inputs, max_new_tokens=768, audio_temperature=0.8, audio_top_k=64):
                if cancelled.is_set():
                    return
                if token.numel() > 1:
                    # End-of-audio is 2048, outside the decoder's codebook.
                    if bool((token >= 2048).any()):
                        break
                    samples = chunks.push(token.detach().to('cpu').reshape(-1))
                    if samples is not None:
                        yield pcm_event(samples, model_id)
            if cancelled.is_set():
                return
            if not chunks.codes:
                raise RuntimeError('AUDIO_SYNTHESIS_EMPTY')
            samples = chunks.flush(final=True)
            if samples is not None:
                yield pcm_event(samples, model_id)
        yield {'type': 'done', 'model': model_id}
    finally:
        audio._model_lock.release()


def _record_audio_events(events, traffic_call, cancelled):
    response_events = []
    finished = False
    try:
        for event in events:
            response_events.append(event)
            yield event
        is_cancelled = cancelled.is_set()
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


def stream_synthesize(text: str, language: str, cancelled: threading.Event):
    """Run local TTS and persist the request and streamed waveform response."""
    from . import audio_mlx
    from .model_traffic_logs import begin_model_traffic
    model_id = audio._MODELS.get(language, 'lfm-audio')
    traffic_call = begin_model_traffic(
        role='read_aloud', model=model_id,
        runtime='mlx-audio' if audio_mlx.enabled() else 'torch-audio',
        request_body={'text': text, 'language': language})
    yield from _record_audio_events(_stream_synthesize_impl(text, language, cancelled),
                                    traffic_call, cancelled)
