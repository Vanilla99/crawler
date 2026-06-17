import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ScopeConfig:
    allowed_domains: List[str] = field(default_factory=list)
    max_depth: int = 2
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=lambda: ["/login", "/account"])
    respect_robots: bool = True


@dataclass
class FetchConfig:
    default: str = "http"
    timeout_seconds: float = 20.0
    concurrency: int = 1
    delay_per_domain_seconds: float = 1.0
    per_domain_delay_seconds: Dict[str, float] = field(default_factory=dict)
    domain_failure_threshold: int = 5
    per_domain_failure_thresholds: Dict[str, int] = field(default_factory=dict)
    user_agent: str = "vcrawl/0.1 (+https://example.invalid/vcrawl)"
    browser_headless: bool = True
    browser_profile: Optional[str] = None
    retries: int = 2
    retry_backoff_seconds: float = 1.0
    auto_throttle: bool = False
    auto_throttle_target_concurrency: float = 1.0
    auto_throttle_min_delay_seconds: float = 0.0
    auto_throttle_max_delay_seconds: float = 30.0


@dataclass
class NetworkConfig:
    proxy_url: Optional[str] = None
    proxy_urls: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    cookies_file: Optional[str] = None
    http_cache: bool = False
    http_cache_dir: Optional[str] = None
    session_pool: bool = True
    session_pool_size: int = 1


@dataclass
class QueueConfig:
    backend: str = "sqlite"
    redis_url: Optional[str] = None
    redis_key_prefix: str = "vcrawl"
    postgres_dsn: Optional[str] = None
    postgres_table: str = "vcrawl_crawl_queue"
    stale_after_seconds: int = 300


@dataclass
class WorkerConfig:
    worker_id: Optional[str] = None
    heartbeat_interval_seconds: int = 30


@dataclass
class ExtractConfig:
    mode: str = "video"
    builtin_plugins: List[str] = field(default_factory=lambda: ["gallery", "playlist", "m3u8", "jsonld"])
    plugin_paths: List[str] = field(default_factory=list)
    plugin_configs: Dict[str, Dict] = field(default_factory=dict)


@dataclass
class MediaConfig:
    resolver_order: List[str] = field(default_factory=lambda: ["builtin", "yt_dlp"])
    download: bool = False
    quality: str = "best"
    max_downloads_per_run: int = 100
    download_concurrency: int = 2
    overwrite: bool = False
    probe: bool = False
    thumbnail: bool = False
    thumbnail_at_seconds: int = 1
    filename_template: str = "{title_or_hash}.{ext}"


@dataclass
class DiscoveryConfig:
    sitemaps: List[str] = field(default_factory=list)
    feeds: List[str] = field(default_factory=list)
    max_discovered_urls: int = 1000


@dataclass
class ScheduleConfig:
    interval_seconds: int = 0
    max_runs: int = 1


@dataclass
class StorageConfig:
    state: str = ".vcrawl/state.sqlite"
    files: str = "downloads"
    metadata: str = "metadata.jsonl"


@dataclass
class ArchiveConfig:
    enabled: bool = True
    root: str = ".vcrawl/archive"
    html_snapshots: bool = True
    jsonl_sidecar: bool = True
    manifest: bool = True
    warc: bool = False


@dataclass
class ObservabilityConfig:
    enabled: bool = True
    json_logs: bool = True
    json_log_path: str = ".vcrawl/logs/events.jsonl"
    timeline: bool = True
    opentelemetry: bool = False
    service_name: str = "vcrawl"


@dataclass
class ProjectConfig:
    project: str = "video-crawl"
    seeds: List[str] = field(default_factory=list)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)


def _dataclass_from_dict(cls, data):
    allowed = set(cls.__dataclass_fields__.keys())
    return cls(**{key: value for key, value in data.items() if key in allowed})


def config_from_dict(data):
    data = data or {}
    return ProjectConfig(
        project=data.get("project", "video-crawl"),
        seeds=list(data.get("seeds", [])),
        scope=_dataclass_from_dict(ScopeConfig, data.get("scope", {})),
        fetch=_dataclass_from_dict(FetchConfig, data.get("fetch", {})),
        network=_dataclass_from_dict(NetworkConfig, data.get("network", {})),
        queue=_dataclass_from_dict(QueueConfig, data.get("queue", {})),
        worker=_dataclass_from_dict(WorkerConfig, data.get("worker", {})),
        extract=_dataclass_from_dict(ExtractConfig, data.get("extract", {})),
        media=_dataclass_from_dict(MediaConfig, data.get("media", {})),
        storage=_dataclass_from_dict(StorageConfig, data.get("storage", {})),
        archive=_dataclass_from_dict(ArchiveConfig, data.get("archive", {})),
        observability=_dataclass_from_dict(ObservabilityConfig, data.get("observability", {})),
        discovery=_dataclass_from_dict(DiscoveryConfig, data.get("discovery", {})),
        schedule=_dataclass_from_dict(ScheduleConfig, data.get("schedule", {})),
    )


