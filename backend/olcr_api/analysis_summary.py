"""Deterministic briefs for records that are not numeric time series."""
from __future__ import annotations

import re
import math


def normalize_observation(row: dict, tool_id: str | None) -> dict:
    """Expose typed provider measures to both arithmetic and synthesis routing."""
    if (tool_id == "trade.un_comtrade" and row.get("value") in (None, "")
            and row.get("trade_value_usd") not in (None, "")):
        flow = str(row.get("flow") or "貿易")
        commodity = str(row.get("commodity") or row.get("commodity_code") or "全品目")
        reporter = str(row.get("reporter") or "対象地域")
        partner = str(row.get("partner") or "世界")
        return {**row, "value": row["trade_value_usd"],
                "indicator": f"trade_value_usd:{flow}:{commodity}",
                "country": f"{reporter} → {partner}", "unit": " US$"}
    if tool_id == "space.space_weather" and row.get("Kp") is not None:
        return {**row, "value": row["Kp"], "date": row.get("time_tag"),
                "indicator": "地磁気Kp指数", "unit": " Kp"}
    return row


def has_numeric_observations(evidence: dict) -> bool:
    data = evidence.get("data") or {}
    from .analysis_semantics import records
    for raw in records(data):
        if not isinstance(raw, dict):
            continue
        row = normalize_observation(raw, evidence.get("tool_id"))
        if not any(row.get(key) is not None for key in ("year", "date", "period")):
            continue
        value = row.get("value")
        if isinstance(value, bool):
            continue
        try:
            if math.isfinite(float(value)):
                return True
        except (TypeError, ValueError):
            continue
    return False


def record_briefing(evidence_rows: list[dict]) -> str:
    sections = []
    for evidence in evidence_rows:
        data = evidence.get('data') or {}
        rows = next((data[k] for k in ('items', 'rows', 'works') if isinstance(data.get(k), list)), [])
        lines = []
        if isinstance(data.get('extract'), str) and data['extract'].strip():
            source = str((evidence.get('sources') or [{}])[0].get('source_id') or '')
            place = data.get('map_place') or data.get('title') or '取得文書'
            sections.append(f"{data.get('map_pin', '')}: {place} [{source}]\n取得資料の抜粋（依頼された分析は未完了）:\n" + data['extract'][:750])
            continue
        for row in rows:
            if not isinstance(row, dict) or 'value' in row:
                continue
            title = next((row[k] for k in ('title', 'name', 'full_name', 'brief_title', 'label', 'meeting') if row.get(k)), None)
            detail = next((row[k] for k in ('summary', 'abstract', 'description', 'snippet', 'text', 'body_excerpt', 'speech_excerpt')
                           if isinstance(row.get(k), str) and row[k].strip()), '')
            if not isinstance(title, str) and not detail:
                continue
            date = next((str(row[k]) for k in ('date', 'publication_year', 'published_at', 'pushed_at', 'year') if row.get(k)), '')
            clean = lambda text: re.sub(r'\s+', ' ', str(text)).strip()
            line = clean(title or '取得文書') + (f'（{clean(date)}）' if date else '')
            if isinstance(row.get('speaker'), str) and row['speaker'].strip():
                line += ' / ' + clean(row['speaker'])
            if detail:
                line += ' — ' + clean(detail)[:360]
            lines.append('- ' + line)
            if len(lines) == 3:
                break
        if lines:
            source = str((evidence.get('sources') or [{}])[0].get('source_id') or '')
            provider = evidence.get('provider') or evidence.get('tool_id') or 'API'
            qualifier = 'カタログの記載' if str(data.get('evidence_kind', '')).endswith('_catalog') else '取得した記録の要点'
            sections.append(f'{provider}: {qualifier}' + (f' [{source}]' if source else '') + '\n' + '\n'.join(lines))
    if not sections:
        return ''
    return '\n\n'.join(sections) + '\n\nこれは取得範囲内の記録です。文書の掲載日を数値指標として扱わず、記録間の因果関係や全体の傾向はこの抜粋だけから断定できません。'
