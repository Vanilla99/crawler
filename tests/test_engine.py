import os
import tempfile
import unittest
from unittest.mock import patch

from vcrawl.config import ArchiveConfig, FetchConfig, MediaConfig, ProjectConfig, ScopeConfig, StorageConfig
from vcrawl.engine import CrawlEngine
from vcrawl.models import FetchResult
from vcrawl.storage import SQLiteStore


class FakeFetcher:
    name = "fake"

    def __init__(self, pages):
        self.pages = pages

    def fetch(self, url):
        if url not in self.pages:
            return FetchResult(url=url, status_code=404, headers={}, text="", fetcher=self.name, error="missing")
        return FetchResult(url=url, status_code=200, headers={}, text=self.pages[url], fetcher=self.name)


class FlakyFetcher:
    name = "flaky"

    def __init__(self):
        self.calls = 0

    def fetch(self, url):
        self.calls += 1
        if self.calls == 1:
            return FetchResult(url=url, status_code=503, headers={}, text="", fetcher=self.name, error="temporary")
        return FetchResult(
            url=url,
            status_code=200,
            headers={},
            text="<html><video src='https://cdn.example.com/v.mp4'></video></html>",
            fetcher=self.name,
        )


class AlwaysFailFetcher:
    name = "fail"

    def __init__(self):
        self.calls = 0

    def fetch(self, url):
        self.calls += 1
        return FetchResult(url=url, status_code=503, headers={}, text="", fetcher=self.name, error="temporary")


class SessionFetcher:
    name = "http"

    def __init__(self):
        self.calls = 0

    def fetch(self, url):
        self.calls += 1
        return FetchResult(
            url=url,
            status_code=200,
            headers={},
            text="<html></html>",
            fetcher=self.name,
            session_id="http-%s" % self.calls,
        )


