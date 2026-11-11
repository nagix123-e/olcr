"""Durable exact-review state machine for explicit travel reservations."""
from __future__ import annotations

from contextlib import closing
from datetime import date
import hashlib
import hmac
import json
import re
import time
from typing import Literal, Protocol
from types import SimpleNamespace
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .permissions import capability_enabled
from .travel_browser import BookingBrowserAction, BookingPageEvidence, TravelBrowserActions


Category = Literal["transport", "lodging", "food", "experience", "rental"]


class BookingSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Category
    origin: str = Field(default="", max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)
    travelers: int = Field(ge=1, le=100)
    start_time: str = Field(default="", max_length=20)
    end_time: str = Field(default="", max_length=20)
    preferences: str = Field(default="", max_length=1000)
    browser_session_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def valid_dates(self):
        for value in (self.start_date, self.end_date):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("TRAVEL_BOOKING_DATE_INVALID")
        if self.end_date < self.start_date:
            raise ValueError("TRAVEL_BOOKING_DATE_RANGE_INVALID")
        return self


class ProviderOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=100)
    external_ref: str = Field(min_length=1, max_length=200)
    category: Category
    title: str = Field(min_length=1, max_length=300)
    price_amount: str = Field(min_length=1, max_length=80)
    currency: str = Field(min_length=3, max_length=8)
    price_kind: Literal["live", "estimate", "unknown"]
    fees_amount: str = Field(default="0", max_length=80)
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)
    party_count: int = Field(ge=1, le=100)
    start_time: str = Field(default="", max_length=80)
    end_time: str = Field(default="", max_length=80)
    cancellation_terms: str = Field(default="", max_length=1000)
    evidence_id: str = Field(min_length=1, max_length=200)
    expires_at: float
    browser_session_id: str | None = Field(default=None, max_length=100)


class BookingOffer(ProviderOffer):
    id: str
    conversation_id: str


class ReservationDetails(BaseModel):
    """Only trip facts; contact, identity and payment details stay in the browser."""
    model_config = ConfigDict(extra="forbid")
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)
    travelers: int = Field(ge=1, le=100)
    start_time: str = Field(default="", max_length=20)
    end_time: str = Field(default="", max_length=20)
    preferences: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def safe_preferences(self):
        if re.search(r"password|passcode|\botp\b|security.?code|cvv|cvc|カード|暗証|確認コード|ワンタイム",self.preferences,re.I):
            raise ValueError("TRAVEL_BOOKING_SENSITIVE_DETAILS_BLOCKED")
        if re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b|(?<!\d)(?:\d[ -]?){13,19}(?!\d)",self.preferences):
            raise ValueError("TRAVEL_BOOKING_SENSITIVE_DETAILS_BLOCKED")
        return self


class ProviderReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=100)
    external_ref: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    category: Category
    price_amount: str = Field(min_length=1, max_length=80)
    currency: str = Field(min_length=3, max_length=8)
    fees_amount: str = Field(default="0", max_length=80)
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)
    party_count: int = Field(ge=1, le=100)
    start_time: str = Field(default="", max_length=80)
    end_time: str = Field(default="", max_length=80)
    cancellation_terms: str = Field(min_length=1, max_length=1000)
    evidence_fingerprint: str = Field(min_length=1, max_length=200)
    expires_at: float
    browser_session_id: str | None = Field(default=None, max_length=100)


class BookingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    conversation_id: str
    offer_id: str
    provider_id: str
    title: str
    category: Category
    price_amount: str
    currency: str
    fees_amount: str
    start_date: str
    end_date: str
    party_count: int
    start_time: str
    end_time: str
    cancellation_terms: str
    price_kind: Literal["live", "estimate", "unknown"]
    status: Literal["review", "committing", "confirmed", "failed", "uncertain", "expired", "cancelled"]
    expires_at: float
    confirmation_id: str
    binding_fingerprint: str
    provider_reference: str | None = None
    browser_session_id: str | None = None


class ProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "failed", "uncertain"]
    provider_reference: str | None = Field(default=None, max_length=160)
    message: str = Field(default="", max_length=500)


class BookingOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["review", "committing", "confirmed", "failed", "uncertain", "expired", "cancelled"]
    review: BookingReview | None = None
    provider_reference: str | None = None
    message: str = ""


class TravelBookingProvider(Protocol):
    provider_id: str
    def search(self, request: BookingSearchRequest) -> list[ProviderOffer]: ...
    def prepare(self, offer: ProviderOffer, details: ReservationDetails) -> ProviderReview: ...
    def commit(self, review: ProviderReview, confirmation_id: str) -> ProviderResult: ...
    def reconcile(self, attempt: dict) -> ProviderResult: ...


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _review_fingerprint(value: ProviderReview) -> str:
    return hashlib.sha256(_json(value.model_dump(exclude={"expires_at"})).encode()).hexdigest()


class TravelBookingService:
    def __init__(self, db, providers: dict[str, TravelBookingProvider] | None = None,
                 *, permission_check=None, review_seconds: int = 300):
        self.db=db
        self.providers=dict(providers or {})
        self.permission_check=permission_check or (lambda: True)
        self.review_seconds=max(60,min(int(review_seconds),600))
        with closing(db.connect()) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS travel_booking_offers(
                id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,provider_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,expires_at REAL NOT NULL,status TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS travel_booking_reviews(
                id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,offer_id TEXT NOT NULL,provider_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,binding_fingerprint TEXT NOT NULL,confirmation_id TEXT NOT NULL,
                expires_at REAL NOT NULL,status TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS travel_booking_attempts(
                review_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,provider_id TEXT NOT NULL,
                status TEXT NOT NULL,provider_reference TEXT,result_json TEXT NOT NULL,
                created_at REAL NOT NULL,updated_at REAL NOT NULL)""")

    def _permission(self):
        self.permission_check()

    def search(self, conversation_id: str, request: BookingSearchRequest) -> list[BookingOffer]:
        self._permission()
        if not conversation_id or len(conversation_id)>160:
            raise ValueError("TRAVEL_BOOKING_CONVERSATION_INVALID")
        offers=[];now=time.time()
        for provider_id,provider in self.providers.items():
            if getattr(provider,"provider_id",provider_id)!=provider_id:
                continue
            try: found=provider.search(request)
            except Exception: continue
            for raw in found[:20] if isinstance(found,list) else []:
                try: item=raw if isinstance(raw,ProviderOffer) else ProviderOffer.model_validate(raw)
                except Exception: continue
                if (item.provider_id!=provider_id or item.category!=request.category
                        or item.start_date!=request.start_date or item.end_date!=request.end_date
                        or item.party_count!=request.travelers
                        or (request.start_time and item.start_time!=request.start_time)
                        or (request.end_time and item.end_time!=request.end_time) or item.expires_at<=now):
                    continue
                offer=BookingOffer(id=uuid4().hex,conversation_id=conversation_id,**item.model_dump())
                payload=_json(offer.model_dump())
                with closing(self.db.connect()) as con,con:
                    con.execute("INSERT INTO travel_booking_offers VALUES(?,?,?,?,?,?)",
                                (offer.id,conversation_id,provider_id,payload,offer.expires_at,"available"))
                offers.append(offer)
        return offers

    def prepare(self, conversation_id: str, offer_id: str, details: ReservationDetails) -> BookingReview:
        self._permission()
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT provider_id,payload_json,expires_at,status FROM travel_booking_offers WHERE id=? AND conversation_id=?",
                            (offer_id,conversation_id)).fetchone()
        if not row or row[3]!="available": raise ValueError("TRAVEL_BOOKING_OFFER_NOT_FOUND")
        if row[2]<=time.time(): raise ValueError("TRAVEL_BOOKING_OFFER_EXPIRED")
        offer=BookingOffer.model_validate(json.loads(row[1]))
        if offer.price_kind!="live": raise ValueError("TRAVEL_BOOKING_LIVE_PRICE_REQUIRED")
        if (details.start_date,details.end_date,details.travelers,details.start_time,details.end_time)!=(
                offer.start_date,offer.end_date,offer.party_count,offer.start_time,offer.end_time):
            raise ValueError("TRAVEL_BOOKING_DETAILS_CHANGED")
        provider=self.providers.get(row[0])
        if provider is None:raise ValueError("TRAVEL_BOOKING_PROVIDER_UNAVAILABLE")
        prepared=provider.prepare(ProviderOffer.model_validate(offer.model_dump(exclude={"id","conversation_id"})),details)
        if not isinstance(prepared,ProviderReview):prepared=ProviderReview.model_validate(prepared)
        if prepared.expires_at<=time.time():
            raise ValueError("TRAVEL_BOOKING_OFFER_EXPIRED")
        if (prepared.provider_id!=offer.provider_id or prepared.external_ref!=offer.external_ref
                or prepared.category!=offer.category or prepared.start_date!=details.start_date
                or prepared.end_date!=details.end_date or prepared.party_count!=details.travelers
                or prepared.start_time!=details.start_time or prepared.end_time!=details.end_time
                ):
            raise ValueError("TRAVEL_BOOKING_REVIEW_BINDING_INVALID")
        expiry=min(prepared.expires_at,offer.expires_at,time.time()+self.review_seconds)
        token=uuid4().hex
        rid=uuid4().hex
        review=BookingReview(id=rid,conversation_id=conversation_id,offer_id=offer_id,
            provider_id=prepared.provider_id,title=prepared.title,category=prepared.category,
            price_amount=prepared.price_amount,currency=prepared.currency,fees_amount=prepared.fees_amount,
            start_date=prepared.start_date,end_date=prepared.end_date,party_count=prepared.party_count,
            start_time=prepared.start_time,end_time=prepared.end_time,cancellation_terms=prepared.cancellation_terms,
            price_kind=offer.price_kind,status="review",expires_at=expiry,confirmation_id=token,
            binding_fingerprint=_review_fingerprint(prepared),browser_session_id=prepared.browser_session_id)
        with closing(self.db.connect()) as con,con:
            con.execute("INSERT INTO travel_booking_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                (rid,conversation_id,offer_id,prepared.provider_id,_json(review.model_dump()),
                 review.binding_fingerprint,token,expiry,"review"))
        return review

    def get_review(self, conversation_id: str, review_id: str) -> BookingReview:
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT r.payload_json,r.status,a.provider_reference FROM travel_booking_reviews r "
                            "LEFT JOIN travel_booking_attempts a ON a.review_id=r.id WHERE r.id=? AND r.conversation_id=?",
                            (review_id,conversation_id)).fetchone()
        if not row:raise ValueError("TRAVEL_BOOKING_REVIEW_NOT_FOUND")
        data=json.loads(row[0]);data["status"]=row[1]
        if row[2]:data["provider_reference"]=row[2]
        return BookingReview.model_validate(data)

    def latest_review(self, conversation_id: str, *, status: str | None = None) -> BookingReview | None:
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT id FROM travel_booking_reviews WHERE conversation_id=? "
                            +("AND status=? " if status else "")+"ORDER BY rowid DESC LIMIT 1",
                            (conversation_id,status) if status else (conversation_id,)).fetchone()
        return self.get_review(conversation_id,row[0]) if row else None

    def cancel_review(self, conversation_id: str, review_id: str) -> BookingOutcome:
        """Invalidate an uncommitted review without contacting the provider."""
        with closing(self.db.connect()) as con,con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT payload_json,status FROM travel_booking_reviews WHERE id=? AND conversation_id=?",
                            (review_id,conversation_id)).fetchone()
            if not row:raise ValueError("TRAVEL_BOOKING_REVIEW_NOT_FOUND")
            if row[1]=="review":
                payload=json.loads(row[0]);payload["status"]="cancelled"
                con.execute("UPDATE travel_booking_reviews SET status='cancelled',payload_json=? "
                            "WHERE id=? AND conversation_id=? AND status='review'",
                            (_json(payload),review_id,conversation_id))
        return self._outcome(conversation_id,review_id)

    def latest_offer(self, conversation_id: str, *, category: str | None = None) -> BookingOffer | None:
        with closing(self.db.connect()) as con:
            query=("SELECT payload_json FROM travel_booking_offers WHERE conversation_id=? AND status='available' "
                   +("AND json_extract(payload_json,'$.category')=? " if category else "")+"ORDER BY rowid DESC LIMIT 1")
            params=(conversation_id,category) if category else (conversation_id,)
            row=con.execute(query,params).fetchone()
        return BookingOffer.model_validate(json.loads(row[0])) if row else None

    def get_offer(self, conversation_id: str, offer_id: str) -> BookingOffer:
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT payload_json,expires_at,status FROM travel_booking_offers WHERE id=? AND conversation_id=?",
                             (offer_id,conversation_id)).fetchone()
        if not row or row[2]!="available":raise ValueError("TRAVEL_BOOKING_OFFER_NOT_FOUND")
        if row[1]<=time.time():raise ValueError("TRAVEL_BOOKING_OFFER_EXPIRED")
        return BookingOffer.model_validate(json.loads(row[0]))

    def _outcome(self, conversation_id: str, review_id: str) -> BookingOutcome:
        review=self.get_review(conversation_id,review_id)
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT result_json FROM travel_booking_attempts WHERE review_id=? AND conversation_id=?",
                            (review_id,conversation_id)).fetchone()
        result=json.loads(row[0]) if row else {}
        messages={"review":"予約内容を確認してください。","committing":"予約先からの応答を確認しています。",
            "confirmed":"予約先が予約を確定しました。","failed":"予約先で予約を完了できませんでした。",
            "uncertain":"予約結果を確認できません。予約サイトで状態を確認してください。再送信は行っていません。",
            "expired":"確認期限が切れました。最新条件でもう一度確認してください。",
            "cancelled":"予約確認を取り消しました。"}
        return BookingOutcome(status=review.status,review=review,provider_reference=review.provider_reference,
                              message=result.get("message") or messages[review.status])

    def confirm(self, conversation_id: str, review_id: str, confirmation_id: str) -> BookingOutcome:
        self._permission()
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT payload_json,confirmation_id,expires_at,status,offer_id,provider_id "
                            "FROM travel_booking_reviews WHERE id=? AND conversation_id=?",
                            (review_id,conversation_id)).fetchone()
            attempt=con.execute("SELECT status FROM travel_booking_attempts WHERE review_id=? AND conversation_id=?",
                                (review_id,conversation_id)).fetchone()
        if not row:raise ValueError("TRAVEL_BOOKING_REVIEW_NOT_FOUND")
        if attempt:
            if attempt[0]=="committing":
                self._save_outcome(conversation_id,review_id,"uncertain",None,"処理結果不明。重複予約を避けるため再送信しません。")
            return self._outcome(conversation_id,review_id)
        if row[3]!="review":return self._outcome(conversation_id,review_id)
        if not hmac.compare_digest(str(row[1]),str(confirmation_id or "")):
            raise ValueError("TRAVEL_BOOKING_CONFIRMATION_INVALID")
        if row[2]<=time.time():
            self._set_review_status(conversation_id,review_id,"expired")
            return self._outcome(conversation_id,review_id)
        review=BookingReview.model_validate(json.loads(row[0]))
        offer_row=None
        with closing(self.db.connect()) as con:
            offer_row=con.execute("SELECT payload_json,expires_at,status FROM travel_booking_offers WHERE id=? AND conversation_id=?",
                                  (row[4],conversation_id)).fetchone()
        if not offer_row or offer_row[1]<=time.time() or offer_row[2]!="available":
            self._set_review_status(conversation_id,review_id,"expired")
            return self._outcome(conversation_id,review_id)
        provider=self.providers.get(row[5])
        if provider is None:raise ValueError("TRAVEL_BOOKING_PROVIDER_UNAVAILABLE")
        offer=BookingOffer.model_validate(json.loads(offer_row[0]))
        details=ReservationDetails(start_date=review.start_date,end_date=review.end_date,travelers=review.party_count,
                                   start_time=review.start_time,end_time=review.end_time)
        fresh=provider.prepare(ProviderOffer.model_validate(offer.model_dump(exclude={"id","conversation_id"})),details)
        if not isinstance(fresh,ProviderReview):fresh=ProviderReview.model_validate(fresh)
        if fresh.expires_at<=time.time() or _review_fingerprint(fresh)!=review.binding_fingerprint:
            self._set_review_status(conversation_id,review_id,"expired")
            raise ValueError("TRAVEL_BOOKING_OFFER_CHANGED")
        # Atomic single-use transition happens before the remote side effect. A
        # crash after this point is reconciled as uncertain, never replayed.
        now=time.time()
        with closing(self.db.connect()) as con,con:
            con.execute("BEGIN IMMEDIATE")
            changed=con.execute("UPDATE travel_booking_reviews SET status='committing' WHERE id=? AND conversation_id=? AND status='review' AND expires_at>?",
                                (review_id,conversation_id,now)).rowcount
            if changed!=1:
                existing=con.execute("SELECT status FROM travel_booking_attempts WHERE review_id=?",(review_id,)).fetchone()
                if existing:return self._outcome(conversation_id,review_id)
                raise ValueError("TRAVEL_BOOKING_REVIEW_EXPIRED_OR_USED")
            data=review.model_dump();data["status"]="committing"
            con.execute("UPDATE travel_booking_reviews SET payload_json=? WHERE id=?",(_json(data),review_id))
            con.execute("INSERT INTO travel_booking_attempts VALUES(?,?,?,?,?,?,?,?)",
                (review_id,conversation_id,row[5],"committing",None,"{}",now,now))
        try:
            self._permission()
            result=provider.commit(fresh,confirmation_id)
            if not isinstance(result,ProviderResult):result=ProviderResult.model_validate(result)
        except Exception:
            self._save_outcome(conversation_id,review_id,"uncertain",None,
                "予約先の応答を確認できません。予約サイトで状態を確認してください。再送信は行っていません。")
            return self._outcome(conversation_id,review_id)
        self._save_outcome(conversation_id,review_id,result.status,result.provider_reference,result.message)
        return self._outcome(conversation_id,review_id)

    def _set_review_status(self, conversation_id: str, review_id: str, status: str):
        with closing(self.db.connect()) as con,con:
            row=con.execute("SELECT payload_json FROM travel_booking_reviews WHERE id=? AND conversation_id=?",(review_id,conversation_id)).fetchone()
            if not row:return
            data=json.loads(row[0]);data["status"]=status
            con.execute("UPDATE travel_booking_reviews SET status=?,payload_json=? WHERE id=? AND conversation_id=?",
                        (status,_json(data),review_id,conversation_id))

    def _save_outcome(self, conversation_id: str, review_id: str, status: str,
                      provider_reference: str | None, message: str):
        now=time.time()
        with closing(self.db.connect()) as con,con:
            row=con.execute("SELECT payload_json FROM travel_booking_reviews WHERE id=? AND conversation_id=?",(review_id,conversation_id)).fetchone()
            if not row:return
            data=json.loads(row[0]);data["status"]=status;data["provider_reference"]=provider_reference
            result={"message":str(message or "")[:500]}
            con.execute("UPDATE travel_booking_reviews SET status=?,payload_json=? WHERE id=? AND conversation_id=?",
                        (status,_json(data),review_id,conversation_id))
            con.execute("INSERT INTO travel_booking_attempts VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(review_id) DO UPDATE SET status=excluded.status,provider_reference=excluded.provider_reference,result_json=excluded.result_json,updated_at=excluded.updated_at",
                        (review_id,conversation_id,data["provider_id"],status,provider_reference,_json(result),now,now))

    def reconcile(self, conversation_id: str, review_id: str) -> BookingOutcome:
        self._permission()
        current=self._outcome(conversation_id,review_id)
        if current.status!="uncertain":return current
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT provider_id,status,provider_reference FROM travel_booking_attempts WHERE review_id=? AND conversation_id=?",
                            (review_id,conversation_id)).fetchone()
        provider=self.providers.get(row[0]) if row else None
        if not provider:return current
        try:
            result=provider.reconcile({"review_id":review_id,"status":row[1],"provider_reference":row[2]})
            if not isinstance(result,ProviderResult):result=ProviderResult.model_validate(result)
        except Exception:return current
        if result.status=="uncertain":return current
        self._save_outcome(conversation_id,review_id,result.status,result.provider_reference,result.message)
        return self._outcome(conversation_id,review_id)


_BROWSER_PRICE = re.compile(r"(?:(JPY|USD|EUR|GBP)\s*([\d,]+)|([¥￥$€£])\s*([\d,]+)|([\d,]+)\s*(円))",re.I)
_BROWSER_CANCEL = re.compile(r"キャンセル|取消|返金|refund|cancellation|non.?refundable",re.I)


def _browser_price(terms: list[str]) -> tuple[str,str,str] | None:
    totals=[]
    fee_values=[]
    fees_explicitly_zero=False
    for line in terms:
        if re.search(r"合計|総額|total|fare|料金",line,re.I):
            match=_BROWSER_PRICE.search(line)
            if match:
                if match.group(1):currency,amount=match.group(1).upper(),match.group(2)
                elif match.group(3):currency={"¥":"JPY","￥":"JPY","$":"USD","€":"EUR","£":"GBP"}[match.group(3)];amount=match.group(4)
                else:currency,amount="JPY",match.group(5)
                totals.append((amount.replace(",",""),currency))
        if re.search(r"手数料|追加料金|service fee|surcharge|\bfee\b|\btax\b|税",line,re.I):
            fee=re.search(r"(?:手数料|追加料金|service fee|surcharge|fee|tax|税)\s*[:：]?\s*(?:¥|￥|JPY|\$|€|£)?\s*([\d,]+)",line,re.I)
            if fee:fee_values.append(fee.group(1).replace(",",""))
            if re.search(r"(?:手数料|追加料金)\s*(?:なし|無料)|no (?:extra )?fees?",line,re.I):fees_explicitly_zero=True
    if len(totals)!=1:return None
    fees=fee_values[0] if len(fee_values)==1 else ("0" if fees_explicitly_zero else "未内訳")
    amount,currency=totals[0]
    return amount,currency,fees


def _browser_evidence_fingerprint(evidence: BookingPageEvidence) -> str:
    value={"session_id":evidence.session_id,"process_id":evidence.process_id,"url":evidence.url,
           "origin":evidence.origin,"document_id":evidence.document_id,"terms":evidence.terms}
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ManagedBrowserTravelProvider:
    """Use one existing persistent, public HTTPS browser session as the fallback provider."""
    provider_id="managed-browser"

    def __init__(self,browser_sessions,*,permission_check=None):
        self.browser_sessions=browser_sessions
        self.permission_check=permission_check or (lambda:True)
        self.actions=TravelBrowserActions(browser_sessions,permission_check=self.permission_check)
        self.last_handoff=""

    def _session(self,request:BookingSearchRequest|None=None):
        sessions=[item for item in self.browser_sessions.list() if item.get("profile_id") and item.get("url","").startswith("https://")]
        requested=getattr(request,"browser_session_id",None) if request else None
        if requested:sessions=[item for item in sessions if item.get("session_id")==requested]
        if len(sessions)!=1:
            self.last_handoff="予約サイトにログイン済みのOLCRブラウザーを1つだけ開き、検索結果または予約内容の確認画面を表示してください。"
            return None
        return sessions[0]

    @staticmethod
    def _fill_value(label:str,request:BookingSearchRequest):
        if re.search(r"時刻|時間|出発時刻|到着時刻|pickup time|drop.?off time|time",label,re.I):
            if re.search(r"返却|到着|drop.?off|return",label,re.I):return request.end_time or None
            return request.start_time or None
        if re.search(r"返却日|返却|帰着日|return date|drop.?off date",label,re.I):return request.end_date
        if re.search(r"受取日|出発日|pickup date|pick.?up date",label,re.I):return request.start_date
        if re.search(r"出発地|乗車駅|出発駅|from|origin",label,re.I):return request.origin or None
        if re.search(r"目的地|到着地|到着駅|行き先|destination|to",label,re.I):return request.destination
        if re.search(r"チェックイン|出発日|乗車日|利用日|開始日|date|check.?in",label,re.I):return request.start_date
        if re.search(r"チェックアウト|終了日|帰着日|return date|check.?out",label,re.I):return request.end_date
        if re.search(r"人数|大人|乗客|宿泊者|traveler|passenger|adult|guest",label,re.I):return str(request.travelers)
        return None

    def _prepare_page(self,request:BookingSearchRequest):
        session=self._session(request)
        if not session:return None,None
        url=session["url"]
        from .travel_browser import _origin
        origin=_origin(url)
        evidence=self.actions.inspect(session["session_id"],origin)
        if evidence.requires_user_action:
            self.last_handoff="予約サイトがログイン、本人確認、または支払い入力を求めています。OLCRのブラウザー上でご自身で対応してください。"
            return session,evidence
        # Fill only labels whose meaning is explicit; never enter identity or payment data.
        for control in list(evidence.controls):
            value=self._fill_value(control.label,request)
            if value is None or control.commit:continue
            if control.kind=="select":
                option=next((item for item in control.options if re.search(r"(?:^|\D)"+re.escape(value)+r"(?:\D|$)",item)),None)
                if option is None:continue
                action=BookingBrowserAction(kind="select",evidence_id=evidence.evidence_id,
                                            control_id=control.id,value=option)
            else:
                if control.kind!="field":continue
                action=BookingBrowserAction(kind="fill",evidence_id=evidence.evidence_id,
                                            control_id=control.id,value=value)
            try:evidence=self.actions.apply(session["session_id"],evidence,action,permission_check=self.permission_check).evidence
            except ValueError:continue
        search=next((item for item in evidence.controls if item.kind=="search"),None)
        if search:
            try:
                evidence=self.actions.apply(session["session_id"],evidence,
                    BookingBrowserAction(kind="click",evidence_id=evidence.evidence_id,control_id=search.id),
                    permission_check=self.permission_check).evidence
            except ValueError:pass
        return session,evidence

    @staticmethod
    def _exact_terms(terms:list[str],request:BookingSearchRequest):
        text="\n".join(terms)
        date_tokens={request.start_date,request.end_date}
        if not all(token in text for token in date_tokens):return None
        if request.start_time and request.start_time not in text:return None
        if request.end_time and request.end_time not in text:return None
        party=re.search(r"(?:大人|子供|小児|人数|people|persons?|adults?)\s*[:：]?\s*"+str(request.travelers)+r"\s*(?:名|人|people|persons?)?|\b"+str(request.travelers)+r"\s*(?:名|人|adults?|people|persons?)\b",text,re.I)
        if not party:return None
        price=_browser_price(terms)
        cancel=next((line for line in terms if _BROWSER_CANCEL.search(line)),None)
        if not price or not cancel:return None
        return price,cancel

    def search(self,request:BookingSearchRequest)->list[ProviderOffer]:
        self.permission_check()
        session,evidence=self._prepare_page(request)
        if not session or not evidence or evidence.requires_user_action:
            return []
        exact=self._exact_terms(evidence.terms,request)
        if not exact:
            self.last_handoff="安全に予約候補として扱うための日付・人数・税込合計・取消条件が同じ画面にそろっていません。OLCRブラウザーで条件を選び、合計金額と取消条件が見える画面を表示してください。"
            return []
        (amount,currency,fees),cancellation=exact
        fingerprint=_browser_evidence_fingerprint(evidence)
        self.last_handoff=""
        return [ProviderOffer(provider_id=self.provider_id,external_ref=fingerprint,
            category=request.category,title=f"{request.destination}の検索結果（ブラウザーで確認）",
            price_amount=amount,currency=currency,price_kind="live",fees_amount=fees,
            start_date=request.start_date,end_date=request.end_date,party_count=request.travelers,
            start_time=request.start_time,end_time=request.end_time,
            cancellation_terms=cancellation,evidence_id=fingerprint,expires_at=min(time.time()+180,time.time()+300),
            browser_session_id=session["session_id"])]

    def _current(self,offer:ProviderOffer):
        if not offer.browser_session_id:raise ValueError("TRAVEL_BROWSER_SESSION_MISSING")
        item=next((x for x in self.browser_sessions.list() if x.get("session_id")==offer.browser_session_id),None)
        if not item or not item.get("url","").startswith("https://"):raise ValueError("TRAVEL_BROWSER_SESSION_MISSING")
        from .travel_browser import _origin
        evidence=self.actions.inspect(offer.browser_session_id,_origin(item["url"]))
        if evidence.requires_user_action:raise ValueError("TRAVEL_BROWSER_USER_HANDOFF_REQUIRED")
        if _browser_evidence_fingerprint(evidence)!=offer.evidence_id:raise ValueError("TRAVEL_BOOKING_OFFER_CHANGED")
        return evidence

    def prepare(self,offer:ProviderOffer,details:ReservationDetails)->ProviderReview:
        self.permission_check()
        evidence=self._current(offer)
        matching=[control for control in evidence.controls if control.commit]
        if len(matching)!=1:raise ValueError("TRAVEL_BROWSER_FINAL_REVIEW_CONTROL_MISSING")
        request=SimpleNamespace(start_date=details.start_date,end_date=details.end_date,travelers=details.travelers,
                                start_time=details.start_time,end_time=details.end_time)
        exact=self._exact_terms(evidence.terms,request)
        if not exact:raise ValueError("TRAVEL_BROWSER_EXACT_TERMS_MISSING")
        (amount,currency,fees),cancellation=exact
        if (amount,currency,fees)!=(offer.price_amount,offer.currency,offer.fees_amount) or cancellation!=offer.cancellation_terms:
            raise ValueError("TRAVEL_BOOKING_OFFER_CHANGED")
        return ProviderReview(provider_id=self.provider_id,external_ref=offer.external_ref,title=offer.title,
            category=offer.category,price_amount=amount,currency=currency,fees_amount=fees,
            start_date=details.start_date,end_date=details.end_date,party_count=details.travelers,
            start_time=details.start_time,end_time=details.end_time,
            cancellation_terms=cancellation,evidence_fingerprint=_browser_evidence_fingerprint(evidence),
            expires_at=min(offer.expires_at,time.time()+180),browser_session_id=offer.browser_session_id)

    def commit(self,review:ProviderReview,confirmation_id:str)->ProviderResult:
        self.permission_check()
        offer=ProviderOffer(provider_id=self.provider_id,external_ref=review.external_ref,category=review.category,
            title=review.title,price_amount=review.price_amount,currency=review.currency,price_kind="live",
            fees_amount=review.fees_amount,start_date=review.start_date,end_date=review.end_date,
            party_count=review.party_count,cancellation_terms=review.cancellation_terms,
            evidence_id=review.evidence_fingerprint,expires_at=review.expires_at,
            browser_session_id=review.browser_session_id)
        try:
            evidence=self._current(offer)
            commit_controls=[control for control in evidence.controls if control.commit]
            if len(commit_controls)!=1:return ProviderResult(status="uncertain",message="確認操作を一意に識別できません。")
            control=commit_controls[0]
            authorization=self.actions.authorize_commit(evidence.evidence_id,control.id,confirmation_id)
            result=self.actions.commit(evidence.session_id,evidence,control.id,authorization,confirmation_id,
                                       permission_check=self.permission_check)
        except Exception:
            return ProviderResult(status="uncertain",message="予約先の結果を安全に確認できません。サイト上で予約状態を確認してください。")
        if result.status=="provider_accepted":
            return ProviderResult(status="confirmed",provider_reference=result.provider_reference,
                                  message="予約先の確認画面に予約完了が表示されました。")
        return ProviderResult(status="uncertain",message="予約結果が不明です。サイト上で予約状態を確認してください。再送信は行いません。")

    def reconcile(self,attempt:dict)->ProviderResult:
        return ProviderResult(status="uncertain",message="予約サイトでの手動確認が必要です。")

