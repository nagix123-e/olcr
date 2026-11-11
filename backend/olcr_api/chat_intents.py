from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SCRAPE_REQUEST = re.compile(
    r"(?:\b(?:scrape|scraping|crawl|web\s*scrap(?:e|ing))\b|"
    r"\b(?:extract|collect)\b.{0,40}\b(?:emails?|images?|metadata|social links?|page text|products?|sitemap|records?)\b|"
    r"スクレイピング|スクレイプ|(?:ページ|サイト|一覧|メール|画像|本文|SNS|サイトマップ).{0,12}(?:収集|抽出|取得|集め))",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"(?:\b(?:don't|do\s+not|never|without|not\s+yet|how\s+to|what\s+is)\b.{0,48}"
    r"\b(?:scrape|scraping|crawl|extract|collect)\b|"
    r"(?:スクレイピング|スクレイプ|収集|抽出|取得).{0,16}(?:しないで|しなくて|不要|やめて|禁止|とは|って何))",
    re.IGNORECASE,
)
_LOCAL_E2E_REQUEST = re.compile(
    r"(?:\b(?:start|run|launch|serve|open|attach|connect)\b.{0,40}\b(?:local\s+)?(?:e2e|end[- ]to[- ]end)\b|"
    r"\b(?:local\s+)?(?:e2e|end[- ]to[- ]end)\b.{0,40}\b(?:start|run|launch|serve|open|attach|connect)\b|"
    r"(?:実\s*(?:GUI\s*)?E(?:2E|to\s*E)|ローカルE(?:2E|to\s*E)).{0,40}(?:開始|起動|立ち上げ|動かして|接続して)|"
    r"(?:開始|起動|立ち上げ|動かして|接続して).{0,40}(?:実\s*(?:GUI\s*)?E(?:2E|to\s*E)|ローカルE(?:2E|to\s*E))|"
    r"(?:サイト|ウェブサイト|webサイト|ブラウザゲーム|ウェブアプリ|webアプリ).{0,30}E(?:2E|to\s*E).{0,40}(?:開始|起動|立ち上げ|動かして|接続して|停止|終了|状態|進捗|確認)|"
    r"(?:開始|起動|立ち上げ|動かして|接続して|停止|終了|状態|進捗|確認).{0,40}E(?:2E|to\s*E).{0,30}(?:サイト|ウェブサイト|webサイト|ブラウザゲーム|ウェブアプリ|webアプリ))",
    re.IGNORECASE,
)
_EXPLICIT_NATIVE_E2E = re.compile(
    r"(?:\b(?:e2e|end[- ]to[- ]end)\b|実\s*(?:gui\s*)?e(?:2e|to\s*e))",
    re.IGNORECASE,
)
_EXPLICIT_BROWSER_E2E = re.compile(
    r"https?://|\b(?:playwright|browser|website|site|web\s+app|web\s+game|local\s+browser|local\s+e2e)\b|"
    r"(?:ブラウザ|ウェブサイト|webサイト|サイト|ブラウザゲーム|ウェブアプリ|webアプリ|ローカルE(?:2E|to\s*E))",
    re.IGNORECASE,
)
_COMPUTER_USE_START = re.compile(
    r"(?:\b(?:start|begin|enable|launch)\s+(?:the\s+)?computer\s+use\b|"
    r"\bcomputer\s+use\b.{0,24}\b(?:start|begin|enable|launch)\b|"
    r"(?:コンピュータユーズ|コンピュータ使用|画面操作).{0,24}(?:開始|起動|始めて|有効にして)|"
    r"(?:開始|起動|始めて|有効にして).{0,24}(?:コンピュータユーズ|コンピュータ使用|画面操作))",
    re.IGNORECASE,
)
_COMPUTER_USE_CONTROL = re.compile(
    r"^\s*(?:(?:status|check|show)\s+(?:the\s+)?computer\s+use|computer\s+use\s+(?:status|check|show)|"
    r"(?:stop|end|disable)\s+(?:the\s+)?computer\s+use|"
    r"(?:コンピュータユーズ|コンピュータ使用|画面操作)(?:の)?(?:状態|状況)(?:を?(?:確認|表示|教えて)(?:して)?)?|"
    r"(?:コンピュータユーズ|コンピュータ使用|画面操作)(?:を)?(?:停止(?:して)?|終了(?:して)?|止めて|やめて))\s*[。.!！]*$",
    re.IGNORECASE,
)
_LOCAL_E2E_CONTROL = re.compile(
    r"^\s*(?:(?:status|check|show|stop|close|end)\s+(?:(?:the\s+)?(?:local\s+)?e2e|computer\s+use|browser|local\s+test)|"
    r"(?:(?:ローカル)?E(?:2E|to\s*E)|コンピュータユーズ|仮想ブラウザ)(?:の)?(?:状態|進捗|停止|終了|閉じる|止めて|状況)(?:を?(?:確認|表示|教えて|して)(?:して)?)?)\s*[。.!！]*$",
    re.IGNORECASE,
)
_LOCAL_E2E_CLICK = re.compile(
    r"^\s*(?:click|press|activate)\s+(?:the\s+)?(?:button\s+)?[\"'`]?(.+?)[\"'`]?\s*(?:button)?\s*[。.!！]*$|"
    r"^\s*[\"'「『](.+?)[\"'」』](?:ボタン)?(?:を)?(?:クリック(?:して)?|押して|押す|選択して)\s*[。.!！]*$",
    re.IGNORECASE,
)
_LOCAL_E2E_TYPE = re.compile(
    r"^\s*(?:type|fill|enter)\s+[\"'`「『](.+?)[\"'`」』]\s+(?:into|in)\s+"
    r"[\"'`「『](.+?)[\"'`」』]\s*[。.!！]*$|"
    r"^\s*[\"'「『](.+?)[\"'」』](?:欄|フィールド|入力欄)?(?:に|へ)"
    r"[\"'「『](.+?)[\"'」』](?:を)?(?:入力|記入|タイプ)(?:して|する)?\s*[。.!！]*$",
    re.IGNORECASE,
)
_LOCAL_E2E_NAVIGATE = re.compile(
    r"^\s*(?:navigate|go)\s+to\s+[\"'`「『](/[^\"'`」』\s]{0,500})[\"'`」』]\s*[。.!！]*$|"
    r"^\s*[\"'「『](/[^\"'「『」』\s]{0,500})[\"'」』](?:へ|に)(?:移動|進んで|開いて)(?:して)?\s*[。.!！]*$",
    re.IGNORECASE,
)
_LOCAL_E2E_WAIT_TEXT = re.compile(
    r"^\s*wait\s+for\s+(?:text\s+)?[\"'`「『](.{1,160}?)[\"'`」』]\s*[。.!！]*$|"
    r"^\s*[\"'「『](.{1,160}?)[\"'」』]が表示されるまで待(?:って|つ)\s*[。.!！]*$",
    re.IGNORECASE,
)
_LOCAL_E2E_ASSERT_TEXT = re.compile(
    r"^\s*(?:assert|verify|check)\s+(?:that\s+)?(?:text\s+)?[\"'`「『](.{1,160}?)[\"'`」』](?:\s+is\s+visible)?\s*[。.!！]*$|"
    r"^\s*[\"'「『](.{1,160}?)[\"'」』]が表示されていることを確認(?:して)?\s*[。.!！]*$",
    re.IGNORECASE,
)
_LOCAL_E2E_ACTION_REQUEST = re.compile(
    r"\b(?:click|press|tap|type|fill|enter|select|choose|open|start|begin|launch|play|continue|submit|"
    r"navigate|go\s+to|wait(?:\s+until)?|verify|check|scroll|proceed|finish|solve|complete|test)\b|"
    r"クリック|押し|入力|記入|選択|開いて|開始|始め|起動|プレイ|遊んで|続け|送信|移動|進め|"
    r"待って|確認|スクロール|解いて|クリア|完了|テストして|操作して",
    re.IGNORECASE,
)
_SCRAPE_CONTROL = re.compile(
    r"^\s*(?:(?:please\s+)?(?:pause|stop|resume|continue|status(?:\s+check)?|export|download)(?:\s+(?:the\s+)?(?:scrape|scraping|scraper|run|task|results?)(?:\s+results?)?)?|"
    r"(?:show|view|display)\s+(?:the\s+)?(?:scrape\s+)?results?|"
    r"(?:(?:スクレイピング|スクレイプ|収集|タスク|実行|結果)\s*を?)?(?:一時停止|止め|停止|再開|続け|進捗|状態|結果(?:を)?(?:表示|見せて|確認|ダウンロード|エクスポート|書き出し|出力))(?:して|を確認して|確認して|見せて|して|する)?)\s*[。.!！]*$",
    re.IGNORECASE,
)


