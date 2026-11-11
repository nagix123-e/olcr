"""Loopback HTTP CONNECT proxy that pins browser sockets to public DNS answers.

Playwright configures Chromium to use this proxy. The proxy validates every
HTTP request and every CONNECT tunnel, then connects to the exact validated
sockaddr. It does not inspect HTTPS payloads.
"""
from __future__ import annotations
from .scrape_tools import EXTRACT_SCRIPT

import select
import base64
import socket
import socketserver
import shutil
import tempfile
import threading
import time
import uuid
import json
import math
import ipaddress
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit
import re

from .web import _resolve_public_addresses
from .local_e2e import normalize_local_origin, origin_from_url
from .recipes import JsonRecipeSource, extract_json_recipe_rows
from .model_telemetry import model_telemetry

_PAGE_LOAD_READY_SCRIPT = r'''async () => {
  if (document.readyState === "complete") return {ready: true};
  const loaded = await Promise.race([
    new Promise(resolve => window.addEventListener("load", () => resolve(true), {once: true})),
    new Promise(resolve => setTimeout(() => resolve(false), 2000))
  ]);
  return {ready: loaded === true && document.readyState === "complete"};
}'''

_LOCAL_E2E_CLICK_CHECK = r'''() => {
  const cfg = __OLCR_CONFIG__;
  let matches;
  try { matches = [...document.querySelectorAll(cfg.selector)]; }
  catch (_) { return JSON.stringify({ok:false, reason:"INVALID_SELECTOR"}); }
  const visible = (el) => { const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return r.width>0 && r.height>0 && s.visibility!=="hidden" && s.display!=="none"; };
  matches = matches.filter(visible);
  if (matches.length !== 1) return JSON.stringify({ok:false, reason:"TARGET_NOT_UNIQUE"});
  const el=matches[0], label=(el.innerText || el.getAttribute("aria-label") || "").replace(/\s+/g," ").trim();
  if (label !== cfg.label) return JSON.stringify({ok:false, reason:"TARGET_CHANGED"});
  if (el.disabled || el.getAttribute("aria-disabled") === "true") return JSON.stringify({ok:false, reason:"TARGET_DISABLED"});
  if (location.origin !== cfg.origin) return JSON.stringify({ok:false, reason:"PAGE_LEFT_ORIGIN"});
  const href=el.tagName.toLowerCase()==="a" ? el.href : "";
  if (href && new URL(href,location.href).origin !== cfg.origin) return JSON.stringify({ok:false, reason:"TARGET_LEAVES_ORIGIN"});
  return JSON.stringify({ok:true});
}'''

_LOCAL_E2E_TYPE_CHECK = r'''() => {
  const cfg = __OLCR_CONFIG__;
  let matches;
  try { matches = [...document.querySelectorAll(cfg.selector)]; }
  catch (_) { return JSON.stringify({ok:false, reason:"INVALID_SELECTOR"}); }
  const visible = (el) => { const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return r.width>0 && r.height>0 && s.visibility!=="hidden" && s.display!=="none"; };
  matches = matches.filter(visible);
  if (matches.length !== 1) return JSON.stringify({ok:false, reason:"TARGET_NOT_UNIQUE"});
  const el=matches[0];
  const labelText = (el) => {
    const labelled = el.getAttribute("aria-labelledby");
    const referenced = labelled ? labelled.split(/\s+/).map(id => document.getElementById(id)?.innerText || "").join(" ") : "";
    const associated = el.labels ? [...el.labels].map(label => label.innerText || label.textContent || "").join(" ") : "";
    return (el.getAttribute("aria-label") || referenced || associated || el.getAttribute("placeholder") || el.name || "")
      .replace(/\s+/g," ").trim();
  };
  if (!/^(input|textarea)$/i.test(el.tagName)) return JSON.stringify({ok:false, reason:"TARGET_NOT_TEXT_FIELD"});
  if (el.tagName.toLowerCase()==="input" && ["hidden","password","file","checkbox","radio","button","submit","reset","image"].includes((el.type||"").toLowerCase()))
    return JSON.stringify({ok:false, reason:"TARGET_INPUT_TYPE_UNSUPPORTED"});
  if (labelText(el) !== cfg.label) return JSON.stringify({ok:false, reason:"TARGET_CHANGED"});
  if (el.disabled || el.readOnly || el.getAttribute("aria-disabled")==="true" || el.getAttribute("aria-readonly")==="true")
    return JSON.stringify({ok:false, reason:"TARGET_NOT_EDITABLE"});
  if (location.origin !== cfg.origin) return JSON.stringify({ok:false, reason:"PAGE_LEFT_ORIGIN"});
  if (cfg.expected !== null && el.value !== cfg.expected) return JSON.stringify({ok:false, reason:"VALUE_NOT_CONFIRMED"});
  return JSON.stringify({ok:true});
}'''

_PICKER_EVIDENCE_SCRIPT = r'''() => {
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const accessibleLabel = (el) => {
    const labelled = el.getAttribute("aria-labelledby");
    const referenced = labelled ? labelled.split(/\s+/).map(id => document.getElementById(id)?.innerText || "").join(" ") : "";
    const associated = el.labels ? [...el.labels].map(label => label.innerText || label.textContent || "").join(" ") : "";
    return el.getAttribute("aria-label") || referenced || associated || el.getAttribute("placeholder") || "";
  };
  const shortText = (el) => (el.innerText || accessibleLabel(el) || "")
    .replace(/\s+/g, " ").trim().slice(0, 240);
  const domPath = (el) => {
    const parts = [];
    for (let node = el, depth = 0; node && node.nodeType === 1 && depth < 7 && node !== document.body; node = node.parentElement, depth++) {
      let part = node.tagName.toLowerCase();
      const same = node.parentElement ? [...node.parentElement.children].filter(sibling => sibling.tagName === node.tagName) : [];
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      parts.unshift(part);
    }
    return parts.join(" > ").slice(0, 300);
  };
  const esc = (value) => CSS.escape(value);
  const selectors = ["tr", "[role=row]", "td", "th", "[role=cell]", "input:not([type=hidden])", "textarea", "select", "button", "a", "nav", "[role=navigation]"];
  const seen = new Set();
  const candidates = [];
  for (const el of document.querySelectorAll(selectors.join(","))) {
    if (!visible(el) || seen.has(el)) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role") || ({a:"link",button:"button",input:"textbox",textarea:"textbox",select:"combobox",tr:"row",nav:"navigation"}[tag] || "");
    const kind = tag === "tr" || role === "row" ? "row" : /^(input|textarea|select|td|th)$/.test(tag) || role === "cell" ? "field" : tag === "a" || tag === "nav" || role === "navigation" ? "navigation" : "control";
    const label = shortText(el);
    const box = el.getBoundingClientRect();
    const attrs = {};
    for (const name of ["aria-label", "name", "placeholder", "type", "data-testid"]) {
      const value = el.getAttribute(name);
      if (value) attrs[name] = value.slice(0, 120);
    }
    if (tag === "a" && el.href) {
      try {
        const link = new URL(el.href);
        if (["http:", "https:"].includes(link.protocol)) {
          attrs.href = link.origin + link.pathname;
          // Keep query values private while exposing action-like parameter names
          // to the server-side read-only navigation policy.
          attrs.href_query_keys = [...link.searchParams.keys()].slice(0,12).join(" ");
        }
      } catch (_) {}
    }
    const selectorCandidates = [];
    if (el.id && el.id.length <= 120) selectorCandidates.push("#" + esc(el.id));
    if (attrs["data-testid"]) selectorCandidates.push(`[data-testid="${esc(attrs["data-testid"])}"]`);
    if (attrs.name && /^(input|textarea|select)$/.test(tag)) selectorCandidates.push(`${tag}[name="${esc(attrs.name)}"]`);
    const aria = attrs["aria-label"];
    if (aria) selectorCandidates.push(`${tag}[aria-label="${esc(aria)}"]`);
    const href = attrs.href;
    if (tag === "a" && href) selectorCandidates.push(`a[href^="${esc(href)}"]`);
    const path = domPath(el);
    if (path) selectorCandidates.push(path);
    const recordRow = el.closest("tr,[role=row]");
    const relativeParts = [];
    for (let node = el, depth = 0; recordRow && node && node !== recordRow && depth < 6; node = node.parentElement, depth++) {
      let part = node.tagName.toLowerCase();
      const same = node.parentElement ? [...node.parentElement.children].filter(sibling => sibling.tagName === node.tagName) : [];
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      relativeParts.unshift(part);
    }
    const relativeSelector = recordRow && relativeParts.length ? relativeParts.join(" > ").slice(0, 300) : "";
    candidates.push({kind, tag, role, text:label, attributes:attrs,
      bbox:{x:Math.round(box.x),y:Math.round(box.y),width:Math.round(box.width),height:Math.round(box.height)},
      dom_path:path, selector_candidates:[...new Set(selectorCandidates)].slice(0,5),
      relative_selector:relativeSelector});
    if (candidates.length >= 200) break;
  }
  return JSON.stringify({candidates, truncated: document.querySelectorAll(selectors.join(",")).length > candidates.length});
}'''

_RECIPE_EXTRACT_SCRIPT = r'''() => {
  const cfg = __OLCR_CONFIG__;
  const bounded = (value) => String(value ?? "").replace(/\s+/g, " ").trim().slice(0, 4000);
  const safeUrl = (value) => {
    try { const url = new URL(value, location.href); return ["http:", "https:"].includes(url.protocol) ? url.origin + url.pathname : ""; }
    catch (_) { return ""; }
  };
  const extract = (element, definition) => {
    if (!element) return null;
    const value = definition.extract === "inner_html" ? element.innerHTML
      : definition.extract === "href" ? safeUrl(element.href || element.getAttribute("href"))
      : definition.extract === "src" ? safeUrl(element.src || element.getAttribute("src"))
      : definition.extract === "attribute" ? element.getAttribute(definition.attribute)
      : element.innerText || element.textContent || "";
    if (definition.extract === "number") {
      const number = Number.parseFloat(String(value).replace(/[^0-9+-.]/g, ""));
      return Number.isFinite(number) ? number : null;
    }
    if (definition.extract === "date") {
      const timestamp = Date.parse(String(value));
      return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : bounded(value);
    }
    if (definition.extract === "boolean") return /^(true|yes|1|available|in stock)$/i.test(bounded(value));
    return bounded(value);
  };
  try {
    const records = [...document.querySelectorAll(cfg.record_selector)];
    const selected = records.slice(0, cfg.limit);
    const field_match_counts = Object.fromEntries(Object.keys(cfg.fields).map(name => [name, 0]));
    const rows = selected.map(record => {
      const fields = {};
      for (const [name, definition] of Object.entries(cfg.fields)) {
        const elements = [...record.querySelectorAll(definition.selector)];
        const matchesRecord = record.matches(definition.selector);
        if (elements.length || matchesRecord) field_match_counts[name] += 1;
        fields[name] = definition.extract === "list"
          ? elements.slice(0, 100).map(element => extract(element, {extract:"text"}))
          : extract(elements[0] || (matchesRecord ? record : null), definition);
      }
      return fields;
    });
    return JSON.stringify({rows, record_count:records.length, field_match_counts, truncated:records.length > cfg.limit});
  } catch (error) {
    return JSON.stringify({error:String(error?.name || "SELECTOR_ERROR").slice(0,80)});
  }
}'''

_RECIPE_SELECTOR_RECOVERY_SCRIPT = r'''() => {
  const cfg = __OLCR_CONFIG__;
  const results = [];
  for (const candidate of cfg.candidates) {
    try {
      const records = [...document.querySelectorAll(candidate.selector)];
      const selected = records.slice(0, 5);
      const field_match_counts = Object.fromEntries(Object.keys(cfg.fields).map(name => [name, 0]));
      for (const record of selected) {
        for (const [name, definition] of Object.entries(cfg.fields)) {
          if (record.matches(definition.selector) || record.querySelector(definition.selector)) {
            field_match_counts[name] += 1;
          }
        }
      }
      results.push({candidate_id:candidate.id, selector:candidate.selector,
        record_count:records.length, field_match_counts});
    } catch (error) {
      results.push({candidate_id:candidate.id, selector:candidate.selector,
        record_count:0, field_match_counts:{}, error:String(error?.name || "SELECTOR_ERROR").slice(0,80)});
    }
  }
  return JSON.stringify({results});
}'''

