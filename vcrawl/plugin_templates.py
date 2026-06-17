import os


PLUGIN_TEMPLATE = '''"""vcrawl site plugin template.

Expose PLUGIN_MANIFEST and extract_videos(page_url, html, title=None, config=None).
Return dictionaries with at least a media_url field.
"""

import re


MEDIA_RE = re.compile(r"https?://[^\\s\\"'<>]+?\\.(?:m3u8|mpd|mp4|webm|mov|m4v|ogv|ogg)(?:\\?[^\\s\\"'<>]*)?", re.I)

PLUGIN_MANIFEST = {
    "name": "site-plugin",
    "version": "0.1",
    "capabilities": ["html", "direct-media"],
    "config_schema": {
        "type": "object",
        "properties": {
            "source_label": {"type": "string"},
        },
    },
    "fixtures": [
        {
            "name": "absolute media url",
            "page_url": "https://example.com/watch",
            "html": "<script>const media='https://cdn.example.com/video.mp4'</script>",
            "expected_media_urls": ["https://cdn.example.com/video.mp4"],
        }
    ],
}


def extract_videos(page_url, html, title=None, config=None):
    config = config or {}
    source = config.get("source_label") or "plugin.template"
    for media_url in sorted(set(MEDIA_RE.findall(html or ""))):
        yield {
            "page_url": page_url,
            "media_url": media_url,
            "kind": "plugin",
            "title": title,
            "source": source,
            "metadata": {},
        }
'''


def write_plugin_template(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(PLUGIN_TEMPLATE)
    return os.path.abspath(path)
