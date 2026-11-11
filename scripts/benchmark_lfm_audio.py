#!/usr/bin/env python3
"""Measure local LFM latency/RTF without playback, microphone, or downloads.

Run with .venv312/bin/python. The MLX engine uses its isolated interpreter.
--legacy reproduces the former Torch full-prefix / 25-frame decoder schedule.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['mlx', 'torch'], default='mlx')
    parser.add_argument('--language', choices=['ja', 'en'], default='ja')
    parser.add_argument('--text', default='こんにちは。今日はいい天気ですね。')
    parser.add_argument('--asr-wav', type=Path, help='Existing PCM16 WAV; transcribe instead of synthesizing')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--bits', choices=['0', '4', '8'], default='0')
    parser.add_argument('--no-compile', action='store_true')
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.legacy and args.engine != 'torch':
        parser.error('--legacy requires --engine torch')
    os.environ.update(OLCR_AUDIO_ENGINE=args.engine, OLCR_AUDIO_MLX_BITS=args.bits,
                      OLCR_AUDIO_MLX_COMPILE='0' if args.no_compile else '1',
                      HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    from olcr_api.audio_mlx import worker
    from olcr_api.audio_synthesis import stream_synthesize
    from olcr_api.audio_transcription import stream_transcribe_audio
    if args.legacy:
        from olcr_api import audio_streaming
        audio_streaming.CHUNK_CODES = 25
        from olcr_api import audio_synthesis
        audio_synthesis.decoder_history = lambda *unused: 1000000
    audio = base64.b64encode(args.asr_wav.read_bytes()).decode('ascii') if args.asr_wav else None
    results = []
    try:
        for run in range(args.runs):
            start, first, samples, final = time.perf_counter(), None, 0, None
            events = (stream_transcribe_audio(audio, args.language) if audio else
                      stream_synthesize(args.text, args.language, threading.Event()))
            for event in events:
                if event['type'] in ('audio', 'text') and first is None:
                    first = time.perf_counter() - start
                if event['type'] == 'audio':
                    samples += len(base64.b64decode(event['pcm_base64'])) // 2
                elif event['type'] == 'done':
                    final = event
            elapsed = time.perf_counter() - start
            seconds = samples / 24000
            row = {'run': run, 'wall_seconds': elapsed, 'first_output_seconds': first,
                   'audio_seconds': seconds, 'rtf': elapsed / seconds if seconds else None,
                   'result': final}
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        worker.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'engine': args.engine, 'language': args.language,
        'task': 'asr' if audio else 'tts', 'bits': int(args.bits), 'compile': not args.no_compile,
        'legacy': args.legacy, 'runs': results}, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