def chat_scrape_control(message: str) -> str | None:
    """Resolve only short, explicit lifecycle controls for an active scrape run."""
    text = str(message or "").strip()
    if not text:
        return None
    selector = re.search(r"\s+#(\d+)\s*[。.!！]*$", text)
    if selector and not 1 <= int(selector.group(1)) <= 99:
        return None
    normalized = re.sub(r"\s+#\d+(?=\s*[。.!！]*$)", "", text)
    match = _SCRAPE_CONTROL.fullmatch(normalized)
    if not match:
        return None
    folded = text.casefold()
    if re.search(r"\b(?:pause)\b|一時停止", folded):
        return "pause"
    if re.search(r"\b(?:stop)\b|停止|止め", folded):
        return "stop"
    if re.search(r"\b(?:resume|continue)\b|再開|続け", folded):
        return "resume"
    if re.search(r"\b(?:export|download)\b|ダウンロード|エクスポート|書き出し|出力", folded):
        return "export"
    if re.search(r"\b(?:show|view|display)\b|結果", folded):
        return "results"
    return "status"


def chat_scrape_control_target_number(message: str) -> int | None:
    """Return a one-based run number from an explicit trailing ``#N`` selector."""
    match = re.search(r"\s+#(\d+)\s*[。.!！]*$", str(message or "").strip())
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 99 else None


