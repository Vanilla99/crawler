import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from vcrawl.cli import main
from vcrawl.models import FetchResult


class FakeDynamicFetcher:
    name = "fake"

    def __init__(self, *args, **kwargs):
        return None

    def fetch(self, url):
        return FetchResult(
            url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            text="""
            <html>
              <head><script src="/assets/hls.js"></script></head>
              <body>
                <script>
                  fetch('/api/player');
                  const source = new MediaSource();
                  videojs('player');
                </script>
              </body>
            </html>
            """,
            fetcher=self.name,
        )


class CliTests(unittest.TestCase):
    def test_inspect_text_prints_dynamic_diagnostic_summary(self):
        with patch("vcrawl.cli.HttpFetcher", FakeDynamicFetcher):
            with redirect_stdout(StringIO()) as stdout:
                code = main(["inspect", "https://example.com/watch"])
        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("diagnostic_summary:", output)
        self.assertIn("dynamic_script_markers:", output)
        self.assertIn("diagnostic_recommendations:", output)
        self.assertIn("rerun_with_browser_fetcher_for_dynamic_player_network_hints", output)

    def test_inspect_json_includes_diagnostic_summary(self):
        with patch("vcrawl.cli.HttpFetcher", FakeDynamicFetcher):
            with redirect_stdout(StringIO()) as stdout:
                code = main(["inspect", "https://example.com/watch", "--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["url"], "https://example.com/watch")
        self.assertEqual(payload["diagnostic_summary"]["final_candidates"], 0)
        self.assertGreater(payload["diagnostic_summary"]["dynamic_score"], 0)
        self.assertIn(
            "rerun_with_browser_fetcher_for_dynamic_player_network_hints",
            payload["diagnostics"]["recommendations"],
        )


if __name__ == "__main__":
    unittest.main()
