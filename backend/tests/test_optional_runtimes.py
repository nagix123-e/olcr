import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from olcr_api.optional_runtimes import OptionalRuntimes, SetupError, extract_tar, extract_zip

class OptionalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);recipes=self.root/'recipes';recipes.mkdir()
        (recipes/'laya.json').write_text(json.dumps({'repo':'test','revision':'fixed','files':[{'path':'model.bin','bytes':4,'sha256':hashlib.sha256(b'data').hexdigest()}]}))
        self.runtime=OptionalRuntimes(self.root/'components',recipes)

    def installer(self,stage):
        (stage/'multilingual').mkdir();(stage/'multilingual/model.bin').write_bytes(b'data')

    def test_status_and_resolver_never_acquire_files(self):
        with patch.object(self.runtime,'download',side_effect=AssertionError('status must be read only')):
            self.assertEqual('not_installed',self.runtime.status('laya')['state'])
            self.assertFalse(self.runtime.installed('laya'))
        self.assertFalse(self.runtime.root.exists())

    def test_atomic_install_is_reused_offline_and_repaired_after_missing_file(self):
        with patch.object(self.runtime,'_install_laya',side_effect=self.installer) as install:
            path=self.runtime.ensure('laya');self.assertTrue(self.runtime.installed('laya'))
            self.runtime.ensure('laya');self.assertEqual(1,install.call_count)
            (path/'multilingual/model.bin').unlink()
            self.assertFalse(self.runtime.installed('laya'))
            self.runtime.ensure('laya');self.assertEqual(2,install.call_count)

    def test_failed_install_is_not_visible_and_can_retry(self):
        def failed(stage):
            self.installer(stage);raise OSError('private upstream failure')
        with patch.object(self.runtime,'_install_laya',side_effect=failed):
            with self.assertRaises(SetupError):self.runtime.ensure('laya')
        self.assertFalse(self.runtime.path('laya').exists())
        self.assertEqual('error',self.runtime.status('laya')['state'])
        self.assertNotIn('private upstream',self.runtime.status('laya')['message'])
        with patch.object(self.runtime,'_install_laya',side_effect=self.installer):self.runtime.ensure('laya')
        self.assertEqual('ready',self.runtime.status('laya')['state'])

    def test_concurrent_calls_install_once(self):
        results=[]
        with patch.object(self.runtime,'_install_laya',side_effect=self.installer) as install:
            threads=[threading.Thread(target=lambda:results.append(self.runtime.ensure('laya'))) for _ in range(4)]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
        self.assertEqual(4,len(results));self.assertEqual(1,install.call_count)

    def test_download_checks_hash_and_never_promotes_partial_content(self):
        target=self.root/'asset'
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'wrong')):
            with self.assertRaises(SetupError):self.runtime.download('laya','https://example.invalid/file','0'*64,target)
        self.assertFalse(target.exists());self.assertEqual([],list(self.runtime.root.rglob('*.part')))
        digest=hashlib.sha256(b'correct').hexdigest()
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'correct')):
            self.runtime.download('laya','https://example.invalid/file',digest,target)
        target.unlink()
        with patch('urllib.request.urlopen',side_effect=AssertionError('must reuse cached download')):
            self.runtime.download('laya','https://example.invalid/file',digest,target)
        self.assertEqual(b'correct',target.read_bytes())

    def test_tar_rejects_traversal_and_symlink_escape(self):
        for name,link in [('../escape',None),('link','/tmp/outside')]:
            archive=self.root/'unsafe.tar'
            with tarfile.open(archive,'w') as output:
                item=tarfile.TarInfo(name)
                if link:item.type=tarfile.SYMTYPE;item.linkname=link
                output.addfile(item)
            with self.assertRaises(tarfile.FilterError):extract_tar(archive,self.root/'unpack')
        self.assertFalse((self.root/'escape').exists())

    def test_zip_rejects_escape_and_symlink(self):
        for name,mode in [('../escape',0),('link',0o120777)]:
            archive=self.root/'unsafe.zip'
            with zipfile.ZipFile(archive,'w') as output:
                entry=zipfile.ZipInfo(name);entry.external_attr=mode<<16;output.writestr(entry,b'payload')
            with self.assertRaises(SetupError):extract_zip(archive,self.root/'unpack')

    def test_unknown_component_cannot_address_arbitrary_paths(self):
        with self.assertRaises(SetupError):self.runtime.ensure('../other')

    def test_prepare_of_cached_component_finishes_without_getting_stuck(self):
        with patch.object(self.runtime,'_install_laya',side_effect=self.installer) as install:
            self.runtime.ensure('laya');self.runtime.prepare('laya')
            self.runtime.workers['laya'].join(timeout=2)
            self.assertEqual('ready',self.runtime.status('laya')['state'])
            self.assertEqual(1,install.call_count)

    def test_post_install_loading_error_remains_visible_for_retry(self):
        with patch.object(self.runtime,'_install_laya',side_effect=self.installer):self.runtime.ensure('laya')
        self.runtime.update('laya','error','Could not load the model')
        self.assertEqual('error',self.runtime.status('laya')['state'])

if __name__=='__main__':unittest.main()
