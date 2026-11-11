"""Persistent, local-only LFM runtime, launched by audio_mlx.py's MLX Python.

No Torch imports, network, microphone capture, or audio playback here. stdout
is exclusively NDJSON; library diagnostics go to stderr.
"""
from __future__ import annotations

import base64
import contextlib
import gc
import json
import os
from pathlib import Path
import sys
import time

os.environ['MLX_ENABLE_TF32'] = '0'
import mlx.core as mx
import mlx.nn as nn
import numpy as np

from audio_streaming import WaveformChunks, decoder_history, pcm_event


def optimize_model(model):
    """Adapt mlx-audio 0.4.6 to liquid-audio's inference math, without patching
    the shared environment (text/vision models use that environment too).
    """
    from mlx_audio.sts.models.lfm_audio.transformer import precompute_freqs_cis
    for block in model.audio_head.depthformer.blocks:
        # The reference MHA uses theta=1e6. This old MLX release hardcodes 1e4.
        block.attn._freqs = precompute_freqs_cis(block.attn.head_dim, 8, 1_000_000.0)

    def sample_frame(hidden, temperature=0.8, top_k=64):
        batch = hidden.shape[0]
        depth = model.depth_linear(hidden).reshape(batch, 1, 8, -1)
        previous = mx.zeros((batch, depth.shape[-1]), dtype=depth.dtype)
        cache, codes = None, []
        for index in range(8):
            output, cache = model.audio_head.depthformer(
                depth[:, :, index, :] + previous[:, None, :], cache=cache, use_cache=True)
            embedding = model.depth_embeddings[index]
            # Reference SharedEmbedding.get_logits normalizes the hidden state,
            # not the input embedding. 0.4.6's .logits omits this normalization.
            logits = embedding.to_logits(embedding.embedding_norm(output[:, -1, :]))
            if temperature <= 0 or top_k == 1:
                code = mx.argmax(logits, axis=-1)
            else:
                logits = logits.astype(mx.float32) / temperature
                threshold = mx.min(mx.topk(logits, top_k, axis=-1), axis=-1, keepdims=True)
                code = mx.random.categorical(mx.where(logits >= threshold, logits, -float('inf')))
            codes.append(code)
            previous = embedding.embedding(code)
        return mx.stack(codes, axis=-1)

    compiled = mx.compile(sample_frame, inputs=mx.random.state, outputs=mx.random.state)
    use_compile = os.environ.get('OLCR_AUDIO_MLX_COMPILE', '1') != '0'
    model._sample_audio_frame = lambda hidden, **kwargs: (
        compiled(hidden) if use_compile else sample_frame(hidden), None)

    def embeddings(text_tokens, audio_features, audio_codes, modalities):
        # ASR/TTS use one conversation with text and, for ASR, one input clip.
        # Scatter complete modality blocks once, rather than hundreds of
        # per-position graph nodes and an implicit float32 LM/cache promotion.
        dtype = model.lfm.embed_tokens.weight.dtype
        mods = modalities[0].tolist()
        result = mx.zeros((1, len(mods), model.config.lfm.hidden_size), dtype=dtype)
        text_positions = [i for i, value in enumerate(mods) if value == 1]
        audio_positions = [i for i, value in enumerate(mods) if value == 2]
        if audio_codes is not None or any(value == 3 for value in mods):
            raise ValueError('AUDIO_PROMPT_MODALITY_UNSUPPORTED')
        if text_positions:
            text = model._embed_text(text_tokens)
            if len(text_positions) != text.shape[1]:
                raise ValueError('AUDIO_PROMPT_LENGTH_INVALID')
            result = result.at[:, mx.array(text_positions), :].add(text.astype(dtype))
        if audio_positions:
            encoded, _ = model._encode_audio(audio_features.astype(dtype))
            if len(audio_positions) != encoded.shape[1]:
                raise ValueError('AUDIO_PROMPT_LENGTH_INVALID')
            result = result.at[:, mx.array(audio_positions), :].add(encoded.astype(dtype))
        return result
    model._build_interleaved_embeddings = embeddings
    return sample_frame


def positioned_decoder(decoder):
    # Keep RoPE's absolute positions across overlapping windows. In low
    # precision, resetting positions changes rounding and the resulting audio.
    if hasattr(decoder, '_olcr_positioned_decode'):
        return decoder._olcr_positioned_decode
    offset = [0]
    def shifted_rope(rope):
        def apply(value, offset_=0, **kwargs):
            return rope(value, offset=offset[0] + kwargs.get('offset', offset_))
        return apply
    for layer in decoder.lfm.layers:
        if layer.layer_type != 'conv':
            layer.self_attn._rope = shifted_rope(layer.self_attn._rope)
    def decode(codes, start_frame=0):
        offset[0] = start_frame * 6
        return decoder(codes)
    decoder._olcr_positioned_decode = decode
    return decode