def chat_scrape_readonly_click(message: str) -> str | None:
    """Resolve an explicit label for a same-origin public-page navigation/reveal control."""
    text = str(message or "").strip()
    patterns = (
        r"^\s*(?:click|press|activate)\s+(?:the\s+)?(?:link\s+|button\s+)?[\"'`](.{1,160}?)[\"'`]\s*(?:link|button)?\s*[。.!！]*$",
        r"^\s*[「『](.{1,160}?)[」』](?:リンク|ボタン)?(?:を)?(?:クリックして|クリックし|押して|選択して)\s*[。.!！]*$",
    )
    label = next((match.group(1) for pattern in patterns
                  if (match := re.fullmatch(pattern, text, re.IGNORECASE))), "")
    label = " ".join(label.split())
    if not label or len(label) > 160:
        return None
    return label


_SCRAPE_NAVIGATION_REQUEST = re.compile(
    r"^\s*(?:(?:please\s+)?(?:go|navigate|move|advance|continue|proceed|open|show|load|follow)\s+"
    r"(?:to\s+)?(?:the\s+)?(?:next|previous|more|older|newer)(?:\s+(?:page|results?|items?))?|"
    r"(?:next|previous)\s+(?:page|results?)|show\s+more(?:\s+(?:results?|items?|products?|posts?))?|"
    r"(?:次のページ|次へ|前のページ|前へ|もっと見る|さらに表示|続きを見る)"
    r"(?:へ|に|を)?(?:進んで|移動して|開いて|表示して|見せて|行って|クリックして|押して)?)[\s。.!！]*$",
    re.IGNORECASE,
)


def chat_scrape_readonly_navigation_request(message: str) -> str | None:
    """Resolve a narrow outcome-level request for one public-page read navigation."""
    text = " ".join(str(message or "").split())
    if not text or len(text) > 400 or chat_scrape_readonly_click(text):
        return None
    if not _SCRAPE_NAVIGATION_REQUEST.fullmatch(text):
        return None
    return text


