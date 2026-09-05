from __future__ import annotations
from dataclasses import dataclass
from html.parser import HTMLParser
import ipaddress, socket
import sys
import re
import json, os
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener

MAX_BYTES=2_000_000; MAX_CHARS=50_000
MAX_SEARCH_RESULTS=5

def setup_guidance(provider: str | None = None) -> str:
    intro="Web検索を使用すると、検索クエリが選択した外部検索プロバイダへ送信されます。\nAPIキー発行、無料枠・無料クレジット、料金、プラン、利用条件は各プロバイダで確認してください。条件は将来変更される場合があります。OLCR自体はWeb検索料金を請求しません。アカウント作成、APIキー発行、プラン選択、支払いは各プロバイダ上でユーザーが行います。\n"
    brave="Brave Search API\nAPIキー発行: https://api-dashboard.search.brave.com/\n料金・プラン: https://brave.com/search/api/\n設定: /web provider brave\n"
    tavily="Tavily\nAPIキー発行: https://app.tavily.com/\n料金・プラン: https://www.tavily.com/pricing\n設定: /web provider tavily\n"
    if provider=="brave": return intro+brave
    if provider=="tavily": return intro+tavily
    return intro+brave+tavily+"Webを使わない: /web off"
class BrowserProvider:
    available=False
    def fetch(self, url:str): raise RuntimeError('BROWSER_NOT_AVAILABLE')
class _Text(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]
    def handle_data(self,data): self.parts.append(data)
def validate_url(url:str)->str:
    p=urlparse(url)
    if p.scheme not in {'http','https'} or not p.hostname: raise ValueError('UNSUPPORTED_SCHEME')
    for info in socket.getaddrinfo(p.hostname,None):
        if ipaddress.ip_address(info[4][0]).is_private or ipaddress.ip_address(info[4][0]).is_loopback or ipaddress.ip_address(info[4][0]).is_link_local: raise ValueError('PRIVATE_ADDRESS_REJECTED')
    return url
class _SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)
def fetch(url:str)->dict:
    print('WEB_REQUEST_START', file=sys.stderr, flush=True); validate_url(url); print('WEB_AUTHORIZATION_VALIDATED=YES WEB_NETWORK_POLICY=PASS', file=sys.stderr, flush=True)
    req=Request(url,headers={'User-Agent':'OLCR/0.4.7'}); print('WEB_HTTP_START', file=sys.stderr, flush=True)
    with build_opener(_SafeRedirects()).open(req,timeout=10) as r:
        data=r.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES: raise ValueError('RESPONSE_TOO_LARGE')
        text=data.decode(r.headers.get_content_charset() or 'utf-8',errors='replace')
        if 'html' in (r.headers.get_content_type() or ''):
            parser=_Text(); parser.feed(text); text=' '.join(' '.join(parser.parts).split())
        print(f'WEB_HTTP_RESPONSE=YES WEB_HTTP_STATUS={r.status} WEB_RESPONSE_BYTES={len(data)} WEB_EXTRACT_END=YES WEB_CONTEXT_CHARS={len(text)} WEB_CONTEXT_VALID=YES', file=sys.stderr, flush=True)
        return {'requested_url':url,'final_url':r.geturl(),'content_type':r.headers.get_content_type(),'http_status':r.status,'provider':'http','text':text[:MAX_CHARS],'truncated':len(text)>MAX_CHARS,'warnings':[]}