_RECIPE_NEXT_LINK_SCRIPT = r'''() => {
  const cfg = __OLCR_CONFIG__;
  try {
    const element = document.querySelector(cfg.selector);
    if (!element || element.tagName.toLowerCase() !== "a") return JSON.stringify({url:null});
    const url = new URL(element.href || element.getAttribute("href"), location.href);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return JSON.stringify({url:null});
    url.hash = "";
    return JSON.stringify({url:url.href});
  } catch (error) {
    return JSON.stringify({error:String(error?.name || "SELECTOR_ERROR").slice(0,80)});
  }
}'''

_RECIPE_LOAD_MORE_SCRIPT = r'''() => {
  const cfg = __OLCR_CONFIG__;
  const matches = [...document.querySelectorAll(cfg.selector)].filter((el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  });
  if (matches.length !== 1) return JSON.stringify({ok:false, reason:"SELECTOR_NOT_UNIQUE_OR_VISIBLE"});
  const el = matches[0];
  const role = el.getAttribute("role");
  const tag = el.tagName.toLowerCase();
  const label = (el.innerText || el.getAttribute("aria-label") || "").replace(/\s+/g," ").trim();
  if (cfg.label && label !== cfg.label) return JSON.stringify({ok:false, reason:"LOAD_MORE_TARGET_CHANGED"});
  if (!(tag === "button" || role === "button") || el.disabled || el.form ||
      el.type === "submit" || el.getAttribute("aria-disabled") === "true") {
    return JSON.stringify({ok:false, reason:"LOAD_MORE_TARGET_NOT_ENABLED_BUTTON"});
  }
  return JSON.stringify({ok:true});
}'''

_NETWORK_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|passwd|auth|session|cookie|credential|api[_-]?key)", re.I)
_SCRAPE_MUTATING_LINK = re.compile(
    r"(?:\b(?:delete|remove|checkout|purchase|buy|submit|send|save|update|create|vote|post|logout|pay)\b|"
    r"add[ _-]?to[ _-]?cart|/(?:delete|remove|checkout|purchase|payment|submit|logout|signout|cart/add)(?:/|$)|"
    r"削除|購入|決済|注文|カート|送信|投稿|保存|更新|作成|投票|ログアウト|サインアウト)",
    re.IGNORECASE,
)
_READ_ONLY_SCRAPE_BUTTON = re.compile(
    r"^(?:load\s+more|show\s+more|more\s+(?:results|items|products|posts|reviews)|"
    r"next(?:\s+page)?|older(?:\s+posts)?|もっと見る|さらに表示|次へ|次のページ|続きを見る|追加表示)$",
    re.IGNORECASE,
)
_READ_ONLY_SCRAPE_QUERY_KEYS = frozenset({
    "page", "p", "offset", "cursor", "limit", "per_page", "page_size", "start",
    "sort", "order", "direction", "filter", "category", "tag", "q", "query", "search",
    "language", "locale",
})


def _read_only_scrape_query_keys(value: str) -> bool:
    keys = [part.casefold() for part in re.findall(r"[A-Za-z0-9_.-]+", str(value or ""))]
    return len(keys) <= 12 and all(key in _READ_ONLY_SCRAPE_QUERY_KEYS or key.startswith("utm_") for key in keys)


def _same_http_origin(left: str, right: str) -> bool:
    try:
        first, second = urlsplit(left), urlsplit(right)
        if (first.scheme not in {"http", "https"} or second.scheme not in {"http", "https"}
                or first.username or first.password or second.username or second.password
                or not first.hostname or not second.hostname):
            return False
        first_port = first.port or (443 if first.scheme == "https" else 80)
        second_port = second.port or (443 if second.scheme == "https" else 80)
        return first.scheme.lower() == second.scheme.lower() and first.hostname.lower() == second.hostname.lower() and first_port == second_port
    except ValueError:
        return False


def _header_value(text: str, name: str) -> str | None:
    match = re.search(rf"(?:^|\n)\s*(?:[-*]\s*)?{re.escape(name)}\s*:\s*([^\r\n]+)", text, re.I)
    return match.group(1).strip() if match else None


def _network_response_body(text: str) -> str:
    body = text.strip()
    if body.startswith("### Result\n"):
        body = body.split("\n", 1)[1].strip()
    if body.startswith("```json") or body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return body


def _bounded_network_json(value):
    """Redact sensitive keys and cap depth, collection width, and string size."""
    state = {"changed": False, "remaining": 12_000}

    def visit(item, depth=0):
        if depth >= 6:
            state["changed"] = True
            return "[depth limit]"
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 40 or state["remaining"] <= 0:
                    state["changed"] = True
                    break
                safe_key = str(key)[:160]
                if safe_key != key:
                    state["changed"] = True
                if _NETWORK_SENSITIVE_KEY.search(safe_key):
                    result[safe_key] = "[redacted]"
                    state["changed"] = True
                else:
                    result[safe_key] = visit(child, depth + 1)
            return result
        if isinstance(item, list):
            if len(item) > 20:
                state["changed"] = True
            return [visit(child, depth + 1) for child in item[:20]]
        if isinstance(item, str):
            size = len(item.encode("utf-8"))
            allowance = min(512, max(0, state["remaining"]))
            bounded = item[:allowance]
            state["remaining"] -= len(bounded.encode("utf-8"))
            if len(bounded) != len(item) or state["remaining"] <= 0:
                state["changed"] = state["changed"] or len(bounded) != len(item)
            if size > allowance:
                state["changed"] = True
            return bounded
        if item is None or isinstance(item, (bool, int, float)):
            return item
        state["changed"] = True
        return str(item)[:128]

    return visit(value), state["changed"]


def _sanitize_network_json_payload(value):
    """Redact sensitive-key values and reject pathological structures before Recipe projection."""
    state = {"nodes": 8192}

    def visit(item, depth=0):
        state["nodes"] -= 1
        if state["nodes"] < 0 or depth > 16:
            raise ValueError("NETWORK_JSON_COMPLEXITY_LIMIT")
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                safe_key = str(key)
                if len(safe_key) > 160:
                    raise ValueError("NETWORK_JSON_KEY_TOO_LONG")
                result[safe_key] = "[redacted]" if _NETWORK_SENSITIVE_KEY.search(safe_key) else visit(child, depth + 1)
            return result
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, str):
            return item[:4000]
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        raise ValueError("NETWORK_JSON_VALUE_UNSUPPORTED")

    return visit(value)


def _resolve_local_addresses(url: str, allowed_origin: str) -> list[tuple]:
    try:
        parsed = urlsplit(url)
        normalized = normalize_local_origin(
            f"{parsed.scheme}://{parsed.netloc}/" if parsed.hostname else "")
    except (TypeError, ValueError):
        normalized = None
    if (normalized is None or origin_from_url(url) != origin_from_url(allowed_origin)
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("LOCAL_E2E_ORIGIN_REJECTED")
    try:
        rows = socket.getaddrinfo(parsed.hostname, normalized[2], 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("LOCAL_E2E_LOOPBACK_RESOLUTION_FAILED") from exc
    addresses = []
    for family, socktype, proto, _canonname, sockaddr in rows:
        try:
            if not ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]).is_loopback:
                raise ValueError("LOCAL_E2E_NON_LOOPBACK_RESOLUTION")
        except ValueError as exc:
            if str(exc) == "LOCAL_E2E_NON_LOOPBACK_RESOLUTION":
                raise
            raise ValueError("LOCAL_E2E_LOOPBACK_RESOLUTION_FAILED") from exc
        candidate = (family, socktype, proto, sockaddr)
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise ValueError("LOCAL_E2E_LOOPBACK_RESOLUTION_EMPTY")
    return addresses


def _connect_pinned(url: str, timeout: float = 10.0,
                    allowed_local_origin: str | None = None) -> socket.socket:
    if allowed_local_origin:
        addresses = _resolve_local_addresses(url, allowed_local_origin)
    else:
        addresses = [(socket.AF_INET6 if len(address) > 2 else socket.AF_INET,
                      socket.SOCK_STREAM, 0, address)
                     for address in _resolve_public_addresses(url)]
    last_error: OSError | None = None
    for family, socktype, proto, address in addresses:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(address)
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last_error = exc
            if sock:
                sock.close()
    if last_error:
        raise last_error
    raise OSError("NO_VALIDATED_ADDRESS")


class _ThreadingProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ProxyHandler(socketserver.BaseRequestHandler):
    MAX_HEADER_BYTES = 64 * 1024
    MAX_BODY_BYTES = 8 * 1024 * 1024
    allowed_local_origin: str | None = None

    def handle(self):
        self.request.settimeout(15)
        try:
            first = self.request.recv(self.MAX_HEADER_BYTES)
            if not first:
                return
            header_end = first.find(b"\r\n\r\n")
            while header_end < 0 and len(first) < self.MAX_HEADER_BYTES:
                block = self.request.recv(min(8192, self.MAX_HEADER_BYTES - len(first)))
                if not block:
                    return
                first += block
                header_end = first.find(b"\r\n\r\n")
            if header_end < 0:
                self._reply(431, b"Request headers too large")
                return
            raw_headers = first[:header_end]
            buffered = first[header_end + 4:]
            lines = raw_headers.split(b"\r\n")
            try:
                method, target, version = lines[0].decode("ascii").split(" ", 2)
            except (UnicodeDecodeError, ValueError):
                self._reply(400, b"Malformed request")
                return
            if method.upper() == "CONNECT":
                self._connect_tunnel(target)
                return
            self._forward_http(method, target, version, lines[1:], buffered)
        except (OSError, ValueError, UnicodeError):
            try:
                self._reply(502, b"Destination rejected or unavailable")
            except OSError:
                pass

    def _connect_tunnel(self, authority: str):
        parsed = urlsplit("https://" + authority)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            self._reply(400, b"Invalid CONNECT authority")
            return
        port = parsed.port or 443
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        upstream = _connect_pinned(f"https://{host}:{port}/",
                                   allowed_local_origin=self.allowed_local_origin)
        try:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(upstream)
        finally:
            upstream.close()

    def _forward_http(self, method: str, target: str, version: str, headers: list[bytes], buffered: bytes):
        parsed = urlsplit(target)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            self._reply(403, b"Unsupported or credential-bearing URL")
            return
        upstream = _connect_pinned(target, allowed_local_origin=self.allowed_local_origin)
        try:
            content_length = 0
            content_length_seen = False
            filtered = []
            for line in headers:
                if b":" not in line:
                    continue
                name, value = line.split(b":", 1)
                lowered = name.strip().lower()
                if lowered in {b"proxy-authorization", b"proxy-connection", b"connection", b"keep-alive"}:
                    continue
                if lowered == b"transfer-encoding":
                    self._reply(501, b"Transfer-encoded request bodies are not supported")
                    return
                if lowered == b"content-length":
                    if content_length_seen:
                        self._reply(400, b"Duplicate content length")
                        return
                    try:
                        content_length = int(value.strip())
                    except ValueError:
                        self._reply(400, b"Invalid content length")
                        return
                    content_length_seen = True
                filtered.append(line)
            if content_length < 0 or content_length > self.MAX_BODY_BYTES:
                self._reply(413, b"Request body too large")
                return
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            request_head = (f"{method} {path} {version}\r\n".encode("ascii")
                            + b"\r\n".join(filtered)
                            + b"\r\nConnection: close\r\n\r\n")
            upstream.sendall(request_head)
            remaining = content_length
            if buffered and remaining:
                chunk = buffered[:remaining]
                upstream.sendall(chunk)
                remaining -= len(chunk)
            while remaining:
                chunk = self.request.recv(min(65536, remaining))
                if not chunk:
                    return
                upstream.sendall(chunk)
                remaining -= len(chunk)
            self._relay(upstream)
        finally:
            upstream.close()

    def _relay(self, upstream: socket.socket):
        peers = (self.request, upstream)
        while True:
            readable, _, _ = select.select(peers, (), (), 120)
            if not readable:
                return
            for source in readable:
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                destination = upstream if source is self.request else self.request
                destination.sendall(data)

    def _reply(self, status: int, body: bytes):
        phrase = {400: "Bad Request", 403: "Forbidden", 413: "Payload Too Large",
                  431: "Request Header Fields Too Large", 501: "Not Implemented",
                  502: "Bad Gateway"}.get(status, "Error")
        self.request.sendall(
            f"HTTP/1.1 {status} {phrase}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii") + body
        )