class Runtime:
    def __init__(self):
        self.path = None
        self.model = None
        self.processor = None

    def load(self, path):
        if self.path == path:
            return
        from mlx_audio.sts.models.lfm_audio import LFM2AudioModel, LFM2AudioProcessor
        from transformers import AutoTokenizer
        self.model = self.processor = None
        gc.collect()
        mx.clear_cache()
        model = LFM2AudioModel.from_pretrained(path)
        bits = int(os.environ.get('OLCR_AUDIO_MLX_BITS', '0'))
        if bits not in (0, 4, 8):
            raise ValueError('AUDIO_QUANTIZATION_INVALID')
        if bits:
            # Protect the audio encoder, embeddings, depthformer and vocoder.
            nn.quantize(model, group_size=64, bits=bits,
                        class_predicate=lambda name, module:
                        name.startswith('lfm.layers.') and isinstance(module, nn.Linear))
            mx.eval(model.parameters())
        processor = LFM2AudioProcessor.from_pretrained(path)
        processor._tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True,
                                                            trust_remote_code=False)
        # Compile the fixed-shape 8-codebook frame boundary, not the LM's
        # mutable, growing hybrid cache. RNG is explicit in optimize_model.
        optimize_model(model)
        self.path, self.model, self.processor = path, model, processor

    def run(self, request):
        started = time.perf_counter()
        if request.get('operation') == 'transcribe_whisper':
            yield from self.run_whisper(request, started)
            return
        self.load(request['path'])
        from mlx_audio.sts.models.lfm_audio.processor import ChatState
        model, processor = self.model, self.processor
        language, operation, model_id = request['language'], request['operation'], request['model']
        chat = ChatState(processor)
        chat.new_turn('system')
        task = 'ASR' if operation == 'transcribe' else 'TTS'
        chat.add_text(f'Perform {task} in japanese.' if language == 'ja' else f'Perform {task}.')
        chat.end_turn()
        chat.new_turn('user')
        if operation == 'transcribe':
            samples = np.frombuffer(base64.b64decode(request['pcm']), dtype='<f4').copy()
            # The mel preprocessor needs at least two valid spectral frames
            # for its sample variance (tiny but valid WAV clips can be shorter).
            minimum = max(512, request['sample_rate'] * 512 // 16000)
            if samples.size < minimum:
                samples = np.pad(samples, (0, minimum - samples.size))
            chat.add_audio(mx.array(samples), request['sample_rate'])
        else:
            chat.add_text(request['text'])
        chat.end_turn()
        chat.new_turn('assistant')
        chunks = None
        if operation == 'synthesize':
            decoder = processor.detokenizer
            decode_wave = positioned_decoder(decoder)
            def decode(codes):
                waveform = decode_wave(mx.stack(codes, axis=1)[None], chunks.start_frame)
                mx.eval(waveform)
                return np.array(waveform, dtype=np.float32).reshape(-1)
            chunks = WaveformChunks(decode, decoder_history(
                decoder.config.layer_types, decoder.config.sliding_window))
        tokens, previous = [], ''
        first = None
        count = 0
        for token, modality in model.generate_from_chat_state(
                chat, mode='sequential', max_new_tokens=512 if chunks is None else 768,
                temperature=0, top_k=1, audio_temperature=0.8, audio_top_k=64):
            # Materialize each yielded frontier. Deferring all evaluations
            # would grow the graph; EOS decisions already require a sync.
            mx.eval(token)
            count += 1
            if chunks is None:
                if token.size != 1:
                    raise RuntimeError('AUDIO_TRANSCRIPTION_MODALITY_INVALID')
                tokens.extend(token.reshape(-1).tolist())
                text = processor.tokenizer.decode(tokens, skip_special_tokens=True).strip()
                if text != previous and not text.endswith('\ufffd'):
                    previous = text
                    first = first or time.perf_counter() - started
                    yield {'type': 'text', 'text': text, 'model': model_id}
            elif token.size > 1:
                if int(mx.max(token)) >= 2048:
                    break
                samples = chunks.push(token)
                if samples is not None:
                    first = first or time.perf_counter() - started
                    yield pcm_event(samples, model_id)
        if chunks is not None:
            if not chunks.codes:
                raise RuntimeError('AUDIO_SYNTHESIS_EMPTY')
            samples = chunks.flush(final=True)
            if samples is not None:
                first = first or time.perf_counter() - started
                yield pcm_event(samples, model_id)
        done = {'type': 'done', 'model': model_id,
                'metrics': {'engine': 'mlx', 'seconds': time.perf_counter() - started,
                            'first_output_seconds': first, 'tokens': count,
                            'quantization_bits': int(os.environ.get('OLCR_AUDIO_MLX_BITS', '0'))}}
        if chunks is None:
            done['text'] = previous
        else:
            done['metrics']['audio_seconds'] = chunks.emitted / 24000
        yield done

    @staticmethod
    def run_whisper(request, started):
        import mlx_whisper
        samples = np.frombuffer(base64.b64decode(request['pcm']), dtype='<f4').copy()
        sample_rate = int(request.get('sample_rate', 16000))
        if sample_rate != 16000 and samples.size:
            duration = samples.size / sample_rate
            source_times = np.arange(samples.size, dtype=np.float64) / sample_rate
            samples = np.interp(np.arange(round(duration * 16000), dtype=np.float64) / 16000,
                                source_times, samples).astype(np.float32)
        result = mlx_whisper.transcribe(samples, path_or_hf_repo=request['path'],
                                         language=request['language'], fp16=False, verbose=False)
        text = str(result.get('text') or '').strip()
        yield {'type': 'text', 'text': text, 'model': request['model']}
        yield {'type': 'done', 'text': text, 'model': request['model'],
               'metrics': {'engine': 'mlx-whisper', 'seconds': time.perf_counter() - started}}


def main():
    output = sys.stdout
    runtime = Runtime()
    for line in sys.stdin:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                request = json.loads(line)
                for event in runtime.run(request):
                    output.write(json.dumps(event, ensure_ascii=False) + '\n')
                    output.flush()
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stderr)
            output.write(json.dumps({'type': 'error', 'error': 'AUDIO_MLX_RUNTIME_FAILED'}) + '\n')
            output.flush()


if __name__ == '__main__':
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    main()
