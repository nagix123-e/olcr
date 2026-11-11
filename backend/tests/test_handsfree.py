import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
from olcr_api.handsfree import HandsFreeManager, validate_audio
from olcr_api.handsfree_gestures import GestureInput


class HandsFreeTests(unittest.TestCase):
    def test_off_and_status_never_create_assets_or_start_a_process(self):
        with tempfile.TemporaryDirectory() as root, patch('subprocess.Popen') as spawn:
            manager = HandsFreeManager(Path(root))
            self.assertFalse(manager.status()['enabled'])
            self.assertFalse(manager.root.exists())
            with self.assertRaisesRegex(ValueError, 'HANDSFREE_OFF'):
                manager.request({'session': 'old', 'op': 'wake', 'audio': ''})
            spawn.assert_not_called()

    def test_stale_disable_cannot_stop_new_enabled_session(self):
        with tempfile.TemporaryDirectory() as root:
            manager = HandsFreeManager(Path(root))
            manager.session = 'new'
            manager.stop('old')
            self.assertEqual('new', manager.session)
            manager.stop('new')
            self.assertIsNone(manager.session)

    def test_cancelled_preparation_does_not_start_more_download_processes(self):
        with tempfile.TemporaryDirectory() as root, patch('subprocess.Popen') as spawn:
            manager = HandsFreeManager(Path(root)); manager.stop()
            with self.assertRaisesRegex(RuntimeError, 'CANCELLED'):
                manager._run(['python', '-m', 'pip'])
            spawn.assert_not_called()

    def test_expired_session_rejects_input_and_stops_worker(self):
        with tempfile.TemporaryDirectory() as root:
            manager = HandsFreeManager(Path(root)); manager.session = 'lease'; manager.last_seen = 0
            with self.assertRaisesRegex(ValueError, 'EXPIRED'):
                manager.require('lease')
            self.assertFalse(manager.status()['enabled'])

    def test_audio_contract_rejects_wrong_rate_and_excessive_duration(self):
        for rate, duration, valid in [(16000, 3, True), (48000, 3, False), (16000, 5, False)]:
            output = io.BytesIO()
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
                wav.writeframes(b'\0\0' * int(rate * duration))
            encoded = base64.b64encode(output.getvalue()).decode()
            if valid: self.assertTrue(validate_audio(encoded))
            else:
                with self.assertRaisesRegex(ValueError, 'AUDIO_INVALID'): validate_audio(encoded)


def hand(pinched=False):
    landmarks = [[.5, .5, 0.] for _ in range(21)]
    landmarks[5] = [.4, .5, 0.]; landmarks[17] = [.7, .5, 0.]
    landmarks[8] = [.5, .3, 0.]; landmarks[4] = [.51 if pinched else .7, .3, 0.]
    return landmarks


class HumanGestureTests(unittest.TestCase):
    def test_short_pinch_clicks_once_and_drag_releases_on_hand_loss(self):
        gestures = GestureInput()
        self.assertEqual('move', gestures.update(hand(True), '', 1)[0]['kind'])
        self.assertIn({'kind': 'click'}, gestures.update(hand(False), '', 1.2))
        self.assertNotIn({'kind': 'click'}, gestures.update(hand(False), '', 1.3))
        gestures.update(hand(True), '', 2)
        self.assertIn({'kind': 'down'}, gestures.update(hand(True), '', 2.6))
        self.assertEqual([{'kind': 'up'}], gestures.update([], '', 2.7))
        self.assertFalse(gestures.dragging)

    def test_open_palm_cancels_a_pending_click(self):
        gestures = GestureInput(); gestures.update(hand(True), '', 1)
        self.assertEqual([], gestures.update(hand(False), 'Open_Palm', 1.1))
        self.assertEqual([], gestures.update(hand(False), '', 1.2))

    def test_invalid_coordinates_release_drag(self):
        gestures = GestureInput(); gestures.update(hand(True), '', 1); gestures.update(hand(True), '', 2)
        bad = hand(); bad[8][0] = float('nan')
        self.assertEqual([{'kind': 'up'}], gestures.update(bad, '', 3))

if __name__ == '__main__': unittest.main()
