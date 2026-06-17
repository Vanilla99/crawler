import http.cookiejar
import hashlib
import json
import os
import time
from dataclasses import asdict
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener, urlopen

from .challenges import detect_challenge
from .media_detection import media_hint_kind
from .models import FetchResult, MediaHint


class HttpFetcher:
    name = "http"

    def __init__(
        self,
        timeout_seconds=20.0,
        user_agent="vcrawl/0.1",
        proxy_url=None,
        headers=None,
        cookies_file=None,
        http_cache=False,
        cache_dir=None,
        session_pool=True,
        session_pool_size=1,
    ):
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.proxy_url = proxy_url
        self.headers = headers or {}
        self.cookies_file = cookies_file
        self.http_cache = bool(http_cache)
        self.cache_dir = cache_dir
        self.session_pool = bool(session_pool)
        self.session_pool_size = max(1, int(session_pool_size or 1))
        self._session_lock = Lock()
        self._session_index = 0
        self.sessions = self._build_sessions()
        self.opener = self.sessions[0]["opener"] if self.sessions else None

    def fetch(self, url):
        cached = self._read_cache(url)
        if cached:
            return cached
        session = self._next_session()
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"}
        headers.update(self.headers)
        request = Request(url, headers=headers)
        try:
            opener = session["opener"] if session else self.opener
            open_fn = opener.open if opener else urlopen
            with open_fn(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                response_headers = dict(response.headers.items())
                charset = response.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                status = getattr(response, "status", None) or 200
                final_url = response.geturl()
        except HTTPError as exc:
            raw = exc.read()
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            charset = exc.headers.get_content_charset() if exc.headers else None
            text = raw.decode(charset or "utf-8", errors="replace")
            return FetchResult(
                url=url,
                status_code=exc.code,
                headers=response_headers,
                text=text,
                final_url=exc.geturl(),
                fetcher=self.name,
                error=str(exc),
                challenge_detected=detect_challenge(url, exc.code, response_headers, text),
                session_id=session["id"] if session else None,
            )
        except URLError as exc:
            return FetchResult(
                url=url,
                status_code=0,
                headers={},
                text="",
                fetcher=self.name,
                error=str(exc),
                session_id=session["id"] if session else None,
            )
        except OSError as exc:
            return FetchResult(
                url=url,
                status_code=0,
                headers={},
                text="",
                fetcher=self.name,
                error=str(exc),
                session_id=session["id"] if session else None,
            )
        result = FetchResult(
            url=url,
            status_code=status,
            headers=response_headers,
            text=text,
            final_url=final_url,
            fetcher=self.name,
            challenge_detected=detect_challenge(final_url or url, status, response_headers, text),
            session_id=session["id"] if session else None,
            media_hints=_media_hints_from_response(final_url or url, status, response_headers, "http.response"),
        )
        self._write_cache(result)
        return result

    def _build_sessions(self):
        size = self.session_pool_size if self.session_pool else 1
        return [
            {
                "id": "http-%s" % (index + 1),
                "opener": self._build_opener(),
            }
            for index in range(size)
        ]

    def _next_session(self):
        if not self.sessions:
            return None
        with self._session_lock:
            session = self.sessions[self._session_index % len(self.sessions)]
            self._session_index += 1
            return session

    def _build_opener(self):
        handlers = []
        if self.proxy_url:
            handlers.append(ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))
        if self.cookies_file:
            jar = http.cookiejar.MozillaCookieJar()
            jar.load(self.cookies_file, ignore_discard=True, ignore_expires=True)
            handlers.append(HTTPCookieProcessor(jar))
        if not handlers:
            return None
        return build_opener(*handlers)

    def _cache_path(self, url):
        if not self.http_cache or not self.cache_dir:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, digest + ".json")

    def _read_cache(self, url):
        path = self._cache_path(url)
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        return FetchResult(
            url=payload.get("url") or url,
            status_code=int(payload.get("status_code") or 0),
            headers=payload.get("headers") or {},
            text=payload.get("text") or "",
            final_url=payload.get("final_url"),
            fetcher="http-cache",
            error=payload.get("error"),
            challenge_detected=bool(payload.get("challenge_detected")),
            session_id="http-cache",
            media_hints=[_media_hint_from_payload(item) for item in payload.get("media_hints") or []],
        )

    def _write_cache(self, result):
        path = self._cache_path(result.url)
        if not path or result.error or result.status_code >= 400 or result.challenge_detected:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "url": result.url,
            "status_code": result.status_code,
            "headers": result.headers,
            "text": result.text,
            "final_url": result.final_url,
            "error": result.error,
            "challenge_detected": result.challenge_detected,
            "session_id": result.session_id,
            "media_hints": [asdict(hint) for hint in result.media_hints],
            "cached_at": time.time(),
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
        os.replace(tmp_path, path)


