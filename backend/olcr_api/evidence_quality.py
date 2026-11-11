"""Current-request evidence checks shared by API analysis and Web recovery.

Search rank, a request echo, and a matching tag are not evidence of relevance.
These checks screen observations; they never change a provider's wire format.
"""
from __future__ import annotations

import json
import re

from .analysis_scope import country_scope, explicit_countries, filter_scope
from .context_policy import relevance

# Bilingual subject identities, rather than complete-prompt recipes. More
# specific identities precede their parent topic (agents before AI).
_SUBJECTS = (
    (r"AIエージェント|自律エージェント|AI agents?|LLM agents?|agentic", r"エージェント|\bagents?\b|agentic|Claude Code|\bCodex\b|\bMCP\b.{0,60}\bLLM\b|\bLLM\b.{0,60}\bMCP\b", "AI agent"),
    (r"免疫療法|immunotherap", r"免疫療法|immunotherap", "immunotherapy"),
    (r"感染症|infectious diseases?", r"感染症|infectious|infection|malaria|influenza|tuberculosis|HIV|COVID|SARS-CoV", "infectious disease"),
    (r"生物多様性|biodiversity", r"生物多様性|biodiversity|species diversity|species richness|conservation|生態系", "biodiversity"),
    (r"気候|climate", r"気候|climat|greenhouse|温室効果|carbon emissions|脱炭素", "climate change"),
    (r"環境政策|environment(?:al)? polic", r"環境|environment|emissions|pollution|conservation|気候|climat", "environmental policy"),
    (r"人工知能|生成AI|\bAI\b|artificial intelligence|large language model", r"人工知能|生成AI|\bAI\b|artificial intelligence|large language models?|\bLLM\b", "artificial intelligence"),
    (r"地域開発|地方創生|regional development|desenvolvimento regional", r"地域開発|地方創生|地域振興|regional development|desenvolvimento regional|desarrollo regional", "regional development"),
    (r"医療政策|医療政策議論|healthcare policy", r"医療|healthcare|health care|public health|\bNHS\b|clinical|patients?", "healthcare policy"),
)


_RECOVERY_SUBJECTS = (
    (r"人口|出生|高齢|population|fertility", r"人口|出生|高齢|population|fertility|demograph", "population demographics"),
    (r"雇用|失業|賃金|employment|unemployment", r"雇用|失業|賃金|employment|unemployment|labou?r|wages?|payroll", "employment unemployment wages"),
    (r"物価|インフレ|inflation|\bCPI\b", r"物価|インフレ|inflation|consumer price|\bCPI\b", "consumer price inflation"),
    (r"成長|債務|経済指標|GDP|growth|debt", r"成長|債務|経済|GDP|growth|debt|economic|income", "economic growth debt"),
    (r"観光|航空便|運航|tourism|flights?", r"観光|航空|運航|touris|arrivals|aviation|passenger|air transport|flight", "tourism aviation passenger traffic"),
    (r"貿易|輸出|輸入|trade|export|import", r"貿易|輸出|輸入|trade|export|import", "trade exports imports"),
    (r"為替|国際収支|exchange rate|balance of payments", r"為替|国際収支|exchange rate|balance of payments|current account", "exchange rates balance of payments"),
    (r"電力|発電|エネルギー|燃料|electricity|energy|fuel", r"電力|発電|エネルギー|燃料|electricity|generation|energy|fuel", "electricity generation energy"),
    (r"大気|空気質|PM2|air quality", r"大気|空気質|PM2|air quality|pollution|ozone", "air quality pollution"),
    (r"気温|気象|temperature|weather", r"気温|気象|temperature|weather|meteorolog", "temperature weather observations"),
    (r"人道|避難|難民|紛争|humanitarian|refugee|conflict", r"人道|避難|難民|紛争|humanitarian|displace|refugee|conflict|relief", "humanitarian displacement conflict"),
    (r"制裁|sanctions?", r"制裁|sanctions?|consolidated list|SDN", "sanctions consolidated lists"),
    (r"政府支出|連邦支出|連邦政府支出|government spending|federal spending", r"支出|spending|outlays?|expenditure|obligations?", "federal spending expenditure"),
    (r"医療|臨床試験|health|clinical trials?", r"医療|臨床|health|clinical|trials?|NHS", "healthcare clinical trials"),
    (r"議会|審議|法令|議論|parliament|legislation", r"議会|審議|法令|議論|parliament|legislat|debate|proceedings|congress", "parliament legislation debates"),
    (r"水資源|水ストレス|農業|water stress|agriculture", r"水資源|水ストレス|農業|water|agricultur|irrigation", "water stress agriculture"),
)


def subject_queries(request: str, *, broad: bool = False) -> list[str]:
    found = [(pattern, query) for trigger, pattern, query in (_SUBJECTS + _RECOVERY_SUBJECTS if broad else _SUBJECTS)
             if re.search(trigger, request, re.I)]
    if any(query == "AI agent" for _, query in found):
        found = [(pattern, query) for pattern, query in found if query != "artificial intelligence"]
    return [query for _, query in found]


