import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from olcr_api.config import Settings
from olcr_api.conversion_runtime import IMAGE, ConversionRuntime, RuntimeSetupError, verified_download, vm_config


class Response(io.BytesIO):
    headers = {'Content-Length': '7'}


class ConversionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = ConversionRuntime(self.root / 'cx')

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_off_is_default_and_status_never_installs(self):
        self.assertFalse(Settings().convert_enabled)
        with patch.object(self.runtime, '_install') as install, patch('urllib.request.urlopen') as network:
            self.assertEqual('off', self.runtime.status()['state'])
            with self.assertRaisesRegex(RuntimeSetupError, 'OFF'):
                self.runtime.ensure(timeout=0)
            install.assert_not_called()
            network.assert_not_called()
        self.assertFalse(self.runtime.root.exists())

    def test_only_enable_starts_preparation_and_disable_cancels(self):
        entered, release = threading.Event(), threading.Event()
        def install(arch):
            entered.set()
            release.wait(2)
            self.runtime.check_enabled()
        with patch('platform.system', return_value='Darwin'), patch('platform.machine', return_value='arm64'), \
             patch('platform.mac_ver', return_value=('14.0', '', '')), \
             patch.object(self.runtime, '_install', side_effect=install), patch.object(self.runtime, '_start') as start:
            self.runtime.configure(True, prepare=True)
            self.assertTrue(entered.wait(2))
            self.runtime.configure(False)
            release.set()
            self.runtime.worker.join(2)
            self.runtime.stopper.join(2)
            start.assert_not_called()
            self.assertEqual('off', self.runtime.status()['state'])

    def test_verified_download_reuses_cache_without_network(self):
        target = self.root / 'payload'
        target.write_bytes(b'payload')
        digest = hashlib.sha256(b'payload').hexdigest()
        with patch('urllib.request.urlopen') as network:
            verified_download('https://example.invalid/archive', target, digest)
            network.assert_not_called()

    def test_corrupt_download_is_never_installed(self):
        target = self.root / 'payload'
        with patch('urllib.request.urlopen', return_value=Response(b'corrupt')):
            with self.assertRaisesRegex(RuntimeSetupError, '検証'):
                verified_download('https://example.invalid/archive', target, '0' * 64)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix('.part').exists())

    def test_download_cancellation_removes_partial_file(self):
        def cancel(text):
            raise RuntimeSetupError('cancelled')
        target = self.root / 'payload'
        with patch('urllib.request.urlopen', return_value=Response(b'payload')):
            with self.assertRaisesRegex(RuntimeSetupError, 'cancelled'):
                verified_download('https://example.invalid/archive', target, '0' * 64, cancel)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix('.part').exists())

    def test_vm_has_no_host_mounts_and_only_loopback_forwarding(self):
        config = json.loads(vm_config(self.root / 'image', 'arm64', '1' * 64, 43210))
        self.assertEqual([], config['mounts'])
        self.assertFalse(config['containerd']['system'])
        self.assertEqual('127.0.0.1', config['portForwards'][0]['hostIP'])
        self.assertTrue(config['portForwards'][1]['ignore'])
        self.assertEqual('vz', config['vmType'])
        self.assertEqual(1000, config['user']['uid'])
        self.assertEqual('any', config['portForwards'][1]['proto'])

    def test_cached_container_restarts_without_image_pull(self):
        instance = self.runtime.root / 'vm/cx'; instance.mkdir(parents=True)
        (instance / 'lima.yaml').write_text('{}')
        (self.runtime.root / 'service.json').write_text(json.dumps({'port': 43210, 'image': IMAGE}))
        self.runtime.configure(True)
        with patch.object(self.runtime, 'command', return_value=True) as command, \
             patch.object(self.runtime, 'healthy', return_value=True):
            self.runtime._start('arm64')
        args = [call.args for call in command.call_args_list]
        self.assertIn(('shell', 'cx', 'nerdctl', 'start', 'olcr-convertx'), args)
        self.assertFalse(any('pull' in call or 'run' in call for call in args))


if __name__ == '__main__':
    unittest.main()