class BrowserFetcher:
    name = "browser"

    def __init__(self, timeout_seconds=20.0, headless=True, profile=None):
        self.timeout_seconds = timeout_seconds
        self.headless = headless
        self.profile = profile

    def fetch(self, url):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Browser fetcher requires Playwright: pip install vcrawl[browser] && playwright install chromium"
            ) from exc

        with sync_playwright() as playwright:
            media_hints = []
            seen_hints = set()

            def handle_response(response):
                hint = self._media_hint_from_playwright_response(response)
                if not hint:
                    return
                key = (hint.url, hint.source)
                if key in seen_hints:
                    return
                seen_hints.add(key)
                media_hints.append(hint)

            if self.profile:
                browser = playwright.chromium.launch_persistent_context(
                    self.profile,
                    headless=self.headless,
                )
                page = browser.new_page()
            else:
                browser = playwright.chromium.launch(headless=self.headless)
                page = browser.new_page()
            page.on("response", handle_response)
            response = page.goto(url, timeout=int(self.timeout_seconds * 1000), wait_until="networkidle")
            text = page.content()
            final_url = page.url
            status = response.status if response else 0
            headers = response.headers if response else {}
            for hint in _media_hints_from_response(final_url or url, status, headers, "browser.document"):
                key = (hint.url, hint.source)
                if key not in seen_hints:
                    seen_hints.add(key)
                    media_hints.append(hint)
            browser.close()
        return FetchResult(
            url=url,
            status_code=status,
            headers=dict(headers),
            text=text,
            final_url=final_url,
            fetcher=self.name,
            challenge_detected=detect_challenge(final_url, status, headers, text),
            session_id="browser-profile" if self.profile else "browser-ephemeral",
            media_hints=media_hints,
        )

    @staticmethod
    def _media_hint_from_playwright_response(response):
        try:
            url = response.url
            status = int(response.status)
            headers = dict(response.headers or {})
            request = response.request
            method = getattr(request, "method", None)
            resource_type = getattr(request, "resource_type", None)
        except Exception:
            return None
        hint = _media_hints_from_response(
            url,
            status,
            headers,
            "browser.network",
            method=method,
            resource_type=resource_type,
        )
        return hint[0] if hint else None


def _media_hints_from_response(url, status_code, headers, source, method=None, resource_type=None):
    content_type = _header_value(headers, "content-type")
    kind = media_hint_kind(url, content_type)
    if not kind:
        return []
    return [
        MediaHint(
            url=url,
            kind=kind,
            source=source,
            content_type=content_type,
            status_code=status_code,
            method=method,
            resource_type=resource_type,
            metadata={
                key: value
                for key, value in {
                    "content_type": content_type,
                    "status_code": str(status_code) if status_code is not None else None,
                    "method": method,
                    "resource_type": resource_type,
                }.items()
                if value
            },
        )
    ]


def _header_value(headers, name):
    lowered = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == lowered:
            return value
    return None


def _media_hint_from_payload(payload):
    return MediaHint(
        url=payload.get("url") or "",
        kind=payload.get("kind") or "media",
        source=payload.get("source") or "network",
        content_type=payload.get("content_type"),
        status_code=payload.get("status_code"),
        method=payload.get("method"),
        resource_type=payload.get("resource_type"),
        metadata=payload.get("metadata") or {},
    )
