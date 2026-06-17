from .doctor import collect_diagnostics


CAPABILITY_DEFINITIONS = [
    {
        "id": "core-crawl",
        "label": "Core crawl",
        "description": "Scoped HTTP crawl, extraction, SQLite state, archive sidecars, and local UI.",
        "requires": ["python", "sqlite3"],
        "install": [],
    },
    {
        "id": "browser-pages",
        "label": "Dynamic pages",
        "description": "Playwright browser fetching for JavaScript-rendered pages and user-owned browser profiles.",
        "requires": ["playwright"],
        "install": [
            'python3 -m pip install -e ".[browser]"',
            "python3 -m playwright install chromium",
        ],
    },
    {
        "id": "media-resolver",
        "label": "Known-site media resolver",
        "description": "yt-dlp metadata and media resolution for supported public sites and embedded players.",
        "requires": ["yt-dlp"],
        "install": ['python3 -m pip install -e ".[media]"'],
    },
    {
        "id": "media-processing",
        "label": "Media processing",
        "description": "ffprobe metadata and FFmpeg thumbnail/post-processing workflows.",
        "requires": ["ffmpeg", "ffprobe"],
        "install": ["brew install ffmpeg"],
    },
    {
        "id": "crawler-frameworks",
        "label": "Crawler framework scaffolds",
        "description": "Scrapy and Crawlee scaffolds for larger scheduler, middleware, dataset, and browser-crawl workflows.",
        "requires": ["scrapy", "crawlee"],
        "install": ['python3 -m pip install -e ".[crawler]"'],
    },
    {
        "id": "redis-queue",
        "label": "Redis shared queue",
        "description": "Redis client support for shared multi-worker crawl queues.",
        "requires": ["redis"],
        "install": ['python3 -m pip install -e ".[redis]"'],
    },
    {
        "id": "postgres-queue",
        "label": "Postgres shared queue",
        "description": "Postgres driver support for shared multi-worker crawl queues using row locking.",
        "requires": ["postgres-driver"],
        "install": ['python3 -m pip install -e ".[postgres]"'],
    },
    {
        "id": "yaml-config",
        "label": "YAML config",
        "description": "Optional YAML project config loading.",
        "requires": ["yaml"],
        "install": ['python3 -m pip install -e ".[yaml]"'],
    },
    {
        "id": "warc-archive",
        "label": "WARC archive",
        "description": "Standard-library WARC/1.1 response records plus JSONL sidecars and manifest verification.",
        "requires": [],
        "install": [],
    },
    {
        "id": "plugin-ecosystem",
        "label": "Plugin ecosystem",
        "description": "Built-in and manifest-based extractor plugins with fixture tests.",
        "requires": [],
        "install": [],
    },
]


CAPABILITY_PACKAGE_DEFINITIONS = [
    {
        "id": "starter",
        "label": "Starter crawl",
        "description": "Lightweight local crawl, SQLite state, archive sidecars, and plugins.",
        "capabilities": ["core-crawl", "warc-archive", "plugin-ecosystem"],
        "verify": [
            "python3 -m vcrawl doctor",
            "python3 -m vcrawl test-matrix --layer unit",
        ],
    },
    {
        "id": "dynamic-video",
        "label": "Dynamic video discovery",
        "description": "Browser rendering and network media hints for JavaScript player pages.",
        "capabilities": ["browser-pages"],
        "verify": [
            "python3 -m vcrawl inspect <url> --browser",
            "python3 -m playwright install chromium",
        ],
    },
    {
        "id": "media-tools",
        "label": "Media resolver and processing",
        "description": "yt-dlp resolution, ffprobe metadata, and FFmpeg thumbnail/post-processing.",
        "capabilities": ["media-resolver", "media-processing"],
        "verify": [
            "python3 -m vcrawl resolve <url>",
            "python3 -m vcrawl download --config vcrawl.json --limit 1",
        ],
    },
    {
        "id": "distributed-workers",
        "label": "Distributed workers",
        "description": "Shared Redis/Postgres queues for multi-worker and crash-recovery crawls.",
        "capabilities": ["redis-queue", "postgres-queue"],
        "verify": [
            "python3 -m vcrawl doctor --capabilities",
            "python3 -m vcrawl test-matrix --layer long-run",
        ],
    },
    {
        "id": "framework-bridges",
        "label": "Crawler framework bridges",
        "description": "Scrapy, Crawlee, Colly, and Nutch scaffolds for larger crawler ecosystems.",
        "capabilities": ["crawler-frameworks"],
        "verify": [
            "python3 -m vcrawl scaffold scrapy --config vcrawl.json --output integrations",
            "python3 -m vcrawl test-matrix --layer integration",
        ],
    },
]


