"""Original knowledge files, validated independently of archive filenames."""
import base64
import binascii
import json
import os
from pathlib import Path
import subprocess

TEXT_TYPES = {'.txt':'text/plain','.md':'text/markdown','.csv':'text/csv','.tsv':'text/tab-separated-values',
              '.json':'application/json','.yaml':'application/yaml','.yml':'application/yaml','.xml':'application/xml'}
BINARY_TYPES = {'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}

def validate_file(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in TEXT_TYPES:
        data.decode('utf-8-sig')
        if b'\0' in data: raise ValueError('AGENT_KNOWLEDGE_TYPE_MISMATCH')
        return TEXT_TYPES[suffix]
    if not data: raise ValueError('AGENT_KNOWLEDGE_FILE_EMPTY')
    valid = {'.pdf':data.startswith(b'%PDF-'), '.png':data.startswith(b'\x89PNG\r\n\x1a\n'),
             '.jpg':data.startswith(b'\xff\xd8\xff'), '.jpeg':data.startswith(b'\xff\xd8\xff'),
             '.webp':data.startswith(b'RIFF') and data[8:12] == b'WEBP'}
    if suffix not in valid or not valid[suffix]: raise ValueError('AGENT_KNOWLEDGE_TYPE_MISMATCH')
    return BINARY_TYPES[suffix]

def decode_file(value: str) -> bytes:
    try: return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc: raise ValueError('AGENT_KNOWLEDGE_BASE64_INVALID') from exc

def file_bytes(item: dict) -> bytes:
    return decode_file(item['file_base64']) if item.get('file_base64') is not None else item['content'].encode('utf-8')

def reader_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    candidates = ([Path(os.environ['OLCR_KNOWLEDGE_READER'])] if os.environ.get('OLCR_KNOWLEDGE_READER') else [])
    candidates += [root / 'knowledge/olcr-knowledge-reader', root / 'desktop/src-tauri/binaries/olcr-knowledge-reader']
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK): return candidate
    raise ValueError('AGENT_KNOWLEDGE_READER_UNAVAILABLE')

def read_file(name: str, data: bytes) -> dict:
    mime = validate_file(name, data)
    if Path(name).suffix.lower() in TEXT_TYPES:
        # Keep the original bytes without imposing a file-size limit. Bound
        # only the extracted text stored alongside that original.
        content = data.decode('utf-8-sig')[:100_000]
    else:
        try:
            result = subprocess.run([str(reader_path())], input=json.dumps({'name':name,'data_base64':base64.b64encode(data).decode()}).encode(),
                                    capture_output=True, timeout=120)
            value = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise ValueError('AGENT_KNOWLEDGE_READ_FAILED') from exc
        if result.returncode or value.get('error'): raise ValueError(value.get('error') or 'AGENT_KNOWLEDGE_READ_FAILED')
        content = value['content']
    return {'name':name,'content':content,'mime_type':mime,'file_base64':base64.b64encode(data).decode()}