class SafeBrowserEgressProxy:
    """An ephemeral loopback proxy owned by one managed browser session."""
    def __init__(self, *, allowed_local_origin: str | None = None):
        normalized_origin = origin_from_url(allowed_local_origin or "") if allowed_local_origin else None
        if allowed_local_origin and normalized_origin is None:
            raise ValueError("LOCAL_E2E_ORIGIN_MUST_BE_LOOPBACK_HTTP")
        handler = type("_SessionProxyHandler", (_ProxyHandler,),
                       {"allowed_local_origin": normalized_origin})
        self._server = _ThreadingProxy(("127.0.0.1", 0), handler)
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> str:
        if self._closed:
            raise RuntimeError("PROXY_CLOSED")
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        return self.url

    def close(self):
        if self._closed:
            return
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=2)
            self._thread = None
        self._server.server_close()
        self._closed = True


def _without_cli_options(command: list[str], options: set[str]) -> list[str]:
    """Remove a bounded set of Playwright MCP flags and their values."""
    value_options = {"--user-data-dir"}
    result = command[:2]
    index = 2
    while index < len(command):
        argument = command[index]
        matched = next((option for option in options
                        if argument == option or argument.startswith(option + "=")), None)
        if matched is None:
            result.append(argument)
            index += 1
        elif argument == matched and matched in value_options:
            index += 2
        else:
            index += 1
    return result


def playwright_egress_command(command: list[str], proxy_url: str, init_script: str,
                              output_dir: str, *, user_data_dir: str | None = None,
                              interactive_login: bool = False) -> list[str]:
    """Add the pinned egress boundary to the bundled Playwright MCP command.

    Chromium has implicit loopback proxy bypass rules. Playwright translates
    ``<-loopback>`` into Chromium's explicit removal of those defaults, which
    ensures even loopback-looking destinations reach this policy gate. This
    managed public-Web mode therefore does not permit local fixture URLs.
    """
    if len(command) < 2 or not Path(command[1]).as_posix().endswith("node_modules/@playwright/mcp/cli.js"):
        raise ValueError("BUNDLED_PLAYWRIGHT_MCP_REQUIRED")
    if interactive_login and not user_data_dir:
        raise ValueError("PERSISTENT_PROFILE_REQUIRED_FOR_INTERACTIVE_LOGIN")
    if user_data_dir:
        launch = _without_cli_options(command, {"--isolated", "--user-data-dir"})
        if interactive_login:
            launch = _without_cli_options(launch, {"--headless"})
        launch.extend(["--user-data-dir", user_data_dir])
        if (not interactive_login and not any(item == "--headless" or item.startswith("--headless=")
                                               for item in launch[2:])):
            launch.append("--headless")
    else:
        launch = _without_cli_options(command, {"--user-data-dir"})
        if not any(item == "--isolated" or item.startswith("--isolated=") for item in launch[2:]):
            launch.append("--isolated")
        if not any(item == "--headless" or item.startswith("--headless=") for item in launch[2:]):
            launch.append("--headless")
    return [*launch, "--proxy-server", proxy_url,
            "--proxy-bypass", "<-loopback>", "--block-service-workers",
            "--output-dir", output_dir, "--init-script", init_script]


