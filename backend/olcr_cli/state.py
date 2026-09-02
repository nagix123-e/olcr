from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import zipfile
import posixpath
import stat

ZIP_MAX_ENTRIES = 512
ZIP_MAX_FILE_BYTES = 1_000_000
ZIP_MAX_TOTAL_BYTES = 8_000_000
ZIP_TEXT_SUFFIXES = {".txt",".md",".markdown",".html",".htm",".css",".js",".mjs",".cjs",".ts",".tsx",".jsx",".json",".yaml",".yml",".toml",".xml",".csv",".py",".sh",".sql"}


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
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    infos = archive.infolist()
                    if len(infos) > ZIP_MAX_ENTRIES: raise ValueError("ZIP contains too many entries")
                    parts=[]; total=0
                    for info in infos:
                        name=info.filename.replace("\\","/")
                        if not name or name.endswith("/") or name.startswith("/") or posixpath.normpath(name) != name or name == ".." or name.startswith("../") or ":" in name.split("/",1)[0]: continue
                        mode=(info.external_attr >> 16) & 0xFFFF
                        if info.create_system == 3 and (mode & 0o170000) and not stat.S_ISREG(mode): continue
                        if Path(name).suffix.lower() not in ZIP_TEXT_SUFFIXES or info.file_size > ZIP_MAX_FILE_BYTES or total + info.file_size > ZIP_MAX_TOTAL_BYTES: continue
                        try: content=archive.read(info).decode("utf-8")
                        except (UnicodeDecodeError, RuntimeError, zipfile.BadZipFile): continue
                        parts.append(f"--- file: {name} ---\n{content}"); total += info.file_size
                    if not parts: raise ValueError("ZIP contains no eligible text files")
                    return self.set_context(f"Archive: {path.name}\n\n"+"\n\n".join(parts),"file",str(path))
            except zipfile.BadZipFile as exc: raise ValueError("invalid ZIP archive") from exc
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
