"""Evidence obligations derived from the current request, independent of model choice.

These are capability cues, not complete-prompt recipes. A provider may cover
several obligations (e.g. World Bank indicators); aliases must be deduplicated
by the caller. The model can add relevant sources but cannot omit obligations.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from .analysis_scope import indicator_id, country_scope, observation_country, requested_years
from .evidence_quality import has_observations
from . import regional_statistics


@dataclass(frozen=True)
class EvidenceNeed:
    key: str
    label: str
    pattern: str
    sources: tuple[str, ...]


_NEEDS = (
    EvidenceNeed("growth", "所得・経済成長", r"GDP|国内総生産|(?:所得|経済)?成長|income growth", ("econ.worldbank",)),
    EvidenceNeed("inflation", "物価", r"物価|インフレ|CPI|inflation|consumer prices?", ("econ.worldbank",)),
    EvidenceNeed("employment", "雇用", r"雇用|失業|労働市場|employment|unemployment|payroll", ("econ.oecd",)),
    EvidenceNeed("wages", "賃金", r"賃金|給与|wages?|salary", ()),
    EvidenceNeed("population", "人口", r"人口|出生|高齢|population|fertility|ageing", ("econ.worldbank",)),
    EvidenceNeed("debt", "債務", r"債務|debt", ("econ.imf",)),
    EvidenceNeed("financial_conditions", "金融環境", r"金融環境|financial conditions", ()),
    EvidenceNeed("macro", "経済・開発指標", r"経済指標|地域開発指標|economic indicators|development indicators", ("econ.worldbank",)),
    EvidenceNeed("housing", "住宅費・家計", r"住宅費|家計|housing costs?|household", ()),
    EvidenceNeed("balance", "国際収支", r"国際収支|balance of payments", ()),
    EvidenceNeed("agriculture", "農業指標", r"農業|agriculture", ("econ.worldbank",)),
    EvidenceNeed("health_metrics", "健康指標", r"健康指標|health indicators", ()),
    EvidenceNeed("rates", "金利", r"金利|interest rates?|policy rates?", ("econ.fred",)),
    EvidenceNeed("trade", "貿易", r"貿易|輸出|輸入|trade|exports?|imports?", ("trade.un_comtrade",)),
    EvidenceNeed("fx", "為替", r"為替|exchange rates?|\bFX\b", ("currency.frankfurter",)),
    EvidenceNeed("tourism", "観光", r"観光|tourism|tourist|arrivals", ("econ.worldbank",)),
    EvidenceNeed("electricity", "電力・再生可能エネルギー", r"電力|発電|再生可能|再エネ|electricity|renewable", ("econ.worldbank",)),
    EvidenceNeed("energy", "エネルギー", r"エネルギー消費|燃料|energy consumption|fuel", ("energy.us_eia",)),
    EvidenceNeed("emissions", "温室効果ガス", r"温室効果|排出|CO2|CO₂|emissions?|greenhouse", ("econ.worldbank",)),
    EvidenceNeed("air", "大気質", r"大気|大気汚染|PM2|PM\s*2|air quality|air pollution", ("environment.air_quality",)),
    EvidenceNeed("weather", "気象", r"気温|天気|降水|temperature|weather", ("weather.open_meteo",)),
    EvidenceNeed("earthquakes", "地震", r"地震|earthquakes?", ("earth.earthquakes",)),
    EvidenceNeed("disasters", "自然災害", r"災害|津波|山火事|洪水|disasters?|tsunami|wildfire|flood", ("earth.disasters",)),
    EvidenceNeed("river", "河川水位", r"河川|水位|流量|streamflow|river level", ("earth.streamflow",)),
    EvidenceNeed("elevation", "標高", r"標高|elevation", ("geo.elevation",)),
    EvidenceNeed("tides", "潮汐", r"潮汐|潮位|満潮|干潮|tides?", ("ocean.tides",)),
    EvidenceNeed("water", "水資源", r"水資源|水ストレス|water stress|water resources", ("development.un_sdg", "econ.worldbank")),
    EvidenceNeed("sdg", "SDG指標", r"SDGs?|持続可能|sustainability", ("development.un_sdg",)),
    EvidenceNeed("space_weather", "宇宙天気", r"太陽活動|宇宙天気|地磁気|solar activity|space weather", ("space.space_weather",)),
    EvidenceNeed("conflict", "紛争イベント", r"紛争|武力|暴力イベント|conflict|violence events?", ("conflict.ucdp",)),
    EvidenceNeed("displacement", "難民・避難", r"避難|難民|refugees?|displacement", ("humanitarian.unhcr",)),
    EvidenceNeed("humanitarian", "人道支援・ニーズ", r"人道|支援ニーズ|支援要請|支援状況|食料危機|humanitarian|aid needs|food crisis", ("humanitarian.hdx",)),
    EvidenceNeed("sanctions", "制裁", r"制裁|sanctions?", ("sanctions.opensanctions",)),
    EvidenceNeed("flights", "航空機運航", r"航空機運航|航空便|運航規模|aircraft|flights?|air traffic", ("aviation.opensky",)),
    EvidenceNeed("aviation_weather", "航空気象", r"航空気象|METAR|TAF|aviation weather", ("aviation.operational_weather",)),
    EvidenceNeed("spending", "政府契約・支出", r"政府契約|政府支出|連邦支出|連邦政府支出|契約支出|government contracts?|federal spending", ("gov.us_spending",)),
    EvidenceNeed("election_finance", "選挙資金", r"選挙資金|campaign financ", ("politics.us_campaign_finance",)),
    EvidenceNeed("parliament", "議会審議", r"議会|議会法案|審議|両院|国会|法案|parliament|congress|legislatur", ()),
    EvidenceNeed("rules", "規則制定", r"規則|規則案|rulemaking|regulat", ("policy.us_federal_register",)),
    EvidenceNeed("law", "法令", r"法令|関連法|law text|legislation", ()),
    EvidenceNeed("courts", "裁判例", r"裁判|判例|court|case law", ("law.us_courts",)),
    EvidenceNeed("trials", "臨床試験", r"臨床試験|治験|clinical trials?", ("health.clinical_trials",)),
    EvidenceNeed("adverse_events", "FDA有害事象", r"有害事象|副作用|FDA|adverse events?", ("health.openfda",)),
    EvidenceNeed("research", "学術研究", r"論文|研究|学術|papers?|research|publications?", ("research.openalex",)),
    EvidenceNeed("protein", "タンパク質", r"タンパク質|蛋白|protein", ("biology.protein",)),
    EvidenceNeed("occurrence", "生物観察", r"生物多様性|観察記録|出現記録|biodiversity|occurrence", ("biology.occurrence",)),
    EvidenceNeed("phylogeny", "系統関係", r"系統関係|系統樹|phylogen", ("biology.phylogeny",)),
    EvidenceNeed("cve", "脆弱性", r"脆弱性|CVE|vulnerabilit", ("security.cve",)),
    EvidenceNeed("exploit", "悪用可能性", r"悪用可能性|エクスプロイト|exploit", ("security.exploit_probability",)),
    EvidenceNeed("articles", "開発記事", r"開発記事|技術記事|実装記事|Qiita|DEV\.to|developer articles?", ("knowledge.qiita",)),
    EvidenceNeed("github", "実装活動", r"GitHub|実装活動|repositories", ("software.github",)),
    EvidenceNeed("nutrition", "食品表示・栄養", r"栄養|食品表示|nutrition|food label", ("food.product_lookup",)),
    EvidenceNeed("recipes", "レシピ", r"レシピ|recipes?", ("food.recipe",)),
)


def evidence_needs(message: str) -> list[dict]:
    """Return independent obligations; never consult previous messages or model output."""
    from .analysis_scope import expand_shared_measure_heads
    message = expand_shared_measure_heads(message)
    needs = []
    jp = bool(re.search(r"日本|Japan|Japanese", message, re.I))
    us = bool(re.search(r"米国|米議会|日米欧|米欧|連邦規則|United States|\bUS\b|\bU\.S\.", message, re.I))
    eu = bool(re.search(r"欧州|ヨーロッパ|日米欧|米欧|(?<![A-Za-z])EU(?![A-Za-z])|Europe", message, re.I))
    multi_region = sum((jp, us, eu, bool(re.search(r"韓国|Korea|カナダ|Canada", message, re.I)))) > 1
    resource_research = (bool(re.search(r"研究|論文|papers?|research", message, re.I))
                         and bool(re.search(r"使用|消費|測定|usage|consumption|measur", message, re.I))
                         and not re.search(r"各国|国別|発電|電力アクセス|貿易|by country", message, re.I))
    for need in _NEEDS:
        if not re.search(need.pattern, message, re.I):
            continue
        if need.key == 'population' and regional_statistics.supported(message):
            needs.append({'key': 'regional_population_density', 'label': '行政区域の常住人口密度', 'sources': ['econ.jp_estat']})
            continue
        if need.key in {'population', 'employment'} and regional_statistics.prefecture_supported(message):
            needs.append({'key': need.key, 'label': need.label + '（都道府県・統計ダッシュボード）', 'sources': ['econ.jp_estat']})
            continue
        # Compound subjects are not evidence obligations for each substring.
        if need.key == "weather" and not re.search(need.pattern, re.sub(r"宇宙天気|space weather", "", message, flags=re.I), re.I):
            continue
        sources = need.sources
        if resource_research and need.key in {"electricity", "energy", "weather", "water", "emissions"}:
            continue
        if (need.key == "energy" and re.search(r"再生可能|renewable", message, re.I)
                and re.search(r"エネルギー消費(?:全体)?(?:に占める|の比率)|share of.*energy consumption", message, re.I)):
            continue
        if need.key == "inflation":
            sources = ("econ.worldbank",) if multi_region else (("econ.jp_estat",) if jp else ("econ.fred",) if us else ("econ.eurostat",) if eu else sources)
        elif need.key == "employment":
            sources = ("econ.us_labor",) if us and not multi_region else ("econ.worldbank",) if multi_region else sources
        elif need.key == "wages" and us and not multi_region:
            sources = ("econ.fred",)
        elif need.key == "rates" and not us:
            # The current ECB adapter is exchange rates, not policy rates.
            sources = ("econ.fred",) if re.search(r"日米欧|米欧", message) else ()
        elif need.key == "electricity" and us and not multi_region:
            sources = (("energy.us_eia",) if re.search(r"構成|生産|発電|mix|generation|production", message, re.I)
                       and not re.search(r"電力アクセス|electrification|access to electricity", message, re.I)
                       else ("econ.worldbank",))
        elif need.key == "electricity" and eu and re.search(r"電力価格|electricity prices?", message, re.I):
            sources = ("econ.eurostat",)
        elif need.key == "flights" and re.search(r"規模|地域別|推移|回復|annual|regional|trend|recovery", message, re.I):
            # OpenSky's current aircraft snapshot is not a historical
            # national/region comparison. Carrier departures are an explicitly
            # labelled proxy, not a count of all flights in an airspace.
            sources = ("econ.worldbank",)
        elif need.key == "energy" and not us:
            sources = ("econ.worldbank",)
        elif need.key == "trade" and re.search(r"輸入依存|energy import dependence|energy dependency", message, re.I):
            sources = ("econ.worldbank",)
        elif need.key == "air" and re.search(r"PM\s*2\.?5|PM2_5|微小粒子", message, re.I) and re.search(r"年次|推移|近年|annual|long.term", message, re.I):
            sources = ("econ.worldbank",)
        elif need.key == "sanctions":
            sources = (("sanctions.unsc", "sanctions.us_ofac") if re.search(r"国連|United Nations|UNSC", message, re.I) and us
                       else ("sanctions.unsc",) if re.search(r"国連|UNSC", message, re.I)
                       else ("sanctions.us_ofac",) if us else sources)
        elif need.key == "parliament":
            sources = (("japan.diet_transcript",) if jp else ("politics.uk_parliament",) if re.search(r"英国|イギリス|\bUK\b|Brit", message, re.I)
                       else ("politics.ca_openparliament",) if re.search(r"カナダ|Canada", message, re.I)
                       else ("politics.br_chamber", "politics.br_senate") if re.search(r"ブラジル|Brazil", message, re.I)
                       else ("politics.eu_parliament",) if eu else ("politics.us_congress",) if us else ())
            if re.search(r"ブラジル|Brazil", message, re.I) and not re.search(r"上下|両院|both chambers", message, re.I):
                if re.search(r"上院|Senado|Senate", message, re.I):
                    sources = ("politics.br_senate",)
                elif re.search(r"下院|Chamber|Câmara", message, re.I):
                    sources = ("politics.br_chamber",)
        elif need.key == "law":
            sources = ("japan.law",) if jp else ("law.eu_cellar",) if eu else ()
        elif need.key == "rules" and not us:
            sources = ()
        elif need.key == "articles" and re.search(r"DEV\.to", message, re.I):
            sources = ("knowledge.devto",)
        needs.append({"key": need.key, "label": need.label, "sources": list(sources)})
    return needs


def required_sources(message: str) -> list[str]:
    return list(dict.fromkeys(tool for need in evidence_needs(message) for tool in need["sources"]))


def missing_coverage(message: str, succeeded: set[str], canonicalize, results=None) -> list[dict]:
    # A successful provider is not proof that all requested measures from that
    # provider were returned. Check actual observations, not requested IDs.
    def complete_payload(data: dict) -> bool:
        """A provider response marked partial cannot satisfy a full evidence need."""
        if not has_observations(data) or str(data.get("evidence_kind") or "").endswith("_catalog"):
            return False
        if data.get("request_complete") is False or data.get("sample_only") or data.get("truncated") or data.get("missing_fields") or data.get("missing_requests"):
            return False
        for key in ("items", "rows", "works"):
            rows = data.get(key)
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and isinstance(row.get("data"), dict):
                        if not complete_payload(row["data"]):
                            return False
        return True

    worldbank_metrics = set()
    observations: dict[str, dict[str, set[int]]] = {}
    def metrics(data):
        for row in data.get("items") or data.get("rows") or []:
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("data"), dict):
                metrics(row["data"])
            elif row.get("value") not in (None, ""):
                metric = indicator_id(row.get("indicator"), data.get("indicator"))
                countries = (observation_country(row.get("country")) | observation_country(row.get("countryiso3code"))
                             | observation_country(row.get("geo")))
                years = {int(y) for y in re.findall(r"(?:19|20)\d{2}", str(row.get("date") or row.get("year") or ""))}
                for country in countries or {"unknown"}:
                    observations.setdefault(metric, {}).setdefault(country, set()).update(years)
    for result in results or []:
        if (canonicalize(result.get("tool_id")) == "statistics.world_bank"
                and isinstance(result.get("data"), dict) and not result["data"].get("sample_only")):
            metrics(result.get("data") or {})
    from .analysis_recovery import flattened_results
    for result in flattened_results(results or []):
        data = result.get("data") or {}
        if (canonicalize(result.get("tool_id")) != "statistics.world_bank"
                and not data.get("sample_only") and not str(data.get("evidence_kind") or "").endswith("_catalog")):
            metrics(data)
    from .analysis_scope import country_scope_for_indicator
    wanted_years = requested_years(message)
    for metric, countries in observations.items():
        targets = country_scope_for_indicator(message, metric) or set(countries)
        if not targets.issubset(countries):
            continue
        if wanted_years and any(not wanted_years.issubset(countries[country]) for country in targets):
            continue
        if (not wanted_years and re.search(r"推移|変化|trend|changes?", message, re.I)
                and any(len(countries[country]) < 2 for country in targets)):
            continue
        worldbank_metrics.add(metric)

    complete_tools = {
        canonicalize(result.get("tool_id"))
        for result in (results or [])
        if isinstance(result, dict) and isinstance(result.get("data"), dict)
        and complete_payload(result["data"])
    }
    expected = {
        "growth": [{"NY.GDP.MKTP.KD.ZG", "NY.GDP.MKTP.KD", "NY.GDP.MKTP.CD", "NY.GDP.PCAP.CD"}],
        "inflation": [{"FP.CPI.TOTL.ZG"}], "employment": [{"SL.UEM.TOTL.ZS"}],
        "population": [{"SP.POP.TOTL", "SP.DYN.TFRT.IN", "SP.POP.65UP.TO.ZS"}],
        "tourism": [{"ST.INT.ARVL"}], "emissions": [{"EN.GHG.ALL.MT.CE.AR5", "EN.GHG.ALL.PC.CE.AR5"}] if re.search(r"温室効果ガス|greenhouse", message, re.I)
                     else [{"EN.ATM.CO2E.KT", "EN.GHG.CO2.PC.CE.AR5", "EN.GHG.CO2.MT.CE.AR5"}],
        "energy": [{"EG.USE.PCAP.KG.OE", "EG.USE.ELEC.KH.PC", "EG.IMP.CONS.ZS"}],
        "air": [{"EN.ATM.PM25.MC.M3"}],
        "flights": [{"IS.AIR.DPRT"}],
        "water": [{"ER.H2O.FWST.ZS"}], "agriculture": [{"NV.AGR.TOTL.ZS"}],
        "macro": [{"NY.GDP.PCAP.CD", "NY.GDP.MKTP.KD.ZG", "NY.GDP.MKTP.CD"}],
    }
    electricity = []
    if re.search(r"再生可能|再エネ|renewable", message, re.I):
        electricity.append({"EG.ELC.RNEW.ZS"} if re.search(r"電力|発電|electricity", message, re.I)
                           else {"EG.ELC.RNEW.ZS", "EG.FEC.RNEW.ZS"})
    if re.search(r"電力アクセス|電化|電力を利用|電気を利用|access.*electricity", message, re.I):
        electricity.append({"EG.ELC.ACCS.ZS"})
    if re.search(r"電源構成|電力構成|electricity mix", message, re.I):
        electricity.append({"electricity_generation_by_source"})
    elif re.search(r"電力生産|electricity production", message, re.I):
        electricity.append({"EG.ELC.PROD.KH", "electricity_generation_by_source"})
    if re.search(r"電力価格|electricity prices?", message, re.I):
        electricity.append({"electricity_prices"})
    expected["electricity"] = electricity or [{"EG.ELC.PROD.KH", "EG.ELC.ACCS.ZS", "EG.ELC.RNEW.ZS", "EG.FEC.RNEW.ZS"}]
    if re.search(r"季節|seasonal|by season", message, re.I):
        worldbank_metrics.discard("EN.ATM.PM25.MC.M3")
    missing = []
    for need in evidence_needs(message):
        # Explicit comparisons of named lists/chambers require each side.
        # They are not interchangeable alternative providers for one measure.
        combine = all if need["key"] in {"sanctions", "parliament"} and len(need["sources"]) > 1 else any
        matched = bool(need["sources"] and combine(
            canonicalize(tool) in succeeded and
            (results is None or canonicalize(tool) in complete_tools)
            for tool in need["sources"]
        ))
        groups = expected.get(need["key"])
        if regional_statistics.prefecture_supported(message) and need["key"] in {"population", "employment"}:
            groups = [{"jp_regional_" + need["key"]}]
        wb_expected = groups and any(canonicalize(tool) == "statistics.world_bank" for tool in need["sources"])
        typed_native = bool(need["key"] == "electricity" and groups
                            and "econ.eurostat" in need["sources"]) or bool(
                            regional_statistics.prefecture_supported(message) and need["key"] in {"population", "employment"})
        if results is not None and groups and (wb_expected or typed_native or worldbank_metrics):
            has_measures = all(group.intersection(worldbank_metrics) for group in groups)
            if wb_expected or typed_native:
                # A missing sibling indicator does not invalidate a complete
                # measure. Conversely one country's value cannot establish a
                # comparison covering all requested countries/periods.
                matched = bool(has_measures)
                if not matched:
                    labels = {"EG.ELC.ACCS.ZS": "電力アクセス率", "EG.ELC.RNEW.ZS": "再生可能電力比率",
                              "EG.FEC.RNEW.ZS": "再生可能エネルギー比率", "electricity_prices": "電力価格",
                              "EG.ELC.PROD.KH": "電力生産・構成"}
                    absent = [group for group in groups if not group.intersection(worldbank_metrics)]
                    names = [labels.get(next(iter(sorted(group))), need["label"]) for group in absent]
                    need = {**need, "label": "、".join(dict.fromkeys(names)),
                            "missing_measures": [sorted(group) for group in absent]}
            elif has_measures:
                matched = True
        if not matched:
            relevant_tools = {canonicalize(tool) for tool in need["sources"]}
            observed = any(
                canonicalize(result.get("tool_id")) in relevant_tools
                and has_observations(result.get("data") or {})
                for result in results or []
            )
            if groups and (wb_expected or typed_native or observations):
                # Values for a sibling measure do not make this measure partial.
                observed = (observed if not (wb_expected or typed_native) else False) or any(group.intersection(observations) for group in groups)
            missing.append({**need, "coverage_status": "partial" if observed else "missing"})
    return missing
