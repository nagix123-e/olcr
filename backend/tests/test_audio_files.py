import base64
import io
import json
import tempfile
import shutil
import subprocess
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock,patch
from fastapi.testclient import TestClient
import olcr_api.app as api
from olcr_api.db import Database
from olcr_api.audio_files import AudioFiles,is_audio,instruction,pending_id,transcribe_file


def wav(seconds=1):
    out=io.BytesIO()
    with wave.open(out,'wb') as file:
        file.setnchannels(1);file.setsampwidth(2);file.setframerate(16000)
        file.writeframes(b'\0\0'*16000*seconds)
    return out.getvalue()

class AudioFileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/'db.sqlite');self.db.initialize()
        self.db.create_conversation('audio',time.time(),'conversation',None)
        self.attachment={'name':'meeting.wav','mimeType':'audio/wav','bytesBase64':base64.b64encode(wav()).decode()}
        for target,value in [('db',self.db),('_gui_session_token','audio-test')]:
            p=patch.object(api,target,value);p.start();self.addCleanup(p.stop)
        p=patch.object(api,'_chat_request_intent',side_effect=AssertionError('Audio must not reach Brain routing'))
        p.start();self.addCleanup(p.stop)
        self.client=TestClient(api.app);self.client.headers['X-OLCR-Session']='audio-test'

    def send(self,text='添付ファイルを確認してください。',attachment=None,ident=None,endpoint='/api/chat'):
        return self.client.post(endpoint,json={'message':text,'project_id':None,'conversation_id':'conversation','attachment':attachment,'message_id':ident})

    def test_file_only_asks_and_confirm_uses_saved_bytes_after_service_recreation(self):
        with patch('olcr_api.audio_files.transcribe_file',return_value='会議の記録です。') as transcribe:
            first=self.send(attachment=self.attachment)
            self.assertEqual(200,first.status_code,first.text)
            self.assertIn('実行しますか',first.json()['response']);transcribe.assert_not_called()
            self.assertNotIn(self.attachment['bytesBase64'],str(self.db.conversation('conversation')))
            self.assertTrue(pending_id(self.db.conversation('conversation')))
            second=self.send('はい')
            self.assertEqual('会議の記録です。',second.json()['response'])
            self.assertEqual(wav(),transcribe.call_args.args[0]);self.assertEqual('ja',transcribe.call_args.args[2])
            self.assertIsNone(pending_id(self.db.conversation('conversation')))
            with self.db.connect() as con:self.assertEqual(0,con.execute('SELECT count(*) FROM chat_audio_files').fetchone()[0])

    def test_explicit_request_runs_once_and_replays_same_message(self):
        with patch('olcr_api.audio_files.transcribe_file',return_value='transcript') as transcribe:
            for _ in range(2):
                response=self.send('文字起こしをして',self.attachment,'same-message')
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual('transcript',response.json()['response'])
            self.assertEqual(1,transcribe.call_count)
            self.assertEqual(2,len(self.db.conversation('conversation')['messages']))

    def test_confirmation_stream_uses_the_same_handler(self):
        self.send(attachment=self.attachment)
        with patch('olcr_api.audio_files.transcribe_file',return_value='stream transcript') as transcribe:
            response=self.send('文字起こしして',endpoint='/api/chat/stream')
            events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
            self.assertIn('stream transcript',[e.get('text') for e in events])
            self.assertEqual('AUDIO_TRANSCRIPTION',events[-1]['response_owner'])
            transcribe.assert_called_once()

    def test_language_setting_is_used_for_english_upload(self):
        with patch('olcr_api.audio_files.transcribe_file',return_value='Hello') as transcribe:
            result=self.client.post('/api/chat',headers={'X-OLCR-Language':'en'},json={'message':'Please transcribe this audio.','project_id':None,'conversation_id':'conversation','attachment':self.attachment})
            self.assertEqual(200,result.status_code,result.text)
            self.assertEqual('en',transcribe.call_args.args[2])

    def test_cancel_and_negation_do_not_transcribe(self):
        with patch('olcr_api.audio_files.transcribe_file') as transcribe:
            self.send(attachment=self.attachment)
            self.assertIn('取り消し',self.send('いいえ').json()['response'])
            result=self.send('文字起こしはしないで',self.attachment)
            self.assertIn('取り消し',result.json()['response']);transcribe.assert_not_called()

    def test_failed_transcription_retains_upload_for_retry(self):
        with patch('olcr_api.audio_files.transcribe_file',side_effect=[RuntimeError('unavailable'),'recovered']):
            first=self.send('文字起こしして',self.attachment)
            self.assertEqual('pending',first.json()['blocks'][0]['status'])
            self.assertEqual('recovered',self.send('実行して').json()['response'])

    def test_interrupted_upload_can_resume_with_the_same_message_id(self):
        from olcr_api.generation_control import GenerationCancelled
        service=AudioFiles(self.db)
        with patch('olcr_api.audio_files.transcribe_file',side_effect=GenerationCancelled('stop')):
            with self.assertRaises(GenerationCancelled):
                service.handle('conversation',self.attachment,'文字起こしして','interrupted','ja',Mock())
        with patch('olcr_api.audio_files.transcribe_file',return_value='resumed'):
            result=service.handle('conversation',self.attachment,'文字起こしして','interrupted','ja',Mock())
        self.assertEqual('resumed',result['response'])
        self.assertEqual(2,len(self.db.conversation('conversation')['messages']))

    def test_explicit_request_cannot_reuse_another_conversations_audio(self):
        self.send(attachment=self.attachment)
        self.db.create_conversation('other',time.time(),'other',None)
        with self.assertRaisesRegex(ValueError,'AUDIO_FILE_EXPIRED'):
            AudioFiles(self.db).handle('other',None,'はい','source','ja',Mock(),pending_id(self.db.conversation('conversation')))

    def test_bad_payload_is_rejected_before_any_model(self):
        with patch('olcr_api.audio_files.transcribe_file') as transcribe:
            response=self.send('文字起こしして',{**self.attachment,'bytesBase64':'not base64!'})
            self.assertEqual(422,response.status_code);transcribe.assert_not_called()

    def test_decoder_chunks_real_wav_and_uses_configured_model_for_each_chunk(self):
        models=Mock();choice={'protocol':'openai','model':'my-transcriber'}
        models.choice.return_value=choice;models.transcribe.side_effect=[{'text':'one'},{'text':'two'},{'text':'three'}]
        self.assertEqual('one\ntwo\nthree',transcribe_file(wav(61),'meeting.wav','en',models))
        models.choice.assert_called_once_with('transcription_en')
        self.assertEqual(3,models.transcribe.call_count)
        for call in models.transcribe.call_args_list:
            self.assertEqual(('en',choice),call.args[1:])
            with wave.open(io.BytesIO(base64.b64decode(call.args[0])),'rb') as file:
                self.assertLessEqual(file.getnframes()/file.getframerate(),30)

    def test_extensions_and_transcription_requests(self):
        self.assertTrue(is_audio({'name':'Interview.M4A','mimeType':'application/octet-stream'}))
        self.assertFalse(is_audio({'name':'notes.txt','mimeType':'text/plain'}))
        for text in ['文字起こしをして','この音声をテキスト化して','Please transcribe this','transcription please']:
            self.assertEqual('run',instruction(text),text)
        self.assertIsNone(instruction('文字起こしの使い方を教えて'))

    def test_compressed_audio_is_decoded_before_using_a_custom_model(self):
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:self.skipTest('ffmpeg fixture encoder is not available')
        source=Path(self.temp.name)/'source.wav';source.write_bytes(wav())
        for name in ['recording with spaces.mp3','録音.m4a']:
            with self.subTest(name=name):
                encoded=source.with_name(name)
                subprocess.run([ffmpeg,'-v','error','-y','-i',str(source),str(encoded)],check=True,capture_output=True)
                models=Mock();models.choice.return_value={'protocol':'openai','model':'configured'}
                models.transcribe.return_value={'text':'decoded transcript'}
                self.assertEqual('decoded transcript',transcribe_file(encoded.read_bytes(),name,'ja',models))
                payload=base64.b64decode(models.transcribe.call_args.args[0])
                with wave.open(io.BytesIO(payload),'rb') as audio:
                    self.assertEqual((1,2,16000),(audio.getnchannels(),audio.getsampwidth(),audio.getframerate()))
                if name.endswith('.m4a') and Path('/usr/bin/afconvert').is_file():
                    with patch('olcr_api.audio_files._ffmpeg_executable',return_value=None):
                        self.assertEqual('decoded transcript',transcribe_file(encoded.read_bytes(),name,'ja',models))

    def test_preset_transcription_uses_existing_language_pipeline(self):
        models=Mock();models.choice.return_value={'protocol':'preset','model':'LFM'}
        with patch('olcr_api.audio_transcription.stream_transcribe_audio',return_value=iter([
                {'type':'text','text':'途中'}, {'type':'done','text':'日本語の全文'}])) as stream:
            self.assertEqual('日本語の全文',transcribe_file(wav(),'speech.wav','ja',models))
            self.assertEqual('ja',stream.call_args.args[1])
            models.transcribe.assert_not_called()
