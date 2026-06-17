import json
import os
import shutil

from .config import config_to_dict


def integration_report():
    return [
        {
            "name": "builtin",
            "kind": "core",
            "available": True,
            "capability": "frontier, scoped crawl, HTTP fetcher, video extractor, SQLite store",
        },
        {
            "name": "scrapy",
            "kind": "python-framework",
            "available": _module_available("scrapy"),
            "capability": "scheduler, downloader middleware, spider middleware, item pipelines, exporters",
        },
        {
            "name": "crawlee-python",
            "kind": "python-framework",
            "available": _module_available("crawlee"),
            "capability": "request queue, HTTP/browser crawlers, datasets, Playwright integration",
        },
        {
            "name": "colly",
            "kind": "go-framework",
            "available": shutil.which("go") is not None,
            "capability": "fast callback crawler, async requests, domain limits, URL filters",
        },
        {
            "name": "nutch",
            "kind": "java-distributed-crawler",
            "available": shutil.which("nutch") is not None,
            "capability": "large-scale extensible crawler, seeds, URL filters, parse/index pipelines",
        },
        {
            "name": "playwright",
            "kind": "browser-automation",
            "available": _module_available("playwright"),
            "capability": "dynamic rendering, scroll/click flows, user-controlled browser profiles",
        },
        {
            "name": "yt-dlp",
            "kind": "media-resolver",
            "available": shutil.which("yt-dlp") is not None,
            "capability": "site extractors and media metadata resolution",
        },
        {
            "name": "ffmpeg",
            "kind": "media-processing",
            "available": shutil.which("ffmpeg") is not None,
            "capability": "transcode, merge, thumbnail, probe workflows",
        },
    ]


def scaffold_scrapy(config, output_dir):
    project = _safe_name(config.project or "vcrawl_project")
    root = os.path.abspath(output_dir)
    package_dir = os.path.join(root, project)
    spiders_dir = os.path.join(package_dir, "spiders")
    os.makedirs(spiders_dir, exist_ok=True)
    _write(os.path.join(root, "scrapy.cfg"), _scrapy_cfg(project))
    _write(os.path.join(package_dir, "__init__.py"), "")
    _write(os.path.join(spiders_dir, "__init__.py"), "")
    _write(os.path.join(package_dir, "items.py"), _scrapy_items())
    _write(os.path.join(package_dir, "middlewares.py"), _scrapy_middlewares())
    _write(os.path.join(package_dir, "pipelines.py"), _scrapy_pipeline())
    _write(os.path.join(package_dir, "settings.py"), _scrapy_settings(project, config))
    _write(os.path.join(spiders_dir, "video_spider.py"), _scrapy_spider(config))
    _write(os.path.join(root, "vcrawl_config.json"), json.dumps(config_to_dict(config), indent=2))
    return root


def scaffold_crawlee_python(config, output_dir):
    root = os.path.abspath(output_dir)
    os.makedirs(root, exist_ok=True)
    _write(os.path.join(root, "requirements.txt"), "crawlee[playwright]\nplaywright\n")
    _write(os.path.join(root, "main.py"), _crawlee_main(config))
    _write(os.path.join(root, "vcrawl_config.json"), json.dumps(config_to_dict(config), indent=2))
    return root


def scaffold_colly(config, output_dir):
    root = os.path.abspath(output_dir)
    os.makedirs(root, exist_ok=True)
    _write(os.path.join(root, "go.mod"), _colly_go_mod(config))
    _write(os.path.join(root, "main.go"), _colly_main(config))
    _write(os.path.join(root, "README.md"), _colly_readme())
    _write(os.path.join(root, "vcrawl_config.json"), json.dumps(config_to_dict(config), indent=2))
    return root


def scaffold_nutch(config, output_dir):
    root = os.path.abspath(output_dir)
    seed_dir = os.path.join(root, "urls")
    conf_dir = os.path.join(root, "conf")
    os.makedirs(seed_dir, exist_ok=True)
    os.makedirs(conf_dir, exist_ok=True)
    _write(os.path.join(seed_dir, "seed.txt"), "\n".join(config.seeds))
    _write(os.path.join(conf_dir, "regex-urlfilter.txt"), _nutch_regex_urlfilter(config))
    _write(os.path.join(conf_dir, "nutch-site.xml"), _nutch_site_xml(config))
    _write(os.path.join(root, "README.md"), _nutch_readme(config))
    _write(os.path.join(root, "vcrawl_config.json"), json.dumps(config_to_dict(config), indent=2))
    return root


