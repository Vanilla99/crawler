# VCrawl Observability

`vcrawl` records crawl/debug signals in three layers:

- SQLite events for the UI and CLI.
- Per-URL timeline rows for fetch, extract, download, archive, policy, and run phases.
- Structured JSONL logs for external tooling.

Useful commands:

```bash
python3 -m vcrawl logs --config vcrawl.json --limit 50
python3 -m vcrawl timeline --config vcrawl.json --limit 100
python3 -m vcrawl timeline --config vcrawl.json --phase fetch
```

The UI Logs tab reads the same SQLite event rows and exposes phase, status, error class, worker, message, and URL. Its Live toggle polls recent logs every five seconds while that tab is active, which is useful for watching long crawls without tailing the JSONL file.

Config:

```json
{
  "observability": {
    "enabled": true,
    "json_logs": true,
    "json_log_path": ".vcrawl/logs/events.jsonl",
    "timeline": true,
    "opentelemetry": false,
    "service_name": "vcrawl"
  }
}
```

Error taxonomy is intentionally small and operational:

- `verification_required`
- `domain_failure_threshold` for global or per-domain failure threshold blocks
- `forbidden_or_auth`
- `not_found`
- `rate_limited`
- `timeout`
- `server_error`
- `tls_error`
- `dns_error`
- `connection_error`
- `robots_blocked`
- `media_resolver_error`
- `media_tool_error`

Policy timeline events include `domain_blocked` and `policy_autothrottle`, so a run can explain both threshold-based skips and automatic per-domain delay changes. Download timeline events start with `download_queued` from crawl stages, then `download`/`downloaded`/`download_failed` from the download worker.

OpenTelemetry is optional. When `observability.opentelemetry` is true and the `opentelemetry-api` package is installed, vcrawl emits lightweight spans from EventBus events. Without the package, the sink degrades to a no-op so the default install remains lightweight.
