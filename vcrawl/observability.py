import json
import os
import re
import time
from urllib.parse import urlparse


URL_RE = re.compile(r"https?://[^\s]+", re.I)


EVENT_PHASES = {
    "run_started": "run",
    "run_finished": "run",
    "run_failed": "run",
    "run_paused": "run",
    "fetch": "fetch",
    "retry": "fetch",
    "fetch_failed": "fetch",
    "page_extracted": "extract",
    "challenge": "verification",
    "domain_blocked": "policy",
    "policy_autothrottle": "policy",
    "archived_page": "archive",
    "download_queued": "download",
    "download": "download",
    "downloaded": "download",
    "download_failed": "download",
    "queue_recovered": "queue",
    "control": "control",
}


EVENT_STATUSES = {
    "run_started": "started",
    "run_finished": "completed",
    "run_failed": "failed",
    "run_paused": "paused",
    "fetch": "started",
    "retry": "retrying",
    "fetch_failed": "failed",
    "page_extracted": "completed",
    "challenge": "blocked",
    "domain_blocked": "blocked",
    "policy_autothrottle": "adjusted",
    "archived_page": "completed",
    "download_queued": "queued",
    "download": "started",
    "downloaded": "completed",
    "download_failed": "failed",
    "queue_recovered": "recovered",
    "control": "completed",
}


class JsonLogWriter:
    def __init__(self, path):
        self.path = path

    def write(self, record):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


class OpenTelemetrySink:
    def __init__(self, service_name="vcrawl"):
        self.service_name = service_name
        try:
            from opentelemetry import trace
        except ImportError:
            self.trace = None
            self.tracer = None
        else:
            self.trace = trace
            self.tracer = trace.get_tracer(service_name)

    @property
    def available(self):
        return self.tracer is not None

    def __call__(self, event):
        if not self.tracer:
            return
        with self.tracer.start_as_current_span("vcrawl.%s" % event.type) as span:
            span.set_attribute("vcrawl.event_type", event.type)
            span.set_attribute("vcrawl.phase", phase_for_event(event.type))
            span.set_attribute("vcrawl.status", status_for_event(event.type))
            if event.url:
                span.set_attribute("url.full", event.url)
                span.set_attribute("server.address", urlparse(event.url).netloc)
            if event.message:
                span.set_attribute("vcrawl.message", event.message[:500])
            for key, value in event.data.items():
                if isinstance(value, (str, int, float, bool)):
                    span.set_attribute("vcrawl.%s" % key, value)


def install_event_sinks(event_bus, config):
    if not getattr(config.observability, "enabled", True):
        return []
    sinks = []
    if getattr(config.observability, "opentelemetry", False):
        sink = OpenTelemetrySink(service_name=config.observability.service_name)
        event_bus.subscribe(sink)
        sinks.append(sink)
    return sinks


def make_observation_record(event_type, message="", url=None, worker_id=None, kind=None, data=None):
    data = dict(data or {})
    error_class = data.get("error_class") or classify_error(
        event_type=event_type,
        message=message,
        status_code=data.get("status_code"),
        challenge_detected=data.get("challenge_detected"),
    )
    return {
        "event_type": event_type,
        "phase": data.get("phase") or phase_for_event(event_type),
        "status": data.get("status") or status_for_event(event_type),
        "severity": severity_for_event(event_type, error_class),
        "message": message or "",
        "url": url or extract_url(message),
        "worker_id": worker_id,
        "kind": kind,
        "error_class": error_class,
        "data": data,
        "created_at": time.time(),
    }


def phase_for_event(event_type):
    return EVENT_PHASES.get(event_type, "event")


def status_for_event(event_type):
    return EVENT_STATUSES.get(event_type, "observed")


def severity_for_event(event_type, error_class=None):
    if error_class or event_type.endswith("_failed") or event_type in ("run_failed", "domain_blocked"):
        return "error"
    if event_type in ("retry", "challenge", "run_paused"):
        return "warning"
    return "info"


def classify_error(event_type=None, message="", status_code=None, challenge_detected=False):
    if challenge_detected or event_type == "challenge":
        return "verification_required"
    if event_type == "domain_blocked":
        return "domain_failure_threshold"
    if event_type == "retry":
        return "retryable_fetch"
    if status_code:
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = 0
        if code in (401, 403):
            return "forbidden_or_auth"
        if code == 404:
            return "not_found"
        if code == 408:
            return "timeout"
        if code == 429:
            return "rate_limited"
        if code >= 500:
            return "server_error"
    text = str(message or "").lower()
    if not text:
        return None
    if "captcha" in text or "verify" in text or "challenge" in text:
        return "verification_required"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "ssl" in text or "certificate" in text:
        return "tls_error"
    if "name or service" in text or "nodename" in text or "dns" in text:
        return "dns_error"
    if "connection refused" in text or "connection reset" in text or "connection aborted" in text:
        return "connection_error"
    if "robots" in text:
        return "robots_blocked"
    if "yt-dlp" in text:
        return "media_resolver_error"
    if "ffmpeg" in text or "ffprobe" in text:
        return "media_tool_error"
    if event_type and event_type.endswith("_failed"):
        return "failed"
    return None


def extract_url(value):
    match = URL_RE.search(value or "")
    return match.group(0) if match else None
