"""Read-only launch guidance for the workspace referred to in the current turn."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import stat

from .coding_tasks import request_instruction_text
from .package_manager import SUPPORTED_PACKAGE_MANAGERS

OWNER = "WORKSPACE_LAUNCH_HELP"
_LIMIT = 256 * 1024
_QUESTION = re.compile(
    r"(?:ターミナル|コマンドライン|端末|ローカル|手元)(?:から|で)\s*"
    r"(?:(?:これ|それ|(?:この|その)(?:アプリ|サイト|プロジェクト|成果物))(?:を)?\s*)?"
    r"(?:起動|実行|動かす|開く)(?:する)?(?:方法|手順|やり方)(?:を(?:教えて(?:ください)?|説明して(?:ください)?|知りたい))?"
    r"|(?:(?:これ|それ|(?:この|その)(?:アプリ|サイト|プロジェクト|成果物))(?:を|の)?\s*)"
    r"(?:(?:ターミナル|ローカル)(?:から|で)\s*)?"
    r"(?:起動|実行|動かす|開く)(?:する)?(?:方法|手順|やり方)(?:を(?:教えて(?:ください)?|説明して(?:ください)?|知りたい))?"
    r"|(?:how (?:do|can|should) i|how to|(?:please )?(?:show|tell) me how to) "
    r"(?:run|start|launch|serve|open) (?:this|it|(?:this|the) (?:app(?:lication)?|site|website|project|dev server))"
    r"(?: locally| (?:from|in) (?:a |the )?(?:terminal|command line))?",
    re.I,
)
_CONTEXTUAL_QUESTION = re.compile(
    r"(?:^|[、,。！？!?;；\s])(?:開き方|開く方法|開くには|起動方法|起動する方法|起動の仕方|"
    r"実行方法|実行する方法|実行の仕方|動かし方|動かす方法)"
    r"(?:\s*(?:を)?\s*(?:教えて(?:ください)?|説明して(?:ください)?|知りたい|お願いします))?"
    r"\s*[。！？?!]*$",
    re.I,
)
_EXPLICIT_EXTERNAL_LOOKUP = re.compile(
    r"(?:\bweb\b|ネット|インターネット|Wikipedia|ウィキペディア|"
    r"検索|調べて|最新|今日|本日|最近|リアルタイム|\b(?:latest|current|news|search)\b)",
    re.I,
)
_WEB_DEPENDENCIES = {
    "@angular/core", "astro", "gatsby", "next", "nuxt", "react", "svelte",
    "vite", "vue",
}
_DESKTOP_DEPENDENCIES = {
    "@tauri-apps/cli", "@tauri-apps/api", "electron", "electron-builder",
    "neutralinojs",
}


def requested(message: str, *, completed_coding_task_context: bool = False) -> bool:
    text = request_instruction_text(message).strip(" \t\r\n。！？?!．.")
    if _QUESTION.fullmatch(text):
        return True
    # Short forms such as "開き方を教えて" have no object to match in the
    # current message. Resolve that omitted object only when the immediately
    # preceding assistant turn belongs to a completed Coding Task.
    return bool(completed_coding_task_context and not _EXPLICIT_EXTERNAL_LOOKUP.search(text)
                and _CONTEXTUAL_QUESTION.search(text))


def _read_manifest(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("package.json is not a regular file")
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("package.json exceeds the read limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("package.json is not an object")
    return result


def _regular(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _manager(root: Path, manifest: dict) -> str | None:
    declared = manifest.get("packageManager")
    if declared:
        name = str(declared).split("@", 1)[0]
        return name if name in SUPPORTED_PACKAGE_MANAGERS else None
    names = [
        name for name, files in (
            ("npm", ("package-lock.json", "npm-shrinkwrap.json")),
            ("pnpm", ("pnpm-lock.yaml",)),
            ("yarn", ("yarn.lock",)),
        ) if any(_regular(root / filename) for filename in files)
    ]
    return names[0] if len(names) == 1 else None


def _package_dependencies(manifest: dict) -> set[str]:
    result: set[str] = set()
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        value = manifest.get(field)
        if isinstance(value, dict):
            result.update(str(name) for name in value)
    return result


def _app_bundle(root: Path) -> Path | None:
    """Find a built macOS app in conventional output folders without following links."""
    parents = (
        root, root / "dist", root / "dist" / "mac", root / "dist" / "mac-arm64",
        root / "build", root / "build" / "Debug", root / "build" / "Products" / "Debug",
        root / "out", root / "release",
        root / "release" / "mac", root / "release" / "mac-arm64",
        root / "target" / "debug" / "bundle" / "macos",
        root / "target" / "release" / "bundle" / "macos",
        root / "src-tauri" / "target" / "debug" / "bundle" / "macos",
        root / "src-tauri" / "target" / "release" / "bundle" / "macos",
    )
    for parent in parents:
        try:
            if parent.is_symlink() or not parent.is_dir():
                continue
            candidates = sorted(parent.glob("*.app"), key=lambda item: item.name)
        except OSError:
            continue
        for candidate in candidates:
            try:
                contents = candidate / "Contents"
                if (candidate.is_symlink() or not candidate.is_dir()
                        or not candidate.resolve().is_relative_to(root)
                        or contents.is_symlink()
                        or not _regular(contents / "Info.plist")):
                    continue
            except (OSError, RuntimeError):
                continue
            return candidate
    return None


def _script_command(root: Path, manifest: dict, names: tuple[str, ...]) -> tuple[str, str] | None:
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return None
    script = next((name for name in names if isinstance(scripts.get(name), str) and scripts[name].strip()), None)
    manager = _manager(root, manifest)
    if not script or not manager:
        return None
    return manager, script


def has_package_launcher(root: Path) -> bool:
    """Whether package metadata identifies a local app/site start script."""
    try:
        manifest = _read_manifest(Path(root) / "package.json")
    except (OSError, ValueError):
        return False
    return _script_command(root, manifest, ("tauri:dev", "desktop:dev", "electron:dev", "dev", "start")) is not None


def _desktop_launch_command(root: Path, manifest: dict, *, tauri: bool) -> tuple[str, str] | None:
    manager = _manager(root, manifest)
    if not manager:
        return None
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return None
    for script in ("tauri:dev", "desktop:dev", "electron:dev"):
        if isinstance(scripts.get(script), str) and scripts[script].strip():
            return manager, f"{manager} run {script}"
    if tauri and isinstance(scripts.get("tauri"), str) and scripts["tauri"].strip():
        # Tauri's scaffold uses `dev: vite` for the webview and `tauri: tauri`
        # for the CLI. Forward `dev` to the CLI so this opens the native window.
        return manager, f"{manager} run tauri -- dev"
    if not tauri:
        for script in ("dev", "start"):
            if isinstance(scripts.get(script), str) and scripts[script].strip():
                return manager, f"{manager} run {script}"
    return None


def _install_command(manager: str, root: Path) -> str:
    if (root / "node_modules").is_dir():
        return ""
    if manager == "npm":
        return "npm ci" if _regular(root / "package-lock.json") else "npm install"
    if manager == "pnpm":
        return "pnpm install --frozen-lockfile" if _regular(root / "pnpm-lock.yaml") else "pnpm install"
    if manager == "yarn":
        return "yarn install --immutable" if _regular(root / "yarn.lock") else "yarn install"
    return f"{manager} install"


def _web_port(manifest: dict, script: str) -> int | None:
    scripts = manifest.get("scripts")
    command = scripts.get(script, "") if isinstance(scripts, dict) else ""
    match = re.search(r"(?:--port(?:=|\s+)|\s-p\s+)(\d{1,5})\b", str(command))
    if match:
        port = int(match.group(1))
        return port if 1 <= port <= 65535 else None
    dependencies = _package_dependencies(manifest)
    if "vite" in dependencies or re.search(r"\bvite\b", str(command)):
        return 5173
    if "next" in dependencies or re.search(r"\bnext\s+dev\b", str(command)):
        return 3000
    return None


def completion_guidance(root: Path | None, *, japanese: bool = True,
                        goal: str = "", plan: dict | None = None) -> str | None:
    """Return evidence-based app/site opening instructions for a Coding Task report."""
    is_site_goal = bool(re.search(r"\b(?:website|web site|site|landing page|homepage)\b|ウェブサイト|webサイト|ホームページ|ランディングページ|サイト", goal, re.I))
    is_app_goal = bool(re.search(r"\b(?:desktop app|native app|application|app)\b|デスクトップアプリ|ネイティブアプリ|アプリ", goal, re.I))
    if root is None:
        if is_site_goal:
            return ("## Webサイトを開く\n\n作業フォルダを確認できませんでした。"
                    if japanese else "## Open the website\n\nI couldn't locate the workspace.")
        if is_app_goal:
            return ("## アプリを開く\n\n作業フォルダを確認できませんでした。"
                    if japanese else "## Open the app\n\nI couldn't locate the workspace.")
        return None
    try:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            return None
    except (OSError, RuntimeError):
        return None

    app = _app_bundle(root)
    if app and is_app_goal:
        command = "open " + shlex.quote(str(app))
        if japanese:
            return ("## アプリを開く\n\nFinder からも開けます。ターミナルで次を実行すると、生成済みのアプリが起動します。\n\n"
                    f"```sh\n{command}\n```")
        return ("## Open the app\n\nOpen it from Finder, or run this command to launch the built app.\n\n"
                f"```sh\n{command}\n```")

    manifest: dict | None = None
    try:
        manifest = _read_manifest(root / "package.json")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        manifest = None

    dependencies = _package_dependencies(manifest) if manifest else set()
    tauri_project = ((root / "src-tauri" / "tauri.conf.json").is_file()
                     or bool(dependencies & {"@tauri-apps/cli", "@tauri-apps/api"}))
    electron_project = bool(dependencies & {"electron", "electron-builder", "neutralinojs"})
    app_project = (tauri_project or electron_project) and (not is_site_goal or is_app_goal)

    if manifest and app_project:
        selected = _desktop_launch_command(root, manifest, tauri=tauri_project)
        if selected:
            manager, run_command = selected
            parts = [part for part in (_install_command(manager, root), run_command) if part]
            command = "cd " + shlex.quote(str(root)) + " && " + " && ".join(parts)
            if japanese:
                return ("## アプリを起動\n\n次のコマンドで開発版アプリを起動すると、アプリのウィンドウが開きます。\n\n"
                        f"```sh\n{command}\n```\n\n終了するにはターミナルで Ctrl+C を押してください。")
            return ("## Launch the app\n\nRun this command to start the development app window.\n\n"
                    f"```sh\n{command}\n```\n\nPress Ctrl+C in the terminal to stop it.")

    static_html = next((root / str(item.get("path")) for item in (plan or {}).get("file_manifest", [])
                        if isinstance(item, dict) and isinstance(item.get("path"), str)
                        and str(item["path"]).lower().endswith((".html", ".htm"))), None)
    if static_html is None and _regular(root / "index.html"):
        static_html = root / "index.html"
    if static_html is None:
        static_html = next((candidate for candidate in (
            root / "dist" / "index.html", root / "build" / "index.html", root / "out" / "index.html",
        ) if _regular(candidate)), None)
    package_web_script = bool(manifest and dependencies & _WEB_DEPENDENCIES
                              and _script_command(root, manifest, ("dev", "start")))
    if package_web_script:
        static_html = None
    if static_html is not None:
        try:
            target = static_html.resolve()
            if (not target.is_relative_to(root) or not target.is_file()
                    or static_html.is_symlink()):
                static_html = None
            else:
                relative = target.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            static_html = None
    if static_html is not None:
        command = "python3 -m http.server 8000 --bind 127.0.0.1 --directory " + shlex.quote(str(root))
        from urllib.parse import quote
        url = "http://127.0.0.1:8000/" + quote(relative)
        if japanese:
            return ("## Webサイトを開く\n\nターミナルで次を実行し、ブラウザーでURLを開いてください。\n\n"
                    f"```sh\n{command}\n```\n\n{url}\n\n終了するにはターミナルで Ctrl+C を押してください。")
        return ("## Open the website\n\nRun this command, then open the URL in a browser.\n\n"
                f"```sh\n{command}\n```\n\n{url}\n\nPress Ctrl+C in the terminal to stop it.")

    web_project = bool(dependencies & _WEB_DEPENDENCIES) or _regular(root / "index.html")
    if manifest and web_project:
        selected = _script_command(root, manifest, ("dev", "start"))
        if selected:
            manager, script = selected
            parts = [part for part in (_install_command(manager, root), f"{manager} run {script}") if part]
            command = "cd " + shlex.quote(str(root)) + " && " + " && ".join(parts)
            port = _web_port(manifest, script)
            url = f"http://localhost:{port}/" if port else None
            if japanese:
                intro = "ターミナルで次を実行し、ブラウザーでサイトを開いてください。"
                if not url:
                    intro += " 起動後、ターミナルに表示されるURLを開いてください。"
                suffix = f"\n\n{url}" if url else ""
                return ("## Webサイトを開く\n\n" + intro + "\n\n"
                        f"```sh\n{command}\n```" + suffix
                        + "\n\n終了するにはターミナルで Ctrl+C を押してください。")
            intro = "Run this command, then open the site in a browser."
            if not url:
                intro += " After startup, use the URL printed in the terminal."
            suffix = f"\n\n{url}" if url else ""
            return ("## Open the website\n\n" + intro + "\n\n"
                    f"```sh\n{command}\n```" + suffix
                    + "\n\nPress Ctrl+C in the terminal to stop it.")

    # For app/site requests that cannot be tied to a real entry point, keep the
    # section visible and state the missing evidence instead of inventing a command.
    if is_site_goal:
        return ("## Webサイトを開く\n\n作業フォルダ内に起動スクリプトや公開用ファイルを確認できませんでした。"
                if japanese else "## Open the website\n\nI couldn't identify a launch script or built website in the workspace.")
    if is_app_goal:
        return ("## アプリを開く\n\n作業フォルダ内に起動スクリプトや生成済みアプリを確認できませんでした。"
                if japanese else "## Open the app\n\nI couldn't identify a launch script or built app in the workspace.")
    return None


def answer(root: Path | None, *, japanese: bool) -> str:
    if root is None:
        return ("起動対象の作業フォルダを確認できません。対象のプロジェクトを開くか、成果物のフォルダを指定してください。"
                if japanese else "I couldn't locate the workspace to launch. Open the project or specify the output folder.")
    command = note = None
    manifest_path = root / "package.json"
    try:
        manifest = _read_manifest(manifest_path)
    except FileNotFoundError:
        manifest = None
    except (OSError, ValueError):
        return ("作業フォルダの package.json を読み取れないため、起動コマンドを確認できません。"
                if japanese else "I couldn't read package.json in the workspace to identify its launch command.")
    if manifest is not None:
        scripts = manifest.get("scripts")
        manager = _manager(root, manifest)
        if isinstance(scripts, dict):
            script = next((key for key in ("dev", "start") if isinstance(scripts.get(key), str) and scripts[key].strip()), None)
            if script:
                if not manager:
                    return ("package.json に起動スクリプトがありますが、使用するパッケージマネージャーを特定できません。使用するものを教えてください。"
                            if japanese else "package.json defines a launch script, but I couldn't identify the package manager. Which one does the project use?")
                command = f"{manager} run {script}"
        if not command and _regular(root / "index.html") and any(
            isinstance(manifest.get(key), dict) and "vite" in manifest[key] for key in ("dependencies", "devDependencies")
        ):
            command = "./node_modules/.bin/vite --host 127.0.0.1"
            note = ("起動スクリプトがないため、インストール済みの Vite を直接使います。Vite が未インストールの場合は、先に依存関係をセットアップしてください。"
                    if japanese else "The project has no launch script. This uses the locally installed Vite; set up dependencies first if it is missing.")
    elif _regular(root / "index.html"):
        command = "python3 -m http.server 8000 --bind 127.0.0.1"
        note = ("Python 3 が必要です。ブラウザーで http://127.0.0.1:8000/ を開いてください。"
                if japanese else "Requires Python 3. Open http://127.0.0.1:8000/ in your browser.")
    if not command:
        return ("作業フォルダの構成から起動コマンドを確認できません。使用するフレームワークか、起動手順が書かれたファイルを教えてください。"
                if japanese else "I couldn't identify a launch command from the workspace. Which framework or setup document should I use?")
    intro = "作業フォルダの構成に基づく起動手順です。" if japanese else "To launch from the current workspace:"
    tail = "起動コマンドは実行していません。" if japanese else "I haven't run the launch command."
    return f"{intro}\n\n```sh\ncd {shlex.quote(str(root))} && {command}\n```\n\n" + (f"{note}\n\n" if note else "") + tail
