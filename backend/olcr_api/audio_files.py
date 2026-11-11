"""Message-scoped audio uploads and explicit transcription consent."""
import base64
import binascii
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path

from .generation_control import check_cancelled, current_generation

MAX_UPLOAD=50_000_000
MAX_DURATION=3600
_LOCK=threading.RLock()
EXTENSIONS={'.wav','.wave','.mp3','.m4a','.aac','.aiff','.aif','.flac','.ogg','.opus','.webm','.caf','.wma'}

def is_audio(attachment):
    return isinstance(attachment,dict) and attachment.get('kind')!='folder' and (
        str(attachment.get('mimeType') or attachment.get('mime_type') or '').startswith('audio/')
        or Path(str(attachment.get('name',''))).suffix.lower() in EXTENSIONS)

def instruction(text):
    if re.search(r'文字起こし.{0,6}(?:しない|不要|やめ|しなく)|transcrib\w*.*(?:do not|don.t)|(?:do not|don.t)\s+transcrib',text,re.I):
        return 'cancel'
    if re.fullmatch(r'\s*(?:いいえ|キャンセル|やめて|no|cancel)[。.!！\s]*',text,re.I):return 'cancel'
    if re.search(r'文字(?:起こし|おこし|にして)|書き起こ|書きおこ|テキスト(?:化|にして|に変換)|transcrib\w*|transcription|speech.to.text',text,re.I):
        if not re.search(r'使い方|方法|できますか|できる[？?]|\bhow\b',text,re.I):return 'run'
    if re.fullmatch(r'\s*(?:はい(?:、?お願い(?:します)?)?|お願い(?:します)?|実行(?:して|してください)?|やって|yes(?: please)?|go ahead|please do)[。.!！\s]*',text,re.I):return 'run'
    return None

def pending_id(conversation):
    messages=(conversation or {}).get('messages') or []
    if not messages:return None
    return next((b.get('id') for b in messages[-1].get('blocks') or []
                 if b.get('type')=='audio_transcription' and b.get('status')=='pending'),None)

def handles(attachment,text,conversation):
    return is_audio(attachment) or (not attachment and pending_id(conversation) and instruction(text))

def _ffmpeg_executable():
    # GUI launches do not always inherit Homebrew's PATH.
    return next((value for value in [shutil.which('ffmpeg'),'/opt/homebrew/bin/ffmpeg','/usr/local/bin/ffmpeg']
                 if value and Path(value).is_file() and os.access(value,os.X_OK)),None)

def _wav_chunks(raw,name):
    """Decode common audio containers locally, then bound each model request."""
    with tempfile.TemporaryDirectory(prefix='olcr-audio-file-') as directory:
        root=Path(directory);source=root/('input'+(Path(name).suffix.lower() if Path(name).suffix.lower() in EXTENSIONS else '.audio'))
        source.write_bytes(raw);target=root/'decoded.wav'
        ffmpeg=_ffmpeg_executable()
        if ffmpeg:
            command=[ffmpeg,'-nostdin','-v','error','-y','-i',str(source),'-t',str(MAX_DURATION+1),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(target)]
        elif Path('/usr/bin/afconvert').is_file():
            command=['/usr/bin/afconvert','-f','WAVE','-d','LEI16@16000','-c','1',str(source),str(target)]
        else:
            # Standard PCM WAV remains available without extra software.
            target=source;command=None
        check_cancelled()
        if command:
            result=subprocess.run(command,capture_output=True,timeout=60)
            if result.returncode:raise ValueError('AUDIO_FILE_DECODE_FAILED')
        check_cancelled()
        try:
            with wave.open(str(target),'rb') as audio:
                rate=audio.getframerate();frames=audio.getnframes()
                if audio.getsampwidth()!=2 or audio.getnchannels() not in (1,2) or not 8000<=rate<=96000:
                    raise ValueError('AUDIO_FORMAT_UNSUPPORTED')
                if not 0<frames/rate<=MAX_DURATION:raise ValueError('AUDIO_FILE_DURATION_INVALID')
                # 30s stays below the speech API's 12 MB bound even at stereo 96kHz.
                while pcm:=audio.readframes(rate*30):
                    check_cancelled();output=io.BytesIO()
                    with wave.open(output,'wb') as chunk:
                        chunk.setnchannels(audio.getnchannels());chunk.setsampwidth(2);chunk.setframerate(rate);chunk.writeframes(pcm)
                    yield base64.b64encode(output.getvalue()).decode('ascii')
        except (wave.Error,EOFError) as exc:raise ValueError('AUDIO_FILE_DECODE_FAILED') from exc

def transcribe_file(raw,name,language,models):
    selected=models.choice('transcription_'+language)
    parts=[]
    for audio in _wav_chunks(raw,name):
        check_cancelled()
        if selected['protocol']=='preset':
            from .audio_transcription import stream_transcribe_audio
            generation=current_generation();text=''
            for event in stream_transcribe_audio(audio,language,cancelled=generation.event if generation else None):
                if isinstance(event,dict) and isinstance(event.get('text'),str):text=event['text']
            result={'text':text}
        else:result=models.transcribe(audio,language,selected)
        check_cancelled()
        if result.get('text','').strip():parts.append(result['text'].strip())
    if not parts:raise ValueError('AUDIO_TRANSCRIPTION_EMPTY')
    return '\n'.join(parts)

