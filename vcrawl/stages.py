from dataclasses import dataclass, field
from typing import List, Optional

from .archive import ArchiveManager, archive_options_from_config
from .challenges import challenge_message
from .events import EventBus
from .models import DownloadTask, ExtractedPage, VideoCandidate
from .pipelines import PipelineContext


@dataclass
class CrawlStageOutcome:
    extracted: Optional[ExtractedPage] = None
    links: List[str] = field(default_factory=list)
    videos: List[VideoCandidate] = field(default_factory=list)
    fetched: int = 0
    failed: int = 0
    challenge_pages: int = 0
    download_tasks: List[DownloadTask] = field(default_factory=list)
    download_queued: int = 0
    downloaded: int = 0
    download_failed: int = 0


class CrawlStageRunner:
    def __init__(
        self,
        config,
        extractor,
        pipeline_runner,
        store=None,
        event_bus=None,
        archive_manager=None,
    ):
        self.config = config
        self.extractor = extractor
        self.pipeline_runner = pipeline_runner
        self.store = store
        self.event_bus = event_bus or EventBus()
        if archive_manager is not None:
            self.archive_manager = archive_manager
        else:
            self.archive_manager = ArchiveManager(archive_options_from_config(config)) if config.archive.enabled else None

    def process(self, request, result, on_event=None):
        outcome = CrawlStageOutcome()
        extracted = None
        if result.error:
            outcome.failed = 1
            self._emit(on_event, "fetch_failed", "%s %s" % (request.url, result.error), url=request.url, error=result.error)
        else:
            extracted = self.extractor.extract(result)
            extracted.videos = self.pipeline_runner.process_videos(
                extracted.videos,
                PipelineContext(page_url=extracted.url, title=extracted.title or ""),
            )
            outcome.extracted = extracted
            outcome.links = list(extracted.links)
            outcome.videos = list(extracted.videos)
            outcome.fetched = 1
            self._emit(
                on_event,
                "page_extracted",
                extracted.url,
                url=extracted.url,
                links=len(extracted.links),
                videos=len(extracted.videos),
                diagnostics=extracted.diagnostics,
            )
            if extracted.challenge_detected:
                outcome.challenge_pages = 1
                self._emit(on_event, "challenge", challenge_message(extracted.url), url=extracted.url)
            if self.store:
                self.store.record_videos(extracted.videos)
            if self.config.media.download and extracted.videos:
                outcome.download_tasks = [_download_task_from_video(video) for video in extracted.videos]
                outcome.download_queued = self._queue_download_tasks(outcome.download_tasks, on_event)

        if self.store:
            self.store.record_page(result, extracted)
        self._archive_page(result, extracted, on_event)
        return outcome

    def _queue_download_tasks(self, tasks, on_event=None):
        if not self.store:
            return 0
        queued = 0
        for task in tasks:
            if self.store.enqueue_download_task(task):
                queued += 1
                self._emit(on_event, "download_queued", task.media_url, url=task.media_url)
        return queued

    def _archive_page(self, result, extracted=None, on_event=None):
        if not self.archive_manager:
            return
        record = self.archive_manager.write_page_snapshot(result, extracted)
        if record and record.get("html_path"):
            self._emit(
                on_event,
                "archived_page",
                record["html_path"],
                url=result.final_url or result.url,
                html_path=record["html_path"],
            )

    def _emit(self, on_event, event_type, message="", url=None, **data):
        self.event_bus.emit(event_type, message=message, url=url, **data)
        if on_event:
            on_event(event_type, message or url or "")


def _download_task_from_video(video):
    return DownloadTask(
        page_url=video.page_url,
        media_url=video.media_url,
        kind=video.kind,
        title=video.title,
        source=video.source,
        metadata=dict(video.metadata or {}),
    )
