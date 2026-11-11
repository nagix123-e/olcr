#!/usr/bin/env python3
"""Opt-in real MLX numerical checks; run with .venv-mlx/bin/python.

Pass an existing local snapshot path. No model downloads or audio playback.
Optional --frame-reference is an NPZ with Torch hidden (D,) and frame (8,).
"""
import argparse
import json
import os
from pathlib import Path
import sys

os.environ['MLX_ENABLE_TF32'] = '0'
import mlx.core as mx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/olcr_api'))
from audio_mlx_worker import Runtime, optimize_model, positioned_decoder
from audio_streaming import WaveformChunks, decoder_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--frame-reference', type=Path)
    args = parser.parse_args()
    runtime = Runtime()
    runtime.load(str(args.snapshot.resolve(strict=True)))
    sample = optimize_model(runtime.model)
    hidden = mx.ones((1, 1, runtime.model.config.lfm.hidden_size), dtype=runtime.model.lfm.embed_tokens.weight.dtype)
    reference = None
    if args.frame_reference:
        reference = np.load(args.frame_reference)
        hidden = mx.array(reference['hidden'], dtype=hidden.dtype)[None, None]
    expected = sample(hidden, temperature=0, top_k=1)
    compiled = mx.compile(lambda value: sample(value, temperature=0, top_k=1))
    actual = compiled(hidden)
    mx.eval(expected, actual)
    np.testing.assert_array_equal(np.array(expected), np.array(actual))
    if reference is not None:
        np.testing.assert_array_equal(np.array(actual)[0], reference['frame'])

    decoder = runtime.processor.detokenizer
    decode_wave = positioned_decoder(decoder)
    codes = mx.array(np.random.default_rng(812).integers(0, 2048, (117, 8)))
    full = decode_wave(codes.T[None])
    mx.eval(full)
    full = np.array(full).reshape(-1)
    sizes = []
    def decode(frames):
        sizes.append(len(frames))
        wave = decode_wave(mx.stack(frames, axis=1)[None], chunks.start_frame)
        mx.eval(wave)
        return np.array(wave).reshape(-1)
    chunks = WaveformChunks(decode, decoder_history(decoder.config.layer_types, decoder.config.sliding_window))
    parts = []
    for code in codes:
        wave = chunks.push(code)
        if wave is not None:
            parts.append(wave)
    parts.append(chunks.flush(final=True))
    wave = np.concatenate(parts)
    # Changing GPU GEMM shapes changes float32 reduction order. Require equal
    # lengths, <=3 PCM16 steps peak error, and a much smaller whole-wave RMS.
    np.testing.assert_allclose(wave, full, atol=3 / 32767, rtol=0)
    rms_error = float(np.sqrt(np.mean((wave - full) ** 2)))
    assert rms_error < 2e-6, rms_error
    assert max(sizes) <= 33
    print(json.dumps({'compiled_frame_equal': True, 'torch_frame_equal': reference is not None,
                      'waveform_samples': len(wave), 'max_sample_error': float(np.max(np.abs(wave - full))),
                      'rms_sample_error': rms_error,
                      'max_decode_frames': max(sizes)}))


if __name__ == '__main__':
    main()
