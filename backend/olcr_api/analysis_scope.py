"""Geography and observation-period checks shared by analytical API calls."""
from __future__ import annotations

from datetime import datetime, timezone
import re


def expand_shared_measure_heads(text: str) -> str:
    """Expand Japanese coordinated nouns without dropping a shared measure head."""
    return re.sub(r"([\u3400-\u9fff]{2,8})[・/／]([\u3400-\u9fff]{2,8})(指標|統計|データ)",
                  lambda m: m[1] + m[3] + "・" + m[2] + m[3], text)


def indicator_id(value, default="") -> str:
    return str(value.get("id") or default) if isinstance(value, dict) else str(value or default)


def country_scope_for_indicator(message: str, metric: str) -> set[str]:
    """Bind broad geographic modifiers to their measure's clause.

    A national foreground and an international background are independent
    retrieval scopes. Neither may erase the other's countries.
    """
    cues = ((r"NY\.GDP", r"GDP|国内総生産|成長|経済指標"),
            (r"SP\.", r"人口|population"),
            (r"FP\.CPI", r"物価|インフレ|inflation|CPI"),
            (r"SL\.", r"雇用|失業|employment"),
            (r"ST\.INT", r"観光|tourism"))
    for code, cue in cues:
        if not re.search(code, metric, re.I):
            continue
        for clause in re.split(r"[、。;；]|\bwhile\b", message):
            if (re.search(cue, clause, re.I)
                    and re.search(r"各国|世界各国|全世界|国別|worldwide|all countries|across countries", clause, re.I)
                    and not any(re.search(pattern, clause, re.I) for pattern, _ in _GROUPS)):
                return set()  # Explicitly global for this measure, not unknown.
    return country_scope(message)


def scope_indicator_requests(arguments: dict, message: str, indicators: list[str]) -> dict:
    """Group only measures that share the same explicit geographic scope."""
    arguments = {key: value for key, value in arguments.items() if key != "requests"}
    countries = arguments.get("countries") or []
    grouped = {}
    for code in indicators:
        scope = country_scope_for_indicator(message, code)
        selected = countries if not scope or set(countries) == scope else sorted(scope)
        if (not scope and country_scope(message)
                and re.search(r"各国|国別|all countries|across countries", message, re.I)):
            selected = ["all"]
        grouped.setdefault(tuple(selected), []).append(code)
    if len(grouped) > 1:
        arguments["requests"] = [{"countries": list(scope), "country": scope[0],
            "indicator": codes[0], "indicators": codes} for scope, codes in grouped.items() if scope]
    elif grouped:
        selected = list(next(iter(grouped)))
        if selected:
            arguments.update(countries=selected, country=selected[0])
    return arguments

