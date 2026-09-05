from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import zipfile
import posixpath
import stat
import shutil

ZIP_MAX_ENTRIES = 512
ZIP_MAX_FILE_BYTES = 1_000_000
ZIP_MAX_TOTAL_BYTES = 8_000_000
ZIP_TEXT_SUFFIXES = {".txt",".md",".markdown",".html",".htm",".css",".js",".mjs",".cjs",".ts",".tsx",".jsx",".json",".yaml",".yml",".toml",".xml",".csv",".py",".sh",".sql"}
IMAGE_MAX_BYTES = 20_000_000
IMAGE_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


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
    def current_file(self) -> Path | None:
        value=self.data.get("current_file"); return Path(value) if value else None
    def set_current_file(self, value: str) -> Path:
        workspace=self.workspace()
        if not workspace: raise ValueError("configure a workspace first")
        path=Path(value).expanduser()
        if not path.is_absolute(): path=workspace/path
        path=path.resolve()
        if workspace != path and workspace not in path.parents: raise PermissionError("file must be inside the authorized workspace")
        if not path.is_file(): raise ValueError("file does not exist")
        self.data["current_file"]=str(path); self.save(); return path
    def clear_current_file(self): self.data.pop("current_file",None); self.save()
    def image(self): return self.data.get("current_image")
    def load_image(self, value: str):
        path = Path(value).expanduser()
        if not path.is_absolute(): raise PermissionError("image path must be absolute")
        if path.is_symlink() or not path.is_file(): raise ValueError("image must be a regular non-symlink file")
        mime = IMAGE_SUFFIXES.get(path.suffix.lower())
        if not mime or path.stat().st_size > IMAGE_MAX_BYTES: raise ValueError("unsupported or oversized image")
        raw = path.read_bytes()
        if (mime == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n")) or (mime == "image/jpeg" and not raw.startswith(b"\xff\xd8\xff")):
            raise ValueError("image content does not match its declared format")
        record={"canonical_path":str(path.resolve()),"filename":path.name,"mime_type":mime,"byte_size":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}
        self.data["current_image"]=record; self.save(); return record
    def clear_image(self): self.data.pop("current_image",None); self.save()
    def external(self): return self.data.get("external_read_grant")
    def set_external(self, value: str):
        path=Path(value).expanduser()
        if not path.is_absolute() or path.is_symlink() or not (path.is_file() or path.is_dir()): raise ValueError("external path must be an existing regular file or directory")
        kind="directory" if path.is_dir() else "file"
        self.data["external_read_grant"]={"canonical_path":str(path.resolve()),"type":kind,"authorization_scope":"exact_path","read_only":True}; self.save(); return self.data["external_read_grant"]
    def clear_external(self): self.data.pop("external_read_grant",None); self.save()
    def import_external(self, destination: str | None = None):
        grant=self.external(); workspace=self.workspace()
        if not grant or not workspace: raise ValueError("external grant and workspace are required")
        src=Path(grant["canonical_path"]); rel=destination or src.name; dest=Path(rel)
        if dest.is_absolute() or ".." in dest.parts: raise PermissionError("import destination must be workspace-relative")
        dest=(workspace/dest).resolve()
        if workspace not in dest.parents and dest != workspace: raise PermissionError("destination outside workspace")
        if dest.exists() or dest.is_symlink(): raise FileExistsError("IMPORT_DESTINATION_EXISTS")
        if any(p.is_symlink() for p in src.rglob("*") if p.is_symlink()): raise PermissionError("external symlink is not allowed")
        dest.parent.mkdir(parents=True,exist_ok=True)
        if src.is_dir(): shutil.copytree(src,dest,symlinks=False)
        else: shutil.copy2(src,dest)
        return dest
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
                        if not name or name.startswith("__MACOSX/") or name.startswith("._") or name.endswith("/") or name.startswith("/") or posixpath.normpath(name) != name or name == ".." or name.startswith("../") or ":" in name.split("/",1)[0]: continue
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
