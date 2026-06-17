import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcrawl.config import (
    ArchiveConfig,
    FetchConfig,
    MediaConfig,
    ObservabilityConfig,
    ProjectConfig,
    QueueConfig,
    ScopeConfig,
    StorageConfig,
)
from vcrawl.events import EventBus, MemoryEventSink
from vcrawl.extractors import VideoExtractor
from vcrawl.models import CrawlRequest, CrawlStats, DownloadTask, VideoCandidate
from vcrawl.pipelines import PipelineContext, PipelineRunner
from vcrawl.queue_backends import InMemoryQueueBackend, PostgresQueueBackend, RedisQueueBackend, SQLiteQueueBackend, make_queue_backend
from vcrawl.stages import CrawlStageRunner
from vcrawl.storage import SQLiteStore
from vcrawl.workers import CrawlWorker, DownloadWorker
from vcrawl.models import FetchResult


class FakeFetcher:
    name = "fake"

    def __init__(self, pages):
        self.pages = pages

    def fetch(self, url):
        return FetchResult(url=url, status_code=200, headers={}, text=self.pages[url], fetcher=self.name)


class FakeRedis:
    def __init__(self):
        self.sets = {}
        self.lists = {}
        self.hashes = {}

    def sadd(self, key, value):
        values = self.sets.setdefault(key, set())
        before = len(values)
        values.add(value)
        return 1 if len(values) > before else 0

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lpop(self, key):
        values = self.lists.setdefault(key, [])
        if not values:
            return None
        return values.pop(0)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hdel(self, key, field):
        self.hashes.setdefault(key, {}).pop(field, None)

    def hgetall(self, key):
        return dict(self.hashes.setdefault(key, {}))


class FakePostgresCursor:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)