# ISO identities, not a provider's arbitrary example countries. ISO3 is useful
# for SDMX/World Bank rows while Japanese/English names occur in user requests.
_COUNTRIES = {
    "JP": ("JPN", "日本", "Japan", "Japanese"), "KR": ("KOR", "韓国", "South Korea", "Korea", "Korean"),
    "US": ("USA", "米国", "United States", "U.S."), "CA": ("CAN", "カナダ", "Canada", "Canadian"),
    "GB": ("GBR", "英国", "イギリス", "United Kingdom", "Britain", "British"),
    "DE": ("DEU", "ドイツ", "Germany", "German"), "FR": ("FRA", "フランス", "France", "French"),
    "CN": ("CHN", "中国", "China"), "UA": ("UKR", "ウクライナ", "Ukraine"),
    "YE": ("YEM", "イエメン", "Yemen"), "SY": ("SYR", "シリア", "Syria"),
    "TR": ("TUR", "トルコ", "Turkey"), "UG": ("UGA", "ウガンダ", "Uganda"),
    "BR": ("BRA", "ブラジル", "Brazil"), "AR": ("ARG", "アルゼンチン", "Argentina"),
    "CL": ("CHL", "チリ", "Chile"), "MX": ("MEX", "メキシコ", "Mexico"),
    "CO": ("COL", "コロンビア", "Colombia"), "PE": ("PER", "ペルー", "Peru"),
    "AF": ("AFG", "アフガニスタン", "Afghanistan"), "BD": ("BGD", "バングラデシュ", "Bangladesh"),
    "BT": ("BTN", "ブータン", "Bhutan"), "IN": ("IND", "インド", "India"),
    "MV": ("MDV", "モルディブ", "Maldives"), "NP": ("NPL", "ネパール", "Nepal"),
    "PK": ("PAK", "パキスタン", "Pakistan"), "LK": ("LKA", "スリランカ", "Sri Lanka"),
    "ID": ("IDN", "インドネシア", "Indonesia"), "PH": ("PHL", "フィリピン", "Philippines"),
    "VN": ("VNM", "ベトナム", "Vietnam"), "TH": ("THA", "タイ", "Thailand"),
    "MY": ("MYS", "マレーシア", "Malaysia"), "SG": ("SGP", "シンガポール", "Singapore"),
    "KH": ("KHM", "カンボジア", "Cambodia"), "LA": ("LAO", "ラオス", "Laos"),
    "MM": ("MMR", "ミャンマー", "Myanmar"), "BN": ("BRN", "ブルネイ", "Brunei"),
    "TL": ("TLS", "東ティモール", "Timor-Leste"), "AL": ("ALB", "アルバニア", "Albania"),
    "AT": ("AUT", "オーストリア", "Austria"), "BE": ("BEL", "ベルギー", "Belgium"),
    "NL": ("NLD", "オランダ", "Netherlands"), "ES": ("ESP", "スペイン", "Spain"),
    "IT": ("ITA", "イタリア", "Italy"), "SE": ("SWE", "スウェーデン", "Sweden"),
    "KE": ("KEN", "ケニア", "Kenya"), "TZ": ("TZA", "タンザニア", "Tanzania"),
    "ML": ("MLI", "マリ", "Mali"), "NE": ("NER", "ニジェール", "Niger"),
    "TD": ("TCD", "チャド", "Chad"), "BF": ("BFA", "ブルキナファソ", "Burkina Faso"),
    "SD": ("SDN", "スーダン", "Sudan"), "SN": ("SEN", "セネガル", "Senegal"),
    "MR": ("MRT", "モーリタニア", "Mauritania"), "NG": ("NGA", "ナイジェリア", "Nigeria"),
}
_GROUPS = (
    (r"東南アジア|Southeast Asia|ASEAN", "BN KH ID LA MY MM PH SG TH TL VN"),
    (r"(?<!東)南アジア|South Asia", "AF BD BT IN MV NP PK LK"),
    (r"東アジア|East Asia", "CN HK JP KR MN TW"),
    (r"サヘル|Sahel", "BF TD ML MR NE SN NG SD"),
    (r"(?<![A-Za-z])EU(?![A-Za-z])|欧州連合|EU各国|European Union", "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE"),
    (r"欧州|ヨーロッパ|Europe", "AL AD AT BY BE BA BG HR CY CZ DK EE FI FR DE GR HU IS IE IT XK LV LI LT LU MT MD MC ME NL MK NO PL PT RO RU SM RS SK SI ES SE CH UA GB VA"),
    (r"ラテンアメリカ|中南米|Latin America", "AR BO BR CL CO CR CU DO EC SV GT HT HN MX NI PA PY PE UY VE BZ GY SR"),
    (r"アフリカ|Africa", "DZ AO BJ BW BF BI CV CM CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM GH GN GW KE LS LR LY MG MW ML MR MU MA MZ NA NE NG RW ST SN SC SL SO ZA SS SD TZ TG TN UG ZM ZW"),
)


def explicit_countries(text: str, *, codes: bool = True) -> list[str]:
    text = re.sub(r"ラテンアメリカ|中南米|南アメリカ|中央アメリカ|Latin America|US\$|US dollars?|U\.S\.\s+dollars?", " ", str(text), flags=re.I)
    matches = []
    for code, names in _COUNTRIES.items():
        for name in names:
            # Country codes are only accepted as standalone tokens. In
            # particular ID in 'indicator ID' must not become Indonesia.
            if name == names[0]:
                continue
            pattern = (rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])" if name.isascii() else
                       re.escape(name) + (r"(?![ァ-ヶ])" if len(name) <= 2 else ""))
            for match in re.finditer(pattern, text, re.I):
                matches.append((match.start(), match.end(), code))
    for match in re.finditer(r"(?<![A-Za-z])アメリカ(?![A-Za-z])", text):
        matches.append((match.start(), match.end(), "US"))
    if codes:
        for match in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2,3})(?![A-Za-z0-9$])", text):
            value = match.group(1)
            if value in _COUNTRIES and value not in {"ID", "IN", "IT"}:
                matches.append((match.start(), match.end(), value))
            else:
                found = next((code for code, names in _COUNTRIES.items() if names[0] == value), None)
                if found:
                    matches.append((match.start(), match.end(), found))
    return list(dict.fromkeys(code for start, end, code in sorted(matches)
                             if not any(a <= start and end <= b and b-a > end-start for a, b, _ in matches)))


