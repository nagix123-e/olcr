from __future__ import annotations
from dataclasses import dataclass
from html.parser import HTMLParser
import codecs
import http.client
import ipaddress, socket
import ssl
import sys
import re
import json, os
import zlib
from urllib.parse import urlparse, urlencode, urljoin
from urllib.request import (Request, urlopen, HTTPRedirectHandler, HTTPHandler, HTTPSHandler,
                            ProxyHandler, build_opener)

MAX_BYTES=2_000_000; MAX_CHARS=50_000
DEFAULT_SEARCH_RESULTS=10
BRAVE_WEB_PAGE_SIZE=20
BRAVE_WEB_MAX_PAGES=10
BRAVE_NEWS_PAGE_SIZE=50
BRAVE_NEWS_MAX_PAGES=10
TAVILY_MAX_RESULTS=20
RUNTIME_PROVIDER_KEYS: dict[str, str] = {}

def requested_result_count(query: str, default: int = DEFAULT_SEARCH_RESULTS) -> int:
    """Read an explicit requested count; the default is not a maximum."""
    match=re.search(r"(?:最大|上位|トップ|top|limit(?:\s+to)?|first)\s*(\d+)",query,re.I)
    if not match:
        match=re.search(r"(?<!\d)(\d+)\s*(?:件|個|人|本|冊|隻|記事|結果|results?|articles?)(?!\d)",query,re.I)
    return max(1,int(match.group(1))) if match else default

def provider_key(provider: str) -> str:
    if provider == "brave": return RUNTIME_PROVIDER_KEYS.get("brave", "").strip() or os.environ.get("OLCR_WEB_BRAVE_API_KEY", "").strip() or os.environ.get("OLCR_WEB_SEARCH_API_KEY", "").strip()
    if provider == "tavily": return RUNTIME_PROVIDER_KEYS.get("tavily", "").strip() or os.environ.get("OLCR_WEB_TAVILY_API_KEY", "").strip()
    return ""

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
    """Bounded semantic HTML text extraction using only the standard library."""
    BLOCKS={"address","article","blockquote","br","dd","div","dl","dt","figcaption","h1","h2","h3","h4","h5","h6","li","main","ol","p","pre","section","table","td","th","tr","ul"}
    IGNORED={"script","style","noscript","svg","template","iframe","title","nav","footer","aside"}
    VOID={"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}
    CHROME_HINTS={"nav","navigation","site-nav","site-navigation","footer","site-footer","sidebar","breadcrumb","breadcrumbs"}
    def __init__(self,base_url:str=""):
        super().__init__(convert_charrefs=True); self.base_url=base_url; self.parts=[]; self.title_parts=[]
        self.description=""; self._ignored=0; self._title=False; self._main_depth=0; self._main_parts=[]
        self._article_depth=0; self._article_parts=[]; self._links=[]; self._hidden_depth=0; self._element_stack=[]
    @staticmethod
    def _is_hidden(attrs):
        if "hidden" in attrs or (attrs.get("aria-hidden") or "").strip().casefold()=="true":return True
        style=attrs.get("style") or ""
        return bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important)?\s*(?:;|$)",style,re.I))
    @classmethod
    def _is_chrome(cls,tag,attrs,main_depth,article_depth):
        if tag in cls.IGNORED or (tag=="header" and not main_depth and not article_depth):return True
        if (attrs.get("role") or "").strip().casefold() in {"navigation","contentinfo","complementary"}:return True
        tokens=set((attrs.get("id") or "").casefold().split())|set((attrs.get("class") or "").casefold().split())
        return bool(tokens & cls.CHROME_HINTS)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        hidden_root=tag not in self.VOID and self._is_hidden(attrs)
        if hidden_root:self._hidden_depth+=1
        chrome_root=self._is_chrome(tag,attrs,self._main_depth,self._article_depth)
        if chrome_root:self._ignored+=1
        if tag not in self.VOID:self._element_stack.append((tag,hidden_root,chrome_root))
        if tag=="title":self._title=True
        description_name=(attrs.get("name") or "").casefold()
        description_property=(attrs.get("property") or "").casefold()
        if tag=="meta" and (description_name=="description" or description_property in {"og:description","twitter:description"}) and not self.description:
            self.description=(attrs.get("content") or "").strip()
        if tag=="main" or (tag=="article" and self._main_depth==0):
            self._main_depth+=1
        if tag=="article":self._article_depth+=1
        if not self._ignored and not self._hidden_depth:
            if tag in self.BLOCKS:self._append("\n")
            if tag in {"h1","h2","h3","h4","h5","h6"}:self._append("#"*int(tag[1])+" ")
            if tag=="a":
                target=urljoin(self.base_url,attrs.get("href",""))
                safe=target if urlparse(target).scheme in {"http","https"} else ""
                self._links.append(safe)
                if safe:self._append("[")
    def handle_endtag(self,tag):
        if tag=="title":self._title=False
        if tag=="main" or (tag=="article" and self._main_depth>0):self._main_depth=max(0,self._main_depth-1)
        if tag=="article":self._article_depth=max(0,self._article_depth-1)
        if tag=="a" and self._links:
            target=self._links.pop()
            if target:self._append("]("+target+")")
        if not self._ignored and not self._hidden_depth and tag in self.BLOCKS:self._append("\n")
        for index in range(len(self._element_stack)-1,-1,-1):
            if self._element_stack[index][0]==tag:
                removed=self._element_stack[index:]
                self._hidden_depth=max(0,self._hidden_depth-sum(1 for _,hidden_root,_ in removed if hidden_root))
                self._ignored=max(0,self._ignored-sum(1 for _,_,chrome_root in removed if chrome_root))
                del self._element_stack[index:]
                break
    def handle_data(self,data):
        if self._title and not self._hidden_depth:self.title_parts.append(data)
        if not self._ignored and not self._hidden_depth and not (self._links and not self._links[-1]):self._append(data)
    def _append(self,value):
        self.parts.append(value)
        if self._main_depth:self._main_parts.append(value)
        if self._article_depth:self._article_parts.append(value)
    @staticmethod
    def _clean(parts):
        lines=[re.sub(r"[ \t\xa0]+"," ",line).strip() for line in "".join(parts).splitlines()]
        return "\n".join(line for index,line in enumerate(lines) if line or (index and lines[index-1]))
    def result(self):
        preferred=self._main_parts or self._article_parts or self.parts
        return {"title":" ".join(" ".join(self.title_parts).split()),"description":self.description,
                "clean_markdown":self._clean(preferred)}
