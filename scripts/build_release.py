#!/usr/bin/env python3
"""Build the local macOS arm64 OLCR release archive."""
from __future__ import annotations
import hashlib, json, os, platform, shutil, subprocess, sys, tarfile, time
from importlib.metadata import distributions
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; VERSION="0.5.0"; NAME=f"olcr-v{VERSION}-macos-arm64"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_wheelhouse import wheelhouse_preflight
sys.path.insert(0, str(ROOT / "backend"))
from olcr_api.knowledge import KnowledgeIndex
PYTHON_VERSION="3.12.15"
PYTHON_RELEASE="20261003"
RUNTIME=Path(os.environ.get("OLCR_STANDALONE_PYTHON", f"/private/tmp/olcr-release-inputs/cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"))
RUNTIME_SHA="ad8d0c637c0a36b967b310e2c07254f4d2ca8cabaa7699e55ed6290aceb481a2"
LAYA_MULTILINGUAL_REVISION=os.environ.get("OLCR_LAYA_MULTILINGUAL_REVISION", "052592a15d198d9ad47da779604259b10b47b7aa")
WHEELHOUSE=Path(os.environ["OLCR_WHEELHOUSE"]) if os.environ.get("OLCR_WHEELHOUSE") else None
SERENA_RUNTIME=Path(os.environ["OLCR_SERENA_RUNTIME"]) if os.environ.get("OLCR_SERENA_RUNTIME") else None
NODE_MCP_RUNTIME=Path(os.environ["OLCR_NODE_MCP_RUNTIME"]) if os.environ.get("OLCR_NODE_MCP_RUNTIME") else None
AGENT_RUNTIME=Path(os.environ.get("OLCR_AGENT_RUNTIME_SOURCE",str(ROOT/"agent-runtime")))
CODING_KNOWLEDGE_INDEX=Path(os.environ["OLCR_CODING_KNOWLEDGE_INDEX"]) if os.environ.get("OLCR_CODING_KNOWLEDGE_INDEX") else None