def _module_available(name):
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _safe_name(value):
    return "".join(ch if ch.isalnum() else "_" for ch in value).strip("_").lower() or "vcrawl_project"


def _write(path, content):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
        if content and not content.endswith("\n"):
            fh.write("\n")


def _scrapy_cfg(project):
    return """[settings]
default = {project}.settings
""".format(project=project)


def _scrapy_items():
    return """import scrapy


class VideoCandidateItem(scrapy.Item):
    page_url = scrapy.Field()
    media_url = scrapy.Field()
    kind = scrapy.Field()
    title = scrapy.Field()
    source = scrapy.Field()
"""


def _scrapy_pipeline():
    return """from scrapy.exceptions import DropItem


class DedupeVideoPipeline:
    def __init__(self):
        self.seen = set()

    def process_item(self, item, spider):
        key = (item.get("page_url"), item.get("media_url"))
        if key in self.seen:
            raise DropItem("duplicate video candidate")
        self.seen.add(key)
        return item
"""


def _scrapy_middlewares():
    return """class StaticProxyMiddleware:
    def process_request(self, request, spider):
        proxy_url = spider.settings.get("STATIC_PROXY_URL")
        if proxy_url:
            request.meta["proxy"] = proxy_url
"""


def _scrapy_settings(project, config):
    proxy = json.dumps(config.network.proxy_url or "")
    cache_enabled = "True" if config.network.http_cache else "False"
    return """BOT_NAME = "{project}"
SPIDER_MODULES = ["{project}.spiders"]
NEWSPIDER_MODULE = "{project}.spiders"
ROBOTSTXT_OBEY = True
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DOWNLOAD_DELAY = 1
RETRY_ENABLED = True
RETRY_TIMES = 2
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1
AUTOTHROTTLE_MAX_DELAY = 30
HTTPCACHE_ENABLED = {cache_enabled}
COOKIES_ENABLED = True
DEFAULT_REQUEST_HEADERS = {headers}
STATIC_PROXY_URL = {proxy}
DOWNLOADER_MIDDLEWARES = {{
    "{project}.middlewares.StaticProxyMiddleware": 350,
}}
ITEM_PIPELINES = {{
    "{project}.pipelines.DedupeVideoPipeline": 300,
}}
FEEDS = {{
    "videos.jsonl": {{"format": "jsonlines", "overwrite": True}},
}}
""".format(project=project, headers=repr(config.network.headers), proxy=proxy, cache_enabled=cache_enabled)


def _scrapy_spider(config):
    seeds = repr(config.seeds)
    domains = repr(config.scope.allowed_domains)
    max_depth = int(config.scope.max_depth)
    return """import re
from urllib.parse import urljoin

import scrapy

from ..items import VideoCandidateItem


MEDIA_RE = re.compile(r"https?://[^\\s\\"'<>]+?\\.(?:m3u8|mpd|mp4|webm|mov|m4v|ogv|ogg)(?:\\?[^\\s\\"'<>]*)?", re.I)


class VideoSpider(scrapy.Spider):
    name = "video"
    start_urls = {seeds}
    allowed_domains = {domains}
    custom_settings = {{"DEPTH_LIMIT": {max_depth}}}

    def parse(self, response):
        title = response.css("title::text").get()
        for src in response.css("video::attr(src), source::attr(src), iframe::attr(src)").getall():
            yield self._item(response.url, urljoin(response.url, src), title, "selector")
        for content in response.css("meta[property='og:video']::attr(content), meta[name='twitter:player']::attr(content)").getall():
            yield self._item(response.url, urljoin(response.url, content), title, "meta")
        for media_url in MEDIA_RE.findall(response.text):
            yield self._item(response.url, media_url, title, "regex")
        for href in response.css("a::attr(href)").getall():
            yield response.follow(href, callback=self.parse)

    def _item(self, page_url, media_url, title, source):
        return VideoCandidateItem(
            page_url=page_url,
            media_url=media_url,
            kind="video",
            title=title,
            source=source,
        )
""".format(seeds=seeds, domains=domains, max_depth=max_depth)