def search(query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """Small public HTML search adapter; results remain untrusted candidates."""
    if not query or limit < 1: return []
    limit=min(limit, MAX_SEARCH_RESULTS)
    endpoint="https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    print("WEB_SEARCH_REQUEST_STARTED=YES", file=sys.stderr, flush=True)
    validate_url(endpoint)
    req=Request(endpoint, headers={"User-Agent":"OLCR/0.4.7"})
    try:
        with build_opener(_SafeRedirects()).open(req, timeout=10) as response:
            data=response.read(MAX_BYTES+1); status=response.status; content_type=response.headers.get_content_type()
    except Exception:
        print("WEB_SEARCH_PROVIDER_STATUS=NETWORK_ERROR", file=sys.stderr, flush=True); raise
    if len(data)>MAX_BYTES: raise ValueError("SEARCH_RESPONSE_TOO_LARGE")
    html=data.decode("utf-8", errors="replace")
    parser=_SearchParser(); parser.feed(html)
    results=parser.results[:limit]
    classification,_=_classify_search_html(html, len(results))
    provider_status="OK" if results else "INTERSTITIAL" if classification=="INTERSTITIAL" else "PARSER_MISMATCH" if classification=="PARSER_MISMATCH" else "PARSE_ZERO"
    print(f"WEB_SEARCH_FINAL_URL={endpoint} WEB_SEARCH_HTTP_STATUS={status} WEB_SEARCH_CONTENT_TYPE={content_type} WEB_SEARCH_RESPONSE_BYTES={len(data)} WEB_SEARCH_PARSE_RESULT_COUNT={len(results)} WEB_SEARCH_PROVIDER_STATUS={provider_status}", file=sys.stderr, flush=True)
    return results

def brave_search(query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    key=os.environ.get("OLCR_WEB_BRAVE_API_KEY", "").strip() or os.environ.get("OLCR_WEB_SEARCH_API_KEY", "").strip()
    print(f"WEB_SEARCH_PROVIDER=brave WEB_SEARCH_KEY_CONFIGURED={'YES' if key else 'NO'}", file=sys.stderr, flush=True)
    if not key: raise RuntimeError("WEB_SEARCH_PROVIDER_NOT_READY")
    query=" ".join(query.split())[:500]
    req=Request("https://api.search.brave.com/res/v1/web/search?" + urlencode({"q":query,"count":min(limit,MAX_SEARCH_RESULTS)}), headers={"Accept":"application/json","X-Subscription-Token":key,"User-Agent":"OLCR/0.4.7"})
    print("WEB_SEARCH_REQUEST_STARTED=YES", file=sys.stderr, flush=True)
    try:
        with build_opener().open(req, timeout=10) as response:
            payload=json.loads(response.read(MAX_BYTES+1)); status=response.status
    except Exception:
        print("WEB_SEARCH_PROVIDER_STATUS=HTTP_ERROR", file=sys.stderr, flush=True); raise
    rows=[]
    for rank,item in enumerate((payload.get("web",{}).get("results",[]) if isinstance(payload,dict) else [])[:MAX_SEARCH_RESULTS],1):
        if isinstance(item,dict) and isinstance(item.get("url"),str) and isinstance(item.get("title"),str):
            rows.append({"title":item["title"][:300],"url":item["url"],"snippet":str(item.get("description", ""))[:1000],"rank":rank,"provider":"brave"})
    print(f"WEB_SEARCH_HTTP_STATUS={status} WEB_SEARCH_RESULT_COUNT={len(rows)} WEB_SEARCH_PROVIDER_STATUS={'OK' if rows else 'NO_RESULTS'}", file=sys.stderr, flush=True)
    return rows

def tavily_search(query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    key=os.environ.get("OLCR_WEB_TAVILY_API_KEY", "").strip()
    print(f"WEB_SEARCH_PROVIDER=tavily WEB_SEARCH_KEY_CONFIGURED={'YES' if key else 'NO'}", file=sys.stderr, flush=True)
    if not key: raise RuntimeError("WEB_SEARCH_PROVIDER_NOT_READY")
    payload=json.dumps({"api_key":key,"query":" ".join(query.split())[:500],"max_results":min(limit,MAX_SEARCH_RESULTS)}).encode()
    req=Request("https://api.tavily.com/search", data=payload, method="POST", headers={"Accept":"application/json","Content-Type":"application/json","User-Agent":"OLCR/0.4.7"})
    print("WEB_SEARCH_REQUEST_STARTED=YES", file=sys.stderr, flush=True)
    try:
        with build_opener().open(req, timeout=10) as response:
            data=json.loads(response.read(MAX_BYTES+1)); status=response.status
    except Exception:
        print("WEB_SEARCH_PROVIDER_STATUS=HTTP_ERROR", file=sys.stderr, flush=True); raise
    rows=[]
    for rank,item in enumerate((data.get("results",[]) if isinstance(data,dict) else [])[:MAX_SEARCH_RESULTS],1):
        if isinstance(item,dict) and isinstance(item.get("url"),str): rows.append({"title":str(item.get("title", ""))[:300],"url":item["url"],"snippet":str(item.get("content", ""))[:1000],"rank":rank,"provider":"tavily"})
    print(f"WEB_SEARCH_HTTP_STATUS={status} WEB_SEARCH_RESULT_COUNT={len(rows)} WEB_SEARCH_PROVIDER_STATUS={'OK' if rows else 'NO_RESULTS'}", file=sys.stderr, flush=True)
    return rows

class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.results=[]; self._current=None; self._capture=False
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=="a" and "result__a" in attrs.get("class", ""):
            self._current={"title":"", "url":attrs.get("href", ""), "snippet":"", "rank":len(self.results)+1}; self._capture=True
        elif tag in {"a","div"} and self._current and "result__snippet" in attrs.get("class", ""):
            self._capture=True
    def handle_data(self, data):
        if self._current and self._capture:
            self._current["title"] += " " + data.strip()
    def handle_endtag(self, tag):
        if tag=="a" and self._current:
            if self._current["title"].strip(): self.results.append(self._current)
            self._current=None; self._capture=False

def _classify_search_html(html: str, result_count: int) -> tuple[str, str]:
    title_match=re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I|re.S)
    title=re.sub(r"\s+", " ", title_match.group(1)).strip()[:120] if title_match else ""
    selector_count=len(re.findall(r'class=[\"\'][^\"\']*result__a', html or "", re.I))
    links=len(re.findall(r'<a\b[^>]+href=', html or "", re.I))
    lower=(html or "").lower()
    interstitial=any(x in lower for x in ("captcha", "unusual traffic", "automated queries", "challenge", "bot verification"))
    if result_count: classification="RESULT_PAGE"
    elif interstitial: classification="INTERSTITIAL"
    elif selector_count or links: classification="PARSER_MISMATCH"
    elif title: classification="UNEXPECTED_HTML"
    else: classification="UNKNOWN"
    print(f"WEB_SEARCH_PAGE_TITLE_PRESENT={'YES' if title else 'NO'} WEB_SEARCH_PAGE_TITLE={title or 'NOT_AVAILABLE'} WEB_SEARCH_RESULT_SELECTOR_MATCH_COUNT={selector_count} WEB_SEARCH_LINK_CANDIDATE_COUNT={links} WEB_SEARCH_INTERSTITIAL_DETECTED={'YES' if interstitial else 'NO'} WEB_SEARCH_RESPONSE_CLASS={classification}", file=sys.stderr, flush=True)
    return classification, title
