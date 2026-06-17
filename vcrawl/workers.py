import json
import uuid

from .downloads import DownloadManager, DownloadOptions
from .engine import CrawlEngine
from .events import EventBus
from .models import DownloadTask, VideoCandidate
from .observability import JsonLogWriter, install_event_sinks, make_observation_record
from .storage import SQLiteStore


class CrawlWorker:
    def __init__(self, config, store=None, event_bus=None, worker_id=None):
        self.config = config
        self.store = store
        self.event_bus = event_bus or EventBus()
        self.worker_id = worker_id or _worker_id(config, "crawl")

    def run(self, max_pages=100, resume=True, on_event=None):
        owns_store = self.store is None
        store = self.store or SQLiteStore(self.config.storage.state)
        store.register_worker(self.worker_id, "crawl")
        install_event_sinks(self.event_bus, self.config)
        try:
            engine = CrawlEngine(
                self.config,
                store=store,
                event_bus=self.event_bus,
                should_stop=lambda: store.control_flag("paused", False),
            )
            stats = engine.crawl(
                max_pages=max_pages,
                resume=resume,
                on_event=_heartbeat_events(store, self.worker_id, "crawl", self.config, on_event),
            )
            status = "paused" if store.control_flag("paused", False) else "completed"
            store.finish_worker(self.worker_id, status)
            return stats
        except Exception as exc:
            store.finish_worker(self.worker_id, "failed", error=str(exc))
            raise
        finally:
            if owns_store:
                store.close()


class DownloadWorker:
    def __init__(self, config, store=None, event_bus=None, worker_id=None):
        self.config = config
        self.store = store
        self.event_bus = event_bus or EventBus()
        self.worker_id = worker_id or _worker_id(config, "download")

    def run_pending(self, limit=None, on_event=None):
        owns_store = self.store is None
        store = self.store or SQLiteStore(self.config.storage.state)
        store.register_worker(self.worker_id, "download")
        install_event_sinks(self.event_bus, self.config)
        wrapped_event = _heartbeat_events(store, self.worker_id, "download", self.config, on_event)
        try:
            rows = store.pending_downloads(limit=limit or self.config.media.max_downloads_per_run)
            tasks = [download_task_from_row(row) for row in rows]
            manager = DownloadManager(self._options(), store=store)
            if store.control_flag("paused", False):
                store.finish_worker(self.worker_id, "paused")
                return []
            for task in tasks[: limit or len(tasks)]:
                if store.control_flag("paused", False):
                    store.finish_worker(self.worker_id, "paused")
                    return []
                self._emit(wrapped_event, "download", task.media_url, url=task.media_url)
            results = manager.download_rows([row_from_task(task) for task in tasks], limit=limit)
            for result in results:
                event_type = "downloaded" if result.status == "downloaded" else "download_failed"
                self._emit(wrapped_event, event_type, result.media_url, url=result.media_url, error=result.error)
            store.finish_worker(self.worker_id, "completed")
            return results
        except Exception as exc:
            store.finish_worker(self.worker_id, "failed", error=str(exc))
            raise
        finally:
            if owns_store:
                store.close()

    def _options(self):
        return DownloadOptions(
            output_dir=self.config.storage.files,
            quality=self.config.media.quality,
            overwrite=self.config.media.overwrite,
            filename_template=self.config.media.filename_template,
            probe=self.config.media.probe,
            thumbnail=self.config.media.thumbnail,
            thumbnail_at_seconds=self.config.media.thumbnail_at_seconds,
            concurrency=self.config.media.download_concurrency,
            user_agent=self.config.fetch.user_agent,
        )

    def _emit(self, on_event, event_type, message="", url=None, **data):
        self.event_bus.emit(event_type, message=message, url=url, **data)
        if on_event:
            on_event(event_type, message or url or "")


def download_task_from_row(row):
    metadata = row.get("metadata_json") or "{}"
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    return DownloadTask(
        page_url=row["page_url"],
        media_url=row["media_url"],
        kind=row.get("kind") or "video",
        title=row.get("title"),
        source=row.get("source") or "store",
        metadata=metadata or {},
    )


def video_candidate_from_task(task):
    return VideoCandidate(
        page_url=task.page_url,
        media_url=task.media_url,
        kind=task.kind,
        title=task.title,
        source=task.source,
        metadata=task.metadata,
    )


def row_from_task(task):
    return {
        "page_url": task.page_url,
        "media_url": task.media_url,
        "kind": task.kind,
        "title": task.title,
        "source": task.source,
        "metadata_json": json.dumps(task.metadata or {}, sort_keys=True),
    }


def _worker_id(config, kind):
    configured = getattr(getattr(config, "worker", None), "worker_id", None)
    if configured:
        return "%s:%s" % (configured, kind)
    return "%s-%s" % (kind, uuid.uuid4().hex[:12])


def _heartbeat_events(store, worker_id, kind, config, on_event=None):
    json_writer = None
    if getattr(config.observability, "enabled", True) and getattr(config.observability, "json_logs", True):
        json_writer = JsonLogWriter(config.observability.json_log_path)

    def wrapped(event_type, message):
        current_url = message if event_type in ("fetch", "download", "download_queued", "page_extracted") else None
        record = make_observation_record(
            event_type,
            message=message,
            url=current_url,
            worker_id=worker_id,
            kind=kind,
            data={"kind": kind},
        )
        store.record_event(
            event_type,
            message=message,
            url=record["url"],
            worker_id=worker_id,
            data={
                "kind": kind,
                "phase": record["phase"],
                "status": record["status"],
                "severity": record["severity"],
                "error_class": record["error_class"],
            },
        )
        if getattr(config.observability, "enabled", True) and getattr(config.observability, "timeline", True):
            store.record_timeline(
                record["url"],
                record["phase"],
                record["status"],
                event_type,
                message=message,
                worker_id=worker_id,
                error_class=record["error_class"],
                data=record["data"],
            )
        if json_writer:
            json_writer.write(record)
        store.heartbeat_worker(
            worker_id,
            current_url=record["url"],
            stats={"last_event": event_type},
            status="running",
        )
        if on_event:
            on_event(event_type, message)

    return wrapped
