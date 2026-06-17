from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class CrawlRequest:
    url: str
    depth: int = 0
    priority: int = 0
    referer: Optional[str] = None


@dataclass
class MediaHint:
    url: str
    kind: str = "media"
    source: str = "network"
    content_type: Optional[str] = None
    status_code: Optional[int] = None
    method: Optional[str] = None
    resource_type: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class FetchResult:
    url: str
    status_code: int
    headers: Dict[str, str]
    text: str
    final_url: Optional[str] = None
    fetcher: str = "http"
    error: Optional[str] = None
    challenge_detected: bool = False
    session_id: Optional[str] = None
    duration_ms: Optional[float] = None
    media_hints: List[MediaHint] = field(default_factory=list)


@dataclass
class VideoCandidate:
    page_url: str
    media_url: str
    kind: str
    title: Optional[str] = None
    source: str = "html"
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class ExtractedPage:
    url: str
    links: List[str] = field(default_factory=list)
    videos: List[VideoCandidate] = field(default_factory=list)
    title: Optional[str] = None
    challenge_detected: bool = False
    diagnostics: Dict[str, object] = field(default_factory=dict)


@dataclass
class CrawlStats:
    fetched: int = 0
    failed: int = 0
    skipped: int = 0
    challenge_pages: int = 0
    videos: int = 0
    download_queued: int = 0
    downloaded: int = 0
    download_failed: int = 0


@dataclass
class DownloadResult:
    page_url: str
    media_url: str
    status: str
    output_path: Optional[str] = None
    resolver: str = "builtin"
    error: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadTask:
    page_url: str
    media_url: str
    kind: str = "video"
    title: Optional[str] = None
    source: str = "store"
    metadata: Dict[str, str] = field(default_factory=dict)
    attempts: int = 0