class ManagedBrowserSession:
    """Own a Playwright MCP process and its mandatory pinned egress proxy."""
    SAFE_TOOLS = frozenset({"browser_navigate", "browser_snapshot", "browser_tabs",
                            "browser_navigate_back", "browser_take_screenshot"})
    RECIPE_INTERACTION_TOOLS = frozenset({"browser_click", "browser_press_key", "browser_type"})
    NETWORK_OBSERVER_TOOLS = frozenset({"browser_network_requests"})
    NETWORK_RESPONSE_TOOLS = frozenset({"browser_network_request"})

    def __init__(self, command: list[str], allowed_tools: tuple[str, ...] =
                 ("browser_navigate", "browser_snapshot", "browser_navigate_back",
                  "browser_take_screenshot"), *, timeout: float = 30,
                 startup_timeout: float = 15, shutdown_timeout: float = 3,
                 user_data_dir: str | None = None, interactive_login: bool = False,
                 allowed_local_origin: str | None = None):
        requested_tools = frozenset(allowed_tools)
        if not requested_tools or not requested_tools.issubset(self.SAFE_TOOLS):
            raise ValueError("UNSAFE_BROWSER_TOOL_ALLOWLIST")
        self.allowed_local_origin = (origin_from_url(allowed_local_origin)
                                     if allowed_local_origin else None)
        if allowed_local_origin and self.allowed_local_origin is None:
            raise ValueError("LOCAL_E2E_ORIGIN_MUST_BE_LOOPBACK_HTTP")
        self.proxy = SafeBrowserEgressProxy(allowed_local_origin=self.allowed_local_origin)
        self.command = command
        self.public_tools = tuple(sorted(requested_tools))
        # The generic evaluate tool is never exposed through call(); the only
        # internal call uses the fixed, read-only picker script above.
        self.allowed_tools = tuple(sorted(requested_tools | {"browser_evaluate"} |
                                          self.RECIPE_INTERACTION_TOOLS | self.NETWORK_OBSERVER_TOOLS |
                                          self.NETWORK_RESPONSE_TOOLS))
        self._runtime_options = {"timeout": timeout, "startup_timeout": startup_timeout,
                                 "shutdown_timeout": shutdown_timeout}
        self.user_data_dir = user_data_dir
        self.interactive_login = bool(interactive_login)
        if self.interactive_login and not self.user_data_dir:
            raise ValueError("PERSISTENT_PROFILE_REQUIRED_FOR_INTERACTIVE_LOGIN")
        self.runtime = None
        self._temporary_directory: tempfile.TemporaryDirectory | None = None
        self._init_script_path: str | None = None

    def start(self) -> dict:
        if self.runtime is not None:
            return {"status": "READY" if self.runtime.state == "READY" else self.runtime.state}
        from .mcp_runtime import MCPRuntime

        proxy_url = self.proxy.start()
        try:
            script = ("for (const name of ['RTCPeerConnection','webkitRTCPeerConnection','WebTransport']) {"
                      "try { Object.defineProperty(globalThis, name, {value: undefined, configurable: false}); }"
                      "catch (_) {} }")
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="olcr-browser-session-")
            self._init_script_path = str(Path(self._temporary_directory.name) / "network-policy.js")
            Path(self._init_script_path).write_text(script, encoding="utf-8")
            secured_command = playwright_egress_command(
                self.command, proxy_url, self._init_script_path, self._temporary_directory.name,
                user_data_dir=self.user_data_dir, interactive_login=self.interactive_login)
            self.runtime = MCPRuntime(secured_command, self.allowed_tools, **self._runtime_options)
            self.runtime.start()
            initialized = self.runtime.initialize()
            if initialized.get("status") != "AVAILABLE":
                self.close()
                return initialized
            listing = self.runtime.tools_list()
            if listing.get("status") != "AVAILABLE":
                self.close()
                return listing
            listed = {(entry or {}).get("name") for entry in
                      ((listing.get("response") or {}).get("result") or {}).get("tools", [])
                      if isinstance(entry, dict)}
            if not set(self.allowed_tools).issubset(listed):
                self.close()
                return {"status": "FAILED", "error": "required browser tool missing"}
            return {"status": "READY", "tools": list(self.public_tools)}
        except Exception as exc:
            self.close()
            return {"status": "FAILED", "error": str(exc)}

    def call(self, tool: str, arguments: dict | None = None) -> dict:
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        if tool not in self.public_tools:
            return {"status": "BLOCKED", "error": "tool not allowed"}
        if tool == "browser_navigate":
            url = (arguments or {}).get("url")
            if not isinstance(url, str):
                return {"status": "BLOCKED", "error": "HTTP(S) URL required"}
            try:
                if self.allowed_local_origin:
                    _resolve_local_addresses(url, self.allowed_local_origin)
                else:
                    _resolve_public_addresses(url)
            except ValueError as exc:
                return {"status": "BLOCKED", "error": str(exc)}
        return self.runtime.call(tool, arguments)

    def screenshot(self) -> dict:
        """Return a bounded PNG/JPEG image from the managed Playwright viewport."""
        filename = "chat-browser-viewport.jpg"
        if self._temporary_directory is not None:
            filename = str(Path(self._temporary_directory.name) / filename)
        # The pinned Playwright MCP supports JPEG at CSS-pixel scale; this is
        # sufficient for an in-chat preview and avoids device-pixel PNG bloat.
        # Use an absolute path inside the per-session temp root: this MCP's
        # filename option otherwise writes relative to its process cwd.
        result = self.call("browser_take_screenshot", {
            "type": "jpeg", "scale": "css", "filename": filename,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "BROWSER_SCREENSHOT_FAILED")}
        response = result.get("response")
        payload = response.get("result") if isinstance(response, dict) else None
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, list) or payload.get("isError"):
            return {"status": "FAILED", "error": "BROWSER_SCREENSHOT_RESPONSE_INVALID"}
        image = next((item for item in content
                      if isinstance(item, dict) and item.get("type") == "image"), None)
        if image is not None:
            mime_type = image.get("mimeType")
            encoded = image.get("data")
            if mime_type not in {"image/png", "image/jpeg"} or not isinstance(encoded, str):
                return {"status": "FAILED", "error": "BROWSER_SCREENSHOT_FORMAT_UNSUPPORTED"}
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                return {"status": "FAILED", "error": "BROWSER_SCREENSHOT_DATA_INVALID"}
        else:
            # With filename set, the pinned MCP writes the screenshot into
            # --output-dir and returns text metadata instead of ImageContent.
            if self._temporary_directory is None:
                return {"status": "FAILED", "error": "BROWSER_SCREENSHOT_IMAGE_MISSING"}
            output_dir = Path(self._temporary_directory.name).resolve()
            screenshot_path = output_dir / "chat-browser-viewport.jpg"
            try:
                if screenshot_path.is_symlink() or screenshot_path.resolve().parent != output_dir:
                    return {"status": "FAILED", "error": "BROWSER_SCREENSHOT_PATH_INVALID"}
                raw = screenshot_path.read_bytes()
            except OSError:
                return {"status": "FAILED", "error": "BROWSER_SCREENSHOT_IMAGE_MISSING"}
            mime_type = "image/jpeg"
            encoded = base64.b64encode(raw).decode("ascii")
        if not raw or len(raw) > 4 * 1024 * 1024:
            return {"status": "FAILED", "error": "BROWSER_SCREENSHOT_SIZE_INVALID"}
        signature_ok = (raw.startswith(b"\x89PNG\r\n\x1a\n") if mime_type == "image/png"
                        else raw.startswith(b"\xff\xd8\xff"))
        if not signature_ok:
            return {"status": "FAILED", "error": "BROWSER_SCREENSHOT_SIGNATURE_INVALID"}
        return {"status": "AVAILABLE", "mime_type": mime_type, "data": encoded,
                "timestamp": time.time()}

    def click_local_control(self, selector: str, label: str, origin: str) -> dict:
        if ((self.allowed_local_origin and origin != self.allowed_local_origin)
                or not _same_http_origin(origin, origin) or self.runtime is None or self.runtime.state != "READY"):
            return {"status": "BLOCKED", "error": "LOCAL_E2E_SESSION_NOT_READY"}
        if (not isinstance(selector, str) or not selector or len(selector) > 512
                or any(char in selector for char in "{};\r\n\x00")
                or not isinstance(label, str) or not label or len(label) > 240):
            return {"status": "BLOCKED", "error": "LOCAL_E2E_CLICK_TARGET_INVALID"}
        function = _LOCAL_E2E_CLICK_CHECK.replace("__OLCR_CONFIG__", json.dumps(
            {"selector": selector, "label": label, "origin": origin}, ensure_ascii=True,
            separators=(",", ":")))
        evidence = self.runtime.call("browser_evaluate", {
            "element": "the exact visible local E2E control selected from current page evidence",
            "function": function,
        })
        try:
            payload = _evaluate_json(evidence)
        except (ValueError, TypeError):
            return {"status": "FAILED", "error": "LOCAL_E2E_CLICK_EVIDENCE_INVALID"}
        if evidence.get("status") != "AVAILABLE" or payload.get("ok") is not True:
            return {"status": "BLOCKED", "error": str(payload.get("reason") or "LOCAL_E2E_CLICK_TARGET_REJECTED")}
        result = self.runtime.call("browser_click", {
            "element": f"the visible control labeled {label}", "target": selector,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "LOCAL_E2E_CLICK_FAILED")}
        return {"status": "AVAILABLE", "page": _mcp_text(result), "timestamp": time.time()}

    def inspect_public_form(self) -> dict:
        """Read safe top-document field labels without reading current values."""
        from .browser_forms import PUBLIC_FORM_SCAN, public_form_origin
        if self.allowed_local_origin or self.runtime is None or self.runtime.state != "READY":
            return {"status":"BLOCKED","error":"BROWSER_FORM_SESSION_NOT_READY"}
        evidence=self.runtime.call("browser_evaluate",{"element":"safe visible labelled public form fields", "function":PUBLIC_FORM_SCAN})
        try:
            value=_evaluate_json(evidence)
            public_form_origin(value.get("url"))
            if evidence.get("status")!="AVAILABLE" or not isinstance(value.get("fields"),list) or len(value["fields"])>100 or not isinstance(value.get("origin"),str) or type(value.get("document_id")) not in (int,float):raise ValueError()
        except (ValueError,TypeError):return {"status":"BLOCKED","error":"BROWSER_FORM_INSPECTION_FAILED"}
        return {"status":"AVAILABLE",**{key:value[key] for key in ("url","origin","document_id","fields")}}

    def inspect_travel_page(self) -> dict:
        """Return host-scanned, bounded travel controls without field values or page text."""
        if self.allowed_local_origin or self.runtime is None or self.runtime.state != "READY":
            return {"status":"BLOCKED","error":"TRAVEL_BROWSER_SESSION_NOT_READY"}
        from .travel_browser import TRAVEL_PAGE_SCAN
        result=self.runtime.call("browser_evaluate",{"element":"safe booking controls and visible reservation terms",
                                                       "function":TRAVEL_PAGE_SCAN})
        if result.get("status")!="AVAILABLE":return {"status":result.get("status","FAILED"),"error":"TRAVEL_BROWSER_SCAN_FAILED"}
        try:payload=_evaluate_json(result)
        except (ValueError,TypeError):return {"status":"FAILED","error":"TRAVEL_BROWSER_SCAN_INVALID"}
        if (not isinstance(payload,dict) or not isinstance(payload.get("url"),str)
                or not isinstance(payload.get("origin"),str) or type(payload.get("document_id")) not in (int,float)
                or not isinstance(payload.get("controls"),list) or len(payload["controls"])>200
                or not isinstance(payload.get("terms"),list) or len(payload["terms"])>40):
            return {"status":"FAILED","error":"TRAVEL_BROWSER_SCAN_INVALID"}
        return {"status":"AVAILABLE",**payload}

    def apply_travel_action(self, identity: dict, control: dict, kind: str, value: str | None,
                            permission_check=lambda:True) -> dict:
        """Apply a fixed, typed action to one control from a fresh public page scan."""
        if self.allowed_local_origin or self.runtime is None or self.runtime.state!="READY":
            return {"status":"BLOCKED","error":"TRAVEL_BROWSER_SESSION_NOT_READY"}
        if kind not in {"fill","select","click","commit"} or not isinstance(identity,dict) or not isinstance(control,dict):
            return {"status":"BLOCKED","error":"TRAVEL_BROWSER_ACTION_INVALID"}
        permission_check()
        fresh=self.inspect_travel_page()
        if fresh.get("status")!="AVAILABLE" or any(fresh.get(k)!=identity.get(k) for k in ("url","origin","document_id")):
            return {"status":"BLOCKED","error":"TRAVEL_BROWSER_PAGE_CHANGED"}
        target={key:control.get(key) for key in ("selector","label","tag","type")}
        matches=[item for item in fresh.get("controls",[]) if {key:item.get(key) for key in target}==target]
        if len(matches)!=1 or fresh.get("requires_user_action"):
            return {"status":"BLOCKED","error":"TRAVEL_BROWSER_CONTROL_CHANGED"}
        permission_check()
        from .travel_browser import TRAVEL_PAGE_ACTION
        config={"url":identity["url"],"origin":identity["origin"],"document_id":identity["document_id"],
                "control":target,"kind":kind,"value":value}
        function=TRAVEL_PAGE_ACTION.replace("__CONFIG__",json.dumps(config,ensure_ascii=True,separators=(",",":")))
        result=self.runtime.call("browser_evaluate",{"element":"one exact reviewed booking control action","function":function})
        if result.get("status")!="AVAILABLE":return {"status":result.get("status","FAILED"),"error":"TRAVEL_BROWSER_ACTION_FAILED"}
        try:payload=_evaluate_json(result)
        except (ValueError,TypeError):return {"status":"FAILED","error":"TRAVEL_BROWSER_ACTION_INVALID"}
        if not isinstance(payload,dict) or payload.get("ok") is not True:
            return {"status":"BLOCKED","error":"TRAVEL_BROWSER_ACTION_REJECTED"}
        if kind!="commit":return {"status":"AVAILABLE","applied":True}
        after=self.inspect_travel_page()
        receipt=after.get("receipt") if after.get("status")=="AVAILABLE" else None
        if isinstance(receipt,dict) and receipt.get("status")=="provider_acceptance_displayed":
            return {"status":"AVAILABLE","accepted":True,"reference":receipt.get("reference"),"url":after.get("url")}
        return {"status":"UNCERTAIN","accepted":False}

    def fill_public_form(self, identity: dict, fields: list[dict], permission_check=lambda:True) -> dict:
        """Set reviewed public DOM values, without dispatching page input events."""
        from .browser_forms import PUBLIC_FORM_SET, public_form_origin, safe_form_field
        if self.allowed_local_origin or self.runtime is None or self.runtime.state!="READY":return {"status":"BLOCKED","error":"BROWSER_FORM_SESSION_NOT_READY"}
        try:
            if public_form_origin(identity["url"])!=identity["origin"] or not 1<=len(fields)<=10 or not all(safe_form_field(f) for f in fields):raise ValueError()
            # Reuse the existing exact-visible-label/editability check before mutation.
            for field in fields:
                permission_check()
                config={"selector":field["selector"],"label":field["label"],"origin":identity["origin"],"expected":None}
                check=self.runtime.call("browser_evaluate",{"element":"exact reviewed public text field", "function":_LOCAL_E2E_TYPE_CHECK.replace("__OLCR_CONFIG__",json.dumps(config,ensure_ascii=True))})
                if check.get("status")!="AVAILABLE" or _evaluate_json(check).get("ok") is not True:raise ValueError()
            permission_check()
            config={**identity,"fields":fields}
            result=self.runtime.call("browser_evaluate",{"element":"apply exact reviewed public form values without events or submission", "function":PUBLIC_FORM_SET.replace("__OLCR_CONFIG__",json.dumps(config,ensure_ascii=True))})
            payload=_evaluate_json(result)
            if result.get("status")!="AVAILABLE" or payload.get("ok") is not True:raise ValueError()
        except (ValueError,TypeError,KeyError):return {"status":"BLOCKED","error":"BROWSER_FORM_APPLY_FAILED"}
        return {"status":"AVAILABLE","filled":len(fields),"submitted":False}

    def type_local_field(self, selector: str, label: str, value: str, origin: str) -> dict:
        if not self.allowed_local_origin or self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "LOCAL_E2E_SESSION_NOT_READY"}
        if (not isinstance(selector, str) or not selector or len(selector) > 512
                or any(char in selector for char in "{};\r\n\x00")
                or not isinstance(label, str) or not label or len(label) > 160
                or not isinstance(value, str) or not value or len(value) > 1000
                or origin != self.allowed_local_origin):
            return {"status": "BLOCKED", "error": "LOCAL_E2E_TYPE_TARGET_INVALID"}

        def check(expected: str | None) -> dict:
            function = _LOCAL_E2E_TYPE_CHECK.replace("__OLCR_CONFIG__", json.dumps(
                {"selector": selector, "label": label, "origin": origin, "expected": expected},
                ensure_ascii=True, separators=(",", ":")))
            evidence = self.runtime.call("browser_evaluate", {
                "element": "the exact visible editable local E2E text field selected from current page evidence",
                "function": function,
            })
            try:
                payload = _evaluate_json(evidence)
            except (ValueError, TypeError):
                return {"status": "FAILED", "error": "LOCAL_E2E_TYPE_EVIDENCE_INVALID"}
            if evidence.get("status") != "AVAILABLE" or payload.get("ok") is not True:
                return {"status": "BLOCKED", "error": str(payload.get("reason") or "LOCAL_E2E_TYPE_TARGET_REJECTED")}
            return {"status": "AVAILABLE"}

        validated = check(None)
        if validated.get("status") != "AVAILABLE":
            return validated
        result = self.runtime.call("browser_type", {
            "element": f"the visible editable field labeled {label}", "target": selector, "text": value,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "LOCAL_E2E_TYPE_FAILED")}
        confirmed = check(value)
        if confirmed.get("status") != "AVAILABLE":
            return confirmed
        return {"status": "AVAILABLE", "page": _mcp_text(result), "timestamp": time.time()}

    def picker_evidence(self) -> dict:
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        result = self.runtime.call("browser_evaluate", {
            "element": "visible page elements for a scrape preview",
            "function": _PICKER_EVIDENCE_SCRIPT,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "PICKER_EVIDENCE_FAILED")}
        text = _mcp_text(result)
        try:
            # Playwright MCP renders evaluate results as a Markdown "Result"
            # section whose first line is a JSON-quoted string. Keep support
            # for servers that return the JSON value directly as well.
            result_text = text.split("\n", 1)[1].strip() if text.startswith("### Result\n") else text
            payload, _consumed = json.JSONDecoder().raw_decode(result_text)
            if isinstance(payload, str):
                payload, _consumed = json.JSONDecoder().raw_decode(payload)
        except (ValueError, TypeError):
            return {"status": "FAILED", "error": "PICKER_EVIDENCE_INVALID"}
        if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
            return {"status": "FAILED", "error": "PICKER_EVIDENCE_INVALID"}
        return {"status": "AVAILABLE", "candidates": payload["candidates"],
                "truncated": bool(payload.get("truncated")), "timestamp": time.time()}

    def wait_for_page_load(self) -> dict:
        """Wait for the current document's load event with a fixed timeout."""
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        result = self.runtime.call("browser_evaluate", {
            "element": "current document load readiness",
            "function": _PAGE_LOAD_READY_SCRIPT,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "PAGE_LOAD_WAIT_FAILED")}
        try:
            payload = _evaluate_json(result)
        except (ValueError, TypeError):
            return {"status": "FAILED", "error": "PAGE_LOAD_WAIT_INVALID"}
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            return {"status": "FAILED", "error": "PAGE_LOAD_WAIT_TIMEOUT"}
        return {"status": "AVAILABLE", "ready": True}

    def extract_recipe(self, record_selector: str, fields: dict, *, limit: int = 100, tool: str | None = None, point: dict | None = None) -> dict:
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        config = {"record_selector": record_selector, "fields": fields, "limit": max(1, min(int(limit), 10000 if tool else 100)), "tool": tool, "point": point}
        function = (EXTRACT_SCRIPT if tool else _RECIPE_EXTRACT_SCRIPT).replace("__OLCR_CONFIG__", json.dumps(config, ensure_ascii=True, separators=(",", ":")))
        result = self.runtime.call("browser_evaluate", {
            "element": "configured visible page records for a validated Recipe preview",
            "function": function,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"), "error": str(result.get("error") or "RECIPE_EXTRACTION_FAILED")}
        try:
            payload = _evaluate_json(result)
        except (ValueError, TypeError):
            return {"status": "FAILED", "error": "RECIPE_EXTRACTION_INVALID"}
        if not isinstance(payload, dict) or "rows" not in payload:
            return {"status": "FAILED", "error": str(payload.get("error") or "RECIPE_EXTRACTION_INVALID") if isinstance(payload, dict) else "RECIPE_EXTRACTION_INVALID"}
        return {"status": "AVAILABLE", "rows": payload["rows"],
                "record_count": int(payload.get("record_count") or 0), "truncated": bool(payload.get("truncated")),
                "field_match_counts": payload.get("field_match_counts", {}),
                "record_selector": payload.get("record_selector"),
                "record_rects": payload.get("record_rects", []),
                "navigation_candidates": payload.get("navigation_candidates", []),
                "internal_links": payload.get("internal_links", []),
                "timestamp": time.time()}

    def probe_recipe_selectors(self, candidates: list[dict], fields: dict) -> dict:
        """Compare a small set of server-generated row selectors read-only."""
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 12:
            return {"status": "BLOCKED", "error": "RECIPE_RECOVERY_CANDIDATES_INVALID"}
        if not isinstance(fields, dict) or len(fields) > 50:
            return {"status": "BLOCKED", "error": "RECIPE_RECOVERY_FIELDS_INVALID"}
        normalized = []
        for item in candidates:
            selector = item.get("selector") if isinstance(item, dict) else None
            candidate_id = item.get("candidate_id") if isinstance(item, dict) else None
            if (not isinstance(candidate_id, str) or len(candidate_id) > 24
                    or not isinstance(selector, str) or not selector.strip() or len(selector) > 512
                    or any(char in selector for char in "{};\r\n\x00")):
                return {"status": "BLOCKED", "error": "RECIPE_RECOVERY_CANDIDATES_INVALID"}
            normalized.append({"candidate_id": candidate_id, "selector": selector.strip()})
        for definition in fields.values():
            if (not isinstance(definition, dict) or not isinstance(definition.get("selector"), str)
                    or not definition["selector"].strip() or len(definition["selector"]) > 512
                    or any(char in definition["selector"] for char in "{};\r\n\x00")):
                return {"status": "BLOCKED", "error": "RECIPE_RECOVERY_FIELDS_INVALID"}
        config = {"candidates": normalized, "fields": fields}
        function = _RECIPE_SELECTOR_RECOVERY_SCRIPT.replace(
            "__OLCR_CONFIG__", json.dumps(config, ensure_ascii=True, separators=(",", ":")))
        result = self.runtime.call("browser_evaluate", {
            "element": "bounded read-only Recipe selector recovery evidence",
            "function": function,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "RECIPE_RECOVERY_PROBE_FAILED")}
        try:
            payload = _evaluate_json(result)
        except (ValueError, TypeError):
            return {"status": "FAILED", "error": "RECIPE_RECOVERY_PROBE_INVALID"}
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) != len(normalized):
            return {"status": "FAILED", "error": "RECIPE_RECOVERY_PROBE_INVALID"}
        expected = {item["candidate_id"]: item["selector"] for item in normalized}
        seen_ids = set()
        results = []
        for item in rows:
            if not isinstance(item, dict):
                return {"status": "FAILED", "error": "RECIPE_RECOVERY_PROBE_INVALID"}
            candidate_id = item.get("candidate_id")
            if (not isinstance(candidate_id, str) or candidate_id not in expected
                    or candidate_id in seen_ids or item.get("selector") != expected[candidate_id]):
                return {"status": "FAILED", "error": "RECIPE_RECOVERY_PROBE_INVALID"}
            seen_ids.add(candidate_id)
            count = item.get("record_count")
            field_counts = item.get("field_match_counts")
            if (not isinstance(count, int) or isinstance(count, bool) or count < 0
                    or not isinstance(field_counts, dict)):
                return {"status": "FAILED", "error": "RECIPE_RECOVERY_PROBE_INVALID"}
            results.append({"candidate_id": candidate_id,
                            "selector": str(item.get("selector") or "")[:512],
                            "record_count": count,
                            "field_match_counts": {str(key)[:120]: int(value) for key, value in field_counts.items()
                                                   if isinstance(value, int) and not isinstance(value, bool) and value >= 0},
                            "error": str(item.get("error") or "")[:80] or None})
        return {"status": "AVAILABLE", "results": results, "timestamp": time.time()}

    def extract_next_link(self, selector: str) -> dict:
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        function = _RECIPE_NEXT_LINK_SCRIPT.replace(
            "__OLCR_CONFIG__", json.dumps({"selector": selector}, ensure_ascii=True, separators=(",", ":")))
        result = self.runtime.call("browser_evaluate", {
            "element": "configured next-page link for a validated Recipe",
            "function": function,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "RECIPE_NEXT_LINK_FAILED")}
        try:
            payload = _evaluate_json(result)
        except (ValueError, TypeError):
            return {"status": "FAILED", "error": "RECIPE_NEXT_LINK_INVALID"}
        if not isinstance(payload, dict) or "url" not in payload:
            return {"status": "FAILED", "error": "RECIPE_NEXT_LINK_INVALID"}
        return {"status": "AVAILABLE", "url": payload.get("url"), "timestamp": time.time()}

    def click_recipe_load_more(self, selector: str, label: str | None = None) -> dict:
        """Click only a unique, visible, enabled button in a validated Recipe."""
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        config = {"selector": selector, "label": label}
        function = _RECIPE_LOAD_MORE_SCRIPT.replace(
            "__OLCR_CONFIG__", json.dumps(config, ensure_ascii=True, separators=(",", ":")))
        evidence = self.runtime.call("browser_evaluate", {
            "element": "configured load-more button for a validated Recipe",
            "function": function,
        })
        try:
            payload = _evaluate_json(evidence)
        except (ValueError, TypeError):
            return {"status": "FAILED", "error": "LOAD_MORE_TARGET_INVALID"}
        if evidence.get("status") != "AVAILABLE" or not isinstance(payload, dict) or payload.get("ok") is not True:
            reason = payload.get("reason") if isinstance(payload, dict) else None
            return {"status": "FAILED", "error": str(reason or evidence.get("error") or "LOAD_MORE_TARGET_INVALID")}
        result = self.runtime.call("browser_click", {
            "element": "the configured load-more button for a validated Recipe",
            "target": selector,
        })
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "LOAD_MORE_CLICK_FAILED")}
        return {"status": "AVAILABLE", "page": _mcp_text(result), "timestamp": time.time()}

    def scroll_for_recipe_page(self) -> dict:
        """Advance one bounded page of an infinite-scroll Recipe via the browser key API."""
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        result = self.runtime.call("browser_press_key", {"key": "End"})
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "INFINITE_SCROLL_FAILED")}
        return {"status": "AVAILABLE", "page": _mcp_text(result), "timestamp": time.time()}

    def network_requests(self) -> dict:
        """Return bounded request metadata only; never read headers or bodies."""
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        result = self.runtime.call("browser_network_requests", {"static": False})
        if result.get("status") != "AVAILABLE":
            return {"status": result.get("status", "FAILED"),
                    "error": str(result.get("error") or "NETWORK_OBSERVER_FAILED")}
        text = _mcp_text(result)
        requests = []
        pattern = re.compile(r"^\s*(\d+)\. \[([A-Z]+)\] (https?://\S+?)(?: => \[(\d{3})\] ([^\n]+))?\s*$")
        for line in text.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            index, method, raw_url, status, status_text = match.groups()
            try:
                parsed = urlsplit(raw_url)
                if parsed.username or parsed.password or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
                port = f":{parsed.port}" if parsed.port else ""
                # Query strings and fragments commonly contain session state or tokens.
                safe_url = f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path[:512]}"
            except ValueError:
                continue
            requests.append({"index": int(index), "method": method,
                             "url": safe_url, "status_code": int(status) if status else None,
                             "status_text": (status_text or "")[:120]})
            if len(requests) >= 100:
                break
        return {"status": "AVAILABLE", "requests": requests,
                "truncated": len(requests) >= 100, "timestamp": time.time()}

    def network_json_evidence(self, request_index: int, current_url: str) -> dict:
        """Read one explicitly selected, same-origin JSON GET response as bounded transient evidence."""
        metadata, payload = self._read_network_json_payload(request_index, current_url)
        if metadata.get("status") != "AVAILABLE":
            return metadata
        redacted, truncated = _bounded_network_json(payload)
        return {**metadata, "preview": redacted, "truncated": truncated, "persisted": False}

    def network_json_recipe_rows(self, current_url: str, endpoint_path: str, records_pointer: str,
                                 fields: dict[str, str], *, limit: int = 100) -> dict:
        """Extract rows from exactly one observed same-origin JSON GET response."""
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}
        listing = self.network_requests()
        if listing.get("status") != "AVAILABLE":
            return listing
        matches = []
        for request in listing["requests"]:
            if request.get("method") != "GET" or not isinstance(request.get("status_code"), int):
                continue
            try:
                if urlsplit(request["url"]).path == endpoint_path:
                    matches.append(request)
            except (KeyError, TypeError, ValueError):
                continue
        if len(matches) != 1:
            return {"status": "BLOCKED", "error": "NETWORK_JSON_ENDPOINT_NOT_UNIQUE" if matches else "NETWORK_JSON_ENDPOINT_NOT_FOUND"}
        request = matches[0]
        if not 200 <= request["status_code"] < 300:
            return {"status": "BLOCKED", "error": "NETWORK_JSON_GET_REQUIRED"}
        metadata, payload = self._read_network_json_payload(request["index"], current_url)
        if metadata.get("status") != "AVAILABLE":
            return metadata
        try:
            sanitized = _sanitize_network_json_payload(payload)
            source = JsonRecipeSource.model_validate({
                "endpoint_path": endpoint_path,
                "records_pointer": records_pointer,
                "fields": {name: {"pointer": pointer} for name, pointer in fields.items()},
            })
            extracted = extract_json_recipe_rows(sanitized, source, limit=limit)
        except (ValueError, TypeError, RecursionError) as exc:
            return {"status": "BLOCKED", "error": str(exc)[:120] or "NETWORK_JSON_EXTRACTION_FAILED"}
        return {"status": "AVAILABLE", **extracted,
                "request_index": request["index"], "url": request["url"],
                "status_code": request["status_code"],
                "content_type": metadata["content_type"], "persisted": False,
                "timestamp": time.time()}

    def _read_network_json_payload(self, request_index: int, current_url: str) -> tuple[dict, object | None]:
        if self.runtime is None or self.runtime.state != "READY":
            return {"status": "BLOCKED", "error": "browser session not ready"}, None
        if not isinstance(request_index, int) or request_index < 1:
            return {"status": "BLOCKED", "error": "NETWORK_REQUEST_INDEX_INVALID"}, None
        listing = self.network_requests()
        if listing.get("status") != "AVAILABLE":
            return listing, None
        request = next((item for item in listing["requests"] if item["index"] == request_index), None)
        if request is None:
            return {"status": "BLOCKED", "error": "NETWORK_REQUEST_NOT_FOUND"}, None
        if request["method"] != "GET" or not isinstance(request.get("status_code"), int) or not 200 <= request["status_code"] < 300:
            return {"status": "BLOCKED", "error": "NETWORK_JSON_GET_REQUIRED"}, None
        if not _same_http_origin(current_url, request["url"]):
            return {"status": "BLOCKED", "error": "NETWORK_JSON_CROSS_ORIGIN"}, None

        headers = self.runtime.call("browser_network_request", {
            "index": request_index, "part": "response-headers",
        })
        if headers.get("status") != "AVAILABLE":
            return ({"status": headers.get("status", "FAILED"),
                    "error": str(headers.get("error") or "NETWORK_RESPONSE_HEADERS_UNAVAILABLE")}, None)
        header_text = _mcp_text(headers)
        content_type = _header_value(header_text, "content-type")
        mime_type = (content_type or "").split(";", 1)[0].strip().lower()
        if not (mime_type == "application/json" or
                (mime_type.startswith("application/") and mime_type.endswith("+json"))):
            return {"status": "BLOCKED", "error": "NETWORK_RESPONSE_NOT_JSON"}, None
        content_length = _header_value(header_text, "content-length")
        if content_length is None:
            return {"status": "BLOCKED", "error": "NETWORK_RESPONSE_LENGTH_UNKNOWN"}, None
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                raise ValueError
            if declared_length > 64 * 1024:
                return {"status": "BLOCKED", "error": "NETWORK_JSON_TOO_LARGE"}, None
        except ValueError:
            return {"status": "BLOCKED", "error": "NETWORK_RESPONSE_LENGTH_INVALID"}, None

        body = self.runtime.call("browser_network_request", {
            "index": request_index, "part": "response-body",
        })
        if body.get("status") != "AVAILABLE":
            return ({"status": body.get("status", "FAILED"),
                    "error": str(body.get("error") or "NETWORK_RESPONSE_BODY_UNAVAILABLE")}, None)
        body_text = _network_response_body(_mcp_text(body))
        if len(body_text.encode("utf-8")) > 64 * 1024:
            return {"status": "BLOCKED", "error": "NETWORK_JSON_TOO_LARGE"}, None
        try:
            payload = json.loads(body_text)
        except (ValueError, TypeError, RecursionError):
            return {"status": "BLOCKED", "error": "NETWORK_RESPONSE_JSON_INVALID"}, None
        if not isinstance(payload, (dict, list)):
            return {"status": "BLOCKED", "error": "NETWORK_RESPONSE_JSON_SHAPE_INVALID"}, None
        metadata = {"status": "AVAILABLE", "request_index": request_index,
                    "url": request["url"], "status_code": request["status_code"],
                    "content_type": mime_type, "persisted": False,
                    "timestamp": time.time()}
        return metadata, payload

    def close(self):
        runtime, self.runtime = self.runtime, None
        try:
            if runtime is not None:
                runtime.close()
        finally:
            try:
                self.proxy.close()
            finally:
                if self._temporary_directory is not None:
                    self._temporary_directory.cleanup()
                    self._temporary_directory = None
                    self._init_script_path = None


