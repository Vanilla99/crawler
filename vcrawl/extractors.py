import json
import re
from html.parser import HTMLParser

from .challenges import detect_challenge
from .media_detection import MEDIA_PATH_RE, MEDIA_URL_RE
from .models import ExtractedPage, VideoCandidate
from .plugins import PluginRegistry
from .scope import canonicalize_url


EMBEDDED_VIDEO_HINT_RE = re.compile(
    r"(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|bilibili\.com|"
    r"player|embed|video|watch|playlist)",
    re.I,
)
PLAYER_SCRIPT_HINT_RE = re.compile(
    r"(player|videojs|hls\.js|dash\.js|jwplayer|flowplayer|brightcove|shaka|mediaelement|m3u8|mpd)",
    re.I,
)
DYNAMIC_SCRIPT_MARKERS = [
    ("fetch_api", re.compile(r"\bfetch\s*\(", re.I)),
    ("xhr_api", re.compile(r"\bXMLHttpRequest\b", re.I)),
    ("media_source", re.compile(r"\bMediaSource\b|\bSourceBuffer\b", re.I)),
    ("hls_player", re.compile(r"\bHls\s*\.|hls\.js|application/vnd\.apple\.mpegurl", re.I)),
    ("dash_player", re.compile(r"\bdashjs\b|dash\.js|application/dash\+xml", re.I)),
    ("generic_player", re.compile(r"\bvideojs\b|\bjwplayer\b|\bflowplayer\b|\bshaka\b", re.I)),
]
LAZY_MEDIA_ATTRS = (
    "data-src",
    "data-video",
    "data-hls",
    "data-m3u8",
    "data-mpd",
    "data-file",
    "data-url",
)


class _VideoHTMLParser(HTMLParser):
    def __init__(self, page_url):
        HTMLParser.__init__(self)
        self.page_url = page_url
        self.links = []
        self.videos = []
        self.title = None
        self._in_title = False
        self._current_video_attrs = {}
        self._jsonld_chunks = []
        self._in_jsonld = False
        self._in_script = False
        self.video_tag_count = 0
        self.source_tag_count = 0
        self.embed_iframe_count = 0
        self.script_count = 0
        self.jsonld_block_count = 0
        self.lazy_media_attrs = 0
        self.script_media_url_hits = 0
        self.player_script_urls = []
        self.dynamic_script_markers = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag == "a" and attrs.get("href"):
            self.links.append(canonicalize_url(attrs["href"], self.page_url))
        if tag == "video":
            self.video_tag_count += 1
            self._current_video_attrs = attrs
            self._observe_lazy_attrs(attrs)
            src = attrs.get("src") or attrs.get("data-src")
            if src:
                self._add_video(src, "video", "video.src", attrs)
        if tag == "source":
            self.source_tag_count += 1
            self._observe_lazy_attrs(attrs)
            src = attrs.get("src") or attrs.get("data-src")
            if src:
                self._add_video(src, "source", "source.src", attrs)
        if tag == "iframe":
            src = attrs.get("src") or attrs.get("data-src")
            if src and _looks_like_embedded_video_url(src):
                self.embed_iframe_count += 1
                self._add_video(src, "iframe", "iframe.src", attrs)
        if tag == "meta":
            prop = (attrs.get("property") or attrs.get("name") or "").lower()
            content = attrs.get("content")
            if content and prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player"):
                self._add_video(content, "embedded", prop, attrs)
        if tag == "link":
            rel = (attrs.get("rel") or "").lower()
            href = attrs.get("href")
            mime_type = (attrs.get("type") or "").lower()
            is_media_link = href and (MEDIA_PATH_RE.search(href) or mime_type.startswith("video/"))
            if href and ("video_src" in rel or ("preload" in rel and is_media_link)):
                self._add_video(href, "link", "link.%s" % rel, attrs)
        if tag == "script":
            self._in_script = True
            self.script_count += 1
            src = attrs.get("src") or ""
            if src and PLAYER_SCRIPT_HINT_RE.search(src):
                self.player_script_urls.append(canonicalize_url(src, self.page_url))
            script_type = (attrs.get("type") or "").lower()
            if "ld+json" in script_type:
                self.jsonld_block_count += 1
                self._in_jsonld = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_script = False
            if self._in_jsonld:
                self._in_jsonld = False

    def handle_data(self, data):
        if self._in_title:
            self.title = ((self.title or "") + data).strip()
        if self._in_script:
            self._observe_script_data(data)
        if self._in_jsonld:
            self._jsonld_chunks.append(data)

    def close(self):
        HTMLParser.close(self)
        for chunk in self._jsonld_chunks:
            for candidate in _videos_from_jsonld(chunk, self.page_url):
                self.videos.append(candidate)

    def _add_video(self, url, kind, source, attrs):
        media_url = canonicalize_url(url, self.page_url)
        self.videos.append(
            VideoCandidate(
                page_url=self.page_url,
                media_url=media_url,
                kind=kind,
                title=attrs.get("title") or attrs.get("aria-label"),
                source=source,
                metadata={key: value for key, value in attrs.items() if isinstance(value, str)},
            )
        )

    def _observe_lazy_attrs(self, attrs):
        if any(attrs.get(name) for name in LAZY_MEDIA_ATTRS):
            self.lazy_media_attrs += 1

    def _observe_script_data(self, data):
        text = data or ""
        if not text:
            return
        self.script_media_url_hits += len(MEDIA_URL_RE.findall(text))
        for label, pattern in DYNAMIC_SCRIPT_MARKERS:
            if pattern.search(text):
                self.dynamic_script_markers[label] = self.dynamic_script_markers.get(label, 0) + 1


