"""Validated, persistence-ready scraping Recipe definitions."""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


_SENSITIVE_QUERY_KEY = re.compile(r"(?:token|secret|password|passwd|auth|session|cookie|credential|api[_-]?key)", re.I)


def _selector(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 512 or any(char in normalized for char in "{};\r\n\x00"):
        raise ValueError("CSS selector must be a bounded selector string")
    return normalized


def build_record_selector_recovery_candidates(
    evidence: dict, original_selector: str, *, limit: int = 12
) -> list[dict[str, str]]:
    """Build bounded row selectors from the existing visible DOM picker evidence."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("candidates"), list):
        return []
    maximum = max(1, min(int(limit), 12))
    seen = {original_selector}
    results: list[dict[str, str]] = []
    for element in evidence["candidates"][:200]:
        if not isinstance(element, dict) or element.get("kind") != "row":
            continue
        selectors = element.get("selector_candidates")
        selectors = selectors if isinstance(selectors, list) else []
        for raw_selector in [*selectors, element.get("dom_path")]:
            if not isinstance(raw_selector, str):
                continue
            try:
                selector = _selector(raw_selector)
            except ValueError:
                continue
            if selector in seen:
                continue
            seen.add(selector)
            results.append({"candidate_id": f"r{len(results)}", "selector": selector})
            if len(results) >= maximum:
                return results
    return results


def extract_json_recipe_rows(payload: object, source: JsonRecipeSource, *, limit: int = 100) -> dict:
    """Project one bounded JSON response into rows using user-authored JSON Pointers."""
    def tokens(pointer: str) -> list[str]:
        if pointer == "":
            return []
        if not pointer.startswith("/"):
            raise ValueError("NETWORK_JSON_POINTER_INVALID")
        return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]

    def resolve(value: object, pointer: str) -> tuple[bool, object]:
        current = value
        for token in tokens(pointer):
            if isinstance(current, dict) and token in current:
                current = current[token]
            elif isinstance(current, list) and token.isdigit() and (token == "0" or not token.startswith("0")):
                index = int(token)
                if index >= len(current):
                    return False, None
                current = current[index]
            else:
                return False, None
        return True, current

    found, records = resolve(payload, source.records_pointer)
    if not found or not isinstance(records, list):
        raise ValueError("NETWORK_JSON_RECORDS_POINTER_NOT_ARRAY")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("NETWORK_JSON_RECORD_SHAPE_INVALID")
    maximum = max(1, min(int(limit), 100))
    selected = records[:maximum]
    field_match_counts = {name: 0 for name in source.fields}
    rows = []
    for record in selected:
        row = {}
        for name, definition in source.fields.items():
            matched, value = resolve(record, definition.pointer)
            row[name] = value if matched else None
            if matched:
                field_match_counts[name] += 1
        rows.append(row)
    return {"rows": rows, "record_count": len(records),
            "field_match_counts": field_match_counts, "truncated": len(records) > maximum}


class RecipeField(BaseModel):
    selector: str = Field(min_length=1, max_length=512)
    extract: Literal["text", "inner_html", "href", "src", "attribute", "number", "date", "boolean", "list"] = "text"
    attribute: str | None = Field(default=None, max_length=128)

    @field_validator("selector")
    @classmethod
    def validate_selector(cls, value: str) -> str:
        return _selector(value)

    @field_validator("attribute")
    @classmethod
    def validate_attribute(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(char in value for char in "\r\n\x00")):
            raise ValueError("invalid attribute name")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def attribute_required_for_attribute_extract(self):
        if self.extract == "attribute" and not self.attribute:
            raise ValueError("attribute name is required for attribute extraction")
        return self


class RecipeNavigation(BaseModel):
    type: Literal["none", "url_parameter", "next", "load_more", "infinite_scroll", "numbered"] = "none"
    selector: str | None = Field(default=None, max_length=512)
    page_parameter: str | None = Field(default=None, max_length=128)

    @field_validator("selector")
    @classmethod
    def validate_selector(cls, value: str | None) -> str | None:
        return _selector(value) if value is not None else None

    @field_validator("page_parameter")
    @classmethod
    def validate_page_parameter(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(char in value for char in "&=?#\r\n\x00")):
            raise ValueError("invalid page parameter")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_navigation_configuration(self):
        if self.type in {"next", "load_more", "numbered"} and not self.selector:
            raise ValueError("navigation selector is required")
        if self.type == "url_parameter" and not self.page_parameter:
            raise ValueError("page parameter is required")
        return self


class RecipeDetailPage(BaseModel):
    link_field: str = Field(min_length=1, max_length=120)
    record_selector: str = Field(default="body", min_length=1, max_length=512)
    fields: dict[str, RecipeField] = Field(min_length=1, max_length=50)

    @field_validator("record_selector")
    @classmethod
    def validate_record_selector(cls, value: str) -> str:
        return _selector(value)

    @field_validator("fields")
    @classmethod
    def validate_field_names(cls, fields: dict[str, RecipeField]) -> dict[str, RecipeField]:
        for name in fields:
            if len(name) > 120 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9 _-]*", name):
                raise ValueError("detail field names must be plain labels beginning with a letter or underscore")
        return fields


class JsonRecipeField(BaseModel):
    pointer: str = Field(max_length=512)

    @field_validator("pointer")
    @classmethod
    def validate_pointer(cls, value: str) -> str:
        if len(value) > 512 or (value and not value.startswith("/")):
            raise ValueError("JSON Pointer must be empty or begin with slash")
        for token in value.split("/")[1:]:
            if re.search(r"~(?![01])", token):
                raise ValueError("JSON Pointer contains an invalid escape")
        return value


class JsonRecipeSource(BaseModel):
    endpoint_path: str = Field(min_length=1, max_length=512)
    records_pointer: str = Field(max_length=512)
    fields: dict[str, JsonRecipeField] = Field(min_length=1, max_length=50)

    @field_validator("endpoint_path")
    @classmethod
    def validate_endpoint_path(cls, value: str) -> str:
        if (not value.startswith("/") or value.startswith("//") or "?" in value
                or "#" in value or any(char in value for char in "\\\r\n\x00")):
            raise ValueError("JSON endpoint must be a bounded same-origin path without query or fragment")
        return value

    @field_validator("records_pointer")
    @classmethod
    def validate_records_pointer(cls, value: str) -> str:
        return JsonRecipeField.validate_pointer(value)

    @field_validator("fields")
    @classmethod
    def validate_field_names(cls, fields: dict[str, JsonRecipeField]) -> dict[str, JsonRecipeField]:
        for name in fields:
            if len(name) > 120 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9 _-]*", name):
                raise ValueError("JSON field names must be plain labels beginning with a letter or underscore")
        return fields


class ScrapeRecipeInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start_url: str = Field(min_length=8, max_length=2048)
    record_selector: str = Field(min_length=1, max_length=512)
    fields: dict[str, RecipeField] = Field(default_factory=dict, max_length=50)
    navigation: RecipeNavigation = Field(default_factory=RecipeNavigation)
    detail: RecipeDetailPage | None = None
    wait_strategy: Literal["dom_stable", "page_load", "short_delay"] = "dom_stable"
    deduplicate_keys: list[str] = Field(default_factory=list, max_length=10)
    max_pages: int = Field(default=10, ge=1, le=500)
    max_records: int = Field(default=10_000, ge=1, le=100_000)
    max_duration_seconds: int = Field(default=900, ge=1, le=3600)
    output_format: Literal["csv", "json", "jsonl"] = "csv"
    source: Literal["dom", "network_json"] = "dom"
    network_json: JsonRecipeSource | None = None
    browser_profile_id: str | None = None
    tool: Literal["list", "page", "text", "emails", "social", "images", "sitemap", "shopify"] | None = None
    urls: list[str] = Field(default_factory=list, max_length=500)
    page_delay_seconds: float = Field(default=0, ge=0, le=30)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Recipe name cannot be empty")
        return value

    @field_validator("browser_profile_id")
    @classmethod
    def validate_browser_profile_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if (len(value) > 64
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", value)):
            raise ValueError("browser profile ID must be a lowercase local profile key")
        return value

    @field_validator("start_url")
    @classmethod
    def validate_start_url(cls, value: str) -> str:
        url = value.strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("Recipe start URL must be credential-free HTTP(S)")
        if any(_SENSITIVE_QUERY_KEY.search(key) for key, _value in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError("sensitive query parameter names cannot be stored in a Recipe")
        return url

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(cls.validate_start_url(value) for value in values))

    @field_validator("record_selector")
    @classmethod
    def validate_record_selector(cls, value: str) -> str:
        return _selector(value)

    @field_validator("fields")
    @classmethod
    def validate_field_names(cls, fields: dict[str, RecipeField]) -> dict[str, RecipeField]:
        for name in fields:
            if len(name) > 120 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9 _-]*", name):
                raise ValueError("Recipe field names must be plain labels beginning with a letter or underscore")
        return fields

    @field_validator("deduplicate_keys")
    @classmethod
    def validate_dedupe_keys(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 120 for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("deduplication keys must be unique field names")
        return cleaned

    @model_validator(mode="after")
    def dedupe_fields_must_exist(self):
        if self.tool:
            if self.source != "dom" or self.detail:
                raise ValueError("automatic tools cannot use manual or network field mappings")
            if self.fields:
                supported_chat_list_fields = {"facility_name", "address", "category"}
                if (self.tool != "list"
                        or not set(self.fields).issubset(supported_chat_list_fields)
                        or any(field.selector != "*" or field.extract != "text"
                               for field in self.fields.values())):
                    raise ValueError("automatic tools cannot use manual or network field mappings")
        if self.urls and self.navigation.type != "none":
            raise ValueError("multi URL recipes cannot combine pagination")
        source_fields = self.network_json.fields if self.source == "network_json" and self.network_json else self.fields
        if any(name not in source_fields for name in self.deduplicate_keys):
            raise ValueError("deduplication keys must refer to configured fields")
        if self.source == "network_json":
            if self.network_json is None:
                raise ValueError("network JSON source configuration is required")
            if self.fields or self.detail is not None:
                raise ValueError("network JSON Recipes use JSON field mappings and cannot define DOM/detail mappings")
            parsed = urlsplit(self.start_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("network JSON Recipe start URL must be HTTP(S)")
            return self
        if self.network_json is not None:
            raise ValueError("network JSON configuration requires source=network_json")
        if self.detail is not None:
            link = self.fields.get(self.detail.link_field)
            if link is None or link.extract != "href":
                raise ValueError("detail link_field must name a configured href field")
            if set(self.detail.fields) & set(self.fields):
                raise ValueError("detail field names must not collide with list fields")
        return self

    def to_record(self, recipe_id: str) -> dict:
        return {
            "id": recipe_id,
            "version": 1,
            "name": self.name,
            "start": {"url": self.start_url},
            "record_selector": self.record_selector,
            "fields": {name: field.model_dump() for name, field in self.fields.items()},
            "navigation": self.navigation.model_dump(),
            "detail": self.detail.model_dump() if self.detail is not None else None,
            "wait_strategy": self.wait_strategy,
            "deduplicate_keys": self.deduplicate_keys,
            "max_pages": self.max_pages,
            "max_records": self.max_records,
            "max_duration_seconds": self.max_duration_seconds,
            "output_format": self.output_format,
            "source": self.source,
            "network_json": self.network_json.model_dump() if self.network_json is not None else None,
            "browser_profile_id": self.browser_profile_id,
            "tool": self.tool, "urls": self.urls, "page_delay_seconds": self.page_delay_seconds,
            # Draft Recipes cannot execute until a later validation/activation flow.
            "status": "DRAFT",
        }

    @classmethod
    def from_record(cls, record: dict) -> "ScrapeRecipeInput":
        start = record.get("start") or {}
        return cls.model_validate({
            "name": record["name"], "start_url": start["url"],
            "record_selector": record.get("record_selector") or "body",
            "fields": record.get("fields") or {},
            "navigation": record.get("navigation") or {"type": "none"},
            "detail": record.get("detail"),
            "wait_strategy": record.get("wait_strategy") or "dom_stable",
            "deduplicate_keys": record.get("deduplicate_keys") or [],
            "max_pages": record.get("max_pages") or 1,
            "max_records": record.get("max_records") or 10_000,
            "max_duration_seconds": record.get("max_duration_seconds") or 900,
            "output_format": record.get("output_format") or "csv",
            "source": record.get("source") or "dom",
            "network_json": record.get("network_json"),
            "browser_profile_id": record.get("browser_profile_id"),
            "tool": record.get("tool"), "urls": record.get("urls") or [],
            "page_delay_seconds": record.get("page_delay_seconds") or 0,
        })


def enrich_detail_rows(recipe: ScrapeRecipeInput, rows: list[dict], browser_sessions, session_id: str,
                       list_url: str, *, wait_after_navigation=None,
                       on_page_visited=None, on_extraction=None) -> dict:
    """Read one same-origin detail page per selected row and return to the list."""
    if recipe.detail is None:
        return {"status": "AVAILABLE", "rows": rows}
    detail_fields = {name: field.model_dump() for name, field in recipe.detail.fields.items()}
    for row in rows:
        detail_url = row.get(recipe.detail.link_field)
        try:
            parsed = urlsplit(str(detail_url))
            valid_http = (parsed.scheme in {"http", "https"} and parsed.hostname is not None
                          and parsed.username is None and parsed.password is None)
            same_origin = _same_origin_url(list_url, str(detail_url))
        except (TypeError, ValueError):
            valid_http, same_origin = False, False
        if not valid_http or not same_origin:
            return {"status": "BLOCKED", "error": "DETAIL_LINK_ORIGIN_CHANGED"}
        page = browser_sessions.navigate(session_id, str(detail_url))
        if page.get("status") != "AVAILABLE":
            return {"status": page.get("status", "FAILED"), "error": str(page.get("error") or "DETAIL_PAGE_NAVIGATION_FAILED")}
        if on_page_visited is not None:
            on_page_visited()
        if wait_after_navigation is not None:
            wait_result = wait_after_navigation()
            if wait_result.get("status") != "AVAILABLE":
                return {"status": wait_result.get("status", "FAILED"),
                        "error": str(wait_result.get("error") or "DETAIL_PAGE_WAIT_FAILED")}
        detail_page_url = page.get("url") or ""
        if not _same_origin_url(list_url, detail_page_url):
            return {"status": "BLOCKED", "error": "DETAIL_PAGE_ORIGIN_CHANGED"}
        if on_extraction is not None:
            on_extraction()
        evidence = browser_sessions.extract_recipe(session_id, recipe.detail.record_selector, detail_fields, limit=1)
        if evidence.get("status") != "AVAILABLE" or len(evidence.get("rows") or []) != 1:
            return {"status": "FAILED", "error": str(evidence.get("error") or "DETAIL_FIELDS_UNAVAILABLE")}
        missing_fields = [name for name in detail_fields
                          if int((evidence.get("field_match_counts") or {}).get(name, 0)) < 1]
        if missing_fields:
            return {"status": "FAILED", "error": "DETAIL_FIELDS_UNAVAILABLE", "fields": missing_fields}
        row.update(evidence["rows"][0])
        restored = browser_sessions.navigate_back(session_id)
        if restored.get("status") != "AVAILABLE" or restored.get("url") != list_url:
            return {"status": "FAILED", "error": "DETAIL_LIST_RESTORE_FAILED"}
    return {"status": "AVAILABLE", "rows": rows}


def _same_origin_url(left: str, right: str) -> bool:
    try:
        first, second = urlsplit(left), urlsplit(right)
        return (first.scheme.lower(), first.hostname.lower() if first.hostname else None, first.port) == (
            second.scheme.lower(), second.hostname.lower() if second.hostname else None, second.port)
    except (TypeError, ValueError):
        return False
