"""Observation identities and extractive facts for analytical answers.

The model may select facts; it cannot change their values, scope or wording.
Inventory counts describe the full filtered evidence, never a context sample.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict


def records(data: dict) -> list[dict]:
    rows = next((data[key] for key in ("rows", "items", "works") if isinstance(data.get(key), list)), [])
    # Public JSON tables often have a header row instead of object records.
    if rows and isinstance(rows[0], list) and all(isinstance(key, str) for key in rows[0]):
        header = rows[0]
        return [dict(zip(header, row)) for row in rows[1:] if isinstance(row, list) and len(row) == len(header)]
    return [row for row in rows if isinstance(row, dict)]


def metric_id(row: dict, data: dict) -> str:
    value = (row.get("series_id") or row.get("metric") or row.get("indicator")
             or row.get("series") or data.get("series_id") or data.get("indicator") or data.get("series") or "")
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value.get("id") or value.get("value") or "") if isinstance(value, dict) else str(value)


def dimensions(row: dict, data: dict) -> dict:
    result = dict(row.get("dimensions") or {}) if isinstance(row.get("dimensions"), dict) else {}
    # Statistical classifications are part of an observation's identity.
    # Neither source IDs nor observation IDs should split a time series.
    for key in ("sex", "age", "age_group", "residence", "urbanisation", "urbanization",
                "education", "occupation", "sector", "frequency", "freq", "seasonal_adjustment",
                "adjustment", "base_year", "price_basis", "valuation", "series_code", "nature",
                "reporting_type", "country_dimension", "country_of_origin", "country_of_asylum",
                "flow_code", "commodity_code", "partner_code", "coicop", "region", "subregion", "admin1", "admin2", "station_id", "geo_level", "fiscal_year_basis"):
        value = row.get(key, data.get(key))
        if value not in (None, "", [], {}):
            result[key] = value
    # Different SDG series can belong to the same indicator.
    if row.get("indicator") and row.get("series"):
        result["series"] = row["series"]
    period = str(row.get("period") or "")
    if re.fullmatch(r"M(?:0[1-9]|1[0-2])", period):
        result["period_basis"] = "monthly"
    elif period == "M13":
        result["period_basis"] = "annual_average"
    return result


def evidence_inventory(evidence_rows: list[dict]) -> dict:
    inventory = {}
    for i, evidence in enumerate(evidence_rows, 1):
        data = evidence.get("data") or {}
        rows = records(data)
        periods = sorted({str(row.get("date") or row.get("year") or row.get("period")
                              or row.get("time_tag") or row.get("time") or "") for row in rows} - {""})
        areas = set()
        for row in rows:
            area = row.get("geo_area") or row.get("country") or row.get("countryiso3code")
            if isinstance(area, dict):
                area = area.get("id") or area.get("value")
            if area:
                areas.add(str(area))
        inventory[evidence.get("evidence_id") or f"E{i}"] = {
            "provider": evidence.get("provider") or evidence.get("tool_id"),
            "record_count": len(rows), "area_count": len(areas),
            "period_start": periods[0] if periods else None,
            "period_end": periods[-1] if periods else None,
            "evidence_kind": data.get("evidence_kind") or data.get("response_kind") or "observations",
            "partial": bool(data.get("request_complete") is False or data.get("sample_only")
                            or data.get("truncated") or data.get("missing_requests")),
            "sources": [source.get("source_id") for source in evidence.get("sources", [])],
        }
    return inventory


def record_facts(evidence_rows: list[dict]) -> list[dict]:
    """Create immutable extractive facts, including event/status payloads."""
    facts = []
    clean = lambda text: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text))).strip()
    for i, evidence in enumerate(evidence_rows, 1):
        data = evidence.get("data") or {}
        rows = records(data)
        if not rows and any(isinstance(data.get(key), str) and data[key].strip()
                            for key in ("extract", "summary", "text", "description")):
            rows = [data]
        for j, row in enumerate(rows, 1):
            if isinstance(row.get("data"), dict):
                continue
            if "value" in row and any(row.get(key) for key in ("date", "year", "period")):
                try:
                    float(row["value"])
                    continue  # Verified arithmetic renders numeric series separately.
                except (TypeError, ValueError):
                    pass
            title = next((row[key] for key in ("title", "name", "full_name", "brief_title", "label", "meeting")
                          if isinstance(row.get(key), str) and row[key].strip()), "")
            detail = next((row[key] for key in ("summary", "abstract", "description", "snippet", "text", "extract", "body_excerpt", "speech_excerpt")
                           if isinstance(row.get(key), str) and row[key].strip()), "")
            if title or detail:
                text = clean(title) + (" — " if title and detail else "") + clean(detail)[:500]
                if len(clean(detail)) > 500:
                    text += "（本文抜粋）"
                date = next((row[key] for key in ("date", "publication_year", "published_at", "pushed_at", "year") if row.get(key)), "")
                if date:
                    text += f" / 日時: {clean(date)}"
                if row.get("speaker"):
                    text += f" / 発言者: {clean(row['speaker'])}"
                excluded = {"title", "name", "full_name", "brief_title", "label", "meeting", "summary", "abstract",
                            "description", "snippet", "text", "extract", "body_excerpt", "speech_excerpt", "speaker",
                            "date", "publication_year", "published_at", "pushed_at", "year", "url", "canonical_url"}
                attributes = []
                for key, value in row.items():
                    if key in excluded or value in (None, "", [], {}) or isinstance(value, bool):
                        continue
                    if isinstance(value, (str, int, float)) or (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                        attributes.append(f"{key}: {clean(value)[:160]}")
                if attributes:
                    text += " / " + " / ".join(attributes[:8])
            else:
                # Unknown typed event records remain visible without invented
                # interpretation of an index, magnitude or status code.
                props = row.get("properties") if isinstance(row.get("properties"), dict) else row
                fields = [(key, value) for key, value in props.items()
                          if isinstance(value, (str, int, float)) and not isinstance(value, bool)
                          and value not in (None, "") and key not in {"url", "id", "source_id", "query", "request", "current_request", "fetched_at"}]
                text = " / ".join(f"{key}: {clean(value)[:120]}" for key, value in fields[:8])
            if not text:
                continue
            facts.append({"id": f"E{i}R{j}", "evidence_id": f"E{i}", "provider": str(evidence.get("provider") or evidence.get("tool_id") or "API"),
                          "text": text, "catalog": str(data.get("evidence_kind", "")).endswith("_catalog") and not row.get("content_url"),
                          "sources": [source.get("source_id") for source in evidence.get("sources", [])]})
    return facts


def choose_facts(facts: list[dict], selected=None, per_provider: int = 3) -> list[dict]:
    """A model selection never hides a retrieved provider or creates a fact."""
    by_id = {fact["id"]: fact for fact in facts}
    chosen = [by_id[key] for key in dict.fromkeys(selected or []) if key in by_id]
    identity = lambda fact: fact.get("evidence_id") or fact["provider"]
    providers = list(dict.fromkeys(identity(fact) for fact in facts))
    for provider in providers:
        if not any(identity(fact) == provider for fact in chosen):
            chosen.append(next(fact for fact in facts if identity(fact) == provider))
    if not selected:
        chosen = facts
    counts = defaultdict(int)
    result = []
    for fact in chosen:
        if counts[identity(fact)] < per_provider:
            result.append(fact)
            counts[identity(fact)] += 1
    return result


def render_facts(facts: list[dict], inventory: dict, selected=None) -> str:
    chosen = choose_facts(facts, selected)
    lines = []
    for item in inventory.values():
        scope = f"{item['provider']}: 取得記録{item['record_count']}件"
        if item["area_count"]:
            scope += f"、対象国・地域{item['area_count']}"
        if item["period_start"]:
            scope += f"、記録期間 {item['period_start']}〜{item['period_end']}"
        scope += "（取得範囲の全記録数。下の抜粋件数とは異なります）"
        if item["partial"]:
            scope += "。提供元の取得範囲は一部です"
        lines.append(scope + " " + " ".join(f"[{s}]" for s in item["sources"] if s))
    grouped = defaultdict(list)
    for fact in chosen:
        grouped[(fact.get("evidence_id"), fact["provider"])].append(fact)
    for (_identity, provider), group in grouped.items():
        lines.append("\n" + provider + ": " + ("カタログ記載の抜粋（本文の確認ではありません）" if all(f["catalog"] for f in group) else "取得記録・本文の抜粋"))
        lines.extend("- " + fact["text"] + " " + " ".join(f"[{s}]" for s in fact["sources"] if s) for fact in group)
    return "\n".join(lines)


def analytical_gaps(message: str, evidence_rows: list[dict]) -> list[dict]:
    """Report unsupported analytical operations, independently of HTTP success."""
    from .analysis_recovery import flattened_results
    evidence_rows = list(flattened_results(evidence_rows))
    gaps = []
    if re.search(r"比較|照合|関係|結び|compare|cross.?check|relat", message, re.I):
        from .analysis_scope import observation_identity
        from .analysis_summary import normalize_observation
        import math
        numeric_scopes = defaultdict(set)
        for e in evidence_rows:
            data = e.get("data") or {}
            for raw in records(data):
                row = normalize_observation(raw, e.get("tool_id"))
                try:
                    if isinstance(row.get("value"), bool) or not math.isfinite(float(row.get("value"))):
                        continue
                except (ValueError, TypeError):
                    continue
                period = str(row.get("date") or row.get("year") or row.get("period") or "")
                if row.get("year") and re.fullmatch(r"M(?:0[1-9]|1[0-2])", str(row.get("period") or "")):
                    period = f"{row['year']}-{row['period'][1:]}"
                if period.endswith(".0"):
                    period = period[:-2]
                if not period:
                    continue
                basis = "fiscal" if row.get("fiscal_year") or data.get("fiscal_year_basis") else "calendar"
                key = (e.get("tool_id"), metric_id(row, data))
                numeric_scopes[key].add((observation_identity(row), period, basis))
        if len(numeric_scopes) > 1 and any(not any(scope & other for key2, other in numeric_scopes.items() if key2 != key)
                                            for key, scope in numeric_scopes.items()):
            gaps.append({"key": "comparison_alignment", "reason": "一部の指標では同じ対象・観測期間・暦年／会計年度の値が揃っていません。各系列の変化は確認できますが、全指標を揃えた直接比較は未完了です。"})
    if re.search(r"各国|各地域|国別|地域別|across countries|by countr|by region", message, re.I):
        areas = {str(row.get("geo_area") or row.get("country") or "")
                 for e in evidence_rows for row in records(e.get("data") or {})} - {""}
        if len(areas) == 1:
            gaps.append({"key": "comparison_scope", "reason": "取得できた数値の対象は1国・地域で、依頼された国・地域間の比較は未完了です。"})
    if re.search(r"構成|内訳|breakdown|composition", message, re.I):
        trade = [e for e in evidence_rows if e.get("tool_id") == "trade.un_comtrade"]
        if trade and all(str(row.get("commodity_code") or row.get("commodity") or "TOTAL").upper() == "TOTAL"
                         for e in trade for row in records(e.get("data") or {})):
            gaps.append({"key": "composition", "reason": "全品目の総額だけでは品目別の構成比を比較できません。品目別の内訳が必要です。"})
    if re.search(r"照合|結び|重なり|背景|関連性|横断|cross.?check|link|overlap", message, re.I) and len(evidence_rows) > 1:
        # Numerical co-movement is descriptive; narratives require an actual
        # shared entity/reference. No supplied reference proves causation.
        gaps.append({"key": "linkage", "reason": "資料を並べた比較です。資料間の対応関係や因果関係は未検証です。共通する対象・期間・識別子を確認する必要があります。"})
    for e in evidence_rows:
        data = e.get("data") or {}
        payload_rows = records(data)
        if payload_rows and all(set(row).issubset({"query", "request", "current_request", "fetched_at", "request_id", "provider"}) for row in payload_rows):
            gaps.append({"key": "evidence_content", "reason": f"{e.get('provider') or e.get('tool_id')}の応答は要求の再掲・取得メタデータのみで、分析の根拠になる観測や本文がありません。"})
        if str(data.get("evidence_kind") or "").endswith("_catalog"):
            reason = (f"{e.get('provider') or e.get('tool_id')}は一部資料の本文を取得しましたが、未取得の資料が残ります。" if data.get("materialized_document_count") else
                      f"{e.get('provider') or e.get('tool_id')}はカタログの記載です。本文・実測値や全体の傾向は確認できていません。")
            gaps.append({"key": "catalog_content", "reason": reason})
        elif (str(e.get("tool_id") or "").startswith(("japan.law", "law.", "politics.", "policy."))
              and records(data) and not any(any(isinstance(row.get(key), str) and row[key].strip()
                  for key in ("text", "summary", "description", "speech_excerpt", "body_excerpt", "abstract")) for row in records(data))):
            gaps.append({"key": "record_content", "reason": f"{e.get('provider') or e.get('tool_id')}は題名・日付などの書誌情報までの取得です。本文の論点や内容の変化は未確認です。"})
        if payload_rows and all(row.get("is_procedural") for row in payload_rows):
            gaps.append({"key": "speech_substance", "reason": "取得できた発言は議事進行の記録です。政策の賛否・論点を比較するための実質的な審議内容は取得できていません。"})
        if (e.get("tool_id") == "space.space_weather"
                and any(row.get("Kp") is not None or row.get("indicator") == "geomagnetic_kp" for row in records(data))
                and not any(str(row.get("indicator") or "").startswith("solar_")
                            for packet in evidence_rows for row in records(packet.get("data") or {}))):
            gaps.append({"key": "measurement_scope", "reason": "今回の取得値は地磁気Kp指数です。太陽活動全体の変化や具体的な通信・電力への影響を、この指数だけで確認したとは扱えません。"})
        from .analysis_scope import requested_years
        if (not requested_years(message)
                and re.search(r"回復|現状|現在|最近|近年|recovery|current|recent", message, re.I)):
            from datetime import datetime
            periods = [int(str(row.get("date") or row.get("year") or "")[:4]) for row in records(data)
                       if re.match(r"^(?:19|20)\d{2}", str(row.get("date") or row.get("year") or ""))]
            if periods and max(periods) < datetime.now().year - 2:
                gaps.append({"key": "recency", "reason": f"{e.get('provider') or e.get('tool_id')}で取得できた最新年は{max(periods)}年です。依頼された現在・回復状況をこの過去系列だけでは確認できません。"})
    return gaps


def no_evidence_answer(requirements: list[dict], *, japanese: bool = True) -> str:
    labels = list(dict.fromkeys(str(item.get("label") or item.get("key") or "requested evidence") for item in requirements))
    if not japanese:
        return ("No verifiable evidence was retrieved for the requested comparison. The requested analysis remains incomplete.\n\n"
                + "\n".join("- Evidence needed: " + label for label in labels)
                + "\n\nA valid comparison requires matching subjects, observation periods, definitions and units.")
    return ("依頼された比較に使える根拠を取得できず、分析は未完了です。現在の動向や地域差は確認できません。\n\n"
            + "\n".join("- 未確認: " + label for label in labels)
            + "\n\n比較には、対象・観測期間・指標の定義・単位が揃ったデータが必要です。")


def trade_composition(evidence_rows: list[dict]) -> str:
    """HS2 shares use the same reporter/partner/period/flow TOTAL denominator."""
    from decimal import Decimal, InvalidOperation
    from .analysis_recovery import flattened_results
    groups = {}
    conflicts = set()
    for evidence in flattened_results(evidence_rows):
        if evidence.get("tool_id") != "trade.un_comtrade":
            continue
        source = str((evidence.get("sources") or [{}])[0].get("source_id") or "")
        for row in records(evidence.get("data") or {}):
            code = str(row.get("commodity_code") or "")
            if code != "TOTAL" and not re.fullmatch(r"\d{2}", code):
                continue
            try: value = Decimal(str(row.get("trade_value_usd")))
            except InvalidOperation: continue
            if not value.is_finite() or value < 0:
                continue
            key = tuple(str(row.get(field) or "") for field in ("reporter", "partner", "year", "flow_code"))
            bucket = groups.setdefault(key, {})
            if code in bucket and bucket[code][0] != value:
                conflicts.add(key)
            bucket[code] = (value, str(row.get("commodity") or code), source)
    valid = {key: values for key, values in groups.items() if key not in conflicts
             and "TOTAL" in values and values["TOTAL"][0] > 0 and len(values) > 1
             and sum(value[0] for code, value in values.items() if code != "TOTAL") <= values["TOTAL"][0] * Decimal("1.0001")}
    latest = {}
    for key, values in valid.items():
        scope = (key[0], key[1], key[3])
        if scope not in latest or key[2] > latest[scope][0][2]:
            latest[scope] = (key, values)
    lines = []
    for key, values in list(latest.values())[:16]:
        reporter, partner, year, flow = key
        total = values["TOTAL"][0]
        source = values["TOTAL"][2]
        lines.append(f"品目別構成（{reporter} → {partner}、{year}年、{flow}、分母TOTAL {total} US$） [{source}]")
        lines.extend(["| HS2 | 品目 | 金額（US$） | TOTAL比（%） |", "|---|---|---:|---:|"])
        for code, (value, title, _) in sorted(((code, value) for code, value in values.items() if code != "TOTAL"), key=lambda item: item[1][0], reverse=True)[:5]:
            share = value / total * 100
            lines.append(f"| {code} | {title.replace('|','／')} | {value} | {share:.2f} |")
        lines.append("取得したHS2品目の上位5件です。分母は同じ国・相手先・年・フローの全品目総額で、表示5品目の合計を100%とはしていません。")
    return "\n".join(lines)


def aligned_comparison(observations, label_for, number_for, preferred_areas=()) -> str:
    """Join actual observations on area and exact period, never on list order."""
    from .analysis_scope import observation_country, observation_identity
    groups = {}
    area_labels = {}
    for provider, source, metric, rows, _missing in observations:
        values = groups.setdefault((provider, source, metric), {})
        _, _, encoded = metric.partition("\x1f")
        qualifiers = json.loads(encoded) if encoded else {}
        calendar_basis = "fiscal" if qualifiers.get("fiscal_year_basis") else "calendar"
        for row in rows:
            area = str(row.get("geo_area") or row.get("country") or "対象地域")
            if area == "対象地域":
                continue  # An instrument/index without geography is not a country.
            key = observation_identity(row)
            area_labels.setdefault(key, area)
            values[(key, row["year"], calendar_basis)] = (row["value"], row.get("unit") or "")
    groups = {key: value for key, value in groups.items() if value}
    # A large classification cube needs a user-selected slice, not a table
    # that silently mixes or aggregates its dimensions.
    if not 2 <= len(groups) <= 6:
        return ""
    common = set.intersection(*(set(value) for value in groups.values()))
    if not common:
        return "同じ対象・同じ観測期間が揃う複数指標の組がなく、指標間の直接比較は未完了です。以下は各系列の独立した観測です。"
    periods = {(period, basis) for _area, period, basis in common}
    period, basis = max(periods, key=lambda p: (sum(item[1:] == p for item in common), p))
    areas = sorted((area for area, p, b in common if (p, b) == (period, basis)),
                   key=lambda area: (next((i for i, code in enumerate(preferred_areas)
                                          if code in observation_country(area_labels[area])), len(preferred_areas)), area))
    keys = list(groups)
    lines = [f"同一対象・同一期間で揃えた比較（{period}、取得範囲内）:",
             "| 国・地域 | " + " | ".join(label_for(key[2]).replace("|", "／") + f" [{key[1]}]" for key in keys) + " |",
             "| --- | " + " | ".join("---" for _key in keys) + " |"]
    for area in areas[:24]:
        cells = [number_for(groups[key][(area, period, basis)][0], groups[key][(area, period, basis)][1])
                 + groups[key][(area, period, basis)][1] for key in keys]
        lines.append("| " + area_labels[area].replace("|", "／") + " | " + " | ".join(cells) + " |")
    lines.append(f"比較可能な対象は{len(areas)}国・地域です" + (f"（表は24対象の抜粋）" if len(areas) > 24 else "")
                 + "。単位の異なる列を直接加減せず、相関や因果はこの表だけから判断できません。")
    return "\n".join(lines)