def subject_matches(request: str, content: str, *, broad: bool = False) -> bool:
    queries = set(subject_queries(request, broad=broad))
    patterns = [pattern for _, pattern, query in (_SUBJECTS + _RECOVERY_SUBJECTS if broad else _SUBJECTS) if query in queries]
    return not patterns or any(re.search(pattern, content, re.I) for pattern in patterns)


def request_countries(request: str) -> set[str]:
    wanted = country_scope(request)
    if not wanted and re.search(r"連邦規則|米議会|federal rule|Congress\.gov", request, re.I):
        wanted = {"US"}
    return wanted


def web_source_matches(request: str, source: dict, threshold: float = .35) -> bool:
    title = str(source.get("title") or "")
    snippet = str(source.get("snippet") or "")
    content = title + "\n" + snippet + "\n" + str(source.get("text") or "")[:16000]
    if not subject_matches(request, content, broad=True):
        return False
    # A title explicitly scoped to another country cannot answer the requested
    # jurisdiction. A Japanese-language page about US law remains eligible.
    wanted, titled = request_countries(request), set(explicit_countries(title))
    if wanted and titled and wanted.isdisjoint(titled):
        return False
    return bool(subject_queries(request, broad=True) or relevance(request, content) >= threshold)


def recovery_query(request: str, missing: list[dict]) -> str:
    """Keep the subject and scope; search for the missing dimensions only."""
    search_terms = {
        "growth": "GDP economic growth", "inflation": "consumer price inflation", "employment": "employment unemployment statistics",
        "population": "population regional statistics", "debt": "general government debt", "financial_conditions": "financial conditions index",
        "macro": "economic development indicators", "balance": "balance of payments statistics", "fx": "reference exchange rates",
        "tourism": "international tourist arrivals recovery regional statistics", "flights": "regional air transport passenger traffic statistics",
        "electricity": "electricity generation mix prices statistics", "energy": "energy consumption statistics",
        "air": "air quality PM2.5 seasonal observations", "weather": "temperature observations time series",
        "emissions": "greenhouse gas emissions statistics", "agriculture": "agriculture value added statistics",
        "water": "water stress indicator", "sdg": "sustainable development indicators",
        "humanitarian": "humanitarian needs response situation data", "displacement": "refugees internally displaced statistics",
        "conflict": "conflict event data", "sanctions": "sanctions consolidated list data",
        "spending": "federal spending outlays data", "parliament": "parliament debates proceedings",
        "rules": "proposed rules", "law": "legislation full text", "trials": "clinical trials studies data",
        "disasters": "disaster impact needs situation report", "earthquakes": "earthquake observations",
        "articles": "developer articles", "github": "repositories implementation",
    }
    measure_terms = {"EG.ELC.ACCS.ZS": "electricity access rate", "EG.ELC.RNEW.ZS": "renewable electricity share",
                     "EG.FEC.RNEW.ZS": "renewable energy share", "electricity_prices": "electricity prices",
                     "electricity_generation_by_source": "electricity generation by fuel",
                     "EG.ELC.PROD.KH": "electricity generation"}
    labels = " ".join(
        " ".join(dict.fromkeys(measure_terms.get(metric, item.get("label", ""))
                               for group in item["missing_measures"] for metric in group))
        if item.get("missing_measures") else search_terms.get(item.get("key"), item.get("label", ""))
        for item in missing)
    if not labels:
        return request[:400]
    countries = request_countries(request)
    names = {"JP": "Japan", "US": "United States", "GB": "United Kingdom", "CA": "Canada",
             "BR": "Brazil", "UA": "Ukraine", "KR": "South Korea", "FR": "France", "DE": "Germany"}
    scopes = " ".join((names[code] + " (" + code + ")") if code in names else code for code in sorted(countries))
    for pattern, label in ((r"EU|欧州|ヨーロッパ", "European Union"), (r"東南アジア", "Southeast Asia"),
                           (r"南アジア", "South Asia"), (r"アフリカ", "Africa"),
                           (r"中南米|ラテンアメリカ", "Latin America"), (r"サヘル", "Sahel")):
        if re.search(pattern, request, re.I):
            scopes = label
            break
    # Each gap query already carries its translated dimension. Adding every
    # sibling topic here would again require a single page to cover them all.
    subjects = " ".join(subject_queries(request))
    years = " ".join(dict.fromkeys(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", request)))
    # Prefer primary data owners for gaps, rather than generic explainers.
    domains = []
    primary = {"population": "worldbank.org", "growth": "worldbank.org", "debt": "imf.org",
               "employment": "ilo.org", "water": "fao.org", "agriculture": "fao.org",
               "space_weather": "swpc.noaa.gov", "spending": "fiscaldata.treasury.gov",
               "humanitarian": "unocha.org", "displacement": "unhcr.org", "conflict": "ucdp.uu.se"}
    keys = {item.get("key") for item in missing}
    if "JP" in countries and keys.intersection({"population", "employment", "housing"}):
        domains.append("stat.go.jp")
    provider_domains = {"japan.diet_transcript": "kokkai.ndl.go.jp", "japan.law": "laws.e-gov.go.jp",
                        "politics.uk_parliament": "parliament.uk", "politics.ca_openparliament": "ourcommons.ca",
                        "politics.br_chamber": "camara.leg.br", "politics.br_senate": "senado.leg.br",
                        "politics.eu_parliament": "europarl.europa.eu", "law.eu_cellar": "eur-lex.europa.eu",
                        "politics.us_congress": "congress.gov", "policy.us_federal_register": "federalregister.gov"}
    for item in missing:
        for tool in item.get("sources", []):
            domain = provider_domains.get(tool)
            if domain and domain not in domains:
                domains.append(domain)
        domain = primary.get(item.get("key"))
        if domain and domain not in domains:
            domains.append(domain)
    official = " OR ".join("site:" + domain for domain in domains[:3])
    if len(domains) > 1: official = "(" + official + ")"
    return " ".join(part for part in (subjects, scopes, years, labels, "official data", official) if part)[:500]


def recovery_queries(request: str, missing: list[dict]) -> list[tuple[list[dict], str]]:
    """Search independent missing dimensions instead of requiring one page to cover all."""
    groups = [[need] for need in missing] if missing else [[]]
    if len(groups) > 4:
        groups = groups[:3] + [[need for group in groups[3:] for need in group]]
    return [(group, recovery_query(request, group)) for group in groups]


_SEARCH_TOOLS = {
    "research.openalex", "knowledge.qiita", "knowledge.devto", "health.clinical_trials",
    "government.us_federal_register", "policy.us_federal_register", "policy.us_rulemaking",
    "politics.us_congress", "politics.eu_parliament", "politics.uk_parliament",
    "politics.ca_openparliament", "politics.br_chamber", "politics.br_senate", "law.eu_cellar",
    "humanitarian.hdx",
    "gov.us_spending", "defense.us_contracts", "software.github",
}
_TEXT_FIELDS = ("title", "name", "full_name", "brief_title", "description", "abstract", "snippet", "text",
                "summary", "body_excerpt", "conditions", "interventions", "scientificName")


def filter_provider_evidence(data: dict, request: str, tool_id: str) -> tuple[dict, int]:
    data, rejected = filter_scope(data, request)
    if rejected:
        # Aggregates made before scope filtering include rejected observations.
        # Keep the accepted records and let composition recompute from them.
        data.pop("analysis_summary", None)
        data.pop("aggregated_row_count", None)
    # Normalized rows are the only rendering/composition authority. Providers
    # often mirror them in raw_metadata/results; leaving those arrays intact
    # would reintroduce a rejected row through the generic nested renderer.
    if any(isinstance(data.get(key), list) for key in ("items", "rows", "works")):
        data = {key: value for key, value in data.items()
                if key not in {"raw_metadata", "raw", "results", "documents", "docs", "articles"}}
    for field in ("items", "rows", "works"):
        if isinstance(data.get(field), list):
            cleaned = []
            for row in data[field]:
                if isinstance(row, dict) and isinstance(row.get("data"), dict):
                    child, count = filter_provider_evidence(row["data"], request, tool_id)
                    rejected += count
                    row = {**row, "data": child}
                cleaned.append(row)
            data[field] = cleaned
    if tool_id == "humanitarian.hdx":
        data.update(evidence_kind="dataset_catalog", request_complete=False)
    if tool_id == "law.eu_cellar":
        data.update(evidence_kind="document_catalog", request_complete=False)
    if tool_id not in _SEARCH_TOOLS or not subject_queries(request):
        return data, rejected
    data = dict(data)
    for field in ("items", "rows", "works"):
        rows = data.get(field)
        if not isinstance(rows, list):
            continue
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("data"), dict):
                child = row["data"]
                if child.get("has_payload") is False:
                    continue
                kept.append({**row, "data": child})
                continue
            # Deliberately exclude query echoes, source labels, and tags.
            content = " ".join(json.dumps(row.get(key), ensure_ascii=False)
                               for key in _TEXT_FIELDS if row.get(key))
            if content and not subject_matches(request, content):
                rejected += 1
                continue
            if not content:
                data.update(subject_unverified=True, request_complete=False)
            kept.append(row)
        data[field] = kept
        if rows and not kept:
            data.update(has_payload=False, semantic_status="NO_RELEVANT_RESULT")
    if rejected:
        data.update(quality_rejected_count=rejected, request_complete=False)
    return data, rejected


def has_observations(data: dict) -> bool:
    if data.get("has_payload") is False or data.get("semantic_status") in {"EMPTY", "NO_RELEVANT_RESULT"}:
        return False
    if data.get("absence_description"):
        return True
    collections = [data[key] for key in ("items", "rows", "works") if isinstance(data.get(key), list)]
    if collections:
        return any(any(has_observations(row["data"]) if isinstance(row, dict) and isinstance(row.get("data"), dict)
                       else bool(row) for row in rows) for rows in collections)
    # An explicit no-match result is still useful negative evidence. Generic
    # envelope metadata (count=0, schema/version, echoed description) is not.
    return bool(data.get("has_payload", data.get("record") or data.get("analysis_summary")))
