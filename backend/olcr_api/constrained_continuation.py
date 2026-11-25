"""User consent for continuing with a specific, disclosed limitation."""
from __future__ import annotations

import re

PENDING = "CONSTRAINED_CONTINUATION"


def choice(text: str) -> str | None:
    value = re.sub(r"[。.!！?？]+$", "", text.strip()).strip().casefold()
    if value in {"制約を受け入れて続行", "その項目を省いて続行", "continue with limitations", "continue without it"}:
        return "accept"
    if value in {"ここで停止", "ここで止める", "stop here", "do not continue"}:
        return "decline"
    return None


def prompt(items: list[str], reason: str, remaining: str, *, ja: bool) -> str:
    if ja:
        return (f"次の項目は現在の制約により実行できません。\n"
                + "\n".join(f"- {item}" for item in items)
                + f"\n\n理由：{reason}\n続ける場合：{remaining}\n\n"
                "この項目を省いて続けますか？「制約を受け入れて続行」または「ここで停止」と返信してください。")
    return ("The current constraints prevent these requested items:\n"
            + "\n".join(f"- {item}" for item in items)
            + f"\n\nReason: {reason}\nIf you continue: {remaining}\n\n"
            'Continue without these items? Reply “continue with limitations” or “stop here”.')
