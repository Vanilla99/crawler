import re
from urllib.parse import urlsplit


MEDIA_EXTENSIONS = {
    ".m3u8": "hls_manifest",
    ".mpd": "dash_manifest",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    ".m4v": "video",
    ".ogv": "video",
    ".ogg": "video",
}

MEDIA_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:m3u8|mpd|mp4|webm|mov|m4v|ogv|ogg)(?:\?[^\s\"'<>]*)?",
    re.I,
)

MEDIA_PATH_RE = re.compile(r"\.(?:m3u8|mpd|mp4|webm|mov|m4v|ogv|ogg)(?:\?|$)", re.I)


def media_kind_from_url(url):
    path = urlsplit(url or "").path.lower()
    for extension, kind in MEDIA_EXTENSIONS.items():
        if path.endswith(extension):
            return kind
    return None


def media_kind_from_content_type(content_type):
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not mime:
        return None
    if mime in ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"):
        return "hls_manifest"
    if mime == "application/dash+xml":
        return "dash_manifest"
    if mime.startswith("video/"):
        return "video"
    return None


def media_hint_kind(url, content_type=None):
    return media_kind_from_content_type(content_type) or media_kind_from_url(url)


def looks_like_media_url(url):
    return media_kind_from_url(url) is not None
