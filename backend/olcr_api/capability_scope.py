"""Deterministic block scoping for capability statements (not model prompts)."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import re


@dataclass(frozen=True)
class PromptBlock:
    text: str
    line_start: int
    line_end: int
    section_path: tuple[str, ...]
    block_kind: str
    parent_block: int | None
    semantic_scope: str
    polarity_context: str


def _scope(title: str) -> str:
    for pattern, scope in (
        (r"future|fix.?benchmark|later|将来", "FUTURE_WORK"),
        (r"failure.?handling|失敗時", "FAILURE_HANDLING"),
        (r"verification|visual review|regression|^done$|検証|完了条件", "VERIFICATION_ONLY"),
        (r"^report(?:ing)?$|final status|報告", "REPORTING"),
        (r"content accuracy|product(?: description)?$|architecture description|reference|example|page structure|ui copy|製品説明", "CONTENT_DESCRIPTION"),
        (r"repository.*check|implementation order|blueprint|^resolve$|^phase\s+\d+", "PLANNING_DETAIL"),
        (r"exclusion|non.?goals|out of scope|対象外|除外|実装しないもの", "CURRENT_PROHIBITION"),
    ):
        if re.search(pattern, title, re.I):
            return scope
    return "CURRENT_IMPLEMENTATION"


def _negative(text: str) -> bool:
    return bool(re.search(
        r"\b(?:do\s+not|don't|must\s+not|never|without|no)\b|\bnot\s+(?:implement|modify|add|introduce|use|create|call)\b|"
        r"不要|なし|使わない|使用しない|実装しない|導入しない|追加しない|含めない|対象外|禁止", text, re.I))


def parse_prompt_blocks(text: str) -> list[PromptBlock]:
    """Keep Markdown hierarchy and plain prompt headings; labels own local lists."""
    lines = text.splitlines()
    blocks = []
    headings: list[tuple[int, str, str, int]] = []
    labels: list[tuple[int, str, str, int]] = []
    fenced = False
    previous_kind = ""
    for n, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip())
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        md = re.match(r"^(#{1,6})\s+(.+)", stripped)
        bullet = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)(.+)", stripped)
        title = (md.group(2) if md else stripped).rstrip(":：").strip()
        # Plain headings have their own paragraph and title-style words.
        # Single technology names remain data, not headings.
        known = bool(re.fullmatch(r"Goal|Product|Done|Report|FailureHandling|Footer|Accessibility|Performance|State|Data|要件|実装するもの|検証|完了条件", title, re.I))
        title_style = bool(not title.endswith(('.', '!', '?')) and re.fullmatch(r"[A-Z][A-Za-z0-9./ -]{2,75}", title) and
                           len(title.split()) > 1 and all(w[0].isupper() or w in {"/", "—", "of", "for", "and"} for w in title.split()))
        isolated = (n == 0 or not lines[n-1].strip()) and (n+1 == len(lines) or not lines[n+1].strip())
        is_heading = not fenced and not bullet and (bool(md) or (not stripped.endswith((":", "：")) and isolated and (known or title_style)))
        if is_heading:
            level = len(md.group(1)) if md else 1
            while headings and headings[-1][0] >= level:
                headings.pop()
            parent_scope = headings[-1][2] if headings else "CURRENT_IMPLEMENTATION"
            scope = _scope(title)
            if scope == "CURRENT_IMPLEMENTATION" and headings:
                scope = parent_scope
            headings.append((level, title, scope, len(blocks)))
            labels = []
            kind = "HEADING"
            polarity = "FORBIDDEN" if scope == "CURRENT_PROHIBITION" else "NEUTRAL"
        else:
            kind = "CODE" if fenced else "QUOTE" if stripped.startswith(">") else "BULLET" if bullet else "LABEL" if stripped.endswith((":", "：")) else "KEY_VALUE" if re.match(r"^[A-Z][A-Z0-9_]*=", stripped) else "PARAGRAPH"
            if kind == "LABEL":
                while labels and labels[-1][0] >= indent:
                    labels.pop()
            elif labels:
                # Dedented prose ends a list. Bare words remain children of
                # a label, supporting 'Do NOT implement:\nbackend\ndatabase'.
                prose = bool(re.search(r"[。.!?]$", stripped) or len(stripped.split()) > 5)
                if indent < labels[-1][0] or (indent <= labels[-1][0] and kind == "PARAGRAPH" and (previous_kind == "BULLET" or prose)):
                    labels = []
            scope = headings[-1][2] if headings else "CURRENT_IMPLEMENTATION"
            polarity = labels[-1][2] if labels else ("FORBIDDEN" if scope == "CURRENT_PROHIBITION" else "NEUTRAL")
            if kind == "LABEL":
                polarity = "FORBIDDEN" if _negative(title) else "NEUTRAL"
                labels.append((indent, title, polarity, len(blocks)))
        path = tuple(h[1] for h in headings) + tuple(l[1] for l in labels)
        parent = (labels[-1][3] if labels and labels[-1][3] != len(blocks) else
                  labels[-2][3] if len(labels) > 1 else
                  headings[-1][3] if headings and headings[-1][3] != len(blocks) else
                  headings[-2][3] if len(headings) > 1 else None)
        blocks.append(PromptBlock(bullet.group(1) if bullet else stripped, n+1, n+1, path, kind, parent, scope, polarity))
        previous_kind = kind
    return blocks


_TECHNOLOGIES = {"nextjs": r"next\.js", "gsap": r"\bgsap\b", "framer_motion": r"framer motion", "threejs": r"three\.js"}
_IGNORED_SCOPES = {"FUTURE_WORK", "FAILURE_HANDLING", "VERIFICATION_ONLY", "REPORTING", "CONTENT_DESCRIPTION"}


def capability_evidence(text: str, aliases: dict[str, str]) -> list[dict]:
    evidence = []
    blocks = parse_prompt_blocks(text)
    for block in blocks:
        inherited = block.polarity_context == "FORBIDDEN"
        for sentence in re.split(r"[。;；!]|\.(?=\s|$)|\bbut\b|ただし|一方", block.text):
            negative = inherited or _negative(sentence)
            for clause in re.split(r"[,、]|\band\b|ですが|だが", sentence):
                if not clause.strip():
                    continue
                local_negative = inherited or _negative(clause)
                action = bool(re.search(r"\b(?:implement|build|create|add|use|install|require|modify)\b|実装|作成|構築|導入|使用|追加|使う", clause, re.I))
                if action and not local_negative and not inherited:
                    negative = False
                else:
                    negative = negative or local_negative
                target_text = clause
                if block.parent_block is not None:
                    parent = blocks[block.parent_block]
                    if re.fullmatch(r"frontend|backend|database|api|dependencies", parent.text.rstrip(":："), re.I):
                        target_text = parent.text + " " + clause
                candidates = [(name, "capability") for name, pattern in aliases.items() if re.search(pattern, target_text, re.I)]
                candidates += [(name, "technology") for name, pattern in _TECHNOLOGIES.items() if re.search(pattern, target_text, re.I)]
                for name, category in candidates:
                    reason = "CURRENT_EXPLICIT_PROHIBITION" if negative else "CURRENT_IMPLEMENTATION_REQUIREMENT"
                    active = True
                    if block.semantic_scope in _IGNORED_SCOPES:
                        reason, active = block.semantic_scope, False
                    elif block.block_kind in {"HEADING", "LABEL", "CODE", "QUOTE", "KEY_VALUE"}:
                        reason, active = "STRUCTURAL_OR_QUOTED_TEXT", False
                    elif re.search(r"^\s*(?:if\b|unless\b)|\b(?:unavailable|incompatible|failed|unknown|missing|not run)\b", clause, re.I):
                        reason, active = "FAILURE_CONDITION", False
                    elif re.search(r"(?:unrelated|unnecessary|不要な|無関係|関係ない).{0,40}(?:frontend|dependenc|フロント|依存)|second.{0,25}(?:framework|dependenc)|global frontend default|\b(?:mix|guess)\b.*(?:version|api|v[0-9])", clause, re.I):
                        reason, active = "SCOPE_CONSTRAINT", False
                    elif category == "technology":
                        reason = "TECHNOLOGY_SPECIFIC_PROHIBITION" if negative else "TECHNOLOGY_REQUIREMENT"
                    elif name == "backend" and re.search(r"\bapis?\b", clause, re.I) and not re.search(r"backend|fastapi|バックエンド|server endpoint", clause, re.I):
                        bound = re.search(r"\b(?:implement|create|build|add)\s+(?:an?\s+|the\s+)?(?:backend\s+|rest\s+|runtime\s+)?api\b", clause, re.I)
                        bare_api = bool(re.fullmatch(r"\s*(?:external\s+|runtime\s+)?apis?\s*", clause, re.I))
                        if not (bound or (negative and bare_api)):
                            reason, active = "CONTENT_DESCRIPTION", False
                    elif not negative and re.search(r"\b(?:describe|inspect|show|copy|list|verify|report|explain)\b|説明|確認", clause, re.I):
                        reason, active = "CONTENT_DESCRIPTION", False
                    elif block.semantic_scope == "PLANNING_DETAIL" and not action and not negative:
                        reason, active = "PLANNING_DETAIL", False
                    elif not negative and not action:
                        stack = any(re.search(r"stack|technology|requirements|required", part, re.I) for part in block.section_path)
                        nominal = re.search(r"frontend[ -]only|frontend implementation|フロントエンド.*実装", clause, re.I)
                        bare = (len(clause.split()) <= 5 or '+' in clause) and not re.search(r"\b(?:status|compatibility|description|guidance|copy)\b", clause, re.I)
                        if not (stack or nominal or bare):
                            reason, active = "REFERENCE_ONLY", False
                    evidence.append({"capability": name, "category": category,
                        "polarity": "forbidden" if negative else "required",
                        "source_scope": "current_user_text", "source_section": " > ".join(block.section_path),
                        "section_path": list(block.section_path), "block_kind": block.block_kind,
                        "semantic_scope": block.semantic_scope, "parent_polarity": block.polarity_context,
                        "line_start": block.line_start, "line_end": block.line_end,
                        "source_span_hash": hashlib.sha256(clause.encode()).hexdigest(),
                        "reason": reason, "active_for_control": active, "decision": "ACTIVE" if active else "IGNORED"})
    return evidence