class EngineTests(unittest.TestCase):
    def test_crawls_scoped_links_and_records_videos(self):
        pages = {
            "https://example.com/index": """
                <html><a href="/watch/1">watch</a><a href="https://outside.test/watch/2">outside</a></html>
            """,
            "https://example.com/watch/1": """
                <html><title>Video</title><video src="https://cdn.example.com/v.mp4"></video></html>
            """,
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/index"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=2),
                fetch=FetchConfig(delay_per_domain_seconds=0),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
                archive=ArchiveConfig(root=os.path.join(tmp, "archive")),
            )
            store = SQLiteStore(config.storage.state)
            try:
                stats = CrawlEngine(config, store=store, fetcher=FakeFetcher(pages)).crawl(max_pages=10)
                self.assertEqual(stats.fetched, 2)
                self.assertEqual(stats.videos, 1)
                stored = store.list_videos()
                self.assertEqual(len(stored), 1)
                self.assertEqual(stored[0]["media_url"], "https://cdn.example.com/v.mp4")
                self.assertEqual(store.get_domain_state("example.com")["health"], "healthy")
                self.assertEqual(store.list_session_health()[0]["status"], "healthy")
                self.assertTrue(os.path.exists(os.path.join(tmp, "archive", "pages.jsonl")))
            finally:
                store.close()

    def test_records_fetch_result_session_id_in_session_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/a", "https://example.com/b"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0, respect_robots=False),
                fetch=FetchConfig(delay_per_domain_seconds=0),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            store = SQLiteStore(config.storage.state)
            try:
                CrawlEngine(config, store=store, fetcher=SessionFetcher()).crawl(max_pages=2, resume=True)
                sessions = {row["session_id"]: row for row in store.list_session_health(limit=10)}
                self.assertIn("http-1", sessions)
                self.assertIn("http-2", sessions)
                self.assertEqual(sessions["http-1"]["status"], "healthy")
            finally:
                store.close()

    def test_retries_transient_fetch_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/index"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0),
                fetch=FetchConfig(delay_per_domain_seconds=0, retries=1, retry_backoff_seconds=0),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            fetcher = FlakyFetcher()
            store = SQLiteStore(config.storage.state)
            try:
                stats = CrawlEngine(config, store=store, fetcher=fetcher).crawl(max_pages=1)
                self.assertEqual(fetcher.calls, 2)
                self.assertEqual(stats.fetched, 1)
                self.assertEqual(stats.failed, 0)
                self.assertEqual(stats.videos, 1)
            finally:
                store.close()

    def test_domain_failure_threshold_skips_later_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/a", "https://example.com/b"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0, respect_robots=False),
                fetch=FetchConfig(delay_per_domain_seconds=0, retries=0, domain_failure_threshold=1, concurrency=1),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            fetcher = AlwaysFailFetcher()
            store = SQLiteStore(config.storage.state)
            try:
                stats = CrawlEngine(config, store=store, fetcher=fetcher).crawl(max_pages=10, resume=True)
                self.assertEqual(fetcher.calls, 1)
                self.assertEqual(stats.failed, 1)
                self.assertEqual(stats.skipped, 1)
                self.assertEqual(store.queue_stats()["skipped"], 1)
                self.assertEqual(store.get_domain_state("example.com")["consecutive_failures"], 1)
            finally:
                store.close()

    def test_per_domain_failure_threshold_overrides_global_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/a", "https://example.com/b"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0, respect_robots=False),
                fetch=FetchConfig(
                    delay_per_domain_seconds=0,
                    retries=0,
                    domain_failure_threshold=5,
                    per_domain_failure_thresholds={"example.com": 1},
                    concurrency=1,
                ),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            fetcher = AlwaysFailFetcher()
            store = SQLiteStore(config.storage.state)
            try:
                stats = CrawlEngine(config, store=store, fetcher=fetcher).crawl(max_pages=10, resume=True)
                self.assertEqual(fetcher.calls, 1)
                self.assertEqual(stats.failed, 1)
                self.assertEqual(stats.skipped, 1)
                self.assertEqual(store.queue_stats()["skipped"], 1)
            finally:
                store.close()

    def test_per_domain_delay_overrides_default_delay(self):
        config = ProjectConfig(
            fetch=FetchConfig(
                delay_per_domain_seconds=0,
                per_domain_delay_seconds={"example.com": 2.0},
            )
        )
        engine = CrawlEngine(config)
        engine._last_fetch_by_domain["example.com"] = 100.0
        with patch("vcrawl.engine.time.time", return_value=101.0), patch("vcrawl.engine.time.sleep") as sleep:
            engine._polite_wait("https://example.com/page")
        sleep.assert_called_once_with(1.0)

    def test_auto_throttle_records_dynamic_domain_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/a"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0, respect_robots=False),
                fetch=FetchConfig(
                    delay_per_domain_seconds=0,
                    retries=0,
                    auto_throttle=True,
                    auto_throttle_target_concurrency=1.0,
                    auto_throttle_max_delay_seconds=10.0,
                ),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            store = SQLiteStore(config.storage.state)
            try:
                with patch("vcrawl.engine.time.monotonic", side_effect=[10.0, 12.0]):
                    CrawlEngine(config, store=store, fetcher=FakeFetcher({"https://example.com/a": "<html></html>"})).crawl(
                        max_pages=1,
                        resume=True,
                    )
                state = store.get_domain_state("example.com")
                self.assertAlmostEqual(state["avg_latency_ms"], 2000.0)
                self.assertAlmostEqual(state["dynamic_delay_seconds"], 1.0)
            finally:
                store.close()

    def test_auto_throttle_failure_increases_existing_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/a"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0, respect_robots=False),
                fetch=FetchConfig(
                    delay_per_domain_seconds=0,
                    retries=0,
                    auto_throttle=True,
                    auto_throttle_target_concurrency=10.0,
                    auto_throttle_max_delay_seconds=10.0,
                ),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            store = SQLiteStore(config.storage.state)
            try:
                store.record_domain_result("example.com", 200, dynamic_delay_seconds=2.0)
                with patch("vcrawl.engine.time.monotonic", side_effect=[10.0, 10.2]):
                    CrawlEngine(config, store=store, fetcher=AlwaysFailFetcher()).crawl(max_pages=1, resume=True)
                state = store.get_domain_state("example.com")
                self.assertAlmostEqual(state["dynamic_delay_seconds"], 4.0)
            finally:
                store.close()

    def test_crawl_with_media_download_queues_download_worker_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/index"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0, respect_robots=False),
                fetch=FetchConfig(delay_per_domain_seconds=0),
                media=MediaConfig(download=True),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite"), files=os.path.join(tmp, "downloads")),
                archive=ArchiveConfig(enabled=False),
            )
            store = SQLiteStore(config.storage.state)
            try:
                stats = CrawlEngine(
                    config,
                    store=store,
                    fetcher=FakeFetcher(
                        {"https://example.com/index": "<html><video src='https://cdn.example.com/a.mp4'></video></html>"}
                    ),
                ).crawl(max_pages=1, resume=True)
                self.assertEqual(stats.videos, 1)
                self.assertEqual(stats.download_queued, 1)
                self.assertEqual(stats.downloaded, 0)
                self.assertEqual(store.recent_downloads()[0]["status"], "queued")
                self.assertFalse(os.path.exists(config.storage.files))
            finally:
                store.close()

    def test_resume_mode_uses_persistent_queue(self):
        pages = {
            "https://example.com/index": """
                <html><a href="/watch/1">watch</a></html>
            """,
            "https://example.com/watch/1": """
                <html><video src="https://cdn.example.com/v.mp4"></video></html>
            """,
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/index"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=2, respect_robots=False),
                fetch=FetchConfig(delay_per_domain_seconds=0),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            store = SQLiteStore(config.storage.state)
            try:
                stats = CrawlEngine(config, store=store, fetcher=FakeFetcher(pages)).crawl(max_pages=10, resume=True)
                self.assertEqual(stats.fetched, 2)
                self.assertEqual(stats.videos, 1)
                queue = store.queue_stats()
                self.assertEqual(queue["fetched"], 2)
                self.assertEqual(len(store.recent_runs()), 1)
            finally:
                store.close()

    def test_resume_mode_can_pause_before_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                project="test",
                seeds=["https://example.com/index"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0, respect_robots=False),
                fetch=FetchConfig(delay_per_domain_seconds=0),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            store = SQLiteStore(config.storage.state)
            try:
                stats = CrawlEngine(
                    config,
                    store=store,
                    fetcher=FakeFetcher({"https://example.com/index": "<html></html>"}),
                    should_stop=lambda: True,
                ).crawl(max_pages=1, resume=True)
                self.assertEqual(stats.fetched, 0)
                self.assertEqual(store.recent_runs()[0]["status"], "paused")
                self.assertEqual(store.queue_stats()["pending"], 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
