import importlib.util
import inspect
import json
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional

from .models import VideoCandidate
from .scope import canonicalize_url


MEDIA_PATH_RE = re.compile(r"\.(?:m3u8|mpd|mp4|webm|mov|m4v|ogv|ogg)(?:\?|$)", re.I)
MEDIA_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:m3u8|mpd|mp4|webm|mov|m4v|ogv|ogg)(?:\?[^\s\"'<>]*)?",
    re.I,
)


@dataclass
class PluginSpec:
    name: str
    version: str = "0.1"
    capabilities: List[str] = field(default_factory=list)
    config_schema: Dict = field(default_factory=dict)
    fixtures: List[Dict] = field(default_factory=list)
    path: Optional[str] = None
    module_path: Optional[str] = None
    builtin: bool = False


@dataclass
class LoadedPlugin:
    spec: PluginSpec
    implementation: object
    config: Dict = field(default_factory=dict)

    def extract_videos(self, page_url, html, title=None):
        hook = getattr(self.implementation, "extract_videos", None)
        if not hook:
            return []
        kwargs = {"page_url": page_url, "html": html, "title": title}
        if _accepts_config(hook):
            kwargs["config"] = self.config
        return hook(**kwargs) or []


class PluginRegistry:
    def __init__(self, paths=None, builtin_plugins=None, plugin_configs=None):
        self.plugin_configs = plugin_configs or {}
        self.plugins = []
        for name in builtin_plugins or []:
            self.plugins.append(load_builtin_plugin(name, config=self.plugin_configs.get(name) or {}))
        for path in paths or []:
            plugin = load_plugin(path, config_map=self.plugin_configs)
            self.plugins.append(plugin)

    def extract_videos(self, page_url, html, title=None):
        videos = []
        for plugin in self.plugins:
            for item in plugin.extract_videos(page_url=page_url, html=html, title=title):
                candidate = _candidate_from_plugin_item(page_url, item, plugin.spec.name)
                if candidate:
                    videos.append(candidate)
        return videos

    def manifests(self):
        return [plugin.spec for plugin in self.plugins]


def load_plugin(path, config_map=None, config=None):
    path = os.path.abspath(path)
    if os.path.isdir(path):
        manifest_path = os.path.join(path, "vcrawl-plugin.json")
        if not os.path.exists(manifest_path):
            manifest_path = os.path.join(path, "plugin.json")
        if not os.path.exists(manifest_path):
            raise RuntimeError("plugin directory requires vcrawl-plugin.json or plugin.json: %s" % path)
        return _load_manifest_plugin(manifest_path, config_map=config_map, config=config)
    if path.endswith(".json"):
        return _load_manifest_plugin(path, config_map=config_map, config=config)
    module = _load_module(path)
    spec = _spec_from_module(module, path)
    config = dict(config if config is not None else _config_for(spec, config_map or {}))
    _validate_config(spec, config)
    return LoadedPlugin(spec=spec, implementation=module, config=config)


def load_builtin_plugin(name, config=None):
    builtin = BUILTIN_PLUGINS.get(name)
    if not builtin:
        raise RuntimeError("unknown built-in plugin: %s" % name)
    implementation = builtin()
    manifest = implementation.manifest()
    spec = PluginSpec(
        name=manifest["name"],
        version=manifest.get("version", "0.1"),
        capabilities=list(manifest.get("capabilities") or []),
        config_schema=manifest.get("config_schema") or {},
        fixtures=list(manifest.get("fixtures") or []),
        builtin=True,
    )
    config = config or {}
    _validate_config(spec, config)
    return LoadedPlugin(spec=spec, implementation=implementation, config=config)


def list_builtin_plugins():
    return [load_builtin_plugin(name).spec for name in sorted(BUILTIN_PLUGINS)]


def run_plugin_tests(path=None, fixture_path=None, builtin=None, config=None):
    if builtin:
        plugin = load_builtin_plugin(builtin, config=config or {})
    elif path:
        plugin = load_plugin(path, config_map={}, config=config)
    else:
        raise RuntimeError("plugin test requires --path or --builtin")
    fixtures = []
    if fixture_path:
        fixtures.extend(_load_fixtures(fixture_path))
    fixtures.extend(plugin.spec.fixtures)
    results = []
    for fixture in fixtures:
        results.append(_run_fixture(plugin, fixture))
    return {
        "ok": bool(fixtures) and all(item["ok"] for item in results),
        "plugin": _spec_dict(plugin.spec),
        "fixtures": results,
    }


def _load_manifest_plugin(manifest_path, config_map=None, config=None):
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    base = os.path.dirname(manifest_path)
    module_path = manifest.get("module") or manifest.get("python")
    if not module_path:
        raise RuntimeError("plugin manifest requires module")
    if not os.path.isabs(module_path):
        module_path = os.path.join(base, module_path)
    module = _load_module(module_path)
    spec = PluginSpec(
        name=manifest.get("name") or os.path.splitext(os.path.basename(module_path))[0],
        version=manifest.get("version", "0.1"),
        capabilities=list(manifest.get("capabilities") or []),
        config_schema=manifest.get("config_schema") or {},
        fixtures=list(manifest.get("fixtures") or []),
        path=manifest_path,
        module_path=module_path,
    )
    if not spec.fixtures:
        module_spec = _spec_from_module(module, module_path)
        spec.fixtures = module_spec.fixtures
    config = dict(config if config is not None else _config_for(spec, config_map or {}))
    _validate_config(spec, config)
    return LoadedPlugin(spec=spec, implementation=module, config=config)


def _load_module(path):
    module_path = os.path.abspath(path)
    name = "vcrawl_plugin_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load plugin: %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec_from_module(module, path):
    manifest = getattr(module, "PLUGIN_MANIFEST", None) or getattr(module, "VCrawl_PLUGIN", None)
    if callable(getattr(module, "plugin_manifest", None)):
        manifest = module.plugin_manifest()
    manifest = manifest or {}
    return PluginSpec(
        name=manifest.get("name") or os.path.splitext(os.path.basename(path))[0],
        version=manifest.get("version", "0.1"),
        capabilities=list(manifest.get("capabilities") or []),
        config_schema=manifest.get("config_schema") or {},
        fixtures=list(manifest.get("fixtures") or []),
        path=path,
        module_path=path,
    )


def _candidate_from_plugin_item(page_url, item, plugin_name="plugin"):
    if isinstance(item, VideoCandidate):
        return item
    if not isinstance(item, dict) or not item.get("media_url"):
        return None
    metadata = item.get("metadata") or {}
    metadata.setdefault("plugin", plugin_name)
    return VideoCandidate(
        page_url=item.get("page_url") or page_url,
        media_url=canonicalize_url(item["media_url"], page_url),
        kind=item.get("kind") or "plugin",
        title=item.get("title"),
        source=item.get("source") or plugin_name,
        metadata=metadata,
    )


def _accepts_config(hook):
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        return False
    return "config" in signature.parameters


def _config_for(spec, config_map):
    return dict(config_map.get(spec.name) or config_map.get(os.path.basename(spec.path or "")) or {})


def _validate_config(spec, config):
    schema = spec.config_schema or {}
    if not schema:
        return
    config = config or {}
    if schema.get("type") == "object" and not isinstance(config, dict):
        raise RuntimeError("plugin %s config must be an object" % spec.name)
    properties = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in config:
            raise RuntimeError("plugin %s missing required config key: %s" % (spec.name, key))
    for key, value in config.items():
        expected = (properties.get(key) or {}).get("type")
        if expected and not _matches_json_type(value, expected):
            raise RuntimeError("plugin %s config key %s must be %s" % (spec.name, key, expected))


def _matches_json_type(value, expected):
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _load_fixtures(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    return data.get("fixtures") or [data]


def _run_fixture(plugin, fixture):
    page_url = fixture.get("page_url") or fixture.get("url") or "https://example.com/"
    actual = list(plugin.extract_videos(page_url, fixture.get("html") or "", fixture.get("title")))
    actual_urls = sorted(
        candidate.media_url if isinstance(candidate, VideoCandidate) else canonicalize_url(candidate.get("media_url"), page_url)
        for candidate in actual
        if isinstance(candidate, VideoCandidate) or (isinstance(candidate, dict) and candidate.get("media_url"))
    )
    expected = fixture.get("expected") or fixture.get("expected_media_urls") or []
    expected_urls = sorted(
        canonicalize_url(item.get("media_url") if isinstance(item, dict) else item, page_url)
        for item in expected
    )
    missing = [url for url in expected_urls if url not in actual_urls]
    unexpected = [url for url in actual_urls if expected_urls and url not in expected_urls]
    return {
        "name": fixture.get("name") or page_url,
        "ok": not missing and not unexpected,
        "expected": expected_urls,
        "actual": actual_urls,
        "missing": missing,
        "unexpected": unexpected,
    }


def _spec_dict(spec):
    return {
        "name": spec.name,
        "version": spec.version,
        "capabilities": spec.capabilities,
        "config_schema": spec.config_schema,
        "fixtures": len(spec.fixtures),
        "path": spec.path,
        "module_path": spec.module_path,
        "builtin": spec.builtin,
    }


class _AttrParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.items = []
        self.jsonld = []
        self._in_jsonld = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        if tag in ("a", "video", "source", "iframe", "link"):
            self.items.append((tag, attrs))
        if tag == "script" and "ld+json" in (attrs.get("type") or "").lower():
            self._in_jsonld = True

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self._in_jsonld = False

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld.append(data)


class GalleryPlugin:
    def manifest(self):
        return {
            "name": "gallery",
            "version": "0.1",
            "capabilities": ["html", "gallery", "direct-media"],
            "config_schema": {"type": "object", "properties": {}},
            "fixtures": [
                {
                    "name": "anchor media gallery",
                    "page_url": "https://example.com/gallery/",
                    "html": '<a href="/media/clip.mp4"><img src="/thumb.jpg"></a>',
                    "expected_media_urls": ["https://example.com/media/clip.mp4"],
                }
            ],
        }

    def extract_videos(self, page_url, html, title=None):
        parser = _AttrParser()
        parser.feed(html or "")
        parser.close()
        for tag, attrs in parser.items:
            href = attrs.get("href") or attrs.get("src") or attrs.get("data-src")
            if tag == "a" and href and MEDIA_PATH_RE.search(href):
                yield _item(page_url, href, "gallery", title, "builtin.gallery")


class PlaylistPlugin:
    def manifest(self):
        return {
            "name": "playlist",
            "version": "0.1",
            "capabilities": ["html", "playlist", "m3u8", "dash"],
            "config_schema": {"type": "object", "properties": {}},
            "fixtures": [
                {
                    "name": "playlist link",
                    "page_url": "https://example.com/watch",
                    "html": '<a href="/live/master.m3u8">live</a><a href="/dash/manifest.mpd">dash</a>',
                    "expected_media_urls": [
                        "https://example.com/live/master.m3u8",
                        "https://example.com/dash/manifest.mpd",
                    ],
                }
            ],
        }

    def extract_videos(self, page_url, html, title=None):
        parser = _AttrParser()
        parser.feed(html or "")
        parser.close()
        for _tag, attrs in parser.items:
            value = attrs.get("href") or attrs.get("src") or attrs.get("data-src")
            if value and re.search(r"\.(?:m3u8|mpd)(?:\?|$)", value, re.I):
                yield _item(page_url, value, "playlist", title, "builtin.playlist")


class M3U8PagePlugin:
    def manifest(self):
        return {
            "name": "m3u8",
            "version": "0.1",
            "capabilities": ["m3u8", "manifest"],
            "config_schema": {"type": "object", "properties": {}},
            "fixtures": [
                {
                    "name": "m3u8 document",
                    "page_url": "https://cdn.example.com/master.m3u8",
                    "html": "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1280000\nlow/index.m3u8\n",
                    "expected_media_urls": ["https://cdn.example.com/master.m3u8"],
                }
            ],
        }

    def extract_videos(self, page_url, html, title=None):
        if page_url.lower().split("?", 1)[0].endswith(".m3u8") or (html or "").lstrip().startswith("#EXTM3U"):
            yield _item(page_url, page_url, "manifest", title, "builtin.m3u8")
        for match in MEDIA_URL_RE.finditer(html or ""):
            if ".m3u8" in match.group(0).lower():
                yield _item(page_url, match.group(0), "manifest", title, "builtin.m3u8")


class JsonLdPlugin:
    def manifest(self):
        return {
            "name": "jsonld",
            "version": "0.1",
            "capabilities": ["json-ld", "metadata", "embedded-video"],
            "config_schema": {"type": "object", "properties": {}},
            "fixtures": [
                {
                    "name": "video object",
                    "page_url": "https://example.com/watch",
                    "html": '<script type="application/ld+json">{"@type":"VideoObject","name":"Demo","contentUrl":"/video.mp4"}</script>',
                    "expected_media_urls": ["https://example.com/video.mp4"],
                }
            ],
        }

    def extract_videos(self, page_url, html, title=None):
        parser = _AttrParser()
        parser.feed(html or "")
        parser.close()
        for chunk in parser.jsonld:
            for item in _videos_from_jsonld(chunk, page_url, title):
                yield item


BUILTIN_PLUGINS = {
    "gallery": GalleryPlugin,
    "playlist": PlaylistPlugin,
    "m3u8": M3U8PagePlugin,
    "jsonld": JsonLdPlugin,
}


def _item(page_url, media_url, kind, title, source):
    return {
        "page_url": page_url,
        "media_url": media_url,
        "kind": kind,
        "title": title,
        "source": source,
        "metadata": {},
    }


def _videos_from_jsonld(raw, page_url, fallback_title=None):
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    candidates = []
    for item in _walk_json(data):
        item_type = item.get("@type") or item.get("type")
        values = item_type if isinstance(item_type, list) else [item_type]
        if not any(str(value).lower() == "videoobject" for value in values):
            continue
        title = item.get("name") or item.get("headline") or fallback_title
        for key in ("contentUrl", "embedUrl", "url", "thumbnailUrl"):
            value = item.get(key)
            urls = value if isinstance(value, list) else [value]
            for media_url in urls:
                if isinstance(media_url, str) and media_url.startswith(("http://", "https://", "/")):
                    candidates.append(
                        {
                            "page_url": page_url,
                            "media_url": media_url,
                            "kind": "jsonld",
                            "title": title,
                            "source": "builtin.jsonld.%s" % key,
                            "metadata": {"jsonld_key": key},
                        }
                    )
    return candidates


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