def build_capability_report(config=None, diagnostics=None):
    diagnostics = diagnostics or collect_diagnostics()
    tools = {
        name: {
            "name": name,
            "detail": detail,
            "ok": bool(ok),
            "status": "ok" if ok else "optional-missing",
        }
        for name, detail, ok in diagnostics
    }
    capabilities = [_capability_state(definition, tools) for definition in CAPABILITY_DEFINITIONS]
    packages = build_capability_packages(capabilities)
    return {
        "summary": _summary(capabilities),
        "tools": list(tools.values()),
        "packages": packages,
        "capabilities": capabilities,
        "install_plan": build_install_plan(capabilities),
        "config": _config_hints(config),
    }


def build_capability_packages(capabilities):
    by_id = {capability["id"]: capability for capability in capabilities}
    return [_package_state(definition, by_id) for definition in CAPABILITY_PACKAGE_DEFINITIONS]


def build_install_plan(capabilities):
    plan = []
    seen = set()
    for capability in capabilities:
        if capability["status"] == "available":
            continue
        for command in capability.get("install") or []:
            if command in seen:
                continue
            seen.add(command)
            plan.append(
                {
                    "capability": capability["id"],
                    "label": capability["label"],
                    "command": command,
                    "missing": capability["missing"],
                }
            )
    return plan


def format_capability_report(report, include_install_plan=True):
    lines = [
        "Capabilities: %s available / %s partial / %s missing"
        % (
            report["summary"]["available"],
            report["summary"]["partial"],
            report["summary"]["missing"],
        )
    ]
    for capability in report["capabilities"]:
        missing = ", ".join(capability["missing"])
        detail = "missing %s" % missing if missing else capability["description"]
        lines.append("%-28s %-10s %s" % (capability["label"], capability["status"], detail))
    if include_install_plan and report["install_plan"]:
        lines.append("")
        lines.append("Install plan:")
        for step in report["install_plan"]:
            lines.append("- %s: %s" % (step["label"], step["command"]))
    if report.get("packages"):
        lines.append("")
        lines.append("Capability packages:")
        for package in report["packages"]:
            next_step = package["next_steps"][0] if package["next_steps"] else package["description"]
            lines.append("%-28s %-10s %s" % (package["label"], package["status"], next_step))
    return "\n".join(lines)


def _capability_state(definition, tools):
    required = list(definition.get("requires") or [])
    missing = [name for name in required if not tools.get(name, {}).get("ok")]
    available = [name for name in required if tools.get(name, {}).get("ok")]
    if not required:
        status = "available"
    elif not missing:
        status = "available"
    elif available:
        status = "partial"
    else:
        status = "missing"
    return {
        "id": definition["id"],
        "label": definition["label"],
        "description": definition["description"],
        "status": status,
        "requires": required,
        "available": available,
        "missing": missing,
        "install": list(definition.get("install") or []),
    }


def _package_state(definition, capabilities_by_id):
    related = [capabilities_by_id[capability_id] for capability_id in definition.get("capabilities", [])]
    statuses = [capability["status"] for capability in related]
    if statuses and all(status == "available" for status in statuses):
        status = "available"
    elif any(status in ("available", "partial") for status in statuses):
        status = "partial"
    else:
        status = "missing"
    missing = []
    install_commands = []
    seen = set()
    for capability in related:
        missing.extend(capability.get("missing") or [])
        if capability["status"] == "available":
            continue
        for command in capability.get("install") or []:
            if command in seen:
                continue
            seen.add(command)
            install_commands.append(command)
    next_steps = _package_next_steps(status, install_commands, definition.get("verify") or [])
    return {
        "id": definition["id"],
        "label": definition["label"],
        "description": definition["description"],
        "status": status,
        "capabilities": [capability["id"] for capability in related],
        "missing": sorted(set(missing)),
        "install_commands": install_commands,
        "verify_commands": list(definition.get("verify") or []),
        "next_steps": next_steps,
    }


def _package_next_steps(status, install_commands, verify_commands):
    if status == "available":
        return ["Verify with %s" % (verify_commands[0] if verify_commands else "python3 -m vcrawl doctor")]
    if install_commands:
        return ["Install: %s" % install_commands[0]]
    return ["Review missing optional tools with python3 -m vcrawl doctor --fix-plan"]


def _summary(capabilities):
    counts = {"available": 0, "partial": 0, "missing": 0}
    for capability in capabilities:
        counts[capability["status"]] = counts.get(capability["status"], 0) + 1
    counts["total"] = len(capabilities)
    return counts


def _config_hints(config):
    if config is None:
        return {}
    return {
        "queue_backend": config.queue.backend,
        "archive_warc": bool(config.archive.warc),
        "fetch_default": config.fetch.default,
        "http_cache": bool(config.network.http_cache),
        "proxy_configured": bool(config.network.proxy_url or config.network.proxy_urls),
        "cookies_configured": bool(config.network.cookies_file),
        "browser_profile_configured": bool(config.fetch.browser_profile),
    }
