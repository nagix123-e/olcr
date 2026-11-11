"""Bound chat evidence independently of the observations used for analysis."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


def response_budget(context_budget: int) -> int:
    return min(20000, max(4000, int(context_budget)))


def bounded_lines(text: str, budget: int) -> str:
    """Keep a contiguous prefix, with table context and fenced code intact.

    Head/tail splicing can attach the final country's rows to the first
    country's table caption. Never resume data after a skipped section.
    """
    import re
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    notice = "一部の一覧を省略しています。表示した観測だけを地域全体の順位・代表値とは扱えません。"
    lines = text.splitlines()
    qualifier = []
    if lines and len(lines[-1]) <= min(800, budget//3) and not re.search(r"\d|^\s*\||^\s*[`~]{3}", lines[-1]) and re.search(
            r"(?:sample|causal|representativ|limitation|因果|代表|範囲内|未確認)", lines[-1], re.I):
        qualifier = [lines[-1]]
        lines = lines[:-1]
    remaining = max(0, budget - len(notice) - sum(len(line)+1 for line in qualifier) - 3)
    head, used, index = [], 0, 0
    while index < len(lines):
        start = index
        fence = re.match(r"\s*(`{3,}|~{3,})", lines[index])
        if fence:
            marker = fence[1]
            index += 1
            while index < len(lines) and not lines[index].lstrip().startswith(marker):
                index += 1
            index = min(len(lines), index + 1)
            block = lines[start:index]
        elif lines[index].lstrip().startswith('|') or (index+1 < len(lines) and lines[index+1].lstrip().startswith('|')):
            table_start = index if lines[index].lstrip().startswith('|') else index+1
            index = table_start
            while index < len(lines) and lines[index].lstrip().startswith('|'):
                index += 1
            table_end = index
            if index < len(lines) and re.match(r"(?:これは|注[:：]|表示|取得|比較可能|単位|分母|計算式|出典)", lines[index]):
                index += 1
            block = lines[start:index]
            if used + sum(len(line)+1 for line in block) > remaining:
                # A partial final table may keep its own caption/header and
                # first rows, never rows taken from a later table.
                prefix = lines[start:table_start+2]
                footer = lines[table_end:index]
                partial = list(prefix)
                cost = sum(len(line)+1 for line in prefix+footer)
                for row in lines[table_start+2:table_end]:
                    if used + cost + len(row)+1 > remaining:
                        break
                    partial.append(row); cost += len(row)+1
                if len(partial) > len(prefix):
                    head.extend(partial+footer)
                break
        else:
            index += 1
            block = lines[start:index]
        cost = sum(len(line)+1 for line in block)
        if used + cost > remaining:
            break
        head.extend(block); used += cost
    while head and (not head[-1].strip() or re.match(r"^#{1,6}\s", head[-1])):
        head.pop()
    return "\n".join([*head, notice, *qualifier])[:budget] if budget < len(notice)+3 else "\n".join([*head, notice, *qualifier])



def display_result(result: dict[str, Any], row_limit: int = 32) -> dict[str, Any]:
    """Sample across series before rendering; never mutate authoritative data."""
    def compact(value, depth=0):
        if depth > 12:
            return None
        if isinstance(value, str):
            return value if len(value) <= 800 else value[:800] + " [抜粋]"
        if isinstance(value, list):
            series = defaultdict(lambda: defaultdict(list))
            for row in value:
                indicator = row.get("indicator") if isinstance(row, dict) else None
                if isinstance(indicator, dict): indicator = indicator.get("id")
                key = str(indicator or (row.get("series_id") or row.get("series") or "") if isinstance(row, dict) else "")
                country = (row.get("geo_area") or row.get("country") or "") if isinstance(row, dict) else ""
                if isinstance(country, dict): country = country.get("id") or country.get("value") or ""
                series[key][str(country)].append(row)
            groups = {}
            for key, countries in series.items():
                pending = {country: deque(sorted(rows, key=lambda row: str(row.get("date") or row.get("year") or row.get("period") or "") if isinstance(row, dict) else "", reverse=True))
                           for country, rows in countries.items()}
                interleaved = deque()
                while pending:
                    for country in list(pending):
                        interleaved.append(pending[country].popleft())
                        if not pending[country]: del pending[country]
                groups[key] = interleaved
            selected = []
            while groups and len(selected) < row_limit:
                for key in list(groups):
                    selected.append(compact(groups[key].popleft(), depth + 1))
                    if not groups[key]: del groups[key]
                    if len(selected) == row_limit: break
            return selected
        if not isinstance(value, dict):
            return value
        sampled = {}
        has_rows = any(isinstance(value.get(k), list) for k in ("items", "rows", "works"))
        for key, child in value.items():
            if has_rows and key in {"raw", "raw_metadata", "results", "documents", "articles"}:
                continue
            sampled[key] = compact(child, depth + 1)
        if any(isinstance(child, list) and len(child) > row_limit for child in value.values()):
            sampled["display_truncated"] = True
        return sampled
    return {**result, "data": compact(result.get("data") or {})}


def concise_unverified_answer(text: str) -> str:
    import re
    text = text[:6000]
    text = re.sub(r"([^\n]{12,160}?)(?:\1){2,}", r"\1（反復部分を省略）", text)
    text = re.sub(r"([。！？])\s*", r"\1\n", text)
    text = re.sub(r"(?<=[.!?])\s+(?=[A-Z])", "\n", text)
    return bounded_lines(text, 1800)
