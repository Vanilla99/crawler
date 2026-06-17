import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.request import Request, urlopen


class FeedLinkParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "a" and attrs.get("href"):
            self.links.append(attrs["href"])


def expand_discovery_seeds(config, fetch_text=None):
    fetch_text = fetch_text or _fetch_text
    urls = list(config.seeds)
    limit = max(0, int(config.discovery.max_discovered_urls or 0))
    for sitemap_url in config.discovery.sitemaps:
        urls.extend(_limited(discover_sitemap_urls(sitemap_url, fetch_text=fetch_text), limit, len(urls)))
    for feed_url in config.discovery.feeds:
        urls.extend(_limited(discover_feed_urls(feed_url, fetch_text=fetch_text), limit, len(urls)))
    return _dedupe(urls)[: limit or None]


def discover_sitemap_urls(url, fetch_text=None):
    fetch_text = fetch_text or _fetch_text
    text = fetch_text(url)
    root = ET.fromstring(text)
    urls = []
    for node in root.iter():
        if _strip_namespace(node.tag) == "loc" and node.text:
            urls.append(node.text.strip())
    return urls


def discover_feed_urls(url, fetch_text=None):
    fetch_text = fetch_text or _fetch_text
    text = fetch_text(url)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        parser = FeedLinkParser()
        parser.feed(text)
        return parser.links
    urls = []
    for node in root.iter():
        tag = _strip_namespace(node.tag)
        if tag == "link":
            href = node.attrib.get("href")
            if href:
                urls.append(href.strip())
            elif node.text and node.text.strip().startswith(("http://", "https://")):
                urls.append(node.text.strip())
        elif tag in ("guid", "id") and node.text and node.text.strip().startswith(("http://", "https://")):
            urls.append(node.text.strip())
    return urls


def _fetch_text(url, timeout_seconds=30):
    request = Request(url, headers={"User-Agent": "vcrawl/0.1"})
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _strip_namespace(tag):
    return tag.rsplit("}", 1)[-1]


def _dedupe(values):
    seen = set()
    output = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _limited(values, limit, already):
    if not limit:
        return values
    remaining = max(0, limit - already)
    return values[:remaining]