def iso3_country(code: str) -> str | None:
    if code in _COUNTRIES:
        return _COUNTRIES[code][0]
    other = dict(item.split(":") for item in (
        "HK:HKG MN:MNG TW:TWN BG:BGR HR:HRV CY:CYP CZ:CZE DK:DNK EE:EST FI:FIN GR:GRC HU:HUN IE:IRL LV:LVA LT:LTU LU:LUX MT:MLT PL:POL PT:PRT RO:ROU SK:SVK SI:SVN "
        "BO:BOL CR:CRI CU:CUB DO:DOM EC:ECU SV:SLV GT:GTM HT:HTI HN:HND NI:NIC PA:PAN PY:PRY UY:URY VE:VEN BZ:BLZ GY:GUY SR:SUR "
        "DZ:DZA AO:AGO BJ:BEN BW:BWA BI:BDI CV:CPV CM:CMR CF:CAF KM:COM CG:COG CD:COD CI:CIV DJ:DJI EG:EGY GQ:GNQ ER:ERI SZ:SWZ ET:ETH GA:GAB GM:GMB GH:GHA GN:GIN GW:GNB LS:LSO LR:LBR LY:LBY MG:MDG MW:MWI MU:MUS MA:MAR MZ:MOZ NA:NAM RW:RWA ST:STP SC:SYC SL:SLE SO:SOM ZA:ZAF SS:SSD TG:TGO TN:TUN ZM:ZMB ZW:ZWE"
    ).split())
    return other.get(code)


def country_scope(text: str) -> set[str]:
    if re.search(r"日米欧|日米|米欧", text):
        countries = set(explicit_countries(text))
        if "日米" in text:
            countries.update({"JP", "US"})
        if "米欧" in text:
            countries.update({"US"})
            countries.update(next(members.split() for pattern, members in _GROUPS if re.search(pattern, "EU")))
        return countries
    for pattern, members in _GROUPS:
        if re.search(pattern, text, re.I):
            return set(members.split()) | set(explicit_countries(text))
    return set(explicit_countries(text))


def named_comparison_countries(text: str) -> list[str]:
    """Named comparison axes precede samples from a larger regional group."""
    result = explicit_countries(text)
    if re.search(r"日米|日欧", text):
        result.insert(0, "JP")
    if re.search(r"日米|米欧", text):
        result.append("US")
    return list(dict.fromkeys(result))


def areal_location(text: str) -> bool:
    """A country/continent is not a representative weather observation point."""
    value = str(text or "").strip()
    names = {alias.casefold() for code, aliases in _COUNTRIES.items() for alias in (code, *aliases)}
    return value.casefold() in names or any(re.fullmatch(pattern, value, re.I) for pattern, _ in _GROUPS)


_ISO_IDENTITIES = {identity: code for code in set(_COUNTRIES).union(
    *(set(members.split()) for _, members in _GROUPS))
    for identity in (code, iso3_country(code)) if identity}

_OBSERVATION_ALIASES = {alias.casefold(): code for code, names in _COUNTRIES.items() for alias in names[1:]}
_OBSERVATION_ALIASES.update({
    "korea, rep.": "KR", "korea, rep": "KR", "united states of america": "US", "uk": "GB",
    "south sudan": "SS", "russian federation": "RU", "syrian arab rep.": "SY", "syrian arab republic": "SY",
    "lao pdr": "LA", "viet nam": "VN", "egypt, arab rep.": "EG", "yemen, rep.": "YE",
    "congo, dem. rep.": "CD", "dem. rep. of the congo": "CD", "congo, rep.": "CG",
    "bolivia (plurinational state of)": "BO", "venezuela (bolivarian republic of)": "VE",
})


