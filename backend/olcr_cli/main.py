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
import shlex
try:
    import readline
except ImportError:
    readline = None
from urllib import request, error
try:
    from prompt_toolkit import PromptSession
except ImportError:
    PromptSession = None

from .state import State
from olcr_api.config import DEFAULT_MAIN_MODEL, MODEL_REQUEST_TIMEOUT_SECONDS
from olcr_api.web import setup_guidance

VERSION="0.4.7"; API="http://127.0.0.1:8000/api"; ACCENT="\033[38;2;149;227;41m"; RESET="\033[0m"
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
    print("BACKEND_ENSURE_START", file=sys.stderr, flush=True)
    try:
        health=api("GET","/health")
        expected_db=str(state.root/"olcr.db")
        # A process started by this CLI invocation is trusted by ownership;
        # tolerate the legacy health-version metadata while retaining strict
        # compatibility checks for externally discovered services.
        owned = OWNED_BACKEND is not None and OWNED_BACKEND.poll() is None
        compatible = health.get("app_support") == str(state.root) and health.get("db_path") == expected_db
        if compatible and (owned or health.get("version") == VERSION):
            print("BACKEND_REUSE=YES", file=sys.stderr, flush=True)
            return health
    except (error.URLError,error.HTTPError,TimeoutError): pass
    root=state.workspace()
    if not root: raise RuntimeError("configure a workspace before starting the OLCR service")
    # Allocate an OS-selected loopback port when the preferred endpoint is
    # occupied by another (incompatible) instance.
    sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM); sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]; sock.close()
    global API
    API=f"http://127.0.0.1:{port}/api"
    print("BACKEND_REUSE=NO", file=sys.stderr, flush=True)
    print(f"BACKEND_PORT={port}", file=sys.stderr, flush=True)
    env=os.environ|{"OLCR_DB_PATH":str(state.root/"olcr.db"),"OLCR_ALLOWED_ROOTS":str(root),"OLCR_BACKEND_PORT":str(port)}
    # Keep backend diagnostics visible in the invoking Terminal while retaining
    # the existing process ownership and loopback-only execution model.
    OWNED_BACKEND = subprocess.Popen([sys.executable,"-m","uvicorn","olcr_api.app:app","--host","127.0.0.1","--port",str(port)],cwd=str(Path(__file__).parents[1]),env=env,start_new_session=True)
    print("BACKEND_PROCESS_START=YES", file=sys.stderr, flush=True)
    print("BACKEND_HEALTH_WAIT_START", file=sys.stderr, flush=True)
    for _ in range(30):
        time.sleep(.2)
        try:
            health=api("GET","/health")
            print("BACKEND_HEALTH_READY=YES", file=sys.stderr, flush=True)
            return health
        except (error.URLError,error.HTTPError,TimeoutError): pass
    # Health never became ready: clean up only the child spawned above.
    shutdown_owned_backend()
    raise RuntimeError("OLCR backend did not become available")

def shutdown_owned_backend():
    """Stop only the service process started by this CLI invocation."""
    global OWNED_BACKEND
    print("BACKEND_SHUTDOWN_START", file=sys.stderr, flush=True)
    proc=OWNED_BACKEND
    print(f"BACKEND_SHUTDOWN_OWNED={'YES' if proc is not None else 'NO'}", file=sys.stderr, flush=True)
    if proc and proc.poll() is None:
        proc.terminate(); print("BACKEND_TERMINATE_SENT=YES", file=sys.stderr, flush=True)
        try: proc.wait(timeout=5); print("BACKEND_KILL_FALLBACK=NO", file=sys.stderr, flush=True)
        except subprocess.TimeoutExpired:
            proc.kill(); print("BACKEND_KILL_FALLBACK=YES", file=sys.stderr, flush=True); proc.wait(timeout=5)
    print(f"BACKEND_EXIT_CONFIRMED={'YES' if proc is None or proc.poll() is not None else 'NO'}", file=sys.stderr, flush=True)
    OWNED_BACKEND = None
    print("BACKEND_SHUTDOWN_END", file=sys.stderr, flush=True)

