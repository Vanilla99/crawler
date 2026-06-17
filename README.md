# VCrawl

`vcrawl` is a Python-first MVP for video-oriented web crawling. It starts from seed URLs, discovers scoped links, fetches pages, extracts video clues, stores crawl state in SQLite, and exposes a small CLI.

The design borrows proven crawler patterns:

- Scrapy-style request scheduling, item pipelines, and pluggable components.
- Crawlee-style unified HTTP/browser fetching and local storage.
- Colly-style lightweight collector ergonomics.
- pyspider-style inspect/debug and future WebUI workflow.
- yt-dlp and FFmpeg are treated as optional media resolver/post-processing backends, not as the crawler entry point.

## Install

For local development:

```bash
python3 -m pip install -e .
```

The MVP has no required third-party runtime dependencies. Optional integrations:

```bash
python3 -m pip install -e ".[browser,yaml]"
python3 -m playwright install chromium
python3 -m pip install -e ".[distributed]"
```

Use the capability guide before installing heavier extras:

```bash
python3 -m vcrawl doctor --fix-plan
python3 -m vcrawl ui --config vcrawl.json
```

The UI Capabilities tab groups optional tooling into user-facing packages such as dynamic video discovery, media tools, distributed workers, and crawler framework bridges. It shows missing tools, install commands, copy buttons, and verification commands without installing anything automatically.

## Quick Start

```bash
python3 -m vcrawl doctor
python3 -m vcrawl init demo-project
cd demo-project
python3 -m vcrawl inspect https://www.w3schools.com/html/html5_video.asp
python3 -m vcrawl inspect https://www.w3schools.com/html/html5_video.asp --json
python3 -m vcrawl crawl --config vcrawl.json --max-pages 20
python3 -m vcrawl ui --config vcrawl.json
```

The UI opens on Crawl Assistant. Enter a seed URL, run Inspect, review the diagnostic summary and recommended next action, then save the suggested config and start the crawl from the same screen. Starting from Assistant switches to Run Cockpit so progress, current worker URLs, failure groups, video candidates, and recovery actions are visible immediately.

## Responsible Automation Boundary

`vcrawl` targets public web pages and pages the user can legitimately access. It does not implement CAPTCHA solving, human-verification bypass, DRM bypass, credential attacks, or anti-bot evasion. It detects common verification/challenge pages, records them, pauses affected requests, and tells the user how to resume after manual verification or configuration changes.

For reliability, `vcrawl` supports mainstream crawler configuration patterns such as polite delays, retries, HTTP cache, cookies, user-provided proxy settings, framework session pools, and persistent queues.

## Useful Commands

```bash
python3 -m vcrawl preview --config vcrawl.json
python3 -m vcrawl crawl --config vcrawl.json --max-pages 50
python3 -m vcrawl crawl --config vcrawl.json --max-pages 50 --resume
python3 -m vcrawl download --config vcrawl.json --limit 10 --concurrency 4
python3 -m vcrawl inspect <url> --browser
python3 -m vcrawl resolve <url>
python3 -m vcrawl discover --config vcrawl.json
python3 -m vcrawl doctor --fix-plan
python3 -m vcrawl integrations
python3 -m vcrawl plugin-template --output plugins/site_plugin.py
python3 -m vcrawl plugin list-builtins
python3 -m vcrawl plugin test --builtin gallery
python3 -m vcrawl plugin test --path plugins/vcrawl-plugin.json
python3 -m vcrawl scaffold scrapy --config vcrawl.json --output integrations
python3 -m vcrawl scaffold crawlee-python --config vcrawl.json --output integrations
python3 -m vcrawl scaffold colly --config vcrawl.json --output integrations
python3 -m vcrawl scaffold nutch --config vcrawl.json --output integrations
python3 -m vcrawl export --config vcrawl.json --format jsonl
python3 -m vcrawl export --config vcrawl.json --format csv
python3 -m vcrawl archive --config vcrawl.json --verify
python3 -m vcrawl archive-verify --config vcrawl.json
python3 -m vcrawl report --config vcrawl.json
python3 -m vcrawl logs --config vcrawl.json --limit 50
python3 -m vcrawl timeline --config vcrawl.json --limit 100
python3 -m vcrawl ui --config vcrawl.json --port 8765
python3 -m vcrawl schedule --config vcrawl.json --interval 3600 --max-runs 3
python3 -m vcrawl profile --path .vcrawl/browser-profile
python3 -m vcrawl test-matrix --layer long-run --long-run-size 1000
```

When `media.download` is enabled, crawl runs queue `DownloadTask` rows for discovered video candidates. The separate `download` worker consumes those queued tasks and records `downloaded` or `failed` results, so crawl and media transfer remain recoverable independently. The UI Downloads tab can filter tasks, inspect details, retry one failed/skipped item, or skip one queued/failed item.

`inspect` and the UI Pages tab expose extraction diagnostics for every fetched page, including HTML candidates, regex hits, plugin hits, browser/network media hints, dynamic player signals, duplicate candidates, final candidates, challenge state, recommendations, and notes such as `try_browser_fetcher_for_dynamic_players`. `inspect` prints a compact diagnostic summary by default, and `inspect --json` returns the same fields in a structured payload. This is the main debugging path for pages that fetch successfully but do not produce video candidates.

