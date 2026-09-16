"""Durable, deterministic continuation for interactive planning choices."""
from __future__ import annotations

import re
from typing import Any


_OPTION = re.compile(r"^\s*(?:[-*]\s*)?([A-Z]|[1-9][0-9]*)\s*(?:[.)．:：]|[-—])\s*(.+?)\s*$", re.I)
_QUESTION = re.compile(r"^\s*(?:質問|問|question|q)\s*([0-9]+)\s*[:：.．)）-]?\s*(.*)$", re.I)
_ALL = re.compile(r"^(?:全部|全て|すべて|all)\s*([ABC]|[1-9][0-9]*)(?:で)?$", re.I)
_RECOMMENDED_ALL = re.compile(r"^(?:全部|全て|すべて|all)\s*(?:おすすめ|お勧め|任せる|おまかせ|おすすめで)$", re.I)
_RECOMMENDED = re.compile(r"^(?:おすすめ|お勧め|任せる|おまかせ|それで|そのまま)$", re.I)


def parse_pending_questions(text: str) -> list[dict[str, Any]]:
    """Turn conventional question lists into durable structured choices."""
    questions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in (text or "").splitlines():
        match = _QUESTION.match(line)
        if match:
            if current and current["options"]:
                questions.append(current)
            current = {"id": f"Q{match.group(1)}", "question": match.group(2).strip(), "options": {}, "state": "PENDING"}
            continue
        option = _OPTION.match(line)
        if current and option:
            current["options"][option.group(1).upper()] = option.group(2)
        elif current and line.strip() and not current["options"]:
            current["question"] = (current["question"] + " " + line.strip()).strip()
    if current and current["options"]:
        questions.append(current)
    return questions


def recommended_option(question: dict[str, Any]) -> str:
    options = question.get("options") or {}
    for key, label in options.items():
        if re.search(r"(?:おすすめ|推奨|minimal|minimum|mvp|最小)", str(label), re.I):
            return str(key)
    return next(iter(options), "")


def _option_for(question: dict[str, Any], token: str) -> str | None:
    options = question.get("options") or {}
    normalized = token.upper()
    if normalized in options:
        return normalized
    if normalized.isdigit():
        index = int(normalized) - 1
        keys = list(options)
        if 0 <= index < len(keys):
            return str(keys[index])
    return None


def _question_for(questions: list[dict[str, Any]], number: str) -> dict[str, Any] | None:
    wanted = "Q" + str(int(number))
    return next((question for question in questions if question.get("id") == wanted), None)


def resolve_short_reply(message: str, pending: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve only deterministic shorthand; None deliberately means normal text."""
    raw = (message or "").strip()
    compact = re.sub(r"[\s、,，。.!！]", "", raw)
    if not pending:
        return {"kind": "NO_PENDING"} if _ALL.match(compact) or _RECOMMENDED_ALL.match(compact) else None
    answers: dict[str, tuple[str, str]] = {}
    all_match = _ALL.match(compact)
    if all_match:
        for question in pending:
            choice = _option_for(question, all_match.group(1))
            if choice:
                answers[str(question["id"])] = (choice, "EXPLICIT_USER")
        return {"kind": "RESOLVED" if answers else "INCOMPATIBLE", "answers": answers}
    if _RECOMMENDED_ALL.match(compact):
        return {"kind": "RESOLVED", "answers": {str(question["id"]): (recommended_option(question), "ASSUMPTION_RECOMMENDED") for question in pending}}
    if _RECOMMENDED.match(compact):
        return {"kind": "RESOLVED", "answers": {str(question["id"]): (recommended_option(question), "ASSUMPTION_RECOMMENDED") for question in pending}}
    if len(pending) == 1 and re.fullmatch(r"(?:[ABC]|[1-9][0-9]*)(?:で)?", compact, re.I):
        choice = _option_for(pending[0], compact.rstrip("で"))
        return {"kind": "RESOLVED", "answers": {str(pending[0]["id"]): (choice, "EXPLICIT_USER")}} if choice else {"kind": "INCOMPATIBLE", "answers": {}}
    for numbers, choice in re.findall(r"(?:Q)?([0-9]+(?:と[0-9]+)*)\s*(?:=|は|:|：)\s*([ABC]|[1-9][0-9]*)", raw, re.I):
        for number in numbers.split("と"):
            question = _question_for(pending, number)
            selected = _option_for(question, choice) if question else None
            if selected:
                answers[str(question["id"])] = (selected, "EXPLICIT_USER")
    first = re.search(r"最初(?:だけ)?\s*([ABC]|[1-9][0-9]*)\s*[、,，]\s*(?:あとは|残りは)\s*([ABC]|[1-9][0-9]*)", raw, re.I)
    if first:
        for index, question in enumerate(pending):
            selected = _option_for(question, first.group(1 if index == 0 else 2))
            if selected:
                answers[str(question["id"])] = (selected, "EXPLICIT_USER")
    return {"kind": "RESOLVED", "answers": answers} if answers else None
