from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import socket
import re
import threading
from urllib import request, error

from .state import State
from olcr_api.config import DEFAULT_MAIN_MODEL, MODEL_REQUEST_TIMEOUT_SECONDS

VERSION="0.2.2"; API="http://127.0.0.1:8000/api"; ACCENT="\033[38;2;149;227;41m"; RESET="\033[0m"
OWNED_BACKEND = None

def color(text, enabled): return f"{ACCENT}{text}{RESET}" if enabled else text
def banner(enabled=True, narrow=False):
    logo="OLCR" if narrow else """ ██████╗ ██╗      ██████╗██████╗
██╔═══██╗██║     ██╔════╝██╔══██╗
██║   ██║██║     ██║     ██████╔╝
██║   ██║██║     ██║     ██╔══██╗
╚██████╔╝███████╗╚██████╗██║  ██║
 ╚═════╝ ╚══════╝ ╚═════╝╚═╝  ╚═╝"""
    return color(logo,enabled)+"\nOLCR · Ollama Local Cognitive Runtime\nLocal. Private. Context-aware."

def api(method, path, body=None):
    data=json.dumps(body).encode() if body is not None else None
    req=request.Request(API+path,data=data,method=method,headers={"Content-Type":"application/json"})
    with request.urlopen(req,timeout=MODEL_REQUEST_TIMEOUT_SECONDS) as response:return json.load(response)