def validate_url(url:str)->str:
    _resolve_public_addresses(url)
    return url

def _resolve_public_addresses(url:str):
    p=urlparse(url)
    if p.scheme not in {'http','https'} or not p.hostname: raise ValueError('UNSUPPORTED_SCHEME')
    if p.username is not None or p.password is not None: raise ValueError('URL_CREDENTIALS_REJECTED')
    try:
        port=p.port or (443 if p.scheme=='https' else 80)
        addresses=socket.getaddrinfo(p.hostname,port,type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError('HOST_RESOLUTION_FAILED') from exc
    if not addresses: raise ValueError('HOST_RESOLUTION_EMPTY')
    for info in addresses:
        try: address=ipaddress.ip_address(info[4][0].split('%',1)[0])
        except ValueError as exc: raise ValueError('HOST_RESOLUTION_INVALID') from exc
        if not address.is_global: raise ValueError('PRIVATE_ADDRESS_REJECTED')
    return tuple(info[4] for info in addresses)

class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self,host,*args,addresses=(),**kwargs):
        super().__init__(host,*args,**kwargs); self._olcr_addresses=addresses
    def connect(self):
        last_error=None
        for address in self._olcr_addresses:
            sock=None
            try:
                family=socket.AF_INET6 if len(address)>2 else socket.AF_INET
                # getaddrinfo's sockaddr contains the exact validated IPv4/IPv6 target.
                sock=socket.socket(family,socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                if self.source_address:sock.bind(self.source_address)
                sock.connect(address)
                self.sock=sock
                if self._tunnel_host:self._tunnel()
                return
            except OSError as exc:
                last_error=exc
                if sock:
                    try:sock.close()
                    except OSError:pass
        if last_error:raise last_error
        raise OSError("NO_VALIDATED_ADDRESS")

class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def __init__(self,host,*args,context=None,check_hostname=None,addresses=(),**kwargs):
        super().__init__(host,*args,addresses=addresses,**kwargs)
        self._olcr_context=context or ssl.create_default_context()
        if check_hostname is not None:self._olcr_context.check_hostname=check_hostname
    def connect(self):
        super().connect()
        self.sock=self._olcr_context.wrap_socket(self.sock,server_hostname=self.host)

class _PinnedHTTPHandler(HTTPHandler):
    def http_open(self,req):
        addresses=_resolve_public_addresses(req.full_url)
        def connection(host,timeout=socket._GLOBAL_DEFAULT_TIMEOUT,**kwargs):
            return _PinnedHTTPConnection(host,timeout=timeout,addresses=addresses,**kwargs)
        return self.do_open(connection,req)

class _PinnedHTTPSHandler(HTTPSHandler):
    def https_open(self,req):
        addresses=_resolve_public_addresses(req.full_url)
        def connection(host,timeout=socket._GLOBAL_DEFAULT_TIMEOUT,context=None,check_hostname=None,**kwargs):
            return _PinnedHTTPSConnection(host,timeout=timeout,context=context,check_hostname=check_hostname,
                                          addresses=addresses,**kwargs)
        # Python 3.14 no longer stores ``check_hostname`` as a handler
        # attribute; HTTPSHandler applies it to ``_context`` in __init__.
        # Keep the old value when present, and otherwise let the context retain
        # its configured certificate/hostname verification policy.
        return self.do_open(connection,req,context=self._context,
                            check_hostname=getattr(self,"_check_hostname",None))

def _safe_opener(*handlers):
    # Direct connections keep the socket destination equal to the validated DNS result.
    # Environment proxies cannot provide that guarantee for the final destination.
    return build_opener(ProxyHandler({}),_SafeRedirects(),_PinnedHTTPHandler(),_PinnedHTTPSHandler(),*handlers)

def _decode_content(data: bytes, content_encoding: str | None) -> bytes:
    """Decode standard HTTP content codings without exceeding the body budget."""
    encodings=[item.strip().casefold() for item in (content_encoding or "").split(",") if item.strip()]
    decoded=data
    for encoding in reversed(encodings):
        if encoding=="identity":
            continue
        if encoding not in {"gzip","deflate"}:
            raise ValueError("UNSUPPORTED_CONTENT_ENCODING")
        window_bits=(zlib.MAX_WBITS | 16) if encoding=="gzip" else zlib.MAX_WBITS
        try:
            decompressor=zlib.decompressobj(window_bits)
            output=decompressor.decompress(decoded,MAX_BYTES+1)
            if len(output)>MAX_BYTES or decompressor.unconsumed_tail:
                raise ValueError("RESPONSE_TOO_LARGE")
            tail=decompressor.flush()
        except zlib.error as exc:
            raise ValueError("CONTENT_DECOMPRESSION_FAILED") from exc
        if len(output)+len(tail)>MAX_BYTES:
            raise ValueError("RESPONSE_TOO_LARGE")
        if not decompressor.eof or decompressor.unused_data:
            raise ValueError("CONTENT_DECOMPRESSION_FAILED")
        decoded=output+tail
    return decoded

_META_CHARSET_RE = re.compile(
    rb"<meta\b[^>]*(?:charset\s*=\s*[\"']?\s*([a-z0-9._:-]+)|"
    rb"content\s*=\s*[\"'][^\"']*?charset\s*=\s*([a-z0-9._:-]+))",
    re.I,
)

def _decode_response_text(data: bytes, content_type: str | None = None) -> tuple[str, str]:
    """Decode provider/page bytes using BOM, HTTP/meta declarations, then safe fallbacks.

    Search results often link to Japanese publishers that still serve Shift_JIS or
    omit/incorrectly advertise their charset. UTF-8 replacement decoding silently
    turns those pages into irreversible mojibake, so try strict decoding first.
    """
    if data.startswith((b"\xef\xbb\xbf",)):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16" if data.startswith(b"\xff\xfe") else "utf-16"
        return data.decode(encoding), encoding

    declared = ""
    if content_type:
        match = re.search(r"charset\s*=\s*[\"']?\s*([^;\s\"']+)", content_type, re.I)
        if match:
            declared = match.group(1).strip()
    if not declared and data[:4096].lstrip().startswith(b"<"):
        match = _META_CHARSET_RE.search(data[:4096])
        if match:
            declared = next((part.decode("ascii", "ignore") for part in match.groups() if part), "")

    candidates = []
    if declared:
        candidates.append(declared)
    candidates.extend(("utf-8", "shift_jis", "euc_jp", "cp1252"))
    tried = set()
    for candidate in candidates:
        normalized = candidate.casefold().replace("_", "-")
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            canonical = codecs.lookup(candidate).name
            return data.decode(candidate, errors="strict"), canonical
        except (LookupError, UnicodeDecodeError):
            continue
    fallback = declared or "utf-8"
    try:
        return data.decode(fallback, errors="replace"), fallback
    except LookupError:
        return data.decode("utf-8", errors="replace"), "utf-8"

class _SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)
def fetch(url:str)->dict:
    print('WEB_REQUEST_START', file=sys.stderr, flush=True); validate_url(url); print('WEB_AUTHORIZATION_VALIDATED=YES WEB_NETWORK_POLICY=PASS', file=sys.stderr, flush=True)
    req=Request(url,headers={'User-Agent':'OLCR/0.4.7'}); print('WEB_HTTP_START', file=sys.stderr, flush=True)
    with _safe_opener().open(req,timeout=10) as r:
        data=r.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES: raise ValueError('RESPONSE_TOO_LARGE')
        data=_decode_content(data,r.headers.get('Content-Encoding'))
        # PDF object streams are binary containers, not UTF-8 source text.
        # A failed document extraction may fall through to other fetched sources;
        # it must never inject PDF dictionaries/compressed bytes into Brain.
        if data.lstrip().startswith(b"%PDF-") or r.headers.get_content_type() == "application/pdf":
            raise ValueError("WEB_DOCUMENT_TEXT_UNAVAILABLE")
        text, decoded_charset = _decode_response_text(data, r.headers.get("Content-Type"))
        if 'html' in (r.headers.get_content_type() or ''):
            parser=_Text(r.geturl()); parser.feed(text); extracted=parser.result(); text=extracted["clean_markdown"]
        else:
            extracted={"title":"","description":"","clean_markdown":text}
        print(f'WEB_HTTP_RESPONSE=YES WEB_HTTP_STATUS={r.status} WEB_RESPONSE_BYTES={len(data)} WEB_EXTRACT_END=YES WEB_CONTEXT_CHARS={len(text)} WEB_CONTEXT_VALID=YES', file=sys.stderr, flush=True)
        clean=text[:MAX_CHARS]
        return {'requested_url':url,'final_url':r.geturl(),'title':extracted['title'],
                'description':extracted['description'],'metadata':{'http_status':r.status,
                'content_type':r.headers.get_content_type(),'charset':decoded_charset},
                'timestamp':__import__('time').time(),'clean_markdown':clean,'text':clean,
                'truncated':len(text)>MAX_CHARS,'warnings':[],'provider':'http',
                'http_status':r.status,'content_type':r.headers.get_content_type()}

