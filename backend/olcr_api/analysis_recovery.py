"""Bounded, measure-level acquisition recovery through existing public adapters.

Provider failures are not evidence about a measure. Conversely a successful
provider cannot satisfy another measure merely because it shares that provider.
These alternatives name actual series, never complete benchmark prompts.
"""
from __future__ import annotations

import re


WORLD_BANK_ALTERNATIVES = {
    "growth": ("NY.GDP.MKTP.KD.ZG",),
    "inflation": ("FP.CPI.TOTL.ZG",),
    "employment": ("SL.UEM.TOTL.ZS",),
    "population": ("SP.POP.TOTL",),
    "macro": ("NY.GDP.PCAP.CD",),
    "tourism": ("ST.INT.ARVL",),
    "energy": ("EG.USE.PCAP.KG.OE",),
    "air": ("EN.ATM.PM25.MC.M3",),
    "agriculture": ("NV.AGR.TOTL.ZS",),
    "water": ("ER.H2O.FWST.ZS",),
}


def flattened_results(results):
    for result in results:
        data = result.get("data") or {}
        children = data.get("items") or []
        if data.get("render_nested_evidence") and children and all(
                isinstance(row, dict) and isinstance(row.get("data"), dict) for row in children):
            yield from flattened_results([{**result, "data": row["data"]} for row in children])
        else:
            yield result


def recovery_requests(message, results, needs, compile_arguments, attempted=()):
    """Only retry a missing measure with a *different* validated request.

    No widening explicit dates, geography, frequency or source-only restrictions.
    Repeating an unchanged unavailable request cannot improve evidence.
    """
    if re.search(r"(?:API|World\s*Bank|世界銀行|FRED|OECD).{0,12}(?:だけ|のみ)|only.{0,20}(?:API|World\s*Bank|FRED|OECD)", message, re.I):
        return []
    if re.search(r"地域別人口|都道府県|市区町村|州別|subnational|by state|municipal", message, re.I):
        return []
    exact_subannual = bool(re.search(r"月次|月別|日次|季節|monthly|daily|seasonal", message, re.I))
    existing = set()
    for result in flattened_results(results):
        data = result.get("data") or {}
        if result.get("tool_id") not in {"econ.worldbank", "statistics.world_bank"}:
            continue
        # Even an empty series is an attempted series. Do not repeat it.
        existing.update(data.get("indicators") or [data.get("indicator")])
    for attempt in attempted:
        if attempt.get("tool_id") in {"econ.worldbank", "statistics.world_bank"}:
            args = attempt.get("arguments") or {}
            if not args.get("unsupported_capability"):
                if not args.get("indicators") and not args.get("indicator"):
                    return []  # Cannot prove a new request differs from this attempt.
                existing.update(args.get("indicators") or [args.get("indicator")])
    codes = []
    covered_needs = []
    for need in needs:
        if exact_subannual and need["key"] in {"inflation", "employment", "energy", "air"}:
            continue
        alternatives = WORLD_BANK_ALTERNATIVES.get(need["key"], ())
        new = [code for code in alternatives if code not in existing]
        if new:
            codes.extend(new)
            covered_needs.append(need["key"])
    if not codes:
        return []
    arguments = compile_arguments("econ.worldbank", message)
    if arguments.get("unsupported_capability"):
        return []
    arguments.update(indicator=codes[0], indicators=list(dict.fromkeys(codes))[:8])
    from .analysis_scope import scope_indicator_requests
    arguments = scope_indicator_requests(arguments, message, arguments["indicators"])
    # The normal compiler owns all current-turn entity and date constraints.
    return [{"tool_id": "econ.worldbank", "arguments": arguments,
             "needs": covered_needs, "reason": "missing_measure_public_alternative"}]


def unresolved_failures(message, failures, needs):
    """Retain diagnostics without allowing an optional failed provider to veto recovery."""
    if re.search(r"(?:API|World\s*Bank|世界銀行|FRED|OECD).{0,12}(?:だけ|のみ)", message, re.I):
        return failures
    unresolved_tools = {tool for need in needs for tool in need.get("sources", [])}
    return [failure for failure in failures if failure.get("tool_id") in unresolved_tools]
