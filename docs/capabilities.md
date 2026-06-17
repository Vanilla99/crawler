# Capability Guide

`vcrawl doctor` keeps the default check compact, and exposes richer guidance when requested.

```bash
python3 -m vcrawl doctor
python3 -m vcrawl doctor --capabilities
python3 -m vcrawl doctor --fix-plan
python3 -m vcrawl doctor --json
python3 -m vcrawl doctor --config vcrawl.json --json
```

Capability groups:

- Core crawl: built-in HTTP crawl, extraction, SQLite state, archive sidecars, and local UI.
- Dynamic pages: Playwright browser fetching and user-owned browser profiles.
- Known-site media resolver: yt-dlp metadata and media resolution.
- Media processing: ffprobe and FFmpeg post-processing.
- Crawler framework scaffolds: Scrapy and Crawlee integration scaffolds.
- Shared queues: Redis and Postgres queue backends for multi-worker crawls.
- YAML config: optional YAML config loading.
- WARC archive and plugins: built-in project capabilities.

Capability packages:

- Starter crawl: core local crawl, SQLite state, archive sidecars, and plugins.
- Dynamic video discovery: Playwright browser fetching and network media hints for JavaScript players.
- Media resolver and processing: yt-dlp, ffprobe, and FFmpeg workflows.
- Distributed workers: Redis/Postgres queue backends and long-running crawl recovery.
- Crawler framework bridges: Scrapy, Crawlee, Colly, and Nutch scaffolds.

The fix plan prints install commands for missing optional capabilities. It does not install anything automatically, so users can keep the default environment small and choose only the capability groups they need.

The UI exposes the same report in the Capabilities tab and `/api/capabilities`. The response includes `packages`, `capabilities`, `install_plan`, local `tools`, and config hints. The UI can copy install commands and shows a verification command for each package so users can confirm an optional capability after installing it.