def search(query: str, limit: int = DEFAULT_SEARCH_RESULTS) -> list[dict]:
    """Small public HTML search adapter; results remain untrusted candidates."""
    if not query or limit < 1: return []
    limit=max(1, int(limit))
    endpoint="https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    print("WEB_SEARCH_REQUEST_STARTED=YES", file=sys.stderr, flush=True)
    validate_url(endpoint)
    req=Request(endpoint, headers={"User-Agent":"OLCR/0.4.7"})
    try:
        with _safe_opener().open(req, timeout=10) as response:
            data=response.read(MAX_BYTES+1); status=response.status; content_type=response.headers.get_content_type()
            content_type_header=response.headers.get("Content-Type")
    except Exception:
        print("WEB_SEARCH_PROVIDER_STATUS=NETWORK_ERROR", file=sys.stderr, flush=True); raise
    if len(data)>MAX_BYTES: raise ValueError("SEARCH_RESPONSE_TOO_LARGE")
    html, _decoded_charset = _decode_response_text(data, content_type_header)
    parser=_SearchParser(); parser.feed(html)
    results=parser.results[:limit]
    classification,_=_classify_search_html(html, len(results))
    provider_status="OK" if results else "INTERSTITIAL" if classification=="INTERSTITIAL" else "PARSER_MISMATCH" if classification=="PARSER_MISMATCH" else "PARSE_ZERO"
    print(f"WEB_SEARCH_FINAL_URL={endpoint} WEB_SEARCH_HTTP_STATUS={status} WEB_SEARCH_CONTENT_TYPE={content_type} WEB_SEARCH_RESPONSE_BYTES={len(data)} WEB_SEARCH_PARSE_RESULT_COUNT={len(results)} WEB_SEARCH_PROVIDER_STATUS={provider_status}", file=sys.stderr, flush=True)
    return results