def _videos_from_jsonld(raw, page_url):
    candidates = []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return candidates
    for item in _walk_json(data):
        item_type = item.get("@type") or item.get("type")
        if isinstance(item_type, list):
            is_video = any(str(value).lower() == "videoobject" for value in item_type)
        else:
            is_video = str(item_type).lower() == "videoobject"
        if not is_video:
            continue
        title = item.get("name") or item.get("headline")
        for key in ("contentUrl", "embedUrl", "url", "thumbnailUrl"):
            value = item.get(key)
            if isinstance(value, list):
                values = value
            else:
                values = [value]
            for media_url in values:
                if isinstance(media_url, str) and media_url.startswith(("http://", "https://", "/")):
                    candidates.append(
                        VideoCandidate(
                            page_url=page_url,
                            media_url=canonicalize_url(media_url, page_url),
                            kind="jsonld",
                            title=title,
                            source="jsonld.%s" % key,
                            metadata={"jsonld_key": key},
                        )
                    )
    return candidates


def _looks_like_embedded_video_url(url):
    return bool(MEDIA_PATH_RE.search(url) or EMBEDDED_VIDEO_HINT_RE.search(url))


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            for item in _walk_json(child):
                yield item
    elif isinstance(value, list):
        for child in value:
            for item in _walk_json(child):
                yield item


class VideoExtractor:
    def __init__(self, plugin_paths=None, builtin_plugins=None, plugin_configs=None):
        self.plugins = PluginRegistry(
            plugin_paths,
            builtin_plugins=builtin_plugins,
            plugin_configs=plugin_configs,
        )

    def extract(self, fetch_result):
        url = fetch_result.final_url or fetch_result.url
        parser = _VideoHTMLParser(url)
        parser.feed(fetch_result.text or "")
        parser.close()
        html_videos = list(parser.videos)
        regex_videos = []
        for match in MEDIA_URL_RE.finditer(fetch_result.text or ""):
            regex_videos.append(
                VideoCandidate(
                    page_url=url,
                    media_url=match.group(0),
                    kind="media_url",
                    title=parser.title,
                    source="regex.media_url",
                )
            )
        plugin_videos = list(self.plugins.extract_videos(url, fetch_result.text or "", title=parser.title))
        hint_videos = _videos_from_media_hints(fetch_result.media_hints, url, parser.title)
        dynamic_signals = _dynamic_signals(parser, fetch_result.media_hints)
        all_videos = html_videos + regex_videos + plugin_videos + hint_videos
        videos = _dedupe_videos(all_videos)
        links = _dedupe(parser.links)
        challenge_detected = fetch_result.challenge_detected or detect_challenge(
            url,
            fetch_result.status_code,
            fetch_result.headers,
            fetch_result.text,
        )
        return ExtractedPage(
            url=url,
            links=links,
            videos=videos,
            title=parser.title,
            challenge_detected=challenge_detected,
            diagnostics=_build_extraction_diagnostics(
                fetch_result,
                links,
                html_videos,
                regex_videos,
                plugin_videos,
                hint_videos,
                all_videos,
                videos,
                challenge_detected,
                dynamic_signals,
            ),
        )


def _dedupe(values):
    seen = set()
    output = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _videos_from_media_hints(media_hints, page_url, title=None):
    candidates = []
    for hint in media_hints or []:
        if not getattr(hint, "url", None):
            continue
        metadata = dict(getattr(hint, "metadata", None) or {})
        if getattr(hint, "content_type", None):
            metadata.setdefault("content_type", hint.content_type)
        if getattr(hint, "status_code", None) is not None:
            metadata.setdefault("status_code", str(hint.status_code))
        if getattr(hint, "method", None):
            metadata.setdefault("method", hint.method)
        if getattr(hint, "resource_type", None):
            metadata.setdefault("resource_type", hint.resource_type)
        candidates.append(
            VideoCandidate(
                page_url=page_url,
                media_url=canonicalize_url(hint.url, page_url),
                kind=getattr(hint, "kind", None) or "media",
                title=title,
                source=getattr(hint, "source", None) or "network",
                metadata=metadata,
            )
        )
    return candidates


