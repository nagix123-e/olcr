import base64
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

import numpy as np

from olcr_api import audio_mlx, audio_transcription
from olcr_api.audio_streaming import WaveformChunks, decoder_history


class AudioStreamingTests(unittest.TestCase):
    def test_chunking_preserves_samples_and_bounds_decoder_work(self):
        lengths = []
        # A finite causal dependency plus one future frame models the decoder's
        # receptive field/ISTFT contract. Long and irregular final chunks must
        # match a single full decode, including both ends.
        def decode(codes):
            lengths.append(len(codes))
            frames = np.asarray(codes, dtype=np.float32)
            left = np.convolve(frames, np.ones(17), mode='full')[:len(frames)]
            right = np.r_[frames[1:], 0]
            return np.repeat(left + right, 1920)
        codes = list(range(117))
        reference = decode(codes)
        lengths.clear()
        chunks = WaveformChunks(decode, history=24)
        parts = []
        for code in codes:
            samples = chunks.push(code)
            if samples is not None:
                parts.append(samples)
        parts.append(chunks.flush(final=True))
        np.testing.assert_array_equal(reference, np.concatenate(parts))
        self.assertLessEqual(max(lengths), 33)
        self.assertIsNone(chunks.flush(final=True))

    def test_short_utterance_and_empty_utterance(self):
        chunks = WaveformChunks(lambda codes: np.zeros(len(codes) * 1920), 24)
        self.assertIsNone(chunks.flush(final=True))
        chunks.push(1)
        self.assertEqual(1920, chunks.flush(final=True).size)

    def test_context_scales_for_different_decoder_configs(self):
        self.assertEqual(24, decoder_history(['conv'] * 5 + ['sliding_attention'] * 3, 30))
        self.assertGreater(decoder_history(['full_attention'] * 8, 100), 100)

    def test_invalid_audio_never_reaches_model_worker(self):
        with patch.object(audio_transcription, '_require_audio_runtime'), patch.object(audio_mlx, 'stream') as worker:
            with self.assertRaisesRegex(ValueError, 'AUDIO_PAYLOAD_INVALID'):
                list(audio_transcription.stream_transcribe_audio('bad base64', 'ja'))
            worker.assert_not_called()

    def test_cancelled_dictation_does_not_initialize_runtime(self):
        cancelled = threading.Event(); cancelled.set()
        with patch.object(audio_transcription, '_require_audio_runtime') as require:
            self.assertEqual([], list(audio_transcription.stream_transcribe_audio('', 'ja', cancelled)))
            require.assert_not_called()

    def test_torch_lock_wait_can_be_cancelled(self):
        audio_transcription._model_lock.acquire()
        cancelled = threading.Event(); cancelled.set()
        try:
            with audio_transcription._model_access(cancelled) as active:
                self.assertFalse(active)
        finally:
            audio_transcription._model_lock.release()

    def test_stereo_wav_is_downmixed_and_uses_selected_local_model(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(2); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(np.array([1000, 3000, -1000, -3000], dtype='<i2').tobytes())
        for language in ('ja', 'en'):
            with patch.object(audio_transcription, '_require_audio_runtime'), patch.object(audio_mlx, 'enabled', return_value=True), patch.object(audio_transcription, '_snapshot', return_value=Path('/local/model')), patch.object(audio_mlx, 'stream', return_value=iter([{'type': 'done', 'text': 'ok'}])) as worker:
                list(audio_transcription.stream_transcribe_audio(base64.b64encode(buffer.getvalue()).decode(), language))
                self.assertEqual(audio_transcription._MODELS[language], worker.call_args.args[3])
                np.testing.assert_allclose(np.frombuffer(base64.b64decode(worker.call_args.kwargs['pcm']), '<f4'),
                                           np.array([2000, -2000]) / 32768)


class AudioWorkerTests(unittest.TestCase):
    def test_cancelled_queued_request_preserves_active_worker(self):
        worker = audio_mlx.AudioWorker()
        worker.lock.acquire()
        cancelled = threading.Event(); cancelled.set()
        with patch.object(worker, '_start') as start, patch.object(worker, 'close') as close:
            self.assertEqual([], list(worker.stream({}, cancelled)))
            start.assert_not_called(); close.assert_not_called()
        worker.lock.release()

    def test_cancelled_generation_exits_and_next_request_has_no_stale_output(self):
        # Exercise real pipes/reader thread with a tiny CPU fixture, without MLX.
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'worker.py'
            fixture.write_text('''import sys,json,time
for line in sys.stdin:
 request=json.loads(line)
 print(json.dumps({'type':'text','text':request['text']}),flush=True)
 if request.get('slow'): time.sleep(5)
 print(json.dumps({'type':'done','text':request['text']}),flush=True)
''')
            worker = audio_mlx.AudioWorker()
            self.addCleanup(worker.close)
            popen = audio_mlx.subprocess.Popen
            def launch(command, **kwargs):
                return popen([sys.executable, '-u', str(fixture)], **kwargs)
            with patch.object(audio_mlx.subprocess, 'Popen', side_effect=launch):
                cancelled = threading.Event()
                stream = worker.stream({'text': 'old', 'slow': True}, cancelled)
                self.assertEqual('old', next(stream)['text'])
                cancelled.set()
                self.assertEqual([], list(stream))
                self.assertIsNone(worker.process)
                events = list(worker.stream({'text': 'new'}))
                process = worker.process
                self.assertEqual(['new', 'new'], [e['text'] for e in events])
                list(worker.stream({'text': 'reuse'}))
                self.assertIs(process, worker.process)

    def test_runtime_environment_does_not_mix_python_versions(self):
        with patch.dict(os.environ, {'PYTHONPATH': '/python312', 'PYTHONHOME': '/python312'}):
            env = audio_mlx._environment()
        self.assertNotIn('PYTHONPATH', env); self.assertNotIn('PYTHONHOME', env)
        self.assertEqual('1', env['HF_HUB_OFFLINE'])


if __name__ == '__main__':
    unittest.main()
