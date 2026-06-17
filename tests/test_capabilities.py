import json
import unittest
from contextlib import redirect_stdout
from io import StringIO

from vcrawl.capabilities import build_capability_report, format_capability_report
from vcrawl.cli import main
from vcrawl.config import ArchiveConfig, FetchConfig, NetworkConfig, ProjectConfig, QueueConfig


class CapabilityTests(unittest.TestCase):
    def test_builds_capability_report_from_diagnostics(self):
        diagnostics = [
            ("python", "3.9", True),
            ("sqlite3", "stdlib", True),
            ("playwright", "not installed", False),
            ("yt-dlp", "installed", True),
            ("ffmpeg", "not found", False),
            ("ffprobe", "not found", False),
            ("scrapy", "not installed", False),
            ("crawlee", "not installed", False),
            ("redis", "not installed", False),
            ("postgres-driver", "psycopg installed", True),
            ("yaml", "installed", True),
        ]
        config = ProjectConfig(
            fetch=FetchConfig(default="browser", browser_profile=".vcrawl/profile"),
            network=NetworkConfig(http_cache=True, proxy_url="http://127.0.0.1:8080"),
            queue=QueueConfig(backend="postgres"),
            archive=ArchiveConfig(warc=True),
        )
        report = build_capability_report(config=config, diagnostics=diagnostics)
        by_id = {item["id"]: item for item in report["capabilities"]}
        packages = {item["id"]: item for item in report["packages"]}
        self.assertEqual(by_id["core-crawl"]["status"], "available")
        self.assertEqual(by_id["browser-pages"]["status"], "missing")
        self.assertEqual(by_id["media-resolver"]["status"], "available")
        self.assertEqual(by_id["media-processing"]["status"], "missing")
        self.assertEqual(by_id["postgres-queue"]["status"], "available")
        self.assertEqual(packages["dynamic-video"]["status"], "missing")
        self.assertIn("playwright", packages["dynamic-video"]["missing"])
        self.assertTrue(packages["dynamic-video"]["install_commands"])
        self.assertEqual(packages["distributed-workers"]["status"], "partial")
        self.assertTrue(report["config"]["archive_warc"])
        self.assertTrue(any("playwright install chromium" in step["command"] for step in report["install_plan"]))

    def test_formats_install_plan(self):
        report = build_capability_report(
            diagnostics=[
                ("python", "3.9", True),
                ("sqlite3", "stdlib", True),
                ("playwright", "not installed", False),
            ]
        )
        text = format_capability_report(report, include_install_plan=True)
        self.assertIn("Capabilities:", text)
        self.assertIn("Install plan:", text)
        self.assertIn("Capability packages:", text)

    def test_doctor_json_cli(self):
        with redirect_stdout(StringIO()) as stdout:
            code = main(["doctor", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIn("capabilities", payload)
        self.assertIn("install_plan", payload)

    def test_doctor_fix_plan_cli(self):
        with redirect_stdout(StringIO()) as stdout:
            code = main(["doctor", "--fix-plan"])
        self.assertEqual(code, 0)
        self.assertIn("Capabilities:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