def chat_scrape_request(message: str) -> dict[str, Any] | None:
    """Resolve an explicit extraction request for bounded HTTP(S) URLs."""
    text = str(message or "").strip()
    if not text or _NEGATED.search(text) or not _SCRAPE_REQUEST.search(text):
        return None
    urls = _URL.findall(text)
    if not urls or len(urls) > 500:
        return None
    cleaned = list(dict.fromkeys(url.rstrip(".,!?;:)]}、。！？") for url in urls))
    raw_url = cleaned[0]
    try:
        for url in cleaned:
            parsed = urlsplit(url)
            if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                return None
    except ValueError:
        return None
    instruction = _URL.sub(" ", text)
    instruction = re.sub(
        r"\b(?:please|start|begin|scrape|scraping|crawl|the|this|that|web|page|site|url)\b|"
        r"スクレイピング(?:を|して|開始|始め|お願い)?|スクレイプ(?:して|開始|始め)?|"
        r"(?:この|次の|以下の)(?:ページ|サイト|URL)?|(?:して|してください|開始して|始めて|お願いします)",
        " ", instruction, flags=re.IGNORECASE,
    )
    instruction = re.sub(r"[\s、。！？!?.:：,;]+", " ", instruction).strip()
    if not instruction:
        instruction = "Propose the main repeating records and their visible text/link fields."
    return {"url": raw_url, "instruction": instruction[:2000], **({"urls": cleaned} if len(cleaned) > 1 else {})}