def config_to_dict(config):
    return {
        "project": config.project,
        "seeds": config.seeds,
        "scope": {
            "allowed_domains": config.scope.allowed_domains,
            "max_depth": config.scope.max_depth,
            "include": config.scope.include,
            "exclude": config.scope.exclude,
            "respect_robots": config.scope.respect_robots,
        },
        "fetch": {
            "default": config.fetch.default,
            "timeout_seconds": config.fetch.timeout_seconds,
            "concurrency": config.fetch.concurrency,
            "delay_per_domain_seconds": config.fetch.delay_per_domain_seconds,
            "per_domain_delay_seconds": config.fetch.per_domain_delay_seconds,
            "domain_failure_threshold": config.fetch.domain_failure_threshold,
            "per_domain_failure_thresholds": config.fetch.per_domain_failure_thresholds,
            "user_agent": config.fetch.user_agent,
            "browser_headless": config.fetch.browser_headless,
            "browser_profile": config.fetch.browser_profile,
            "retries": config.fetch.retries,
            "retry_backoff_seconds": config.fetch.retry_backoff_seconds,
            "auto_throttle": config.fetch.auto_throttle,
            "auto_throttle_target_concurrency": config.fetch.auto_throttle_target_concurrency,
            "auto_throttle_min_delay_seconds": config.fetch.auto_throttle_min_delay_seconds,
            "auto_throttle_max_delay_seconds": config.fetch.auto_throttle_max_delay_seconds,
        },
        "network": {
            "proxy_url": config.network.proxy_url,
            "proxy_urls": config.network.proxy_urls,
            "headers": config.network.headers,
            "cookies_file": config.network.cookies_file,
            "http_cache": config.network.http_cache,
            "http_cache_dir": config.network.http_cache_dir,
            "session_pool": config.network.session_pool,
            "session_pool_size": config.network.session_pool_size,
        },
        "queue": {
            "backend": config.queue.backend,
            "redis_url": config.queue.redis_url,
            "redis_key_prefix": config.queue.redis_key_prefix,
            "postgres_dsn": config.queue.postgres_dsn,
            "postgres_table": config.queue.postgres_table,
            "stale_after_seconds": config.queue.stale_after_seconds,
        },
        "worker": {
            "worker_id": config.worker.worker_id,
            "heartbeat_interval_seconds": config.worker.heartbeat_interval_seconds,
        },
        "extract": {
            "mode": config.extract.mode,
            "builtin_plugins": config.extract.builtin_plugins,
            "plugin_paths": config.extract.plugin_paths,
            "plugin_configs": config.extract.plugin_configs,
        },
        "media": {
            "resolver_order": config.media.resolver_order,
            "download": config.media.download,
            "quality": config.media.quality,
            "max_downloads_per_run": config.media.max_downloads_per_run,
            "download_concurrency": config.media.download_concurrency,
            "overwrite": config.media.overwrite,
            "probe": config.media.probe,
            "thumbnail": config.media.thumbnail,
            "thumbnail_at_seconds": config.media.thumbnail_at_seconds,
            "filename_template": config.media.filename_template,
        },
        "storage": {
            "state": config.storage.state,
            "files": config.storage.files,
            "metadata": config.storage.metadata,
        },
        "archive": {
            "enabled": config.archive.enabled,
            "root": config.archive.root,
            "html_snapshots": config.archive.html_snapshots,
            "jsonl_sidecar": config.archive.jsonl_sidecar,
            "manifest": config.archive.manifest,
            "warc": config.archive.warc,
        },
        "observability": {
            "enabled": config.observability.enabled,
            "json_logs": config.observability.json_logs,
            "json_log_path": config.observability.json_log_path,
            "timeline": config.observability.timeline,
            "opentelemetry": config.observability.opentelemetry,
            "service_name": config.observability.service_name,
        },
        "discovery": {
            "sitemaps": config.discovery.sitemaps,
            "feeds": config.discovery.feeds,
            "max_discovered_urls": config.discovery.max_discovered_urls,
        },
        "schedule": {
            "interval_seconds": config.schedule.interval_seconds,
            "max_runs": config.schedule.max_runs,
        },
    }


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("YAML config requires optional dependency: pip install vcrawl[yaml]") from exc
        data = yaml.safe_load(raw) or {}
    else:
        data = json.loads(raw)
    base = os.path.dirname(os.path.abspath(path))
    config = config_from_dict(data)
    config.storage.state = _resolve_project_path(base, config.storage.state)
    config.storage.files = _resolve_project_path(base, config.storage.files)
    config.storage.metadata = _resolve_project_path(base, config.storage.metadata)
    config.archive.root = _resolve_project_path(base, config.archive.root)
    config.observability.json_log_path = _resolve_project_path(base, config.observability.json_log_path)
    if config.network.cookies_file:
        config.network.cookies_file = _resolve_project_path(base, config.network.cookies_file)
    if config.network.http_cache_dir:
        config.network.http_cache_dir = _resolve_project_path(base, config.network.http_cache_dir)
    if config.fetch.browser_profile:
        config.fetch.browser_profile = _resolve_project_path(base, config.fetch.browser_profile)
    config.extract.plugin_paths = [
        _resolve_project_path(base, path) if path else path
        for path in config.extract.plugin_paths
    ]
    return config


def save_config(config, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config_to_dict(config), fh, indent=2)
        fh.write("\n")


def _resolve_project_path(base, path):
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base, path))
