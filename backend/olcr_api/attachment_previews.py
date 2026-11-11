"""Local image previews, separate from chat history and available for 24 hours."""
import base64
import time
import uuid
from fastapi import HTTPException

TTL = 86400


def _table(con):
    con.execute('CREATE TABLE IF NOT EXISTS message_image_previews(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,data_url TEXT NOT NULL,expires_at REAL NOT NULL)')
    con.execute('DELETE FROM message_image_previews WHERE expires_at<=?', (time.time(),))


def image_preview(db, conversation_id, image):
    if not isinstance(image, dict): return {}
    data = image.get('data_url') or image.get('dataUrl')
    if not isinstance(data, str) or len(data)>6_700_100: return {}
    header, _, encoded = data.partition(',')
    mime = header.removeprefix('data:').removesuffix(';base64')
    extensions={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}
    if mime not in extensions or header!=f'data:{mime};base64': return {}
    try:
        from .agent_knowledge import validate_file
        raw=base64.b64decode(encoded,validate=True)
        if not raw or len(raw)>5_000_000 or validate_file('image'+extensions[mime],raw)!=mime: return {}
    except ValueError: return {}
    ident=uuid.uuid4().hex;expires=time.time()+TTL
    with db.connect() as con:
        _table(con)
        con.execute('INSERT INTO message_image_previews VALUES(?,?,?,?)',(ident,conversation_id,data,expires))
    return {'preview_id':ident,'preview_expires_at':expires,'mime_type':mime}


def register_preview_routes(app, database):
    @app.get('/api/message-image-previews/{ident}')
    def read_preview(ident: str):
        with database().connect() as con:
            _table(con)
            row=con.execute('SELECT data_url,expires_at FROM message_image_previews WHERE id=?',(ident,)).fetchone()
        if not row: raise HTTPException(404,'IMAGE_PREVIEW_EXPIRED')
        from fastapi.responses import JSONResponse
        return JSONResponse({'data_url':row[0],'expires_at':row[1]},headers={'Cache-Control':'no-store'})
