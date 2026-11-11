"""Bounded, category-aware travel research with explicit evidence coverage."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from .generation_control import check_cancelled


CATEGORY_QUERIES = {
    "transport": "{origin} {destination} 交通 アクセス 公式",
    "lodging": "{destination} 宿泊 ホテル 公式 料金",
    "food": "{destination} 食事 飲食店 公式",
    "activities": "{destination} 観光 体験 施設 公式 {interests}",
    "hours": "{destination} 営業時間 休業 公式",
}
CATEGORY_LABELS = {
    "transport": "交通", "lodging": "宿泊", "food": "食事",
    "activities": "観光・体験", "hours": "営業時間・休業", "weather": "天気",
}


def _safe_url(value: str) -> bool:
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
            return False
        parts = urlsplit(value)
        return parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
    except (TypeError, ValueError):
        return False


def weather_evidence_for_date(result: dict, requested_date: str) -> dict | None:
    """Return a single requested date only when the provider payload matches it."""
    data = result.get("data") if isinstance(result, dict) else None
    daily = data.get("daily") if isinstance(data, dict) else None
    days = daily.get("time") if isinstance(daily, dict) else None
    if not isinstance(days, list) or requested_date not in days:
        return None
    index = days.index(requested_date)
    forecast = {"date": requested_date}
    for key, values in daily.items():
        if key == "time" or not isinstance(values, list) or index >= len(values):
            continue
        forecast[key] = values[index]
    return {"date": requested_date, "location": data.get("location"), "forecast": forecast,
            "provider": "Open-Meteo"}


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def collect_evidence(brief, settings) -> tuple[list[dict], list[dict], list[str]]:
    from . import external_tools, web
    from .permissions import capability_enabled

    sources: list[dict] = []
    context: list[dict] = []
    warnings: list[str] = []
    coverage = {key: {"status": "not_checked", "source_ids": []} for key in (*CATEGORY_QUERIES, "weather")}

    def finish():
        context.append({"coverage": coverage})
        return sources, context, warnings

    if not capability_enabled(settings, "external_access_enabled"):
        warnings.append("外部アクセスがOFFのため、最新情報は未確認です。")
        return finish()

    provider = getattr(settings, "web_provider", "none")
    search = {"brave": web.brave_search, "tavily": web.tavily_search,
              "duckduckgo": web.search}.get(provider)
    web_enabled = getattr(settings, "web_mode", "off") != "off" and provider != "none" and search is not None
    if not web_enabled:
        warnings.append("Web検索がOFFのため、宿泊・交通・食事・施設情報は未確認です。")
    else:
        values = {"origin": brief.origin or "", "destination": brief.destination or "",
                  "interests": brief.interests or ""}
        fetched = 0
        fetched_urls: set[str] = set()
        for category, pattern in CATEGORY_QUERIES.items():
            query = pattern.format(**values).strip()
            if not brief.origin and category == "transport":
                coverage[category] = {"status": "not_checked", "source_ids": [], "reason": "出発地が未指定"}
                continue
            try:
                check_cancelled()
                if not capability_enabled(settings, "external_access_enabled"):
                    warnings.append("調査中に外部アクセス許可が無効になりました。")
                    break
                rows = search(query[:400], 3)[:3]
                category_ids = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    url = row.get("url", "")
                    if not _safe_url(url):
                        continue
                    found = next((item for item in sources if item["url"] == url), None)
                    if found is None:
                        found = {"id": len(sources) + 1,
                                 "title": str(row.get("title") or url)[:300], "url": url,
                                 "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                 "kind": "検索結果の抜粋（本文未確認）", "categories": []}
                        sources.append(found)
                    if category not in found["categories"]:
                        found["categories"].append(category)
                    category_ids.append(found["id"])
                    excerpt = str(row.get("snippet") or row.get("description") or "")[:2500]
                    kind = "検索結果の抜粋（本文未確認）"
                    if url not in fetched_urls and fetched < 6 and capability_enabled(settings, "external_access_enabled"):
                        try:
                            check_cancelled()
                            page = web.fetch(url)
                            body = page.get("clean_markdown", "") if isinstance(page, dict) else ""
                            if body:
                                excerpt, kind = str(body)[:6000], "ページ本文"
                                fetched += 1
                                fetched_urls.add(url)
                        except Exception:
                            check_cancelled()
                    found["kind"] = kind
                    context.append({"source_id": found["id"], "category": category,
                                    "kind": kind, "excerpt": excerpt})
                coverage[category] = {"status": "checked" if category_ids else "no_results",
                                      "source_ids": sorted(set(category_ids))}
            except Exception:
                check_cancelled()
                coverage[category] = {"status": "failed", "source_ids": []}
                warnings.append(f"{CATEGORY_LABELS[category]}の検索に失敗しました。予約前に最新情報を確認してください。")

    # Forecasts are fetched one requested date at a time and attached only when
    # the provider returns that exact date. We never reuse today's weather for a trip.
    if brief.start_date and brief.end_date:
        try:
            start, end = date.fromisoformat(brief.start_date), date.fromisoformat(brief.end_date)
        except ValueError:
            start = end = None
        if start and end:
            today = _today_utc()
            eligible = [start + timedelta(days=n) for n in range((end - start).days + 1)
                        if 0 <= ((start + timedelta(days=n)) - today).days <= 14]
            forecasts = []
            if eligible and capability_enabled(settings, "external_access_enabled"):
                for trip_day in eligible:
                    try:
                        check_cancelled()
                        result = external_tools.weather(location=brief.destination, date=trip_day.isoformat())
                        record = weather_evidence_for_date(result, trip_day.isoformat())
                        if record:
                            forecasts.append(record)
                            source_meta = (result.get("sources") or [{}])[0] if isinstance(result, dict) else {}
                            sid = len(sources) + 1
                            sources.append({"id": sid, "title": "Open-Meteo（" + trip_day.isoformat() + "の予報）",
                                            "url": "https://open-meteo.com/",
                                            "retrieved_at": source_meta.get("retrieved_at") or datetime.now(timezone.utc).isoformat(),
                                            "kind": "日付一致の予報", "categories": ["weather"]})
                            context.append({"weather": record, "source_id": sid})
                    except Exception:
                        check_cancelled()
                coverage["weather"] = {"status": "checked" if forecasts else "no_results",
                                       "source_ids": [s["id"] for s in sources if "weather" in s.get("categories", [])]}
            else:
                coverage["weather"] = {"status": "not_checked", "source_ids": [],
                                        "reason": "予報期間外"}
            if not forecasts:
                warnings.append("旅行期間の天気予報は未確認です。出発前に日ごとの予報を確認してください。")
    else:
        coverage["weather"] = {"status": "not_checked", "source_ids": [], "reason": "日程未確定"}
    for category, record in coverage.items():
        if record["status"] in {"not_checked", "no_results", "failed"}:
            label = CATEGORY_LABELS[category]
            warnings.append(f"{label}：未確認（{record.get('reason') or '根拠が見つかっていません'}）。")
    return finish()