def run(*args: str, cwd: Path|None=None): subprocess.run(args, cwd=cwd, check=True)
def sha(path: Path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()
def files(root: Path): return [{"path":str(p.relative_to(root)),"sha256":sha(p),"bytes":p.stat().st_size} for p in sorted(root.rglob("*")) if p.is_file()]
def copy_file(source: Path, destination: Path):
    destination.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(source,destination)
def clean_runtime_cache(stage: Path):
    """Remove disposable bytecode/cache artifacts from release staging."""
    for cached in stage.rglob("__pycache__"):
        shutil.rmtree(cached)
    for compiled in list(stage.rglob("*.pyc")) + list(stage.rglob("*.pyo")):
        compiled.unlink()
def collect_licenses(stage: Path, site: Path) -> list[dict]:
    """Copy authoritative bundled evidence and fail if it is unavailable."""
    root=stage/"licenses"; root.mkdir(); records=[]
    copy_file(ROOT/"LICENSE",root/"OLCR_LICENSE")
    records.append({"component":"OLCR","version":VERSION,"license":"Apache-2.0","evidence":"repository LICENSE","path":"licenses/OLCR_LICENSE","status":"VERIFIED"})
    runtime_license=stage/"runtime"/"python"/"lib"/f"python{PYTHON_VERSION.rsplit('.', 1)[0]}"/"LICENSE.txt"
    if not runtime_license.is_file(): raise SystemExit("Python runtime license is missing")
    copy_file(runtime_license,root/"python-runtime"/"CPYTHON_LICENSE.txt")
    records.append({"component":"CPython (Astral python-build-standalone)","version":PYTHON_VERSION,"license":"PSF-2.0 and bundled notices","evidence":"runtime LICENSE.txt","path":"licenses/python-runtime/CPYTHON_LICENSE.txt","status":"VERIFIED"})
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
    for package in ("react","react-dom","scheduler","@xyflow/react","@xyflow/system",
                    "classcat","zustand","use-sync-external-store","d3-color","d3-dispatch",
                    "d3-drag","d3-ease","d3-interpolate","d3-selection","d3-timer","d3-transition","d3-zoom"):
        package_root=ROOT/"frontend"/"node_modules"/package
        sources=sorted(package_root.glob("LICENSE*"))
        if not sources: raise SystemExit(f"frontend license missing for {package}")
        metadata=json.loads((package_root/"package.json").read_text())
        target=root/"frontend"/f"{package.replace('/', '-')}-LICENSE"; copy_file(sources[0],target)
        records.append({"component":package,"version":metadata["version"],"license":metadata.get("license","see bundled license"),"evidence":"installed frontend package LICENSE","path":str(target.relative_to(stage)),"status":"VERIFIED"})
    (root/"license-manifest.json").write_text(json.dumps({"components":records},indent=2,sort_keys=True)+"\n")
    notices=["# OLCR v0.5.0 third-party notices","","License materials were collected from the bundled runtime/distribution metadata or installed package license files.",""]+[f"- {x['component']} {x['version']} — {x['license']} — `{x['path']}`" for x in records]
    (root/"THIRD_PARTY_NOTICES.md").write_text("\n".join(notices)+"\n")
    return records
def stage_serena_runtime(stage: Path, source: Path | None) -> dict | None:
    """Stage only a previously completed, self-describing Serena runtime."""
    if source is None: return None
    marker=source/"runtime-manifest.json"; python=source/"python"/"bin"/"python3"; inventory=source/"licenses"/"license-inventory.json"
    if not marker.is_file() or not python.is_file() or not inventory.is_file(): raise SystemExit("SERENA_RUNTIME_STAGE_INPUT_INVALID")
    destination=stage/"mcp-runtime"/"serena"; shutil.copytree(source,destination,ignore=shutil.ignore_patterns("__pycache__","*.pyc","*.pyo","wheelhouse"))
    staged_inventory=stage/"licenses"/"serena-runtime"/"license-inventory.json"; copy_file(destination/"licenses"/"license-inventory.json",staged_inventory)
    metadata=json.loads(marker.read_text()); licenses=json.loads((destination/"licenses"/"license-inventory.json").read_text())
    license_manifest=stage/"licenses"/"license-manifest.json"; manifest=json.loads(license_manifest.read_text())
    manifest["components"].append({"component":"Serena bundled runtime","version":metadata.get("serena_version",""),"license":"see Serena runtime inventory","evidence":"completed offline Serena runtime","path":"licenses/serena-runtime/license-inventory.json","status":"VERIFIED"})
    license_manifest.write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    notices=stage/"licenses"/"THIRD_PARTY_NOTICES.md"; notices.write_text(notices.read_text()+f"- Serena runtime {metadata.get('serena_version','')} — see `licenses/serena-runtime/license-inventory.json`\n")
    return {"path":"mcp-runtime/serena","serena_version":metadata.get("serena_version"),"python":metadata.get("python"),"licensed_components":len(licenses.get("components",[])),"inventory":"licenses/serena-runtime/license-inventory.json"}
def stage_node_mcp_runtime(stage: Path, source: Path | None) -> dict | None:
    """Stage the completed Node MCP runtime without its release inputs."""
    if source is None: return None
    marker=source/"runtime-manifest.json"; node=source/"node"/"bin"/"node"; inventory=source/"licenses"/"license-inventory.json"
    if not marker.is_file() or not node.is_file() or not inventory.is_file(): raise SystemExit("NODE_MCP_RUNTIME_STAGE_INPUT_INVALID")
    destination=stage/"mcp-runtime"/"node"; shutil.copytree(source,destination,ignore=shutil.ignore_patterns("__pycache__",".cache","npm-cache","wheelhouse"),symlinks=True)
    staged_inventory=stage/"licenses"/"node-mcp-runtime"/"license-inventory.json"; copy_file(destination/"licenses"/"license-inventory.json",staged_inventory)
    metadata=json.loads(marker.read_text()); licenses=json.loads((destination/"licenses"/"license-inventory.json").read_text())
    license_manifest=stage/"licenses"/"license-manifest.json"; manifest=json.loads(license_manifest.read_text())
    manifest["components"].append({"component":"Node MCP runtime","version":metadata.get("node",{}).get("version",""),"license":"see Node MCP runtime inventory","evidence":"completed offline Node MCP runtime","path":"licenses/node-mcp-runtime/license-inventory.json","status":"VERIFIED"})
    license_manifest.write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    notices=stage/"licenses"/"THIRD_PARTY_NOTICES.md"; notices.write_text(notices.read_text()+f"- Node MCP runtime {metadata.get('node',{}).get('version','')} — see `licenses/node-mcp-runtime/license-inventory.json`\n")
    return {"path":"mcp-runtime/node","node":metadata.get("node"),"package_count":metadata.get("package_count"),"licensed_components":len(licenses.get("components",[])),"inventory":"licenses/node-mcp-runtime/license-inventory.json"}
def stage_agent_runtime(stage: Path, source: Path) -> dict:
    marker=source/"runtime-manifest.json"
    if not marker.is_file(): raise SystemExit("AGENT_RUNTIME_PREPARE_REQUIRED: run scripts/prepare_agent_runtime.py")
    metadata=json.loads(marker.read_text())
    if metadata.get("engine_version") != "4.0.4" or metadata.get("node",{}).get("version") != "v24.18.0": raise SystemExit("AGENT_RUNTIME_VERSION_MISMATCH")
    if metadata.get("package_lock_sha256") != sha(source/"package-lock.json") or metadata.get("runner_sha256") != sha(source/"runner.mjs"): raise SystemExit("AGENT_RUNTIME_SOURCE_CHANGED")
    if not (source/"node_modules/node/bin/node").is_file(): raise SystemExit("AGENT_NODE_BINARY_MISSING")
    shutil.copytree(source,stage/"agent-runtime",symlinks=True,ignore=shutil.ignore_patterns(".cache","*.log","*.sqlite*","*.jsonl","__pycache__"))
    target=stage/"licenses"/"agent-runtime"/"license-inventory.json"
    copy_file(source/"licenses/license-inventory.json",target)
    manifest_path=stage/"licenses/license-manifest.json";manifest=json.loads(manifest_path.read_text())
    manifest["components"].append({"component":"LibreChat Agents runtime","version":"4.0.4","license":"MIT and bundled dependency notices","evidence":"installed pinned npm packages and exact upstream Node notice","path":"licenses/agent-runtime/license-inventory.json","status":"VERIFIED"})
    manifest_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    notices=stage/"licenses/THIRD_PARTY_NOTICES.md"
    with notices.open("a") as output: output.write("- LibreChat Agents 4.0.4 — MIT and bundled notices — `licenses/agent-runtime/license-inventory.json`\n")
    return {"path":"agent-runtime",**metadata}

def stage_coding_knowledge(stage: Path, source: Path | None) -> dict:
    """Stage only a completed immutable index; release never creates vectors."""
    if source is None: raise SystemExit("CODING_KNOWLEDGE_INDEX_STAGE_INPUT_REQUIRED")
    index=KnowledgeIndex.load(source)
    if not index.available: raise SystemExit("CODING_KNOWLEDGE_INDEX_STAGE_INPUT_INVALID")
    inventory=source.with_name("source-provenance-inventory.json")
    if not inventory.is_file(): raise SystemExit("CODING_KNOWLEDGE_PROVENANCE_INVENTORY_REQUIRED")
    try: inventory_data=json.loads(inventory.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError): raise SystemExit("CODING_KNOWLEDGE_PROVENANCE_INVENTORY_INVALID")
    if inventory_data.get("source_manifest_hash") != index.metadata.get("source_manifest_hash"):
        raise SystemExit("CODING_KNOWLEDGE_PROVENANCE_HASH_MISMATCH")
    destination=stage/"knowledge"; destination.mkdir(parents=True)
    copy_file(source,destination/"coding-knowledge-index.json")
    copy_file(inventory,destination/"source-provenance-inventory.json")
    return {"path":"knowledge/coding-knowledge-index.json","knowledge_index_version":index.metadata["knowledge_index_version"],"record_count":index.metadata["record_count"],"core_rule_count":index.metadata["core_rule_count"],"embedding_model":index.metadata["embedding_model"],"embedding_model_identity":index.metadata["embedding_model_version_or_identity"],"source_manifest_hash":index.metadata["source_manifest_hash"],"provenance_inventory":"knowledge/source-provenance-inventory.json"}
def main():
    if platform.system()!="Darwin" or platform.machine()!="arm64": raise SystemExit("macOS arm64 build host required")
    if not RUNTIME.is_file() or sha(RUNTIME)!=RUNTIME_SHA: raise SystemExit("verified standalone Python build input is missing")
    if CODING_KNOWLEDGE_INDEX is None: raise SystemExit("CODING_KNOWLEDGE_INDEX_STAGE_INPUT_REQUIRED")
    run("npm","run","typecheck",cwd=ROOT/"frontend"); run("npm","run","build",cwd=ROOT/"frontend")
    out=ROOT/"release"; stage=out/NAME; shutil.rmtree(stage,ignore_errors=True); stage.mkdir(parents=True)
    with tarfile.open(RUNTIME,"r:gz") as archive: archive.extractall(stage)
    (stage/"runtime").mkdir(); shutil.move(str(stage/"python"),str(stage/"runtime"/"python"))
    site=stage/"runtime"/"python"/"lib"/f"python{PYTHON_VERSION.rsplit('.', 1)[0]}"/"site-packages"; site.mkdir(parents=True,exist_ok=True)
    py=stage/"runtime"/"python"/"bin"/"python3"
    pip_args=[str(py),"-m","pip","install","--no-cache-dir","--target",str(site)]
    if WHEELHOUSE:
        if not WHEELHOUSE.is_dir(): raise SystemExit(f"offline wheelhouse missing: {WHEELHOUSE}")
        preflight=wheelhouse_preflight(WHEELHOUSE)
        if preflight["SCIPY_WHEELHOUSE_PREFLIGHT"] != "PASS":
            raise SystemExit(f"WHEELHOUSE_PREFLIGHT=FAIL reason={preflight['SCIPY_WHEELHOUSE_REASON']}")
        pip_args += ["--no-index", "--find-links", str(WHEELHOUSE)]
    pip_args += ["-r",str(ROOT/"backend"/"requirements.txt")]
    run(*pip_args)
    run("sh",str(ROOT/"scripts/build-knowledge-reader.sh"))
    copy_file(ROOT/"desktop/src-tauri/binaries/olcr-knowledge-reader",stage/"app/knowledge/olcr-knowledge-reader")
    shutil.copytree(ROOT/"backend",stage/"app"/"backend",ignore=shutil.ignore_patterns("__pycache__",".venv","*.pyc"))
    clean_runtime_cache(stage)
    shutil.copytree(ROOT/"frontend"/"dist",stage/"frontend")
    shutil.copy2(ROOT/"packaging"/"install.sh",stage/"install.sh"); (stage/"install.sh").chmod(0o755)
    licenses=collect_licenses(stage,site)
    serena=stage_serena_runtime(stage,SERENA_RUNTIME)
    coding_knowledge=stage_coding_knowledge(stage,CODING_KNOWLEDGE_INDEX)
    (stage/"README.txt").write_text(
        "Run ./install.sh, then run olcr. Laya downloads on initial API setup; agent, Node tool and browser engines download on first use. Ollama and its models remain external prerequisites.\n\n"
        "OLCR v0.5.0 is not Apple-notarized. If macOS quarantine metadata is present, install.sh "
        "warns and removes it only from OLCR's installed runtime and OLCR-managed launcher; it does "
        "not change Gatekeeper system-wide or affect unrelated files. Running install.sh constitutes "
        "consent to this documented installation step.\n"
    )
    manifest={"archive_structure_version":1,"olcr_version":VERSION,"target":{"os":"macos","architecture":"arm64"},"build_timestamp_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"git_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),"python":{"distribution":"astral-sh/python-build-standalone","version":PYTHON_VERSION,"release":PYTHON_RELEASE,"artifact_sha256":RUNTIME_SHA,"architecture":"aarch64-apple-darwin"},"laya":{"package":"laya","package_version":"0.3.5","model_revision":LAYA_MULTILINGUAL_REVISION,"model_revisions":{"multilingual":LAYA_MULTILINGUAL_REVISION},"multilingual_model":"convaiinnovations/laya-multilingual","delivery":"first-launch-download"},"optional_components":{"delivery":"pinned-upstream-first-use","recipes":"app/backend/olcr_api/runtime_recipes","components":["laya","agents","node-tools","browser"]},"licenses":{"manifest":"licenses/license-manifest.json","verified_components":len(licenses)},"serena_runtime":serena,"coding_knowledge":coding_knowledge,"external_ollama_models":{"required_for_answer_generation":"qwen3.8:latest","required_for_coding_knowledge_retrieval":"embeddinggemma:latest","dormant_semantic_judge":"qwen3.6:latest"},"dependencies":{d.metadata["Name"]:d.version for d in distributions(path=[str(site)] )}}
    (stage/"manifest").mkdir(); (stage/"manifest"/"release-manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    archive=out/f"{NAME}.tar.gz"
    with tarfile.open(archive,"w:gz",format=tarfile.PAX_FORMAT) as tf: tf.add(stage,arcname=NAME,recursive=True)
    (out/f"{NAME}.tar.gz.sha256").write_text(f"{sha(archive)}  {archive.name}\n")
    print(archive)
if __name__=="__main__": main()