def brave_search(query: str, limit: int = DEFAULT_SEARCH_RESULTS) -> list[dict]:
    key=provider_key("brave")
    print(f"WEB_SEARCH_PROVIDER=brave WEB_SEARCH_KEY_CONFIGURED={'YES' if key else 'NO'}", file=sys.stderr, flush=True)
    if not key: raise RuntimeError("WEB_SEARCH_PROVIDER_NOT_READY")
    limit=max(1, int(limit))
    provider_max=BRAVE_WEB_PAGE_SIZE*BRAVE_WEB_MAX_PAGES
    if limit>provider_max: raise ValueError(f"PROVIDER_RESULT_LIMIT_EXCEEDED:{provider_max}")
    query=" ".join(query.split())[:500]
    rows=[]
    seen=set(); status=None; more_results=True
    for page in range(BRAVE_WEB_MAX_PAGES):
        params={"q":query,"count":BRAVE_WEB_PAGE_SIZE,"offset":page}
        req=Request("https://api.search.brave.com/res/v1/web/search?" + urlencode(params), headers={"Accept":"application/json","X-Subscription-Token":key,"User-Agent":"OLCR/0.4.7"})
        print("WEB_SEARCH_REQUEST_STARTED=YES", file=sys.stderr, flush=True)
        try:
            with _safe_opener().open(req, timeout=10) as response:
                body=response.read(MAX_BYTES+1); status=response.status
        except Exception:
            print("WEB_SEARCH_PROVIDER_STATUS=HTTP_ERROR", file=sys.stderr, flush=True); raise
        if len(body)>MAX_BYTES: raise ValueError("SEARCH_RESPONSE_TOO_LARGE")
        payload=json.loads(body)
        block=payload.get("web",{}) if isinstance(payload,dict) else {}
        batch=block.get("results",[]) if isinstance(block,dict) else []
        query_meta=payload.get("query",{}) if isinstance(payload,dict) else {}
        more_results=bool(query_meta.get("more_results_available",False)) if isinstance(query_meta,dict) else False
        for item in batch if isinstance(batch,list) else []:
            if not (isinstance(item,dict) and isinstance(item.get("url"),str) and isinstance(item.get("title"),str)): continue
            if item["url"] in seen: continue
            seen.add(item["url"])
            rows.append({"title":item["title"][:300],"url":item["url"],"snippet":str(item.get("description", ""))[:1000],"rank":len(rows)+1,"provider":"brave"})
            if len(rows)>=limit: break
        if len(rows)>=limit or not more_results or not batch: break
    print(f"WEB_SEARCH_HTTP_STATUS={status} WEB_SEARCH_RESULT_COUNT={len(rows)} WEB_SEARCH_PROVIDER_STATUS={'OK' if rows else 'NO_RESULTS'}", file=sys.stderr, flush=True)
    return rows

