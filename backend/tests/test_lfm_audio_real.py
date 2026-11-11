"""Opt-in numerical regression against existing local weights; never downloads."""
import os
from types import SimpleNamespace
import unittest


@unittest.skipUnless(os.environ.get('OLCR_TEST_LFM_LOCAL') == '1', 'opt-in local LFM checkpoint test')
class LocalLFMDecoderTests(unittest.TestCase):
    def test_streamed_waveform_matches_full_decoder_with_real_weights(self):
        import numpy as np
        import torch
        from olcr_api.audio_transcription import _snapshot, _MODELS
        from olcr_api.audio_synthesis import _decoder
        from olcr_api.audio_streaming import WaveformChunks, decoder_history
        path = _snapshot(_MODELS[os.environ.get('OLCR_TEST_LFM_LANGUAGE', 'ja')])
        decoder = _decoder(SimpleNamespace(_audio_detokenizer=None, detokenizer_path=path / 'audio_detokenizer'))
        generator = torch.Generator().manual_seed(812)
        codes = torch.randint(0, 2048, (1, 8, 117), generator=generator)
        config = decoder.lfm.config
        sizes = []
        def decode(frames):
            sizes.append(len(frames))
            return decoder(torch.stack(frames, 1)[None]).float().reshape(-1).numpy()
        with torch.inference_mode():
            reference = decoder(codes).float().reshape(-1).numpy()
            chunks = WaveformChunks(decode, decoder_history(config.layer_types, decoder.sliding_window_size))
            parts = []
            for code in codes[0].T:
                samples = chunks.push(code)
                if samples is not None:
                    parts.append(samples)
            parts.append(chunks.flush(final=True))
        actual = np.concatenate(parts)
        self.assertEqual(reference.shape, actual.shape)
        # RoPE shifts/windowed GEMMs can differ by roundoff. Check the entire
        # waveform, especially chunk seams, with an absolute PCM-scale bound.
        np.testing.assert_allclose(actual, reference, atol=1 / 32767, rtol=1e-4)
        self.assertLessEqual(max(sizes), 33)


if __name__ == '__main__':
    unittest.main()
