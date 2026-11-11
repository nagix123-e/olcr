"""Opt-in, relevance-gated YouTube companions for knowledge answers."""
from __future__ import annotations

import re
import unicodedata


_VIDEO_REQUEST = re.compile(
    r"(?:youtube|youtu\.be|動画|映像|ミュージックビデオ|\bvideos?\b|\bclips?\b)"
    r".*?(?:見せ|出して|検索|探して|紹介|教えて|リンク|見たい|見られ|見れる|ありますか|ある\?|候補|\bshow\b|\bfind\b|\bsearch\b|\brecommend\b|\bany\b)|"
    r"(?:見せ|出して|検索|探して|紹介|教えて|リンク|見たい|見られ|見れる|ありますか|ある\?|候補|\bshow\b|\bfind\b|\bsearch\b|\brecommend\b|\bany\b)"
    r".*?(?:youtube|youtu\.be|動画|映像|\bvideos?\b|\bclips?\b)", re.I)
_KNOWLEDGE_QUESTION = re.compile(
    r"とは|って何|について|教えて|説明|概要|なぜ|どういう|\bwhat\b|\bwho\b|\bwhy\b|\bhow\b|\bexplain\b|\btell me about\b", re.I)
_WORK_KIND = re.compile(r"映画|作品|曲|歌|アルバム|アニメ|漫画|小説|ドラマ|番組|ゲーム|film|movie|song|album|anime|manga|novel|series|show|game", re.I)
_TITLE = re.compile(r"[『「\"']([^『』「」\"'\n]{2,120})[』」\"']")


def youtube_query_for_knowledge(question: str, answer: str) -> str | None:
    """Only search alongside a knowledge answer when requested or title-matched."""
    normalized = unicodedata.normalize("NFKC", question).strip()
    if explicit_knowledge_video_request(normalized):
        query = normalized
        query = re.sub(r"(?:youtube|youtu\.be)(?:\s*music)?", " ", query, flags=re.I)
        query = re.sub(r"関連(?:する)?|おすすめ|お勧め|オススメ|動画|映像|ミュージックビデオ|\bvideos?\b|\bclips?\b", " ", query, flags=re.I)
        query = re.sub(r"(?:も|を)?(?:見せて|出して|検索して|探して|紹介して|教えて|リンクして)(?:ください)?", " ", query)
        query = re.sub(r"(?:とは|って何|について|を説明して|説明して|教えて|概要を教えて|なぜ|どういう)", " ", query, flags=re.I)
        query = re.sub(r"\b(?:what|who|why|how|is|are|the|explain|tell me about|show me|find|search|please)\b", " ", query, flags=re.I)
        query = re.sub(r"[、。,.!?！？:：]", " ", query)
        query = re.sub(r"\s+", " ", query).strip()
        return query.strip(" 、,「」『』\"'。.!！?？:")[:200] or None

    # In the absence of an explicit video request, require a quoted work title
    # that is both discussed as a media work and actually appears in the answer.
    if not _WORK_KIND.search(normalized):
        return None
    for match in _TITLE.finditer(normalized):
        title = match.group(1).strip()
        if title and title.casefold() in answer.casefold():
            return title[:200]
    return None


def explicit_knowledge_video_request(question: str) -> bool:
    normalized = unicodedata.normalize("NFKC", question).strip()
    return bool(_VIDEO_REQUEST.search(normalized) and _KNOWLEDGE_QUESTION.search(normalized))


def youtube_links(text: str) -> list[tuple[str, str]]:
    """Return (display title, URL) pairs for direct YouTube video links."""
    found: list[tuple[str, str]] = []
    patterns = (
        re.compile(r"\[([^\]]{1,200})\]\((https?://(?:www\.)?(?:youtube\.com/watch\?[^)]+|youtu\.be/[^)]+))\)", re.I),
        re.compile(r"(?<![\w(])((?:https?://)?(?:www\.)?(?:youtube\.com/watch\?[^\s<>]+|youtu\.be/[^\s<>]+))", re.I),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            title, url = (match.group(1), match.group(2)) if len(match.groups()) == 2 else ("", match.group(1))
            if not url.startswith("http"):
                url = "https://" + url
            if not any(existing[1] == url for existing in found):
                found.append((title.strip(), url))
    return found[:5]


def remove_youtube_links(text: str) -> str:
    text = re.sub(r"\[([^\]]{1,200})\]\((https?://(?:www\.)?(?:youtube\.com/watch\?[^)]+|youtu\.be/[^)]+))\)", r"\1", text, flags=re.I)
    text = re.sub(r"(?<![\w(])(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?[^\s<>]+|youtu\.be/[^\s<>]+)", "", text, flags=re.I)
    return re.sub(r"[ \t]{2,}", " ", text)
