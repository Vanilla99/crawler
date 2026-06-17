import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from .config import ProjectConfig, ScopeConfig, load_config
from .doctor import collect_diagnostics
from .engine import CrawlEngine
from .integrations import scaffold_colly, scaffold_crawlee_python, scaffold_nutch, scaffold_scrapy
from .models import CrawlRequest
from .queue_backends import SQLiteQueueBackend
from .storage import SQLiteStore


MATRIX_LAYERS = ("unit", "smoke", "optional", "integration", "long-run")
DEFAULT_SMOKE_URL = "https://www.w3schools.com/html/html5_video.asp"


def run_test_matrix(
    layer="unit",
    config_path=None,
    output_dir=None,
    smoke_url=None,
    max_pages=1,
    long_run_size=1000,
):
    """Run one or more verification layers and return a JSON-serializable report."""
    requested = (layer or "unit").lower()
    if requested == "all":
        layers = list(MATRIX_LAYERS)
    elif requested in MATRIX_LAYERS:
        layers = [requested]
    else:
        raise ValueError("unsupported test matrix layer: %s" % layer)

    started = time.time()
    sections = []
    for name in layers:
        section_started = time.time()
        try:
            section = _run_layer(
                name,
                config_path=config_path,
                output_dir=output_dir,
                smoke_url=smoke_url,
                max_pages=max_pages,
                long_run_size=long_run_size,
            )
        except Exception as exc:
            section = {"name": name, "ok": False, "error": "%s: %s" % (exc.__class__.__name__, exc)}
        section["duration_seconds"] = round(time.time() - section_started, 3)
        sections.append(section)

    return {
        "ok": all(section.get("ok") for section in sections),
        "layer": requested,
        "duration_seconds": round(time.time() - started, 3),
        "sections": sections,
    }


def format_test_matrix_summary(report):
    status = "ok" if report.get("ok") else "failed"
    lines = [
        "test-matrix layer=%s status=%s duration=%ss"
        % (report.get("layer"), status, report.get("duration_seconds"))
    ]
    for section in report.get("sections", []):
        section_status = "ok" if section.get("ok") else "failed"
        detail = section.get("summary") or section.get("error") or ""
        lines.append("  %-12s %-6s %s" % (section.get("name"), section_status, detail))
    return "\n".join(lines)


def _run_layer(name, **kwargs):
    if name == "unit":
        return run_unit_layer()
    if name == "smoke":
        return run_smoke_layer(kwargs.get("config_path"), kwargs.get("smoke_url"), kwargs.get("max_pages"))
    if name == "optional":
        return run_optional_layer()
    if name == "integration":
        return run_integration_layer(kwargs.get("config_path"), kwargs.get("output_dir"))
    if name == "long-run":
        return run_long_run_layer(kwargs.get("long_run_size"))
    raise ValueError("unsupported test matrix layer: %s" % name)


