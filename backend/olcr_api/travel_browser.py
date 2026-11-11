"""Host-owned, evidence-bound actions for a user's selected booking browser."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


_SENSITIVE = re.compile(
    r"password|passwd|passcode|secret|token|api[ _-]?key|one[ _-]?time|otp|\bpin\b|csrf|cvv|cvc|"
    r"security[ _-]?code|credit|debit|card|bank|billing|payment|account|username|login|e.?mail|phone|mobile|telephone|contact|"
    r"パスワード|暗証|認証|確認コード|ワンタイム|秘密|カード|支払|決済|銀行|口座|ログイン|メール|電話|連絡先|氏名|名前|搭乗者|宿泊者|予約者|旅客名|full.?name|passenger.?name|guest.?name|traveler.?name", re.I)
_COMMIT = re.compile(r"予約を?(?:確定|する|申し込む|完了)|予約確定|購入を?確定|購入する|支払う|決済する|申し込む|book\s*now|reserve\s*now|purchase|pay\s*now|confirm\s*booking", re.I)
_CANCEL = re.compile(r"キャンセル|取消|取り消し|中止|戻る|やめる|cancel|back|withdraw|refund", re.I)
_SEARCH = re.compile(r"^(?:検索|検索する|空席を検索|空席照会|料金を検索|search|find|check availability)$", re.I)
_SAFE_TYPES = {"text", "search", "email", "tel", "url", "date", "time", "datetime-local", "number", "textarea", "select"}
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_LONG_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_PRICE_OR_DATE = re.compile(r"(?:¥|￥|JPY|USD|EUR|GBP|\$|€|£)\s*[\d,.]+|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日", re.I)
_TERMS_WORDS = re.compile(r"キャンセル|取消|返金|refund|cancellation|non.?refundable|税|手数料|合計|total|料金|fare|fee", re.I)
_PARTY_TERM = re.compile(r"(?:大人|子供|小児|人数|people|persons?|adults?)\s*[:：]?\s*\d+\s*(?:名|人|people|persons?)?|\b\d+\s*(?:adults?|people|persons?)\b", re.I)
_TIME_TERM = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
_TERM_SENSITIVE = re.compile(r"password|passcode|\botp\b|security.?code|cvv|cvc|card|credit|debit|bank|account|username|login|e.?mail|phone|mobile|telephone|contact|パスワード|暗証|認証|確認コード|ワンタイム|カード|銀行|口座|ログイン|メール|電話|連絡先|氏名|名前|ご利用者|お客様|予約者|搭乗者|宿泊者|旅客名",re.I)

TRAVEL_PAGE_SCAN = r'''() => {
 const sensitive=new RegExp(__SENSITIVE__,'i');
 const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&!el.closest('[aria-hidden="true"], [inert]');};
 const labelOf=el=>{const ids=el.getAttribute('aria-labelledby');const ref=ids?ids.split(/\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' '):'';const associated=el.labels?[...el.labels].map(x=>x.innerText||x.textContent||'').join(' '):'';return (el.getAttribute('aria-label')||ref||associated||el.getAttribute('placeholder')||el.innerText||el.textContent||'').replace(/\s+/g,' ').trim().slice(0,160);};
 const pathOf=el=>{const parts=[];for(let n=el;n&&n.nodeType===1;n=n.parentElement){let p=n.tagName.toLowerCase();if(n.parentElement){const peers=[...n.parentElement.children].filter(x=>x.tagName===n.tagName);p+=':nth-of-type('+(peers.indexOf(n)+1)+')';}parts.unshift(p);if(parts.length>30)return '';}return parts.join(' > ');};
 const controls=[];let challenge=/(?:^|\/)(?:login|sign-in|payment|billing|oauth|authorize)(?:\/|$)/i.test(location.pathname);
 for(const el of document.querySelectorAll('input,textarea,select')){if(!visible(el)||el.disabled||el.readOnly)continue;const tag=el.tagName.toLowerCase(),type=tag==='textarea'?'textarea':tag==='select'?'select':(el.type||'text').toLowerCase(),label=labelOf(el),meta=[label,el.name,el.id,el.autocomplete,el.getAttribute('inputmode')].join(' ');if(type==='password'||sensitive.test(meta)){challenge=true;continue;}if(!label||label.length>160||!['text','search','email','tel','url','date','time','datetime-local','number','textarea','select'].includes(type))continue;const form=el.form;if(form&&[...form.querySelectorAll('input')].some(x=>visible(x)&&(x.type==='password'||sensitive.test([x.name,x.id,labelOf(x),x.autocomplete].join(' '))))){challenge=true;continue;}const options=tag==='select'?[...el.options].filter(x=>!x.disabled).slice(0,100).map(x=>(x.label||x.textContent||'').replace(/\s+/g,' ').trim().slice(0,120)).filter(Boolean):[];controls.push({selector:pathOf(el),label,tag,type,options});}
 for(const el of document.querySelectorAll('button,[role=button]')){if(!visible(el)||el.disabled)continue;const label=labelOf(el);if(!label||label.length>160||sensitive.test(label))continue;if((/予約を?(?:確定|する|申し込む|完了)|予約確定|購入を?確定|購入する|支払う|決済する|申し込む|book\s*now|reserve\s*now|purchase|pay\s*now|confirm\s*booking/i.test(label)&&!/キャンセル|取消|取り消し|中止|戻る|やめる|cancel|back|withdraw|refund/i.test(label))||/^(検索|検索する|空席を検索|空席照会|料金を検索|search|find|check availability)$/i.test(label))controls.push({selector:pathOf(el),label,tag:'button',type:(el.type||'button').toLowerCase(),options:[]});}
 const lines=(document.body?.innerText||'').split(/\n+/).map(x=>x.replace(/\s+/g,' ').trim()).filter(Boolean);const terms=lines.filter(x=>x.length<=400&&(/(?:¥|￥|JPY|USD|EUR|GBP|\$|€|£)\s*[\d,.]+|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日|(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)/i.test(x)||/キャンセル|取消|返金|refund|cancellation|non.?refundable|税|手数料|合計|total|料金|fare|fee|(?:大人|子供|小児|人数|people|persons?|adults?)\s*[:：]?\s*\d+\s*(?:名|人|people|persons?)?|\b\d+\s*(?:adults?|people|persons?)\b/i.test(x))).slice(0,40);
 const text=lines.join(' ').slice(0,12000);const confirmed=/(?:予約|booking|reservation).{0,24}(?:完了|確定|confirmed|complete|success)|(?:完了|確定|confirmed|complete|success).{0,24}(?:予約|booking|reservation)/i.test(text);const refLine=confirmed?lines.find(x=>/(?:予約番号|確認番号|reference|confirmation number|booking id)/i.test(x))||'':'';const reference=refLine.match(/[A-Z0-9-]{5,32}/i)?.[0]||null;
 return JSON.stringify({url:location.href,origin:location.origin,document_id:performance.timeOrigin,controls:challenge?[]:controls,requires_user_action:challenge,terms,receipt:confirmed?{status:'provider_acceptance_displayed',reference}:null});
}'''.replace('__SENSITIVE__', json.dumps(_SENSITIVE.pattern))

TRAVEL_PAGE_ACTION = r'''() => {
 const cfg=__CONFIG__;const sensitive=new RegExp(__SENSITIVE__,'i');
 const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&!el.closest('[aria-hidden="true"], [inert]');};
 const labelOf=el=>{const ids=el.getAttribute('aria-labelledby');const ref=ids?ids.split(/\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' '):'';const associated=el.labels?[...el.labels].map(x=>x.innerText||x.textContent||'').join(' '):'';return (el.getAttribute('aria-label')||ref||associated||el.getAttribute('placeholder')||el.innerText||el.textContent||'').replace(/\s+/g,' ').trim().slice(0,160);};
 if(location.href!==cfg.url||location.origin!==cfg.origin||performance.timeOrigin!==cfg.document_id)return JSON.stringify({ok:false,reason:'PAGE_CHANGED'});
 const matches=[...document.querySelectorAll(cfg.control.selector)];if(matches.length!==1)return JSON.stringify({ok:false,reason:'TARGET_NOT_UNIQUE'});const el=matches[0];if(!visible(el)||labelOf(el)!==cfg.control.label||el.tagName.toLowerCase()!==cfg.control.tag)return JSON.stringify({ok:false,reason:'TARGET_CHANGED'});
 if(sensitive.test([labelOf(el),el.name,el.id,el.autocomplete].join(' '))||el.type==='password')return JSON.stringify({ok:false,reason:'SENSITIVE_CONTROL'});
 if(cfg.kind==='fill'){if(!['input','textarea'].includes(el.tagName.toLowerCase())||!['text','search','email','tel','url','date','time','datetime-local','number',''].includes((el.type||'').toLowerCase())||!cfg.value)return JSON.stringify({ok:false,reason:'FILL_NOT_ALLOWED'});const proto=el.tagName.toLowerCase()==='textarea'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;if(!setter)return JSON.stringify({ok:false,reason:'SETTER_UNAVAILABLE'});setter.call(el,cfg.value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return JSON.stringify({ok:el.value===cfg.value});}
 if(cfg.kind==='select'){if(el.tagName.toLowerCase()!=='select'||!cfg.value)return JSON.stringify({ok:false,reason:'SELECT_NOT_ALLOWED'});const options=[...el.options].filter(x=>!x.disabled&&(x.label||x.textContent||'').replace(/\s+/g,' ').trim()===cfg.value);if(options.length!==1)return JSON.stringify({ok:false,reason:'OPTION_CHANGED'});el.value=options[0].value;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return JSON.stringify({ok:el.value===options[0].value});}
 if(cfg.kind==='click'||cfg.kind==='commit'){const label=labelOf(el);const isCommit=/予約を?(?:確定|する|申し込む|完了)|予約確定|購入を?確定|購入する|支払う|決済する|申し込む|book\s*now|reserve\s*now|purchase|pay\s*now|confirm\s*booking/i.test(label)&&!/キャンセル|取消|取り消し|中止|戻る|やめる|cancel|back|withdraw|refund/i.test(label);if(cfg.kind==='commit'&&!isCommit)return JSON.stringify({ok:false,reason:'COMMIT_CONTROL_CHANGED'});if(cfg.kind==='click'&&(!/^(検索|検索する|空席を検索|空席照会|料金を検索|search|find|check availability)$/i.test(label)||isCommit))return JSON.stringify({ok:false,reason:'CLICK_NOT_ALLOWED'});if(el.tagName.toLowerCase()!=='button'&&el.getAttribute('role')!=='button')return JSON.stringify({ok:false,reason:'BUTTON_REQUIRED'});if(cfg.kind==='click'&&el.type==='submit'&&(!el.form||el.form.method.toLowerCase()!=='get'||new URL(el.form.action,location.href).origin!==location.origin))return JSON.stringify({ok:false,reason:'SEARCH_SUBMIT_NOT_SAFE'});el.click();return JSON.stringify({ok:true,clicked:true});}
 return JSON.stringify({ok:false,reason:'ACTION_NOT_ALLOWED'});
}'''.replace('__SENSITIVE__', json.dumps(_SENSITIVE.pattern))


class BookingControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    kind: Literal["field", "select", "search", "commit", "link"]
    value_type: str = ""
    options: list[str] = Field(default_factory=list, max_length=100)
    commit: bool = False


class BookingPageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["travel_booking_page"] = "travel_booking_page"
    evidence_id: str
    session_id: str
    process_id: int
    url: str
    origin: str
    document_id: float
    captured_at: float
    controls: list[BookingControl] = Field(default_factory=list, max_length=100)
    terms: list[str] = Field(default_factory=list, max_length=40)
    requires_user_action: bool = False


class BookingBrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["navigate", "fill", "select", "click"]
    evidence_id: str
    control_id: str | None = None
    url: str | None = None
    value: str | None = Field(default=None, max_length=1000)


@dataclass
class _StoredEvidence:
    evidence: BookingPageEvidence
    controls: dict[str, dict]
    terms_fingerprint: str
    control_fingerprint: str
    expires_at: float


@dataclass
class _CommitGrant:
    evidence_id: str
    control_id: str
    review_fingerprint: str
    expires_at: float


@dataclass(frozen=True)
class BookingCommitResult:
    status: Literal["provider_accepted", "uncertain"]
    provider_reference: str | None
    evidence: BookingPageEvidence | None


def _origin(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise ValueError("TRAVEL_BROWSER_PUBLIC_HTTPS_REQUIRED")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or not host or "." not in host or parts.username or parts.password
                or parts.port not in (None, 443) or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        return "https://" + host
    except (TypeError, ValueError):
        raise ValueError("TRAVEL_BROWSER_PUBLIC_HTTPS_REQUIRED") from None


def _safe_terms(values) -> list[str]:
    safe = []
    for raw in values if isinstance(values, list) else []:
        line = " ".join(str(raw).split())[:400]
        if not line or _TERM_SENSITIVE.search(line):
            continue
        line = _EMAIL.sub("[redacted]", line)
        if not re.search(r"\b\d{4}-\d{2}-\d{2}\b",line):
            line = _PHONE.sub("[redacted]", line)
        line = _LONG_NUMBER.sub("[redacted]", line)
        if _PRICE_OR_DATE.search(line) or _TERMS_WORDS.search(line) or _PARTY_TERM.search(line) or _TIME_TERM.search(line):
            safe.append(line)
    return safe[:40]


def _commit_label(label: str) -> bool:
    value=label or ""
    return bool(_COMMIT.search(value) and not _CANCEL.search(value))


class TravelBrowserActions:
    """Keeps exact current-page evidence private and allows only typed actions."""
    def __init__(self, browser_sessions, *, evidence_ttl: int = 180, review_ttl: int = 300, permission_check=None):
        self.browser_sessions = browser_sessions
        self.permission_check = permission_check or (lambda: True)
        self.evidence_ttl = max(30, min(int(evidence_ttl), 600))
        self.review_ttl = max(30, min(int(review_ttl), 600))
        self._lock = threading.RLock()
        self._evidence: dict[str, _StoredEvidence] = {}
        self._grants: dict[str, _CommitGrant] = {}

    @staticmethod
    def _identity(payload: dict) -> tuple:
        return (payload.get("session_id"), payload.get("process_id"), payload.get("url"),
                payload.get("origin"), payload.get("document_id"))

    def _normalize(self, session_id: str, expected_origin: str, payload: dict) -> _StoredEvidence:
        if not isinstance(payload, dict) or payload.get("status") != "AVAILABLE":
            raise ValueError("TRAVEL_BROWSER_EVIDENCE_UNAVAILABLE")
        url = payload.get("url")
        origin = _origin(url)
        if origin != expected_origin or payload.get("origin") != origin:
            raise ValueError("TRAVEL_BROWSER_ORIGIN_CHANGED")
        if (payload.get("session_id") != session_id or type(payload.get("process_id")) is not int
                or payload["process_id"] <= 0 or type(payload.get("document_id")) not in (int, float)):
            raise ValueError("TRAVEL_BROWSER_PAGE_IDENTITY_INVALID")
        if not isinstance(payload.get("controls"), list) or len(payload["controls"]) > 200:
            raise ValueError("TRAVEL_BROWSER_CONTROLS_INVALID")

        requires_user_action = bool(payload.get("requires_user_action"))
        controls: dict[str, dict] = {}
        exposed: list[BookingControl] = []
        for index, raw in enumerate(payload["controls"]):
            if not isinstance(raw, dict):
                continue
            label = " ".join(str(raw.get("label") or "").split())[:160]
            tag = str(raw.get("tag") or "").lower()
            value_type = str(raw.get("type") or "").lower()
            if not label or _SENSITIVE.search(label) or _SENSITIVE.search(value_type):
                requires_user_action = requires_user_action or bool(label or value_type)
                continue
            if tag in {"input", "textarea", "select"} and value_type not in _SAFE_TYPES:
                continue
            if tag not in {"input", "textarea", "select", "button", "a"}:
                continue
            selector = raw.get("selector")
            if not isinstance(selector, str) or not selector or len(selector) > 512 or any(c in selector for c in "{};\r\n\x00"):
                continue
            options = raw.get("options", [])
            if not isinstance(options, list):
                options = []
            options = [" ".join(str(option).split())[:120] for option in options if str(option).strip()][:100]
            is_commit = _commit_label(label)
            if tag in {"input", "textarea", "select"}:
                kind = "select" if tag == "select" else "field"
            elif is_commit:
                kind = "commit"
            elif tag == "a":
                kind = "link"
            elif _SEARCH.fullmatch(label):
                kind = "search"
            else:
                continue
            control_id = f"c{index+1}"
            item = {"selector": selector, "label": label, "tag": tag, "type": value_type,
                    "options": options, "kind": kind}
            controls[control_id] = item
            exposed.append(BookingControl(id=control_id, label=label, kind=kind,
                                          value_type=value_type, options=options, commit=is_commit))

        if requires_user_action:
            controls = {}
            exposed = []
        terms = _safe_terms(payload.get("terms", []))
        terms_fingerprint = hashlib.sha256("\n".join(terms).encode()).hexdigest()
        control_fingerprint = hashlib.sha256(
            "\n".join(f"{k}:{v['label']}:{v['kind']}:{v['type']}:{','.join(v['options'])}" for k,v in controls.items()).encode()
        ).hexdigest()
        evidence = BookingPageEvidence(evidence_id=uuid4().hex, session_id=session_id,
            process_id=payload["process_id"], url=url, origin=origin,
            document_id=float(payload["document_id"]), captured_at=time.time(),
            controls=exposed, terms=terms, requires_user_action=requires_user_action)
        stored = _StoredEvidence(evidence, controls, terms_fingerprint, control_fingerprint,
                                 time.time()+self.evidence_ttl)
        with self._lock:
            self._prune_locked()
            self._evidence[evidence.evidence_id] = stored
        return stored

    def _prune_locked(self):
        now=time.time()
        self._evidence={key:value for key,value in self._evidence.items() if value.expires_at>now}
        self._grants={key:value for key,value in self._grants.items() if value.expires_at>now}

    def inspect(self, session_id: str, expected_origin: str) -> BookingPageEvidence:
        self.permission_check()
        origin = _origin(expected_origin)
        if origin != expected_origin:
            raise ValueError("TRAVEL_BROWSER_ORIGIN_INVALID")
        payload = self.browser_sessions.inspect_travel_page(session_id)
        return self._normalize(session_id, origin, payload).evidence

    def _saved(self, evidence: BookingPageEvidence) -> _StoredEvidence:
        with self._lock:
            self._prune_locked()
            saved=self._evidence.get(evidence.evidence_id)
        if not saved or saved.evidence.model_dump() != evidence.model_dump():
            raise ValueError("TRAVEL_BROWSER_EVIDENCE_STALE")
        return saved

    def _fresh(self, saved: _StoredEvidence) -> _StoredEvidence:
        payload=self.browser_sessions.inspect_travel_page(saved.evidence.session_id)
        fresh=self._normalize(saved.evidence.session_id,saved.evidence.origin,payload)
        if self._identity(payload) != (saved.evidence.session_id,saved.evidence.process_id,saved.evidence.url,
                                       saved.evidence.origin,saved.evidence.document_id):
            raise ValueError("TRAVEL_BROWSER_EVIDENCE_STALE")
        if fresh.control_fingerprint != saved.control_fingerprint:
            raise ValueError("TRAVEL_BROWSER_CONTROLS_CHANGED")
        return fresh

    def apply(self, session_id: str, evidence: BookingPageEvidence, action: BookingBrowserAction,
              *, permission_check) -> "TravelBrowserActionResult":
        saved=self._saved(evidence)
        if session_id != evidence.session_id or action.evidence_id != evidence.evidence_id:
            raise ValueError("TRAVEL_BROWSER_EVIDENCE_STALE")
        permission_check()
        fresh=self._fresh(saved)
        if fresh.terms_fingerprint != saved.terms_fingerprint:
            raise ValueError("TRAVEL_BROWSER_TERMS_CHANGED")
        if action.kind == "navigate":
            target=_origin(action.url or "")
            if target != evidence.origin:
                raise ValueError("TRAVEL_BROWSER_ORIGIN_CHANGED")
            control={"label":"same-origin navigation","selector":"","tag":"a","type":""}
        else:
            control=saved.controls.get(action.control_id or "")
            if not control:
                raise ValueError("TRAVEL_BROWSER_CONTROL_NOT_FOUND")
            if action.kind == "fill" and (control["tag"] not in {"input","textarea"} or not action.value):
                raise ValueError("TRAVEL_BROWSER_FILL_INVALID")
            if action.kind == "select" and (control["tag"]!="select" or action.value not in control["options"]):
                raise ValueError("TRAVEL_BROWSER_SELECT_INVALID")
            if action.kind == "click" and (control["kind"] != "search" or control["kind"] == "commit"):
                raise ValueError("TRAVEL_BROWSER_CLICK_NOT_ALLOWED")
            if action.value and (_SENSITIVE.search(action.value) or _EMAIL.search(action.value)
                                 or _LONG_NUMBER.search(action.value)):
                raise ValueError("TRAVEL_BROWSER_SENSITIVE_VALUE_BLOCKED")
        permission_check()
        if action.kind == "navigate":
            result=self.browser_sessions.navigate(session_id,action.url)
        else:
            result=self.browser_sessions.apply_travel_action(session_id,
                {"session_id":evidence.session_id,"process_id":evidence.process_id,"url":evidence.url,
                 "origin":evidence.origin,"document_id":evidence.document_id},control,action.kind,
                action.value,permission_check=permission_check)
        if not isinstance(result,dict) or result.get("status")!="AVAILABLE":
            raise ValueError("TRAVEL_BROWSER_ACTION_FAILED")
        next_payload=self.browser_sessions.inspect_travel_page(session_id)
        next_evidence=self._normalize(session_id,evidence.origin,next_payload).evidence
        return TravelBrowserActionResult(applied=True,evidence=next_evidence)

    def authorize_commit(self, evidence_id: str, control_id: str, review_fingerprint: str) -> str:
        with self._lock:
            self._prune_locked()
            saved=self._evidence.get(evidence_id)
            if not saved or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}",str(review_fingerprint or "")):
                raise ValueError("TRAVEL_BROWSER_REVIEW_INVALID")
            control=saved.controls.get(control_id)
            if not control or control["kind"]!="commit" or not _commit_label(control["label"]):
                raise ValueError("TRAVEL_BROWSER_COMMIT_CONTROL_INVALID")
            token=uuid4().hex
            self._grants[token]=_CommitGrant(evidence_id,control_id,review_fingerprint,time.time()+self.review_ttl)
            return token

    def commit(self, session_id: str, evidence: BookingPageEvidence, control_id: str,
               confirmation_id: str, review_fingerprint: str, *, permission_check) -> BookingCommitResult:
        saved=self._saved(evidence)
        with self._lock:
            self._prune_locked()
            grant=self._grants.get(confirmation_id)
        if not grant or (grant.evidence_id,grant.control_id,grant.review_fingerprint)!=(evidence.evidence_id,control_id,review_fingerprint):
            raise ValueError("TRAVEL_BROWSER_CONFIRMATION_INVALID_OR_ALREADY_USED")
        if session_id!=evidence.session_id or saved.controls.get(control_id,{}).get("kind")!="commit":
            raise ValueError("TRAVEL_BROWSER_COMMIT_CONTROL_INVALID")
        permission_check()
        fresh=self._fresh(saved)
        if fresh.terms_fingerprint!=saved.terms_fingerprint:
            raise ValueError("TRAVEL_BROWSER_TERMS_CHANGED")
        with self._lock:
            if self._grants.pop(confirmation_id,None) is None:
                raise ValueError("TRAVEL_BROWSER_CONFIRMATION_INVALID_OR_ALREADY_USED")
        try:
            permission_check()
            result=self.browser_sessions.apply_travel_action(session_id,
                {"session_id":evidence.session_id,"process_id":evidence.process_id,"url":evidence.url,
                 "origin":evidence.origin,"document_id":evidence.document_id},saved.controls[control_id],
                "commit",None,permission_check=permission_check)
        except Exception:
            return BookingCommitResult("uncertain",None,None)
        reference=result.get("reference") if isinstance(result,dict) else None
        accepted=isinstance(result,dict) and result.get("status")=="AVAILABLE" and result.get("accepted") is True
        if not accepted:
            return BookingCommitResult("uncertain",None,None)
        try:
            payload=self.browser_sessions.inspect_travel_page(session_id)
            next_evidence=self._normalize(session_id,evidence.origin,payload).evidence
        except Exception:
            next_evidence=None
        return BookingCommitResult("provider_accepted",str(reference)[:160] if reference else None,next_evidence)


class TravelBrowserActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applied: bool
    evidence: BookingPageEvidence

