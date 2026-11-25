"""Canonical aliases for the languages supported by PLaMo 3 Translate."""
from __future__ import annotations

from collections import Counter
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


# PLaMo 3 Translate 2B model card, plus the existing Japanese(easy) option.
_LANGUAGES = (
    ("Japanese", "ja", ("日本語",)),
    ("English", "en", ("英語",)),
    ("Chinese (Simplified)", "zh-hans", ("Simplified Chinese", "简体中文", "簡体中文", "簡体字中国語")),
    ("Chinese (Traditional)", "zh-hant", ("Traditional Chinese", "Taiwanese", "繁体中文", "繁體中文", "繁体字中国語", "台湾華語")),
    ("Korean", "ko", ("韓国語", "朝鮮語")),
    ("Arabic", "ar", ("アラビア語",)),
    ("Azerbaijani", "az", ("アゼルバイジャン語",)),
    ("Belarusian", "be", ("ベラルーシ語",)),
    ("Bulgarian", "bg", ("ブルガリア語",)),
    ("Bengali", "bn", ("ベンガル語",)),
    ("Catalan", "ca", ("カタルーニャ語",)),
    ("Cebuano", "ceb", ("セブアノ語",)),
    ("German", "de", ("ドイツ語",)),
    ("Spanish", "es", ("スペイン語",)),
    ("Estonian", "et", ("エストニア語",)),
    ("Basque", "eu", ("バスク語",)),
    ("Persian", "fa", ("ペルシア語", "ペルシャ語")),
    ("French", "fr", ("フランス語",)),
    ("Gujarati", "gu", ("グジャラート語",)),
    ("Hebrew", "he", ("ヘブライ語",)),
    ("Hindi", "hi", ("ヒンディー語",)),
    ("Croatian", "hr", ("クロアチア語",)),
    ("Hungarian", "hu", ("ハンガリー語",)),
    ("Armenian", "hy", ("アルメニア語",)),
    ("Indonesian", "id", ("インドネシア語",)),
    ("Italian", "it", ("イタリア語",)),
    ("Javanese", "jv", ("ジャワ語",)),
    ("Georgian", "ka", ("ジョージア語", "グルジア語")),
    ("Kazakh", "kk", ("カザフ語",)),
    ("Khmer", "km", ("クメール語",)),
    ("Lithuanian", "lt", ("リトアニア語",)),
    ("Latvian", "lv", ("ラトビア語",)),
    ("Malayalam", "ml", ("マラヤーラム語",)),
    ("Marathi", "mr", ("マラーティー語",)),
    ("Malay", "ms", ("マレー語",)),
    ("Burmese", "my", ("ビルマ語", "ミャンマー語")),
    ("Nepali", "ne", ("ネパール語",)),
    ("Dutch", "nl", ("オランダ語",)),
    ("Punjabi", "pa", ("パンジャーブ語", "パンジャービー語")),
    ("Polish", "pl", ("ポーランド語",)),
    ("Russian", "ru", ("ロシア語",)),
    ("Sinhala", "si", ("シンハラ語",)),
    ("Slovak", "sk", ("スロバキア語",)),
    ("Slovenian", "sl", ("スロベニア語",)),
    ("Serbian", "sr", ("セルビア語",)),
    ("Swahili", "sw", ("スワヒリ語",)),
    ("Tamil", "ta", ("タミル語",)),
    ("Telugu", "te", ("テルグ語",)),
    ("Thai", "th", ("タイ語",)),
    ("Tagalog", "tl", ("タガログ語", "フィリピノ語")),
    ("Urdu", "ur", ("ウルドゥー語",)),
    ("Uzbek", "uz", ("ウズベク語",)),
    ("Vietnamese", "vi", ("ベトナム語",)),
    ("Japanese (easy)", "ja(easy)", ("Japanese(easy)", "やさしい日本語", "易しい日本語", "簡単な日本語")),
)

LANGUAGE_CODES_BY_NAME: dict[str, str] = {}
for english_name, code, localized_names in _LANGUAGES:
    for alias in (english_name, code, *localized_names):
        LANGUAGE_CODES_BY_NAME[alias.casefold()] = code

for alias, code in {
    "Chinese": "zh-hans",
    "中文": "zh-hans",
    "中国語": "zh-hans",
    "zh": "zh-hans",
    "zh-cn": "zh-hans",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    "zh-mo": "zh-hant",
    "ja-easy": "ja(easy)",
}.items():
    LANGUAGE_CODES_BY_NAME[alias.casefold()] = code

LANGUAGE_PATTERN = "|".join(
    re.escape(alias) for alias in sorted(LANGUAGE_CODES_BY_NAME, key=len, reverse=True)
)