def run_unit_layer():
    root = _repo_root()
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
    return {
        "name": "unit",
        "ok": completed.returncode == 0,
        "summary": "unittest discover returncode=%s" % completed.returncode,
        "command": command,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def run_smoke_layer(config_path=None, smoke_url=None, max_pages=1):
    config = _load_or_default_config(config_path)
    url = smoke_url or (config.seeds[0] if config.seeds else DEFAULT_SMOKE_URL)
    config.seeds = [url]
    config.scope = ScopeConfig(
        allowed_domains=[_domain(url)],
        max_depth=0,
        respect_robots=False,
    )
    config.fetch.delay_per_domain_seconds = 0
    config.fetch.retries = 0
    result, extracted = CrawlEngine(config).inspect(url)
    status_ok = bool(result.status_code and 200 <= int(result.status_code) < 400)
    videos = len(extracted.videos) if extracted else 0
    ok = bool(not result.error and status_ok and videos > 0)
    diagnostics = extracted.diagnostics if extracted else {}
    notes = diagnostics.get("notes") or []
    summary = "url=%s status=%s videos=%s" % (url, result.status_code, videos)
    if notes and not ok:
        summary += " notes=%s" % ",".join(notes[:3])
    return {
        "name": "smoke",
        "ok": ok,
        "summary": summary,
        "url": url,
        "status_code": result.status_code,
        "fetcher": result.fetcher,
        "videos": videos,
        "links": len(extracted.links) if extracted else 0,
        "media_hints": len(result.media_hints or []),
        "diagnostics": diagnostics,
        "diagnostic_notes": notes,
        "candidate_sources": diagnostics.get("candidate_sources") or {},
        "challenge_detected": bool(result.challenge_detected or (extracted and extracted.challenge_detected)),
        "error": result.error,
        "max_pages": max_pages,
    }


def run_optional_layer():
    rows = []
    for name, detail, ok in collect_diagnostics():
        rows.append(
            {
                "name": name,
                "status": "ok" if ok else "optional-missing",
                "detail": detail,
                "required": name in ("python", "sqlite3"),
            }
        )
    required_ok = all(row["status"] == "ok" for row in rows if row["required"])
    optional_available = [row["name"] for row in rows if not row["required"] and row["status"] == "ok"]
    optional_missing = [row["name"] for row in rows if not row["required"] and row["status"] != "ok"]
    return {
        "name": "optional",
        "ok": required_ok,
        "summary": "%s optional available, %s optional missing"
        % (len(optional_available), len(optional_missing)),
        "diagnostics": rows,
        "optional_available": optional_available,
        "optional_missing": optional_missing,
    }


def run_integration_layer(config_path=None, output_dir=None):
    config = _load_or_default_config(config_path)
    if output_dir:
        root = os.path.abspath(output_dir)
        os.makedirs(root, exist_ok=True)
        return _run_integration_scaffolds(config, root, ephemeral=False)
    with tempfile.TemporaryDirectory(prefix="vcrawl-integration-") as tmp:
        return _run_integration_scaffolds(config, tmp, ephemeral=True)


def run_long_run_layer(long_run_size=1000):
    size = int(long_run_size or 0)
    if size <= 0:
        raise ValueError("long_run_size must be positive")
    with tempfile.TemporaryDirectory(prefix="vcrawl-long-run-") as tmp:
        store = SQLiteStore(os.path.join(tmp, "state.sqlite"))
        try:
            queue = SQLiteQueueBackend(store)
            for index in range(size):
                queue.enqueue(
                    CrawlRequest(
                        url="https://example.com/video/%06d" % index,
                        depth=index % 3,
                        priority=index % 10,
                    )
                )
            claimed = queue.next_batch(size)
            stale_at = time.time() - 3600
            with store._lock:
                store.conn.execute(
                    "UPDATE crawl_queue SET updated_at=? WHERE status='in_progress'",
                    (stale_at,),
                )
                store.conn.commit()
            recovered = queue.recover_stale(300)
            stats_after_recovery = store.queue_stats()
            rechecked = queue.next_batch(min(10, size))
            ok = (
                len(claimed) == size
                and recovered == size
                and stats_after_recovery.get("pending") == size
                and len(rechecked) == min(10, size)
            )
            return {
                "name": "long-run",
                "ok": ok,
                "summary": "queued=%s claimed=%s recovered=%s"
                % (size, len(claimed), recovered),
                "queued": size,
                "claimed": len(claimed),
                "recovered": recovered,
                "stats_after_recovery": stats_after_recovery,
                "rechecked": len(rechecked),
            }
        finally:
            store.close()


def _run_integration_scaffolds(config, root, ephemeral):
    targets = {
        "scrapy": scaffold_scrapy(config, os.path.join(root, "scrapy")),
        "crawlee-python": scaffold_crawlee_python(config, os.path.join(root, "crawlee-python")),
        "colly": scaffold_colly(config, os.path.join(root, "colly")),
        "nutch": scaffold_nutch(config, os.path.join(root, "nutch")),
    }
    checks = [
        _check_scrapy_scaffold(targets["scrapy"], config.project),
        _check_crawlee_scaffold(targets["crawlee-python"]),
        _check_colly_scaffold(targets["colly"]),
        _check_nutch_scaffold(targets["nutch"]),
    ]
    ok = all(check["ok"] for check in checks)
    return {
        "name": "integration",
        "ok": ok,
        "summary": "%s/%s scaffolds verified" % (sum(1 for check in checks if check["ok"]), len(checks)),
        "artifact_dir": root,
        "ephemeral": ephemeral,
        "targets": targets,
        "checks": checks,
    }


def _check_scrapy_scaffold(root, project):
    package = _safe_name(project or "vcrawl_project")
    expected = [
        os.path.join(root, "scrapy.cfg"),
        os.path.join(root, package, "items.py"),
        os.path.join(root, package, "settings.py"),
        os.path.join(root, package, "middlewares.py"),
        os.path.join(root, package, "pipelines.py"),
        os.path.join(root, package, "spiders", "video_spider.py"),
    ]
    return _compile_python_scaffold("scrapy", expected)


def _check_crawlee_scaffold(root):
    expected = [os.path.join(root, "main.py"), os.path.join(root, "requirements.txt")]
    return _compile_python_scaffold("crawlee-python", expected)


def _check_colly_scaffold(root):
    expected = [os.path.join(root, "go.mod"), os.path.join(root, "main.go"), os.path.join(root, "README.md")]
    missing = [path for path in expected if not os.path.exists(path)]
    content_ok = False
    main_go = os.path.join(root, "main.go")
    if os.path.exists(main_go):
        with open(main_go, "r", encoding="utf-8") as fh:
            content = fh.read()
        content_ok = "colly.NewCollector" in content and "VideoCandidate" in content
    return {
        "target": "colly",
        "ok": not missing and content_ok,
        "missing": missing,
        "compiled": False,
        "external_tool_available": shutil.which("go") is not None,
    }


def _check_nutch_scaffold(root):
    expected = [
        os.path.join(root, "urls", "seed.txt"),
        os.path.join(root, "conf", "regex-urlfilter.txt"),
        os.path.join(root, "conf", "nutch-site.xml"),
        os.path.join(root, "README.md"),
    ]
    missing = [path for path in expected if not os.path.exists(path)]
    xml_ok = False
    xml_error = None
    site_xml = os.path.join(root, "conf", "nutch-site.xml")
    if os.path.exists(site_xml):
        try:
            ET.parse(site_xml)
            xml_ok = True
        except ET.ParseError as exc:
            xml_error = str(exc)
    return {
        "target": "nutch",
        "ok": not missing and xml_ok,
        "missing": missing,
        "xml_ok": xml_ok,
        "xml_error": xml_error,
        "external_tool_available": shutil.which("nutch") is not None,
    }


def _compile_python_scaffold(target, expected):
    missing = [path for path in expected if not os.path.exists(path)]
    compiled = []
    errors = []
    for path in expected:
        if not path.endswith(".py") or not os.path.exists(path):
            continue
        try:
            py_compile.compile(path, doraise=True)
            compiled.append(path)
        except py_compile.PyCompileError as exc:
            errors.append("%s: %s" % (path, exc.msg))
    return {
        "target": target,
        "ok": not missing and not errors,
        "missing": missing,
        "compiled": compiled,
        "errors": errors,
    }


def _load_or_default_config(config_path):
    if config_path:
        return load_config(config_path)
    return ProjectConfig(
        project="test-matrix",
        seeds=[DEFAULT_SMOKE_URL],
        scope=ScopeConfig(allowed_domains=["www.w3schools.com"], max_depth=0, respect_robots=False),
    )


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tail(text, lines=40):
    values = (text or "").splitlines()
    return "\n".join(values[-lines:])


def _domain(url):
    if "://" not in url:
        return ""
    return url.split("://", 1)[1].split("/", 1)[0]


def _safe_name(value):
    return "".join(ch if ch.isalnum() else "_" for ch in value).strip("_").lower() or "vcrawl_project"
