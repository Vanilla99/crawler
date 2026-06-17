# Open-Source Crawler Integrations

`vcrawl` keeps the core install small, then exposes optional bridges to larger crawler ecosystems. These bridges focus on mainstream crawler capabilities: queues, throttling, retries, HTTP cache, cookies, user-provided proxies, session pools, pipelines, and distributed seed/filter workflows.

## Scrapy

Use Scrapy when the crawl needs mature scheduling, downloader middleware, spider middleware, item pipelines, feed exports, and a large Python ecosystem.

```bash
python3 -m pip install "scrapy>=2.16"
python3 -m vcrawl scaffold scrapy --config vcrawl.json --output integrations
cd integrations/scrapy
scrapy crawl video
```

Generated Scrapy settings include `ROBOTSTXT_OBEY`, retries, AutoThrottle, optional HTTP cache, cookies, default headers, feed export, and a static proxy middleware when `network.proxy_url` is configured.

## Crawlee Python

Use Crawlee when the crawl needs a high-level request queue with browser crawling and dataset-style outputs.

```bash
python3 -m pip install "crawlee[playwright]" playwright
python3 -m playwright install chromium
python3 -m vcrawl scaffold crawlee-python --config vcrawl.json --output integrations
cd integrations/crawlee-python
python3 main.py
```

Generated Crawlee code wires PlaywrightCrawler, session pool, concurrency settings, and `ProxyConfiguration` when `network.proxy_urls` or `network.proxy_url` is configured.

## Colly

Use Colly when you want a lightweight Go crawler with callback-style extraction, async visits, domain limits, and simple JSONL output.

```bash
python3 -m vcrawl scaffold colly --config vcrawl.json --output integrations
cd integrations/colly
go mod tidy
go run . > videos.jsonl
```

Generated Colly code includes allowed domains, depth limit, async mode, cookie jar, parallelism limit, and round-robin proxy switching when proxy URLs are configured.

## Apache Nutch

Use Nutch for large-scale, extensible crawls with a crawler database, segment-based fetch/parse/update cycles, and distributed deployment options.

```bash
python3 -m vcrawl scaffold nutch --config vcrawl.json --output integrations
cd integrations/nutch
```

The generated scaffold contains:

- `urls/seed.txt`
- `conf/regex-urlfilter.txt`
- `conf/nutch-site.xml`
- `README.md` with a minimal Nutch crawl flow

Nutch itself must be installed and configured separately.
The generated Nutch config includes agent, robots agent, fetch delay, timeout, and optional proxy host/port values.

## Playwright

Use the built-in browser fetcher for dynamic pages:

```bash
python3 -m pip install -e ".[browser]"
python3 -m playwright install chromium
python3 -m vcrawl inspect https://example.com/videos --browser
```

The browser fetcher also records media-looking network responses. HLS/DASH manifests and common video files discovered through player XHR/fetch requests are converted into normal `VideoCandidate` rows, so pipelines, storage, download queuing, and UI details work the same way as HTML-discovered media.

## Boundary

These integrations add mainstream crawler scheduling, browser automation, pipelines, datasets, session/cookie handling, user-provided proxy configuration, callback crawlers, and large-scale seed/filter workflows. They do not add CAPTCHA bypass, DRM bypass, paywall bypass, or account-risk evasion.

## yt-dlp and FFmpeg

`yt-dlp` is treated as a resolver for known video sites and embedded players. `FFmpeg` is treated as a post-processing tool. They are optional because a crawler should still discover and index video clues without downloading or transcoding.

```bash
python3 -m vcrawl doctor
python3 -m vcrawl integrations
python3 -m vcrawl resolve https://example.com/watch/123
python3 -m vcrawl download --config vcrawl.json --thumbnail
```

## Plugins

Built-in generic plugins are enabled by default for gallery links, playlist manifests, M3U8 pages, and JSON-LD video metadata:

```bash
python3 -m vcrawl plugin list-builtins
python3 -m vcrawl plugin test --builtin gallery
```

Generate a local extractor template:

```bash
python3 -m vcrawl plugin-template --output plugins/site_plugin.py
```

Then add the Python file, plugin manifest JSON, or plugin directory to `extract.plugin_paths` in `vcrawl.json`. Use `vcrawl plugin test --path <plugin>` to run manifest fixtures before using it in a crawl.

## Discovery

Add sitemap or feed URLs under `discovery.sitemaps` and `discovery.feeds`, then inspect expanded crawl seeds:

```bash
python3 -m vcrawl discover --config vcrawl.json
```

## Resume and Reports

For longer crawls, use the persistent queue:

```bash
python3 -m vcrawl crawl --config vcrawl.json --resume --max-pages 100
python3 -m vcrawl report --config vcrawl.json
python3 -m vcrawl export --config vcrawl.json --format csv
```

SQLite is the default local queue. For shared workers, set `queue.backend` to `redis` with `queue.redis_url`, or `postgres` with `queue.postgres_dsn` and optional `queue.postgres_table`. Install `vcrawl[redis]`, `vcrawl[postgres]`, or `vcrawl[distributed]` only when those backends are needed.
