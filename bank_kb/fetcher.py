"""HTTP fetcher with polite delays, retries, and conditional GET via ETag/Last-Modified.

The engine talks to bank IR pages (HTML) and PDF endpoints. Bank sites are often slow,
sometimes rate-limit, and sometimes serve broken HTTPS — so we keep timeouts generous,
retry transient failures, and never crash on a single bad host.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    status: int
    content: Optional[bytes]
    headers: dict
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.content is not None


class Fetcher:
    def __init__(self, user_agent: str, request_delay_seconds: float = 2.0,
                 request_timeout_seconds: int = 45, max_pdf_size_mb: int = 80):
        self.delay = request_delay_seconds
        self.timeout = request_timeout_seconds
        self.max_bytes = max_pdf_size_mb * 1024 * 1024
        self._last_request_at = 0.0

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/pdf,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_at = time.monotonic()

    def get(self, url: str, *, extra_headers: Optional[dict] = None) -> FetchResult:
        # file:// URLs are produced by per-bank adapters that pre-render content
        # the engine can't fetch directly (e.g. Yes Bank press releases live as
        # HTML in a CMS field, not as downloadable PDFs — the adapter renders
        # them to PDF locally and hands us a file:// path). No politeness
        # throttling, no retries, no HTTP at all.
        if url.startswith("file://"):
            return self._read_file_url(url)

        self._throttle()
        headers = dict(extra_headers or {})
        try:
            # Some bank sites have stale/invalid SSL — verify=True by default; if it
            # fails we surface the error and skip rather than blindly downgrading.
            r = self.session.get(url, headers=headers, timeout=self.timeout, stream=True)
            content = None
            if r.status_code == 200:
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > self.max_bytes:
                        log.warning("Aborting download (>%d MB): %s", self.max_bytes // (1024 * 1024), url)
                        r.close()
                        return FetchResult(url=url, status=0, content=None, headers=dict(r.headers))
                content = bytes(buf)
            return FetchResult(url=url, status=r.status_code, content=content, headers=dict(r.headers))
        except requests.RequestException as e:
            log.warning("Fetch failed for %s: %s", url, e)
            return FetchResult(url=url, status=0, content=None, headers={})

    def _read_file_url(self, url: str) -> FetchResult:
        """Read a local file produced by an adapter and return it like an HTTP
        response. Adapters use this when the upstream source isn't a PDF the
        engine can download directly."""
        path = Path(unquote(urlparse(url).path))
        if not path.is_file():
            log.warning("file:// fetch missing: %s", path)
            return FetchResult(url=url, status=404, content=None, headers={})
        try:
            data = path.read_bytes()
        except OSError as e:
            log.warning("file:// read failed for %s: %s", path, e)
            return FetchResult(url=url, status=500, content=None, headers={})
        if len(data) > self.max_bytes:
            log.warning("file:// over size limit (%d MB): %s",
                        self.max_bytes // (1024 * 1024), path)
            return FetchResult(url=url, status=0, content=None, headers={})
        return FetchResult(url=url, status=200, content=data,
                           headers={"Content-Length": str(len(data))})

    @staticmethod
    def sha1(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    @staticmethod
    def save(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