def _crawlee_main(config):
    seeds = repr(config.seeds)
    proxy_urls = repr(config.network.proxy_urls or ([config.network.proxy_url] if config.network.proxy_url else []))
    use_session_pool = "True" if config.network.session_pool else "False"
    media_pattern = r"https?://[^\s\"'<>]+?\.(?:m3u8|mpd|mp4|webm|mov|m4v|ogv|ogg)(?:\?[^\s\"'<>]*)?"
    return '''import asyncio
import re
from urllib.parse import urljoin

from crawlee import ConcurrencySettings
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.proxy_configuration import ProxyConfiguration


START_URLS = {seeds}
PROXY_URLS = {proxy_urls}
MEDIA_RE = re.compile(r"{media_pattern}", re.I)


async def main():
    proxy_configuration = ProxyConfiguration(proxy_urls=PROXY_URLS) if PROXY_URLS else None
    crawler = PlaywrightCrawler(
        max_requests_per_crawl=100,
        proxy_configuration=proxy_configuration,
        use_session_pool={use_session_pool},
        concurrency_settings=ConcurrencySettings(max_concurrency=2),
    )

    @crawler.router.default_handler
    async def handler(context: PlaywrightCrawlingContext) -> None:
        page = context.page
        title = await page.title()
        html = await page.content()
        url = page.url

        for media_url in set(MEDIA_RE.findall(html)):
            await context.push_data({{
                "page_url": url,
                "media_url": media_url,
                "title": title,
                "source": "regex",
            }})

        for src in await page.locator("video, source, iframe").evaluate_all(
            "(nodes) => nodes.map(n => n.currentSrc || n.src).filter(Boolean)"
        ):
            await context.push_data({{
                "page_url": url,
                "media_url": urljoin(url, src),
                "title": title,
                "source": "selector",
            }})

        await context.enqueue_links()

    await crawler.run(START_URLS)


if __name__ == "__main__":
    asyncio.run(main())
'''.format(seeds=seeds, proxy_urls=proxy_urls, use_session_pool=use_session_pool, media_pattern=media_pattern)


def _colly_go_mod(config):
    module = _safe_name(config.project or "vcrawl_colly")
    return """module {module}

go 1.22

require github.com/gocolly/colly/v2 v2.1.0
""".format(module=module)


def _colly_main(config):
    seeds = _go_string_slice(config.seeds)
    domains = _go_string_slice(config.scope.allowed_domains)
    max_depth = int(config.scope.max_depth)
    template = r'''package main

import (
	"net/http/cookiejar"
	"encoding/json"
	"log"
	"net/url"
	"os"
	"regexp"

	"github.com/gocolly/colly/v2"
	"github.com/gocolly/colly/v2/proxy"
)

type VideoCandidate struct {
	PageURL  string `json:"page_url"`
	MediaURL string `json:"media_url"`
	Kind     string `json:"kind"`
	Title    string `json:"title"`
	Source   string `json:"source"`
}

var mediaRe = regexp.MustCompile(`https?://[^\s"'<>]+?\.(m3u8|mpd|mp4|webm|mov|m4v|ogv|ogg)(\?[^\s"'<>]*)?`)
var seeds = []string{__SEEDS__}
var allowedDomains = []string{__DOMAINS__}
var proxyURLs = []string{__PROXY_URLS__}

func main() {
	jar, err := cookiejar.New(nil)
	if err != nil {
		log.Fatal(err)
	}
	collector := colly.NewCollector(
		colly.AllowedDomains(allowedDomains...),
		colly.MaxDepth(__MAX_DEPTH__),
		colly.Async(true),
	)
	collector.SetCookieJar(jar)
	if len(proxyURLs) > 0 {
		switcher, err := proxy.RoundRobinProxySwitcher(proxyURLs...)
		if err != nil {
			log.Fatal(err)
		}
		collector.SetProxyFunc(switcher)
	}
	collector.Limit(&colly.LimitRule{DomainGlob: "*", Parallelism: 2})

	encoder := json.NewEncoder(os.Stdout)

	collector.OnHTML("title", func(e *colly.HTMLElement) {
		e.Request.Ctx.Put("title", e.Text)
	})
	collector.OnHTML("video[src], source[src], iframe[src]", func(e *colly.HTMLElement) {
		emit(encoder, e.Request.URL.String(), e.Request.AbsoluteURL(e.Attr("src")), "selector", e.Request.Ctx.Get("title"))
	})
	collector.OnHTML("meta[property='og:video'], meta[name='twitter:player']", func(e *colly.HTMLElement) {
		emit(encoder, e.Request.URL.String(), e.Request.AbsoluteURL(e.Attr("content")), "meta", e.Request.Ctx.Get("title"))
	})
	collector.OnResponse(func(r *colly.Response) {
		for _, match := range mediaRe.FindAllString(string(r.Body), -1) {
			emit(encoder, r.Request.URL.String(), match, "regex", r.Request.Ctx.Get("title"))
		}
	})
	collector.OnHTML("a[href]", func(e *colly.HTMLElement) {
		link := e.Request.AbsoluteURL(e.Attr("href"))
		if link != "" && sameAllowedDomain(link) {
			_ = e.Request.Visit(link)
		}
	})
	collector.OnError(func(r *colly.Response, err error) {
		log.Printf("error url=%s status=%d err=%s", r.Request.URL.String(), r.StatusCode, err)
	})

	for _, seed := range seeds {
		if err := collector.Visit(seed); err != nil {
			log.Printf("seed error url=%s err=%s", seed, err)
		}
	}
	collector.Wait()
}

func emit(encoder *json.Encoder, pageURL string, mediaURL string, source string, title string) {
	if mediaURL == "" {
		return
	}
	_ = encoder.Encode(VideoCandidate{
		PageURL:  pageURL,
		MediaURL: mediaURL,
		Kind:     "video",
		Title:    title,
		Source:   source,
	})
}

func sameAllowedDomain(raw string) bool {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Host == "" {
		return false
	}
	for _, domain := range allowedDomains {
		if parsed.Host == domain {
			return true
		}
	}
	return false
}
'''
    return (
        template.replace("__SEEDS__", seeds)
        .replace("__DOMAINS__", domains)
        .replace("__PROXY_URLS__", _go_string_slice(config.network.proxy_urls or ([config.network.proxy_url] if config.network.proxy_url else [])))
        .replace("__MAX_DEPTH__", str(max_depth))
    )


