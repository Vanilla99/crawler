import os
import tempfile
import unittest
from unittest.mock import patch

from vcrawl.models import FetchResult

from vcrawl.fetchers import BrowserFetcher, HttpFetcher


class FakeHeaders:
    def __init__(self, values=None):
        self.values = values or {"content-type": "text/html"}

    def items(self):
        return self.values.items()

    def get_content_charset(self):
        return "utf-8"


class FakeResponse:
    def __init__(self, url, body=b"<html></html>", headers=None, status=200):
        self.url = url
        self.body = body
        self.headers = FakeHeaders(headers)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body

    def geturl(self):
        return self.url


class FetcherConfigTests(unittest.TestCase):
    def test_http_fetcher_builds_proxy_opener(self):
        fetcher = HttpFetcher(proxy_url="http://127.0.0.1:8080", headers={"X-Test": "1"})
        self.assertIsNotNone(fetcher.opener)
        self.assertEqual(fetcher.headers["X-Test"], "1")

    def test_http_fetcher_loads_cookie_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = os.path.join(tmp, "cookies.txt")
            with open(cookie_path, "w", encoding="utf-8") as fh:
                fh.write("# Netscape HTTP Cookie File\n")
            fetcher = HttpFetcher(cookies_file=cookie_path)
            self.assertIsNotNone(fetcher.opener)

    def test_http_cache_reads_cached_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = HttpFetcher(http_cache=True, cache_dir=tmp)
            result = FetchResult(
                url="https://example.com/page",
                status_code=200,
                headers={"content-type": "text/html"},
                text="<html>cached</html>",
                final_url="https://example.com/page",
                fetcher="http",
            )
            fetcher._write_cache(result)
            cached = fetcher.fetch("https://example.com/page")
            self.assertEqual(cached.fetcher, "http-cache")
            self.assertEqual(cached.session_id, "http-cache")
            self.assertIn("cached", cached.text)

    def test_http_fetcher_session_pool_rotates_session_ids(self):
        fetcher = HttpFetcher(session_pool=True, session_pool_size=2)
        with patch("vcrawl.fetchers.urlopen") as fake_urlopen:
            fake_urlopen.side_effect = [
                FakeResponse("https://example.com/a"),
                FakeResponse("https://example.com/b"),
                FakeResponse("https://example.com/c"),
            ]
            first = fetcher.fetch("https://example.com/a")
            second = fetcher.fetch("https://example.com/b")
            third = fetcher.fetch("https://example.com/c")
        self.assertEqual([first.session_id, second.session_id, third.session_id], ["http-1", "http-2", "http-1"])

    def test_http_cache_skips_failed_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = HttpFetcher(http_cache=True, cache_dir=tmp)
            result = FetchResult(
                url="https://example.com/page",
                status_code=500,
                headers={},
                text="",
                fetcher="http",
                error="server error",
            )
            fetcher._write_cache(result)
            with patch("vcrawl.fetchers.urlopen") as fake_urlopen:
                fake_urlopen.side_effect = OSError("network attempted")
                fetched = fetcher.fetch("https://example.com/page")
            self.assertEqual(fetched.status_code, 0)
            self.assertIn("network attempted", fetched.error)

    def test_http_fetcher_records_direct_media_response_hint(self):
        fetcher = HttpFetcher()
        with patch("vcrawl.fetchers.urlopen") as fake_urlopen:
            fake_urlopen.return_value = FakeResponse(
                "https://cdn.example.com/master.m3u8",
                body=b"#EXTM3U\n",
                headers={"content-type": "application/vnd.apple.mpegurl"},
            )
            result = fetcher.fetch("https://cdn.example.com/master.m3u8")
        self.assertEqual(len(result.media_hints), 1)
        self.assertEqual(result.media_hints[0].kind, "hls_manifest")
        self.assertEqual(result.media_hints[0].source, "http.response")
        self.assertEqual(result.media_hints[0].content_type, "application/vnd.apple.mpegurl")

    def test_http_fetcher_defaults_missing_status_to_200(self):
        fetcher = HttpFetcher()
        with patch("vcrawl.fetchers.urlopen") as fake_urlopen:
            fake_urlopen.return_value = FakeResponse("https://example.com/page", status=None)
            result = fetcher.fetch("https://example.com/page")
        self.assertEqual(result.status_code, 200)

    def test_http_cache_preserves_media_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = HttpFetcher(http_cache=True, cache_dir=tmp)
            with patch("vcrawl.fetchers.urlopen") as fake_urlopen:
                fake_urlopen.return_value = FakeResponse(
                    "https://cdn.example.com/video.mp4",
                    body=b"fake",
                    headers={"content-type": "video/mp4"},
                )
                first = fetcher.fetch("https://cdn.example.com/video.mp4")
            cached = fetcher.fetch("https://cdn.example.com/video.mp4")
            self.assertEqual(first.media_hints[0].kind, "video")
            self.assertEqual(cached.fetcher, "http-cache")
            self.assertEqual(cached.media_hints[0].url, "https://cdn.example.com/video.mp4")

    def test_browser_response_hint_uses_network_response_metadata(self):
        class FakeRequest:
            method = "GET"
            resource_type = "xhr"

        class FakeBrowserResponse:
            url = "https://cdn.example.com/live/manifest.mpd"
            status = 200
            headers = {"content-type": "application/dash+xml"}
            request = FakeRequest()

        hint = BrowserFetcher._media_hint_from_playwright_response(FakeBrowserResponse())
        self.assertEqual(hint.kind, "dash_manifest")
        self.assertEqual(hint.source, "browser.network")
        self.assertEqual(hint.resource_type, "xhr")
        self.assertEqual(hint.metadata["content_type"], "application/dash+xml")


if __name__ == "__main__":
    unittest.main()
