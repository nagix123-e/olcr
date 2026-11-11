"""Bounded download of image URLs already collected by a scrape run."""
from io import BytesIO
import json
from urllib.request import Request
from zipfile import ZipFile, ZIP_STORED
from .web import _safe_opener, validate_url

IMAGE_TYPES = {'image/png':'png', 'image/jpeg':'jpg', 'image/gif':'gif', 'image/webp':'webp', 'image/avif':'avif'}

def image_archive(rows, *, limit=20):
    urls = list(dict.fromkeys(str(row.get('image_url') or '') for row in rows if row.get('image_url')))[:limit]
    if not urls: raise ValueError('NO_COLLECTED_IMAGE_URLS')
    output, manifest, total = BytesIO(), [], 0
    with ZipFile(output, 'w', ZIP_STORED) as archive:
        for index, url in enumerate(urls, 1):
            if total >= 20_000_000:
                manifest.append({'url':url,'status':'TOTAL_SIZE_LIMIT'});continue
            try:
                validate_url(url)
                with _safe_opener().open(Request(url, headers={'User-Agent':'OLCR/0.4.7'}), timeout=5) as response:
                    mime = response.headers.get_content_type()
                    if mime not in IMAGE_TYPES: raise ValueError('UNSUPPORTED_IMAGE_TYPE')
                    data = response.read(min(5_000_000, 20_000_000-total)+1)
                    if len(data)>5_000_000 or total+len(data)>20_000_000: raise ValueError('IMAGE_SIZE_LIMIT')
                filename=f'image-{index:04d}.{IMAGE_TYPES[mime]}'
                archive.writestr(filename,data);total+=len(data)
                manifest.append({'url':url,'file':filename,'status':'SAVED'})
            except Exception:
                manifest.append({'url':url,'status':'DOWNLOAD_FAILED'})
        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
    return output.getvalue()