def _colly_readme():
    return """# VCrawl Colly Integration

This scaffold uses Colly for a fast Go crawler that emits JSONL video candidates to stdout.

```bash
go mod tidy
go run . > videos.jsonl
```

Use this when you want a lightweight compiled crawler with callback-style extraction.
"""


def _nutch_regex_urlfilter(config):
    lines = ["# Generated by vcrawl", "# reject common non-content URLs", "-.*(login|account|logout|signup).*"]
    for domain in config.scope.allowed_domains:
        escaped = domain.replace(".", "\\.")
        lines.append("+^https?://%s/.*" % escaped)
    lines.append("-.*")
    return "\n".join(lines)


def _nutch_site_xml(config):
    delay_ms = int(float(config.fetch.delay_per_domain_seconds or 1) * 1000)
    timeout_ms = int(float(config.fetch.timeout_seconds or 20) * 1000)
    agent = config.fetch.user_agent
    proxy_host = ""
    proxy_port = ""
    if config.network.proxy_url:
        parsed = _parse_proxy(config.network.proxy_url)
        proxy_host = parsed.get("host", "")
        proxy_port = parsed.get("port", "")
    return """<?xml version="1.0"?>
<configuration>
  <property>
    <name>http.agent.name</name>
    <value>{agent}</value>
  </property>
  <property>
    <name>http.robots.agents</name>
    <value>{agent}</value>
  </property>
  <property>
    <name>fetcher.server.delay</name>
    <value>{delay_ms}</value>
  </property>
  <property>
    <name>http.timeout</name>
    <value>{timeout_ms}</value>
  </property>
  <property>
    <name>http.proxy.host</name>
    <value>{proxy_host}</value>
  </property>
  <property>
    <name>http.proxy.port</name>
    <value>{proxy_port}</value>
  </property>
</configuration>
""".format(
        agent=agent,
        delay_ms=delay_ms,
        timeout_ms=timeout_ms,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
    )


def _nutch_readme(config):
    project = config.project or "vcrawl"
    return """# VCrawl Nutch Integration

This scaffold prepares seeds and URL filters for Apache Nutch.

Project: {project}

Files:

- `urls/seed.txt`: seed URLs generated from `vcrawl.json`.
- `conf/regex-urlfilter.txt`: domain-scoped URL filter.

Example Nutch flow, assuming Nutch is installed and configured:

```bash
nutch inject crawl/crawldb urls
nutch generate crawl/crawldb crawl/segments
nutch fetch crawl/segments/* -threads 10
nutch parse crawl/segments/*
nutch updatedb crawl/crawldb crawl/segments/*
```

Use Nutch for large-scale, extensible crawls where a distributed crawler/indexing pipeline is more appropriate than the built-in local SQLite queue.
""".format(project=project)


def _go_string_slice(values):
    return ", ".join(json.dumps(value) for value in values)


def _parse_proxy(proxy_url):
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    return {"host": parsed.hostname or "", "port": str(parsed.port or "")}