def configure_backend(state):
    print("BACKEND_SETTINGS_SYNC_START", file=sys.stderr, flush=True)
    backend(state)
    # Reload persisted settings into an already-running OLCR backend so a
    # previous CLI process cannot retain stale model configuration.
    try: settings=api("POST","/settings/reload")
    except (error.URLError,error.HTTPError,TimeoutError): settings=api("GET","/settings")
    settings["allowed_roots"]=[str(state.workspace())]
    if not settings.get("main_model"): settings["main_model"]=os.environ.get("OLLAMA_MODEL") or DEFAULT_MAIN_MODEL
    # Preserve explicitly supplied CLI environment configuration when syncing
    # persisted settings; absent variables continue to use persisted values.
    if "OLCR_VECTOR_ENABLED" in os.environ:
        settings["vector_enabled"] = os.environ["OLCR_VECTOR_ENABLED"].lower() == "true"
    if "OLLAMA_EMBEDDING_MODEL" in os.environ:
        settings["embedding_model"] = os.environ["OLLAMA_EMBEDDING_MODEL"]
    if "OLLAMA_SEMANTIC_JUDGE_MODEL" in os.environ:
        settings["semantic_judge_model"] = os.environ["OLLAMA_SEMANTIC_JUDGE_MODEL"]
    api("PUT","/settings",settings)
    print("BACKEND_SETTINGS_SYNC_END", file=sys.stderr, flush=True)

def runtime_status(state):
    checks={"platform":"READY" if platform.system()=="Darwin" and platform.machine()=="arm64" else "UNSUPPORTED","workspace":"READY" if state.workspace() else "MISSING","ripgrep":"READY" if shutil.which("rg") else "DEGRADED (Python fallback)","ollama":"UNAVAILABLE","semantic":"Experimental / unchecked"}
    try:
        tags=request.urlopen("http://127.0.0.1:11434/api/tags",timeout=2); names={x["name"] for x in json.load(tags).get("models",[])}; checks["ollama"]="READY"; vision=os.environ.get("OLCR_VISION_MODEL","qwen2.5vl:3b"); brain=os.environ.get("OLLAMA_MODEL",DEFAULT_MAIN_MODEL); checks.update({name:("READY" if name in names else "MISSING") for name in ("embeddinggemma:latest",brain,vision)})
    except (error.URLError,TimeoutError): pass
    try:
        health=api("GET","/health"); checks["backend"]="READY"; checks["model configuration"]="READY" if health.get("model_configuration")=="ready" else "NOT READY (set a main Ollama model)"; checks["semantic"]="Experimental" if health.get("vector_enabled") else "DISABLED"
    except (error.URLError,error.HTTPError,TimeoutError): checks["backend"]="NOT RUNNING"
    return checks

def context_text(state):
    item=state.context(); return item.get("content","") if item else ""

def _auto_image_path(value: str) -> str | None:
    if "\n" in value: return None
    raw=value.strip()
    if raw.startswith("/") and raw.lower().endswith(tuple({".png",".jpg",".jpeg",".webp",".svg",".heic"})):
        # Accept a pasted terminal path with literal or backslash-escaped spaces.
        candidate=raw.replace("\\ "," ")
        if not any(x in candidate for x in ("\n", "\r", "\t")): return candidate
    try: tokens=shlex.split(value.strip())
    except ValueError: return None
    if len(tokens)!=1 or not tokens[0].startswith("/"): return None
    if Path(tokens[0]).suffix.lower() not in {".png",".jpg",".jpeg",".webp",".svg",".heic"}: return None
    return tokens[0]

def _explicit_implementation_request(text: str) -> bool:
    """Recognize imperative workspace mutation requests before capability checks."""
    # Questions about whether OLCR can write are informational, even when they
    # contain the same file-operation vocabulary.
    if re.search(r"(?:できますか|可能ですか|できますでしょうか|can\s+(?:you|olcr)|is\s+it\s+possible)", text, re.I):
        return False
    return bool(re.search(
        r"(?:実際に[^\n。！？]*?(?:作|作成|実装|書き込|更新|変更)|"
        r"(?:作って|作成して|実装して|書き込んで|更新して|変更して|実行して)|"
        r"(?:create|build|implement|write|modify|update)\b[^\n]*?(?:workspace|file|html|css|javascript|app))",
        text, re.I))

