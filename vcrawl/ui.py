import html
import json
import mimetypes
import os
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .archive import ArchiveManager, archive_options_from_config, verify_archive
from .capabilities import build_capability_report
from .config import config_from_dict, load_config
from .engine import CrawlEngine
from .fetchers import BrowserFetcher, HttpFetcher
from .plugins import list_builtin_plugins, load_plugin, run_plugin_tests
from .queue_backends import make_queue_backend
from .storage import SQLiteStore
from .workers import CrawlWorker, DownloadWorker


def serve(config_path, host="127.0.0.1", port=8765):
    config_path = os.path.abspath(config_path)

    def current_config():
        return load_config(config_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                return self._handle_api(parsed)
            body = _html_page(current_config().project).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                return self._handle_api_post(parsed)
            self._send_json(404, {"error": "not found"})

        def _handle_api(self, parsed):
            query = parse_qs(parsed.query)
            config = current_config()
            if parsed.path == "/api/file":
                return self._send_project_file(config, _str_arg(query, "path"))
            store = SQLiteStore(config.storage.state)
            try:
                if parsed.path == "/api/summary":
                    payload = {
                        "project": config.project,
                        "stats": store.stats(),
                        "runs": store.recent_runs(limit=_int_arg(query, "limit", 5)),
                        "downloads": store.recent_downloads(limit=10),
                        "workers": store.list_workers(limit=10),
                        "controls": store.list_controls(),
                    }
                elif parsed.path == "/api/videos":
                    payload = {
                        "videos": store.search_videos(
                            limit=_int_arg(query, "limit", 200),
                            query=_str_arg(query, "q"),
                            download_status=_str_arg(query, "status"),
                        )
                    }
                elif parsed.path == "/api/pages":
                    payload = {
                        "pages": store.list_pages(
                            limit=_int_arg(query, "limit", 200),
                            query=_str_arg(query, "q"),
                        )
                    }
                elif parsed.path == "/api/queue":
                    payload = {
                        "backend": config.queue.backend,
                        "stale_after_seconds": config.queue.stale_after_seconds,
                        "stats": store.queue_stats(),
                        "queue": store.list_queue(
                            limit=_int_arg(query, "limit", 200),
                            status=_str_arg(query, "status"),
                        )
                    }
                elif parsed.path == "/api/runs":
                    payload = {"runs": store.recent_runs(limit=_int_arg(query, "limit", 50))}
                elif parsed.path == "/api/downloads":
                    payload = {
                        "downloads": store.list_downloads(
                            limit=_int_arg(query, "limit", 100),
                            status=_str_arg(query, "status"),
                            query=_str_arg(query, "q"),
                        )
                    }
                elif parsed.path == "/api/workers":
                    payload = {"workers": store.list_workers(limit=_int_arg(query, "limit", 100))}
                elif parsed.path == "/api/logs":
                    payload = {
                        "events": store.list_events(
                            limit=_int_arg(query, "limit", 200),
                            event_type=_str_arg(query, "type"),
                        )
                    }
                elif parsed.path == "/api/timeline":
                    payload = {
                        "timeline": store.list_timeline(
                            limit=_int_arg(query, "limit", 200),
                            url=_str_arg(query, "url"),
                            phase=_str_arg(query, "phase"),
                            status=_str_arg(query, "status"),
                        ),
                        "summary": store.timeline_summary(limit=_int_arg(query, "summary_limit", 50)),
                    }
                elif parsed.path == "/api/policies":
                    payload = _policy_payload(config, store)
                elif parsed.path == "/api/capabilities":
                    payload = build_capability_report(config=config)
                elif parsed.path == "/api/plugins":
                    payload = _plugin_payload(config)
                elif parsed.path == "/api/archive":
                    payload = _archive_payload(config, store)
                elif parsed.path == "/api/cockpit":
                    payload = _cockpit_payload(config, store)
                elif parsed.path == "/api/config":
                    payload = _read_config_payload(config_path)
                elif parsed.path == "/api/assistant":
                    payload = _assistant_state(config)
                else:
                    payload = {"error": "not found"}
                    return self._send_json(404, payload)
            finally:
                store.close()
            return self._send_json(200, payload)

        def _handle_api_post(self, parsed):
            payload = self._read_json()
            config = current_config()
            if parsed.path == "/api/config":
                try:
                    text = payload.get("text") or json.dumps(payload.get("config") or {}, indent=2)
                    _validate_config_text(config_path, text)
                    with open(config_path, "w", encoding="utf-8") as fh:
                        fh.write(text.rstrip() + "\n")
                except Exception as exc:
                    return self._send_json(400, {"error": str(exc)})
                return self._send_json(200, {"status": "saved"})
            if parsed.path == "/api/policies":
                try:
                    updated = _update_policy_config(config_path, payload)
                except Exception as exc:
                    return self._send_json(400, {"error": str(exc)})
                store = SQLiteStore(updated.storage.state)
                try:
                    store.record_event("control", message="policy updated", data=payload)
                    response = _policy_payload(updated, store)
                    response["status"] = "saved"
                finally:
                    store.close()
                return self._send_json(200, response)
            if parsed.path == "/api/plugins/test":
                try:
                    response = _run_plugin_test_payload(payload)
                except Exception as exc:
                    return self._send_json(400, {"error": str(exc)})
                return self._send_json(200, response)
            if parsed.path == "/api/archive/write":
                store = SQLiteStore(config.storage.state)
                try:
                    manager = ArchiveManager(archive_options_from_config(config))
                    result = manager.write_sidecars(store)
                    store.record_event("control", message="archive refreshed", data={"root": result.get("root")})
                    response = _archive_payload(config, store)
                    response["status"] = "written"
                    response["write_result"] = result
                except Exception as exc:
                    return self._send_json(400, {"error": str(exc)})
                finally:
                    store.close()
                return self._send_json(200, response)
            if parsed.path == "/api/archive/verify":
                store = SQLiteStore(config.storage.state)
                try:
                    result = verify_archive(archive_options_from_config(config).root)
                    store.record_event("control", message="archive verified", data={"ok": result.get("ok")})
                    response = _archive_payload(config, store)
                    response["status"] = "verified"
                    response["verification"] = result
                except Exception as exc:
                    return self._send_json(400, {"error": str(exc)})
                finally:
                    store.close()
                return self._send_json(200, response)
            if parsed.path == "/api/assistant/inspect":
                store = SQLiteStore(config.storage.state)
                try:
                    response = _assistant_inspect_payload(config, payload)
                    store.record_event(
                        "control",
                        message="assistant inspect",
                        url=response["url"],
                        data={
                            "fetcher": response["fetcher"],
                            "videos": response["summary"]["videos"],
                            "challenge_detected": response["summary"]["challenge_detected"],
                        },
                    )
                except Exception as exc:
                    return self._send_json(400, {"error": str(exc)})
                finally:
                    store.close()
                return self._send_json(200, response)
            if parsed.path == "/api/assistant/apply":
                try:
                    updated = _apply_assistant_config(config_path, payload)
                except Exception as exc:
                    return self._send_json(400, {"error": str(exc)})
                store = SQLiteStore(updated.storage.state)
                try:
                    store.record_event("control", message="assistant config applied", data=payload)
                    response = _assistant_state(updated)
                    response["status"] = "saved"
                finally:
                    store.close()
                return self._send_json(200, response)

            store = SQLiteStore(config.storage.state)
            try:
                if parsed.path == "/api/control/start-crawl":
                    max_pages = int(payload.get("max_pages") or 100)
                    resume = bool(payload.get("resume", True))
                    store.set_control("paused", False)
                    store.record_event("control", message="start crawl", data={"max_pages": max_pages, "resume": resume})
                    worker_name = _start_crawl_thread(config_path, max_pages=max_pages, resume=resume)
                    response = {"status": "started", "worker": worker_name}
                elif parsed.path == "/api/control/start-download":
                    limit = payload.get("limit")
                    limit = int(limit) if limit not in (None, "") else None
                    store.set_control("paused", False)
                    store.record_event("control", message="start download", data={"limit": limit})
                    worker_name = _start_download_thread(config_path, limit=limit)
                    response = {"status": "started", "worker": worker_name}
                elif parsed.path == "/api/control/pause":
                    store.set_control("paused", True)
                    store.record_event("control", message="pause requested")
                    response = {"status": "paused"}
                elif parsed.path == "/api/control/resume-crawl":
                    max_pages = int(payload.get("max_pages") or 100)
                    store.set_control("paused", False)
                    store.record_event("control", message="resume crawl", data={"max_pages": max_pages})
                    worker_name = _start_crawl_thread(config_path, max_pages=max_pages, resume=True)
                    response = {"status": "started", "worker": worker_name}
                elif parsed.path == "/api/control/clear-queue":
                    status = payload.get("status") or None
                    cleared = store.clear_queue(status=status)
                    store.record_event("control", message="clear queue", data={"status": status, "cleared": cleared})
                    response = {"status": "cleared", "cleared": cleared}
                elif parsed.path == "/api/control/recover-queue":
                    response = _recover_queue(config, store, payload)
                    store.record_event(
                        "control",
                        message="recover stale queue",
                        data={
                            "backend": response["backend"],
                            "stale_after_seconds": response["stale_after_seconds"],
                            "recovered": response["recovered"],
                        },
                    )
                elif parsed.path == "/api/control/retry-downloads":
                    limit = payload.get("limit")
                    limit = int(limit) if limit not in (None, "") else None
                    retried = store.retry_failed_downloads(limit=limit)
                    store.set_control("paused", False)
                    store.record_event("control", message="retry downloads", data={"limit": limit, "retried": retried})
                    worker_name = _start_download_thread(config_path, limit=limit)
                    response = {"status": "started", "retried": retried, "worker": worker_name}
                elif parsed.path == "/api/control/retry-download":
                    page_url, media_url = _download_identity(payload)
                    retried = store.retry_download(page_url, media_url)
                    store.set_control("paused", False)
                    store.record_event("control", message="retry download", url=media_url, data={"page_url": page_url, "retried": retried})
                    worker_name = _start_download_thread(config_path, limit=1) if retried else None
                    response = {"status": "started" if retried else "unchanged", "retried": retried, "worker": worker_name}
                elif parsed.path == "/api/control/skip-download":
                    page_url, media_url = _download_identity(payload)
                    skipped = store.skip_download(page_url, media_url)
                    store.record_event("control", message="skip download", url=media_url, data={"page_url": page_url, "skipped": skipped})
                    response = {"status": "skipped" if skipped else "unchanged", "skipped": skipped}
                else:
                    return self._send_json(404, {"error": "not found"})
            except Exception as exc:
                return self._send_json(400, {"error": str(exc)})
            finally:
                store.close()
            return self._send_json(200, response)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

        def _send_project_file(self, config, path):
            if not path:
                return self._send_json(400, {"error": "path is required"})
            root = os.path.abspath(config.storage.files)
            candidate = os.path.abspath(path)
            try:
                if os.path.commonpath([root, candidate]) != root:
                    return self._send_json(403, {"error": "path is outside storage.files"})
            except ValueError:
                return self._send_json(403, {"error": "invalid file path"})
            if not os.path.isfile(candidate):
                return self._send_json(404, {"error": "file not found"})
            content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
            with open(candidate, "rb") as fh:
                body = fh.read()
            self._send(200, body, content_type)

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print("vcrawl ui: http://%s:%s" % (host, port))
    server.serve_forever()


def _int_arg(query, name, default):
    try:
        return int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default


def _str_arg(query, name):
    value = (query.get(name) or [None])[0]
    return value or None


def _download_identity(payload):
    page_url = payload.get("page_url")
    media_url = payload.get("media_url")
    if not page_url or not media_url:
        raise ValueError("page_url and media_url are required")
    return page_url, media_url


def _cockpit_payload(config, store):
    stats = store.stats()
    runs = store.recent_runs(limit=5)
    workers = store.list_workers(limit=20)
    queue_stats = store.queue_stats()
    queue_rows = store.list_queue(limit=50)
    recent_pages = store.list_pages(limit=20)
    recent_videos = store.search_videos(limit=20)
    recent_downloads = store.list_downloads(limit=20)
    events = store.list_events(limit=50)
    timeline = store.list_timeline(limit=50)
    failures = _cockpit_failure_summary(
        pages=recent_pages,
        downloads=recent_downloads,
        queue_rows=queue_rows,
        events=events,
        timeline=timeline,
    )
    active_workers = [worker for worker in workers if worker.get("status") == "running"]
    current_urls = [worker.get("current_url") for worker in active_workers if worker.get("current_url")]
    mode = "paused" if stats.get("paused") else "running" if active_workers else "idle"
    latest_run = runs[0] if runs else None
    return {
        "project": config.project,
        "mode": mode,
        "stats": stats,
        "latest_run": latest_run,
        "runs": runs,
        "workers": workers,
        "current_urls": current_urls,
        "queue": {
            "backend": config.queue.backend,
            "stale_after_seconds": config.queue.stale_after_seconds,
            "stats": queue_stats,
            "recent": queue_rows[:20],
        },
        "recent_pages": recent_pages,
        "recent_videos": recent_videos,
        "recent_downloads": recent_downloads,
        "recent_events": events[:20],
        "recent_timeline": timeline[:20],
        "failures": failures,
        "actions": _cockpit_actions(stats, queue_stats, failures),
    }


def _cockpit_failure_summary(pages=None, downloads=None, queue_rows=None, events=None, timeline=None):
    categories = {
        "fetch_error": _cockpit_category(
            "fetch_error",
            "Fetch error",
            "Check URL availability, timeout, proxy, cookies, and retry policy before retrying.",
            "Retry crawl after fixing the network or policy setting.",
        ),
        "challenge": _cockpit_category(
            "challenge",
            "Challenge",
            "Use a slower policy or a user-owned browser profile after manual verification.",
            "Open the page manually, verify access, then resume the crawl.",
        ),
        "no_video": _cockpit_category(
            "no_video",
            "No video",
            "The page fetched successfully but produced no video candidates.",
            "Inspect diagnostics, broaden scope if appropriate, or add a site plugin.",
        ),
        "dynamic_player": _cockpit_category(
            "dynamic_player",
            "Dynamic player",
            "The page has player or network-loading signals that static HTML may miss.",
            "Try browser fetching and the dynamic video capability package.",
        ),
        "scope_robots": _cockpit_category(
            "scope_robots",
            "Scope or robots",
            "A request appears to be skipped by crawl scope or robots policy.",
            "Review allowed domains, max depth, and robots settings before retrying.",
        ),
        "download_failed": _cockpit_category(
            "download_failed",
            "Download failed",
            "A queued media transfer failed after discovery.",
            "Retry failed downloads or install/verify media tools for the resolver.",
        ),
    }

    for page in pages or []:
        url = page.get("url")
        status_code = _safe_int(page.get("status_code"), default=0)
        video_count = _safe_int(page.get("video_count"), default=0)
        challenge_detected = _truthy(page.get("challenge_detected"))
        error = page.get("error")
        dynamic_score = _page_dynamic_score(page)
        if challenge_detected:
            _append_failure(categories["challenge"], url=url, status=status_code, error=error)
        elif error or status_code == 0 or status_code >= 400:
            _append_failure(categories["fetch_error"], url=url, status=status_code, error=error)
        if not challenge_detected and not error and 0 < status_code < 400 and video_count == 0:
            _append_failure(categories["no_video"], url=url, status=status_code)
        if not challenge_detected and video_count == 0 and dynamic_score > 0:
            _append_failure(
                categories["dynamic_player"],
                url=url,
                status=status_code,
                extra={"dynamic_score": dynamic_score},
            )

    for download in downloads or []:
        if download.get("status") == "failed":
            _append_failure(
                categories["download_failed"],
                url=download.get("page_url"),
                target=download.get("media_url"),
                status=download.get("status"),
                error=download.get("error"),
            )

    for row in queue_rows or []:
        text = " ".join(str(row.get(key) or "") for key in ("status", "error", "url"))
        if _looks_like_scope_or_robots(text):
            _append_failure(
                categories["scope_robots"],
                url=row.get("url"),
                status=row.get("status"),
                error=row.get("error"),
            )
        elif row.get("status") == "failed":
            _append_failure(categories["fetch_error"], url=row.get("url"), status="queue_failed", error=row.get("error"))

    for event in list(events or []) + list(timeline or []):
        text = " ".join(
            str(event.get(key) or "")
            for key in ("message", "error_class", "event_type", "phase", "status", "url")
        )
        if _looks_like_scope_or_robots(text):
            _append_failure(
                categories["scope_robots"],
                url=event.get("url"),
                status=event.get("status") or event.get("event_type"),
                error=event.get("error_class") or event.get("message"),
            )

    ordered = [categories[key] for key in ("fetch_error", "challenge", "no_video", "dynamic_player", "scope_robots", "download_failed")]
    return {
        "total": sum(item["count"] for item in ordered),
        "active_categories": sum(1 for item in ordered if item["count"] > 0),
        "categories": ordered,
    }


def _cockpit_category(identifier, label, advice, action):
    return {
        "id": identifier,
        "label": label,
        "count": 0,
        "advice": advice,
        "action": action,
        "samples": [],
    }


def _append_failure(category, url=None, target=None, status=None, error=None, extra=None):
    category["count"] += 1
    if len(category["samples"]) >= 5:
        return
    sample = {
        "url": url or "",
        "target": target or "",
        "status": status,
        "error": error or "",
    }
    if extra:
        sample.update(extra)
    category["samples"].append(sample)


def _cockpit_actions(stats, queue_stats, failures):
    by_id = {item["id"]: item for item in failures.get("categories") or []}
    return [
        {
            "id": "retry_downloads",
            "label": "Retry failed downloads",
            "enabled": by_id.get("download_failed", {}).get("count", 0) > 0,
            "reason": "Failed downloads can be put back into the recoverable queue.",
        },
        {
            "id": "recover_queue",
            "label": "Recover stale queue",
            "enabled": (queue_stats or {}).get("in_progress", 0) > 0,
            "reason": "Stale in-progress requests can be moved back to pending after a worker interruption.",
        },
        {
            "id": "start_download",
            "label": "Start download",
            "enabled": stats.get("queued_downloads", 0) > 0,
            "reason": "Queued media candidates are waiting for the download worker.",
        },
        {
            "id": "refresh_archive",
            "label": "Refresh archive",
            "enabled": stats.get("pages", 0) > 0 or stats.get("videos", 0) > 0,
            "reason": "Write sidecar files from the latest crawl state.",
        },
        {
            "id": "verify_archive",
            "label": "Verify archive",
            "enabled": True,
            "reason": "Check manifest, sidecar, and WARC consistency.",
        },
    ]


def _page_dynamic_score(page):
    diagnostics = _json_object(page.get("diagnostics_json"))
    signals = diagnostics.get("dynamic_signals") or {}
    return _safe_int(signals.get("dynamic_score"), default=0)


def _json_object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _looks_like_scope_or_robots(text):
    text = (text or "").lower()
    return any(marker in text for marker in ("robot", "robots.txt", "scope", "out of scope", "allowed domain"))


def _assistant_state(config):
    report = build_capability_report(config=config)
    return {
        "project": config.project,
        "seeds": list(config.seeds or []),
        "default_url": (config.seeds or [""])[0] if config.seeds else "",
        "fetch": {
            "default": config.fetch.default,
            "timeout_seconds": config.fetch.timeout_seconds,
            "browser_profile": config.fetch.browser_profile or "",
            "max_depth": config.scope.max_depth,
            "respect_robots": bool(config.scope.respect_robots),
        },
        "network": {
            "http_cache": bool(config.network.http_cache),
            "http_cache_dir": config.network.http_cache_dir or "",
            "cookies_file": config.network.cookies_file or "",
            "proxy_configured": bool(config.network.proxy_url or config.network.proxy_urls),
        },
        "media": {
            "download": bool(config.media.download),
            "max_downloads_per_run": config.media.max_downloads_per_run,
        },
        "capability_packages": report.get("packages") or [],
    }


def _assistant_inspect_payload(config, payload, fetcher=None):
    url = _required_http_url(payload.get("url"))
    use_browser = bool(payload.get("browser"))
    timeout = float(payload.get("timeout") or config.fetch.timeout_seconds)
    fetcher = fetcher or _make_assistant_fetcher(config, use_browser=use_browser, timeout=timeout)
    result, extracted = CrawlEngine(config, fetcher=fetcher).inspect(url)
    diagnostics = extracted.diagnostics if extracted else {}
    videos = [asdict(video) for video in (extracted.videos if extracted else [])]
    links = list(extracted.links if extracted else [])
    capability_report = build_capability_report(config=config)
    data = {
        "url": result.final_url or result.url,
        "requested_url": result.url,
        "status": result.status_code,
        "fetcher": result.fetcher,
        "error": result.error,
        "challenge_detected": bool(result.challenge_detected or (extracted and extracted.challenge_detected)),
        "title": extracted.title if extracted else None,
        "links": links,
        "videos": videos[:20],
        "media_hints": [asdict(hint) for hint in (result.media_hints or [])],
        "diagnostics": diagnostics,
        "diagnostic_summary": _diagnostic_summary(diagnostics),
        "summary": {
            "videos": len(videos),
            "links": len(links),
            "media_hints": len(result.media_hints or []),
            "challenge_detected": bool(result.challenge_detected or (extracted and extracted.challenge_detected)),
            "dynamic_score": _diagnostic_summary(diagnostics)["dynamic_score"],
        },
        "capability_packages": capability_report.get("packages") or [],
    }
    data["recommendations"] = _assistant_recommendations(config, data)
    data["recommended_config"] = _assistant_recommended_config(config, data)
    return data


def _make_assistant_fetcher(config, use_browser=False, timeout=None):
    timeout = timeout if timeout is not None else config.fetch.timeout_seconds
    if use_browser:
        return BrowserFetcher(
            timeout_seconds=timeout,
            headless=config.fetch.browser_headless,
            profile=config.fetch.browser_profile,
        )
    return HttpFetcher(
        timeout_seconds=timeout,
        user_agent=config.fetch.user_agent,
        proxy_url=config.network.proxy_url,
        headers=config.network.headers,
        cookies_file=config.network.cookies_file,
        http_cache=config.network.http_cache,
        cache_dir=_assistant_http_cache_dir(config),
        session_pool=config.network.session_pool,
        session_pool_size=config.network.session_pool_size,
    )


def _assistant_http_cache_dir(config):
    if not config.network.http_cache:
        return None
    if config.network.http_cache_dir:
        return config.network.http_cache_dir
    if config.storage.state:
        return os.path.join(os.path.dirname(config.storage.state), "http-cache")
    return os.path.join(".vcrawl", "http-cache")


def _assistant_recommendations(config, inspect_payload):
    summary = inspect_payload.get("summary") or {}
    diagnostics = inspect_payload.get("diagnostics") or {}
    diag_summary = inspect_payload.get("diagnostic_summary") or {}
    packages = {item["id"]: item for item in inspect_payload.get("capability_packages") or []}
    recommendations = []

    if inspect_payload.get("error"):
        recommendations.append(
            _recommendation(
                "fix_fetch",
                "Fix fetch error",
                "Fetch failed before extraction could run.",
                "Check URL, timeout, proxy, cookies, or site availability.",
            )
        )
        return recommendations
    if summary.get("challenge_detected"):
        recommendations.append(
            _recommendation(
                "manual_verification",
                "Manual verification",
                "The page looks like a human-verification or bot-challenge page.",
                "Use a slower policy or a user-owned browser profile after manual verification.",
            )
        )
    if summary.get("videos", 0) > 0:
        recommendations.append(
            _recommendation(
                "ready_to_crawl",
                "Ready to crawl",
                "This inspect run produced video candidates.",
                "Save this URL as a seed and start a scoped crawl.",
            )
        )
        if not config.media.download:
            recommendations.append(
                _recommendation(
                    "enable_download_queue",
                    "Enable download queue",
                    "Video candidates can be queued as recoverable DownloadTask rows.",
                    "Turn on media.download before a crawl when local archiving is desired.",
                )
            )
    dynamic_score = int(diag_summary.get("dynamic_score") or 0)
    if summary.get("videos", 0) == 0 and dynamic_score > 0 and inspect_payload.get("fetcher") != "browser":
        recommendations.append(
            _recommendation(
                "use_browser_fetcher",
                "Use browser fetcher",
                "The page has dynamic player signals but no static video candidates.",
                "Install the dynamic video package if needed, then inspect with browser fetching.",
                package=packages.get("dynamic-video"),
            )
        )
    if summary.get("videos", 0) == 0 and inspect_payload.get("fetcher") == "browser" and summary.get("media_hints", 0) == 0:
        recommendations.append(
            _recommendation(
                "site_plugin",
                "Add site plugin",
                "Browser rendering completed without visible media network hints.",
                "Use a site plugin or a supported resolver for player-specific extraction.",
            )
        )
    if (diagnostics.get("dynamic_signals") or {}).get("embedded_player_iframes"):
        recommendations.append(
            _recommendation(
                "media_tools",
                "Check media tools",
                "An embedded player was detected.",
                "Install the media package when yt-dlp metadata resolution is appropriate.",
                package=packages.get("media-tools"),
            )
        )
    if not config.network.http_cache:
        recommendations.append(
            _recommendation(
                "enable_http_cache",
                "Enable HTTP cache",
                "Inspect and crawl retries are easier to debug with cached successful HTML responses.",
                "Turn on HTTP cache for repeatable local debugging.",
            )
        )
    if not recommendations:
        recommendations.append(
            _recommendation(
                "review_scope",
                "Review scope",
                "No strong video or dynamic-player signals were found.",
                "Check crawl scope, seed URL quality, or add a site-specific plugin.",
            )
        )
    return recommendations


def _recommendation(identifier, label, reason, action, package=None):
    item = {
        "id": identifier,
        "label": label,
        "reason": reason,
        "action": action,
    }
    if package:
        item["package"] = {
            "id": package.get("id"),
            "label": package.get("label"),
            "status": package.get("status"),
            "missing": package.get("missing") or [],
            "install_commands": package.get("install_commands") or [],
            "verify_commands": package.get("verify_commands") or [],
        }
    return item


def _assistant_recommended_config(config, inspect_payload):
    summary = inspect_payload.get("summary") or {}
    diag_summary = inspect_payload.get("diagnostic_summary") or {}
    should_use_browser = config.fetch.default == "browser" or (
        summary.get("videos", 0) == 0
        and int(diag_summary.get("dynamic_score") or 0) > 0
        and inspect_payload.get("fetcher") != "browser"
    )
    return {
        "url": inspect_payload.get("requested_url") or inspect_payload.get("url") or "",
        "add_seed": True,
        "use_browser": should_use_browser,
        "enable_http_cache": bool(config.network.http_cache) or True,
        "enable_downloads": bool(config.media.download) or summary.get("videos", 0) > 0,
        "browser_profile": config.fetch.browser_profile or (".vcrawl/browser-profile" if should_use_browser else ""),
        "cookies_file": config.network.cookies_file or "",
        "max_depth": config.scope.max_depth,
    }


def _apply_assistant_config(config_path, payload):
    data = _read_config_data(config_path)
    url = _optional_text(payload.get("url"))
    if url:
        url = _required_http_url(url)
    fetch = data.setdefault("fetch", {})
    network = data.setdefault("network", {})
    media = data.setdefault("media", {})
    scope = data.setdefault("scope", {})

    if payload.get("add_seed") and url:
        seeds = list(data.get("seeds") or [])
        if url not in seeds:
            seeds.append(url)
        data["seeds"] = seeds
        domain = _normalize_domain(url)
        if domain:
            allowed = list(scope.get("allowed_domains") or [])
            if domain not in allowed:
                allowed.append(domain)
            scope["allowed_domains"] = allowed
    if "use_browser" in payload:
        fetch["default"] = "browser" if bool(payload.get("use_browser")) else "http"
    if "max_depth" in payload and not _is_blank(payload.get("max_depth")):
        scope["max_depth"] = _nonnegative_int(payload.get("max_depth"), "max_depth")
    if "enable_http_cache" in payload:
        network["http_cache"] = bool(payload.get("enable_http_cache"))
        if network["http_cache"] and not network.get("http_cache_dir"):
            network["http_cache_dir"] = ".vcrawl/http-cache"
    if "enable_downloads" in payload:
        media["download"] = bool(payload.get("enable_downloads"))
    if "browser_profile" in payload:
        fetch["browser_profile"] = _optional_text(payload.get("browser_profile"))
    if "cookies_file" in payload:
        network["cookies_file"] = _optional_text(payload.get("cookies_file"))

    config_from_dict(data)
    _write_config_data(config_path, data)
    return load_config(config_path)


def _diagnostic_summary(diagnostics):
    diagnostics = diagnostics or {}
    signals = diagnostics.get("dynamic_signals") or {}
    return {
        "final_candidates": int(diagnostics.get("final_candidates") or 0),
        "html_candidates": int(diagnostics.get("html_candidates") or 0),
        "regex_candidates": int(diagnostics.get("regex_candidates") or 0),
        "plugin_candidates": int(diagnostics.get("plugin_candidates") or 0),
        "media_hint_candidates": int(diagnostics.get("media_hint_candidates") or 0),
        "dynamic_score": int(signals.get("dynamic_score") or 0),
        "embedded_player_iframes": int(signals.get("embedded_player_iframes") or 0),
        "browser_media_hints": int(signals.get("browser_media_hints") or 0),
        "script_markers": _format_counts(signals.get("dynamic_script_markers") or {}),
        "network_hint_kinds": _format_counts(signals.get("network_media_hints_by_kind") or {}),
        "player_scripts": _format_values(signals.get("player_script_urls") or []),
        "notes": list(diagnostics.get("notes") or []),
        "recommendations": list(diagnostics.get("recommendations") or []),
    }


def _format_counts(values):
    if not values:
        return "-"
    return ", ".join("%s=%s" % (key, values[key]) for key in sorted(values))


def _format_values(values, limit=3):
    values = list(values or [])
    if not values:
        return "-"
    shown = values[:limit]
    suffix = "" if len(values) <= limit else ", +%s more" % (len(values) - limit)
    return ", ".join(shown) + suffix


def _required_http_url(value):
    url = _optional_text(value)
    if not url:
        raise ValueError("url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("url must be an http or https URL")
    return url


def _recover_queue(config, store, payload):
    stale_after = payload.get("stale_after_seconds")
    stale_after = config.queue.stale_after_seconds if stale_after in (None, "") else int(stale_after)
    if stale_after <= 0:
        raise ValueError("stale_after_seconds must be greater than 0")
    queue = make_queue_backend(config, store=store)
    recovered = queue.recover_stale(stale_after)
    return {
        "status": "recovered",
        "backend": config.queue.backend,
        "stale_after_seconds": stale_after,
        "recovered": recovered,
        "queue": store.queue_stats(),
    }


def _start_crawl_thread(config_path, max_pages=100, resume=True):
    def run():
        config = load_config(config_path)
        CrawlWorker(config).run(max_pages=max_pages, resume=resume)

    thread = threading.Thread(target=run, name="vcrawl-crawl", daemon=True)
    thread.start()
    return thread.name


def _start_download_thread(config_path, limit=None):
    def run():
        config = load_config(config_path)
        DownloadWorker(config).run_pending(limit=limit)

    thread = threading.Thread(target=run, name="vcrawl-download", daemon=True)
    thread.start()
    return thread.name


def _read_config_payload(config_path):
    with open(config_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    payload = {"text": text}
    try:
        if config_path.endswith((".yaml", ".yml")):
            import yaml

            payload["config"] = yaml.safe_load(text) or {}
        else:
            payload["config"] = json.loads(text)
    except Exception:
        payload["config"] = None
    return payload


def _validate_config_text(config_path, text):
    if config_path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("YAML config requires optional dependency: pip install vcrawl[yaml]") from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    config_from_dict(data)


def _update_policy_config(config_path, payload):
    data = _read_config_data(config_path)
    fetch = data.setdefault("fetch", {})
    network = data.setdefault("network", {})

    if "default_delay" in payload:
        fetch["delay_per_domain_seconds"] = _nonnegative_float(payload["default_delay"], "default_delay")
    if "failure_threshold" in payload:
        fetch["domain_failure_threshold"] = _nonnegative_int(payload["failure_threshold"], "failure_threshold")
    if "auto_throttle" in payload:
        fetch["auto_throttle"] = bool(payload["auto_throttle"])
    if "auto_throttle_target_concurrency" in payload:
        fetch["auto_throttle_target_concurrency"] = _positive_float(
            payload["auto_throttle_target_concurrency"],
            "auto_throttle_target_concurrency",
        )
    if "auto_throttle_min_delay" in payload:
        fetch["auto_throttle_min_delay_seconds"] = _nonnegative_float(
            payload["auto_throttle_min_delay"],
            "auto_throttle_min_delay",
        )
    if "auto_throttle_max_delay" in payload:
        fetch["auto_throttle_max_delay_seconds"] = _nonnegative_float(
            payload["auto_throttle_max_delay"],
            "auto_throttle_max_delay",
        )
    if "http_cache" in payload:
        network["http_cache"] = bool(payload["http_cache"])
    if "http_cache_dir" in payload:
        network["http_cache_dir"] = _optional_text(payload["http_cache_dir"])
    if "session_pool" in payload:
        network["session_pool"] = bool(payload["session_pool"])
    if "session_pool_size" in payload:
        network["session_pool_size"] = max(1, _nonnegative_int(payload["session_pool_size"], "session_pool_size"))
    if "cookies_file" in payload:
        network["cookies_file"] = _optional_text(payload["cookies_file"])
    if "proxy_url" in payload:
        network["proxy_url"] = _optional_text(payload["proxy_url"])
    if "browser_profile" in payload:
        fetch["browser_profile"] = _optional_text(payload["browser_profile"])

    domain = _normalize_domain(payload.get("domain"))
    if domain:
        if "domain_delay" in payload:
            per_domain_delay = dict(fetch.get("per_domain_delay_seconds") or {})
            if _is_blank(payload["domain_delay"]):
                per_domain_delay.pop(domain, None)
            else:
                per_domain_delay[domain] = _nonnegative_float(payload["domain_delay"], "domain_delay")
            fetch["per_domain_delay_seconds"] = per_domain_delay
        if "domain_failure_threshold" in payload:
            per_domain_thresholds = dict(fetch.get("per_domain_failure_thresholds") or {})
            if _is_blank(payload["domain_failure_threshold"]):
                per_domain_thresholds.pop(domain, None)
            else:
                per_domain_thresholds[domain] = _nonnegative_int(
                    payload["domain_failure_threshold"],
                    "domain_failure_threshold",
                )
            fetch["per_domain_failure_thresholds"] = per_domain_thresholds

    config_from_dict(data)
    _write_config_data(config_path, data)
    return load_config(config_path)


def _read_config_data(config_path):
    with open(config_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if config_path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("YAML config requires optional dependency: pip install vcrawl[yaml]") from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def _write_config_data(config_path, data):
    if config_path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("YAML config requires optional dependency: pip install vcrawl[yaml]") from exc
        text = yaml.safe_dump(data, sort_keys=False)
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_blank(value):
    return value is None or str(value).strip() == ""


def _nonnegative_float(value, name):
    if _is_blank(value):
        raise ValueError("%s is required" % name)
    number = float(value)
    if number < 0:
        raise ValueError("%s must be >= 0" % name)
    return number


def _positive_float(value, name):
    number = _nonnegative_float(value, name)
    if number <= 0:
        raise ValueError("%s must be > 0" % name)
    return number


def _nonnegative_int(value, name):
    if _is_blank(value):
        raise ValueError("%s is required" % name)
    number = int(value)
    if number < 0:
        raise ValueError("%s must be >= 0" % name)
    return number


def _normalize_domain(value):
    text = _optional_text(value)
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else "//" + text)
    domain = parsed.netloc or parsed.path
    return domain.split("@")[-1].split(":")[0].strip().lower()


def _policy_payload(config, store):
    return {
        "fetch": {
            "default_delay": config.fetch.delay_per_domain_seconds,
            "per_domain_delay": config.fetch.per_domain_delay_seconds,
            "failure_threshold": config.fetch.domain_failure_threshold,
            "per_domain_failure_thresholds": config.fetch.per_domain_failure_thresholds,
            "retries": config.fetch.retries,
            "retry_backoff_seconds": config.fetch.retry_backoff_seconds,
            "auto_throttle": config.fetch.auto_throttle,
            "auto_throttle_target_concurrency": config.fetch.auto_throttle_target_concurrency,
            "auto_throttle_min_delay": config.fetch.auto_throttle_min_delay_seconds,
            "auto_throttle_max_delay": config.fetch.auto_throttle_max_delay_seconds,
        },
        "network": {
            "proxy_configured": bool(config.network.proxy_url or config.network.proxy_urls),
            "proxy_count": len(config.network.proxy_urls) + (1 if config.network.proxy_url else 0),
            "headers_count": len(config.network.headers),
            "cookies_file": bool(config.network.cookies_file),
            "cookies_file_path": config.network.cookies_file or "",
            "http_cache": config.network.http_cache,
            "http_cache_dir": config.network.http_cache_dir or "",
            "session_pool": config.network.session_pool,
            "session_pool_size": config.network.session_pool_size,
            "browser_profile": bool(config.fetch.browser_profile),
            "browser_profile_path": config.fetch.browser_profile or "",
        },
        "domains": store.list_domain_state(limit=200),
        "sessions": store.list_session_health(limit=200),
    }


def _plugin_payload(config):
    enabled_builtins = set(config.extract.builtin_plugins or [])
    builtins = []
    for spec in list_builtin_plugins():
        item = _plugin_spec_payload(spec)
        item["enabled"] = spec.name in enabled_builtins
        item["status"] = "enabled" if item["enabled"] else "available"
        item["test_payload"] = {"builtin": spec.name}
        builtins.append(item)

    configured = []
    for path in config.extract.plugin_paths or []:
        item = {
            "path": path,
            "builtin": False,
            "enabled": True,
            "status": "loaded",
            "test_payload": {"path": path},
        }
        try:
            plugin = load_plugin(path, config_map=config.extract.plugin_configs)
            item.update(_plugin_spec_payload(plugin.spec))
            item["config_keys"] = sorted((plugin.config or {}).keys())
        except Exception as exc:
            item.update(
                {
                    "name": os.path.basename(path) or path,
                    "version": "",
                    "capabilities": [],
                    "config_schema": {},
                    "fixtures": 0,
                    "status": "error",
                    "error": str(exc),
                    "config_keys": [],
                }
            )
        configured.append(item)

    return {
        "builtins": builtins,
        "configured": configured,
        "enabled_builtin_names": sorted(enabled_builtins),
        "plugin_paths": list(config.extract.plugin_paths or []),
    }


def _plugin_spec_payload(spec):
    return {
        "name": spec.name,
        "version": spec.version,
        "capabilities": list(spec.capabilities or []),
        "config_schema": spec.config_schema or {},
        "fixtures": len(spec.fixtures or []),
        "path": spec.path,
        "module_path": spec.module_path,
        "builtin": bool(spec.builtin),
    }


def _run_plugin_test_payload(payload):
    builtin = _optional_text(payload.get("builtin"))
    path = _optional_text(payload.get("path"))
    fixture_path = _optional_text(payload.get("fixture_path"))
    config = payload.get("config")
    if config in (None, ""):
        config = None
    if config is not None and not isinstance(config, dict):
        raise ValueError("config must be an object")
    return run_plugin_tests(path=path, fixture_path=fixture_path, builtin=builtin, config=config)


def _archive_payload(config, store):
    options = archive_options_from_config(config)
    manifest_path = os.path.join(options.root, "manifest.json")
    manifest = _read_json_file(manifest_path)
    verification = verify_archive(options.root)
    files = [
        _archive_file_state(options.root, "manifest.json"),
        _archive_file_state(options.root, "pages.jsonl"),
        _archive_file_state(options.root, "videos.jsonl"),
        _archive_file_state(options.root, "assets.jsonl"),
        _archive_file_state(options.root, "archive.warc"),
    ]
    return {
        "config": {
            "enabled": bool(config.archive.enabled),
            "root": options.root,
            "html_snapshots": bool(options.html_snapshots),
            "jsonl_sidecar": bool(options.jsonl_sidecar),
            "manifest": bool(options.manifest),
            "warc": bool(options.warc),
        },
        "stats": store.stats(),
        "manifest": manifest,
        "manifest_path": manifest_path,
        "files": files,
        "verification": verification,
    }


def _archive_file_state(root, relpath):
    path = os.path.join(root, relpath)
    exists = os.path.exists(path)
    return {
        "name": relpath,
        "path": path,
        "exists": exists,
        "bytes": os.path.getsize(path) if exists and os.path.isfile(path) else 0,
        "updated_at": os.path.getmtime(path) if exists else None,
    }


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _html_page(project):
    escaped_project = html.escape(project)
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>vcrawl - {project}</title>
  <style>
    :root {{
      --bg: #f7f8fb;
      --surface: #ffffff;
      --surface-2: #f0f3f8;
      --line: #d8dee8;
      --text: #151a22;
      --muted: #667085;
      --blue: #2563eb;
      --green: #0f8a5f;
      --amber: #b7791f;
      --red: #c2413b;
      --violet: #6d5bd0;
      --shadow: 0 12px 32px rgba(28, 35, 48, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    a {{ color: var(--blue); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .shell {{ display: grid; grid-template-columns: 248px minmax(0, 1fr); min-height: 100vh; }}
    .sidebar {{
      background: #111827;
      color: #eef2ff;
      padding: 24px 18px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    .brand {{ display: flex; align-items: center; gap: 10px; min-height: 36px; }}
    .brand-mark {{
      width: 34px; height: 34px; border-radius: 8px;
      background: linear-gradient(135deg, #2dd4bf, #60a5fa 55%, #a78bfa);
    }}
    .brand-title {{ font-weight: 740; font-size: 17px; }}
    .brand-sub {{ color: #aeb8ca; font-size: 12px; margin-top: 2px; }}
    .nav {{ display: grid; gap: 6px; }}
    .nav button {{
      height: 38px;
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: #cbd5e1;
      text-align: left;
      padding: 0 12px;
      font: inherit;
      cursor: pointer;
    }}
    .nav button.active, .nav button:hover {{ background: #1f2937; color: #ffffff; }}
    .side-group {{ margin-top: auto; display: grid; gap: 8px; }}
    .cmd {{
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 8px;
      padding: 10px;
      color: #dbe4f0;
      background: rgba(255,255,255,0.04);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    main {{ padding: 26px; min-width: 0; }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 20px;
    }}
    h1 {{ margin: 0; font-size: 24px; line-height: 1.2; }}
    .subtle {{ color: var(--muted); font-size: 13px; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .actionbar {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }}
    .button {{
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      padding: 0 12px;
      font: inherit;
      cursor: pointer;
    }}
    .button.primary {{ background: var(--blue); color: white; border-color: var(--blue); }}
    .button.danger {{ color: var(--red); border-color: #f4c7c3; }}
    .button.compact {{ height: 30px; padding: 0 9px; font-size: 12px; }}
    .button:hover {{ box-shadow: 0 4px 14px rgba(31, 41, 55, 0.10); }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .stat {{
      min-height: 84px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px 14px;
      box-shadow: var(--shadow);
    }}
    .stat-label {{ color: var(--muted); font-size: 12px; }}
    .stat-value {{ font-size: 26px; font-weight: 760; margin-top: 8px; }}
    .stat.pages .stat-value {{ color: var(--blue); }}
    .stat.videos .stat-value {{ color: var(--violet); }}
    .stat.downloads .stat-value {{ color: var(--green); }}
    .stat.failures .stat-value {{ color: var(--red); }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
      margin-bottom: 16px;
    }}
    .panel-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }}
    .panel-title {{ font-size: 15px; font-weight: 720; }}
    .filters {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    .assistant-url {{ flex: 1 1 360px; min-width: 260px; }}
    .assistant-actions {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    input, select, textarea {{
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      padding: 0 10px;
      font: inherit;
      min-width: 180px;
    }}
    input.small {{ min-width: 96px; width: 110px; }}
    input[type="checkbox"] {{ min-width: 16px; width: 16px; height: 16px; padding: 0; }}
    textarea {{
      width: 100%;
      height: 460px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
      resize: vertical;
    }}
    video {{
      width: 100%;
      max-height: 360px;
      background: #0f172a;
      border-radius: 8px;
      border: 1px solid var(--line);
    }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{
      border-bottom: 1px solid #edf0f5;
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}
    th {{ color: var(--muted); font-size: 12px; background: #fafbfe; font-weight: 680; }}
    td.truncate, th.truncate {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 2px 9px;
      font-size: 12px;
      font-weight: 680;
      border: 1px solid var(--line);
      color: var(--muted);
      background: var(--surface-2);
    }}
    .badge.downloaded, .badge.fetched, .badge.completed, .badge.available {{ color: var(--green); background: #e9f8f2; border-color: #b7ead8; }}
    .badge.failed, .badge.missing {{ color: var(--red); background: #fff0ef; border-color: #f4c7c3; }}
    .badge.pending, .badge.running, .badge.partial, .badge.queued, .badge.skipped {{ color: var(--amber); background: #fff7e6; border-color: #f1d28f; }}
    .grid-2 {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 0.55fr); gap: 16px; }}
    .policy-editor {{ display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 10px; align-items: center; }}
    .checkline {{ display: inline-flex; align-items: center; gap: 8px; min-height: 36px; color: var(--text); font-size: 13px; }}
    .empty {{ padding: 36px 16px; text-align: center; color: var(--muted); }}
    .detail {{ padding: 14px 16px; border-top: 1px solid var(--line); display: grid; gap: 10px; }}
    .detail-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .detail-kv {{ min-width: 0; }}
    .detail-kv .subtle {{ margin-bottom: 4px; }}
    .hidden {{ display: none; }}
    .mobile-label {{ display: none; }}
    @media (max-width: 1040px) {{
      .shell {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; min-height: auto; }}
      .stats {{ grid-template-columns: repeat(3, minmax(120px, 1fr)); }}
      .grid-2 {{ grid-template-columns: 1fr; }}
      .policy-editor {{ grid-template-columns: repeat(2, minmax(150px, 1fr)); }}
      .detail-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 720px) {{
      main {{ padding: 16px; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--line); padding: 8px 0; }}
      td {{ border: 0; padding: 5px 12px; white-space: normal; }}
      .mobile-label {{ display: inline; color: var(--muted); font-size: 12px; margin-right: 6px; }}
      input, select {{ min-width: 0; width: 100%; }}
      .filters {{ width: 100%; }}
      .policy-editor {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark"></div>
        <div>
          <div class="brand-title">vcrawl</div>
          <div class="brand-sub">Video crawler console</div>
        </div>
      </div>
      <nav class="nav">
        <button class="active" data-tab="assistant">Assistant</button>
        <button data-tab="cockpit">Run Cockpit</button>
        <button data-tab="overview">Overview</button>
        <button data-tab="videos">Videos</button>
        <button data-tab="pages">Pages</button>
        <button data-tab="downloads">Downloads</button>
        <button data-tab="queue">Queue</button>
        <button data-tab="workers">Workers</button>
        <button data-tab="policies">Policies</button>
        <button data-tab="capabilities">Capabilities</button>
        <button data-tab="plugins">Plugins</button>
        <button data-tab="archive">Archive</button>
        <button data-tab="timeline">Timeline</button>
        <button data-tab="logs">Logs</button>
        <button data-tab="config">Config</button>
        <button data-tab="runs">Runs</button>
      </nav>
      <div class="side-group">
        <div class="subtle">Quick commands</div>
        <div class="cmd">python3 -m vcrawl crawl --resume</div>
        <div class="cmd">python3 -m vcrawl download --limit 20</div>
        <div class="cmd">python3 -m vcrawl export --format csv</div>
      </div>
    </aside>
    <main>
      <div class="topbar">
        <div>
          <h1>{project}</h1>
          <div class="subtle" id="last-updated">Loading project state...</div>
        </div>
        <div class="toolbar">
          <button class="button" id="refresh">Refresh</button>
          <a class="button primary" href="/api/summary">API</a>
        </div>
      </div>

      <section class="actionbar">
        <input class="small" id="max-pages" type="number" min="1" value="100" title="Max pages">
        <button class="button primary" id="start-crawl">Start Crawl</button>
        <button class="button" id="resume-crawl">Resume Crawl</button>
        <button class="button danger" id="pause-workers">Pause</button>
        <input class="small" id="download-limit" type="number" min="1" placeholder="Limit" title="Download limit">
        <button class="button" id="start-download">Download</button>
        <button class="button" id="retry-downloads">Retry Failed</button>
        <button class="button danger" id="clear-pending">Clear Pending</button>
        <span class="subtle" id="action-status"></span>
      </section>

      <section class="stats" id="stats"></section>

      <section class="panel tab" id="tab-assistant">
        <div class="panel-head">
          <div>
            <div class="panel-title">Crawl Assistant</div>
            <div class="subtle" id="assistant-summary"></div>
          </div>
          <div class="filters">
            <input class="assistant-url" id="assistant-url" type="url" placeholder="Seed URL">
            <label class="checkline"><input id="assistant-browser" type="checkbox"> Browser</label>
            <button class="button compact primary" id="assistant-inspect">Inspect</button>
          </div>
        </div>
        <div class="detail">
          <div class="detail-grid" id="assistant-result"></div>
          <div id="assistant-recommendations"></div>
        </div>
        <div class="detail">
          <div class="panel-title">Config Actions</div>
          <div class="policy-editor">
            <label class="checkline"><input id="assistant-add-seed" type="checkbox" checked> Add seed</label>
            <label class="checkline"><input id="assistant-use-browser" type="checkbox"> Browser fetch</label>
            <label class="checkline"><input id="assistant-http-cache" type="checkbox"> HTTP cache</label>
            <label class="checkline"><input id="assistant-downloads" type="checkbox"> Download queue</label>
            <input id="assistant-browser-profile" type="text" placeholder="Browser profile">
            <input id="assistant-cookies-file" type="text" placeholder="Cookies file">
            <input id="assistant-max-depth" type="number" min="0" step="1" placeholder="Max depth">
            <button class="button compact primary" id="assistant-apply">Save Config</button>
          </div>
          <div class="assistant-actions">
            <input class="small" id="assistant-max-pages" type="number" min="1" value="20" title="Max pages">
            <button class="button compact" id="assistant-start-crawl">Start Crawl</button>
            <span class="subtle" id="assistant-status"></span>
          </div>
        </div>
      </section>

      <section class="panel tab hidden" id="tab-cockpit">
        <div class="panel-head">
          <div>
            <div class="panel-title">Run Cockpit</div>
            <div class="subtle" id="cockpit-summary"></div>
          </div>
          <div class="filters">
            <label class="checkline"><input id="cockpit-live" type="checkbox"> Live</label>
            <button class="button compact" id="cockpit-refresh">Refresh</button>
          </div>
        </div>
        <div class="detail">
          <div class="detail-grid" id="cockpit-current"></div>
          <div class="stats" id="cockpit-stats"></div>
          <div class="assistant-actions">
            <button class="button compact primary" id="cockpit-retry-downloads">Retry Failed Downloads</button>
            <button class="button compact" id="cockpit-recover-stale">Recover Stale Queue</button>
            <button class="button compact" id="cockpit-start-download">Start Download</button>
            <button class="button compact" id="cockpit-refresh-archive">Refresh Archive</button>
            <button class="button compact" id="cockpit-verify-archive">Verify Archive</button>
            <span class="subtle" id="cockpit-status"></span>
          </div>
        </div>
        <div class="detail">
          <div class="panel-title">Failure Groups</div>
          <table>
            <thead><tr><th>Category</th><th>Count</th><th>Advice</th><th>Action</th><th>Samples</th></tr></thead>
            <tbody id="cockpit-failures-body"></tbody>
          </table>
          <div class="empty hidden" id="cockpit-failures-empty">No failure signals in the recent run window.</div>
        </div>
        <div class="detail">
          <div class="panel-title">Workers</div>
          <table>
            <thead><tr><th>Worker</th><th>Kind</th><th>Status</th><th>Current URL</th><th>Error</th></tr></thead>
            <tbody id="cockpit-workers-body"></tbody>
          </table>
          <div class="empty hidden" id="cockpit-workers-empty">No workers have reported activity yet.</div>
        </div>
        <div class="detail">
          <div class="panel-title">Recent Video Candidates</div>
          <table>
            <thead><tr><th>Title</th><th>Media</th><th>Page</th><th>Source</th><th>Status</th></tr></thead>
            <tbody id="cockpit-videos-body"></tbody>
          </table>
          <div class="empty hidden" id="cockpit-videos-empty">No recent video candidates yet.</div>
        </div>
        <div class="detail">
          <div class="panel-title">Recent Downloads</div>
          <table>
            <thead><tr><th>Media</th><th>Status</th><th>Resolver</th><th>Output</th><th>Error</th></tr></thead>
            <tbody id="cockpit-downloads-body"></tbody>
          </table>
          <div class="empty hidden" id="cockpit-downloads-empty">No recent download activity yet.</div>
        </div>
      </section>

      <section class="panel tab hidden" id="tab-overview">
        <div class="panel-head">
          <div class="panel-title">System State</div>
          <div class="subtle" id="queue-summary"></div>
        </div>
        <div class="grid-2">
          <div>
            <table>
              <thead><tr><th>Recent Download</th><th>Status</th><th>Resolver</th><th>Output</th></tr></thead>
              <tbody id="downloads-body"></tbody>
            </table>
            <div class="empty hidden" id="downloads-empty">No downloads yet.</div>
          </div>
          <div>
            <table>
              <thead><tr><th>Run</th><th>Status</th><th>Stats</th></tr></thead>
              <tbody id="runs-mini-body"></tbody>
            </table>
            <div class="empty hidden" id="runs-empty">No runs recorded yet.</div>
          </div>
        </div>
      </section>

      <section class="panel tab hidden" id="tab-videos">
        <div class="panel-head">
          <div class="panel-title">Video Candidates</div>
          <div class="filters">
            <input id="video-query" type="search" placeholder="Search title, URL, source">
            <select id="video-status">
              <option value="">All downloads</option>
              <option value="pending">Pending</option>
              <option value="downloaded">Downloaded</option>
              <option value="failed">Failed</option>
            </select>
          </div>
        </div>
        <table>
          <thead><tr><th>Title</th><th>Media</th><th>Page</th><th>Source</th><th>Status</th><th>Output</th><th>Action</th></tr></thead>
          <tbody id="videos-body"></tbody>
        </table>
        <div class="empty hidden" id="videos-empty">No video candidates match the current filters.</div>
        <div class="detail hidden" id="video-detail"></div>
      </section>

      <section class="panel tab hidden" id="tab-pages">
        <div class="panel-head">
          <div class="panel-title">Page Diagnostics</div>
          <div class="filters">
            <input id="page-query" type="search" placeholder="Search URL, title, error, diagnostics">
          </div>
        </div>
        <table>
          <thead><tr><th>URL</th><th>Status</th><th>Fetcher</th><th>Videos</th><th>Challenge</th><th>Notes</th><th>Action</th></tr></thead>
          <tbody id="pages-body"></tbody>
        </table>
        <div class="empty hidden" id="pages-empty">No fetched pages match the current filters.</div>
        <div class="detail hidden" id="page-detail"></div>
      </section>

      <section class="panel tab hidden" id="tab-downloads">
        <div class="panel-head">
          <div class="panel-title">Download Tasks</div>
          <div class="filters">
            <input id="download-query" type="search" placeholder="Search media, page, output">
            <select id="download-status">
              <option value="">All states</option>
              <option value="queued">Queued</option>
              <option value="downloaded">Downloaded</option>
              <option value="failed">Failed</option>
              <option value="skipped">Skipped</option>
            </select>
          </div>
        </div>
        <table>
          <thead><tr><th>Media</th><th>Status</th><th>Resolver</th><th>Output</th><th>Error</th><th>Action</th></tr></thead>
          <tbody id="download-tasks-body"></tbody>
        </table>
        <div class="empty hidden" id="download-tasks-empty">No download tasks match the current filters.</div>
        <div class="detail hidden" id="download-detail"></div>
      </section>

      <section class="panel tab hidden" id="tab-queue">
        <div class="panel-head">
          <div>
            <div class="panel-title">Crawl Queue</div>
            <div class="subtle" id="queue-tab-summary"></div>
          </div>
          <div class="filters">
            <input class="small" id="stale-seconds" type="number" min="1" value="300" title="Stale seconds">
            <button class="button compact" id="recover-stale">Recover Stale</button>
            <select id="queue-status">
              <option value="">All queue states</option>
              <option value="pending">Pending</option>
              <option value="in_progress">In progress</option>
              <option value="fetched">Fetched</option>
              <option value="skipped">Skipped</option>
              <option value="failed">Failed</option>
            </select>
          </div>
        </div>
        <table>
          <thead><tr><th>URL</th><th>Status</th><th>Depth</th><th>Attempts</th><th>Error</th></tr></thead>
          <tbody id="queue-body"></tbody>
        </table>
        <div class="empty hidden" id="queue-empty">The persistent queue is empty.</div>
      </section>

      <section class="panel tab hidden" id="tab-workers">
        <div class="panel-head">
          <div class="panel-title">Workers</div>
          <div class="subtle">Heartbeat and current task state</div>
        </div>
        <table>
          <thead><tr><th>Worker</th><th>Kind</th><th>Status</th><th>Heartbeat</th><th>Current URL</th><th>Error</th></tr></thead>
          <tbody id="workers-body"></tbody>
        </table>
        <div class="empty hidden" id="workers-empty">No worker activity has been recorded yet.</div>
      </section>

      <section class="panel tab hidden" id="tab-policies">
        <div class="panel-head">
          <div class="panel-title">Policy Health</div>
          <div class="subtle" id="policy-summary"></div>
        </div>
        <div class="detail">
          <div class="policy-editor">
            <input id="policy-default-delay" type="number" min="0" step="0.1" placeholder="Default delay">
            <input id="policy-failure-threshold" type="number" min="0" step="1" placeholder="Failure threshold">
            <label class="checkline"><input id="policy-auto-throttle" type="checkbox"> Auto throttle</label>
            <input id="policy-auto-target-concurrency" type="number" min="0.1" step="0.1" placeholder="Target concurrency">
            <input id="policy-auto-min-delay" type="number" min="0" step="0.1" placeholder="Min delay">
            <input id="policy-auto-max-delay" type="number" min="0" step="0.1" placeholder="Max delay">
            <label class="checkline"><input id="policy-http-cache" type="checkbox"> HTTP cache</label>
            <input id="policy-http-cache-dir" type="text" placeholder="HTTP cache dir">
            <label class="checkline"><input id="policy-session-pool" type="checkbox"> Session pool</label>
            <input id="policy-session-pool-size" type="number" min="1" step="1" placeholder="Session pool size">
            <input id="policy-cookies-file" type="text" placeholder="Cookies file">
            <input id="policy-browser-profile" type="text" placeholder="Browser profile">
            <input id="policy-proxy-url" type="text" placeholder="Proxy URL">
            <input id="policy-domain" type="text" placeholder="Domain override">
            <input id="policy-domain-delay" type="number" min="0" step="0.1" placeholder="Domain delay">
            <input id="policy-domain-failure-threshold" type="number" min="0" step="1" placeholder="Domain threshold">
            <button class="button compact primary" id="save-policy">Save Policy</button>
            <span class="subtle" id="policy-status"></span>
          </div>
          <div class="detail-grid" id="policy-settings"></div>
        </div>
        <table>
          <thead><tr><th>Domain</th><th>Health</th><th>Delay</th><th>Latency</th><th>Success</th><th>Failures</th><th>Consecutive</th><th>Last Error</th></tr></thead>
          <tbody id="domains-body"></tbody>
        </table>
        <div class="empty hidden" id="domains-empty">No domain policy state has been recorded yet.</div>
        <div class="detail">
          <div class="panel-title">Sessions</div>
          <table>
            <thead><tr><th>Session</th><th>Kind</th><th>Status</th><th>Domain</th><th>Success</th><th>Failures</th></tr></thead>
            <tbody id="sessions-body"></tbody>
          </table>
          <div class="empty hidden" id="sessions-empty">No session health has been recorded yet.</div>
        </div>
      </section>

      <section class="panel tab hidden" id="tab-capabilities">
        <div class="panel-head">
          <div class="panel-title">Capability Guide</div>
          <div class="subtle" id="capability-summary"></div>
        </div>
        <div class="detail">
          <div class="panel-title">Capability Packages</div>
          <table>
            <thead><tr><th>Package</th><th>Status</th><th>Missing</th><th>Next Step</th><th>Verify</th></tr></thead>
            <tbody id="capability-packages-body"></tbody>
          </table>
          <div class="empty hidden" id="capability-packages-empty">No capability package data loaded.</div>
        </div>
        <div class="detail">
          <div class="panel-title">Capability Details</div>
        <table>
          <thead><tr><th>Capability</th><th>Status</th><th>Missing</th><th>Description</th></tr></thead>
          <tbody id="capabilities-body"></tbody>
        </table>
        <div class="empty hidden" id="capabilities-empty">No capability data loaded.</div>
        </div>
        <div class="detail">
          <div class="panel-title">Install Plan</div>
          <table>
            <thead><tr><th>Capability</th><th>Command</th><th>Missing</th><th>Action</th></tr></thead>
            <tbody id="install-plan-body"></tbody>
          </table>
          <div class="empty hidden" id="install-plan-empty">All optional capabilities are available or built in.</div>
        </div>
      </section>

      <section class="panel tab hidden" id="tab-plugins">
        <div class="panel-head">
          <div class="panel-title">Plugins</div>
          <div class="subtle" id="plugin-summary"></div>
        </div>
        <div class="detail">
          <div class="panel-title">Built-in Plugins</div>
          <table>
            <thead><tr><th>Plugin</th><th>Status</th><th>Capabilities</th><th>Fixtures</th><th>Action</th></tr></thead>
            <tbody id="builtin-plugins-body"></tbody>
          </table>
          <div class="empty hidden" id="builtin-plugins-empty">No built-in plugin data loaded.</div>
        </div>
        <div class="detail">
          <div class="panel-title">Configured Plugins</div>
          <table>
            <thead><tr><th>Plugin</th><th>Status</th><th>Path</th><th>Capabilities</th><th>Error</th><th>Action</th></tr></thead>
            <tbody id="configured-plugins-body"></tbody>
          </table>
          <div class="empty hidden" id="configured-plugins-empty">No local plugins are configured.</div>
        </div>
        <div class="detail hidden" id="plugin-result"></div>
      </section>

      <section class="panel tab hidden" id="tab-archive">
        <div class="panel-head">
          <div>
            <div class="panel-title">Archive</div>
            <div class="subtle" id="archive-summary"></div>
          </div>
          <div class="filters">
            <button class="button compact primary" id="refresh-archive">Refresh Archive</button>
            <button class="button compact" id="verify-archive">Verify</button>
          </div>
        </div>
        <div class="detail">
          <div class="detail-grid" id="archive-settings"></div>
          <div class="subtle" id="archive-action-status"></div>
        </div>
        <div class="detail">
          <div class="panel-title">Archive Files</div>
          <table>
            <thead><tr><th>File</th><th>Status</th><th>Bytes</th><th>Updated</th><th>Path</th></tr></thead>
            <tbody id="archive-files-body"></tbody>
          </table>
        </div>
        <div class="detail">
          <div class="panel-title">Verification</div>
          <div class="detail-grid" id="archive-verification"></div>
          <div class="detail-kv"><div class="subtle">Errors</div><span class="cmd" id="archive-errors"></span></div>
          <div class="detail-kv"><div class="subtle">Warnings</div><span class="cmd" id="archive-warnings"></span></div>
        </div>
      </section>

      <section class="panel tab hidden" id="tab-timeline">
        <div class="panel-head">
          <div class="panel-title">URL Timeline</div>
          <div class="filters">
            <select id="timeline-phase">
              <option value="">All phases</option>
              <option value="fetch">Fetch</option>
              <option value="extract">Extract</option>
              <option value="download">Download</option>
              <option value="archive">Archive</option>
              <option value="policy">Policy</option>
            </select>
            <button class="button compact" id="refresh-timeline">Refresh Timeline</button>
          </div>
        </div>
        <div class="detail">
          <div class="panel-title">Recent URLs</div>
          <table>
            <thead><tr><th>URL</th><th>Events</th><th>Last Event</th><th>Error</th></tr></thead>
            <tbody id="timeline-summary-body"></tbody>
          </table>
        </div>
        <table>
          <thead><tr><th>Time</th><th>Phase</th><th>Status</th><th>Type</th><th>Error</th><th>URL</th></tr></thead>
          <tbody id="timeline-body"></tbody>
        </table>
        <div class="empty hidden" id="timeline-empty">No timeline events recorded yet.</div>
      </section>

      <section class="panel tab hidden" id="tab-logs">
        <div class="panel-head">
          <div class="panel-title">Event Logs</div>
          <div class="filters">
            <label class="checkline"><input id="logs-live" type="checkbox"> Live</label>
            <button class="button compact" id="refresh-logs">Refresh Logs</button>
          </div>
        </div>
        <table>
          <thead><tr><th>Time</th><th>Type</th><th>Phase</th><th>Status</th><th>Error</th><th>Worker</th><th>Message</th><th>URL</th></tr></thead>
          <tbody id="logs-body"></tbody>
        </table>
        <div class="empty hidden" id="logs-empty">No events recorded yet.</div>
      </section>

      <section class="panel tab hidden" id="tab-config">
        <div class="panel-head">
          <div class="panel-title">Project Config</div>
          <div class="filters">
            <button class="button compact" id="reload-config">Reload</button>
            <button class="button compact primary" id="save-config">Save</button>
          </div>
        </div>
        <div class="detail">
          <textarea id="config-text" spellcheck="false"></textarea>
          <div class="subtle" id="config-status"></div>
        </div>
      </section>

      <section class="panel tab hidden" id="tab-runs">
        <div class="panel-head">
          <div class="panel-title">Run History</div>
          <div class="subtle">Latest 50 runs</div>
        </div>
        <table>
          <thead><tr><th>ID</th><th>Status</th><th>Started</th><th>Finished</th><th>Stats</th></tr></thead>
          <tbody id="runs-body"></tbody>
        </table>
      </section>
    </main>
  </div>

  <script>
    const state = {{ tab: "assistant", assistant: {{}}, assistantResult: null, cockpit: {{}}, videos: [], pages: [], queue: [], queueInfo: {{}}, workers: [], policies: {{}}, capabilities: {{}}, plugins: {{}}, pluginResult: null, archive: {{}}, timeline: [], timelineSummary: [], logs: [], runs: [], downloads: [], recentDownloads: [], stats: {{}}, selectedVideo: null, selectedPage: null, selectedDownload: null }};
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[ch]));
    const badge = (value) => `<span class="badge ${{esc(value || "pending")}}">${{esc(value || "pending")}}</span>`;
    const short = (value, fallback = "") => value ? esc(value) : `<span class="subtle">${{fallback}}</span>`;
    const fmtTime = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString() : "";

    async function getJSON(url) {{
      const response = await fetch(url);
      if (!response.ok) throw new Error(`${{response.status}} ${{response.statusText}}`);
      return response.json();
    }}

    async function postJSON(url, payload = {{}}) {{
      const response = await fetch(url, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload)
      }});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `${{response.status}} ${{response.statusText}}`);
      return data;
    }}

    async function loadAll() {{
      const [summary, assistant, cockpit, videos, pages, downloads, queue, workers, policies, capabilities, plugins, archive, timeline, logs, runs] = await Promise.all([
        getJSON("/api/summary"),
        getJSON("/api/assistant"),
        getJSON("/api/cockpit"),
        getJSON(videoApiUrl()),
        getJSON(pageApiUrl()),
        getJSON(downloadApiUrl()),
        getJSON(queueApiUrl()),
        getJSON("/api/workers?limit=100"),
        getJSON("/api/policies"),
        getJSON("/api/capabilities"),
        getJSON("/api/plugins"),
        getJSON("/api/archive"),
        getJSON(timelineApiUrl()),
        getJSON("/api/logs?limit=120"),
        getJSON("/api/runs?limit=50")
      ]);
      state.stats = summary.stats || {{}};
      state.assistant = assistant || {{}};
      state.cockpit = cockpit || {{}};
      state.recentDownloads = summary.downloads || [];
      state.downloads = downloads.downloads || [];
      state.videos = videos.videos || [];
      state.pages = pages.pages || [];
      state.queue = queue.queue || [];
      state.queueInfo = queue || {{}};
      state.workers = workers.workers || summary.workers || [];
      state.policies = policies || {{}};
      state.capabilities = capabilities || {{}};
      state.plugins = plugins || {{}};
      state.archive = archive || {{}};
      state.timeline = timeline.timeline || [];
      state.timelineSummary = timeline.summary || [];
      state.logs = logs.events || [];
      state.runs = runs.runs || summary.runs || [];
      render();
    }}

    function videoApiUrl() {{
      const params = new URLSearchParams({{ limit: "200" }});
      const q = $("video-query")?.value.trim();
      const status = $("video-status")?.value;
      if (q) params.set("q", q);
      if (status) params.set("status", status);
      return `/api/videos?${{params.toString()}}`;
    }}

    function pageApiUrl() {{
      const params = new URLSearchParams({{ limit: "200" }});
      const q = $("page-query")?.value.trim();
      if (q) params.set("q", q);
      return `/api/pages?${{params.toString()}}`;
    }}

    function downloadApiUrl() {{
      const params = new URLSearchParams({{ limit: "200" }});
      const q = $("download-query")?.value.trim();
      const status = $("download-status")?.value;
      if (q) params.set("q", q);
      if (status) params.set("status", status);
      return `/api/downloads?${{params.toString()}}`;
    }}

    function queueApiUrl() {{
      const params = new URLSearchParams({{ limit: "200" }});
      const status = $("queue-status")?.value;
      if (status) params.set("status", status);
      return `/api/queue?${{params.toString()}}`;
    }}

    function timelineApiUrl() {{
      const params = new URLSearchParams({{ limit: "200", summary_limit: "50" }});
      const phase = $("timeline-phase")?.value;
      if (phase) params.set("phase", phase);
      return `/api/timeline?${{params.toString()}}`;
    }}

    function render() {{
      renderAssistant();
      renderCockpit();
      renderStats();
      renderDownloads();
      renderDownloadTasks();
      renderVideos();
      renderPages();
      renderQueue();
      renderWorkers();
      renderPolicies();
      renderCapabilities();
      renderPlugins();
      renderArchive();
      renderTimeline();
      renderLogs();
      renderRuns();
      $("last-updated").textContent = `Updated ${{new Date().toLocaleTimeString()}}`;
      const queue = state.stats.queue || {{}};
      const mode = state.stats.paused ? "paused" : "running-ready";
      $("queue-summary").textContent = `${{mode}} / pending ${{queue.pending || 0}} / fetched ${{queue.fetched || 0}} / failed ${{queue.failed || 0}}`;
    }}

    function renderAssistant() {{
      const config = state.assistant || {{}};
      const fetch = config.fetch || {{}};
      const network = config.network || {{}};
      const media = config.media || {{}};
      const urlInput = $("assistant-url");
      if (urlInput && !urlInput.dataset.touched && !urlInput.value) {{
        urlInput.value = config.default_url || "";
      }}
      setAssistantChecked("assistant-use-browser", fetch.default === "browser");
      setAssistantChecked("assistant-http-cache", network.http_cache);
      setAssistantChecked("assistant-downloads", media.download);
      setAssistantValue("assistant-browser-profile", fetch.browser_profile || "");
      setAssistantValue("assistant-cookies-file", network.cookies_file || "");
      setAssistantValue("assistant-max-depth", fetch.max_depth ?? "");
      $("assistant-summary").textContent = `${{fetch.default || "http"}} / cache ${{network.http_cache ? "on" : "off"}} / downloads ${{media.download ? "on" : "off"}}`;
      renderAssistantResult();
    }}

    function setAssistantChecked(id, value) {{
      const node = $(id);
      if (node && !node.dataset.touched) node.checked = Boolean(value);
    }}

    function setAssistantValue(id, value) {{
      const node = $(id);
      if (node && !node.dataset.touched) node.value = value ?? "";
    }}

    function renderAssistantResult() {{
      const result = state.assistantResult;
      const panel = $("assistant-result");
      const recPanel = $("assistant-recommendations");
      if (!result) {{
        panel.innerHTML = `
          <div class="detail-kv"><div class="subtle">Status</div>${{badge("pending")}}</div>
          <div class="detail-kv"><div class="subtle">Videos</div>0</div>
          <div class="detail-kv"><div class="subtle">Dynamic Score</div>0</div>
          <div class="detail-kv"><div class="subtle">Recommendation</div><span class="subtle">No inspect result yet.</span></div>
        `;
        recPanel.innerHTML = "";
        return;
      }}
      const summary = result.summary || {{}};
      const diag = result.diagnostic_summary || {{}};
      panel.innerHTML = `
        <div class="detail-kv"><div class="subtle">URL</div><a href="${{esc(result.url)}}">${{short(result.url, "-")}}</a></div>
        <div class="detail-kv"><div class="subtle">Fetcher</div>${{badge(result.fetcher || "http")}}</div>
        <div class="detail-kv"><div class="subtle">Status</div>${{badge(result.error ? "failed" : result.status)}}</div>
        <div class="detail-kv"><div class="subtle">Challenge</div>${{badge(summary.challenge_detected ? "yes" : "no")}}</div>
        <div class="detail-kv"><div class="subtle">Videos</div>${{esc(summary.videos || 0)}}</div>
        <div class="detail-kv"><div class="subtle">Media Hints</div>${{esc(summary.media_hints || 0)}}</div>
        <div class="detail-kv"><div class="subtle">Dynamic Score</div>${{esc(summary.dynamic_score || 0)}}</div>
        <div class="detail-kv"><div class="subtle">Script Markers</div>${{short(diag.script_markers, "-")}}</div>
        <div class="detail-kv"><div class="subtle">Network Kinds</div>${{short(diag.network_hint_kinds, "-")}}</div>
        <div class="detail-kv"><div class="subtle">Error</div>${{short(result.error, "-")}}</div>
      `;
      recPanel.innerHTML = assistantRecommendationsTable(result.recommendations || []);
      document.querySelectorAll("[data-copy-command]").forEach((button) => {{
        button.addEventListener("click", () => copyCommand(button.dataset.copyCommand));
      }});
    }}

    function assistantRecommendationsTable(items) {{
      if (!items.length) return "";
      return `
        <table>
          <thead><tr><th>Next Action</th><th>Reason</th><th>Action</th><th>Package</th></tr></thead>
          <tbody>${{items.map((item) => `
            <tr>
              <td class="truncate"><span class="mobile-label">Next</span>${{short(item.label)}}</td>
              <td class="truncate"><span class="mobile-label">Reason</span>${{short(item.reason, "-")}}</td>
              <td class="truncate"><span class="mobile-label">Action</span>${{short(item.action, "-")}}</td>
              <td class="truncate"><span class="mobile-label">Package</span>${{assistantPackageCell(item.package)}}</td>
            </tr>
          `).join("")}}</tbody>
        </table>
      `;
    }}

    function assistantPackageCell(pkg) {{
      if (!pkg) return `<span class="subtle">-</span>`;
      const command = (pkg.install_commands || [])[0];
      const copy = command ? ` <button class="button compact" data-copy-command="${{esc(command)}}">Copy</button>` : "";
      return `${{short(pkg.label)}} ${{badge(pkg.status)}}${{copy}}`;
    }}

    function renderCockpit() {{
      const payload = state.cockpit || {{}};
      const stats = payload.stats || state.stats || {{}};
      const queue = payload.queue || {{}};
      const queueStats = queue.stats || stats.queue || {{}};
      const latest = payload.latest_run || {{}};
      const failures = payload.failures || {{}};
      const latestLabel = latest.id ? "#" + latest.id + " " + (latest.status || "") : "none";
      $("cockpit-summary").textContent = `${{payload.mode || "idle"}} / latest run ${{latestLabel}} / failure groups ${{failures.active_categories || 0}}`;
      $("cockpit-current").innerHTML = [
        ["Mode", payload.mode || "idle"],
        ["Latest run", latestLabel],
        ["Current URL", (payload.current_urls || []).join(", ") || "-"],
        ["Queue", `pending ${{queueStats.pending || 0}} / in progress ${{queueStats.in_progress || 0}} / failed ${{queueStats.failed || 0}}`],
        ["Queue backend", `${{queue.backend || "sqlite"}} / stale ${{queue.stale_after_seconds || 300}}s`],
        ["Latest run stats", latest.stats_json || "{{}}"],
      ].map(([label, value]) => `
        <div class="detail-kv"><div class="subtle">${{esc(label)}}</div>${{label === "Mode" ? badge(value) : short(value, "-")}}</div>
      `).join("");
      const statItems = [
        ["pages", "Pages", stats.pages || 0],
        ["videos", "Videos", stats.videos || 0],
        ["pending", "Queue Pending", queueStats.pending || 0],
        ["workers", "Active Workers", stats.active_workers || 0],
        ["failures", "Failures", failures.total || 0],
        ["downloads", "Downloads", stats.downloads || 0],
      ];
      $("cockpit-stats").innerHTML = statItems.map(([key, label, value]) => `
        <div class="stat ${{key}}">
          <div class="stat-label">${{esc(label)}}</div>
          <div class="stat-value">${{esc(value)}}</div>
        </div>
      `).join("");
      const categories = failures.categories || [];
      $("cockpit-failures-body").innerHTML = categories.map((item) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Category</span>${{short(item.label)}} ${{badge(item.id)}}</td>
          <td><span class="mobile-label">Count</span>${{esc(item.count || 0)}}</td>
          <td class="truncate"><span class="mobile-label">Advice</span>${{short(item.advice, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Action</span>${{short(item.action, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Samples</span>${{cockpitFailureSamples(item)}}</td>
        </tr>
      `).join("");
      $("cockpit-failures-empty").classList.toggle("hidden", categories.length > 0);
      const workers = payload.workers || [];
      $("cockpit-workers-body").innerHTML = workers.map((worker) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Worker</span>${{short(worker.worker_id)}}</td>
          <td><span class="mobile-label">Kind</span>${{short(worker.kind, "-")}}</td>
          <td><span class="mobile-label">Status</span>${{badge(worker.status)}}</td>
          <td class="truncate"><span class="mobile-label">Current URL</span>${{short(worker.current_url, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Error</span>${{short(worker.error, "-")}}</td>
        </tr>
      `).join("");
      $("cockpit-workers-empty").classList.toggle("hidden", workers.length > 0);
      const videos = payload.recent_videos || [];
      $("cockpit-videos-body").innerHTML = videos.map((item) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Title</span>${{short(item.title, "Untitled")}}</td>
          <td class="truncate"><span class="mobile-label">Media</span><a href="${{esc(item.media_url)}}">${{short(item.media_url)}}</a></td>
          <td class="truncate"><span class="mobile-label">Page</span><a href="${{esc(item.page_url)}}">${{short(item.page_url)}}</a></td>
          <td><span class="mobile-label">Source</span>${{short(item.source, "-")}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.download_status)}}</td>
        </tr>
      `).join("");
      $("cockpit-videos-empty").classList.toggle("hidden", videos.length > 0);
      const downloads = payload.recent_downloads || [];
      $("cockpit-downloads-body").innerHTML = downloads.map((item) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Media</span>${{short(item.media_url)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.status)}}</td>
          <td><span class="mobile-label">Resolver</span>${{short(item.resolver, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Output</span>${{short(item.output_path, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Error</span>${{short(item.error, "-")}}</td>
        </tr>
      `).join("");
      $("cockpit-downloads-empty").classList.toggle("hidden", downloads.length > 0);
      syncCockpitActionState(payload.actions || []);
    }}

    function cockpitFailureSamples(item) {{
      const samples = item.samples || [];
      if (!samples.length) return `<span class="subtle">-</span>`;
      return samples.map((sample) => {{
        const where = sample.target || sample.url || "-";
        const detail = [sample.status, sample.error, sample.dynamic_score ? `dynamic ${{sample.dynamic_score}}` : ""]
          .filter(Boolean)
          .join(" / ");
        return `<div class="truncate">${{short(where)}}${{detail ? ` <span class="subtle">${{esc(detail)}}</span>` : ""}}</div>`;
      }}).join("");
    }}

    function syncCockpitActionState(actions) {{
      const byId = Object.fromEntries(actions.map((item) => [item.id, item]));
      setButtonDisabled("cockpit-retry-downloads", !(byId.retry_downloads || {{}}).enabled);
      setButtonDisabled("cockpit-recover-stale", !(byId.recover_queue || {{}}).enabled);
      setButtonDisabled("cockpit-start-download", !(byId.start_download || {{}}).enabled);
      setButtonDisabled("cockpit-refresh-archive", !(byId.refresh_archive || {{}}).enabled);
      setButtonDisabled("cockpit-verify-archive", !(byId.verify_archive || {{}}).enabled);
    }}

    function setButtonDisabled(id, disabled) {{
      const node = $(id);
      if (node) node.disabled = Boolean(disabled);
    }}

    function renderStats() {{
      const items = [
        ["pages", "Pages", state.stats.pages || 0],
        ["videos", "Videos", state.stats.videos || 0],
        ["pending", "Download Queue", state.stats.queued_downloads || 0],
        ["downloads", "Downloads", state.stats.downloads || 0],
        ["failures", "Failures", state.stats.failures || 0],
        ["pending", "Queue Pending", (state.stats.queue || {{}}).pending || 0],
        ["workers", "Active Workers", state.stats.active_workers || 0],
      ];
      $("stats").innerHTML = items.map(([key, label, value]) => `
        <div class="stat ${{key}}">
          <div class="stat-label">${{esc(label)}}</div>
          <div class="stat-value">${{esc(value)}}</div>
        </div>
      `).join("");
    }}

    function renderDownloads() {{
      const rows = state.recentDownloads.map((item) => `
        <tr>
          <td class="truncate">${{short(item.media_url)}}</td>
          <td>${{badge(item.status)}}</td>
          <td>${{short(item.resolver)}}</td>
          <td class="truncate">${{short(item.output_path, "-")}}</td>
        </tr>
      `).join("");
      $("downloads-body").innerHTML = rows;
      $("downloads-empty").classList.toggle("hidden", state.recentDownloads.length > 0);
    }}

    function renderDownloadTasks() {{
      $("download-tasks-body").innerHTML = state.downloads.map((item, index) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Media</span>${{short(item.media_url)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.status)}}</td>
          <td><span class="mobile-label">Resolver</span>${{short(item.resolver, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Output</span>${{short(item.output_path, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Error</span>${{short(item.error, "-")}}</td>
          <td><span class="mobile-label">Action</span>${{downloadActions(item, index)}}</td>
        </tr>
      `).join("");
      $("download-tasks-empty").classList.toggle("hidden", state.downloads.length > 0);
      document.querySelectorAll("[data-download-index]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const item = state.downloads[Number(button.dataset.downloadIndex)];
          const action = button.dataset.downloadAction;
          state.selectedDownload = item;
          if (action === "details") renderDownloadDetail();
          if (action === "retry") runDownloadAction("Retry download", "/api/control/retry-download", item);
          if (action === "skip") runDownloadAction("Skip download", "/api/control/skip-download", item);
        }});
      }});
      renderDownloadDetail();
    }}

    function downloadActions(item, index) {{
      const buttons = [`<button class="button compact" data-download-index="${{index}}" data-download-action="details">Details</button>`];
      if (item.status === "failed" || item.status === "skipped") {{
        buttons.push(`<button class="button compact" data-download-index="${{index}}" data-download-action="retry">Retry</button>`);
      }}
      if (item.status === "queued" || item.status === "failed") {{
        buttons.push(`<button class="button compact danger" data-download-index="${{index}}" data-download-action="skip">Skip</button>`);
      }}
      return buttons.join(" ");
    }}

    function renderDownloadDetail() {{
      const panel = $("download-detail");
      const item = state.selectedDownload;
      if (!item) {{
        panel.classList.add("hidden");
        panel.innerHTML = "";
        return;
      }}
      const metadata = item.metadata_json || "{{}}";
      const src = item.output_path ? `/api/file?path=${{encodeURIComponent(item.output_path)}}` : item.media_url;
      panel.classList.remove("hidden");
      panel.innerHTML = `
        <div class="panel-title">Download Detail</div>
        <div class="detail-grid">
          <div class="detail-kv"><div class="subtle">Status</div>${{badge(item.status)}}</div>
          <div class="detail-kv"><div class="subtle">Resolver</div>${{short(item.resolver, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Output</div>${{short(item.output_path, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Updated</div>${{esc(fmtTime(item.updated_at))}}</div>
          <div class="detail-kv"><div class="subtle">Error</div>${{short(item.error, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Metadata</div><span class="cmd">${{short(metadata, "{{}}")}}</span></div>
        </div>
        <video controls src="${{esc(src)}}"></video>
        <div class="detail-grid">
          <div class="detail-kv"><div class="subtle">Media URL</div><a href="${{esc(item.media_url)}}">${{short(item.media_url)}}</a></div>
          <div class="detail-kv"><div class="subtle">Page URL</div><a href="${{esc(item.page_url)}}">${{short(item.page_url)}}</a></div>
        </div>
      `;
    }}

    function renderVideos() {{
      $("videos-body").innerHTML = state.videos.map((item, index) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Title</span>${{short(item.title, "Untitled")}}</td>
          <td class="truncate"><span class="mobile-label">Media</span><a href="${{esc(item.media_url)}}">${{short(item.media_url)}}</a></td>
          <td class="truncate"><span class="mobile-label">Page</span><a href="${{esc(item.page_url)}}">${{short(item.page_url)}}</a></td>
          <td><span class="mobile-label">Source</span>${{short(item.source)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.download_status)}}</td>
          <td class="truncate"><span class="mobile-label">Output</span>${{short(item.output_path, "-")}}</td>
          <td><button class="button compact" data-video-index="${{index}}">Details</button></td>
        </tr>
      `).join("");
      $("videos-empty").classList.toggle("hidden", state.videos.length > 0);
      document.querySelectorAll("[data-video-index]").forEach((button) => {{
        button.addEventListener("click", () => {{
          state.selectedVideo = state.videos[Number(button.dataset.videoIndex)];
          renderVideoDetail();
        }});
      }});
      renderVideoDetail();
    }}

    function renderVideoDetail() {{
      const panel = $("video-detail");
      const item = state.selectedVideo;
      if (!item) {{
        panel.classList.add("hidden");
        panel.innerHTML = "";
        return;
      }}
      const src = item.output_path ? `/api/file?path=${{encodeURIComponent(item.output_path)}}` : item.media_url;
      panel.classList.remove("hidden");
      panel.innerHTML = `
        <div class="panel-title">Video Detail</div>
        <div class="detail-grid">
          <div class="detail-kv"><div class="subtle">Title</div>${{short(item.title, "Untitled")}}</div>
          <div class="detail-kv"><div class="subtle">Status</div>${{badge(item.download_status)}}</div>
          <div class="detail-kv"><div class="subtle">Source</div>${{short(item.source)}}</div>
          <div class="detail-kv"><div class="subtle">Output</div>${{short(item.output_path, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Metadata</div><span class="cmd">${{short(item.metadata_json, "{{}}")}}</span></div>
        </div>
        <video controls src="${{esc(src)}}"></video>
        <div class="detail-grid">
          <div class="detail-kv"><div class="subtle">Media URL</div><a href="${{esc(item.media_url)}}">${{short(item.media_url)}}</a></div>
          <div class="detail-kv"><div class="subtle">Page URL</div><a href="${{esc(item.page_url)}}">${{short(item.page_url)}}</a></div>
        </div>
      `;
    }}

    function parseDiagnostics(value) {{
      try {{
        return JSON.parse(value || "{{}}");
      }} catch (_error) {{
        return {{ raw: value || "{{}}" }};
      }}
    }}

    function diagnosticNotes(item) {{
      const diagnostics = parseDiagnostics(item.diagnostics_json);
      const notes = diagnostics.notes || [];
      return Array.isArray(notes) && notes.length ? notes.join(", ") : "-";
    }}

    function renderPages() {{
      $("pages-body").innerHTML = state.pages.map((item, index) => `
        <tr>
          <td class="truncate"><span class="mobile-label">URL</span>${{short(item.url)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.status_code)}}</td>
          <td><span class="mobile-label">Fetcher</span>${{short(item.fetcher, "-")}}</td>
          <td><span class="mobile-label">Videos</span>${{esc(item.video_count || 0)}}</td>
          <td><span class="mobile-label">Challenge</span>${{badge(item.challenge_detected ? "yes" : "no")}}</td>
          <td class="truncate"><span class="mobile-label">Notes</span>${{short(diagnosticNotes(item), "-")}}</td>
          <td><button class="button compact" data-page-index="${{index}}">Details</button></td>
        </tr>
      `).join("");
      $("pages-empty").classList.toggle("hidden", state.pages.length > 0);
      document.querySelectorAll("[data-page-index]").forEach((button) => {{
        button.addEventListener("click", () => {{
          state.selectedPage = state.pages[Number(button.dataset.pageIndex)];
          renderPageDetail();
        }});
      }});
      renderPageDetail();
    }}

    function renderPageDetail() {{
      const panel = $("page-detail");
      const item = state.selectedPage;
      if (!item) {{
        panel.classList.add("hidden");
        panel.innerHTML = "";
        return;
      }}
      const diagnostics = parseDiagnostics(item.diagnostics_json);
      const signals = diagnostics.dynamic_signals || {{}};
      const recommendations = diagnostics.recommendations || [];
      const markers = Object.entries(signals.dynamic_script_markers || {{}})
        .map(([key, value]) => `${{key}}=${{value}}`)
        .join(", ") || "-";
      const networkKinds = Object.entries(signals.network_media_hints_by_kind || {{}})
        .map(([key, value]) => `${{key}}=${{value}}`)
        .join(", ") || "-";
      panel.classList.remove("hidden");
      panel.innerHTML = `
        <div class="panel-title">Extraction Diagnostics</div>
        <div class="detail-grid">
          <div class="detail-kv"><div class="subtle">URL</div><a href="${{esc(item.url)}}">${{short(item.url)}}</a></div>
          <div class="detail-kv"><div class="subtle">Title</div>${{short(item.title, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Status</div>${{badge(item.status_code)}}</div>
          <div class="detail-kv"><div class="subtle">Fetcher</div>${{short(item.fetcher, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Videos</div>${{esc(item.video_count || 0)}}</div>
          <div class="detail-kv"><div class="subtle">Challenge</div>${{badge(item.challenge_detected ? "yes" : "no")}}</div>
          <div class="detail-kv"><div class="subtle">Error</div>${{short(item.error, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Fetched</div>${{esc(fmtTime(item.fetched_at))}}</div>
        </div>
        <div class="detail-grid">
          <div class="detail-kv"><div class="subtle">Dynamic Score</div>${{esc(signals.dynamic_score || 0)}}</div>
          <div class="detail-kv"><div class="subtle">Browser Media Hints</div>${{esc(signals.browser_media_hints || 0)}}</div>
          <div class="detail-kv"><div class="subtle">Embedded Players</div>${{esc(signals.embedded_player_iframes || 0)}}</div>
          <div class="detail-kv"><div class="subtle">Lazy Media Attrs</div>${{esc(signals.lazy_media_attrs || 0)}}</div>
          <div class="detail-kv"><div class="subtle">Network Hint Kinds</div>${{short(networkKinds, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Script Markers</div>${{short(markers, "-")}}</div>
          <div class="detail-kv"><div class="subtle">Player Scripts</div>${{short((signals.player_script_urls || []).join(", "), "-")}}</div>
          <div class="detail-kv"><div class="subtle">Recommendations</div>${{short(recommendations.join(", "), "-")}}</div>
        </div>
        <div class="detail-kv"><div class="subtle">Diagnostics</div><span class="cmd">${{esc(JSON.stringify(diagnostics, null, 2))}}</span></div>
      `;
    }}

    function renderQueue() {{
      const info = state.queueInfo || {{}};
      const stats = info.stats || state.stats.queue || {{}};
      $("queue-tab-summary").textContent = `${{info.backend || "sqlite"}} / stale after ${{info.stale_after_seconds || 300}}s / pending ${{stats.pending || 0}} / in progress ${{stats.in_progress || 0}}`;
      const staleInput = $("stale-seconds");
      if (staleInput && document.activeElement !== staleInput && !staleInput.dataset.touched) {{
        staleInput.value = info.stale_after_seconds || 300;
      }}
      $("queue-body").innerHTML = state.queue.map((item) => `
        <tr>
          <td class="truncate"><span class="mobile-label">URL</span>${{short(item.url)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.status)}}</td>
          <td><span class="mobile-label">Depth</span>${{esc(item.depth)}}</td>
          <td><span class="mobile-label">Attempts</span>${{esc(item.attempts)}}</td>
          <td class="truncate"><span class="mobile-label">Error</span>${{short(item.error, "-")}}</td>
        </tr>
      `).join("");
      $("queue-empty").classList.toggle("hidden", state.queue.length > 0);
    }}

    function renderWorkers() {{
      $("workers-body").innerHTML = state.workers.map((worker) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Worker</span>${{short(worker.worker_id)}}</td>
          <td><span class="mobile-label">Kind</span>${{short(worker.kind)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(worker.status)}}</td>
          <td><span class="mobile-label">Heartbeat</span>${{esc(fmtTime(worker.heartbeat_at))}}</td>
          <td class="truncate"><span class="mobile-label">Current URL</span>${{short(worker.current_url, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Error</span>${{short(worker.error, "-")}}</td>
        </tr>
      `).join("");
      $("workers-empty").classList.toggle("hidden", state.workers.length > 0);
    }}

    function renderPolicies() {{
      const policies = state.policies || {{}};
      const fetch = policies.fetch || {{}};
      const network = policies.network || {{}};
      const domains = policies.domains || [];
      const sessions = policies.sessions || [];
      renderPolicyEditor(fetch, network);
      $("policy-summary").textContent = `failure threshold ${{fetch.failure_threshold ?? 0}} / default delay ${{fetch.default_delay ?? 0}}s`;
      $("policy-settings").innerHTML = [
        ["HTTP cache", network.http_cache ? "on" : "off"],
        ["Session pool", network.session_pool ? `on (${{network.session_pool_size || 1}})` : "off"],
        ["Auto throttle", fetch.auto_throttle ? `on (target ${{fetch.auto_throttle_target_concurrency || 1}})` : "off"],
        ["Proxy", network.proxy_configured ? `${{network.proxy_count}} configured` : "not configured"],
        ["Cookies", network.cookies_file ? network.cookies_file_path : "not configured"],
        ["Browser profile", network.browser_profile ? network.browser_profile_path : "not configured"],
        ["Per-domain delay", Object.keys(fetch.per_domain_delay || {{}}).length || 0],
        ["Per-domain failure threshold", Object.keys(fetch.per_domain_failure_thresholds || {{}}).length || 0],
      ].map(([label, value]) => `
        <div class="detail-kv"><div class="subtle">${{esc(label)}}</div>${{short(value, "-")}}</div>
      `).join("");
      $("domains-body").innerHTML = domains.map((item) => `
        <tr>
          <td class="truncate">${{short(item.domain)}}</td>
          <td>${{badge(item.health)}}</td>
          <td>${{item.dynamic_delay_seconds == null ? "-" : `${{Number(item.dynamic_delay_seconds).toFixed(2)}}s`}}</td>
          <td>${{item.avg_latency_ms == null ? "-" : `${{Math.round(Number(item.avg_latency_ms))}}ms`}}</td>
          <td>${{esc(item.successes || 0)}}</td>
          <td>${{esc(item.failures || 0)}}</td>
          <td>${{esc(item.consecutive_failures || 0)}}</td>
          <td class="truncate">${{short(item.last_error, "-")}}</td>
        </tr>
      `).join("");
      $("domains-empty").classList.toggle("hidden", domains.length > 0);
      $("sessions-body").innerHTML = sessions.map((item) => `
        <tr>
          <td class="truncate">${{short(item.session_id)}}</td>
          <td>${{short(item.kind)}}</td>
          <td>${{badge(item.status)}}</td>
          <td class="truncate">${{short(item.domain, "-")}}</td>
          <td>${{esc(item.successes || 0)}}</td>
          <td>${{esc(item.failures || 0)}}</td>
        </tr>
      `).join("");
      $("sessions-empty").classList.toggle("hidden", sessions.length > 0);
    }}

    function renderPolicyEditor(fetch, network) {{
      const setValue = (id, value) => {{
        const node = $(id);
        if (node) node.value = value ?? "";
      }};
      const setChecked = (id, value) => {{
        const node = $(id);
        if (node) node.checked = Boolean(value);
      }};
      setValue("policy-default-delay", fetch.default_delay ?? "");
      setValue("policy-failure-threshold", fetch.failure_threshold ?? "");
      setChecked("policy-auto-throttle", fetch.auto_throttle);
      setValue("policy-auto-target-concurrency", fetch.auto_throttle_target_concurrency ?? 1);
      setValue("policy-auto-min-delay", fetch.auto_throttle_min_delay ?? 0);
      setValue("policy-auto-max-delay", fetch.auto_throttle_max_delay ?? 30);
      setChecked("policy-http-cache", network.http_cache);
      setValue("policy-http-cache-dir", network.http_cache_dir || "");
      setChecked("policy-session-pool", network.session_pool);
      setValue("policy-session-pool-size", network.session_pool_size || 1);
      setValue("policy-cookies-file", network.cookies_file_path || "");
      setValue("policy-browser-profile", network.browser_profile_path || "");
      setValue("policy-proxy-url", network.proxy_configured && network.proxy_count === 1 ? "configured" : "");
      const delayOverrides = fetch.per_domain_delay || {{}};
      const thresholdOverrides = fetch.per_domain_failure_thresholds || {{}};
      const domainNode = $("policy-domain");
      const domains = Array.from(new Set([...Object.keys(delayOverrides), ...Object.keys(thresholdOverrides)]));
      const domain = domainNode.value.trim() || domains[0] || "";
      if (!domainNode.value && domain) domainNode.value = domain;
      setValue("policy-domain-delay", domain ? delayOverrides[domain] ?? "" : "");
      setValue("policy-domain-failure-threshold", domain ? thresholdOverrides[domain] ?? "" : "");
    }}

    function renderCapabilities() {{
      const payload = state.capabilities || {{}};
      const summary = payload.summary || {{}};
      const packages = payload.packages || [];
      const capabilities = payload.capabilities || [];
      const plan = payload.install_plan || [];
      $("capability-summary").textContent = `${{summary.available || 0}} available / ${{summary.partial || 0}} partial / ${{summary.missing || 0}} missing`;
      $("capability-packages-body").innerHTML = packages.map((item, index) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Package</span>${{short(item.label)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.status)}}</td>
          <td class="truncate"><span class="mobile-label">Missing</span>${{short((item.missing || []).join(", "), "-")}}</td>
          <td class="truncate"><span class="mobile-label">Next</span>${{short((item.next_steps || [])[0], "-")}}</td>
          <td class="truncate"><span class="mobile-label">Verify</span>${{short((item.verify_commands || [])[0], "-")}}</td>
        </tr>
      `).join("");
      $("capability-packages-empty").classList.toggle("hidden", packages.length > 0);
      $("capabilities-body").innerHTML = capabilities.map((item) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Capability</span>${{short(item.label)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.status)}}</td>
          <td class="truncate"><span class="mobile-label">Missing</span>${{short((item.missing || []).join(", "), "-")}}</td>
          <td class="truncate"><span class="mobile-label">Description</span>${{short(item.description, "-")}}</td>
        </tr>
      `).join("");
      $("capabilities-empty").classList.toggle("hidden", capabilities.length > 0);
      $("install-plan-body").innerHTML = plan.map((item) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Capability</span>${{short(item.label)}}</td>
          <td class="cmd"><span class="mobile-label">Command</span>${{short(item.command)}}</td>
          <td class="truncate"><span class="mobile-label">Missing</span>${{short((item.missing || []).join(", "), "-")}}</td>
          <td><button class="button compact" data-copy-command="${{esc(item.command)}}">Copy</button></td>
        </tr>
      `).join("");
      $("install-plan-empty").classList.toggle("hidden", plan.length > 0);
      document.querySelectorAll("[data-copy-command]").forEach((button) => {{
        button.addEventListener("click", () => copyCommand(button.dataset.copyCommand));
      }});
    }}

    function renderPlugins() {{
      const payload = state.plugins || {{}};
      const builtins = payload.builtins || [];
      const configured = payload.configured || [];
      $("plugin-summary").textContent = `${{builtins.filter((item) => item.enabled).length}} built-ins enabled / ${{configured.length}} local configured`;
      $("builtin-plugins-body").innerHTML = builtins.map((item, index) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Plugin</span>${{short(item.name)}} <span class="subtle">${{short(item.version, "")}}</span></td>
          <td><span class="mobile-label">Status</span>${{badge(item.status)}}</td>
          <td class="truncate"><span class="mobile-label">Capabilities</span>${{short((item.capabilities || []).join(", "), "-")}}</td>
          <td><span class="mobile-label">Fixtures</span>${{esc(item.fixtures || 0)}}</td>
          <td><button class="button compact" data-plugin-kind="builtin" data-plugin-index="${{index}}">Test</button></td>
        </tr>
      `).join("");
      $("builtin-plugins-empty").classList.toggle("hidden", builtins.length > 0);
      $("configured-plugins-body").innerHTML = configured.map((item, index) => `
        <tr>
          <td class="truncate"><span class="mobile-label">Plugin</span>${{short(item.name)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.status)}}</td>
          <td class="truncate"><span class="mobile-label">Path</span>${{short(item.path, "-")}}</td>
          <td class="truncate"><span class="mobile-label">Capabilities</span>${{short((item.capabilities || []).join(", "), "-")}}</td>
          <td class="truncate"><span class="mobile-label">Error</span>${{short(item.error, "-")}}</td>
          <td>${{item.status === "loaded" ? `<button class="button compact" data-plugin-kind="configured" data-plugin-index="${{index}}">Test</button>` : ""}}</td>
        </tr>
      `).join("");
      $("configured-plugins-empty").classList.toggle("hidden", configured.length > 0);
      document.querySelectorAll("[data-plugin-kind]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const list = button.dataset.pluginKind === "builtin" ? builtins : configured;
          const item = list[Number(button.dataset.pluginIndex)];
          runPluginTest(item);
        }});
      }});
      renderPluginResult();
    }}

    async function runPluginTest(item) {{
      const panel = $("plugin-result");
      panel.classList.remove("hidden");
      panel.innerHTML = `<div class="panel-title">Plugin Test Result</div><div class="subtle">Running ${{esc(item.name || item.path)}}...</div>`;
      try {{
        state.pluginResult = await postJSON("/api/plugins/test", item.test_payload || {{}});
      }} catch (error) {{
        state.pluginResult = {{ ok: false, error: error.message, plugin: {{ name: item.name || item.path }} }};
      }}
      renderPluginResult();
    }}

    function renderPluginResult() {{
      const panel = $("plugin-result");
      if (!state.pluginResult) {{
        panel.classList.add("hidden");
        panel.innerHTML = "";
        return;
      }}
      const result = state.pluginResult;
      panel.classList.remove("hidden");
      panel.innerHTML = `
        <div class="panel-title">Plugin Test Result</div>
        <div class="detail-grid">
          <div class="detail-kv"><div class="subtle">Plugin</div>${{short((result.plugin || {{}}).name || "-")}}</div>
          <div class="detail-kv"><div class="subtle">Status</div>${{badge(result.ok ? "passed" : "failed")}}</div>
          <div class="detail-kv"><div class="subtle">Fixtures</div>${{esc((result.fixtures || []).length || 0)}}</div>
          <div class="detail-kv"><div class="subtle">Error</div>${{short(result.error, "-")}}</div>
        </div>
        <div class="detail-kv"><div class="subtle">Raw Result</div><span class="cmd">${{esc(JSON.stringify(result, null, 2))}}</span></div>
      `;
    }}

    function renderArchive() {{
      const payload = state.archive || {{}};
      const config = payload.config || {{}};
      const manifest = payload.manifest || {{}};
      const counts = manifest.counts || {{}};
      const verification = payload.verification || {{}};
      const files = payload.files || [];
      $("archive-summary").textContent = `${{verification.ok ? "verified" : "needs attention"}} / pages ${{counts.pages ?? 0}} / videos ${{counts.videos ?? 0}} / assets ${{counts.assets ?? 0}}`;
      $("archive-settings").innerHTML = [
        ["Root", config.root || "-"],
        ["Archive enabled", config.enabled ? "yes" : "no"],
        ["HTML snapshots", config.html_snapshots ? "on" : "off"],
        ["JSONL sidecars", config.jsonl_sidecar ? "on" : "off"],
        ["Manifest", config.manifest ? "on" : "off"],
        ["WARC", config.warc ? "on" : "off"],
        ["WARC records", verification.warc_records ?? 0],
        ["WARC SHA-256", verification.warc_sha256 || "-"],
      ].map(([label, value]) => `
        <div class="detail-kv"><div class="subtle">${{esc(label)}}</div>${{short(value, "-")}}</div>
      `).join("");
      $("archive-files-body").innerHTML = files.map((item) => `
        <tr>
          <td class="truncate"><span class="mobile-label">File</span>${{short(item.name)}}</td>
          <td><span class="mobile-label">Status</span>${{badge(item.exists ? "available" : "missing")}}</td>
          <td><span class="mobile-label">Bytes</span>${{esc(item.bytes || 0)}}</td>
          <td><span class="mobile-label">Updated</span>${{esc(fmtTime(item.updated_at))}}</td>
          <td class="truncate"><span class="mobile-label">Path</span>${{short(item.path, "-")}}</td>
        </tr>
      `).join("");
      $("archive-verification").innerHTML = [
        ["Status", verification.ok ? "ok" : "failed"],
        ["Manifest", verification.manifest ? "present" : "missing"],
        ["Pages sidecar", verification.pages ?? 0],
        ["Videos sidecar", verification.videos ?? 0],
        ["Assets sidecar", verification.assets ?? 0],
        ["WARC", verification.warc ? "present" : "not present"],
      ].map(([label, value]) => `
        <div class="detail-kv"><div class="subtle">${{esc(label)}}</div>${{label === "Status" ? badge(value) : short(value, "-")}}</div>
      `).join("");
      $("archive-errors").textContent = JSON.stringify(verification.errors || [], null, 2);
      $("archive-warnings").textContent = JSON.stringify(verification.warnings || [], null, 2);
    }}

    async function runArchiveAction(label, url) {{
      $("archive-action-status").textContent = `${{label}}...`;
      try {{
        state.archive = await postJSON(url, {{}});
        $("archive-action-status").textContent = `${{label}}: ${{state.archive.status || "ok"}}`;
        renderArchive();
        await loadAll();
      }} catch (error) {{
        $("archive-action-status").textContent = `${{label}} failed: ${{error.message}}`;
      }}
    }}

    function renderTimeline() {{
      $("timeline-summary-body").innerHTML = state.timelineSummary.map((item) => `
        <tr>
          <td class="truncate">${{short(item.url, "-")}}</td>
          <td>${{esc(item.events || 0)}}</td>
          <td>${{esc(fmtTime(item.last_event_at))}}</td>
          <td>${{badge(item.has_error ? "error" : "ok")}}</td>
        </tr>
      `).join("");
      $("timeline-body").innerHTML = state.timeline.map((item) => `
        <tr>
          <td>${{esc(fmtTime(item.created_at))}}</td>
          <td>${{badge(item.phase)}}</td>
          <td>${{badge(item.status)}}</td>
          <td>${{short(item.event_type)}}</td>
          <td class="truncate">${{short(item.error_class, "-")}}</td>
          <td class="truncate">${{short(item.url, "-")}}</td>
        </tr>
      `).join("");
      $("timeline-empty").classList.toggle("hidden", state.timeline.length > 0);
    }}

    function renderLogs() {{
      $("logs-body").innerHTML = state.logs.map((event) => `
        <tr>
          <td>${{esc(fmtTime(event.created_at))}}</td>
          <td>${{badge(event.event_type)}}</td>
          <td>${{badge(logData(event).phase || "event")}}</td>
          <td>${{badge(logData(event).status || "observed")}}</td>
          <td class="truncate">${{short(logData(event).error_class, "-")}}</td>
          <td class="truncate">${{short(event.worker_id, "-")}}</td>
          <td class="truncate">${{short(event.message, "-")}}</td>
          <td class="truncate">${{short(event.url, "-")}}</td>
        </tr>
      `).join("");
      $("logs-empty").classList.toggle("hidden", state.logs.length > 0);
    }}

    function logData(event) {{
      try {{
        return JSON.parse(event.data_json || "{{}}");
      }} catch (_error) {{
        return {{}};
      }}
    }}

    function renderRuns() {{
      const rows = state.runs.map((run) => `
        <tr>
          <td>${{esc(run.id)}}</td>
          <td>${{badge(run.status)}}</td>
          <td>${{esc(fmtTime(run.started_at))}}</td>
          <td>${{esc(fmtTime(run.finished_at))}}</td>
          <td class="truncate">${{short(run.stats_json, "{{}}")}}</td>
        </tr>
      `).join("");
      $("runs-body").innerHTML = rows;
      $("runs-mini-body").innerHTML = state.runs.slice(0, 5).map((run) => `
        <tr><td>${{esc(run.id)}}</td><td>${{badge(run.status)}}</td><td class="truncate">${{short(run.stats_json, "{{}}")}}</td></tr>
      `).join("");
      $("runs-empty").classList.toggle("hidden", state.runs.length > 0);
    }}

    async function loadConfigEditor() {{
      const payload = await getJSON("/api/config");
      $("config-text").value = payload.text || "";
      $("config-status").textContent = `Loaded ${{new Date().toLocaleTimeString()}}`;
    }}

    async function saveConfigEditor() {{
      $("config-status").textContent = "Saving...";
      await postJSON("/api/config", {{ text: $("config-text").value }});
      $("config-status").textContent = `Saved ${{new Date().toLocaleTimeString()}}`;
      await loadAll();
    }}

    function readPolicyPayload() {{
      const payload = {{
        default_delay: $("policy-default-delay").value,
        failure_threshold: $("policy-failure-threshold").value,
        auto_throttle: $("policy-auto-throttle").checked,
        auto_throttle_target_concurrency: $("policy-auto-target-concurrency").value,
        auto_throttle_min_delay: $("policy-auto-min-delay").value,
        auto_throttle_max_delay: $("policy-auto-max-delay").value,
        http_cache: $("policy-http-cache").checked,
        http_cache_dir: $("policy-http-cache-dir").value,
        session_pool: $("policy-session-pool").checked,
        session_pool_size: $("policy-session-pool-size").value,
        cookies_file: $("policy-cookies-file").value,
        browser_profile: $("policy-browser-profile").value,
        domain: $("policy-domain").value,
        domain_delay: $("policy-domain-delay").value,
        domain_failure_threshold: $("policy-domain-failure-threshold").value
      }};
      if ($("policy-proxy-url").value !== "configured") {{
        payload.proxy_url = $("policy-proxy-url").value;
      }}
      return payload;
    }}

    async function savePolicyEditor() {{
      $("policy-status").textContent = "Saving...";
      const result = await postJSON("/api/policies", readPolicyPayload());
      state.policies = result || state.policies;
      $("policy-status").textContent = `Saved ${{new Date().toLocaleTimeString()}}`;
      await loadAll();
    }}

    async function runAssistantInspect() {{
      $("assistant-status").textContent = "Inspecting...";
      try {{
        state.assistantResult = await postJSON("/api/assistant/inspect", {{
          url: $("assistant-url").value,
          browser: $("assistant-browser").checked
        }});
        applyAssistantRecommendedConfig(state.assistantResult.recommended_config || {{}});
        $("assistant-status").textContent = `Inspect: ${{state.assistantResult.summary?.videos || 0}} videos`;
        renderAssistantResult();
      }} catch (error) {{
        $("assistant-status").textContent = `Inspect failed: ${{error.message}}`;
      }}
    }}

    function applyAssistantRecommendedConfig(config) {{
      if ("use_browser" in config) $("assistant-use-browser").checked = Boolean(config.use_browser);
      if ("enable_http_cache" in config) $("assistant-http-cache").checked = Boolean(config.enable_http_cache);
      if ("enable_downloads" in config) $("assistant-downloads").checked = Boolean(config.enable_downloads);
      if (config.browser_profile && !$("assistant-browser-profile").dataset.touched) $("assistant-browser-profile").value = config.browser_profile;
      if (config.cookies_file && !$("assistant-cookies-file").dataset.touched) $("assistant-cookies-file").value = config.cookies_file;
      if (config.max_depth != null && !$("assistant-max-depth").dataset.touched) $("assistant-max-depth").value = config.max_depth;
    }}

    function readAssistantPayload() {{
      return {{
        url: $("assistant-url").value,
        add_seed: $("assistant-add-seed").checked,
        use_browser: $("assistant-use-browser").checked,
        enable_http_cache: $("assistant-http-cache").checked,
        enable_downloads: $("assistant-downloads").checked,
        browser_profile: $("assistant-browser-profile").value,
        cookies_file: $("assistant-cookies-file").value,
        max_depth: $("assistant-max-depth").value
      }};
    }}

    async function saveAssistantConfig() {{
      $("assistant-status").textContent = "Saving...";
      try {{
        state.assistant = await postJSON("/api/assistant/apply", readAssistantPayload());
        $("assistant-status").textContent = `Saved ${{new Date().toLocaleTimeString()}}`;
        await loadAll();
      }} catch (error) {{
        $("assistant-status").textContent = `Save failed: ${{error.message}}`;
      }}
    }}

    async function startAssistantCrawl() {{
      $("assistant-status").textContent = "Saving and starting...";
      try {{
        state.assistant = await postJSON("/api/assistant/apply", readAssistantPayload());
        const result = await postJSON("/api/control/start-crawl", {{
          max_pages: Number($("assistant-max-pages").value || 20),
          resume: true
        }});
        $("assistant-status").textContent = `Started ${{result.worker || "crawl worker"}}`;
        $("cockpit-live").checked = true;
        $("cockpit-status").textContent = `Started ${{result.worker || "crawl worker"}}`;
        activateTab("cockpit");
        await loadAll();
      }} catch (error) {{
        $("assistant-status").textContent = `Start failed: ${{error.message}}`;
      }}
    }}

    async function runAction(label, url, payload = {{}}) {{
      $("action-status").textContent = `${{label}}...`;
      try {{
        const result = await postJSON(url, payload);
        const suffix = result.recovered == null ? (result.status || "ok") : `${{result.status || "ok"}} (${{result.recovered}})`;
        $("action-status").textContent = `${{label}}: ${{suffix}}`;
        await loadAll();
      }} catch (error) {{
        $("action-status").textContent = `${{label}} failed: ${{error.message}}`;
      }}
    }}

    async function copyCommand(command) {{
      try {{
        await navigator.clipboard.writeText(command);
        $("action-status").textContent = "Copied install command";
      }} catch (_error) {{
        $("action-status").textContent = command;
      }}
    }}

    async function runDownloadAction(label, url, item) {{
      await runAction(label, url, {{ page_url: item.page_url, media_url: item.media_url }});
      const current = state.downloads.find((download) => download.page_url === item.page_url && download.media_url === item.media_url);
      state.selectedDownload = current || null;
      renderDownloadDetail();
    }}

    async function runCockpitAction(label, url, payload = {{}}) {{
      $("cockpit-status").textContent = `${{label}}...`;
      try {{
        const result = await postJSON(url, payload);
        const suffix = result.recovered == null ? (result.retried == null ? (result.status || "ok") : `${{result.status || "ok"}} (${{result.retried}})`) : `${{result.status || "ok"}} (${{result.recovered}})`;
        $("cockpit-status").textContent = `${{label}}: ${{suffix}}`;
        $("action-status").textContent = `${{label}}: ${{suffix}}`;
        await loadAll();
      }} catch (error) {{
        $("cockpit-status").textContent = `${{label}} failed: ${{error.message}}`;
      }}
    }}

    function activateTab(tab) {{
      state.tab = tab;
      document.querySelectorAll(".nav button").forEach((node) => {{
        node.classList.toggle("active", node.dataset.tab === tab);
      }});
      document.querySelectorAll(".tab").forEach((node) => node.classList.add("hidden"));
      const panel = $(`tab-${{state.tab}}`);
      if (panel) panel.classList.remove("hidden");
      syncLogsLive();
      syncCockpitLive();
      if (state.tab === "config") loadConfigEditor().catch((error) => {{
        $("config-status").textContent = `Load failed: ${{error.message}}`;
      }});
    }}

    let logsLiveTimer = null;
    function syncLogsLive() {{
      const enabled = $("logs-live").checked;
      if (enabled && !logsLiveTimer) {{
        logsLiveTimer = setInterval(() => {{
          if (state.tab === "logs") loadAll().catch((error) => {{
            $("last-updated").textContent = `Live logs failed: ${{error.message}}`;
          }});
        }}, 5000);
      }}
      if (!enabled && logsLiveTimer) {{
        clearInterval(logsLiveTimer);
        logsLiveTimer = null;
      }}
    }}

    let cockpitLiveTimer = null;
    function syncCockpitLive() {{
      const node = $("cockpit-live");
      const enabled = Boolean(node && node.checked && state.tab === "cockpit");
      if (enabled && !cockpitLiveTimer) {{
        cockpitLiveTimer = setInterval(() => {{
          if (state.tab === "cockpit") loadAll().catch((error) => {{
            $("last-updated").textContent = `Live cockpit failed: ${{error.message}}`;
          }});
        }}, 5000);
      }}
      if (!enabled && cockpitLiveTimer) {{
        clearInterval(cockpitLiveTimer);
        cockpitLiveTimer = null;
      }}
    }}

    document.querySelectorAll(".nav button").forEach((button) => {{
      button.addEventListener("click", () => activateTab(button.dataset.tab));
    }});
    $("refresh").addEventListener("click", loadAll);
    $("cockpit-refresh").addEventListener("click", loadAll);
    $("cockpit-live").addEventListener("change", syncCockpitLive);
    $("cockpit-retry-downloads").addEventListener("click", () => runCockpitAction("Retry failed downloads", "/api/control/retry-downloads", {{
      limit: $("download-limit").value || null
    }}));
    $("cockpit-recover-stale").addEventListener("click", () => runCockpitAction("Recover stale queue", "/api/control/recover-queue", {{
      stale_after_seconds: $("stale-seconds").value || ((state.cockpit.queue || {{}}).stale_after_seconds) || null
    }}));
    $("cockpit-start-download").addEventListener("click", () => runCockpitAction("Start download", "/api/control/start-download", {{
      limit: $("download-limit").value || null
    }}));
    $("cockpit-refresh-archive").addEventListener("click", () => runCockpitAction("Refresh archive", "/api/archive/write"));
    $("cockpit-verify-archive").addEventListener("click", () => runCockpitAction("Verify archive", "/api/archive/verify"));
    $("assistant-inspect").addEventListener("click", runAssistantInspect);
    $("assistant-apply").addEventListener("click", saveAssistantConfig);
    $("assistant-start-crawl").addEventListener("click", startAssistantCrawl);
    ["assistant-url", "assistant-use-browser", "assistant-http-cache", "assistant-downloads", "assistant-browser-profile", "assistant-cookies-file", "assistant-max-depth"].forEach((id) => {{
      const node = $(id);
      if (node) node.addEventListener("input", () => {{ node.dataset.touched = "1"; }});
      if (node) node.addEventListener("change", () => {{ node.dataset.touched = "1"; }});
    }});
    $("start-crawl").addEventListener("click", () => runAction("Start crawl", "/api/control/start-crawl", {{
      max_pages: Number($("max-pages").value || 100),
      resume: true
    }}));
    $("resume-crawl").addEventListener("click", () => runAction("Resume crawl", "/api/control/resume-crawl", {{
      max_pages: Number($("max-pages").value || 100)
    }}));
    $("pause-workers").addEventListener("click", () => runAction("Pause", "/api/control/pause"));
    $("start-download").addEventListener("click", () => runAction("Download", "/api/control/start-download", {{
      limit: $("download-limit").value || null
    }}));
    $("retry-downloads").addEventListener("click", () => runAction("Retry downloads", "/api/control/retry-downloads", {{
      limit: $("download-limit").value || null
    }}));
    $("clear-pending").addEventListener("click", () => runAction("Clear pending", "/api/control/clear-queue", {{
      status: "pending"
    }}));
    $("recover-stale").addEventListener("click", () => runAction("Recover stale", "/api/control/recover-queue", {{
      stale_after_seconds: $("stale-seconds").value || null
    }}));
    $("stale-seconds").addEventListener("input", () => {{
      $("stale-seconds").dataset.touched = "1";
    }});
    $("refresh-logs").addEventListener("click", loadAll);
    $("logs-live").addEventListener("change", syncLogsLive);
    $("refresh-timeline").addEventListener("click", loadAll);
    $("refresh-archive").addEventListener("click", () => runArchiveAction("Refresh archive", "/api/archive/write"));
    $("verify-archive").addEventListener("click", () => runArchiveAction("Verify archive", "/api/archive/verify"));
    $("timeline-phase").addEventListener("change", loadAll);
    $("reload-config").addEventListener("click", () => loadConfigEditor().catch((error) => {{
      $("config-status").textContent = `Load failed: ${{error.message}}`;
    }}));
    $("save-config").addEventListener("click", () => saveConfigEditor().catch((error) => {{
      $("config-status").textContent = `Save failed: ${{error.message}}`;
    }}));
    $("save-policy").addEventListener("click", () => savePolicyEditor().catch((error) => {{
      $("policy-status").textContent = `Save failed: ${{error.message}}`;
    }}));
    $("video-query").addEventListener("input", () => loadAll());
    $("video-status").addEventListener("change", () => loadAll());
    $("page-query").addEventListener("input", () => loadAll());
    $("download-query").addEventListener("input", () => loadAll());
    $("download-status").addEventListener("change", () => loadAll());
    $("queue-status").addEventListener("change", () => loadAll());
    $("policy-domain").addEventListener("change", () => renderPolicies());
    loadAll().catch((error) => {{
      $("last-updated").textContent = `Load failed: ${{error.message}}`;
    }});
  </script>
</body>
</html>""".format(project=escaped_project)
