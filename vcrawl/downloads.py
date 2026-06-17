import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .media_tools import ffmpeg_thumbnail, ffprobe
from .models import DownloadResult, VideoCandidate
from .resolvers import BuiltinMediaResolver, YtDlpResolver


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class DownloadOptions:
    output_dir: str
    quality: str = "best"
    overwrite: bool = False
    filename_template: str = "{title_or_hash}.{ext}"
    probe: bool = False
    thumbnail: bool = False
    thumbnail_at_seconds: int = 1
    concurrency: int = 2
    user_agent: str = "vcrawl/0.1"


class DownloadManager:
    def __init__(self, options, store=None):
        self.options = options
        self.store = store
        self.builtin = BuiltinMediaResolver()
        self.yt_dlp = YtDlpResolver()

    def download_candidate(self, candidate):
        if self.builtin.can_resolve(candidate.media_url):
            result = self._download_direct(candidate)
        else:
            result = self._download_with_ytdlp(candidate)
        if self.store:
            self.store.record_download(result)
        return result

    def download_rows(self, rows, limit=None):
        selected = rows[: limit or len(rows)]
        candidates = [candidate_from_row(row) for row in selected]
        concurrency = max(1, int(self.options.concurrency or 1))
        if concurrency == 1 or len(candidates) <= 1:
            return [self.download_candidate(candidate) for candidate in candidates]
        results = []
        with ThreadPoolExecutor(max_workers=min(concurrency, len(candidates))) as executor:
            futures = [executor.submit(self.download_candidate, candidate) for candidate in candidates]
            for future in as_completed(futures):
                results.append(future.result())
        return results

    def _download_direct(self, candidate):
        os.makedirs(self.options.output_dir, exist_ok=True)
        output_path = self._output_path(candidate)
        if os.path.exists(output_path) and not self.options.overwrite:
            return DownloadResult(
                page_url=candidate.page_url,
                media_url=candidate.media_url,
                status="downloaded",
                output_path=output_path,
                resolver="builtin",
                metadata={"skipped_existing": "true"},
            )
        try:
            request = Request(candidate.media_url, headers={"User-Agent": self.options.user_agent})
            with urlopen(request, timeout=60) as response:
                with open(output_path, "wb") as fh:
                    shutil.copyfileobj(response, fh)
            metadata = {}
            metadata.update(self._post_process(output_path))
            return DownloadResult(
                page_url=candidate.page_url,
                media_url=candidate.media_url,
                status="downloaded",
                output_path=output_path,
                resolver="builtin",
                metadata=metadata,
            )
        except Exception as exc:
            return DownloadResult(
                page_url=candidate.page_url,
                media_url=candidate.media_url,
                status="failed",
                output_path=output_path,
                resolver="builtin",
                error=str(exc),
            )

    def _download_with_ytdlp(self, candidate):
        if not self.yt_dlp.available():
            return DownloadResult(
                page_url=candidate.page_url,
                media_url=candidate.media_url,
                status="failed",
                resolver="yt_dlp",
                error="yt-dlp is not installed or not on PATH",
            )
        os.makedirs(self.options.output_dir, exist_ok=True)
        output_template = os.path.join(self.options.output_dir, "%(title).180B-%(id)s.%(ext)s")
        command = [
            "yt-dlp",
            "-f",
            self.options.quality,
            "-o",
            output_template,
            "--print",
            "after_move:filepath",
            candidate.media_url,
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3600,
            check=False,
        )
        if completed.returncode != 0:
            return DownloadResult(
                page_url=candidate.page_url,
                media_url=candidate.media_url,
                status="failed",
                resolver="yt_dlp",
                error=completed.stderr.strip() or "yt-dlp failed",
            )
        output_path = _last_nonempty_line(completed.stdout)
        metadata = self._post_process(output_path) if output_path else {}
        return DownloadResult(
            page_url=candidate.page_url,
            media_url=candidate.media_url,
            status="downloaded",
            output_path=output_path,
            resolver="yt_dlp",
            metadata=metadata,
        )

    def _output_path(self, candidate):
        ext = _extension_for_url(candidate.media_url) or "bin"
        title_or_hash = _safe_name(candidate.title or _short_hash(candidate.media_url))
        filename = self.options.filename_template.format(
            title_or_hash=title_or_hash,
            ext=ext,
            hash=_short_hash(candidate.media_url),
        )
        return os.path.join(self.options.output_dir, filename)

    def _post_process(self, output_path):
        metadata = {}
        if self.options.probe:
            metadata["ffprobe"] = json.dumps(ffprobe(output_path), sort_keys=True)
        if self.options.thumbnail:
            thumbnail_path = _thumbnail_path(output_path)
            metadata["thumbnail"] = ffmpeg_thumbnail(
                output_path,
                thumbnail_path,
                at_seconds=self.options.thumbnail_at_seconds,
            )
        return metadata


def candidate_from_row(row):
    metadata = row.get("metadata_json") or "{}"
    if isinstance(metadata, str):
        try:
            metadata_dict = json.loads(metadata)
        except json.JSONDecodeError:
            metadata_dict = {}
    else:
        metadata_dict = metadata or {}
    return VideoCandidate(
        page_url=row["page_url"],
        media_url=row["media_url"],
        kind=row.get("kind") or "video",
        title=row.get("title"),
        source=row.get("source") or "store",
        metadata=metadata_dict,
    )


def _extension_for_url(url):
    parsed = urlparse(url)
    basename = os.path.basename(parsed.path)
    if "." in basename:
        return basename.rsplit(".", 1)[1].lower()
    mime_type, _encoding = mimetypes.guess_type(url)
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed.lstrip(".")
    return None


def _safe_name(value):
    cleaned = SAFE_NAME_RE.sub("_", value).strip("._-")
    return cleaned[:180] or "media"


def _short_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _last_nonempty_line(value):
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _thumbnail_path(output_path):
    root, _ext = os.path.splitext(output_path)
    return root + ".jpg"