def request_text(state,text):
    lower=text.strip().lower()
    if lower in {"hi","hello","こんにちは","ありがとう"}:
        return "こんにちは。何をお手伝いしましょうか？"
    # Capability questions are informational and must never enter the
    # implementation executor merely because they mention file mutation.
    capability = (not _explicit_implementation_request(text)) and bool(re.search(r"(workspace|ワークスペース).*(作成|更新|編集|create|update|edit)|can\s+(?:you|olcr).*(create|update|edit).*file", text, re.I))
    if capability:
        if state.workspace() and state.workspace().is_dir():
            return "OLCRは現在認可されたworkspace内でのみファイルを作成・更新できます。実際の変更は実装実行と書き込み・再読込確認が成功した場合に限り報告されます。"
        return "ファイルの作成・更新には、まず認可されたworkspaceを設定してください: /workspace set \"/path/to/workspace\""
    configure_backend(state)
    ws=state.workspace()
    if ws and re.search(r"(どこ|どのファイル).*(index\.html|開)|index\.html.*(どこ|場所)", text, re.I):
        matches=sorted(str(p) for p in Path(ws).rglob("index.html"))
        return ("見つかりませんでした。" if not matches else "index.html: " + ", ".join(matches[:10]))
    if re.search(r"(どうやって|どう|何を|どのファイル|どこ).*(開く|実行|見る)|ブラウザで(見る|開く)", text):
        entry=Path(ws)/"index.html" if ws else None
        return "index.html をブラウザで開いてください。" if entry and entry.is_file() else "workspace内にindex.htmlが見つかりません。"
    explicit_core = bool(re.search(r"core\s*context|この(?:コア|core)コンテキスト|コアコンテキストに書|コアコンテキストによると", text, re.I))
    if explicit_core and not state.context():
        return "core contextが設定されていません。/context set または /context load \"/path/to/file\" を使用してください。"
    topics = sum(bool(re.search(pattern, text, re.I)) for pattern in (r"lock\s*delay|ロックディレイ", r"盤面|board", r"drop\s*interval|落下", r"lock\s*timer|リセット回数|reset"))
    if explicit_core and topics <= 1 and re.search(r"lock\s*delay", text, re.I):
        snapshot=context_text(state); match=re.search(r"(?:lock\s*delay|固定まで)[^\n]{0,80}?([0-9]+\s*ms)", snapshot, re.I)
        if not match:
            lines=snapshot.splitlines()
            for i,line in enumerate(lines):
                if re.search(r"lock\s*delay|固定まで|接地.*固定", line, re.I):
                    meaningful=0
                    for nxt in lines[i+1:i+7]:
                        if not nxt.strip() or re.fullmatch(r"\s*[-*_]{3,}\s*", nxt): continue
                        if re.match(r"\s*#{1,6}\s+", nxt): break
                        meaningful += 1
                        match=re.search(r"([0-9]+\s*ms)", nxt, re.I)
                        if match or meaningful >= 2: break
                    if match: break
        if match: return f"core contextによると、lock delayは{match.group(1)}です。"
    core = (context_text(state) if explicit_core else (None if len(text.strip()) < 80 else (context_text(state) or None)))
    image=state.image()
    image_relevant=bool(image and re.search(r"画像|スクショ|画面|見た目|ui|visual|screenshot|image", text, re.I))
    progress_label = "checking picture..." if image_relevant else "generating..."
    if image_relevant and sys.stdout.isatty(): print("checking picture...", flush=True)
    stop=threading.Event()
    def animate():
        frames='|/-\\'; i=0
        while not stop.wait(0.35):
            if sys.stdout.isatty():
                print(f"{progress_label} {frames[i%len(frames)]}\r",end='',flush=True); i+=1
    spinner=threading.Thread(target=animate,daemon=True); spinner.start()
    try:
        payload={"message":text,"core_context":core}
        if state.data.get("web_context"): payload["core_context"]=(payload.get("core_context") or "")+"\n[WEB_CONTEXT]\n"+state.data["web_context"].get("text","")
        if state.external(): payload["external"]=state.external()
        if image_relevant: payload["image"]=image
        result=api("POST", "/chat", payload)
        if image_relevant and any(c.get("stage")=="QWEN36_MAIN_MODEL" for c in result.get("task",{}).get("model_calls",[])):
            progress_label="generating..."
            if sys.stdout.isatty(): print("generating...", flush=True)
        return result["response"]
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
    if head=="help": output("/status · /option show|set|reset <brain|router|vision> · /workspace show|set <path> · /file set|show|clear · /image load|show|clear · /web open|show|clear|status · /external set|show|clear · /import external [to <path>] · /context show|set|load <path>|reload|clear · /models · /quit"); return 0
    if head=="status": show_status(state); return 0
    if head=="models":
        for k,v in runtime_status(state).items():
            if k in {"ollama","embeddinggemma:latest","semantic"} or k.startswith(("qwen3:","qwen2.5vl:")): output(f"{k}: {v}")
        return 0
    if head=="option":
        roles={"brain":"main_model","router":"router_model","vision":"vision_model"}
        try:
            current=api("GET","/settings")
            action=tail[0] if tail else "show"
            if action=="show":
                for role,key in roles.items():
                    model=current.get(key) or "(not configured)"
                    output(f"{role}: {model}")
                return 0
            if action not in {"set","reset"} or len(tail)<2 or tail[1] not in roles:
                output("Error: usage /option show|set <brain|router|vision> \"model\" | /option reset <brain|router|vision>"); return 2
            role,key=tail[1],roles[tail[1]]
            value=("qwen3:14b" if role=="brain" else "qwen2.5vl:3b" if role=="vision" else "") if action=="reset" else " ".join(tail[2:]).strip('"')
            if value:
                tags=json.load(request.urlopen("http://127.0.0.1:11434/api/tags",timeout=2)).get("models",[])
                if value not in {item.get("name") for item in tags}: output(f"Error: model unavailable: {value}"); return 2
            current[key]=value; api("PUT","/settings",current); output(f"{role}: {value or '(not configured)'}"); return 0
        except Exception as exc: output(f"Error: {exc}"); return 2
    if head=="file":
        action=tail[0] if tail else "show"
        if action=="set":
            try: output(f"Current file: {state.set_current_file(' '.join(tail[1:]))}")
            except (ValueError, PermissionError) as exc: output(f"Error: {exc}")
        elif action=="show": output(str(state.current_file() or "not configured"))
        elif action=="clear": state.clear_current_file(); output("Current file cleared.")
        else: output("Error: usage /file set \"path\" | /file show | /file clear")
        return 0
    if head=="image":
        action=tail[0] if tail else "show"
        if action=="load":
            try:
                image=state.load_image(" ".join(tail[1:]).strip().strip('"')); output(f"Image loaded: {image['filename']} ({image['mime_type']}, {image['byte_size']} bytes)")
            except (ValueError, PermissionError, OSError) as exc: output(f"Error: {exc}")
        elif action=="show":
            image=state.image(); output(json.dumps(image or {"status":"not configured"}, ensure_ascii=False))
        elif action=="clear": state.clear_image(); output("Image cleared.")
        else: output("Error: usage /image load \"absolute-path\" | /image show | /image clear")
        return 0
    if head=="web":
        action=tail[0] if tail else "status"
        if action=="open" and len(tail)>1:
            try:
                backend(state)
                result=api("POST","/web/fetch",{"url":" ".join(tail[1:]).strip('"')}); state.data["web_context"]={"url":result["requested_url"],"provider":result["provider"],"text":result["text"]}; state.save(); output(f"Web loaded: {result['requested_url']}")
            except Exception as exc: output(f"Error: {exc}")
        elif action in {"off", "manual", "auto"}:
            try:
                current=api("GET", "/settings"); current["web_mode"]=action; api("PUT", "/settings", current)
                output(f"WEB_MODE={action} EXTERNAL_NETWORK_ALLOWED={'YES' if action != 'off' else 'NO'}")
            except Exception as exc: output(f"Error: {exc}")
        elif action=="provider":
            sub=tail[1] if len(tail)>1 else "show"
            try:
                current=api("GET", "/settings")
                provider=current.get('web_provider','none'); key_name="OLCR_WEB_BRAVE_API_KEY" if provider=="brave" else "OLCR_WEB_TAVILY_API_KEY" if provider=="tavily" else ""
                if sub=="show": output(f"WEB_SEARCH_PROVIDER={provider} WEB_PROVIDER_READY={'YES' if provider != 'none' and bool(os.environ.get(key_name) or (provider=='brave' and os.environ.get('OLCR_WEB_SEARCH_API_KEY'))) else 'NO'} WEB_SEARCH_KEY_CONFIGURED={'YES' if bool(os.environ.get(key_name) or (provider=='brave' and os.environ.get('OLCR_WEB_SEARCH_API_KEY'))) else 'NO'}")
                elif sub=="brave": current["web_provider"]="brave"; api("PUT", "/settings", current); output("WEB_SEARCH_PROVIDER=brave")
                elif sub=="tavily": current["web_provider"]="tavily"; api("PUT", "/settings", current); output("WEB_SEARCH_PROVIDER=tavily")
                elif sub=="clear": current["web_provider"]="none"; api("PUT", "/settings", current); output("WEB_SEARCH_PROVIDER=none")
                else: output("Error: usage /web provider show|brave")
            except Exception as exc: output(f"Error: {exc}")
        elif action=="setup":
            output(setup_guidance())
        elif action=="show":
            try:
                current=api("GET", "/settings"); mode=current.get("web_mode", "off")
                output(f"WEB_MODE={mode} EXTERNAL_NETWORK_ALLOWED={'YES' if mode != 'off' else 'NO'}")
            except Exception as exc: output(f"Error: {exc}")
        elif action=="clear": state.data.pop("web_context",None); state.save(); output("Web context cleared.")
        elif action=="status":
            running="RUNNING" if OWNED_BACKEND and OWNED_BACKEND.poll() is None else "NOT_RUNNING"
            output(f"web: READY (HTTP provider); backend: {running}; browser: NOT_AVAILABLE")
        return 0
    if head=="external":
        action=tail[0] if tail else "show"
        if action=="set":
            try:
                source=" ".join(tail[1:]).strip().strip('"'); grant=state.set_external(source); output(f"External read grant: {grant['canonical_path']}")
            except (ValueError,OSError) as exc: output(f"Error: {exc}")
        elif action=="show": output(json.dumps(state.external() or {"status":"not configured"},ensure_ascii=False))
        elif action=="clear": state.clear_external(); output("External read grant cleared.")
        return 0
    if head=="import" and len(tail)>=1 and tail[0]=="external":
        try:
            dest=None
            if len(tail)>=3 and tail[1]=="to": dest=tail[2]
            output(f"Import: PASS — {state.import_external(dest)}")
        except (ValueError,PermissionError,FileExistsError,OSError) as exc: output(f"Error: {exc}")
        return 0
    if head=="workspace":
        if tail and tail[0]=="set":
            try: output(str(state.set_workspace(" ".join(tail[1:]))))
            except ValueError as exc: output(f"Error: {exc}")
            return 0
        output(str(state.workspace() or "not configured")); return 0
    if head=="context":
        action=tail[0] if tail else "show"
        if action=="show": output(json.dumps(state.context() or {"status":"not configured"},indent=2)); return 0
        if action=="set": state.set_context(multiline(input_fn,output),"direct"); output("Core context snapshot saved."); return 0
        if action=="load":
            try: state.load_context_file(" ".join(tail[1:]))
            except (ValueError, PermissionError, OSError) as exc: output(f"Error: {exc}"); return 0
            output("Core context file snapshot saved."); return 0
        if action=="reload": state.reload_context(); output("Core context snapshot reloaded."); return 0
        if action=="clear": state.clear_context(); output("Core context cleared."); return 0
    output("Unknown command. Type /help."); return 2

