import argparse
from dataclasses import asdict
import json
import os

from .archive import ArchiveManager, archive_options_from_config, verify_archive
from .capabilities import build_capability_report, format_capability_report
from .config import ProjectConfig, ScopeConfig, config_to_dict, load_config, save_config
from .discovery import expand_discovery_seeds
from .doctor import format_diagnostics
from .downloads import DownloadManager, DownloadOptions
from .engine import CrawlEngine
from .exporters import export_csv, export_jsonl
from .fetchers import BrowserFetcher, HttpFetcher
from .integrations import (
    integration_report,
    scaffold_colly,
    scaffold_crawlee_python,
    scaffold_nutch,
    scaffold_scrapy,
)
from .plugin_templates import write_plugin_template
from .plugins import list_builtin_plugins, run_plugin_tests
from .resolvers import BuiltinMediaResolver, YtDlpResolver
from .storage import SQLiteStore
from .test_matrix import MATRIX_LAYERS, format_test_matrix_summary, run_test_matrix
from .workers import CrawlWorker, DownloadWorker


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vcrawl", description="Video-oriented web crawler MVP.")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Create a starter project config.")
    init_p.add_argument("project", nargs="?", default="video-project")
    init_p.add_argument("--seed", action="append", default=[])

    doctor_p = sub.add_parser("doctor", help="Check local optional tooling.")
    doctor_p.add_argument("--capabilities", action="store_true", help="Show crawler capability levels.")
    doctor_p.add_argument("--fix-plan", action="store_true", help="Show install commands for missing capabilities.")
    doctor_p.add_argument("--json", action="store_true", help="Print capability report as JSON.")
    doctor_p.add_argument("--config", default=None, help="Include project config posture hints.")
    doctor_p.set_defaults(func=cmd_doctor)

    inspect_p = sub.add_parser("inspect", help="Fetch and inspect one URL for video clues.")
    inspect_p.add_argument("url")
    inspect_p.add_argument("--browser", action="store_true", help="Use Playwright browser fetcher.")
    inspect_p.add_argument("--timeout", type=float, default=20.0)
    inspect_p.add_argument("--json", action="store_true", help="Print structured inspect result as JSON.")
    inspect_p.set_defaults(func=cmd_inspect)

    preview_p = sub.add_parser("preview", help="Show project config and current stored results.")
    preview_p.add_argument("--config", default="vcrawl.json")
    preview_p.set_defaults(func=cmd_preview)

    crawl_p = sub.add_parser("crawl", help="Run a scoped crawl.")
    crawl_p.add_argument("--config", default="vcrawl.json")
    crawl_p.add_argument("--max-pages", type=int, default=100)
    crawl_p.add_argument("--resume", action="store_true", help="Use the configured persistent crawl queue.")
    crawl_p.set_defaults(func=cmd_crawl)

    download_p = sub.add_parser("download", help="Download stored video candidates.")
    download_p.add_argument("--config", default="vcrawl.json")
    download_p.add_argument("--limit", type=int, default=None)
    download_p.add_argument("--probe", action="store_true", help="Run ffprobe after successful downloads.")
    download_p.add_argument("--thumbnail", action="store_true", help="Generate thumbnails with ffmpeg.")
    download_p.add_argument("--concurrency", type=int, default=None)
    download_p.set_defaults(func=cmd_download)

    resolve_p = sub.add_parser("resolve", help="Resolve a page/media URL with yt-dlp metadata extraction.")
    resolve_p.add_argument("url")
    resolve_p.add_argument("--timeout", type=int, default=30)
    resolve_p.set_defaults(func=cmd_resolve)

    export_p = sub.add_parser("export", help="Export stored video candidates.")
    export_p.add_argument("--config", default="vcrawl.json")
    export_p.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export_p.add_argument("--output", default=None)
    export_p.set_defaults(func=cmd_export)

    archive_p = sub.add_parser("archive", help="Build archive sidecars and manifest from stored crawl state.")
    archive_p.add_argument("--config", default="vcrawl.json")
    archive_p.add_argument("--root", default=None)
    archive_p.add_argument("--verify", action="store_true", help="Verify archive after writing sidecars.")
    archive_p.set_defaults(func=cmd_archive)

    archive_verify_p = sub.add_parser("archive-verify", help="Verify archive sidecars, manifest, and HTML snapshots.")
    archive_verify_p.add_argument("--config", default="vcrawl.json")
    archive_verify_p.add_argument("--root", default=None)
    archive_verify_p.set_defaults(func=cmd_archive_verify)

    report_p = sub.add_parser("report", help="Print crawl state, queue, download, and recent run summary.")
    report_p.add_argument("--config", default="vcrawl.json")
    report_p.add_argument("--runs", type=int, default=5)
    report_p.set_defaults(func=cmd_report)

    logs_p = sub.add_parser("logs", help="Print structured event logs from SQLite.")
    logs_p.add_argument("--config", default="vcrawl.json")
    logs_p.add_argument("--limit", type=int, default=50)
    logs_p.add_argument("--type", default=None)
    logs_p.set_defaults(func=cmd_logs)

    timeline_p = sub.add_parser("timeline", help="Print per-URL crawl/download timeline.")
    timeline_p.add_argument("--config", default="vcrawl.json")
    timeline_p.add_argument("--url", default=None)
    timeline_p.add_argument("--phase", default=None)
    timeline_p.add_argument("--status", default=None)
    timeline_p.add_argument("--limit", type=int, default=100)
    timeline_p.set_defaults(func=cmd_timeline)

    integrations_p = sub.add_parser("integrations", help="List optional open-source framework integrations.")
    integrations_p.set_defaults(func=cmd_integrations)

    scaffold_p = sub.add_parser("scaffold", help="Generate an integration project.")
    scaffold_p.add_argument("target", choices=["scrapy", "crawlee-python", "colly", "nutch"])
    scaffold_p.add_argument("--config", default="vcrawl.json")
    scaffold_p.add_argument("--output", default="integrations")
    scaffold_p.set_defaults(func=cmd_scaffold)

    plugin_p = sub.add_parser("plugin-template", help="Create a local Python extractor plugin template.")
    plugin_p.add_argument("--output", default="plugins/site_plugin.py")
    plugin_p.set_defaults(func=cmd_plugin_template)

    plugin_cmd_p = sub.add_parser("plugin", help="Manage and test extractor plugins.")
    plugin_sub = plugin_cmd_p.add_subparsers(dest="plugin_command", required=True)
    plugin_test_p = plugin_sub.add_parser("test", help="Run plugin fixture tests.")
    plugin_test_p.add_argument("--path", default=None, help="Python plugin, plugin manifest JSON, or plugin directory.")
    plugin_test_p.add_argument("--builtin", default=None, help="Built-in plugin name to test.")
    plugin_test_p.add_argument("--fixture", default=None, help="Fixture JSON file.")
    plugin_test_p.add_argument("--config-json", default=None, help="Plugin config as JSON object.")
    plugin_test_p.set_defaults(func=cmd_plugin_test)
    plugin_list_p = plugin_sub.add_parser("list-builtins", help="List built-in generic plugins.")
    plugin_list_p.set_defaults(func=cmd_plugin_list_builtins)

    discover_p = sub.add_parser("discover", help="Expand configured seed, sitemap, and feed URLs.")
    discover_p.add_argument("--config", default="vcrawl.json")
    discover_p.add_argument("--limit", type=int, default=50)
    discover_p.set_defaults(func=cmd_discover)

    profile_p = sub.add_parser("profile", help="Create a browser profile directory and print config guidance.")
    profile_p.add_argument("--path", default=".vcrawl/browser-profile")
    profile_p.set_defaults(func=cmd_profile)

    schedule_p = sub.add_parser("schedule", help="Run crawl repeatedly with a local interval.")
    schedule_p.add_argument("--config", default="vcrawl.json")
    schedule_p.add_argument("--max-pages", type=int, default=100)
    schedule_p.add_argument("--interval", type=int, default=None)
    schedule_p.add_argument("--max-runs", type=int, default=None)
    schedule_p.set_defaults(func=cmd_schedule)

    ui_p = sub.add_parser("ui", help="Start a small local status UI.")
    ui_p.add_argument("--config", default="vcrawl.json")
    ui_p.add_argument("--host", default="127.0.0.1")
    ui_p.add_argument("--port", type=int, default=8765)
    ui_p.set_defaults(func=cmd_ui)

    test_matrix_p = sub.add_parser("test-matrix", help="Run vcrawl's layered verification matrix.")
    test_matrix_p.add_argument("--layer", choices=list(MATRIX_LAYERS) + ["all"], default="unit")
    test_matrix_p.add_argument("--config", default=None)
    test_matrix_p.add_argument("--output", default=".vcrawl/test-matrix")
    test_matrix_p.add_argument("--smoke-url", default=None)
    test_matrix_p.add_argument("--max-pages", type=int, default=1)
    test_matrix_p.add_argument("--long-run-size", type=int, default=1000)
    test_matrix_p.add_argument("--json", action="store_true", help="Print the full JSON report.")
    test_matrix_p.set_defaults(func=cmd_test_matrix)

    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args)
    if args.command == "init":
        return cmd_init(args)
    return 2


