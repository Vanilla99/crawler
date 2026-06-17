# VCrawl Design

## Positioning

`vcrawl` is a vertical crawler for video discovery and archiving workflows. The crawler entry point is the frontier, not a downloader. A run flows through:

```text
seed URLs -> scoped frontier -> fetcher -> extractor -> media resolver -> pipeline -> storage/export
```

## Open-Source Framework Capabilities Reflected

- Scrapy: scheduler/frontier, request metadata, middleware-like hooks, item pipelines.
- Crawlee: HTTP and browser fetchers under one crawler API, local dataset/key-value storage direction, optional Playwright.
- Colly: simple collector callbacks and fast single-binary mental model.
- Nutch/StormCrawler: seed-driven crawl, domain scope, depth, retry, politeness, future distributed frontier.
- pyspider: inspect-first debugging, task monitoring, future browser WebUI.
- yt-dlp: optional resolver for known media sites and embedded players.
- FFmpeg: optional post-processing for probe/transcode/merge.

## Human Verification Handling

The framework must not bypass CAPTCHA or human verification. Instead it provides:

- Challenge detection by markup, URL, title, and common provider markers.
- `challenge_detected` page status in SQLite.
- A clear recovery path: reduce concurrency, use official APIs, authenticate manually where appropriate, or resume after browser profile verification.
- Browser fetcher hooks for user-owned sessions without solving or outsourcing CAPTCHA.

## MVP Modules

- `frontier`: scoped URL queue, dedupe, depth, priority.
- `fetchers`: standard HTTP fetcher and optional Playwright browser fetcher.
- `extractors`: links, `<video>`, `<source>`, OpenGraph, Twitter player, JSON-LD `VideoObject`, and media URL regex.
- `resolvers`: built-in direct media resolver and optional yt-dlp command bridge.
- `storage`: SQLite pages, video candidates, and download state.
- `downloads`: direct URL downloader, yt-dlp command bridge, optional ffprobe metadata.
- `archive`: HTML snapshots, JSONL sidecars, media asset inventory, manifest generation, and archive verification.
- `discovery`: sitemap and RSS/Atom expansion before crawl startup.
- `robots`: cached `robots.txt` checks through Python's standard `robotparser`.
- `plugins`: built-in generic extractors plus manifest-based local Python plugins with capabilities, config schema, and fixture tests.
- `events`: internal event bus for fetch, retry, extract, download, and run lifecycle events.
- `observability`: structured JSON logs, per-URL timeline records, error taxonomy, and optional OpenTelemetry event sink.
- `pipelines`: item-style video candidate processing before storage/download.
- `stages`: fetch-result processing stages that turn responses into extracted pages, item pipeline output, storage/archive side effects, and optional `DownloadTask` queueing.
- `queue_backends`: queue abstraction with in-memory and SQLite implementations.
- `workers`: recoverable crawl and download worker entrypoints used by CLI/UI orchestration.
- `cli`: init, doctor, inspect, crawl, download, schedule, preview, export, profile, integrations, scaffold, ui.

## Runtime Kernel Direction

The core runtime is moving toward a Scrapy-inspired flow:

```text
CrawlRequest -> FetchResult -> ExtractedPage -> VideoCandidate -> DownloadTask -> DownloadResult
```

The engine owns crawl orchestration and scheduling. `CrawlStageRunner` owns the post-fetch stage boundary: `FetchResult` extraction, video item pipelines, page/video storage, archive writes, `DownloadTask` queueing, and stage-level events. Queue backends own request state, pipelines own item processing, workers own recoverable runnable jobs, and the event bus gives CLI/UI/telemetry one place to observe lifecycle events.

## Current Production-Oriented Features

- Politeness: global delay, per-domain delay overrides, retry/backoff, per-domain failure thresholds, optional AutoThrottle feedback, and optional `robots.txt` enforcement.
- Policy health: fetch results update domain health, dynamic delay, average latency, and per-session health so users can see consecutive failures, challenge pages, cache/session state, and configured cookie/profile/proxy posture in the UI. The Policies tab can edit common crawl strategy fields, and HTTP fetches rotate through the configured session pool size.
- Browser media hints: browser fetches listen to Playwright network responses and attach media-looking responses to `FetchResult.media_hints`. The extractor turns those hints into ordinary `VideoCandidate` rows with source `browser.network` and metadata such as status, method, resource type, and content type.
- Extraction diagnostics: every `ExtractedPage` carries structured diagnostics for HTML candidates, regex hits, plugin hits, media hints, duplicate collapse, final candidate count, challenge state, and debugging notes. SQLite stores the diagnostics on `pages`, and the UI Pages tab exposes them for URLs that produced no videos.
- Discovery: configured seed URLs can be expanded from sitemap and feed URLs.
- Resume: `crawl --resume` uses the configured persistent queue. SQLite is the default local backend; Redis and Postgres are optional shared multi-worker queue backends.
- Reporting: each crawl run is recorded in SQLite with a compact stats snapshot.
- Downloading: crawl runs queue `DownloadTask` rows; the download worker consumes queued tasks with configurable concurrency. yt-dlp is available for non-direct candidates when installed.
- Post-processing: optional ffprobe metadata and FFmpeg thumbnail generation.
- Archiving: page HTML snapshots and optional WARC response records are written during crawl; `archive` refreshes video/assets sidecars and `archive-verify` checks manifest, JSONL validity, WARC presence, and snapshot checksums.
- Extensibility: built-in generic plugins cover gallery, playlist, M3U8, and JSON-LD pages; local manifest plugins can declare capabilities, config schema, and fixture tests without modifying core code.
- Observability: workers record structured JSONL logs, URL timelines, and categorized errors; the UI and `logs`/`timeline` CLI commands expose the same state.
