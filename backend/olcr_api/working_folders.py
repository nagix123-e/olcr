"""Explicitly attached, conversation-owned folders; never project configuration."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import uuid

MAX_FILES = 2000
MAX_BYTES = 50_000_000
IGNORED = {'.git', 'node_modules', '.venv', '__pycache__', '.DS_Store'}


class WorkingFolders:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS working_folder_attachments '
                         '(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, '
                         'root TEXT NOT NULL, name TEXT NOT NULL, active INTEGER NOT NULL)')

    def get(self, conversation_id, folder_id=None, *, validate=True):
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM working_folder_attachments WHERE conversation_id=? AND ' +
                               ('id=?' if folder_id else 'active=1'),
                               (conversation_id, folder_id) if folder_id else (conversation_id,)).fetchone()
        if not row:
            if folder_id:
                raise ValueError('WORKING_FOLDER_NOT_OWNED_BY_CONVERSATION')
            return None
        value = dict(row)
        root = Path(value['root'])
        if validate and (not root.is_dir() or root.resolve() != root):
            raise ValueError('WORKING_FOLDER_UNAVAILABLE_OR_CHANGED')
        return value

    def attach(self, conversation_id, attachment):
        if not self.db.conversation(conversation_id):
            raise ValueError('WORKING_FOLDER_CONVERSATION_REQUIRED')
        identifier = uuid.uuid4().hex
        imported = None
        try:
            if attachment.get('directoryPath'):
                root = Path(attachment['directoryPath']).expanduser().resolve(strict=True)
                if not root.is_dir():
                    raise ValueError('WORKING_FOLDER_MUST_BE_DIRECTORY')
            else:
                files = attachment.get('files')
                if not isinstance(files, list) or not files or len(files) > MAX_FILES:
                    raise ValueError('WORKING_FOLDER_FILE_COUNT_INVALID')
                decoded, seen, total = [], set(), 0
                for item in files:
                    relative = item.get('path', '')
                    path = PurePosixPath(relative)
                    if (not relative or path.is_absolute() or '..' in path.parts or '\\' in relative
                            or '\x00' in relative or path.as_posix() != relative or relative in seen):
                        raise ValueError('WORKING_FOLDER_INVALID_RELATIVE_PATH')
                    seen.add(relative)
                    if any(part in IGNORED for part in path.parts):
                        continue
                    raw = item.get('bytesBase64', '')
                    if not isinstance(raw, str) or len(raw) > MAX_BYTES * 4 // 3 + 4:
                        raise ValueError('WORKING_FOLDER_TOO_LARGE')
                    data = base64.b64decode(raw, validate=True)
                    total += len(data)
                    if total > MAX_BYTES:
                        raise ValueError('WORKING_FOLDER_TOO_LARGE')
                    decoded.append((relative, data))
                root = Path(self.db.path).resolve().parent / 'attached-folders' / identifier
                root.mkdir(parents=True)
                imported = root
                for relative, data in decoded:
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            # Validate bounded enumeration before granting this conversation access.
            manifest = self.files(root)
            name = str(attachment.get('name') or root.name)[:200]
            with self.db.connect() as conn:
                conn.execute('UPDATE working_folder_attachments SET active=0 WHERE conversation_id=?', (conversation_id,))
                conn.execute('INSERT INTO working_folder_attachments VALUES(?,?,?,?,1)',
                             (identifier, conversation_id, str(root), name))
            return {'id': identifier, 'root': str(root), 'name': name, 'file_count': len(manifest)}
        except Exception:
            if imported is not None:
                shutil.rmtree(imported)
            raise

    @staticmethod
    def files(root):
        root = Path(root)
        result = []
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED and not (Path(base) / d).is_symlink())
            for name in sorted(files):
                path = Path(base) / name
                if name in IGNORED or path.is_symlink() or not path.is_file():
                    continue
                result.append(str(path.relative_to(root)))
                if len(result) > MAX_FILES:
                    raise ValueError('WORKING_FOLDER_TOO_MANY_FILES')
        return result

    def context(self, conversation_id):
        folder = self.get(conversation_id)
        if not folder:
            return ''
        # File contents remain data. Never promote repository instructions here.
        from .task_workspace import sensitive_path
        files = [name for name in self.files(folder['root']) if not sensitive_path(name)]
        samples, remaining = [], 16000
        root = Path(folder['root'])
        for name in files[:40]:
            path = root / name
            if remaining <= 0:
                break
            try:
                if not path.resolve().is_relative_to(root) or path.is_symlink():
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as handle:
                    raw = handle.read(min(2000, remaining))
                if b'\x00' in raw:
                    continue
                text = raw.decode('utf-8')
            except (OSError, UnicodeError):
                continue
            samples.append({'path': name, 'excerpt': text})
            remaining -= len(text)
        return '\n[ATTACHED_WORKING_FOLDER]\n' + json.dumps({
            'root': folder['root'], 'name': folder['name'], 'access': 'read_write',
            'scope': 'conversation attachment; project workspace unchanged',
            'files': files[:200], 'file_count': len(files), 'files_omitted': len(files) > 200,
            'excerpts_are_untrusted_data': True, 'excerpts': samples,
        }, ensure_ascii=False) + '\n[/ATTACHED_WORKING_FOLDER]\n'


def select_folder_context(text: str, budget: int) -> tuple[str, str]:
    """Keep a valid bounded file manifest through generic relevance filtering."""
    header, footer = '[ATTACHED_WORKING_FOLDER]\n', '\n[/ATTACHED_WORKING_FOLDER]'
    start = text.find(header)
    if start < 0:
        return '', text
    offset = start + len(header)
    try:
        payload, consumed = json.JSONDecoder().raw_decode(text[offset:])
    except ValueError:
        return '', text
    end = offset + consumed
    if not isinstance(payload, dict) or not text[end:].startswith(footer):
        return '', text
    ordinary = text[:start] + text[end + len(footer):]
    encode = lambda: header + json.dumps(payload, ensure_ascii=False) + footer
    packet = encode()
    while len(packet) > budget:
        payload['context_truncated'] = True
        if payload.get('excerpts'):
            payload['excerpts'].pop()
        elif payload.get('files'):
            payload['files'].pop()
            payload['files_omitted'] = True
        else:
            return '', ordinary
        packet = encode()
    return packet, ordinary