def brave_news_search(query: str, limit: int = DEFAULT_SEARCH_RESULTS, freshness: str | None = None) -> list[dict]:
    """Brave's dedicated News Search adapter, normalized to web candidates."""
    key=provider_key("brave")
    print(f"BRAVE_SEARCH_RESOURCE=NEWS WEB_SEARCH_PROVIDER=brave WEB_SEARCH_KEY_CONFIGURED={'YES' if key else 'NO'}", file=sys.stderr, flush=True)
    if not key: raise RuntimeError("WEB_SEARCH_PROVIDER_NOT_READY")
    limit=max(1, int(limit)); provider_max=BRAVE_NEWS_PAGE_SIZE*BRAVE_NEWS_MAX_PAGES
    if limit>provider_max: raise ValueError(f"PROVIDER_RESULT_LIMIT_EXCEEDED:{provider_max}")
    query=" ".join(query.split())[:500]
    rows=[]
    seen=set(); status=None
    for page in range(BRAVE_NEWS_MAX_PAGES):
        params={"q":query,"count":BRAVE_NEWS_PAGE_SIZE,"offset":page}
        if freshness: params["freshness"]=freshness
        req=Request("https://api.search.brave.com/res/v1/news/search?" + urlencode(params), headers={"Accept":"application/json","X-Subscription-Token":key,"User-Agent":"OLCR/0.4.7"})
        print("WEB_SEARCH_REQUEST_STARTED=YES", file=sys.stderr, flush=True)
        try:
            with _safe_opener().open(req, timeout=10) as response:
                body=response.read(MAX_BYTES+1); status=response.status
        except Exception:
            print("WEB_SEARCH_PROVIDER_STATUS=HTTP_ERROR", file=sys.stderr, flush=True); raise
        if len(body)>MAX_BYTES: raise ValueError("SEARCH_RESPONSE_TOO_LARGE")
        payload=json.loads(body); batch=payload.get("results",[]) if isinstance(payload,dict) else []
        for item in batch if isinstance(batch,list) else []:
            if not (isinstance(item,dict) and isinstance(item.get("url"),str) and isinstance(item.get("title"),str)): continue
            if item["url"] in seen: continue
            seen.add(item["url"])
            rows.append({"title":item["title"][:300],"url":item["url"],"snippet":str(item.get("description", ""))[:1000],"published":item.get("age") or item.get("page_age"),"rank":len(rows)+1,"provider":"brave"})
            if len(rows)>=limit: break
        if len(rows)>=limit or not batch: break
    print(f"WEB_SEARCH_HTTP_STATUS={status} WEB_SEARCH_RESULT_COUNT={len(rows)} WEB_SEARCH_PROVIDER_STATUS={'OK' if rows else 'NO_RESULTS'}", file=sys.stderr, flush=True)
    return rows