def observation_country(value) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(observation_country(value.get(key)) for key in ("id", "value", "name")))
    if isinstance(value, list):
        return set().union(*(observation_country(item) for item in value)) if value else set()
    raw = str(value or "").strip()
    if raw.upper() in _ISO_IDENTITIES:
        return {_ISO_IDENTITIES[raw.upper()]}
    if raw.casefold() in _OBSERVATION_ALIASES:
        return {_OBSERVATION_ALIASES[raw.casefold()]}
    for pattern, members in _GROUPS:
        if re.fullmatch(pattern, raw, re.I):
            return set(members.split())
    # Observation labels are identities, not prose containing country names.
    # "South Sudan", "Virgin Islands (U.S.)" and "St. Martin (French part)"
    # must never become Sudan, United States or France by substring match.
    if re.fullmatch(r"[A-Z]{2,3}", raw):
        return {f"ISO{len(raw)}:{raw}"}
    return set()


def observation_identity(row: dict) -> str:
    """Use authoritative geographic codes for joins, retaining unknown codes."""
    for key in ("countryiso3code", "country_code"):
        value = str(row.get(key) or "").strip().upper()
        if re.fullmatch(r"[A-Z]{3}", value):
            return "ISO3:" + value
    area = row.get("geo_area") or row.get("country")
    code = area.get("id") if isinstance(area, dict) else area
    raw_code = str(code or "").strip()
    if re.fullmatch(r"[A-Z]{3}", raw_code):
        return "ISO3:" + raw_code
    identities = observation_country(area)
    if len(identities) == 1:
        identity = next(iter(identities))
        iso3 = iso3_country(identity)
        return "ISO3:" + iso3 if iso3 else identity
    return "LABEL:" + str(area.get("value") or area.get("id") if isinstance(area, dict) else area or "対象地域")


def requested_years(text: str) -> set[int]:
    years = sorted({int(x) for x in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)})
    current = datetime.now(timezone.utc).year
    if len(years) > 1 and re.search(r"から|[〜～–-]|to|through", text, re.I):
        return set(range(years[0], years[-1] + 1))
    if years:
        if len(years) == 1 and re.search(r"以降|から|以後|since|onward", text, re.I):
            return set(range(years[0], max(current, years[0]) + 1))
        return set(years)
    if re.search(r"昨年|去年|last year", text, re.I):
        return {current - 1}
    if re.search(r"今年|本年|this year", text, re.I):
        return {current}
    duration = re.search(r"(?:過去|直近|この)\s*(\d{1,2})\s*年|past\s+(\d{1,2})\s+years?", text, re.I)
    return set(range(current - int(next(x for x in duration.groups() if x)), current + 1)) if duration else set()


def row_matches_scope(row: dict, message: str) -> bool:
    wanted = country_scope_for_indicator(message, indicator_id(row.get("indicator")))
    observed = set()
    for key in ("country", "country_name", "countryiso3code", "country_code", "geo_area", "geo", "ref_area", "region", "jurisdiction", "countries"):
        if row.get(key) is not None:
            observed.update(observation_country(row[key]))
    if wanted and observed and wanted.isdisjoint(observed):
        return False
    years = requested_years(message)
    observed_years = set()
    for key in ("year", "date", "date_start", "date_end", "event_date", "time_period", "period", "time", "TIME_PERIOD", "publication_year", "publication_date"):
        observed_years.update(int(x) for x in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(row.get(key) or "")))
    if row.get("date_start") and row.get("date_end") and observed_years:
        observed_years.update(range(min(observed_years), max(observed_years) + 1))
    return not (years and observed_years and years.isdisjoint(observed_years))


def filter_scope(data: dict, message: str) -> tuple[dict, int]:
    """Filter observations, including comparison fanouts; don't validate query echoes."""
    data = dict(data)
    rejected = 0
    for field in ("items", "rows", "works"):
        rows = data.get(field)
        if not isinstance(rows, list):
            continue
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            if isinstance(row.get("data"), dict):
                child, count = filter_scope(row["data"], message)
                rejected += count
                if child.get("semantic_status") == "EMPTY":
                    continue
                row = {**row, "data": child}
            elif not row_matches_scope(row, message):
                rejected += 1
                continue
            kept.append(row)
        data[field] = kept
        if rows and not kept:
            data.update(has_payload=False, semantic_status="EMPTY")
    if rejected:
        # Out-of-scope records are not missing requested observations. Keep
        # the provider's completeness declaration; coverage checks validate
        # the remaining countries, measures and periods independently.
        data.update(scope_rejected_count=rejected)
    return data, rejected


def country_name(code: str) -> str:
    """Provider-neutral English label for a recognized ISO identity."""
    names = _COUNTRIES.get(str(code).upper(), ())
    return next((name for name in names[1:] if re.search(r"[A-Za-z]", name)), str(code).upper())
