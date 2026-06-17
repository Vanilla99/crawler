import json
import shutil
import subprocess


MEDIA_EXTENSIONS = (".m3u8", ".mpd", ".mp4", ".webm", ".mov", ".m4v", ".ogv", ".ogg")


class BuiltinMediaResolver:
    name = "builtin"

    def can_resolve(self, media_url):
        lower = media_url.split("?", 1)[0].lower()
        return lower.endswith(MEDIA_EXTENSIONS)

    def resolve(self, candidate):
        return {
            "resolver": self.name,
            "page_url": candidate.page_url,
            "media_url": candidate.media_url,
            "kind": candidate.kind,
            "title": candidate.title,
            "downloadable": self.can_resolve(candidate.media_url),
        }


class YtDlpResolver:
    name = "yt_dlp"

    def available(self):
        return shutil.which("yt-dlp") is not None

    def resolve(self, url, timeout_seconds=30):
        if not self.available():
            raise RuntimeError("yt-dlp is not installed or not on PATH")
        command = ["yt-dlp", "--dump-single-json", "--skip-download", url]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "yt-dlp failed")
        return json.loads(completed.stdout)