def tavily_search(query: str, limit: int = DEFAULT_SEARCH_RESULTS) -> list[dict]:
    key=provider_key("tavily")
    print(f"WEB_SEARCH_PROVIDER=tavily WEB_SEARCH_KEY_CONFIGURED={'YES' if key else 'NO'}", file=sys.stderr, flush=True)
    if not key: raise RuntimeError("WEB_SEARCH_PROVIDER_NOT_READY")
    limit=max(1, int(limit))
    if limit>TAVILY_MAX_RESULTS: raise ValueError(f"PROVIDER_RESULT_LIMIT_EXCEEDED:{TAVILY_MAX_RESULTS}")
    payload=json.dumps({"api_key":key,"query":" ".join(query.split())[:500],"max_results":limit}).encode()
    req=Request("https://api.tavily.com/search", data=payload, method="POST", headers={"Accept":"application/json","Content-Type":"application/json","User-Agent":"OLCR/0.4.7"})
    print("WEB_SEARCH_REQUEST_STARTED=YES", file=sys.stderr, flush=True)
    try:
        with _safe_opener().open(req, timeout=10) as response:
            body=response.read(MAX_BYTES+1); status=response.status
    except Exception:
        print("WEB_SEARCH_PROVIDER_STATUS=HTTP_ERROR", file=sys.stderr, flush=True); raise
    if len(body)>MAX_BYTES: raise ValueError("SEARCH_RESPONSE_TOO_LARGE")
    data=json.loads(body)
    rows=[]
    for rank,item in enumerate((data.get("results",[]) if isinstance(data,dict) else []),1):
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
