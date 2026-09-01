#!/usr/bin/env python3
"""Build the local macOS arm64 OLCR release archive."""
from __future__ import annotations
import hashlib, json, os, platform, shutil, subprocess, tarfile, time
from importlib.metadata import distributions
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; VERSION="0.1.5"; NAME=f"olcr-v{VERSION}-macos-arm64"
RUNTIME=Path(os.environ.get("OLCR_STANDALONE_PYTHON", "/private/tmp/olcr-release-inputs/cpython-3.10.21+20260825-aarch64-apple-darwin-install_only_stripped.tar.gz"))
RUNTIME_SHA="78c7cb7cf464985bf8fd30fbf913aa428d152fd76b045e149f00b0f6b681a5ae"
MODEL=Path(os.environ.get("OLCR_RERANKER_SOURCE", "/private/tmp/olcr-rerank-hf/hub/models--Qwen--Qwen3-Reranker-0.6B/snapshots/e61197ed45024b0ed8a2d74b80b4d909f1255473"))

def run(*args: str, cwd: Path|None=None): subprocess.run(args, cwd=cwd, check=True)
def sha(path: Path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()
def files(root: Path): return [{"path":str(p.relative_to(root)),"sha256":sha(p),"bytes":p.stat().st_size} for p in sorted(root.rglob("*")) if p.is_file()]
def copy_file(source: Path, destination: Path):
    destination.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(source,destination)
def collect_licenses(stage: Path, site: Path) -> list[dict]:
    """Copy authoritative bundled evidence and fail if it is unavailable."""
    root=stage/"licenses"; root.mkdir(); records=[]
    copy_file(ROOT/"LICENSE",root/"OLCR_LICENSE")
    records.append({"component":"OLCR","version":VERSION,"license":"Apache-2.0","evidence":"repository LICENSE","path":"licenses/OLCR_LICENSE","status":"VERIFIED"})
    runtime_license=stage/"runtime"/"python"/"lib"/"python3.10"/"LICENSE.txt"
    if not runtime_license.is_file(): raise SystemExit("Python runtime license is missing")
    copy_file(runtime_license,root/"python-runtime"/"CPYTHON_LICENSE.txt")
    records.append({"component":"CPython (Astral python-build-standalone)","version":"3.10.21","license":"PSF-2.0 and bundled notices","evidence":"runtime LICENSE.txt","path":"licenses/python-runtime/CPYTHON_LICENSE.txt","status":"VERIFIED"})
    fallback=site/"transformers-4.57.6.dist-info"/"licenses"/"LICENSE"
    for dist in sorted(site.glob("*.dist-info")):
        meta=dist/"METADATA"; name=next((line[6:] for line in meta.read_text(errors="replace").splitlines() if line.startswith("Name: ")),dist.name); version=next((line[9:] for line in meta.read_text(errors="replace").splitlines() if line.startswith("Version: ")),"")
        sources=list((dist/"licenses").rglob("*") if (dist/"licenses").exists() else [])
        sources=[p for p in sources if p.is_file()]
        if not sources: sources=[p for p in dist.iterdir() if p.is_file() and p.name.lower().startswith(("license","copying","notice"))]
        if not sources and name=="tokenizers" and fallback.is_file(): sources=[fallback]
        if not sources: raise SystemExit(f"license evidence missing for {name} {version}")
        target=root/"python-packages"/dist.name
        for source in sources: copy_file(source,target/source.name)
        license_line=next((line.split(": ",1)[1] for line in meta.read_text(errors="replace").splitlines() if line.startswith(("License: ","License-Expression: "))),"see bundled license")
        records.append({"component":name,"version":version,"license":license_line,"evidence":"installed distribution metadata and license file","path":str((target/sources[0].name).relative_to(stage)),"status":"VERIFIED"})
    if not fallback.is_file(): raise SystemExit("Apache-2.0 text for reranker is missing")
    copy_file(fallback,root/"reranker"/"Qwen3-Reranker-0.6B-APACHE-2.0")
    records.append({"component":"Qwen/Qwen3-Reranker-0.6B","version":"e61197ed45024b0ed8a2d74b80b4d909f1255473","license":"Apache-2.0","evidence":"official model card metadata plus bundled Apache-2.0 text","path":"licenses/reranker/Qwen3-Reranker-0.6B-APACHE-2.0","status":"VERIFIED"})
    for package in ("react","react-dom","scheduler"):
        source=ROOT/"frontend"/"node_modules"/package/"LICENSE"
        if not source.is_file(): raise SystemExit(f"frontend license missing for {package}")
        target=root/"frontend"/f"{package}-LICENSE"; copy_file(source,target)
        records.append({"component":package,"version":"see package-lock.json","license":"MIT","evidence":"installed frontend package LICENSE","path":str(target.relative_to(stage)),"status":"VERIFIED"})
    (root/"license-manifest.json").write_text(json.dumps({"components":records},indent=2,sort_keys=True)+"\n")
    notices=["# OLCR v0.1.3 third-party notices","","License materials were collected from the bundled runtime/distribution metadata or installed package license files.",""]+[f"- {x['component']} {x['version']} — {x['license']} — `{x['path']}`" for x in records]
    (root/"THIRD_PARTY_NOTICES.md").write_text("\n".join(notices)+"\n")
    return records
def main():
    if platform.system()!="Darwin" or platform.machine()!="arm64": raise SystemExit("macOS arm64 build host required")
    if not RUNTIME.is_file() or sha(RUNTIME)!=RUNTIME_SHA: raise SystemExit("verified standalone Python build input is missing")
    if not MODEL.is_dir() or not (MODEL/"model.safetensors").is_file(): raise SystemExit("validated reranker snapshot is missing")
    run("npm","run","typecheck",cwd=ROOT/"frontend"); run("npm","run","build",cwd=ROOT/"frontend")
    out=ROOT/"release"; stage=out/NAME; shutil.rmtree(stage,ignore_errors=True); stage.mkdir(parents=True)
    with tarfile.open(RUNTIME,"r:gz") as archive: archive.extractall(stage)
    (stage/"runtime").mkdir(); shutil.move(str(stage/"python"),str(stage/"runtime"/"python"))
    site=stage/"runtime"/"python"/"lib"/"python3.10"/"site-packages"; site.mkdir(parents=True,exist_ok=True)
    py=stage/"runtime"/"python"/"bin"/"python3"
    run(str(py),"-m","pip","install","--no-cache-dir","--target",str(site),"-r",str(ROOT/"backend"/"requirements.txt"))
    shutil.copytree(ROOT/"backend",stage/"app"/"backend",ignore=shutil.ignore_patterns("__pycache__",".venv","*.pyc"))
    shutil.copytree(ROOT/"frontend"/"dist",stage/"frontend")
    shutil.copytree(MODEL,stage/"models"/"qwen3-reranker-0.6b",symlinks=False)
    shutil.copy2(ROOT/"packaging"/"install.sh",stage/"install.sh"); (stage/"install.sh").chmod(0o755)
    licenses=collect_licenses(stage,site)
    (stage/"README.txt").write_text(
        "Run ./install.sh, then run olcr. Ollama and its models remain external prerequisites.\n\n"
        "OLCR v0.1.3 is not Apple-notarized. If macOS quarantine metadata is present, install.sh "
        "warns and removes it only from OLCR's installed runtime and OLCR-managed launcher; it does "
        "not change Gatekeeper system-wide or affect unrelated files. Running install.sh constitutes "
        "consent to this documented installation step.\n"
    )
    manifest={"archive_structure_version":1,"olcr_version":VERSION,"target":{"os":"macos","architecture":"arm64"},"build_timestamp_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"git_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),"python":{"distribution":"astral-sh/python-build-standalone","version":"3.10.21","artifact_sha256":RUNTIME_SHA,"architecture":"aarch64-apple-darwin"},"reranker":{"id":"Qwen/Qwen3-Reranker-0.6B","revision":"e61197ed45024b0ed8a2d74b80b4d909f1255473","path":"models/qwen3-reranker-0.6b","license":"Apache-2.0","files":files(stage/"models"/"qwen3-reranker-0.6b")},"licenses":{"manifest":"licenses/license-manifest.json","verified_components":len(licenses)},"external_ollama_models":{"required_for_answer_generation":"qwen3.8:latest","required_for_experimental_semantic_retrieval":"embeddinggemma:latest","dormant_semantic_judge":"qwen3.6:latest"},"dependencies":{d.metadata["Name"]:d.version for d in distributions(path=[str(site)])}}
    (stage/"manifest").mkdir(); (stage/"manifest"/"release-manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    archive=out/f"{NAME}.tar.gz"
    with tarfile.open(archive,"w:gz",format=tarfile.PAX_FORMAT) as tf: tf.add(stage,arcname=NAME,recursive=True)
    (out/f"{NAME}.tar.gz.sha256").write_text(f"{sha(archive)}  {archive.name}\n")
    print(archive)
if __name__=="__main__": main()