_SWIFT_LANGUAGE_SCRIPT = r'''
import Foundation
import NaturalLanguage

let data = FileHandle.standardInput.readDataToEndOfFile()
let text = String(data: data, encoding: .utf8) ?? ""
let recognizer = NLLanguageRecognizer()
recognizer.processString(text)
print(recognizer.dominantLanguage?.rawValue ?? "")
'''

_NATURAL_LANGUAGE_ALIASES = {
    "zh": "zh-hans", "zh-cn": "zh-hans", "zh-sg": "zh-hans",
    "zh-hans": "zh-hans", "zh-tw": "zh-hant", "zh-hk": "zh-hant",
    "zh-mo": "zh-hant", "zh-hant": "zh-hant", "fil": "tl", "iw": "he",
    "in": "id", "sr-latn": "sr", "sr-cyrl": "sr", "yue": "zh-hant",
}
_SUPPORTED_LANGUAGE_CODES = frozenset(code for _, code, _ in _LANGUAGES)


def _plamo_code_for_natural_language(tag: str) -> str | None:
    tag = tag.strip().replace("_", "-").casefold()
    tag = _NATURAL_LANGUAGE_ALIASES.get(tag, tag)
    if tag not in _SUPPORTED_LANGUAGE_CODES:
        base = tag.split("-", 1)[0]
        tag = _NATURAL_LANGUAGE_ALIASES.get(base, base)
    return tag if tag in _SUPPORTED_LANGUAGE_CODES else None