The UI Plugins tab lists built-in and configured local plugins, shows declared capabilities and fixture counts, reports local plugin load errors, and can run fixture tests from the browser before a plugin is used in a crawl.

The UI Assistant combines the inspect path, capability packages, dynamic-video diagnostics, and common config toggles. It can add the inspected URL as a seed, set allowed domain scope, enable browser fetching, enable HTTP cache, enable download queueing, and start a recoverable crawl. The Run Cockpit view then aggregates run status, workers, queue state, recent pages, recent videos, recent downloads, safe failure-group advice, and one-click recovery/download/archive actions.

## Long Running Crawls

`crawl --resume` uses a persistent queue. The default backend is SQLite, which is still the easiest local setup:

```json
{
  "queue": {
    "backend": "sqlite",
    "stale_after_seconds": 300
  },
  "worker": {
    "worker_id": "laptop-1",
    "heartbeat_interval_seconds": 30
  }
}
```

For multi-process or multi-machine experiments, use an optional shared queue backend. Redis is compact and fast for crawler-style queues; Postgres is useful when the crawl platform already runs on a relational database:

```json
{
  "queue": {
    "backend": "postgres",
    "postgres_dsn": "postgresql://vcrawl:vcrawl@localhost:5432/vcrawl",
    "postgres_table": "vcrawl_crawl_queue",
    "stale_after_seconds": 300
  }
}
```

Set `queue.backend` to `redis` with `queue.redis_url`, or to `postgres` with `queue.postgres_dsn`. These backends are optional; install `vcrawl[redis]`, `vcrawl[postgres]`, or `vcrawl[distributed]` only when you need shared queue state. The local UI exposes worker heartbeat and current URL state at `/api/workers`, and the Queue tab can recover stale `in_progress` requests back to `pending` after the configured lease window.

## Crawl Policies

The default policy is conservative and local-first. You can tune global delay, per-domain delay, failure threshold, HTTP cache, HTTP session pool size, cookies, browser profile, and user-provided proxy settings in `vcrawl.json`:

```json
{
  "fetch": {
    "delay_per_domain_seconds": 1.0,
    "per_domain_delay_seconds": {
      "example.com": 2.0
    },
    "domain_failure_threshold": 5,
    "per_domain_failure_thresholds": {
      "fragile.example": 1
    },
    "auto_throttle": true,
    "auto_throttle_target_concurrency": 1.0,
    "auto_throttle_min_delay_seconds": 0.0,
    "auto_throttle_max_delay_seconds": 30.0
  },
  "network": {
    "http_cache": true,
    "http_cache_dir": ".vcrawl/http-cache",
    "cookies_file": ".vcrawl/cookies.txt",
    "session_pool": true,
    "session_pool_size": 1,
    "proxy_url": null
  }
}
```

The UI Policies tab and `/api/policies` show domain health, consecutive failures, per-domain policy overrides, dynamic AutoThrottle delay, average latency, session health, HTTP cache state, and whether cookies/profile/proxy settings are configured. The same tab can save common strategy fields such as delay, failure thresholds, AutoThrottle, cache, cookies, browser profile, and session pool settings.

## Browser Media Hints

Static HTML is often not enough for modern video pages because playlists and media files are loaded through player XHR/fetch requests. When `inspect --browser` or `fetch.default = "browser"` is used, `vcrawl` listens to browser network responses and converts media-looking responses into normal `VideoCandidate` rows. It recognizes common video files plus HLS and DASH manifests by URL and `Content-Type`, then preserves status, method, resource type, and MIME metadata for debugging.

When no video is found, diagnostics now record dynamic signals such as player script URLs, fetch/XHR/MediaSource markers, embedded player iframes, lazy media attributes, and browser media hint counts. The UI turns those signals into safe next actions such as trying the browser fetcher, adding a site plugin, checking click/scroll flows, or using a user-owned browser profile after manual verification.

## Archive Output

`vcrawl` writes lightweight archive artifacts under `.vcrawl/archive` by default:

- `pages/<domain>/<hash>.html`: HTML snapshots captured during crawl.
- `pages.jsonl`: page-level sidecar records with URL, status, title, video count, and snapshot checksum.
- `videos.jsonl`: stored video candidate records.
- `assets.jsonl`: downloaded media, thumbnails, subtitles, and manifest-style assets when present.
- `archive.warc`: WARC/1.1 response records for fetched HTML pages when `archive.warc` is enabled.
- `manifest.json`: counts, enabled formats, and archive metadata.

Run `python3 -m vcrawl archive --config vcrawl.json --verify` after a crawl to refresh sidecars and verify snapshots. The UI Archive tab exposes the same refresh and verify workflow with manifest counts, file status, and verification errors/warnings. WARC output is standard-library friendly; WACZ packaging is not emitted yet.

Integration notes live in `docs/integrations.md`.
UI notes live in `docs/ui.md`.
Archive notes live in `docs/archive.md`.
Capability notes live in `docs/capabilities.md`.
Plugin notes live in `docs/plugins.md`.
Observability notes live in `docs/observability.md`.
Test matrix notes live in `docs/test-matrix.md`.

Run tests:

```bash
python3 -m unittest discover -s tests
python3 -m vcrawl test-matrix --layer unit
```
