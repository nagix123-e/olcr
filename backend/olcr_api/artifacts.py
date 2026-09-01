from __future__ import annotations
import json
from pathlib import Path
import time
import uuid
from .db import Database
from .models import SearchResult


class ArtifactStore:
    def __init__(self, root: str, db: Database, max_bytes: int = 2_000_000, ttl_seconds: int = 604800):
        self.root, self.db, self.max_bytes, self.ttl = Path(root).resolve(), db, max_bytes, ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)
    def create(self, task_id: str, results: list[SearchResult]) -> dict:
        artifact_id, now = str(uuid.uuid4()), time.time(); path = self.root / f"{artifact_id}.jsonl"
        payload = ""; kept = 0
        for item in results:
            row = json.dumps(item.__dict__, ensure_ascii=False) + "\n"
            if len((payload + row).encode()) > self.max_bytes: break
            payload += row; kept += 1
        path.write_text(payload)
        size = path.stat().st_size
        with self.db.connect() as db: db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", (artifact_id,task_id,str(path),kept,size,now,now+self.ttl))
        return {"id":artifact_id,"result_count":kept,"size_bytes":size,"expires_at":now+self.ttl}
    def read(self, artifact_id: str, offset: int, limit: int) -> dict:
        with self.db.connect() as db: row=db.execute("SELECT * FROM artifacts WHERE id=?",(artifact_id,)).fetchone()
        if not row: raise KeyError("artifact not found")
        path=Path(row["path"]).resolve()
        if self.root not in path.parents: raise PermissionError("invalid artifact path")
        lines=path.read_text().splitlines(); return {"artifact_id":artifact_id,"offset":offset,"results":[json.loads(x) for x in lines[offset:offset+limit]],"total":len(lines)}
