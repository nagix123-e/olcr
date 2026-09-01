from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time


def app_support() -> Path:
    return Path(os.environ.get("OLCR_APP_SUPPORT", Path.home()/"Library"/"Application Support"/"OLCR"))


class State:
    def __init__(self, root: Path | None = None):
        self.root=(root or app_support()).expanduser(); self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/"cli-state.json"; self.data=self._read()
    def _read(self):
        try:return json.loads(self.path.read_text())
        except (OSError,json.JSONDecodeError):return {"version":1,"workspace":None,"setup_complete":False}
    def save(self): self.path.write_text(json.dumps(self.data,indent=2))
    @staticmethod
    def workspace_id(path: Path) -> str: return hashlib.sha256(str(path).encode()).hexdigest()[:20]
    def set_workspace(self, value: str) -> Path:
        path=Path(value).expanduser().resolve()
        if not path.is_dir(): raise ValueError("workspace must be an existing directory")
        self.data["workspace"]={"id":self.workspace_id(path),"root":str(path),"updated_at":time.time()}; self.data["setup_complete"]=True; self.save(); return path
    def workspace(self) -> Path | None:
        value=self.data.get("workspace") or {}; return Path(value["root"]) if value.get("root") else None
    def context_path(self) -> Path:
        workspace=self.data.get("workspace") or {}; ident=workspace.get("id")
        if not ident: raise ValueError("configure a workspace first")
        path=self.root/"workspaces"/ident; path.mkdir(parents=True,exist_ok=True); return path/"core-context.json"
    def context(self):
        if not self.workspace(): return None
        try:return json.loads(self.context_path().read_text())
        except (OSError,json.JSONDecodeError):return None
    def set_context(self, content: str, source_type: str, source_path: str | None = None):
        if not content.strip(): raise ValueError("core context cannot be empty")
        record={"content":content,"source_type":source_type,"source_path":source_path,"source_hash":hashlib.sha256(content.encode()).hexdigest(),"created_at":time.time(),"updated_at":time.time()}
        if source_path:
            source=Path(source_path); record["source_modified_at"]=source.stat().st_mtime
        self.context_path().write_text(json.dumps(record,indent=2)); return record
    def clear_context(self):
        path=self.context_path()
        if path.exists(): path.unlink()
    def load_context_file(self, value: str):
        workspace=self.workspace()
        if not workspace: raise ValueError("configure a workspace first")
        path=Path(value).expanduser().resolve()
        if path != workspace and workspace not in path.parents: raise PermissionError("core-context file must be inside the authorized workspace")
        return self.set_context(path.read_text(),"file",str(path))
    def reload_context(self):
        current=self.context()
        if not current or current.get("source_type")!="file" or not current.get("source_path"): raise ValueError("no file-backed core context to reload")
        return self.load_context_file(current["source_path"])
    def context_changed(self):
        current=self.context()
        if not current or current.get("source_type")!="file": return False
        try:return Path(current["source_path"]).stat().st_mtime != current.get("source_modified_at")
        except OSError:return False