def _language_from_natural_language(text: str) -> str | None:
    """Use macOS NaturalLanguage when available; it supports many PLaMo inputs."""
    swift = shutil.which("swift")
    if not swift or not text.strip():
        return None
    sample = text if len(text) <= 40_000 else text[:20_000] + "\n" + text[-20_000:]
    try:
        cache = Path(tempfile.gettempdir()) / "olcr-swift-language-module-cache"
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            cache.chmod(0o700)
        except OSError:
            pass
        result = subprocess.run(
            [swift, "-module-cache-path", str(cache), "-e", _SWIFT_LANGUAGE_SCRIPT],
            input=sample, text=True, encoding="utf-8", stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        if result.returncode != 0:
            return None
        return _plamo_code_for_natural_language(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return None


_LATIN_LANGUAGE_MARKERS = {
    "es": set("el la los las un una unos unas que de del en y por para es era como con su".split()),
    "fr": set("le la les un une des du de que en et est pour dans avec qui son sa".split()),
    "de": set("der die das ein eine und ist war ich du er sie es mit von zu für".split()),
    "it": set("il lo la gli un una che di del e per con è era sono".split()),
    "nl": set("de het een en is was van voor met dat die niet".split()),
    "ca": set("el la els les un una que de del en i per amb és".split()),
    "eu": set("eta da bat ez ere du ditu dela".split()),
    "id": set("yang dan di untuk dengan adalah itu ini dari tidak".split()),
    "ms": set("yang dan di untuk dengan adalah itu ini dari tidak".split()),
    "sw": set("na ya wa kwa ni katika kwamba kutoka sana".split()),
    "tl": set("ang ng mga at sa ay ito siya para hindi".split()),
    "ceb": set("ang sa ug nga mga og dili siya kini".split()),
    "pl": set("i w z na że jest nie się do oraz jak".split()),
    "hu": set("és a az hogy nem van volt egy mint is".split()),
    "et": set("ja on see et ei oli kui ka ning".split()),
    "lv": set("un ir tas ka no ar par viņš viņa".split()),
    "lt": set("ir kad su į iš tai buvo kaip ne".split()),
    "hr": set("i u je da se na od za kao sam".split()),
    "sl": set("in je da se na za od z kot".split()),
    "az": set("və bir bu üçün ilə deyil ki mən".split()),
    "uz": set("va bir bu uchun bilan emas ham".split()),
    "jv": set("lan iku ing karo kanggo ora iki".split()),
}


def _fallback_language_detection(text: str) -> str | None:
    """Detect scripts and clear Latin-language word patterns without dependencies."""
    sample = text[:12_000]
    counts: Counter[str] = Counter()
    for character in sample:
        value = ord(character)
        if 0x3040 <= value <= 0x30FF or 0xFF66 <= value <= 0xFF9F:
            counts["ja"] += 1
        elif 0xAC00 <= value <= 0xD7AF or 0x1100 <= value <= 0x11FF:
            counts["ko"] += 1
        elif 0x0E00 <= value <= 0x0E7F:
            counts["th"] += 1
        elif 0x1780 <= value <= 0x17FF:
            counts["km"] += 1
        elif 0x1000 <= value <= 0x109F or 0xA9E0 <= value <= 0xA9FF:
            counts["my"] += 1
        elif 0x0600 <= value <= 0x06FF or 0x0750 <= value <= 0x077F:
            counts["ar-script"] += 1
        elif 0x0590 <= value <= 0x05FF:
            counts["he"] += 1
        elif 0x0530 <= value <= 0x058F:
            counts["hy"] += 1
        elif 0x10A0 <= value <= 0x10FF:
            counts["ka"] += 1
        elif 0x0980 <= value <= 0x09FF:
            counts["bn"] += 1
        elif 0x0A80 <= value <= 0x0AFF:
            counts["gu"] += 1
        elif 0x0A00 <= value <= 0x0A7F:
            counts["pa"] += 1
        elif 0x0B00 <= value <= 0x0B7F:
            counts["or"] += 1
        elif 0x0B80 <= value <= 0x0BFF:
            counts["ta"] += 1
        elif 0x0C00 <= value <= 0x0C7F:
            counts["te"] += 1
        elif 0x0C80 <= value <= 0x0CFF:
            counts["kn"] += 1
        elif 0x0D00 <= value <= 0x0D7F:
            counts["ml"] += 1
        elif 0x0D80 <= value <= 0x0DFF:
            counts["si"] += 1
        elif 0x0900 <= value <= 0x097F:
            counts["deva"] += 1
        elif 0x0400 <= value <= 0x052F:
            counts["cyrillic"] += 1
        elif 0x3400 <= value <= 0x9FFF or 0xF900 <= value <= 0xFAFF:
            counts["zh-hans"] += 1
    for code in ("ja", "ko", "th", "km", "my", "he", "hy", "ka", "bn", "gu",
                 "pa", "ta", "te", "ml", "si"):
        if counts[code]:
            return code
    if counts["deva"]:
        if "ळ" in sample:
            return "mr"
        if any(token in sample for token in ("छैन", "छन्", "छु", "गर्छ")):
            return "ne"
        return "hi"
    if counts["ar-script"]:
        if any(character in sample for character in "پچژگ"):
            return "fa"
        if any(character in sample for character in "ٹڈڑںھ"):
            return "ur"
        return "ar"
    if counts["cyrillic"]:
        if any(character in sample.casefold() for character in "ў"):
            return "be"
        if any(character in sample.casefold() for character in "әіңғұқөһ"):
            return "kk"
        if any(character in sample.casefold() for character in "ђјљњћџ"):
            return "sr"
        if "ъ" in sample.casefold():
            return "bg"
        return "ru"
    if counts["zh-hans"]:
        if any(character in sample for character in "體國學後門車馬見說書長點東萬與為"):
            return "zh-hant"
        return "zh-hans"

    words = re.findall(r"[a-zA-ZÀ-ÖØ-öø-ÿ]+", sample.casefold())
    if not words:
        return None
    if any(character in sample.casefold() for character in "ăâđêôơư"):
        return "vi"
    word_set = set(words)
    scores = {code: len(word_set & markers) for code, markers in _LATIN_LANGUAGE_MARKERS.items()}
    best = max(scores, key=scores.get)
    if scores[best] >= 2 and scores[best] >= max(scores.values()) - 1:
        # Indonesian and Malay use almost identical common vocabulary. Keep the
        # PLaMo source direction explicit with the more frequent local markers.
        if best in {"id", "ms"}:
            id_score = len(word_set & {"adalah", "tidak", "dari", "untuk"})
            ms_score = len(word_set & {"ialah", "tidak", "daripada", "untuk"})
            return "ms" if ms_score > id_score else "id"
        return best
    return None


def plamo_source_language_tag(source: str | None, text: str) -> str:
    """Return a nonempty PLaMo input-language tag for one translation request.

    PLaMo's automatic source direction only covers English and Japanese. For
    any other supported language, leaving the input tag blank makes a request
    ambiguous; detect the language locally and pass its canonical code.
    """
    explicit = normalize_language_code(source, allow_auto=True)
    if explicit:
        return explicit
    detected = _language_from_natural_language(text)
    if detected is None:
        detected = _fallback_language_detection(text)
    return detected or "English|Japanese"


def normalize_language_code(value: str | None, *, allow_auto: bool = False) -> str:
    """Return a canonical PFML3 code; empty string means automatic source detection."""
    normalized = str(value or "").strip().replace("_", "-").casefold()
    if allow_auto and normalized in {"", "auto", "autodetect", "auto-detect", "automatic", "english|japanese"}:
        return ""
    code = LANGUAGE_CODES_BY_NAME.get(normalized)
    if code is None:
        raise ValueError("MODEL_LANGUAGE_UNSUPPORTED")
    return code