def latest_chat_scrape_task(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest conversation-owned scrape task, if any."""
    for entry in reversed(history or []):
        blocks = entry.get("blocks") or []
        for block in reversed(blocks):
            if isinstance(block, dict) and block.get("type") == "scrape_task":
                return block
    return None


def chat_scrape_clarification(message: str, history: list[dict[str, Any]]) -> dict[str, str] | None:
    """Resolve a free-form answer to the latest pending chat scrape clarification."""
    text = str(message or "").strip()
    if not text or len(text) > 2000:
        return None
    if chat_scrape_control(text):
        return None

    latest_task = latest_chat_scrape_task(history)
    if (not latest_task or latest_task.get("needs_clarification") is not True
            or latest_task.get("run_id") or not latest_task.get("session_id")):
        return None

    urls = [url.rstrip(".,!?;:)]}、。！？") for url in _URL.findall(text)]
    pending_url = str(latest_task.get("url") or "").rstrip(".,!?;:)]}、。！？")
    # Repeating the same URL while refining the requested fields should reuse
    # the chat-owned browser. A different URL remains a new scrape request.
    if urls and any(url != pending_url for url in urls):
        return None

    clarification = _URL.sub(" ", text).strip()[:1200]
    if not clarification:
        return None
    suffix = ("\n\nLatest user clarification (takes precedence where it conflicts): "
              f"{clarification}")
    original = str(latest_task.get("instruction") or "")[:max(0, 2000 - len(suffix))]
    return {
        "session_id": str(latest_task["session_id"])[:160],
        "url": str(latest_task.get("url") or "")[:2000],
        "instruction": (original + suffix)[:2000],
    }


def chat_local_e2e_request(message: str) -> dict[str, str | None] | None:
    """Resolve an explicit chat request to start or attach to the selected project's local E2E."""
    text = str(message or "").strip()
    if not text or _SCRAPE_REQUEST.search(text) or not _LOCAL_E2E_REQUEST.search(text):
        return None
    urls = _URL.findall(text)
    if len(urls) > 1:
        return None
    origin = urls[0].rstrip(".,!?;:)]}、。！？") if urls else None
    if origin:
        try:
            parsed = urlsplit(origin)
            if parsed.scheme.lower() != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
                return None
        except ValueError:
            return None
    mode = "attach" if re.search(r"\battach\b|接続して|既存.*(?:サーバ|アプリ)", text, re.I) else "start"
    script_match = re.search(
        r"\b(?:using|with)\s+(?:(?:npm|pnpm|yarn)\s+run\s+)?(dev|start|serve)(?:\s+script)?\b|"
        r"(?:スクリプト|script)\s*(?:は|を|=|:)?\s*(dev|start|serve)\b",
        text, re.I)
    script = next((group for group in script_match.groups() if group), None) if script_match else None
    command_match = re.search(
        r"\bcommand\s*[:=]?\s*[`\"“](.{1,2000}?)[`\"”]|"
        r"コマンド\s*[:=：]?\s*[「『`\"“](.{1,2000}?)[」』`\"”]",
        text, re.IGNORECASE | re.DOTALL,
    )
    command = next((group for group in command_match.groups() if group), None) if command_match else None
    if command and ("\n" in command or "\r" in command):
        return None
    request = {"mode": mode, "origin": origin, "script": script.lower() if script else None}
    if command:
        request["command"] = command.strip()
    return request


def chat_local_e2e_script_choice(message: str, history: list[dict]) -> dict[str, str | None] | None:
    """Resolve a short script choice only after this conversation asked for one."""
    selection = re.fullmatch(
        r"\s*(?:(?:use|choose|select)\s+)?(dev|start|serve)(?:\s+script)?(?:\s*(?:please|を使って|にして|で|でお願いします))?\s*[。.!！]*\s*",
        str(message or ""), re.IGNORECASE)
    if not selection:
        return None
    chosen = selection.group(1).lower()
    assistant_index = next((index for index in range(len(history) - 1, -1, -1)
                            if history[index].get("role") == "assistant"), None)
    if assistant_index is None:
        return None
    prompt = re.search(r"起動候補が複数あります[（(]([^）)]+)[）)]",
                       str(history[assistant_index].get("content") or ""))
    if not prompt:
        return None
    candidates = {part.strip().lower() for part in prompt.group(1).split(",")}
    if chosen not in candidates:
        return None
    original = next((entry for entry in reversed(history[:assistant_index])
                     if entry.get("role") == "user"), None)
    request = chat_local_e2e_request(str((original or {}).get("content") or ""))
    if not request or request.get("mode") != "start":
        return None
    return {**request, "script": chosen}


def chat_computer_use_intent(message: str) -> str | None:
    """Resolve only direct native Computer Use session lifecycle commands."""
    text = str(message or "").strip()
    if not text or _SCRAPE_REQUEST.search(text):
        return None
    # An explicit native/real-GUI E2E request belongs to Computer Use unless
    # the user names a browser/Playwright target. Resolve it before the broad
    # local-E2E phrase matcher can claim it.
    native_e2e_start = (
        # Python's Unicode-aware ``\b`` does not see a boundary between the
        # ASCII word "Use" and an immediately following Japanese particle
        # (for example, "Computer Useで実GUI E2Eを開始して"). Accept common
        # particles explicitly so frontend/backend chat intent validation
        # agrees for natural Japanese requests.
        re.search(r"\bcomputer\s+use(?=\s|$|[、。,.!?]|で|を|に|の|は|が|として|オン)"
                  r"|コンピュータユーズ|コンピュータ使用|画面操作", text, re.I)
        and _EXPLICIT_NATIVE_E2E.search(text)
        and not _EXPLICIT_BROWSER_E2E.search(text)
        and re.search(r"\b(?:start|begin|launch|run)\b|開始|起動|立ち上げ|始めて|動かして", text, re.I)
    )
    if native_e2e_start:
        return "start"
    if _LOCAL_E2E_REQUEST.search(text):
        return None
    if re.search(r"\b(?:e2e|end[- ]to[- ]end|playwright|local\s+browser)\b|実\s*(?:GUI\s*)?E(?:2E|to\s*E)|ローカルE(?:2E|to\s*E)", text, re.I):
        return None
    match = _COMPUTER_USE_CONTROL.fullmatch(text)
    if match:
        folded = text.casefold()
        if re.search(r"\b(?:stop|end|disable)\b|停止|終了|止め|やめ", folded):
            return "stop"
        return "status"
    if _COMPUTER_USE_START.search(text):
        return "start"
    return None


def chat_local_e2e_control(message: str) -> str | None:
    text = str(message or "").strip()
    match = _LOCAL_E2E_CONTROL.fullmatch(text)
    if not match:
        return None
    folded = text.casefold()
    if re.search(r"\bstop\b|\bclose\b|\bend\b|停止|終了|閉じる|止め", folded):
        return "stop"
    return "status"


def chat_local_e2e_click(message: str) -> str | None:
    match = _LOCAL_E2E_CLICK.fullmatch(str(message or "").strip())
    if not match:
        return None
    label = next((value for value in match.groups() if value), "")
    label = " ".join(label.strip().split())
    if not label or len(label) > 160 or label.casefold() in {"a", "button", "the button", "ボタン", "ボタンを"}:
        return None
    return label


def chat_local_e2e_type(message: str) -> dict[str, str] | None:
    """Resolve an explicit quoted value and accessible field label for local E2E."""
    match = _LOCAL_E2E_TYPE.fullmatch(str(message or "").strip())
    if not match:
        return None
    if match.group(1) is not None:
        value, label = match.group(1), match.group(2)
    else:
        label, value = match.group(3), match.group(4)
    label = " ".join(label.strip().split())
    value = value.strip()
    if (not label or len(label) > 160 or not value or len(value) > 1000
            or label.casefold() in {"input", "field", "textbox", "入力欄", "フィールド"}):
        return None
    return {"label": label, "value": value}


_LOCAL_E2E_ACTION_SEPARATOR = re.compile(
    r"(?:\s*(?:;|；|->|→|,|，|、)\s*|\s*\b(?:and\s+then|then|and)\b\s*|"
    r"\s*(?:それから|その後|次に|続けて|確認してから|してから|から)\s*)",
    re.IGNORECASE,
)


def _split_quoted_local_e2e_actions(text: str) -> list[str]:
    pairs = {'"': '"', "'": "'", "`": "`", "「": "」", "『": "』"}
    parts: list[str] = []
    start = 0
    quote_end: str | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if quote_end:
            if character == quote_end:
                quote_end = None
            index += 1
            continue
        if character in pairs:
            quote_end = pairs[character]
            index += 1
            continue
        separator = _LOCAL_E2E_ACTION_SEPARATOR.match(text, index)
        if separator:
            parts.append(text[start:index].strip(" \t,，、。"))
            index = separator.end()
            start = index
            continue
        index += 1
    if quote_end:
        return []
    parts.append(text[start:].strip(" \t,，、。"))
    return [part for part in parts if part]


def chat_local_e2e_action_sequence(message: str) -> list[dict[str, str]] | None:
    """Resolve a short, explicitly delimited sequence of grounded local-E2E actions."""
    text = str(message or "").strip()
    parts = _split_quoted_local_e2e_actions(text)
    if not parts or len(parts) > 6:
        return None
    actions: list[dict[str, str]] = []
    for part in parts:
        click = chat_local_e2e_click(part)
        if click:
            actions.append({"kind": "click", "label": click})
            continue
        typed = chat_local_e2e_type(part)
        if typed:
            actions.append({"kind": "type", **typed})
            continue
        navigate = _LOCAL_E2E_NAVIGATE.fullmatch(part)
        if navigate:
            path = next((value for value in navigate.groups() if value), "")
            if path.startswith("//") or "\\" in path or any(ord(char) < 0x20 for char in path):
                return None
            actions.append({"kind": "navigate", "path": path})
            continue
        wait_text = _LOCAL_E2E_WAIT_TEXT.fullmatch(part)
        if wait_text:
            text = next((value for value in wait_text.groups() if value), "").strip()
            if not text:
                return None
            actions.append({"kind": "wait_for_text", "text": text})
            continue
        assert_text = _LOCAL_E2E_ASSERT_TEXT.fullmatch(part)
        if assert_text:
            text = next((value for value in assert_text.groups() if value), "").strip()
            if not text:
                return None
            actions.append({"kind": "assert_text", "text": text})
            continue
        return None
    return actions if 2 <= len(actions) <= 6 or (len(actions) == 1 and actions[0]["kind"] in {
        "navigate", "wait_for_text", "assert_text"}) else None


def chat_local_e2e_action_request(message: str) -> bool:
    """Identify natural-language browser action requests for the scoped planner."""
    text = str(message or "").strip()
    if not text or len(text) > 2000 or _SCRAPE_REQUEST.search(text):
        return False
    if re.match(r"^(?:what|why|how|which|where|when|who|does|do|is|are)\b", text, re.I) and "?" in text:
        return False
    return bool(_LOCAL_E2E_ACTION_REQUEST.search(text))


def validate_chat_local_e2e_plan(plan: Any, instruction: str,
                                 candidates: list[dict[str, Any]], origin: str,
                                 current_visible_text: str = "") -> dict[str, Any]:
    """Validate typed model output against user text and current visible browser evidence."""
    if not isinstance(plan, dict) or plan.get("decision") not in {"execute", "clarify"}:
        raise ValueError("LOCAL_E2E_PLAN_INVALID")
    if plan["decision"] == "clarify":
        clarification = " ".join(str(plan.get("clarification") or "").split())[:400]
        return {"decision": "clarify", "clarification": clarification or
                "操作対象か確認したい内容をもう少し具体的に教えてください。", "actions": []}
    actions = plan.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 6:
        raise ValueError("LOCAL_E2E_PLAN_ACTION_COUNT_INVALID")

    visible = [candidate for candidate in candidates if isinstance(candidate, dict)]
    instruction_folded = " ".join(str(instruction or "").split()).casefold()
    validated: list[dict[str, str]] = []
    action_before_outcome_check = False
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("LOCAL_E2E_PLAN_ACTION_INVALID")
        kind = action.get("kind")
        if kind in {"click", "type"}:
            label = " ".join(str(action.get("label") or "").split())
            if not label or len(label) > 160:
                raise ValueError("LOCAL_E2E_PLAN_TARGET_INVALID")
            if kind == "click":
                matches = [item for item in visible if item.get("kind") in {"control", "navigation"}
                           and item.get("tag") in {"button", "a"}
                           and " ".join(str(item.get("text") or "").split()).casefold() == label.casefold()]
                if len(matches) != 1:
                    raise ValueError("LOCAL_E2E_PLAN_CLICK_NOT_UNIQUELY_GROUNDED")
                grounded_label = " ".join(str(matches[0].get("text") or "").split())
                validated.append({"kind": "click", "label": grounded_label})
                action_before_outcome_check = True
            else:
                value = str(action.get("value") or "")
                if not value or len(value) > 500 or " ".join(value.split()).casefold() not in instruction_folded:
                    raise ValueError("LOCAL_E2E_PLAN_INPUT_VALUE_NOT_USER_PROVIDED")
                matches = [item for item in visible if item.get("kind") == "field"
                           and item.get("tag") in {"input", "textarea", "select"}
                           and " ".join(str(item.get("text") or "").split()).casefold() == label.casefold()]
                if len(matches) != 1:
                    raise ValueError("LOCAL_E2E_PLAN_FIELD_NOT_UNIQUELY_GROUNDED")
                field_type = str((matches[0].get("attributes") or {}).get("type") or "text").casefold()
                if field_type in {"hidden", "password", "file", "checkbox", "radio", "button", "submit", "reset", "image"}:
                    raise ValueError("LOCAL_E2E_PLAN_FIELD_TYPE_UNSUPPORTED")
                grounded_label = " ".join(str(matches[0].get("text") or "").split())
                validated.append({"kind": "type", "label": grounded_label, "value": value})
                action_before_outcome_check = True
        elif kind == "navigate":
            path = str(action.get("path") or "")
            if (not path.startswith("/") or path.startswith("//") or len(path) > 500
                    or "\\" in path or any(ord(char) < 0x20 for char in path)):
                raise ValueError("LOCAL_E2E_PLAN_NAVIGATION_INVALID")
            href_matches = []
            for item in visible:
                attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
                href = str(attributes.get("href") or "")
                try:
                    parsed = urlsplit(href)
                    canonical = f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}"
                    if parsed.port and parsed.port != 80:
                        canonical += f":{parsed.port}"
                    same_origin_path = (parsed.scheme.lower() == "http" and canonical == origin
                                        and parsed.path == path and not parsed.username and not parsed.password)
                except ValueError:
                    same_origin_path = False
                if item.get("kind") == "navigation" and same_origin_path:
                    href_matches.append(item)
            if len(href_matches) != 1:
                raise ValueError("LOCAL_E2E_PLAN_NAVIGATION_NOT_GROUNDED")
            validated.append({"kind": "navigate", "path": path})
            action_before_outcome_check = True
        elif kind in {"wait_for_text", "assert_text"}:
            expected = " ".join(str(action.get("text") or "").split())
            if (not expected or len(expected) > 160
                    or (expected.casefold() not in instruction_folded and not action_before_outcome_check)):
                raise ValueError("LOCAL_E2E_PLAN_EXPECTED_TEXT_NOT_USER_PROVIDED")
            if (expected.casefold() not in instruction_folded
                    and expected.casefold() in " ".join(str(current_visible_text or "").split()).casefold()):
                raise ValueError("LOCAL_E2E_PLAN_INFERRED_OUTCOME_ALREADY_VISIBLE")
            validated.append({"kind": str(kind), "text": expected})
            action_before_outcome_check = False
        else:
            raise ValueError("LOCAL_E2E_PLAN_ACTION_KIND_UNSUPPORTED")
    return {"decision": "execute", "actions": validated}
