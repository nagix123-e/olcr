"""Bounded-context LFM waveform streaming (shared by Torch and MLX)."""
from __future__ import annotations

import math

SAMPLES_PER_CODE = 1920  # 6 spectral frames * 320 samples, at 24 kHz
CHUNK_CODES = 8


def decoder_history(layer_types, sliding_window: int, conv_kernel: int = 3) -> int:
    # Receptive fields add across causal layers. Include the ISTFT's four
    # overlapping spectral frames and round UP to complete audio code frames.
    spectral = sum(conv_kernel - 1 if layer == 'conv' else sliding_window - 1
                   for layer in layer_types)
    return max(24, math.ceil((spectral + 4) / 6) + 1)


class WaveformChunks:
    """Decode new frames with enough left context; retain one frame for ISTFT.

    `decode` takes a list of code vectors and returns a flat numpy waveform.
    Causal attention/convolutions have finite context; decoding the entire
    prefix each time grows quadratically with the utterance's duration.
    """
    def __init__(self, decode, history: int):
        self.decode = decode
        self.history = history
        self.codes = []
        self.emitted = 0
        self.start_frame = 0

    def push(self, code):
        self.codes.append(code)
        if len(self.codes) % CHUNK_CODES == 0:
            return self.flush()
        return None

    def flush(self, final=False):
        if not self.codes:
            return None
        end = (len(self.codes) - (0 if final else 1)) * SAMPLES_PER_CODE
        if end <= self.emitted:
            return None
        start = max(0, self.emitted // SAMPLES_PER_CODE - self.history)
        self.start_frame = start
        waveform = self.decode(self.codes[start:])
        offset = start * SAMPLES_PER_CODE
        if waveform.size != (len(self.codes) - start) * SAMPLES_PER_CODE:
            raise RuntimeError('AUDIO_DECODER_LENGTH_INVALID')
        samples = waveform[self.emitted - offset:end - offset]
        self.emitted = end
        return samples


def pcm_event(samples, model_id):
    import base64
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
    return {'type': 'audio', 'pcm_base64': base64.b64encode(pcm).decode('ascii'),
            'sample_rate': 24000, 'model': model_id}
