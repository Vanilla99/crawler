import unittest

from vcrawl.extractors import VideoExtractor
from vcrawl.models import FetchResult, MediaHint


class VideoExtractorTests(unittest.TestCase):
    def test_extracts_common_video_clues(self):
        html = """
        <html>
          <head>
            <title>Sample Video Page</title>
            <meta property="og:video" content="https://cdn.example.com/open-graph.mp4">
            <script type="application/ld+json">
              {"@type": "VideoObject", "name": "JSON Video", "contentUrl": "https://cdn.example.com/video.m3u8"}
            </script>
          </head>
          <body>
            <a href="/watch/next">next</a>
            <video controls poster="/thumb.jpg">
              <source src="/media/sample.webm" type="video/webm">
            </video>
            <script>var url = "https://cdn.example.com/path/stream.mpd?token=demo";</script>
          </body>
        </html>
        """
        result = FetchResult(
            url="https://example.com/watch/1",
            status_code=200,
            headers={"content-type": "text/html"},
            text=html,
        )
        page = VideoExtractor().extract(result)
        media_urls = {video.media_url for video in page.videos}
        self.assertEqual(page.title, "Sample Video Page")
        self.assertIn("https://example.com/watch/next", page.links)
        self.assertIn("https://cdn.example.com/open-graph.mp4", media_urls)
        self.assertIn("https://cdn.example.com/video.m3u8", media_urls)
        self.assertIn("https://example.com/media/sample.webm", media_urls)
        self.assertIn("https://cdn.example.com/path/stream.mpd?token=demo", media_urls)
        self.assertEqual(page.diagnostics["final_candidates"], len(page.videos))
        self.assertGreaterEqual(page.diagnostics["html_candidates"], 3)
        self.assertEqual(page.diagnostics["regex_candidates"], 3)
        self.assertIn("regex.media_url", page.diagnostics["candidate_sources"])

    def test_detects_human_verification(self):
        html = "<html><title>Checking your browser</title><body>Verify you are human captcha</body></html>"
        result = FetchResult(url="https://example.com/", status_code=403, headers={}, text=html)
        page = VideoExtractor().extract(result)
        self.assertTrue(page.challenge_detected)

    def test_ignores_non_video_iframes(self):
        html = """
        <html><body>
          <iframe src="https://www.googletagmanager.com/ns.html?id=GTM-ABC"></iframe>
          <iframe src="https://www.youtube.com/embed/demo"></iframe>
        </body></html>
        """
        result = FetchResult(url="https://example.com/", status_code=200, headers={}, text=html)
        page = VideoExtractor().extract(result)
        media_urls = [video.media_url for video in page.videos]
        self.assertEqual(media_urls, ["https://www.youtube.com/embed/demo"])

    def test_extracts_browser_network_media_hints(self):
        result = FetchResult(
            url="https://example.com/watch/1",
            status_code=200,
            headers={"content-type": "text/html"},
            text="<html><title>Player</title><body><script>bootPlayer()</script></body></html>",
            media_hints=[
                MediaHint(
                    url="https://cdn.example.com/live/master.m3u8",
                    kind="hls_manifest",
                    source="browser.network",
                    content_type="application/vnd.apple.mpegurl",
                    status_code=200,
                    method="GET",
                    resource_type="xhr",
                )
            ],
        )
        page = VideoExtractor().extract(result)
        self.assertEqual(len(page.videos), 1)
        self.assertEqual(page.videos[0].media_url, "https://cdn.example.com/live/master.m3u8")
        self.assertEqual(page.videos[0].kind, "hls_manifest")
        self.assertEqual(page.videos[0].source, "browser.network")
        self.assertEqual(page.videos[0].metadata["content_type"], "application/vnd.apple.mpegurl")
        self.assertEqual(page.videos[0].metadata["resource_type"], "xhr")
        self.assertEqual(page.diagnostics["media_hint_count"], 1)
        self.assertEqual(page.diagnostics["media_hint_candidates"], 1)
        self.assertIn("network_media_hints_detected", page.diagnostics["notes"])

    def test_diagnostics_explain_empty_static_page(self):
        result = FetchResult(
            url="https://example.com/plain",
            status_code=200,
            headers={"content-type": "text/html"},
            text="<html><title>Plain</title><body>No player here</body></html>",
        )
        page = VideoExtractor().extract(result)
        self.assertEqual(page.videos, [])
        self.assertEqual(page.diagnostics["final_candidates"], 0)
        self.assertIn("no_video_tags_jsonld_media_urls_plugins_or_network_hints", page.diagnostics["notes"])
        self.assertIn("try_browser_fetcher_for_dynamic_players", page.diagnostics["notes"])
        self.assertIn("no_dynamic_media_signals_detected_check_scope_or_add_site_specific_plugin", page.diagnostics["recommendations"])

    def test_diagnostics_surface_dynamic_player_signals(self):
        html = """
        <html>
          <head><script src="/assets/hls.js"></script></head>
          <body>
            <script>
              const playerConfig = fetch('/api/player-config');
              const mediaSource = new MediaSource();
              videojs('player');
            </script>
          </body>
        </html>
        """
        result = FetchResult(
            url="https://example.com/watch/dynamic",
            status_code=200,
            headers={"content-type": "text/html"},
            text=html,
            fetcher="http",
        )
        page = VideoExtractor().extract(result)
        signals = page.diagnostics["dynamic_signals"]
        self.assertEqual(page.videos, [])
        self.assertGreater(signals["dynamic_score"], 0)
        self.assertEqual(signals["script_tags"], 2)
        self.assertIn("https://example.com/assets/hls.js", signals["player_script_urls"])
        self.assertIn("fetch_api", signals["dynamic_script_markers"])
        self.assertIn("media_source", signals["dynamic_script_markers"])
        self.assertIn("generic_player", signals["dynamic_script_markers"])
        self.assertIn(
            "rerun_with_browser_fetcher_for_dynamic_player_network_hints",
            page.diagnostics["recommendations"],
        )


if __name__ == "__main__":
    unittest.main()
