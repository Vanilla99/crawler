# VCrawl UI

Start the local console:

```bash
python3 -m vcrawl ui --config vcrawl.json --port 8765
```

Open:

```text
http://127.0.0.1:8765
```

Useful API endpoints:

```text
/api/summary
/api/videos?q=term&status=downloaded
/api/pages?q=term
/api/queue?status=pending
/api/runs
/api/downloads?status=queued&q=mp4
/api/workers
/api/logs
/api/timeline
/api/policies
/api/capabilities
/api/plugins
/api/archive
/api/cockpit
/api/config
/api/assistant
```

Control endpoints:

```text
POST /api/assistant/inspect        {"url": "https://example.com/watch", "browser": false}
POST /api/assistant/apply          {"url": "https://example.com/watch", "add_seed": true, "use_browser": true}
POST /api/control/start-crawl      {"max_pages": 100, "resume": true}
POST /api/control/resume-crawl     {"max_pages": 100}
POST /api/control/pause
POST /api/control/start-download   {"limit": 20}
POST /api/control/retry-downloads  {"limit": 20}
POST /api/control/retry-download   {"page_url": "https://example.com/watch", "media_url": "https://cdn.example.com/a.mp4"}
POST /api/control/skip-download    {"page_url": "https://example.com/watch", "media_url": "https://cdn.example.com/a.mp4"}
POST /api/control/clear-queue      {"status": "pending"}
POST /api/control/recover-queue    {"stale_after_seconds": 300}
POST /api/policies                 {"default_delay": 1, "auto_throttle": true, "domain": "example.com", "domain_delay": 2}
POST /api/plugins/test             {"builtin": "gallery"}
POST /api/plugins/test             {"path": "plugins/vcrawl-plugin.json"}
POST /api/archive/write
POST /api/archive/verify
POST /api/config                   {"text": "{...json...}"}
```

Pause is cooperative: the current fetch or download is allowed to finish, then the worker stops at the next loop boundary and records a `paused` status. Resume clears the pause flag and starts a new recoverable crawl worker.

The Assistant tab is the default first screen. It runs single-URL inspect checks, summarizes video candidates, media hints, dynamic-player signals, challenge state, and safe recommendations, then lets users save common config changes before starting a recoverable crawl. The apply action can add the inspected URL as a seed, add its domain to scope, enable browser fetching, HTTP cache, download queueing, browser profile, cookies file, and max depth. Starting a crawl from Assistant switches directly to Run Cockpit with live refresh enabled.

The Run Cockpit tab aggregates the current run, workers, queue counts, recent pages, recent video candidates, recent downloads, and recent failure signals from `/api/cockpit`. Failures are grouped into fetch error, challenge, no video, dynamic player, scope/robots, and download failed categories. Each group includes safe next-step advice and recent samples. Cockpit actions can retry failed downloads, recover stale queue rows, start the download worker, refresh archive sidecars, and verify the archive without sending users to separate tabs.

The Queue tab shows queue backend, stale timeout, queue state counts, and the filtered request list. `Recover Stale` sends `POST /api/control/recover-queue` and moves stale `in_progress` requests back to `pending` through the configured `QueueBackend`. This is intended for worker crashes or interrupted local sessions, not for bypassing site limits.

The Downloads tab shows queued, downloaded, failed, and skipped `DownloadTask` rows. Users can filter by status or text, inspect a task detail panel, retry one failed/skipped task, skip one queued/failed task, or start the download worker for the current queue.

The Pages tab shows fetched pages even when no video candidates were found. Each row includes status, fetcher, video count, challenge state, and extraction diagnostic notes. The detail panel summarizes dynamic score, browser media hints, embedded players, lazy media attributes, network hint kinds, script markers, player scripts, and recommendations. It also exposes the raw diagnostics JSON, including HTML candidate count, regex hits, plugin hits, browser/network media hints, duplicate candidates, final candidates, and suggested notes such as `try_browser_fetcher_for_dynamic_players`.

The Policies tab shows:

- configured global and per-domain crawl delay
- global/per-domain failure thresholds and current domain health
- dynamic AutoThrottle delay and average latency per domain
- per-session health for HTTP/browser/cache-backed fetches
- HTTP cache, cookie file, browser profile, and proxy configuration status
- editable controls for common strategy fields without opening the raw config editor

The Capabilities tab shows local capability packages, detailed capability groups, missing optional tools, install commands, copy buttons, next steps, and verification commands from the same report as `vcrawl doctor --fix-plan`. Packages include starter crawl, dynamic video discovery, media resolver and processing, distributed workers, and crawler framework bridges.

The Plugins tab lists built-in extractors and configured local plugins. It shows plugin capabilities, fixture counts, load errors for local manifests, and can run a single plugin's fixture tests before you use that plugin in a crawl.

The Archive tab shows archive configuration, manifest counts, sidecar/WARC file status, verification errors and warnings, and controls to refresh archive sidecars or run archive verification from the browser.

The Logs tab shows structured event records with phase, status, error class, worker, message, and URL. Turn on Live to poll recent logs every five seconds while the Logs tab is active.

The Timeline tab shows recent URL-level fetch, extract, download, archive, and policy events. Use the phase filter to narrow the view when debugging why a URL did not produce video candidates.