def backend(state):
    global OWNED_BACKEND
    try:
        health=api("GET","/health")
        expected_db=str(state.root/"olcr.db")
        if health.get("version") == VERSION and health.get("app_support") == str(state.root) and health.get("db_path") == expected_db:
            return health
    except (error.URLError,error.HTTPError,TimeoutError): pass
    root=state.workspace()
    if not root: raise RuntimeError("configure a workspace before starting the OLCR service")
    # Allocate an OS-selected loopback port when the preferred endpoint is
    # occupied by another (incompatible) instance.
    sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM); sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]; sock.close()
    global API
    API=f"http://127.0.0.1:{port}/api"
    env=os.environ|{"OLCR_DB_PATH":str(state.root/"olcr.db"),"OLCR_ALLOWED_ROOTS":str(root),"OLCR_BACKEND_PORT":str(port)}
    OWNED_BACKEND = subprocess.Popen([sys.executable,"-m","uvicorn","olcr_api.app:app","--host","127.0.0.1","--port",str(port)],cwd=str(Path(__file__).parents[1]),env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    for _ in range(30):
        time.sleep(.2)
        try:return api("GET","/health")
        except (error.URLError,error.HTTPError,TimeoutError): pass
    raise RuntimeError("OLCR backend did not become available")

def shutdown_owned_backend():
    """Stop only the service process started by this CLI invocation."""
    global OWNED_BACKEND
    if OWNED_BACKEND and OWNED_BACKEND.poll() is None:
        OWNED_BACKEND.terminate()
        try: OWNED_BACKEND.wait(timeout=5)
        except subprocess.TimeoutExpired: OWNED_BACKEND.kill()
    OWNED_BACKEND = None

def configure_backend(state):
    backend(state)
    # Reload persisted settings into an already-running OLCR backend so a
    # previous CLI process cannot retain stale model configuration.
    try: settings=api("POST","/settings/reload")
    except (error.URLError,error.HTTPError,TimeoutError): settings=api("GET","/settings")
    settings["allowed_roots"]=[str(state.workspace())]
    if not settings.get("main_model"): settings["main_model"]=os.environ.get("OLLAMA_MODEL") or DEFAULT_MAIN_MODEL
    api("PUT","/settings",settings)

def runtime_status(state):
    checks={"platform":"READY" if platform.system()=="Darwin" and platform.machine()=="arm64" else "UNSUPPORTED","workspace":"READY" if state.workspace() else "MISSING","ripgrep":"READY" if shutil.which("rg") else "DEGRADED (Python fallback)","ollama":"UNAVAILABLE","semantic":"Experimental / unchecked"}
    try:
        tags=request.urlopen("http://127.0.0.1:11434/api/tags",timeout=2); names={x["name"] for x in json.load(tags).get("models",[])}; checks["ollama"]="READY"; checks.update({name:("READY" if name in names else "MISSING") for name in ("embeddinggemma:latest","qwen3.8:latest","qwen3.6:latest")})
    except (error.URLError,TimeoutError): pass
    try:
        health=api("GET","/health"); checks["backend"]="READY"; checks["model configuration"]="READY" if health.get("model_configuration")=="ready" else "NOT READY (set a main Ollama model)"; checks["semantic"]="Experimental" if health.get("vector_enabled") else "DISABLED"
    except (error.URLError,error.HTTPError,TimeoutError): checks["backend"]="NOT RUNNING"
    return checks

def context_text(state):
    item=state.context(); return item.get("content","")[:4000] if item else ""

def request_text(state,text):
    configure_backend(state)
    lower=text.strip().lower()
    if lower in {"hi","hello","こんにちは","ありがとう"}:
        return "こんにちは。何をお手伝いしましょうか？"
    ws=state.workspace()
    if ws and re.search(r"(どこ|どのファイル).*(index\.html|開)|index\.html.*(どこ|場所)", text, re.I):
        matches=sorted(str(p) for p in Path(ws).rglob("index.html"))
        return ("見つかりませんでした。" if not matches else "index.html: " + ", ".join(matches[:10]))
    if re.search(r"(どうやって|どう|何を|どのファイル|どこ).*(開く|実行|見る)|ブラウザで(見る|開く)", text):
        entry=Path(ws)/"index.html" if ws else None
        return "index.html をブラウザで開いてください。" if entry and entry.is_file() else "workspace内にindex.htmlが見つかりません。"
    core = None if len(text.strip()) < 80 else (context_text(state) or None)
    stop=threading.Event()
    def animate():
        frames='|/-\\'; i=0
        while not stop.wait(0.35):
            if sys.stdout.isatty():
                print(f"generating... {frames[i%len(frames)]}\r",end='',flush=True); i+=1
    spinner=threading.Thread(target=animate,daemon=True); spinner.start()
    try:
        result=api("POST", "/chat", {"message":text,"core_context":core}); return result["response"]
    finally:
        stop.set(); spinner.join(timeout=1)
        if sys.stdout.isatty(): print("\033[2K\r",end='',flush=True)

def show_status(state):
    print(f"OLCR {VERSION} · macOS Apple Silicon target");
    for name,value in runtime_status(state).items(): print(f"{name:24} {value}")
    print("core context             "+("configured" if state.context() else "not configured"))
    if state.context_changed(): print("! Core context source changed since last load; use /context reload.")

def wizard(state,input_fn=input,output=print):
    output("Welcome to OLCR. It runs locally and may read files only inside your chosen workspace.")
    while not state.workspace():
        value=input_fn("Workspace directory: ").strip()
        try: path=state.set_workspace(value); output(f"Workspace authorized: {path}")
        except ValueError as exc: output(f"! {exc}")
    output("Core context is an optional workspace snapshot: purpose, rules, conventions, and constraints.")
    choice=input_fn("Core context: [1] direct [2] file [3] skip: ").strip() or "3"
    if choice=="1": state.set_context(multiline(input_fn,output),"direct")
    elif choice=="2": state.load_context_file(input_fn("Context file inside workspace: ").strip())
    elif choice!="3": output("Skipped; configure later with /context.")
    state.data["setup_complete"]=True; state.save(); output("✓ Setup complete. Type /help for commands.")

def multiline(input_fn,output):
    output("Enter core context. Type a line containing only .done to save, or .cancel to cancel."); lines=[]
    while True:
        line=input_fn("")
        if line==".done": return "\n".join(lines)
        if line==".cancel": raise ValueError("core-context entry cancelled")
        lines.append(line)

def command(state,parts,input_fn=input,output=print):
    head=parts[0] if parts else "help"; tail=parts[1:]
    if head=="help": output("/status · /workspace show|set <path> · /context show|set|load <path>|reload|clear · /models · /quit"); return 0
    if head=="status": show_status(state); return 0
    if head=="models":
        for k,v in runtime_status(state).items():
            if k in {"ollama","embeddinggemma:latest","qwen3.8:latest","qwen3.6:latest","semantic"}: output(f"{k}: {v}")
        return 0
    if head=="workspace":
        if tail and tail[0]=="set": output(str(state.set_workspace(" ".join(tail[1:])))); return 0
        output(str(state.workspace() or "not configured")); return 0
    if head=="context":
        action=tail[0] if tail else "show"
        if action=="show": output(json.dumps(state.context() or {"status":"not configured"},indent=2)); return 0
        if action=="set": state.set_context(multiline(input_fn,output),"direct"); output("Core context snapshot saved."); return 0
        if action=="load": state.load_context_file(" ".join(tail[1:])); output("Core context file snapshot saved."); return 0
        if action=="reload": state.reload_context(); output("Core context snapshot reloaded."); return 0
        if action=="clear": state.clear_context(); output("Core context cleared."); return 0
    output("Unknown command. Type /help."); return 2

def repl(state,input_fn=input,output=print):
    ansi=sys.stdout.isatty() and os.environ.get("NO_COLOR") is None; output(banner(ansi,shutil.get_terminal_size((80,24)).columns<48));
    if not state.data.get("setup_complete") or not state.workspace(): wizard(state,input_fn,output)
    show_status(state)
    pending=None
    try:
        while True:
            try:
                value=(pending if pending is not None else input_fn("olcr> ").strip()); pending=None
                # Pasted Japanese implementation briefs are commonly multiline;
                # collect continuation lines until the paste's blank terminator.
                if value and re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value) and not value.startswith("/"):
                    lines=[value]
                    while True:
                        nxt=input_fn("").rstrip("\n")
                        if not nxt.strip(): break
                        if nxt.lstrip().startswith("/"):
                            pending=nxt.strip(); break
                        lines.append(nxt)
                    value="\n".join(lines).strip()
            except (EOFError,KeyboardInterrupt): output("\nGoodbye."); return 0
            if not value: continue
            if value in {"/quit","/exit"}: output("Goodbye."); return 0
            if value.startswith("/"): command(state,value[1:].split(),input_fn,output); continue
            if re.match(r'^cd\s+["\']?', value, re.I):
                output("REPL内のcdはサポートされていません。/workspace set \"/path/to/workspace\" を使用してください。"); continue
            try: output(request_text(state,value))
            except Exception as exc: output(f"! {exc}")
    finally: shutdown_owned_backend()

def main(argv=None):
    parser=argparse.ArgumentParser(prog="olcr",description="OLCR terminal-first local workspace")
    parser.add_argument("--version",action="version",version=VERSION); parser.add_argument("command",nargs="*"); args=parser.parse_args(argv); state=State()
    if not args.command:return repl(state)
    if args.command[0]=="search":
        try: print(request_text(state,"search "+" ".join(args.command[1:]))); return 0
        except Exception as exc: print(f"! {exc}",file=sys.stderr); return 1
        finally: shutdown_owned_backend()
    return command(state,args.command)

if __name__=="__main__": raise SystemExit(main())