class FakePostgresConnection:
    def __init__(self):
        self.rows = {}
        self.commits = 0

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("create table") or normalized.startswith("create index"):
            return FakePostgresCursor()
        if normalized.startswith("insert into"):
            url, depth, priority, referer, created_at, updated_at = params
            existing = self.rows.get(url)
            if existing:
                existing["depth"] = min(existing["depth"], depth)
                existing["priority"] = max(existing["priority"], priority)
                existing["referer"] = existing["referer"] or referer
                existing["updated_at"] = updated_at
            else:
                self.rows[url] = {
                    "url": url,
                    "depth": depth,
                    "priority": priority,
                    "referer": referer,
                    "status": "pending",
                    "attempts": 0,
                    "error": None,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            return FakePostgresCursor(rowcount=1)
        if normalized.startswith("with claimed as"):
            limit, updated_at = params
            pending = [row for row in self.rows.values() if row["status"] == "pending"]
            pending.sort(key=lambda row: (-row["priority"], row["created_at"]))
            claimed = pending[:limit]
            for row in claimed:
                row["status"] = "in_progress"
                row["attempts"] += 1
                row["updated_at"] = updated_at
            return FakePostgresCursor(
                rows=[(row["url"], row["depth"], row["priority"], row["referer"]) for row in claimed],
                rowcount=len(claimed),
            )
        if "where status='in_progress' and updated_at <" in normalized:
            updated_at, cutoff = params
            recovered = 0
            for row in self.rows.values():
                if row["status"] == "in_progress" and row["updated_at"] < cutoff:
                    row["status"] = "pending"
                    row["error"] = None
                    row["updated_at"] = updated_at
                    recovered += 1
            return FakePostgresCursor(rowcount=recovered)
        if normalized.startswith("update") and "where url=%s" in normalized:
            status, error, updated_at, url = params
            row = self.rows.get(url)
            if row:
                row["status"] = status
                row["error"] = error
                row["updated_at"] = updated_at
                return FakePostgresCursor(rowcount=1)
            return FakePostgresCursor(rowcount=0)
        raise AssertionError("unexpected SQL: %s" % normalized)

    def commit(self):
        self.commits += 1


class KernelTests(unittest.TestCase):
    def test_event_bus_and_memory_sink(self):
        sink = MemoryEventSink()
        bus = EventBus([sink])
        bus.emit("fetch", "https://example.com", url="https://example.com")
        events = sink.list_events()
        self.assertEqual(events[0].type, "fetch")
        self.assertEqual(events[0].url, "https://example.com")

    def test_pipeline_dedupes_video_candidates(self):
        candidate = VideoCandidate(page_url="https://example.com", media_url="https://cdn.example.com/a.mp4", kind="source")
        runner = PipelineRunner()
        output = runner.process_videos([candidate, candidate], PipelineContext(page_url="https://example.com"))
        self.assertEqual(len(output), 1)

    def test_crawl_stage_runner_extracts_pipelines_stores_and_emits(self):
        class KeepOnlyMp4Pipeline:
            def process(self, candidate, context):
                if candidate.media_url.endswith("keep.mp4"):
                    return candidate
                return None

        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
                archive=ArchiveConfig(enabled=False),
            )
            store = SQLiteStore(config.storage.state)
            sink = MemoryEventSink()
            bus = EventBus([sink])
            try:
                runner = CrawlStageRunner(
                    config,
                    extractor=VideoExtractor(),
                    pipeline_runner=PipelineRunner([KeepOnlyMp4Pipeline()]),
                    store=store,
                    event_bus=bus,
                )
                result = FetchResult(
                    url="https://example.com/index",
                    status_code=200,
                    headers={},
                    text="""
                        <html>
                          <a href="/next">next</a>
                          <video src="https://cdn.example.com/keep.mp4"></video>
                          <video src="https://cdn.example.com/drop.webm"></video>
                        </html>
                    """,
                )
                outcome = runner.process(CrawlRequest(url="https://example.com/index"), result)
                self.assertEqual(outcome.fetched, 1)
                self.assertEqual(outcome.failed, 0)
                self.assertEqual(outcome.links, ["https://example.com/next"])
                self.assertEqual(len(outcome.videos), 1)
                self.assertEqual(store.list_videos()[0]["media_url"], "https://cdn.example.com/keep.mp4")
                self.assertIn("page_extracted", [event.type for event in sink.list_events()])
            finally:
                store.close()

    def test_crawl_stage_runner_queues_download_tasks_without_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite"), files=os.path.join(tmp, "downloads")),
                archive=ArchiveConfig(enabled=False),
                media=MediaConfig(download=True),
            )
            store = SQLiteStore(config.storage.state)
            sink = MemoryEventSink()
            bus = EventBus([sink])
            try:
                runner = CrawlStageRunner(
                    config,
                    extractor=VideoExtractor(),
                    pipeline_runner=PipelineRunner(),
                    store=store,
                    event_bus=bus,
                )
                result = FetchResult(
                    url="https://example.com/index",
                    status_code=200,
                    headers={},
                    text="<html><video src='https://cdn.example.com/queued.mp4'></video></html>",
                )
                outcome = runner.process(CrawlRequest(url="https://example.com/index"), result)
                self.assertEqual(outcome.download_queued, 1)
                self.assertEqual(outcome.downloaded, 0)
                self.assertEqual(store.recent_downloads()[0]["status"], "queued")
                self.assertFalse(os.path.exists(config.storage.files))
                event_types = [event.type for event in sink.list_events()]
                self.assertIn("download_queued", event_types)
                self.assertNotIn("download", event_types)
            finally:
                store.close()

    def test_queue_backends(self):
        memory = InMemoryQueueBackend(["https://example.com/a"])
        self.assertEqual(memory.next_batch(1)[0].url, "https://example.com/a")

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
            try:
                queue = SQLiteQueueBackend(store)
                request = CrawlRequest(url="https://example.com/b", depth=0)
                queue.enqueue(request)
                batch = queue.next_batch(1)
                self.assertEqual(batch[0].url, request.url)
                queue.mark_done(batch[0])
                self.assertEqual(store.queue_stats()["fetched"], 1)
            finally:
                store.close()

        fake = FakeRedis()
        redis_queue = RedisQueueBackend(client=fake)
        redis_queue.enqueue(CrawlRequest(url="https://example.com/c", depth=2, priority=1))
        redis_batch = redis_queue.next_batch(1)
        self.assertEqual(redis_batch[0].url, "https://example.com/c")
        redis_queue.mark_failed(redis_batch[0], "boom")
        self.assertEqual(fake.hashes[redis_queue.failed_key]["https://example.com/c"], "boom")

        postgres_queue = PostgresQueueBackend(connection=FakePostgresConnection())
        postgres_queue.enqueue(CrawlRequest(url="https://example.com/d", depth=3, priority=5))
        postgres_batch = postgres_queue.next_batch(1)
        self.assertEqual(postgres_batch[0].url, "https://example.com/d")
        postgres_queue.mark_done(postgres_batch[0])
        self.assertEqual(postgres_queue.connection.rows["https://example.com/d"]["status"], "fetched")

        config = ProjectConfig(queue=QueueConfig(backend="postgres", postgres_dsn="postgresql://example/db"))
        self.assertIsInstance(make_queue_backend(config, client=FakePostgresConnection()), PostgresQueueBackend)

    def test_redis_queue_recovers_stale_in_progress(self):
        fake = FakeRedis()
        queue = RedisQueueBackend(client=fake)
        queue.enqueue(CrawlRequest(url="https://example.com/stale", depth=1))
        batch = queue.next_batch(1)
        self.assertEqual(batch[0].url, "https://example.com/stale")
        for payload in fake.hashes[queue.in_progress_key].values():
            import json

            data = json.loads(payload)
            data["claimed_at"] = 1
            fake.hashes[queue.in_progress_key]["https://example.com/stale"] = json.dumps(data)
        self.assertEqual(queue.recover_stale(300), 1)
        self.assertEqual(queue.next_batch(1)[0].url, "https://example.com/stale")

    def test_postgres_queue_recovers_stale_in_progress(self):
        fake = FakePostgresConnection()
        queue = PostgresQueueBackend(connection=fake)
        queue.enqueue(CrawlRequest(url="https://example.com/stale", depth=1))
        batch = queue.next_batch(1)
        self.assertEqual(batch[0].url, "https://example.com/stale")
        fake.rows["https://example.com/stale"]["updated_at"] = 1
        self.assertEqual(queue.recover_stale(300), 1)
        self.assertEqual(queue.next_batch(1)[0].url, "https://example.com/stale")

    def test_crawl_worker_emits_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                seeds=["https://example.com/index"],
                scope=ScopeConfig(allowed_domains=["example.com"], max_depth=0, respect_robots=False),
                fetch=FetchConfig(delay_per_domain_seconds=0),
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
            )
            sink = MemoryEventSink()
            bus = EventBus([sink])
            store = SQLiteStore(config.storage.state)
            try:
                worker = CrawlWorker(config, store=store, event_bus=bus)
                worker_config_engine_fetcher = FakeFetcher(
                    {"https://example.com/index": "<html><video src='https://cdn.example.com/a.mp4'></video></html>"}
                )
                from vcrawl.engine import CrawlEngine

                stats = CrawlEngine(
                    config,
                    store=store,
                    fetcher=worker_config_engine_fetcher,
                    event_bus=bus,
                ).crawl(max_pages=1, resume=True)
                self.assertEqual(stats.videos, 1)
                self.assertIn("page_extracted", [event.type for event in sink.list_events()])
            finally:
                store.close()

    def test_crawl_worker_records_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                storage=StorageConfig(state=os.path.join(tmp, "state.sqlite")),
                observability=ObservabilityConfig(json_log_path=os.path.join(tmp, "events.jsonl")),
            )
            store = SQLiteStore(config.storage.state)

            class FakeEngine:
                def __init__(self, config, store=None, event_bus=None, **_kwargs):
                    self.store = store

                def crawl(self, max_pages=100, resume=True, on_event=None):
                    on_event("fetch", "https://example.com/current")
                    return CrawlStats(fetched=1)

            try:
                with patch("vcrawl.workers.CrawlEngine", FakeEngine):
                    stats = CrawlWorker(config, store=store, worker_id="worker-test").run(max_pages=1)
                self.assertEqual(stats.fetched, 1)
                workers = store.list_workers()
                self.assertEqual(workers[0]["worker_id"], "worker-test")
                self.assertEqual(workers[0]["status"], "completed")
                self.assertEqual(workers[0]["current_url"], "https://example.com/current")
                self.assertEqual(store.list_events()[0]["event_type"], "fetch")
                self.assertEqual(store.list_timeline()[0]["event_type"], "fetch")
                with open(config.observability.json_log_path, "r", encoding="utf-8") as fh:
                    row = json.loads(fh.readline())
                self.assertEqual(row["event_type"], "fetch")
                self.assertEqual(row["phase"], "fetch")
            finally:
                store.close()

    def test_download_worker_downloads_pending_file_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.mp4")
            with open(source, "wb") as fh:
                fh.write(b"demo")
            config = ProjectConfig(storage=StorageConfig(state=os.path.join(tmp, "state.sqlite"), files=os.path.join(tmp, "downloads")))
            store = SQLiteStore(config.storage.state)
            try:
                store.enqueue_download_task(
                    DownloadTask(
                        page_url="https://example.com/watch",
                        media_url=Path(source).as_uri(),
                        kind="source",
                        title="Demo",
                        source="test",
                    )
                )
                results = DownloadWorker(config, store=store).run_pending(limit=1)
                self.assertEqual(results[0].status, "downloaded")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
