import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from olcr_api.file_conversion import ConversionError, attachment_bytes, conversion_target, convert_attachment


class FileConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.raw = b'PK\x00\xff\x81original document bytes'
        self.attachment = {'name': 'report.docx', 'bytesBase64': base64.b64encode(self.raw).decode()}
        self.calls = []
        self.progress = 0
        self.failure = False
        self.output = '/download/123/7/report.pdf'
        self.strategy = 'pdf,libreoffice'
        self.transport = httpx.MockTransport(self.handle)

    def tearDown(self):
        self.tmp.cleanup()

    def handle(self, request):
        self.calls.append(request.url.path)
        if request.url.path == '/':
            return httpx.Response(200, headers=[('set-cookie', 'auth=local; Path=/'), ('set-cookie', 'jobId=7; Path=/')])
        self.assertIn('auth=local', request.headers.get('cookie', ''))
        if request.url.path == '/conversions':
            return httpx.Response(200, text=f'<button data-value="{self.strategy}"></button><option value="txt,pandoc"></option>')
        if request.url.path == '/upload':
            self.assertIn(self.raw, request.content)
            return httpx.Response(200, json={'message': 'Files uploaded successfully.'})
        if request.url.path == '/convert':
            self.assertEqual([self.strategy], parse_qs(request.content.decode())['convert_to'])
            return httpx.Response(302, headers={'location': '/results/7'})
        if request.url.path == '/progress/7':
            self.progress += 1
            if self.failure:
                return httpx.Response(200, text='Failed, check logs')
            return httpx.Response(200, text=f'<a href="{self.output}" download="{self.output.rsplit("/",1)[-1]}">Download</a>')
        if request.url.path == self.output:
            return httpx.Response(200, content=b'%PDF-real-converter-output', headers={'content-type': 'application/pdf'})
        self.fail(str(request.url))

    def convert(self, target='pdf', workspace=None):
        return convert_attachment(self.attachment, target, workspace, transport=self.transport)

    def test_japanese_and_english_requests(self):
        for text, target in [('pdfに変えて', 'pdf'), ('このファイルをDOCXに変換してください', 'docx'),
                             ('拡張子を.pdfに変えて', 'pdf'), ('convert this to png', 'png'),
                             ('PNG形式に変えて', 'png'), ('pngにして', 'png')]:
            self.assertEqual(target, conversion_target(text), text)
        self.assertIsNone(conversion_target('このコードの変換処理を修正して'))
        self.assertIsNone(conversion_target('pdfに変換して、pngに変換して'))

    def test_project_destination_preserves_binary_and_existing_file(self):
        (self.root / 'report.pdf').write_bytes(b'existing')
        paths = self.convert(workspace=str(self.root))
        self.assertEqual([str(self.root / 'report (1).pdf')], paths)
        self.assertEqual(b'%PDF-real-converter-output', Path(paths[0]).read_bytes())
        self.assertEqual(b'existing', (self.root / 'report.pdf').read_bytes())

    def test_no_workspace_saves_to_downloads(self):
        with patch('olcr_api.file_conversion.Path.home', return_value=self.root):
            paths = self.convert()
        self.assertEqual(str(self.root / 'Downloads/report.pdf'), paths[0])

    def test_upstream_alias_is_selected_and_requested_suffix_is_preserved(self):
        self.strategy = 'jpeg,vips'
        self.output = '/download/123/7/report.jpg'
        paths = self.convert('jpeg', str(self.root))
        self.assertTrue(paths[0].endswith('/report.jpeg'))
        self.assertEqual(b'%PDF-real-converter-output', Path(paths[0]).read_bytes())

    def test_missing_configured_workspace_does_not_fall_back(self):
        with self.assertRaises(ConversionError):
            self.convert(workspace=str(self.root / 'missing'))
        self.assertEqual([], self.calls)

    def test_unsupported_pair_never_uploads_or_writes(self):
        with self.assertRaisesRegex(ConversionError, '対応していません'):
            self.convert('mp4', str(self.root))
        self.assertNotIn('/upload', self.calls)
        self.assertEqual([], list(self.root.iterdir()))

    def test_failed_conversion_never_writes(self):
        self.failure = True
        with self.assertRaisesRegex(ConversionError, '変換に失敗'):
            self.convert(workspace=str(self.root))
        self.assertEqual([], list(self.root.iterdir()))

    def test_malformed_attachment_and_remote_service_are_rejected(self):
        for attachment in [{'name': '../secret', 'bytesBase64': 'AA=='}, {'name': 'x.pdf', 'bytesBase64': '!!!'}]:
            with self.assertRaises(ConversionError):
                attachment_bytes(attachment)
        with patch.dict('os.environ', {'OLCR_CONVERTX_URL': 'https://example.com'}):
            with self.assertRaises(ConversionError):
                self.convert(workspace=str(self.root))

    def test_cross_job_download_rejected(self):
        self.output = '/download/123/8/report.pdf'
        with self.assertRaises(ConversionError):
            self.convert(workspace=str(self.root))
        self.assertEqual([], list(self.root.iterdir()))

    def test_chat_routes_before_models_and_replays_saved_result(self):
        with patch.dict('os.environ', {'OLCR_DB_PATH': str(self.root / 'import.db')}):
            import olcr_api.app as api
        api.stop_coding_scheduler()
        from olcr_api.db import Database
        database = Database(str(self.root / 'chat.db'))
        database.initialize()
        database.create_project('Conversion', str(self.root), 1, 'conversion')
        database.create_conversation('Convert', 1, 'chat', 'conversion')
        value = api.ChatInput(message='pdfに変えて', project_id='conversion', conversation_id='chat',
                              message_id='conversion-message', attachment=self.attachment)
        with patch.object(api, 'db', database), patch.object(api.settings, 'convert_enabled', True), \
             patch('olcr_api.file_conversion.convert_attachment', return_value=[str(self.root / 'report.pdf')]) as convert, \
             patch.object(api.runtime, 'execute', side_effect=AssertionError('model must not run')):
            first = api.chat(value)
            second = api.chat(value)
        self.assertEqual(first, second)
        convert.assert_called_once_with(self.attachment, 'pdf', str(self.root))
        self.assertEqual(2, len(database.conversation('chat')['messages']))

    def test_off_blocks_conversion_even_with_full_permissions(self):
        with patch.dict('os.environ', {'OLCR_DB_PATH': str(self.root / 'import-off.db')}):
            import olcr_api.app as api
        from olcr_api.db import Database
        database = Database(str(self.root / 'disabled.db')); database.initialize()
        database.create_conversation('Convert', 1, 'off-chat', None)
        value = api.ChatInput(message='pdfに変えて', project_id=None, conversation_id='off-chat', attachment=self.attachment)
        with patch.object(api, 'db', database), patch.object(api.settings, 'convert_enabled', False), \
             patch.object(api.settings, 'permission_level', 'full'), \
             patch('olcr_api.file_conversion.convert_attachment') as convert, \
             patch('olcr_api.conversion_runtime.runtime.prepare') as prepare:
            result = api.chat(value)
            self.assertIn('OFF', result['response'])
            self.assertFalse(api.file_conversion_runtime_status()['enabled'])
            with self.assertRaises(api.HTTPException):
                api.retry_file_conversion_runtime()
        convert.assert_not_called(); prepare.assert_not_called()

    def test_enable_persists_preference_and_starts_setup_without_model_rebuild(self):
        with patch.dict('os.environ', {'OLCR_DB_PATH': str(self.root / 'import-enable.db')}):
            import olcr_api.app as api
        from olcr_api.db import Database
        database = Database(str(self.root / 'settings.db')); database.initialize()
        original = api.settings
        try:
            with patch.object(api, 'db', database), patch.object(api, 'rebuild') as rebuild, \
                 patch('olcr_api.conversion_runtime.runtime.configure') as configure:
                api.patch_capability_settings(api.CapabilitySettingsPatch(convert_enabled=True))
                self.assertTrue(database.load_application_settings()['convert_enabled'])
                configure.assert_called_with(True, prepare=True)
                api.patch_capability_settings(api.CapabilitySettingsPatch(convert_enabled=False))
                self.assertFalse(database.load_application_settings()['convert_enabled'])
                configure.assert_called_with(False, prepare=False)
                rebuild.assert_not_called()
        finally:
            api.settings = original


if __name__ == '__main__':
    unittest.main()
