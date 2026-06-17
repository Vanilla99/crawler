import json
import os
import tempfile
import time
import unittest

from vcrawl.models import CrawlRequest, ExtractedPage, FetchResult
from vcrawl.models import DownloadResult, DownloadTask, VideoCandidate
from vcrawl.storage import SQLiteStore


class StorageTests(unittest.TestCase):
    def test_persistent_queue_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.enqueue_request(CrawlRequest(url="https://example.com/a", depth=0))
                store.enqueue_request(CrawlRequest(url="https://example.com/b", depth=1, priority=2))
                self.assertEqual(store.queue_stats()["pending"], 2)
                rows = store.next_queued_requests(limit=1)
                self.assertEqual(rows[0]["url"], "https://example.com/b")
                self.assertEqual(store.queue_stats()["in_progress"], 1)
                store.mark_queue_status(rows[0]["url"], "fetched")
                stats = store.queue_stats()
                self.assertEqual(stats["fetched"], 1)
                self.assertEqual(stats["pending"], 1)
            finally:
                store.close()

    def test_run_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                run_id = store.start_run(note="test")
                store.finish_run(run_id, "completed", {"fetched": 1})
                runs = store.recent_runs()
                self.assertEqual(runs[0]["status"], "completed")
                self.assertIn('"fetched": 1', runs[0]["stats_json"])
            finally:
                store.close()

    def test_worker_lifecycle_and_stale_queue_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.register_worker("worker-1", "crawl")
                store.heartbeat_worker("worker-1", current_url="https://example.com/a", stats={"fetched": 1})
                workers = store.list_workers()
                self.assertEqual(workers[0]["worker_id"], "worker-1")
                self.assertEqual(workers[0]["status"], "running")
                self.assertEqual(workers[0]["current_url"], "https://example.com/a")

                store.enqueue_request(CrawlRequest(url="https://example.com/stale", depth=0))
                store.next_queued_requests(limit=1)
                old = time.time() - 600
                store.conn.execute("UPDATE crawl_queue SET updated_at=? WHERE url=?", (old, "https://example.com/stale"))
                store.conn.commit()
                self.assertEqual(store.recover_stale_queue(300), 1)
                self.assertEqual(store.queue_stats()["pending"], 1)

                store.finish_worker("worker-1", "completed")
                self.assertEqual(store.list_workers()[0]["status"], "completed")
            finally:
                store.close()

    def test_search_videos_and_recent_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.record_page(
                    FetchResult(
                        url="https://example.com/watch",
                        status_code=200,
                        headers={"content-type": "text/html"},
                        text="<html></html>",
                        fetcher="http",
                    ),
                    ExtractedPage(
                        url="https://example.com/watch",
                        title="Demo Page",
                        diagnostics={"final_candidates": 1, "notes": ["html_video_found"]},
                    ),
                )
                store.record_videos(
                    [
                        VideoCandidate(
                            page_url="https://example.com/watch",
                            media_url="https://cdn.example.com/demo.mp4",
                            kind="source",
                            title="Demo Clip",
                            source="test",
                        )
                    ]
                )
                store.record_download(
                    DownloadResult(
                        page_url="https://example.com/watch",
                        media_url="https://cdn.example.com/demo.mp4",
                        status="downloaded",
                        output_path="/tmp/demo.mp4",
                    )
                )
                self.assertEqual(len(store.search_videos(query="Demo", download_status="downloaded")), 1)
                self.assertEqual(len(store.search_videos(query="missing")), 0)
                self.assertEqual(store.recent_downloads()[0]["status"], "downloaded")
                video = store.search_videos(query="Demo")[0]
                self.assertIn("page_diagnostics_json", video)
                self.assertIn("html_video_found", video["page_diagnostics_json"])
                page = store.list_pages(query="Demo")[0]
                self.assertEqual(page["video_count"], 1)
                self.assertEqual(json.loads(page["diagnostics_json"])["final_candidates"], 1)
            finally:
                store.close()

    def test_record_page_preserves_failed_fetch_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.record_page(
                    FetchResult(
                        url="https://example.com/fail",
                        status_code=0,
                        headers={},
                        text="",
                        fetcher="http",
                        error="timeout",
                    )
                )
                page = store.get_page("https://example.com/fail")
                diagnostics = json.loads(page["diagnostics_json"])
                self.assertIn("fetch_error", diagnostics["notes"])
                self.assertIn("empty_response_body", diagnostics["notes"])
            finally:
                store.close()

    def test_controls_events_queue_clear_and_retry_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.set_control("paused", True)
                self.assertTrue(store.control_flag("paused"))
                self.assertTrue(store.stats()["paused"])

                store.record_event("control", message="pause requested", worker_id="ui")
                events = store.list_events()
                self.assertEqual(events[0]["event_type"], "control")
                self.assertEqual(events[0]["message"], "pause requested")

                store.enqueue_request(CrawlRequest(url="https://example.com/pending", depth=0))
                self.assertEqual(store.clear_queue(status="pending"), 1)
                self.assertEqual(store.queue_stats(), {})

                store.record_videos(
                    [
                        VideoCandidate(
                            page_url="https://example.com/watch",
                            media_url="https://cdn.example.com/fail.mp4",
                            kind="source",
                            title="Failed Clip",
                            source="test",
                        )
                    ]
                )
                store.record_download(
                    DownloadResult(
                        page_url="https://example.com/watch",
                        media_url="https://cdn.example.com/fail.mp4",
                        status="failed",
                        error="boom",
                    )
                )
                self.assertEqual(store.retry_failed_downloads(), 1)
                self.assertEqual(store.recent_downloads()[0]["status"], "queued")

                queued = store.enqueue_download_task(
                    DownloadTask(
                        page_url="https://example.com/watch",
                        media_url="https://cdn.example.com/new.mp4",
                        kind="source",
                        title="Queued Clip",
                        metadata={"quality": "best"},
                    )
                )
                self.assertTrue(queued)
                self.assertEqual(store.stats()["queued_downloads"], 2)
                self.assertEqual(store.pending_downloads(limit=10)[0]["media_url"], "https://cdn.example.com/fail.mp4")

                store.record_download(
                    DownloadResult(
                        page_url="https://example.com/watch",
                        media_url="https://cdn.example.com/new.mp4",
                        status="downloaded",
                        output_path="/tmp/new.mp4",
                    )
                )
                self.assertFalse(
                    store.enqueue_download_task(
                        DownloadTask(
                            page_url="https://example.com/watch",
                            media_url="https://cdn.example.com/new.mp4",
                        )
                    )
                )
                self.assertEqual(store.recent_downloads()[0]["status"], "downloaded")
                self.assertEqual(len(store.list_downloads(status="queued")), 1)
                self.assertTrue(store.skip_download("https://example.com/watch", "https://cdn.example.com/fail.mp4"))
                self.assertEqual(store.get_download("https://example.com/watch", "https://cdn.example.com/fail.mp4")["status"], "skipped")
                self.assertTrue(store.retry_download("https://example.com/watch", "https://cdn.example.com/fail.mp4"))
                self.assertEqual(store.get_download("https://example.com/watch", "https://cdn.example.com/fail.mp4")["status"], "queued")
                self.assertEqual(len(store.list_downloads(query="new.mp4")), 1)
            finally:
                store.close()

    def test_domain_and_session_health_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.record_domain_result("example.com", 200, latency_ms=100, dynamic_delay_seconds=0.5)
                state = store.get_domain_state("example.com")
                self.assertEqual(state["health"], "healthy")
                self.assertEqual(state["successes"], 1)
                self.assertEqual(state["dynamic_delay_seconds"], 0.5)
                self.assertEqual(state["avg_latency_ms"], 100)

                store.record_domain_result(
                    "example.com",
                    503,
                    error="temporary",
                    latency_ms=300,
                    dynamic_delay_seconds=2.0,
                )
                state = store.get_domain_state("example.com")
                self.assertEqual(state["health"], "degraded")
                self.assertEqual(state["failures"], 1)
                self.assertEqual(state["consecutive_failures"], 1)
                self.assertEqual(state["dynamic_delay_seconds"], 2.0)
                self.assertAlmostEqual(state["avg_latency_ms"], 140.0)

                store.record_session_health(
                    "default-http",
                    kind="http",
                    status="healthy",
                    domain="example.com",
                    success=True,
                    metadata={"http_cache": True},
                )
                sessions = store.list_session_health()
                self.assertEqual(sessions[0]["session_id"], "default-http")
                self.assertEqual(sessions[0]["status"], "healthy")
                self.assertIn("http_cache", sessions[0]["metadata_json"])
            finally:
                store.close()

    def test_timeline_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                store.record_timeline(
                    "https://example.com/watch",
                    "fetch",
                    "started",
                    "fetch",
                    message="https://example.com/watch",
                    worker_id="worker-1",
                )
                store.record_timeline(
                    "https://example.com/watch",
                    "fetch",
                    "failed",
                    "fetch_failed",
                    message="timeout",
                    error_class="timeout",
                )
                rows = store.list_timeline(url="https://example.com/watch")
                self.assertEqual(rows[0]["status"], "failed")
                self.assertEqual(rows[0]["error_class"], "timeout")
                summary = store.timeline_summary()
                self.assertEqual(summary[0]["url"], "https://example.com/watch")
                self.assertEqual(summary[0]["has_error"], 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