class BrowserSessionManager:
    """Bounded process-local registry for explicit Web/Scrape browser sessions."""
    def __init__(self, command_provider: Callable[[], list[str] | None], *,
                 maximum_sessions: int = 2, idle_timeout: float = 900,
                 session_factory=ManagedBrowserSession,
                 profile_root: str | Path | None = None, session_policy=None):
        self._session_policy = session_policy
        self._command_provider = command_provider
        self._maximum_sessions = max(1, maximum_sessions)
        self._idle_timeout = max(60, idle_timeout)
        self._session_factory = session_factory
        self._profile_root = Path(profile_root).expanduser().resolve() if profile_root is not None else None
        self._sessions: dict[str, dict] = {}
        self._active_profiles: dict[str, str] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None
        self._closed = False

    @staticmethod
    def _validated_profile_id(profile_id: str) -> str:
        value = str(profile_id or "")
        if len(value) > 64 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", value):
            raise ValueError("BROWSER_PROFILE_ID_INVALID")
        return value

    def _profile_directory(self, profile_id: str, *, create: bool) -> Path:
        if self._profile_root is None:
            raise ValueError("BROWSER_PROFILES_UNAVAILABLE")
        self._profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._profile_root.chmod(0o700)
        except OSError:
            pass
        directory = self._profile_root / profile_id
        if directory.is_symlink():
            raise ValueError("BROWSER_PROFILE_PATH_INVALID")
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        if not directory.is_dir():
            raise ValueError("BROWSER_PROFILE_NOT_FOUND")
        resolved = directory.resolve()
        if resolved.parent != self._profile_root:
            raise ValueError("BROWSER_PROFILE_PATH_INVALID")
        return resolved

    def profiles(self) -> list[dict]:
        if self._profile_root is None or not self._profile_root.is_dir():
            return []
        with self._lock:
            result = []
            for directory in self._profile_root.iterdir():
                if directory.is_symlink() or not directory.is_dir():
                    continue
                try:
                    profile_id = self._validated_profile_id(directory.name)
                    self._profile_directory(profile_id, create=False)
                except ValueError:
                    continue
                result.append({"profile_id": profile_id,
                               "in_use": profile_id in self._active_profiles})
            return sorted(result, key=lambda item: item["profile_id"])

    def forget_profile(self, profile_id: str) -> bool:
        profile_id = self._validated_profile_id(profile_id)
        with self._lock:
            if profile_id in self._active_profiles:
                raise ValueError("BROWSER_PROFILE_IN_USE")
            directory = self._profile_directory(profile_id, create=False)
            shutil.rmtree(directory)
            return True

    def open(self, profile_id: str | None = None, *, interactive_login: bool = False) -> dict:
        return self._open(profile_id, interactive_login=interactive_login)

    def open_local(self, origin: str) -> dict:
        """Open a browser whose egress is fixed to one project-local origin."""
        normalized = origin_from_url(origin)
        if normalized is None:
            return {"status": "FAILED", "error": "LOCAL_E2E_ORIGIN_MUST_BE_LOOPBACK_HTTP"}
        return self._open(allowed_local_origin=normalized)

    def _open(self, profile_id: str | None = None, *, interactive_login: bool = False,
              allowed_local_origin: str | None = None) -> dict:
        with self._lock:
            if self._closed:
                return {"status": "UNAVAILABLE", "error": "BROWSER_SESSION_MANAGER_CLOSED"}
            if interactive_login and not profile_id:
                return {"status": "FAILED", "error": "PERSISTENT_PROFILE_REQUIRED_FOR_INTERACTIVE_LOGIN"}
            self._expire_locked(time.monotonic())
            if len(self._sessions) >= self._maximum_sessions:
                return {"status": "LIMIT_REACHED", "error": "BROWSER_SESSION_LIMIT_REACHED"}
            user_data_dir = None
            if profile_id and allowed_local_origin:
                return {"status": "FAILED", "error": "LOCAL_E2E_PERSISTENT_PROFILE_UNSUPPORTED"}
            if profile_id:
                try:
                    profile_id = self._validated_profile_id(profile_id)
                    if profile_id in self._active_profiles:
                        return {"status": "PROFILE_IN_USE", "error": "BROWSER_PROFILE_IN_USE"}
                    user_data_dir = str(self._profile_directory(profile_id, create=True))
                except ValueError as exc:
                    return {"status": "FAILED", "error": str(exc)}
            command = self._command_provider()
            if not command:
                return {"status": "UNAVAILABLE", "error": "BUNDLED_PLAYWRIGHT_UNAVAILABLE"}
            if allowed_local_origin:
                try:
                    session = self._session_factory(command, allowed_local_origin=allowed_local_origin)
                except TypeError:
                    return {"status": "UNAVAILABLE", "error": "LOCAL_E2E_BROWSER_FACTORY_UNSUPPORTED"}
            elif profile_id:
                session = self._session_factory(command, user_data_dir=user_data_dir,
                                                interactive_login=interactive_login)
            else:
                session = self._session_factory(command)
            ready = session.start()
            if ready.get("status") != "READY":
                session.close()
                return {"status": "FAILED", "error": str(ready.get("error") or "BROWSER_START_FAILED")}
            session_id = str(uuid.uuid4())
            now = time.monotonic(); wall_now = time.time()
            self._sessions[session_id] = {"session": session, "created_at": wall_now,
                                          "last_activity": wall_now,
                                          "last_activity_monotonic": now, "url": "",
                                          "profile_id": profile_id,
                                          "interactive_login": bool(interactive_login),
                                          "allowed_local_origin": allowed_local_origin}
            if profile_id:
                self._active_profiles[profile_id] = session_id
            if self._sweeper is None:
                self._sweeper = threading.Thread(target=self._sweep, daemon=True,
                                                 name="olcr-browser-session-sweeper")
                self._sweeper.start()
            return {"status": "READY", "session_id": session_id,
                    "tools": ready.get("tools", []), "profile_id": profile_id,
                    "interactive_login": bool(interactive_login),
                    "local_e2e": bool(allowed_local_origin),
                    "origin": allowed_local_origin}

    def list(self) -> list[dict]:
        with self._lock:
            self._expire_locked(time.monotonic())
            return [{"session_id": key, "created_at": entry["created_at"],
                     "last_activity": entry["last_activity"], "url": entry["url"],
                     "profile_id": entry.get("profile_id"),
                     "interactive_login": bool(entry.get("interactive_login"))}
                    for key, entry in self._sessions.items()]

    def navigate(self, session_id: str, url: str) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            allowed_local_origin = entry.get("allowed_local_origin")
            if allowed_local_origin and origin_from_url(url) != allowed_local_origin:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_ORIGIN_MISMATCH"}
            result = entry["session"].call("browser_navigate", {"url": url})
            model_telemetry.record_playwright_action(
                "navigate", successful=result.get("status") == "AVAILABLE")
            if result.get("status") != "AVAILABLE":
                return {"status": result.get("status", "FAILED"),
                        "error": str(result.get("error") or "BROWSER_NAVIGATION_FAILED")}
            page = _mcp_text(result)
            if not page:
                return {"status": "FAILED", "error": "BROWSER_NAVIGATION_EMPTY"}
            snapshot = entry["session"].call("browser_snapshot", {})
            if snapshot.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_FAILED"}
            content = _mcp_text(snapshot)
            if not content:
                return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_EMPTY"}
            entry["url"] = _page_value(page, "Page URL") or url
            if allowed_local_origin and origin_from_url(entry["url"]) != allowed_local_origin:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_NAVIGATION_LEFT_ORIGIN"}
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
            return {"status": "AVAILABLE", "url": entry["url"],
                    "title": _page_value(page, "Page Title"),
                    "clean_markdown": content[:50_000], "truncated": len(content) > 50_000,
                    "timestamp": time.time(), "provider": "playwright_accessibility",
                    "warnings": ["Content is an accessibility snapshot, not a full DOM export."]}

    def navigate_local_path(self, session_id: str, path: str) -> dict:
        """Navigate within the active local E2E session's exact HTTP origin."""
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or len(path) > 512 or "\\" in path
                or any(ord(char) < 0x20 for char in path)):
            return {"status": "BLOCKED", "error": "LOCAL_E2E_PATH_INVALID"}
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc:
            return {"status": "BLOCKED", "error": "LOCAL_E2E_PATH_INVALID"}
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            origin = entry.get("allowed_local_origin")
            if not origin or origin_from_url(entry.get("url") or "") != origin:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_SESSION_NOT_ACTIVE"}
        return self.navigate(session_id, f"{origin}{path}")

    def check_local_text(self, session_id: str, expected_text: str, *,
                         wait: bool = False, timeout_seconds: float = 10.0) -> dict:
        """Check text in fresh accessibility snapshots, with a bounded optional wait."""
        expected = " ".join(str(expected_text or "").split())
        if not expected or len(expected) > 160:
            return {"status": "BLOCKED", "error": "LOCAL_E2E_EXPECTED_TEXT_INVALID"}
        timeout = max(0.0, min(float(timeout_seconds), 10.0)) if wait else 0.0
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                entry = self._get_locked(session_id)
                if entry is None:
                    return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
                origin = entry.get("allowed_local_origin")
                if not origin or origin_from_url(entry.get("url") or "") != origin:
                    return {"status": "BLOCKED", "error": "LOCAL_E2E_SESSION_NOT_ACTIVE"}
            snapshot = self.snapshot(session_id)
            if snapshot.get("status") != "AVAILABLE":
                return {"status": snapshot.get("status", "FAILED"),
                        "error": str(snapshot.get("error") or "LOCAL_E2E_SNAPSHOT_FAILED")}
            visible_text = " ".join(str(snapshot.get("clean_markdown") or "").split())
            if expected.casefold() in visible_text.casefold():
                model_telemetry.record_playwright_action(
                    "wait_for_text" if wait else "assert_text", successful=True)
                return {"status": "AVAILABLE", "verified": True, "text": expected,
                        "url": snapshot.get("url"), "timestamp": snapshot.get("timestamp")}
            if time.monotonic() >= deadline:
                model_telemetry.record_playwright_action(
                    "wait_for_text" if wait else "assert_text", successful=False)
                return {"status": "FAILED", "verified": False,
                        "error": "LOCAL_E2E_TEXT_WAIT_TIMEOUT" if wait else "LOCAL_E2E_TEXT_NOT_FOUND",
                        "url": snapshot.get("url"), "timestamp": snapshot.get("timestamp")}
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def navigate_back(self, session_id: str) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].call("browser_navigate_back", {})
            model_telemetry.record_playwright_action(
                "navigate_back", successful=result.get("status") == "AVAILABLE")
            if result.get("status") != "AVAILABLE":
                return {"status": result.get("status", "FAILED"),
                        "error": str(result.get("error") or "BROWSER_BACK_FAILED")}
            page = _mcp_text(result)
            url = _page_value(page, "Page URL")
            if not page or not url:
                return {"status": "FAILED", "error": "BROWSER_BACK_STATE_UNAVAILABLE"}
            snapshot = entry["session"].call("browser_snapshot", {})
            if snapshot.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_FAILED"}
            content = _mcp_text(snapshot)
            if not content:
                return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_EMPTY"}
            entry["url"] = url
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
            return {"status": "AVAILABLE", "url": url,
                    "title": _page_value(page, "Page Title"),
                    "clean_markdown": content[:50_000], "truncated": len(content) > 50_000,
                    "timestamp": time.time(), "provider": "playwright_accessibility",
                    "warnings": ["Content is an accessibility snapshot, not a full DOM export."]}

    def reload(self, session_id: str) -> dict:
        """Reload the current public URL through the same validated navigation route."""
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            url = entry.get("url")
        if not url:
            return {"status": "FAILED", "error": "BROWSER_SESSION_NOT_NAVIGATED"}
        # The pinned Playwright MCP has no dedicated reload tool. Re-navigating
        # the current URL uses its supported navigation tool and the same DNS
        # pinning, while keeping this operation explicitly tied to this session.
        return self.navigate(session_id, url)

    def network_requests(self, session_id: str) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].network_requests()
            if result.get("status") == "AVAILABLE":
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
                result["url"] = entry["url"]
            return result

    def network_json_evidence(self, session_id: str, request_index: int) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].network_json_evidence(request_index, entry.get("url") or "")
            if result.get("status") == "AVAILABLE":
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
            return result

    def snapshot(self, session_id: str) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].call("browser_snapshot", {})
            if result.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_FAILED"}
            content = _mcp_text(result)
            if not content:
                return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_EMPTY"}
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
            return {"status": "AVAILABLE", "url": entry["url"],
                    "clean_markdown": content[:50_000], "truncated": len(content) > 50_000,
                    "timestamp": time.time(), "provider": "playwright_accessibility"}

    def screenshot(self, session_id: str) -> dict:
        """Capture the current viewport for a chat task surface."""
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].screenshot()
            if result.get("status") != "AVAILABLE":
                return result
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
            return {**result, "url": entry["url"]}

    def wait_for_page_load(self, session_id: str) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].wait_for_page_load()
            if result.get("status") == "AVAILABLE":
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
            return result

    def picker_evidence(self, session_id: str, kind: str = "all") -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].picker_evidence()
            if result.get("status") != "AVAILABLE":
                return {"status": result.get("status", "FAILED"),
                        "error": str(result.get("error") or "PICKER_EVIDENCE_FAILED")}
            candidates = result.get("candidates", [])
            if kind != "all":
                candidates = [item for item in candidates
                              if isinstance(item, dict) and item.get("kind") == kind]
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
            return {"status": "AVAILABLE", "url": entry["url"],
                    "candidates": candidates, "truncated": result.get("truncated", False),
                    "timestamp": result.get("timestamp", time.time())}

    def click_local_control(self, session_id: str, label: str) -> dict:
        """Click one exact, unique visible control on a local E2E page."""
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            origin = entry.get("allowed_local_origin")
            if not origin or origin_from_url(entry.get("url") or "") != origin:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_SESSION_NOT_ACTIVE"}
            evidence = entry["session"].picker_evidence()
            if evidence.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": str(evidence.get("error") or "LOCAL_E2E_EVIDENCE_FAILED")}
            wanted = " ".join(str(label or "").split())
            candidates = [candidate for candidate in evidence.get("candidates", [])
                          if isinstance(candidate, dict)
                          and candidate.get("kind") in {"control", "navigation"}
                          and candidate.get("tag") in {"button", "a"}
                          and " ".join(str(candidate.get("text") or "").split()) == wanted]
            if len(candidates) != 1:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_CLICK_TARGET_NOT_UNIQUE"}
            candidate = candidates[0]
            selectors = candidate.get("selector_candidates")
            if not isinstance(selectors, list) or not selectors:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_CLICK_SELECTOR_UNAVAILABLE"}
            result = entry["session"].click_local_control(str(selectors[0]), wanted, origin)
            if result.get("status") == "AVAILABLE":
                result["before_url"] = entry["url"]
                result["url"] = _page_value(str(result.get("page") or ""), "Page URL") or entry["url"]
                if origin_from_url(result["url"]) != origin:
                    return {"status": "BLOCKED", "error": "LOCAL_E2E_NAVIGATION_LEFT_ORIGIN"}
                entry["url"] = result["url"]
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
            return result

    def _public_form_entry(self, session_id):
        entry=self._get_locked(session_id)
        if entry is None or entry.get("allowed_local_origin"):return None
        runtime=getattr(entry["session"],"runtime",None);process=getattr(runtime,"process",None)
        if runtime is None or runtime.state!="READY" or process is None or process.poll() is not None or type(process.pid) is not int or process.pid<=0:return None
        return entry

    def inspect_public_form(self, session_id: str) -> dict:
        """Inspect only an owned live public session, bound to its process PID."""
        with self._lock:
            entry=self._public_form_entry(session_id)
            if entry is None:return {"status":"BLOCKED","error":"BROWSER_FORM_SESSION_NOT_READY"}
            result=entry["session"].inspect_public_form()
            if result.get("status")!="AVAILABLE":return result
            entry["url"]=result["url"]
            return {**result,"session_id":session_id,"process_id":entry["session"].runtime.process.pid}

    def inspect_travel_page(self, session_id: str) -> dict:
        """Inspect a single owned public session and bind evidence to its process."""
        with self._lock:
            entry=self._public_form_entry(session_id)
            if entry is None:return {"status":"BLOCKED","error":"TRAVEL_BROWSER_SESSION_NOT_READY"}
            result=entry["session"].inspect_travel_page()
            if result.get("status")!="AVAILABLE":return result
            try:
                from .travel_browser import _origin
                origin=_origin(result.get("url"))
            except ValueError:return {"status":"BLOCKED","error":"TRAVEL_BROWSER_PUBLIC_HTTPS_REQUIRED"}
            if result.get("origin")!=origin:return {"status":"BLOCKED","error":"TRAVEL_BROWSER_ORIGIN_INVALID"}
            entry["url"]=result["url"]
            return {**result,"session_id":session_id,"process_id":entry["session"].runtime.process.pid}

    def apply_travel_action(self, session_id: str, identity: dict, control: dict, kind: str,
                            value: str | None, *, permission_check=lambda:True) -> dict:
        with self._lock:
            permission_check()
            entry=self._public_form_entry(session_id)
            if (entry is None or identity.get("session_id")!=session_id
                    or identity.get("process_id")!=entry["session"].runtime.process.pid
                    or identity.get("url")!=entry.get("url")):
                return {"status":"BLOCKED","error":"TRAVEL_BROWSER_SESSION_CHANGED"}
            fresh=entry["session"].inspect_travel_page()
            if (fresh.get("status")!="AVAILABLE" or any(fresh.get(k)!=identity.get(k) for k in ("url","origin","document_id"))):
                return {"status":"BLOCKED","error":"TRAVEL_BROWSER_PAGE_CHANGED"}
            permission_check()
            result=entry["session"].apply_travel_action(identity,control,kind,value,permission_check)
            if result.get("status")=="AVAILABLE":
                current=entry["session"].inspect_travel_page()
                if current.get("status")=="AVAILABLE":entry["url"]=current.get("url",entry.get("url"))
            return result

    def fill_public_form(self, session_id: str, identity: dict, fields: list[dict], *, permission_check=lambda:True) -> dict:
        with self._lock:
            permission_check()
            entry=self._public_form_entry(session_id)
            if entry is None or identity.get("session_id")!=session_id or identity.get("process_id")!=entry["session"].runtime.process.pid or identity.get("url")!=entry.get("url"):
                return {"status":"BLOCKED","error":"BROWSER_FORM_SESSION_CHANGED"}
            evidence=entry["session"].inspect_public_form()
            if evidence.get("status")!="AVAILABLE" or any(evidence.get(key)!=identity.get(key) for key in ("url","origin","document_id")):
                return {"status":"BLOCKED","error":"BROWSER_FORM_PAGE_CHANGED"}
            for field in fields:
                if {key:field.get(key) for key in ("selector","label","tag","type")} not in evidence.get("fields",[]):return {"status":"BLOCKED","error":"BROWSER_FORM_FIELD_CHANGED"}
            permission_check()
            return entry["session"].fill_public_form(identity,fields,permission_check)

    def type_local_field(self, session_id: str, label: str, value: str) -> dict:
        """Type into one exact, unique visible editable text field on a local E2E page."""
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            origin = entry.get("allowed_local_origin")
            if not origin or origin_from_url(entry.get("url") or "") != origin:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_SESSION_NOT_ACTIVE"}
            evidence = entry["session"].picker_evidence()
            if evidence.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": str(evidence.get("error") or "LOCAL_E2E_EVIDENCE_FAILED")}
            wanted = " ".join(str(label or "").split())
            candidates = [candidate for candidate in evidence.get("candidates", [])
                          if isinstance(candidate, dict) and candidate.get("kind") == "field"
                          and candidate.get("tag") in {"input", "textarea"}
                          and " ".join(str(candidate.get("text") or "").split()) == wanted]
            if len(candidates) != 1:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_TYPE_TARGET_NOT_UNIQUE"}
            candidate = candidates[0]
            if (candidate.get("tag") == "input"
                    and str((candidate.get("attributes") or {}).get("type", "text")).lower()
                    in {"hidden", "password", "file", "checkbox", "radio", "button", "submit", "reset", "image"}):
                return {"status": "BLOCKED", "error": "LOCAL_E2E_TYPE_INPUT_TYPE_UNSUPPORTED"}
            selectors = candidate.get("selector_candidates")
            if not isinstance(selectors, list) or not selectors:
                return {"status": "BLOCKED", "error": "LOCAL_E2E_TYPE_SELECTOR_UNAVAILABLE"}
            result = entry["session"].type_local_field(str(selectors[0]), wanted, value, origin)
            if result.get("status") == "AVAILABLE":
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
                result["url"] = entry["url"]
            return result

    def extract_recipe(self, session_id: str, record_selector: str, fields: dict, *, limit: int = 100, tool: str | None = None, point: dict | None = None) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].extract_recipe(record_selector, fields, limit=limit, **({"tool": tool, "point": point} if tool else {}))
            if result.get("status") == "AVAILABLE":
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
                result["url"] = entry["url"]
            return result

    def extract_network_json_recipe(self, session_id: str, source: dict, *, limit: int = 100) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            try:
                validated = JsonRecipeSource.model_validate(source)
            except (TypeError, ValueError) as exc:
                return {"status": "BLOCKED", "error": str(exc)[:120]}
            result = entry["session"].network_json_recipe_rows(
                str(entry.get("url") or ""), validated.endpoint_path,
                validated.records_pointer,
                {name: item.pointer for name, item in validated.fields.items()}, limit=limit,
            )
            if result.get("status") == "AVAILABLE":
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
                result["url"] = entry["url"]
            return result

    def probe_recipe_selectors(self, session_id: str, candidates: list[dict], fields: dict) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].probe_recipe_selectors(candidates, fields)
            if result.get("status") == "AVAILABLE":
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
                result["url"] = entry["url"]
            return result

    def extract_next_link(self, session_id: str, selector: str) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = entry["session"].extract_next_link(selector)
            if result.get("status") == "AVAILABLE":
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
                result["current_url"] = entry["url"]
            return result

    def _advance_recipe_session(self, session_id: str, method: str, *args) -> dict:
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            result = getattr(entry["session"], method)(*args)
            action = {"click_recipe_load_more": "load_more",
                      "scroll_for_recipe_page": "scroll"}.get(method)
            if action is not None:
                model_telemetry.record_playwright_action(
                    action, successful=result.get("status") == "AVAILABLE")
            if result.get("status") != "AVAILABLE":
                return {"status": result.get("status", "FAILED"),
                        "error": str(result.get("error") or "BROWSER_RECIPE_NAVIGATION_FAILED")}
            page_text = str(result.get("page") or "")
            resulting_url = _page_value(page_text, "Page URL") or entry["url"]
            if not _same_origin_url(entry["url"], resulting_url):
                return {"status": "BLOCKED", "error": "RECIPE_NAVIGATION_ORIGIN_CHANGED"}
            snapshot = entry["session"].call("browser_snapshot", {})
            if snapshot.get("status") != "AVAILABLE" or not _mcp_text(snapshot):
                return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_FAILED"}
            entry["url"] = resulting_url
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
            return {"status": "AVAILABLE", "url": resulting_url,
                    "clean_markdown": _mcp_text(snapshot)[:50_000], "truncated": len(_mcp_text(snapshot)) > 50_000,
                    "timestamp": time.time(), "provider": "playwright_accessibility"}

    def click_recipe_load_more(self, session_id: str, selector: str) -> dict:
        return self._advance_recipe_session(session_id, "click_recipe_load_more", selector)

    def click_scrape_readonly_control(self, session_id: str, label: str) -> dict:
        """Follow a same-origin link or click a clearly read-only reveal control."""
        wanted = " ".join(str(label or "").split())
        if not wanted or len(wanted) > 160:
            return {"status": "BLOCKED", "error": "SCRAPE_CONTROL_LABEL_INVALID"}
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            current_url = str(entry.get("url") or "")
            if not _same_http_origin(current_url, current_url):
                return {"status": "BLOCKED", "error": "SCRAPE_PAGE_ORIGIN_INVALID"}
            evidence = entry["session"].picker_evidence()
            if evidence.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": str(evidence.get("error") or "SCRAPE_PAGE_EVIDENCE_FAILED")}
            candidates = [item for item in evidence.get("candidates", [])
                          if isinstance(item, dict)
                          and item.get("kind") in {"control", "navigation"}
                          and " ".join(str(item.get("text") or "").split()) == wanted]
            if len(candidates) != 1:
                return {"status": "BLOCKED", "error": "SCRAPE_CONTROL_NOT_UNIQUE"}
            candidate = candidates[0]
            selectors = candidate.get("selector_candidates")
            if not isinstance(selectors, list) or not selectors or not isinstance(selectors[0], str):
                return {"status": "BLOCKED", "error": "SCRAPE_CONTROL_SELECTOR_UNAVAILABLE"}
            tag = str(candidate.get("tag") or "").lower()
            if tag == "a" and candidate.get("kind") == "navigation":
                attributes = candidate.get("attributes") if isinstance(candidate.get("attributes"), dict) else {}
                href = str(attributes.get("href") or "")
                destination = urljoin(current_url, href)
                if not _same_http_origin(current_url, destination):
                    return {"status": "BLOCKED", "error": "SCRAPE_NAVIGATION_LEFT_ORIGIN"}
                destination_parts = urlsplit(destination)
                if ("href_query_keys" not in attributes
                        or not _read_only_scrape_query_keys(str(attributes.get("href_query_keys") or ""))
                        or _SCRAPE_MUTATING_LINK.search(f"{wanted} {destination_parts.path}")):
                    return {"status": "BLOCKED", "error": "SCRAPE_CONTROL_NOT_READ_ONLY"}
                origin = urlsplit(current_url)
                allowed_origin = f"{origin.scheme.lower()}://{origin.netloc.lower()}"
                result = entry["session"].click_local_control(str(selectors[0]), wanted, allowed_origin)
                model_telemetry.record_playwright_action("click", successful=result.get("status") == "AVAILABLE")
                if result.get("status") != "AVAILABLE":
                    return {"status": result.get("status", "FAILED"),
                            "error": str(result.get("error") or "SCRAPE_LINK_CLICK_FAILED")}
                page_text = str(result.get("page") or "")
                resulting_url = _page_value(page_text, "Page URL") or current_url
                if not _same_http_origin(current_url, resulting_url):
                    return {"status": "BLOCKED", "error": "SCRAPE_NAVIGATION_LEFT_ORIGIN"}
                snapshot = entry["session"].call("browser_snapshot", {})
                content = _mcp_text(snapshot) if snapshot.get("status") == "AVAILABLE" else ""
                if not content:
                    return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_FAILED"}
                entry["url"] = resulting_url
                entry["last_activity"] = time.time()
                entry["last_activity_monotonic"] = time.monotonic()
                return {"status": "AVAILABLE", "url": resulting_url,
                        "title": _page_value(page_text, "Page Title"),
                        "clean_markdown": content[:50_000], "truncated": len(content) > 50_000,
                        "timestamp": time.time(), "provider": "playwright_accessibility"}
            if tag != "button" or candidate.get("kind") != "control" or not _READ_ONLY_SCRAPE_BUTTON.fullmatch(wanted):
                return {"status": "BLOCKED", "error": "SCRAPE_CONTROL_NOT_READ_ONLY"}
            result = entry["session"].click_recipe_load_more(str(selectors[0]), wanted)
            model_telemetry.record_playwright_action("load_more", successful=result.get("status") == "AVAILABLE")
            if result.get("status") != "AVAILABLE":
                return {"status": result.get("status", "FAILED"),
                        "error": str(result.get("error") or "SCRAPE_READ_ONLY_CONTROL_FAILED")}
            page_text = str(result.get("page") or "")
            resulting_url = _page_value(page_text, "Page URL") or current_url
            if not _same_http_origin(current_url, resulting_url):
                return {"status": "BLOCKED", "error": "SCRAPE_NAVIGATION_LEFT_ORIGIN"}
            snapshot = entry["session"].call("browser_snapshot", {})
            content = _mcp_text(snapshot) if snapshot.get("status") == "AVAILABLE" else ""
            if not content:
                return {"status": "FAILED", "error": "BROWSER_SNAPSHOT_FAILED"}
            entry["url"] = resulting_url
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
            return {"status": "AVAILABLE", "url": resulting_url,
                    "title": _page_value(page_text, "Page Title"),
                    "clean_markdown": content[:50_000], "truncated": len(content) > 50_000,
                    "timestamp": time.time(), "provider": "playwright_accessibility"}

    def scrape_readonly_control_candidates(self, session_id: str) -> dict:
        """List fresh same-origin links and allowlisted reveal buttons for chat planning."""
        with self._lock:
            entry = self._get_locked(session_id)
            if entry is None:
                return {"status": "NOT_FOUND", "error": "BROWSER_SESSION_NOT_FOUND"}
            current_url = str(entry.get("url") or "")
            if not _same_http_origin(current_url, current_url):
                return {"status": "BLOCKED", "error": "SCRAPE_PAGE_ORIGIN_INVALID"}
            evidence = entry["session"].picker_evidence()
            if evidence.get("status") != "AVAILABLE":
                return {"status": "FAILED", "error": str(evidence.get("error") or "SCRAPE_PAGE_EVIDENCE_FAILED")}
            safe = []
            for item in evidence.get("candidates", []):
                if not isinstance(item, dict):
                    continue
                label = " ".join(str(item.get("text") or "").split())
                tag = str(item.get("tag") or "").lower()
                kind = str(item.get("kind") or "")
                attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
                if not label or len(label) > 160:
                    continue
                if kind == "navigation" and tag == "a":
                    href = str(attributes.get("href") or "")
                    destination = urljoin(current_url, href)
                    parts = urlsplit(destination)
                    if (not href or not _same_http_origin(current_url, destination)
                            or "href_query_keys" not in attributes
                            or not _read_only_scrape_query_keys(str(attributes.get("href_query_keys") or ""))
                            or _SCRAPE_MUTATING_LINK.search(f"{label} {parts.path}")):
                        continue
                elif (kind == "control" and tag == "button"
                      and _READ_ONLY_SCRAPE_BUTTON.fullmatch(label)):
                    pass
                else:
                    continue
                safe.append({"label": label, "kind": kind, "tag": tag})
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
            return {"status": "AVAILABLE", "url": current_url, "candidates": safe,
                    "truncated": bool(evidence.get("truncated")), "timestamp": evidence.get("timestamp", time.time())}

    def scroll_for_recipe_page(self, session_id: str) -> dict:
        return self._advance_recipe_session(session_id, "scroll_for_recipe_page")

    def close(self, session_id: str) -> bool:
        with self._lock:
            entry = self._sessions.pop(session_id, None)
            if entry is None:
                return False
            entry["session"].close()
            profile_id = entry.get("profile_id")
            if profile_id and self._active_profiles.get(profile_id) == session_id:
                self._active_profiles.pop(profile_id, None)
            return True

    def close_all(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            entries, self._sessions = self._sessions, {}
            self._active_profiles.clear()
            for entry in entries.values():
                entry["session"].close()
        if self._sweeper is not None and self._sweeper is not threading.current_thread():
            self._sweeper.join(timeout=1)

    def _get_locked(self, session_id: str) -> dict | None:
        self._expire_locked(time.monotonic())
        entry = self._sessions.get(session_id)
        if entry is not None:
            entry["last_activity"] = time.time()
            entry["last_activity_monotonic"] = time.monotonic()
        return entry

    def _profile_allowed(self, entry):
        if self._session_policy is None: return True
        try: return bool(self._session_policy(entry.get('profile_id')))
        except Exception: return False

    def _expire_locked(self, now: float):
        expired = [key for key, entry in self._sessions.items()
                   if now - entry["last_activity_monotonic"] > self._idle_timeout or not self._profile_allowed(entry)]
        for key in expired:
            entry = self._sessions.pop(key)
            entry["session"].close()
            profile_id = entry.get("profile_id")
            if profile_id and self._active_profiles.get(profile_id) == key:
                self._active_profiles.pop(profile_id, None)

    def _sweep(self):
        while not self._stop.wait(30):
            with self._lock:
                self._expire_locked(time.monotonic())


def _mcp_text(result: dict) -> str:
    response = result.get("response") if isinstance(result, dict) else None
    payload = response.get("result") if isinstance(response, dict) else None
    if not isinstance(payload, dict) or payload.get("isError"):
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(item.get("text", "") for item in content
                     if isinstance(item, dict) and item.get("type") == "text").strip()


def _evaluate_json(result: dict) -> dict:
    text = _mcp_text(result)
    result_text = text.split("\n", 1)[1].strip() if text.startswith("### Result\n") else text
    payload, _consumed = json.JSONDecoder().raw_decode(result_text)
    if isinstance(payload, str):
        payload, _consumed = json.JSONDecoder().raw_decode(payload)
    if not isinstance(payload, dict):
        raise ValueError("evaluation result is not an object")
    return payload


def _page_value(text: str, name: str) -> str:
    prefix = f"- {name}: "
    return next((line[len(prefix):].strip() for line in text.splitlines()
                 if line.startswith(prefix)), "")


def _same_origin_url(left: str, right: str) -> bool:
    first, second = urlsplit(left), urlsplit(right)
    return (first.scheme.lower(), first.hostname.lower() if first.hostname else None, first.port) == (
        second.scheme.lower(), second.hostname.lower() if second.hostname else None, second.port)