class AudioFiles:
    def __init__(self,db):self.db=db

    def handle(self,conversation_id,attachment,text,source_id,language,models,pending=None):
        ja=language=='ja'
        with _LOCK:
            with self.db.connect() as con:
                con.execute('CREATE TABLE IF NOT EXISTS chat_audio_files(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,name TEXT NOT NULL,audio BLOB NOT NULL,expires_at REAL NOT NULL,status TEXT NOT NULL)')
                con.execute('DELETE FROM chat_audio_files WHERE expires_at<=?',(time.time(),))
                result_id=uuid.uuid5(uuid.NAMESPACE_URL,'audio-result:'+conversation_id+':'+source_id).hex
                prior=con.execute('SELECT content,blocks_json FROM messages WHERE id=?',(result_id,)).fetchone()
                if prior:
                    return {'conversation_id':conversation_id,'response':prior[0],'blocks':json.loads(prior[1] or '[]'),'sources':[],'response_owner':'AUDIO_TRANSCRIPTION'}
                saved_user=con.execute("SELECT content,blocks_json FROM messages WHERE id=? AND conversation_id=? AND role='user'",(source_id,conversation_id)).fetchone()
            blocks=[]
            if saved_user:
                if saved_user[0]!=text:raise ValueError('REQUEST_ID_REUSED')
                ident=next((b['id'] for b in json.loads(saved_user[1] or '[]') if b.get('type')=='audio_transcription'),pending)
                with self.db.connect() as con:
                    row=con.execute("SELECT name,audio FROM chat_audio_files WHERE id=? AND conversation_id=? AND status='pending'",(ident,conversation_id)).fetchone()
                if not row:raise ValueError('AUDIO_FILE_EXPIRED')
                name,raw=row
            elif attachment:
                encoded=attachment.get('bytesBase64') or attachment.get('dataBase64') or attachment.get('audio_base64')
                if not isinstance(encoded,str) or len(encoded)>MAX_UPLOAD*4//3+8:raise ValueError('AUDIO_FILE_TOO_LARGE_OR_MISSING')
                try:raw=base64.b64decode(encoded,validate=True)
                except (ValueError,binascii.Error) as exc:raise ValueError('AUDIO_PAYLOAD_INVALID') from exc
                if not raw or len(raw)>MAX_UPLOAD:raise ValueError('AUDIO_FILE_TOO_LARGE_OR_MISSING')
                name=str(attachment.get('name') or 'audio.wav')[:200];ident=uuid.uuid4().hex
                with self.db.connect() as con:
                    con.execute('INSERT INTO chat_audio_files VALUES(?,?,?,?,?,?)',(ident,conversation_id,name,raw,time.time()+86400,'pending'))
                blocks=[{'type':'attachment','name':name,'mime_type':attachment.get('mimeType') or attachment.get('mime_type') or 'audio/wav'},
                        {'type':'audio_transcription','id':ident,'status':'pending','name':name}]
            else:
                ident=pending
                with self.db.connect() as con:
                    row=con.execute("SELECT name,audio FROM chat_audio_files WHERE id=? AND conversation_id=? AND status='pending'",(ident,conversation_id)).fetchone()
                if not row:raise ValueError('AUDIO_FILE_EXPIRED')
                name,raw=row
            if not saved_user:self.db.add_message(conversation_id,'user',text,time.time(),source_id,blocks=blocks)
            action=instruction(text)
            if action=='cancel':
                response='文字起こしを取り消しました。' if ja else 'Transcription cancelled.'
                status='cancelled'
                with self.db.connect() as con:con.execute('DELETE FROM chat_audio_files WHERE id=?',(ident,))
            elif action=='run':
                try:
                    transcript=transcribe_file(raw,name,language,models)
                    response=transcript;status='completed'
                    with self.db.connect() as con:con.execute('DELETE FROM chat_audio_files WHERE id=?',(ident,))
                except ValueError as exc:
                    response=(f'文字起こしを実行できませんでした（{exc}）。設定またはファイルを確認し、「文字起こしして」で再試行できます。' if ja else f'Transcription failed ({exc}). Check the file and transcription settings, then say “transcribe” to retry.')
                    status='pending'
                except Exception as exc:
                    from .generation_control import GenerationCancelled
                    if isinstance(exc,GenerationCancelled):raise
                    response='文字起こしモデルを利用できませんでした。設定を確認し、「文字起こしして」で再試行できます。' if ja else 'The transcription model is unavailable. Check your settings and say “transcribe” to retry.'
                    status='pending'
            else:
                response='この音声ファイルを文字起こしできます。実行しますか？' if ja else 'I can transcribe this audio file. Would you like me to proceed?'
                status='pending'
            result_blocks=[{'type':'audio_transcription','id':ident,'status':status,'name':name}]
            self.db.add_message(conversation_id,'assistant',response,time.time(),result_id,blocks=result_blocks)
            return {'conversation_id':conversation_id,'response':response,'blocks':result_blocks,'sources':[],'response_owner':'AUDIO_TRANSCRIPTION'}