def _build_extraction_diagnostics(
    fetch_result,
    links,
    html_videos,
    regex_videos,
    plugin_videos,
    hint_videos,
    all_videos,
    final_videos,
    challenge_detected,
    dynamic_signals,
):
    media_hints = list(fetch_result.media_hints or [])
    duplicate_candidates = max(0, len(all_videos) - len(final_videos))
    diagnostics = {
        "status_code": fetch_result.status_code,
        "fetcher": fetch_result.fetcher,
        "content_type": _header_value(fetch_result.headers, "content-type") or "",
        "links": len(links),
        "html_candidates": len(html_videos),
        "regex_candidates": len(regex_videos),
        "plugin_candidates": len(plugin_videos),
        "media_hint_count": len(media_hints),
        "media_hint_candidates": len(hint_videos),
        "duplicate_candidates": duplicate_candidates,
        "final_candidates": len(final_videos),
        "candidate_sources": _source_counts(all_videos),
        "media_hint_sources": _hint_source_counts(media_hints),
        "dynamic_signals": dynamic_signals,
        "challenge_detected": bool(challenge_detected),
        "notes": _diagnostic_notes(fetch_result, all_videos, final_videos, media_hints, challenge_detected),
        "recommendations": _diagnostic_recommendations(
            fetch_result,
            all_videos,
            final_videos,
            media_hints,
            challenge_detected,
            dynamic_signals,
        ),
    }
    return diagnostics


def _dynamic_signals(parser, media_hints):
    media_hints = list(media_hints or [])
    network_by_kind = {}
    browser_hints = 0
    for hint in media_hints:
        kind = getattr(hint, "kind", None) or "media"
        network_by_kind[kind] = network_by_kind.get(kind, 0) + 1
        if (getattr(hint, "source", None) or "").startswith("browser."):
            browser_hints += 1
    score = (
        parser.embed_iframe_count
        + parser.lazy_media_attrs
        + parser.script_media_url_hits
        + len(parser.player_script_urls)
        + sum(parser.dynamic_script_markers.values())
        + browser_hints
    )
    return {
        "video_tags": parser.video_tag_count,
        "source_tags": parser.source_tag_count,
        "embedded_player_iframes": parser.embed_iframe_count,
        "script_tags": parser.script_count,
        "jsonld_blocks": parser.jsonld_block_count,
        "lazy_media_attrs": parser.lazy_media_attrs,
        "script_media_url_hits": parser.script_media_url_hits,
        "player_script_urls": parser.player_script_urls[:20],
        "dynamic_script_markers": dict(sorted(parser.dynamic_script_markers.items())),
        "browser_media_hints": browser_hints,
        "network_media_hints_by_kind": dict(sorted(network_by_kind.items())),
        "dynamic_score": score,
    }


def _source_counts(videos):
    counts = {}
    for video in videos:
        source = video.source or "unknown"
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def _hint_source_counts(media_hints):
    counts = {}
    for hint in media_hints:
        source = getattr(hint, "source", None) or "network"
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def _diagnostic_notes(fetch_result, all_videos, final_videos, media_hints, challenge_detected):
    notes = []
    if challenge_detected:
        notes.append("challenge_detected")
    if media_hints:
        notes.append("network_media_hints_detected")
    if len(all_videos) > len(final_videos):
        notes.append("duplicate_candidates_collapsed")
    if final_videos:
        return notes
    if fetch_result.error:
        notes.append("fetch_error")
    if not (fetch_result.text or "").strip():
        notes.append("empty_response_body")
    if not all_videos:
        notes.append("no_video_tags_jsonld_media_urls_plugins_or_network_hints")
    if fetch_result.fetcher != "browser" and not media_hints:
        notes.append("try_browser_fetcher_for_dynamic_players")
    return notes


def _diagnostic_recommendations(
    fetch_result,
    all_videos,
    final_videos,
    media_hints,
    challenge_detected,
    dynamic_signals,
):
    recommendations = []
    if challenge_detected:
        recommendations.append("manual_verification_required_resume_after_user_owned_session")
    if fetch_result.error:
        recommendations.append("fix_fetch_error_before_extraction")
    if final_videos:
        if media_hints:
            recommendations.append("network_media_hints_promoted_to_video_candidates")
        return recommendations
    dynamic_score = int((dynamic_signals or {}).get("dynamic_score") or 0)
    if dynamic_score and fetch_result.fetcher != "browser":
        recommendations.append("rerun_with_browser_fetcher_for_dynamic_player_network_hints")
    if dynamic_score and fetch_result.fetcher == "browser" and not media_hints:
        recommendations.append("browser_loaded_no_media_requests_detected_consider_wait_click_scroll_or_site_plugin")
    if (dynamic_signals or {}).get("embedded_player_iframes"):
        recommendations.append("embedded_player_detected_consider_site_plugin_or_yt_dlp_resolver")
    if not dynamic_score and not all_videos:
        recommendations.append("no_dynamic_media_signals_detected_check_scope_or_add_site_specific_plugin")
    return recommendations


def _header_value(headers, name):
    lowered = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == lowered:
            return value
    return None


def _dedupe_videos(videos):
    seen = set()
    output = []
    for video in videos:
        key = (video.page_url, video.media_url)
        if key in seen:
            continue
        seen.add(key)
        output.append(video)
    return output