def cmd_init(args):
    project_dir = os.path.abspath(args.project)
    os.makedirs(project_dir, exist_ok=True)
    seeds = args.seed or ["https://www.w3schools.com/html/html5_video.asp"]
    allowed_domains = sorted({seed.split("/")[2] for seed in seeds if "://" in seed})
    config = ProjectConfig(project=os.path.basename(project_dir), seeds=seeds)
    config.scope = ScopeConfig(allowed_domains=allowed_domains, max_depth=1)
    config_path = os.path.join(project_dir, "vcrawl.json")
    save_config(config, config_path)
    print("created %s" % config_path)
    print("next: cd %s && python3 -m vcrawl preview --config vcrawl.json" % project_dir)
    return 0


def cmd_doctor(args):
    config = load_config(args.config) if args.config else None
    if args.json or args.capabilities or args.fix_plan:
        report = build_capability_report(config=config)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_capability_report(report, include_install_plan=args.fix_plan))
        return 0
    print(format_diagnostics())
    return 0


def cmd_inspect(args):
    fetcher = BrowserFetcher(timeout_seconds=args.timeout) if args.browser else HttpFetcher(timeout_seconds=args.timeout)
    config = ProjectConfig(seeds=[args.url])
    engine = CrawlEngine(config, fetcher=fetcher)
    result, extracted = engine.inspect(args.url)
    if args.json:
        print(json.dumps(_inspect_payload(result, extracted), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not result.error else 1
    _print_inspect_text(result, extracted)
    return 0 if not result.error else 1


def _inspect_payload(result, extracted):
    return {
        "url": result.final_url or result.url,
        "requested_url": result.url,
        "status": result.status_code,
        "fetcher": result.fetcher,
        "error": result.error,
        "challenge_detected": bool(result.challenge_detected or (extracted and extracted.challenge_detected)),
        "title": extracted.title if extracted else None,
        "links": extracted.links if extracted else [],
        "media_hints": [asdict(hint) for hint in (result.media_hints or [])],
        "videos": [asdict(video) for video in (extracted.videos if extracted else [])],
        "diagnostics": extracted.diagnostics if extracted else {},
        "diagnostic_summary": _diagnostic_summary(extracted.diagnostics if extracted else {}),
    }


def _print_inspect_text(result, extracted):
    print("url: %s" % (result.final_url or result.url))
    print("status: %s" % result.status_code)
    print("fetcher: %s" % result.fetcher)
    if result.error:
        print("error: %s" % result.error)
        return
    print("challenge_detected: %s" % bool(result.challenge_detected or (extracted and extracted.challenge_detected)))
    print("title: %s" % ((extracted.title if extracted else None) or ""))
    print("links: %s" % (len(extracted.links) if extracted else 0))
    print("media_hints: %s" % len(result.media_hints or []))
    print("videos: %s" % (len(extracted.videos) if extracted else 0))
    if extracted:
        for line in _format_diagnostic_lines(extracted.diagnostics):
            print(line)
        print("diagnostics: %s" % json.dumps(extracted.diagnostics, ensure_ascii=False, sort_keys=True))
    resolver = BuiltinMediaResolver()
    for idx, video in enumerate((extracted.videos if extracted else [])[:20], 1):
        resolved = resolver.resolve(video)
        detail = ""
        if video.metadata.get("content_type"):
            detail = " content_type=%s" % video.metadata["content_type"]
        print(
            "%02d. [%s] %s (%s)%s"
            % (idx, video.source, video.media_url, "direct" if resolved["downloadable"] else "embedded", detail)
        )


def _format_diagnostic_lines(diagnostics):
    summary = _diagnostic_summary(diagnostics)
    lines = [
        (
            "diagnostic_summary: final=%s html=%s regex=%s plugins=%s network=%s dynamic_score=%s"
            % (
                summary["final_candidates"],
                summary["html_candidates"],
                summary["regex_candidates"],
                summary["plugin_candidates"],
                summary["media_hint_candidates"],
                summary["dynamic_score"],
            )
        )
    ]
    if summary["script_markers"] != "-":
        lines.append("dynamic_script_markers: %s" % summary["script_markers"])
    if summary["player_scripts"] != "-":
        lines.append("player_scripts: %s" % summary["player_scripts"])
    if summary["network_hint_kinds"] != "-":
        lines.append("network_hint_kinds: %s" % summary["network_hint_kinds"])
    if summary["notes"]:
        lines.append("diagnostic_notes: %s" % ", ".join(summary["notes"]))
    if summary["recommendations"]:
        lines.append("diagnostic_recommendations: %s" % ", ".join(summary["recommendations"]))
    return lines


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


def cmd_preview(args):
    config = load_config(args.config)
    print(json.dumps(config_to_dict(config), indent=2, ensure_ascii=False))
    if os.path.exists(config.storage.state):
        store = SQLiteStore(config.storage.state)
        try:
            print("stored:", json.dumps(store.stats(), ensure_ascii=False, sort_keys=True))
        finally:
            store.close()
    return 0


def cmd_crawl(args):
    config = load_config(args.config)
    stats = CrawlWorker(config).run(max_pages=args.max_pages, resume=args.resume, on_event=_print_event)
    print(
        "done fetched=%s failed=%s skipped=%s challenges=%s videos=%s queued=%s downloaded=%s download_failed=%s"
        % (
            stats.fetched,
            stats.failed,
            stats.skipped,
            stats.challenge_pages,
            stats.videos,
            stats.download_queued,
            stats.downloaded,
            stats.download_failed,
        )
    )
    return 0


def cmd_download(args):
    config = load_config(args.config)
    if args.probe:
        config.media.probe = True
    if args.thumbnail:
        config.media.thumbnail = True
    if args.concurrency:
        config.media.download_concurrency = args.concurrency
    results = DownloadWorker(config).run_pending(limit=args.limit, on_event=_print_event)
    downloaded = sum(1 for result in results if result.status == "downloaded")
    failed = sum(1 for result in results if result.status == "failed")
    print("downloaded=%s failed=%s candidates=%s" % (downloaded, failed, len(results)))
    return 0 if failed == 0 else 1


def cmd_resolve(args):
    resolver = YtDlpResolver()
    try:
        metadata = resolver.resolve(args.url, timeout_seconds=args.timeout)
    except Exception as exc:
        print("error: %s" % exc)
        return 1
    summary = {
        "id": metadata.get("id"),
        "title": metadata.get("title"),
        "duration": metadata.get("duration"),
        "extractor": metadata.get("extractor"),
        "webpage_url": metadata.get("webpage_url"),
        "formats": len(metadata.get("formats") or []),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args):
    config = load_config(args.config)
    output_path = args.output or config.storage.metadata
    if args.format == "csv" and args.output is None:
        output_path = os.path.splitext(config.storage.metadata)[0] + ".csv"
    store = SQLiteStore(config.storage.state)
    try:
        if args.format == "csv":
            count = export_csv(store, output_path)
        else:
            count = export_jsonl(store, output_path)
    finally:
        store.close()
    print("exported %s videos to %s" % (count, output_path))
    return 0


def cmd_archive(args):
    config = load_config(args.config)
    if args.root:
        config.archive.root = os.path.abspath(args.root)
    store = SQLiteStore(config.storage.state)
    try:
        manager = ArchiveManager(archive_options_from_config(config))
        result = manager.write_sidecars(store)
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.verify:
        verification = verify_archive(config.archive.root)
        print(json.dumps({"verify": verification}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if verification["ok"] else 1
    return 0


def cmd_archive_verify(args):
    config = load_config(args.config)
    root = os.path.abspath(args.root) if args.root else config.archive.root
    result = verify_archive(root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def cmd_report(args):
    config = load_config(args.config)
    store = SQLiteStore(config.storage.state)
    try:
        stats = store.stats()
        runs = store.recent_runs(limit=args.runs)
    finally:
        store.close()
    print(json.dumps({"stats": stats, "runs": runs}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_logs(args):
    config = load_config(args.config)
    store = SQLiteStore(config.storage.state)
    try:
        rows = store.list_events(limit=args.limit, event_type=args.type)
    finally:
        store.close()
    print(json.dumps({"events": rows}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_timeline(args):
    config = load_config(args.config)
    store = SQLiteStore(config.storage.state)
    try:
        rows = store.list_timeline(limit=args.limit, url=args.url, phase=args.phase, status=args.status)
        summary = store.timeline_summary(limit=50)
    finally:
        store.close()
    print(json.dumps({"timeline": rows, "summary": summary}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_integrations(_args):
    for item in integration_report():
        status = "available" if item["available"] else "optional-missing"
        print("%-15s %-17s %-16s %s" % (item["name"], item["kind"], status, item["capability"]))
    return 0


def cmd_scaffold(args):
    config = load_config(args.config)
    if args.target == "scrapy":
        output = scaffold_scrapy(config, os.path.join(args.output, "scrapy"))
    elif args.target == "crawlee-python":
        output = scaffold_crawlee_python(config, os.path.join(args.output, "crawlee-python"))
    elif args.target == "colly":
        output = scaffold_colly(config, os.path.join(args.output, "colly"))
    else:
        output = scaffold_nutch(config, os.path.join(args.output, "nutch"))
    print("created %s integration scaffold at %s" % (args.target, output))
    return 0


def cmd_plugin_template(args):
    path = write_plugin_template(args.output)
    print("created plugin template: %s" % path)
    print("add it to extract.plugin_paths in vcrawl.json")
    return 0


def cmd_plugin_test(args):
    config = json.loads(args.config_json) if args.config_json else None
    result = run_plugin_tests(path=args.path, fixture_path=args.fixture, builtin=args.builtin, config=config)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def cmd_plugin_list_builtins(_args):
    rows = [
        {
            "name": spec.name,
            "version": spec.version,
            "capabilities": spec.capabilities,
            "fixtures": len(spec.fixtures),
        }
        for spec in list_builtin_plugins()
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_discover(args):
    config = load_config(args.config)
    urls = expand_discovery_seeds(config)
    for url in urls[: args.limit]:
        print(url)
    if len(urls) > args.limit:
        print("... %s more" % (len(urls) - args.limit))
    return 0


def cmd_profile(args):
    path = os.path.abspath(args.path)
    os.makedirs(path, exist_ok=True)
    print("created browser profile directory: %s" % path)
    print('set fetch.default to "browser" and fetch.browser_profile to this path in vcrawl.json')
    return 0


def cmd_schedule(args):
    from .scheduler import run_schedule

    config = load_config(args.config)
    results = run_schedule(
        config,
        max_pages=args.max_pages,
        interval_seconds=args.interval,
        max_runs=args.max_runs,
        on_event=_print_event,
    )
    for idx, stats in enumerate(results, 1):
        print(
            "run=%s fetched=%s failed=%s videos=%s downloaded=%s"
            % (idx, stats.fetched, stats.failed, stats.videos, stats.downloaded)
        )
    return 0


def cmd_ui(args):
    from .ui import serve

    serve(args.config, host=args.host, port=args.port)
    return 0


def cmd_test_matrix(args):
    report = run_test_matrix(
        layer=args.layer,
        config_path=args.config,
        output_dir=args.output,
        smoke_url=args.smoke_url,
        max_pages=args.max_pages,
        long_run_size=args.long_run_size,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_test_matrix_summary(report))
    return 0 if report["ok"] else 1


def _print_event(kind, message):
    print("%s: %s" % (kind, message))


def _yt_dlp_available():
    return YtDlpResolver().available()
