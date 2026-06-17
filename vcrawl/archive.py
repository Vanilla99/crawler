import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import urlparse
from uuid import uuid4


SAFE_PART_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ArchiveOptions:
    root: str
    project: str = "video-crawl"
    html_snapshots: bool = True
    jsonl_sidecar: bool = True
    manifest: bool = True
    warc: bool = False


class ArchiveManager:
    def __init__(self, options):
        self.options = options

    def write_page_snapshot(self, fetch_result, extracted=None):
        if not self.options.html_snapshots and not self.options.jsonl_sidecar and not self.options.warc:
            return None
        os.makedirs(self.options.root, exist_ok=True)
        url = fetch_result.final_url or fetch_result.url
        html_path = None
        html_sha256 = None
        html_bytes = 0
        if self.options.html_snapshots and fetch_result.text:
            html_path = self._page_path(url)
            os.makedirs(os.path.dirname(html_path), exist_ok=True)
            body = fetch_result.text.encode("utf-8")
            with open(html_path, "wb") as fh:
                fh.write(body)
            html_sha256 = hashlib.sha256(body).hexdigest()
            html_bytes = len(body)
        warc_record = self._write_warc_record(fetch_result, url) if self.options.warc and fetch_result.text else None
        record = {
            "url": fetch_result.url,
            "final_url": fetch_result.final_url,
            "status_code": fetch_result.status_code,
            "fetcher": fetch_result.fetcher,
            "title": extracted.title if extracted else None,
            "challenge_detected": bool(fetch_result.challenge_detected or (extracted and extracted.challenge_detected)),
            "links": len(extracted.links) if extracted else 0,
            "videos": len(extracted.videos) if extracted else 0,
            "html_path": _relpath(html_path, self.options.root) if html_path else None,
            "html_sha256": html_sha256,
            "html_bytes": html_bytes,
            "warc_path": warc_record["path"] if warc_record else None,
            "warc_record_id": warc_record["record_id"] if warc_record else None,
            "warc_offset": warc_record["offset"] if warc_record else None,
            "warc_bytes": warc_record["bytes"] if warc_record else 0,
            "archived_at": time.time(),
        }
        if self.options.jsonl_sidecar:
            self._append_jsonl("pages.jsonl", record)
        return record

    def write_sidecars(self, store):
        os.makedirs(self.options.root, exist_ok=True)
        videos = store.list_videos(limit=100000)
        downloads = store.recent_downloads(limit=100000)
        assets = _assets_from_downloads(downloads)
        if self.options.jsonl_sidecar:
            self._write_jsonl("videos.jsonl", videos)
            self._write_jsonl("assets.jsonl", assets)
        manifest = self.write_manifest(store, videos=videos, downloads=downloads, assets=assets)
        return {
            "root": self.options.root,
            "videos": len(videos),
            "downloads": len(downloads),
            "assets": len(assets),
            "manifest": manifest,
        }

    def write_manifest(self, store, videos=None, downloads=None, assets=None):
        if not self.options.manifest:
            return None
        videos = videos if videos is not None else store.list_videos(limit=100000)
        downloads = downloads if downloads is not None else store.recent_downloads(limit=100000)
        assets = assets if assets is not None else _assets_from_downloads(downloads)
        stats = store.stats()
        manifest = {
            "archive_version": 1,
            "project": self.options.project,
            "generated_at": time.time(),
            "root": self.options.root,
            "format": {
                "html_snapshots": self.options.html_snapshots,
                "jsonl_sidecar": self.options.jsonl_sidecar,
                "warc": self.options.warc,
                "warc_status": "not_enabled" if not self.options.warc else "written",
            },
            "files": {
                "pages": "pages.jsonl",
                "videos": "videos.jsonl",
                "assets": "assets.jsonl",
                "manifest": "manifest.json",
                "warc": "archive.warc" if self.options.warc else None,
            },
            "counts": {
                "pages": stats.get("pages", 0),
                "videos": len(videos),
                "downloads": len(downloads),
                "assets": len(assets),
                "html_snapshots": _jsonl_count(os.path.join(self.options.root, "pages.jsonl")),
                "warc_records": _warc_count(self._warc_path()) if self.options.warc else 0,
            },
            "checksums": {
                "warc_sha256": _sha256_file(self._warc_path()) if self.options.warc and os.path.exists(self._warc_path()) else None,
            },
            "notes": _manifest_notes(self.options),
        }
        path = os.path.join(self.options.root, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return manifest

    def _page_path(self, url):
        parsed = urlparse(url)
        domain = _safe_part(parsed.netloc or "unknown")
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".html"
        return os.path.join(self.options.root, "pages", domain, name)

    def _warc_path(self):
        return os.path.join(self.options.root, "archive.warc")

    def _write_warc_record(self, fetch_result, url):
        os.makedirs(self.options.root, exist_ok=True)
        body = (fetch_result.text or "").encode("utf-8")
        http_message = _http_response_bytes(fetch_result, body)
        record_id = "<urn:uuid:%s>" % uuid4()
        header = _warc_header(
            url=url,
            record_id=record_id,
            content_length=len(http_message),
            block_digest=_sha256_bytes(http_message),
            payload_digest=_sha256_bytes(body),
        )
        payload = header + http_message + b"\r\n\r\n"
        path = self._warc_path()
        offset = os.path.getsize(path) if os.path.exists(path) else 0
        with open(path, "ab") as fh:
            fh.write(payload)
        return {
            "path": _relpath(path, self.options.root),
            "record_id": record_id,
            "offset": offset,
            "bytes": len(payload),
        }

    def _append_jsonl(self, filename, record):
        path = os.path.join(self.options.root, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")

    def _write_jsonl(self, filename, rows):
        path = os.path.join(self.options.root, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                fh.write("\n")


def archive_options_from_config(config):
    root = config.archive.root
    if root and not os.path.isabs(root) and os.path.isabs(config.storage.state):
        root = os.path.join(os.path.dirname(config.storage.state), root)
    return ArchiveOptions(
        root=root,
        project=config.project,
        html_snapshots=config.archive.html_snapshots,
        jsonl_sidecar=config.archive.jsonl_sidecar,
        manifest=config.archive.manifest,
        warc=config.archive.warc,
    )


def verify_archive(root):
    errors = []
    warnings = []
    manifest_path = os.path.join(root, "manifest.json")
    if not os.path.exists(manifest_path):
        errors.append("missing manifest.json")
        manifest = None
    else:
        manifest = _read_json(manifest_path, errors)
    pages_path = os.path.join(root, "pages.jsonl")
    videos_path = os.path.join(root, "videos.jsonl")
    assets_path = os.path.join(root, "assets.jsonl")
    pages = _read_jsonl(pages_path, errors, required=False)
    videos = _read_jsonl(videos_path, errors, required=False)
    assets = _read_jsonl(assets_path, errors, required=False)
    warc_path = None
    warc_records = 0
    warc_sha256 = None
    for page in pages:
        rel = page.get("html_path")
        if rel:
            path = os.path.join(root, rel)
            if not os.path.exists(path):
                errors.append("missing html snapshot: %s" % rel)
            else:
                expected = page.get("html_sha256")
                if expected and _sha256_file(path) != expected:
                    errors.append("html checksum mismatch: %s" % rel)
        warc_rel = page.get("warc_path")
        if warc_rel:
            path = os.path.join(root, warc_rel)
            if not os.path.exists(path):
                errors.append("missing WARC file: %s" % warc_rel)
            else:
                warc_path = path
    for asset in assets:
        path = asset.get("path")
        if path and not os.path.exists(path):
            warnings.append("asset path missing on disk: %s" % path)
    if manifest and manifest.get("format", {}).get("warc"):
        warc_rel = manifest.get("files", {}).get("warc") or "archive.warc"
        path = os.path.join(root, warc_rel)
        expected_records = (
            len(pages)
            or int(manifest.get("counts", {}).get("html_snapshots") or 0)
            or int(manifest.get("counts", {}).get("pages") or 0)
        )
        if not os.path.exists(path) and expected_records:
            errors.append("missing WARC file: %s" % warc_rel)
        elif os.path.exists(path):
            warc_path = path
        else:
            warnings.append("WARC enabled but no fetched pages were archived")
    if warc_path:
        warc_records = _warc_count(warc_path)
        warc_sha256 = _sha256_file(warc_path)
        if warc_records <= 0:
            errors.append("WARC file has no response records: %s" % _relpath(warc_path, root))
    return {
        "ok": not errors,
        "root": root,
        "manifest": bool(manifest),
        "pages": len(pages),
        "videos": len(videos),
        "assets": len(assets),
        "warc": bool(warc_path),
        "warc_records": warc_records,
        "warc_sha256": warc_sha256,
        "errors": errors,
        "warnings": warnings,
    }


def _assets_from_downloads(downloads):
    assets = []
    for row in downloads:
        output = row.get("output_path")
        metadata = _metadata(row.get("metadata_json"))
        if output:
            assets.append(_asset_record("media", output, row))
        thumbnail = metadata.get("thumbnail")
        if thumbnail:
            assets.append(_asset_record("thumbnail", thumbnail, row))
        subtitles = metadata.get("subtitles") or metadata.get("subtitle")
        if subtitles:
            for path in _as_list(subtitles):
                assets.append(_asset_record("subtitle", path, row))
        manifest = metadata.get("manifest") or metadata.get("manifest_path")
        if manifest:
            assets.append(_asset_record("manifest", manifest, row))
    return assets


def _asset_record(kind, path, row):
    exists = bool(path and os.path.exists(path))
    return {
        "kind": kind,
        "path": path,
        "exists": exists,
        "sha256": _sha256_file(path) if exists and os.path.isfile(path) else None,
        "bytes": os.path.getsize(path) if exists and os.path.isfile(path) else 0,
        "page_url": row.get("page_url"),
        "media_url": row.get("media_url"),
        "status": row.get("status"),
        "resolver": row.get("resolver"),
    }


def _metadata(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _as_list(value):
    if isinstance(value, list):
        return value
    return [value]


def _read_json(path, errors):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        errors.append("invalid json %s: %s" % (path, exc))
        return None


def _read_jsonl(path, errors, required=False):
    if not os.path.exists(path):
        if required:
            errors.append("missing %s" % path)
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                errors.append("invalid jsonl %s:%s: %s" % (path, line_no, exc))
    return rows


def _jsonl_count(path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _warc_header(url, record_id, content_length, block_digest, payload_digest):
    lines = [
        "WARC/1.1",
        "WARC-Type: response",
        "WARC-Record-ID: %s" % record_id,
        "WARC-Date: %s" % datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "WARC-Target-URI: %s" % _header_value(url),
        "Content-Type: application/http; msgtype=response",
        "Content-Length: %s" % content_length,
        "WARC-Block-Digest: sha256:%s" % block_digest,
        "WARC-Payload-Digest: sha256:%s" % payload_digest,
        "",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


def _http_response_bytes(fetch_result, body):
    status_code = int(fetch_result.status_code or 0)
    reason = _reason_phrase(status_code)
    lines = ["HTTP/1.1 %s %s" % (status_code, reason)]
    headers = dict(fetch_result.headers or {})
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for key, value in headers.items():
        name = str(key)
        if name.lower() in ("content-length", "transfer-encoding", "connection"):
            continue
        lines.append("%s: %s" % (_header_name(name), _header_value(value)))
    if "content-type" not in lowered:
        lines.append("Content-Type: text/html; charset=utf-8")
    lines.append("Content-Length: %s" % len(body))
    lines.extend(["", ""])
    return "\r\n".join(lines).encode("utf-8") + body


def _reason_phrase(status_code):
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Status"


def _header_name(value):
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return cleaned or "X-VCrawl-Header"


def _header_value(value):
    return str(value).replace("\r", " ").replace("\n", " ")


def _warc_count(path):
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, "rb") as fh:
        for line in fh:
            if line.rstrip(b"\r\n") == b"WARC-Type: response":
                count += 1
    return count


def _manifest_notes(options):
    notes = ["JSONL sidecars and HTML snapshots are standard-library friendly."]
    if options.warc:
        notes.append("WARC response records are written for fetched HTML pages as archive.warc.")
        notes.append("WACZ packaging is not emitted yet; use the WARC plus sidecars as the portable archive base.")
    return notes


def _safe_part(value):
    cleaned = SAFE_PART_RE.sub("_", value).strip("._-")
    return cleaned[:160] or "unknown"


def _relpath(path, root):
    return os.path.relpath(path, root)
