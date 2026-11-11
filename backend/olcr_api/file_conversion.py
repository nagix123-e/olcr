"""Deterministic attachment conversion through a loopback ConvertX service."""
from __future__ import annotations

import base64
import binascii
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

import httpx

MAX_INPUT = 50_000_000
MAX_OUTPUT = 250_000_000
CONVERSION_LOCK = threading.RLock()
FORMAT_ALIASES = {'jpg': 'jpeg', 'jfif': 'jpeg', 'htm': 'html', 'tex': 'latex', 'md': 'markdown'}
OUTPUT_EXTENSIONS = {'jpeg': 'jpg', 'latex': 'tex', 'markdown': 'md'}


class ConversionError(ValueError):
    pass


def conversion_target(message: str) -> str | None:
    """Only explicit conversion commands; never ask a language model to rewrite data."""
    text = message.strip().lower()
    if re.search(r'(?:変換.{0,12}(?:方法|プログラム|コード|実装)|変換できますか|how\s+to\s+convert)', text):
        return None
    conversion_verb = re.search(r'(?:変換|変え|にして|\bconvert\b)', text)
    # "files may change ... to support imports" is a coding instruction.
    # English "change" requires an explicit file/format conversion object.
    change_file = re.match(r'^(?:please\s+)?change\s+(?:(?:this|that|the|my|a)\s+)?(?:file|document|image|format|extension)\b', text)
    if not conversion_verb and not change_file:
        return None
    matches = re.findall(r'(?<![a-z0-9_/])\.?([a-z][a-z0-9]{0,15}(?:\.[a-z0-9]{1,8})?)\s*(?:形式\s*)?(?:に|へ)', text)
    if not matches:
        matches = re.findall(r'\b(?:to|into)\s+[.]*([a-z][a-z0-9]{0,15}(?:\.[a-z0-9]{1,8})?)\b', text)
    if len(set(matches)) != 1:
        return None
    return matches[0]


class Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__()
        self.strategies: list[str] = []
        self.downloads: list[tuple[str, str]] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        value = a.get('data-value') or (a.get('value') if tag == 'option' else '')
        if value and re.fullmatch(r'[a-zA-Z0-9_.+-]+,[a-zA-Z0-9_-]+', value):
            if value not in self.strategies:
                self.strategies.append(value)
        if tag == 'a' and 'download' in a and str(a.get('href', '')).startswith('/download/'):
            pair = (a['href'], a.get('download') or unquote(a['href'].rsplit('/', 1)[-1]))
            if pair not in self.downloads:
                self.downloads.append(pair)


