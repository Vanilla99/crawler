import json
import os
import sqlite3
import time
from threading import Lock


def _fetch_diagnostics(fetch_result):
    notes = []
    if fetch_result.error:
        notes.append("fetch_error")
    if fetch_result.challenge_detected:
        notes.append("challenge_detected")
    if not (fetch_result.text or "").strip():
        notes.append("empty_response_body")
    if fetch_result.fetcher != "browser" and not (fetch_result.media_hints or []):
        notes.append("try_browser_fetcher_for_dynamic_players")
    return {
        "status_code": fetch_result.status_code,
        "fetcher": fetch_result.fetcher,
        "media_hint_count": len(fetch_result.media_hints or []),
        "final_candidates": 0,
        "challenge_detected": bool(fetch_result.challenge_detected),
        "notes": notes,
    }


class SQLiteStore:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = Lock()
        self._init_schema()

    def close(self):
        self.conn.close()

    def _init_schema(self):
        with self._lock:
            self.conn.executescript(
                """
            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                status_code INTEGER NOT NULL,
                fetcher TEXT NOT NULL,
                title TEXT,
                error TEXT,
                challenge_detected INTEGER NOT NULL DEFAULT 0,
                diagnostics_json TEXT NOT NULL DEFAULT '{}',
                fetched_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_url TEXT NOT NULL,
                media_url TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT,
                source TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(page_url, media_url, source)
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_url TEXT NOT NULL,
                media_url TEXT NOT NULL,
                status TEXT NOT NULL,
                output_path TEXT,
                resolver TEXT NOT NULL,
                error TEXT,
                metadata_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(page_url, media_url)
            );

            CREATE TABLE IF NOT EXISTS crawl_queue (
                url TEXT PRIMARY KEY,
                depth INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                referer TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                stats_json TEXT NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                current_url TEXT,
                stats_json TEXT NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS controls (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                message TEXT,
                url TEXT,
                worker_id TEXT,
                data_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS domain_state (
                domain TEXT PRIMARY KEY,
                successes INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_status_code INTEGER,
                last_error TEXT,
                challenge_detected INTEGER NOT NULL DEFAULT 0,
                health TEXT NOT NULL DEFAULT 'new',
                dynamic_delay_seconds REAL,
                avg_latency_ms REAL,
                last_fetch_at REAL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_health (
                session_id TEXT PRIMARY KEY,
                domain TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                successes INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                last_used_at REAL,
                metadata_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS url_timeline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT,
                worker_id TEXT,
                error_class TEXT,
                duration_ms REAL,
                data_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
            )
            self._ensure_column("pages", "diagnostics_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("domain_state", "dynamic_delay_seconds", "REAL")
            self._ensure_column("domain_state", "avg_latency_ms", "REAL")
            self.conn.commit()

    def _ensure_column(self, table, column, declaration):
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in columns:
            self.conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration))

    def record_page(self, fetch_result, extracted=None):
        title = extracted.title if extracted else None
        diagnostics = extracted.diagnostics if extracted else _fetch_diagnostics(fetch_result)
        with self._lock:
            self.conn.execute(
                """
            INSERT INTO pages(url, status_code, fetcher, title, error, challenge_detected, diagnostics_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                status_code=excluded.status_code,
                fetcher=excluded.fetcher,
                title=excluded.title,
                error=excluded.error,
                challenge_detected=excluded.challenge_detected,
                diagnostics_json=excluded.diagnostics_json,
                fetched_at=excluded.fetched_at
            """,
            (
                fetch_result.final_url or fetch_result.url,
                fetch_result.status_code,
                fetch_result.fetcher,
                title,
                fetch_result.error,
                1 if (fetch_result.challenge_detected or (extracted and extracted.challenge_detected)) else 0,
                json.dumps(diagnostics or {}, sort_keys=True),
                time.time(),
            ),
            )
            self.conn.commit()

    def record_videos(self, videos):
        with self._lock:
            for video in videos:
                self.conn.execute(
                    """
                INSERT OR IGNORE INTO videos(page_url, media_url, kind, title, source, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        video.page_url,
                        video.media_url,
                        video.kind,
                        video.title,
                        video.source,
                        json.dumps(video.metadata, sort_keys=True),
                        time.time(),
                    ),
                )
            self.conn.commit()

    def record_download(self, result):
        with self._lock:
            self.conn.execute(
                """
            INSERT INTO downloads(page_url, media_url, status, output_path, resolver, error, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_url, media_url) DO UPDATE SET
                status=excluded.status,
                output_path=excluded.output_path,
                resolver=excluded.resolver,
                error=excluded.error,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                result.page_url,
                result.media_url,
                result.status,
                result.output_path,
                result.resolver,
                result.error,
                json.dumps(result.metadata, sort_keys=True),
                time.time(),
            ),
            )
            self.conn.commit()

    def enqueue_download_task(self, task):
        now = time.time()
        metadata = json.dumps(task.metadata or {}, sort_keys=True)
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT status
                FROM downloads
                WHERE page_url=? AND media_url=?
                """,
                (task.page_url, task.media_url),
            )
            existing = cursor.fetchone()
            if existing and existing["status"] == "downloaded":
                return False
            self.conn.execute(
                """
                INSERT OR IGNORE INTO videos(page_url, media_url, kind, title, source, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.page_url,
                    task.media_url,
                    task.kind,
                    task.title,
                    task.source,
                    metadata,
                    now,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO downloads(page_url, media_url, status, output_path, resolver, error, metadata_json, updated_at)
                VALUES (?, ?, 'queued', NULL, 'queued', NULL, ?, ?)
                ON CONFLICT(page_url, media_url) DO UPDATE SET
                    status='queued',
                    output_path=NULL,
                    resolver='queued',
                    error=NULL,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (task.page_url, task.media_url, metadata, now),
            )
            self.conn.commit()
            return True

    def enqueue_download_tasks(self, tasks):
        queued = 0
        for task in tasks:
            if self.enqueue_download_task(task):
                queued += 1
        return queued

    def enqueue_request(self, request):
        now = time.time()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO crawl_queue(url, depth, priority, referer, status, attempts, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    depth=MIN(crawl_queue.depth, excluded.depth),
                    priority=MAX(crawl_queue.priority, excluded.priority),
                    referer=COALESCE(crawl_queue.referer, excluded.referer),
                    updated_at=excluded.updated_at
                """,
                (request.url, request.depth, request.priority, request.referer, now, now),
            )
            self.conn.commit()

    def enqueue_requests(self, requests):
        for request in requests:
            self.enqueue_request(request)

    def next_queued_requests(self, limit=1):
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT url, depth, priority, referer
                FROM crawl_queue
                WHERE status='pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            now = time.time()
            for row in rows:
                self.conn.execute(
                    """
                    UPDATE crawl_queue
                    SET status='in_progress', attempts=attempts + 1, updated_at=?
                    WHERE url=? AND status='pending'
                    """,
                    (now, row["url"]),
                )
            self.conn.commit()
        return rows

    def mark_queue_status(self, url, status, error=None):
        with self._lock:
            self.conn.execute(
                """
                UPDATE crawl_queue
                SET status=?, error=?, updated_at=?
                WHERE url=?
                """,
                (status, error, time.time(), url),
            )
            self.conn.commit()

    def reset_in_progress_requests(self):
        with self._lock:
            self.conn.execute(
                """
                UPDATE crawl_queue
                SET status='pending', updated_at=?
                WHERE status='in_progress'
                """,
                (time.time(),),
            )
            self.conn.commit()

    def clear_queue(self, status=None):
        with self._lock:
            if status:
                cursor = self.conn.execute("DELETE FROM crawl_queue WHERE status=?", (status,))
            else:
                cursor = self.conn.execute("DELETE FROM crawl_queue")
            self.conn.commit()
            return cursor.rowcount

    def recover_stale_queue(self, stale_after_seconds):
        if stale_after_seconds is None or stale_after_seconds <= 0:
            return 0
        cutoff = time.time() - float(stale_after_seconds)
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE crawl_queue
                SET status='pending', error=NULL, updated_at=?
                WHERE status='in_progress' AND updated_at < ?
                """,
                (time.time(), cutoff),
            )
            self.conn.commit()
            return cursor.rowcount

    def queue_stats(self):
        cursor = self.conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM crawl_queue
            GROUP BY status
            """
        )
        return {row["status"]: row["count"] for row in cursor.fetchall()}

    def list_queue(self, limit=100, status=None):
        if status:
            cursor = self.conn.execute(
                """
                SELECT url, depth, priority, referer, status, attempts, error, created_at, updated_at
                FROM crawl_queue
                WHERE status=?
                ORDER BY priority DESC, updated_at DESC
                LIMIT ?
                """,
                (status, limit),
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT url, depth, priority, referer, status, attempts, error, created_at, updated_at
                FROM crawl_queue
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]

    def start_run(self, note=None):
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO runs(status, started_at, finished_at, stats_json, note)
                VALUES ('running', ?, NULL, '{}', ?)
                """,
                (time.time(), note),
            )
            self.conn.commit()
            return cursor.lastrowid

    def register_worker(self, worker_id, kind, status="running"):
        now = time.time()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO workers(worker_id, kind, status, started_at, heartbeat_at, current_url, stats_json, error)
                VALUES (?, ?, ?, ?, ?, NULL, '{}', NULL)
                ON CONFLICT(worker_id) DO UPDATE SET
                    kind=excluded.kind,
                    status=excluded.status,
                    started_at=excluded.started_at,
                    heartbeat_at=excluded.heartbeat_at,
                    current_url=NULL,
                    stats_json='{}',
                    error=NULL
                """,
                (worker_id, kind, status, now, now),
            )
            self.conn.commit()

    def heartbeat_worker(self, worker_id, current_url=None, stats=None, status="running"):
        with self._lock:
            self.conn.execute(
                """
                UPDATE workers
                SET status=?, heartbeat_at=?, current_url=COALESCE(?, current_url), stats_json=?
                WHERE worker_id=?
                """,
                (
                    status,
                    time.time(),
                    current_url,
                    json.dumps(stats or {}, sort_keys=True),
                    worker_id,
                ),
            )
            self.conn.commit()

    def finish_worker(self, worker_id, status="stopped", error=None):
        with self._lock:
            self.conn.execute(
                """
                UPDATE workers
                SET status=?, heartbeat_at=?, error=?
                WHERE worker_id=?
                """,
                (status, time.time(), error, worker_id),
            )
            self.conn.commit()

    def list_workers(self, limit=100):
        cursor = self.conn.execute(
            """
            SELECT worker_id, kind, status, started_at, heartbeat_at, current_url, stats_json, error
            FROM workers
            ORDER BY heartbeat_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def set_control(self, name, value):
        text = "1" if value is True else "0" if value is False else str(value)
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO controls(name, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (name, text, time.time()),
            )
            self.conn.commit()

    def get_control(self, name, default=None):
        cursor = self.conn.execute("SELECT value FROM controls WHERE name=?", (name,))
        row = cursor.fetchone()
        return row["value"] if row else default

    def control_flag(self, name, default=False):
        value = self.get_control(name, "1" if default else "0")
        return str(value).lower() in ("1", "true", "yes", "on")

    def list_controls(self):
        cursor = self.conn.execute(
            """
            SELECT name, value, updated_at
            FROM controls
            ORDER BY name ASC
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def record_event(self, event_type, message="", url=None, worker_id=None, data=None):
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO events(event_type, message, url, worker_id, data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    message,
                    url,
                    worker_id,
                    json.dumps(data or {}, sort_keys=True),
                    time.time(),
                ),
            )
            self.conn.commit()

    def list_events(self, limit=200, event_type=None):
        if event_type:
            cursor = self.conn.execute(
                """
                SELECT id, event_type, message, url, worker_id, data_json, created_at
                FROM events
                WHERE event_type=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (event_type, limit),
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT id, event_type, message, url, worker_id, data_json, created_at
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]

    def record_timeline(
        self,
        url,
        phase,
        status,
        event_type,
        message="",
        worker_id=None,
        error_class=None,
        duration_ms=None,
        data=None,
    ):
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO url_timeline(
                    url, phase, status, event_type, message, worker_id,
                    error_class, duration_ms, data_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    phase,
                    status,
                    event_type,
                    message,
                    worker_id,
                    error_class,
                    duration_ms,
                    json.dumps(data or {}, sort_keys=True),
                    time.time(),
                ),
            )
            self.conn.commit()

    def list_timeline(self, limit=200, url=None, phase=None, status=None):
        clauses = []
        params = []
        if url:
            clauses.append("url=?")
            params.append(url)
        if phase:
            clauses.append("phase=?")
            params.append(phase)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cursor = self.conn.execute(
            """
            SELECT id, url, phase, status, event_type, message, worker_id,
                   error_class, duration_ms, data_json, created_at
            FROM url_timeline
            %s
            ORDER BY id DESC
            LIMIT ?
            """
            % where,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def timeline_summary(self, limit=50):
        cursor = self.conn.execute(
            """
            SELECT url,
                   COUNT(*) AS events,
                   MAX(created_at) AS last_event_at,
                   MAX(CASE WHEN status IN ('failed', 'blocked') THEN 1 ELSE 0 END) AS has_error
            FROM url_timeline
            WHERE url IS NOT NULL
            GROUP BY url
            ORDER BY last_event_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def record_domain_result(
        self,
        domain,
        status_code,
        error=None,
        challenge_detected=False,
        latency_ms=None,
        dynamic_delay_seconds=None,
    ):
        if not domain:
            return
        now = time.time()
        failed = bool(error or status_code == 0 or status_code >= 400 or challenge_detected)
        existing = self.get_domain_state(domain)
        successes = int(existing["successes"]) if existing else 0
        failures = int(existing["failures"]) if existing else 0
        consecutive = int(existing["consecutive_failures"]) if existing else 0
        avg_latency_ms = existing.get("avg_latency_ms") if existing else None
        if latency_ms is not None:
            latency_ms = float(latency_ms)
            avg_latency_ms = latency_ms if avg_latency_ms is None else (float(avg_latency_ms) * 0.8) + (latency_ms * 0.2)
        elif existing:
            avg_latency_ms = existing.get("avg_latency_ms")
        if dynamic_delay_seconds is None and existing:
            dynamic_delay_seconds = existing.get("dynamic_delay_seconds")
        if failed:
            failures += 1
            consecutive += 1
            health = "challenge" if challenge_detected else "degraded"
        else:
            successes += 1
            consecutive = 0
            health = "healthy"
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO domain_state(
                    domain, successes, failures, consecutive_failures,
                    last_status_code, last_error, challenge_detected,
                    health, dynamic_delay_seconds, avg_latency_ms, last_fetch_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    successes=excluded.successes,
                    failures=excluded.failures,
                    consecutive_failures=excluded.consecutive_failures,
                    last_status_code=excluded.last_status_code,
                    last_error=excluded.last_error,
                    challenge_detected=excluded.challenge_detected,
                    health=excluded.health,
                    dynamic_delay_seconds=excluded.dynamic_delay_seconds,
                    avg_latency_ms=excluded.avg_latency_ms,
                    last_fetch_at=excluded.last_fetch_at,
                    updated_at=excluded.updated_at
                """,
                (
                    domain,
                    successes,
                    failures,
                    consecutive,
                    status_code,
                    error,
                    1 if challenge_detected else 0,
                    health,
                    dynamic_delay_seconds,
                    avg_latency_ms,
                    now,
                    now,
                ),
            )
            self.conn.commit()

    def get_domain_state(self, domain):
        cursor = self.conn.execute(
            """
            SELECT domain, successes, failures, consecutive_failures, last_status_code,
                   last_error, challenge_detected, health, dynamic_delay_seconds,
                   avg_latency_ms, last_fetch_at, updated_at
            FROM domain_state
            WHERE domain=?
            """,
            (domain,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_domain_state(self, limit=200):
        cursor = self.conn.execute(
            """
            SELECT domain, successes, failures, consecutive_failures, last_status_code,
                   last_error, challenge_detected, health, dynamic_delay_seconds,
                   avg_latency_ms, last_fetch_at, updated_at
            FROM domain_state
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def record_session_health(self, session_id, kind, status, domain=None, success=True, metadata=None):
        now = time.time()
        existing = self.get_session_health(session_id)
        successes = int(existing["successes"]) if existing else 0
        failures = int(existing["failures"]) if existing else 0
        if success:
            successes += 1
        else:
            failures += 1
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO session_health(
                    session_id, domain, kind, status, successes, failures,
                    last_used_at, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    domain=excluded.domain,
                    kind=excluded.kind,
                    status=excluded.status,
                    successes=excluded.successes,
                    failures=excluded.failures,
                    last_used_at=excluded.last_used_at,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    domain,
                    kind,
                    status,
                    successes,
                    failures,
                    now,
                    json.dumps(metadata or {}, sort_keys=True),
                    now,
                ),
            )
            self.conn.commit()

    def get_session_health(self, session_id):
        cursor = self.conn.execute(
            """
            SELECT session_id, domain, kind, status, successes, failures,
                   last_used_at, metadata_json, updated_at
            FROM session_health
            WHERE session_id=?
            """,
            (session_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_session_health(self, limit=200):
        cursor = self.conn.execute(
            """
            SELECT session_id, domain, kind, status, successes, failures,
                   last_used_at, metadata_json, updated_at
            FROM session_health
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def finish_run(self, run_id, status, stats, note=None):
        with self._lock:
            self.conn.execute(
                """
                UPDATE runs
                SET status=?, finished_at=?, stats_json=?, note=COALESCE(?, note)
                WHERE id=?
                """,
                (status, time.time(), json.dumps(stats, sort_keys=True), note, run_id),
            )
            self.conn.commit()

    def recent_runs(self, limit=20):
        cursor = self.conn.execute(
            """
            SELECT id, status, started_at, finished_at, stats_json, note
            FROM runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def list_videos(self, limit=100):
        cursor = self.conn.execute(
            """
            SELECT v.page_url, v.media_url, v.kind, v.title, v.source, v.metadata_json,
                   d.status AS download_status, d.output_path AS output_path, d.error AS download_error,
                   p.status_code AS page_status_code, p.fetcher AS page_fetcher,
                   p.challenge_detected AS page_challenge_detected,
                   p.diagnostics_json AS page_diagnostics_json
            FROM videos v
            LEFT JOIN downloads d ON d.page_url = v.page_url AND d.media_url = v.media_url
            LEFT JOIN pages p ON p.url = v.page_url
            ORDER BY v.id LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def list_pages(self, limit=100, query=None):
        clauses = []
        params = []
        if query:
            clauses.append("(p.url LIKE ? OR p.title LIKE ? OR p.error LIKE ? OR p.diagnostics_json LIKE ?)")
            pattern = "%%%s%%" % query
            params.extend([pattern, pattern, pattern, pattern])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cursor = self.conn.execute(
            """
            SELECT p.url, p.status_code, p.fetcher, p.title, p.error,
                   p.challenge_detected, p.diagnostics_json, p.fetched_at,
                   COUNT(v.id) AS video_count
            FROM pages p
            LEFT JOIN videos v ON v.page_url = p.url
            %s
            GROUP BY p.url
            ORDER BY p.fetched_at DESC
            LIMIT ?
            """
            % where,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_page(self, url):
        cursor = self.conn.execute(
            """
            SELECT p.url, p.status_code, p.fetcher, p.title, p.error,
                   p.challenge_detected, p.diagnostics_json, p.fetched_at,
                   COUNT(v.id) AS video_count
            FROM pages p
            LEFT JOIN videos v ON v.page_url = p.url
            WHERE p.url=?
            GROUP BY p.url
            """,
            (url,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def search_videos(self, limit=100, query=None, download_status=None):
        clauses = []
        params = []
        if query:
            clauses.append("(v.title LIKE ? OR v.page_url LIKE ? OR v.media_url LIKE ? OR v.source LIKE ?)")
            pattern = "%%%s%%" % query
            params.extend([pattern, pattern, pattern, pattern])
        if download_status:
            if download_status == "pending":
                clauses.append("d.status IS NULL")
            else:
                clauses.append("d.status = ?")
                params.append(download_status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cursor = self.conn.execute(
            """
            SELECT v.page_url, v.media_url, v.kind, v.title, v.source, v.metadata_json,
                   d.status AS download_status, d.output_path AS output_path, d.error AS download_error,
                   p.status_code AS page_status_code, p.fetcher AS page_fetcher,
                   p.challenge_detected AS page_challenge_detected,
                   p.diagnostics_json AS page_diagnostics_json
            FROM videos v
            LEFT JOIN downloads d ON d.page_url = v.page_url AND d.media_url = v.media_url
            LEFT JOIN pages p ON p.url = v.page_url
            %s
            ORDER BY v.id DESC LIMIT ?
            """
            % where,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def recent_downloads(self, limit=50):
        return self.list_downloads(limit=limit)

    def list_downloads(self, limit=50, status=None, query=None):
        clauses = []
        params = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if query:
            clauses.append("(page_url LIKE ? OR media_url LIKE ? OR output_path LIKE ? OR resolver LIKE ? OR error LIKE ?)")
            pattern = "%%%s%%" % query
            params.extend([pattern, pattern, pattern, pattern, pattern])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cursor = self.conn.execute(
            """
            SELECT page_url, media_url, status, output_path, resolver, error, metadata_json, updated_at
            FROM downloads
            %s
            ORDER BY updated_at DESC
            LIMIT ?
            """
            % where,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_download(self, page_url, media_url):
        cursor = self.conn.execute(
            """
            SELECT page_url, media_url, status, output_path, resolver, error, metadata_json, updated_at
            FROM downloads
            WHERE page_url=? AND media_url=?
            """,
            (page_url, media_url),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def retry_download(self, page_url, media_url):
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE downloads
                SET status='queued', resolver='queued', error=NULL, updated_at=?
                WHERE page_url=? AND media_url=? AND status IN ('failed', 'skipped')
                """,
                (time.time(), page_url, media_url),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def skip_download(self, page_url, media_url, reason="skipped by user"):
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE downloads
                SET status='skipped', error=?, updated_at=?
                WHERE page_url=? AND media_url=? AND status IN ('queued', 'failed')
                """,
                (reason, time.time(), page_url, media_url),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def pending_downloads(self, limit=100):
        cursor = self.conn.execute(
            """
            SELECT v.page_url, v.media_url, v.kind, v.title, v.source, v.metadata_json
            FROM videos v
            LEFT JOIN downloads d ON d.page_url = v.page_url AND d.media_url = v.media_url
            WHERE d.status IS NULL OR d.status IN ('failed', 'queued')
            ORDER BY v.id LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def retry_failed_downloads(self, limit=None):
        with self._lock:
            if limit:
                cursor = self.conn.execute(
                    """
                    UPDATE downloads
                    SET status='queued', error=NULL, updated_at=?
                    WHERE id IN (
                        SELECT id FROM downloads
                        WHERE status='failed'
                        ORDER BY updated_at ASC
                        LIMIT ?
                    )
                    """,
                    (time.time(), limit),
                )
            else:
                cursor = self.conn.execute(
                    """
                    UPDATE downloads
                    SET status='queued', error=NULL, updated_at=?
                    WHERE status='failed'
                    """,
                    (time.time(),),
                )
            self.conn.commit()
            return cursor.rowcount

    def stats(self):
        pages = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        videos = self.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        challenges = self.conn.execute("SELECT COUNT(*) FROM pages WHERE challenge_detected=1").fetchone()[0]
        failures = self.conn.execute("SELECT COUNT(*) FROM pages WHERE status_code=0 OR status_code>=400").fetchone()[0]
        downloads = self.conn.execute("SELECT COUNT(*) FROM downloads WHERE status='downloaded'").fetchone()[0]
        queued_downloads = self.conn.execute("SELECT COUNT(*) FROM downloads WHERE status='queued'").fetchone()[0]
        download_failures = self.conn.execute("SELECT COUNT(*) FROM downloads WHERE status='failed'").fetchone()[0]
        active_workers = self.conn.execute("SELECT COUNT(*) FROM workers WHERE status='running'").fetchone()[0]
        paused = self.control_flag("paused", False)
        queue = self.queue_stats()
        return {
            "pages": pages,
            "videos": videos,
            "challenges": challenges,
            "failures": failures,
            "downloads": downloads,
            "queued_downloads": queued_downloads,
            "download_failures": download_failures,
            "active_workers": active_workers,
            "paused": paused,
            "queue": queue,
        }