def repl(state,input_fn=input,output=print):
    ansi=sys.stdout.isatty() and os.environ.get("NO_COLOR") is None; output(banner(ansi,shutil.get_terminal_size((80,24)).columns<48));
    if not state.data.get("setup_complete") or not state.workspace(): wizard(state,input_fn,output)
    try:
        configure_backend(state)
        print("BACKEND_STARTUP_COMPLETE=YES", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"BACKEND_STARTUP_COMPLETE=NO: {exc}", file=sys.stderr, flush=True)
        output(f"Error: backend startup failed: {exc}")
        return 1
    show_status(state)
    pending=None
    try:
        while True:
            try:
                if pending is not None: value=pending; pending=None
                elif input_fn is input and PromptSession is not None:
                    if not hasattr(repl,"_session"): repl._session=PromptSession(enable_history_search=True)
                    value=repl._session.prompt("olcr> ")
                else: value=input_fn("olcr> ")
                value=value.strip()
                # input() owns exactly one submitted buffer; never infer
                # submission from content, length, wrapping, or language.
            except (EOFError,KeyboardInterrupt): output("\nGoodbye."); return 0
            if not value: continue
            if value in {"/quit","/exit"}: output("Goodbye."); return 0
            if value.startswith("/"):
                try: parts=shlex.split(value[1:])
                except ValueError as exc: output(f"Error: invalid command syntax ({exc})"); continue
                known={"help","status","models","file","image","workspace","context"}
                if parts and parts[0].lower() in known:
                    command(state,parts,input_fn,output); continue
                auto_image=_auto_image_path(value)
                if auto_image is not None:
                    command(state,["image","load",auto_image],input_fn,output); continue
                command(state,parts,input_fn,output); continue
            auto_image=_auto_image_path(value)
            if auto_image is not None:
                command(state,["image","load",auto_image],input_fn,output); continue
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