def attachment_bytes(attachment: dict) -> tuple[str, bytes]:
    name = str(attachment.get('name') or '')
    if not name or '/' in name or '\\' in name or name in {'.', '..'} or any(ord(c) < 32 for c in name):
        raise ConversionError('添付ファイル名が不正です。')
    encoded = attachment.get('bytesBase64') or attachment.get('dataBase64')
    if not encoded and attachment.get('data_url'):
        header, sep, encoded = str(attachment['data_url']).partition(',')
        if not sep or not header.endswith(';base64'):
            raise ConversionError('添付データの形式が不正です。')
    if not isinstance(encoded, str) or len(encoded) > (MAX_INPUT * 4 // 3 + 4):
        raise ConversionError('元ファイルのデータがありません。ファイルを添付し直してください（最大50 MB）。')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ConversionError('添付データが破損しています。') from exc
    if not raw or len(raw) > MAX_INPUT:
        raise ConversionError('添付できるファイルは0バイトより大きく50 MB以下です。')
    return name, raw


def service_url(*, managed=True) -> str:
    if managed and not os.environ.get('OLCR_CONVERTX_URL'):
        from .conversion_runtime import runtime, RuntimeSetupError
        try:
            return runtime.ensure()
        except RuntimeSetupError as exc:
            raise ConversionError(str(exc)) from exc
    url = os.environ.get('OLCR_CONVERTX_URL', 'http://127.0.0.1:3047').rstrip('/')
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ConversionError('ConvertXの接続先にはローカルのHTTPアドレスを設定してください。')
    return url


def convert_attachment(attachment: dict, target: str, workspace: str | None,
                       *, transport=None, timeout: float = 300) -> list[str]:
    name, raw = attachment_bytes(attachment)
    source = Path(name).suffix.lstrip('.').lower()
    if not source:
        raise ConversionError('元ファイルに拡張子が必要です。')
    destination = Path(workspace).expanduser() if workspace else Path.home() / 'Downloads'
    if workspace and not destination.is_dir():
        raise ConversionError('プロジェクトの保存先フォルダが見つかりません。設定を確認してください。')
    try:
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination.resolve(strict=True)
    except OSError as exc:
        raise ConversionError('保存先フォルダを利用できません。') from exc
    endpoint = service_url(managed=transport is None)
    started = time.monotonic()
    def remaining():
        seconds = timeout - (time.monotonic() - started)
        if seconds <= 0:
            raise ConversionError('ConvertXの変換がタイムアウトしました。')
        return min(seconds, 30)
    def request(client, method, path, **kwargs):
        response = client.request(method, path, timeout=remaining(), **kwargs)
        if response.status_code >= 400:
            raise ConversionError(f'ConvertXの処理に失敗しました（HTTP {response.status_code}）。')
        return response
    try:
        with httpx.Client(base_url=endpoint, transport=transport, trust_env=False,
                          follow_redirects=False) as client:
            request(client, 'GET', '/')
            job = client.cookies.get('jobId')
            if not job or not re.fullmatch(r'\d+', job) or not client.cookies.get('auth'):
                raise ConversionError('ConvertXのローカルサービス設定を確認してください（ALLOW_UNAUTHENTICATED=true、HTTP_ALLOWED=true）。')
            strategies = Page(request(client, 'POST', '/conversions', json={'fileType': source}).text).strategies
            canonical_target = FORMAT_ALIASES.get(target, target)
            choices = [s for s in strategies if s.split(',')[0].lower() == canonical_target]
            if not choices:
                supported = sorted({s.split(',')[0] for s in strategies})
                raise ConversionError(f'{source} → {target} はConvertXで対応していません。'
                                      + ('対応形式: ' + ', '.join(supported[:40]) if supported else ''))
            request(client, 'POST', '/upload', files={'file': (name, raw, 'application/octet-stream')})
            response = request(client, 'POST', '/convert', data={'file_names': json.dumps([name]), 'convert_to': choices[0]})
            if response.status_code not in {302, 303} or response.headers.get('location') != f'/results/{job}':
                raise ConversionError('ConvertXが変換ジョブを受け付けませんでした。')
            while True:
                response = request(client, 'POST', f'/progress/{job}')
                if any(error in response.text for error in ('Failed, check logs', 'File type not supported')):
                    raise ConversionError('ConvertXで変換に失敗しました。入力形式と変換先を確認してください。')
                downloads = Page(response.text).downloads
                if downloads:
                    break
                time.sleep(min(0.3, remaining()))
            with tempfile.TemporaryDirectory(prefix='olcr-convert-') as stage:
                staged = []
                total = 0
                for index, (href, output_name) in enumerate(downloads):
                    parts = urlsplit(href)
                    if (parts.scheme or parts.netloc or parts.query or parts.fragment
                            or not re.fullmatch(r'/download/[^/]+/' + job + r'/[^/]+', parts.path)
                            or Path(output_name).name != output_name or '\\' in output_name
                            or not output_name.lower().endswith(('.' + target, '.' + OUTPUT_EXTENSIONS.get(canonical_target, canonical_target)))
                            or any(ord(c) < 32 for c in output_name)):
                        raise ConversionError('ConvertXの出力ファイル情報が不正です。')
                    temporary = Path(stage) / str(index)
                    with client.stream('GET', href, timeout=remaining()) as result:
                        if result.status_code != 200 or ('text/html' in result.headers.get('content-type', '') and target not in {'html', 'htm'}):
                            raise ConversionError('変換後のファイルを取得できませんでした。')
                        with temporary.open('wb') as stream:
                            for chunk in result.iter_bytes():
                                remaining()
                                total += len(chunk)
                                if total > MAX_OUTPUT:
                                    raise ConversionError('変換結果が250 MBを超えました。')
                                stream.write(chunk)
                    if temporary.stat().st_size == 0:
                        raise ConversionError('ConvertXの出力ファイルが空です。')
                    # Upstream normalizes jpeg->jpg, markdown->md, etc. Keep the
                    # requested suffix for equivalent encodings without altering data.
                    if not output_name.lower().endswith('.' + target):
                        output_name = str(Path(output_name).with_suffix('.' + target))
                    staged.append((temporary, output_name))
                saved = []
                try:
                    for temporary, output_name in staged:
                        stem = output_name[:-(len(target) + 1)]
                        for number in range(10000):
                            path = destination / (output_name if number == 0 else f'{stem} ({number}).{target}')
                            try:
                                output = path.open('xb')
                            except FileExistsError:
                                continue
                            saved.append(str(path))
                            with output, temporary.open('rb') as source_file:
                                import shutil
                                shutil.copyfileobj(source_file, output)
                            break
                        else:
                            raise ConversionError('同名ファイルが多すぎるため保存できませんでした。')
                except Exception:
                    for path in saved:
                        Path(path).unlink(missing_ok=True)
                    raise
                return saved
    except httpx.HTTPError as exc:
        raise ConversionError('変換環境に接続できません。設定のConvertの状態を確認して再試行してください。') from exc
    except OSError as exc:
        raise ConversionError('変換後のファイルを保存できませんでした。保存先の権限と空き容量を確認してください。') from exc
