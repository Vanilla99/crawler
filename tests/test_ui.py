import json
import os
import tempfile
import unittest

from vcrawl.archive import ArchiveManager, ArchiveOptions
from vcrawl.config import ArchiveConfig, ExtractConfig, FetchConfig, ProjectConfig, QueueConfig, StorageConfig
from vcrawl.models import CrawlRequest, DownloadResult, ExtractedPage, FetchResult
from vcrawl.storage import SQLiteStore
from vcrawl.ui import (
    _apply_assistant_config,
    _archive_payload,
    _assistant_inspect_payload,
    _assistant_state,
    _cockpit_payload,
    _html_page,
    _plugin_payload,
    _policy_payload,
    _recover_queue,
    _run_plugin_test_payload,
    _update_policy_config,
)


class FakePolicyStore:
    def list_domain_state(self, limit=200):
        return []

    def list_session_health(self, limit=200):
        return []


class FakeDynamicFetcher:
    name = "fake"

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
                  fetch('/player/config');
                  const source = new MediaSource();
                  videojs('player');
                </script>
              </body>
            </html>
            """,
            fetcher=self.name,
        )


class UITests(unittest.TestCase):
    def test_dashboard_html_contains_core_surfaces(self):
        html = _html_page("demo")
        self.assertIn("Video crawler console", html)
        self.assertIn("data-tab=\"assistant\"", html)
        self.assertIn("Crawl Assistant", html)
        self.assertIn("assistant-url", html)
        self.assertIn("/api/assistant/inspect", html)
        self.assertIn("/api/assistant/apply", html)
        self.assertIn("data-tab=\"cockpit\"", html)
        self.assertIn("Run Cockpit", html)
        self.assertIn("/api/cockpit", html)
        self.assertIn("cockpit-live", html)
        self.assertIn("cockpit-retry-downloads", html)
        self.assertIn("cockpit-recover-stale", html)
        self.assertIn("data-tab=\"videos\"", html)
        self.assertIn("data-tab=\"pages\"", html)
        self.assertIn("data-tab=\"downloads\"", html)
        self.assertIn("/api/summary", html)
        self.assertIn("video-query", html)
        self.assertIn("Page Diagnostics", html)
        self.assertIn("page-query", html)
        self.assertIn("/api/pages", html)
        self.assertIn("Download Queue", html)
        self.assertIn("Download Tasks", html)
        self.assertIn("download-status", html)
        self.assertIn("Metadata", html)
        self.assertIn("/api/control/retry-download", html)
        self.assertIn("/api/control/skip-download", html)
        self.assertIn("queue-status", html)
        self.assertIn("recover-stale", html)
        self.assertIn("stale-seconds", html)
        self.assertIn("/api/control/recover-queue", html)
        self.assertIn("data-tab=\"workers\"", html)
        self.assertIn("data-tab=\"policies\"", html)
        self.assertIn("data-tab=\"capabilities\"", html)
        self.assertIn("data-tab=\"plugins\"", html)
        self.assertIn("data-tab=\"archive\"", html)
        self.assertIn("data-tab=\"timeline\"", html)
        self.assertIn("/api/workers", html)
        self.assertIn("/api/capabilities", html)
        self.assertIn("/api/plugins", html)
        self.assertIn("/api/plugins/test", html)
        self.assertIn("/api/archive", html)
        self.assertIn("/api/archive/write", html)
        self.assertIn("/api/archive/verify", html)
        self.assertIn("id=\"start-crawl\"", html)
        self.assertIn("id=\"pause-workers\"", html)
        self.assertIn("data-tab=\"logs\"", html)
        self.assertIn("logs-live", html)
        self.assertIn("Live", html)
        self.assertIn("logData(event).phase", html)
        self.assertIn("syncLogsLive", html)
        self.assertIn("data-tab=\"config\"", html)
        self.assertIn("/api/control/start-crawl", html)
        self.assertIn("/api/control/retry-downloads", html)
        self.assertIn("/api/logs", html)
        self.assertIn("/api/policies", html)
        self.assertIn("/api/timeline", html)
        self.assertIn("Policy Health", html)
        self.assertIn("Per-domain failure threshold", html)
        self.assertIn("Auto throttle", html)
        self.assertIn("id=\"save-policy\"", html)
        self.assertIn("/api/policies", html)
        self.assertIn("Capability Guide", html)
        self.assertIn("Capability Packages", html)
        self.assertIn("capability-packages-body", html)
        self.assertIn("data-copy-command", html)
        self.assertIn("Built-in Plugins", html)
        self.assertIn("Configured Plugins", html)
        self.assertIn("Archive Files", html)
        self.assertIn("URL Timeline", html)

    def test_plugin_payload_lists_builtins_and_configured_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_path = os.path.join(tmp, "site_plugin.py")
            with open(plugin_path, "w", encoding="utf-8") as fh:
                fh.write("def extract_videos(page_url, html, title=None):\n    return []\n")
            manifest_path = os.path.join(tmp, "vcrawl-plugin.json")
            with open(manifest_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "name": "demo-plugin",
                        "module": "site_plugin.py",
                        "capabilities": ["html", "direct-media"],
                        "fixtures": [
                            {
                                "name": "empty",
                                "page_url": "https://example.com/",
                                "html": "<html></html>",
                                "expected_media_urls": [],
                            }
                        ],
                    },
                    fh,
                )
            config = ProjectConfig(
                extract=ExtractConfig(
                    builtin_plugins=["gallery"],
                    plugin_paths=[manifest_path],
                )
            )
            payload = _plugin_payload(config)
            gallery = [item for item in payload["builtins"] if item["name"] == "gallery"][0]
            self.assertTrue(gallery["enabled"])
            self.assertEqual(payload["configured"][0]["name"], "demo-plugin")
            self.assertEqual(payload["configured"][0]["status"], "loaded")

    def test_run_plugin_test_payload_uses_builtin_fixtures(self):
        result = _run_plugin_test_payload({"builtin": "gallery"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["plugin"]["name"], "gallery")

    def test_archive_payload_reports_manifest_and_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = os.path.join(tmp, "archive")
            state_path = os.path.join(tmp, "state.sqlite")
            store = SQLiteStore(state_path)
            try:
                fetch_result = FetchResult(
                    url="https://example.com/watch",
                    status_code=200,
                    headers={},
                    text="<html><title>Demo</title></html>",
                    fetcher="fake",
                )
                extracted = ExtractedPage(url=fetch_result.url, title="Demo")
                manager = ArchiveManager(ArchiveOptions(root=archive_root, project="demo"))
                manager.write_page_snapshot(fetch_result, extracted)
                manager.write_sidecars(store)
                config = ProjectConfig(
                    archive=ArchiveConfig(root=archive_root),
                    storage=StorageConfig(state=state_path),
                )
                payload = _archive_payload(config, store)
            finally:
                store.close()
            self.assertTrue(payload["verification"]["ok"])
            self.assertTrue(payload["manifest"])
            self.assertEqual(payload["config"]["root"], archive_root)
            self.assertTrue(any(item["name"] == "manifest.json" and item["exists"] for item in payload["files"]))

    def test_policy_payload_includes_per_domain_failure_thresholds(self):
        config = ProjectConfig(
            fetch=FetchConfig(
                per_domain_delay_seconds={"example.com": 0.2},
                per_domain_failure_thresholds={"example.com": 1},
            )
        )
        payload = _policy_payload(config, FakePolicyStore())
        self.assertEqual(payload["fetch"]["per_domain_failure_thresholds"]["example.com"], 1)
        self.assertEqual(payload["fetch"]["per_domain_delay"]["example.com"], 0.2)

    def test_update_policy_config_edits_common_strategy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "vcrawl.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "project": "demo",
                        "fetch": {
                            "delay_per_domain_seconds": 1,
                            "domain_failure_threshold": 5,
                        },
                        "network": {
                            "proxy_url": "http://proxy.example:8080",
                            "http_cache": False,
                        },
                    },
                    fh,
                )

            config = _update_policy_config(
                config_path,
                {
                    "default_delay": "0.25",
                    "failure_threshold": "3",
                    "auto_throttle": True,
                    "auto_throttle_target_concurrency": "2.5",
                    "auto_throttle_min_delay": "0.1",
                    "auto_throttle_max_delay": "8",
                    "http_cache": True,
                    "http_cache_dir": ".vcrawl/cache",
                    "session_pool": True,
                    "session_pool_size": "4",
                    "cookies_file": ".vcrawl/cookies.txt",
                    "browser_profile": ".vcrawl/profile",
                    "domain": "https://Example.com:443/watch",
                    "domain_delay": "2.5",
                    "domain_failure_threshold": "1",
                },
            )

            self.assertEqual(config.fetch.delay_per_domain_seconds, 0.25)
            self.assertEqual(config.fetch.domain_failure_threshold, 3)
            self.assertTrue(config.fetch.auto_throttle)
            self.assertEqual(config.fetch.auto_throttle_target_concurrency, 2.5)
            self.assertEqual(config.fetch.auto_throttle_min_delay_seconds, 0.1)
            self.assertEqual(config.fetch.auto_throttle_max_delay_seconds, 8)
            self.assertEqual(config.fetch.per_domain_delay_seconds["example.com"], 2.5)
            self.assertEqual(config.fetch.per_domain_failure_thresholds["example.com"], 1)
            self.assertTrue(config.network.http_cache)
            self.assertEqual(config.network.session_pool_size, 4)
            with open(config_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self.assertEqual(raw["network"]["proxy_url"], "http://proxy.example:8080")

            _update_policy_config(
                config_path,
                {
                    "domain": "example.com",
                    "domain_delay": "",
                    "domain_failure_threshold": "",
                },
            )
            with open(config_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self.assertNotIn("example.com", raw["fetch"]["per_domain_delay_seconds"])
            self.assertNotIn("example.com", raw["fetch"]["per_domain_failure_thresholds"])

    def test_recover_queue_restores_stale_in_progress_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.sqlite")
            store = SQLiteStore(state_path)
            try:
                store.enqueue_request(CrawlRequest(url="https://example.com/stale"))
                claimed = store.next_queued_requests(limit=1)
                self.assertEqual(claimed[0]["url"], "https://example.com/stale")
                store.conn.execute(
                    "UPDATE crawl_queue SET updated_at=? WHERE url=?",
                    (0, "https://example.com/stale"),
                )
                store.conn.commit()
                config = ProjectConfig(
                    storage=StorageConfig(state=state_path),
                    queue=QueueConfig(stale_after_seconds=60),
                )
                payload = _recover_queue(config, store, {})
                row = store.list_queue(limit=1)[0]
            finally:
                store.close()
        self.assertEqual(payload["status"], "recovered")
        self.assertEqual(payload["recovered"], 1)
        self.assertEqual(payload["stale_after_seconds"], 60)
        self.assertEqual(row["status"], "pending")

    def test_cockpit_payload_groups_failures_and_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.sqlite")
            store = SQLiteStore(state_path)
            try:
                store.record_page(
                    FetchResult(
                        url="https://example.com/error",
                        status_code=500,
                        headers={},
                        text="",
                        fetcher="fake",
                        error="server error",
                    )
                )
                store.record_page(
                    FetchResult(
                        url="https://example.com/challenge",
                        status_code=200,
                        headers={},
                        text="",
                        fetcher="fake",
                        challenge_detected=True,
                    ),
                    ExtractedPage(url="https://example.com/challenge", challenge_detected=True),
                )
                store.record_page(
                    FetchResult(
                        url="https://example.com/dynamic",
                        status_code=200,
                        headers={},
                        text="",
                        fetcher="fake",
                    ),
                    ExtractedPage(
                        url="https://example.com/dynamic",
                        diagnostics={"dynamic_signals": {"dynamic_score": 4}},
                    ),
                )
                store.record_page(
                    FetchResult(
                        url="https://example.com/empty",
                        status_code=200,
                        headers={},
                        text="",
                        fetcher="fake",
                    ),
                    ExtractedPage(url="https://example.com/empty"),
                )
                store.record_download(
                    DownloadResult(
                        page_url="https://example.com/dynamic",
                        media_url="https://cdn.example.com/video.m3u8",
                        status="failed",
                        error="resolver failed",
                    )
                )
                store.enqueue_request(CrawlRequest(url="https://example.com/robots"))
                store.mark_queue_status("https://example.com/robots", "skipped", error="blocked by robots.txt")
                config = ProjectConfig(
                    storage=StorageConfig(state=state_path),
                    queue=QueueConfig(stale_after_seconds=45),
                )
                payload = _cockpit_payload(config, store)
            finally:
                store.close()

        by_category = {item["id"]: item for item in payload["failures"]["categories"]}
        self.assertGreaterEqual(by_category["fetch_error"]["count"], 1)
        self.assertGreaterEqual(by_category["challenge"]["count"], 1)
        self.assertGreaterEqual(by_category["no_video"]["count"], 1)
        self.assertGreaterEqual(by_category["dynamic_player"]["count"], 1)
        self.assertGreaterEqual(by_category["scope_robots"]["count"], 1)
        self.assertGreaterEqual(by_category["download_failed"]["count"], 1)
        self.assertEqual(payload["queue"]["stale_after_seconds"], 45)
        self.assertTrue(any(action["id"] == "retry_downloads" and action["enabled"] for action in payload["actions"]))

    def test_assistant_inspect_recommends_browser_for_dynamic_page(self):
        config = ProjectConfig()
        payload = _assistant_inspect_payload(
            config,
            {"url": "https://example.com/watch"},
            fetcher=FakeDynamicFetcher(),
        )
        self.assertEqual(payload["summary"]["videos"], 0)
        self.assertGreater(payload["summary"]["dynamic_score"], 0)
        recommendation_ids = [item["id"] for item in payload["recommendations"]]
        self.assertIn("use_browser_fetcher", recommendation_ids)
        self.assertTrue(payload["recommended_config"]["use_browser"])
        self.assertTrue(payload["recommended_config"]["enable_http_cache"])

    def test_assistant_apply_updates_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "vcrawl.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"project": "demo", "seeds": []}, fh)

            config = _apply_assistant_config(
                config_path,
                {
                    "url": "https://example.com/watch",
                    "add_seed": True,
                    "use_browser": True,
                    "enable_http_cache": True,
                    "enable_downloads": True,
                    "browser_profile": ".vcrawl/profile",
                    "cookies_file": ".vcrawl/cookies.txt",
                    "max_depth": "2",
                },
            )
            state = _assistant_state(config)
            with open(config_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)

        self.assertEqual(raw["seeds"], ["https://example.com/watch"])
        self.assertEqual(raw["scope"]["allowed_domains"], ["example.com"])
        self.assertEqual(raw["scope"]["max_depth"], 2)
        self.assertEqual(raw["fetch"]["default"], "browser")
        self.assertEqual(raw["fetch"]["browser_profile"], ".vcrawl/profile")
        self.assertTrue(raw["network"]["http_cache"])
        self.assertTrue(raw["media"]["download"])
        self.assertEqual(state["fetch"]["default"], "browser")


if __name__ == "__main__":
    unittest.main()
