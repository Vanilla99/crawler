import json
import re
import time
from collections import deque
from typing import Iterable, List, Optional

from .models import CrawlRequest


class QueueBackend:
    def enqueue(self, request: CrawlRequest):
        raise NotImplementedError

    def enqueue_many(self, requests: Iterable[CrawlRequest]):
        for request in requests:
            self.enqueue(request)

    def next_batch(self, limit: int) -> List[CrawlRequest]:
        raise NotImplementedError

    def mark_done(self, request: CrawlRequest):
        raise NotImplementedError

    def mark_failed(self, request: CrawlRequest, error: str):
        raise NotImplementedError

    def mark_skipped(self, request: CrawlRequest, reason: Optional[str] = None):
        raise NotImplementedError

    def recover_stale(self, stale_after_seconds: int):
        return 0


class InMemoryQueueBackend(QueueBackend):
    def __init__(self, seeds=None):
        self._queue = deque()
        self._seen = set()
        self.enqueue_many(CrawlRequest(url=seed, depth=0) for seed in seeds or [])

    def enqueue(self, request):
        if request.url in self._seen:
            return False
        self._seen.add(request.url)
        if request.priority > 0:
            self._queue.appendleft(request)
        else:
            self._queue.append(request)
        return True

    def next_batch(self, limit):
        batch = []
        while self._queue and len(batch) < limit:
            batch.append(self._queue.popleft())
        return batch

    def mark_done(self, request):
        return None

    def mark_failed(self, request, error):
        return None

    def mark_skipped(self, request, reason=None):
        return None

    def __len__(self):
        return len(self._queue)


class SQLiteQueueBackend(QueueBackend):
    def __init__(self, store):
        self.store = store

    def enqueue(self, request):
        self.store.enqueue_request(request)
        return True

    def next_batch(self, limit):
        rows = self.store.next_queued_requests(limit=limit)
        return [
            CrawlRequest(
                url=row["url"],
                depth=row["depth"],
                priority=row["priority"],
                referer=row["referer"],
            )
            for row in rows
        ]

    def mark_done(self, request):
        self.store.mark_queue_status(request.url, "fetched")

    def mark_failed(self, request, error):
        self.store.mark_queue_status(request.url, "failed", error=error)

    def mark_skipped(self, request, reason=None):
        self.store.mark_queue_status(request.url, "skipped", error=reason)

    def recover_stale(self, stale_after_seconds):
        return self.store.recover_stale_queue(stale_after_seconds)


class RedisQueueBackend(QueueBackend):
    def __init__(self, redis_url=None, key_prefix="vcrawl", client=None):
        if client is None:
            if not redis_url:
                raise RuntimeError("Redis queue backend requires queue.redis_url")
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("Redis queue backend requires optional dependency: pip install redis") from exc
            client = redis.Redis.from_url(redis_url)
        self.client = client
        self.key_prefix = key_prefix or "vcrawl"

    @property
    def pending_key(self):
        return "%s:queue:pending" % self.key_prefix

    @property
    def seen_key(self):
        return "%s:queue:seen" % self.key_prefix

    @property
    def in_progress_key(self):
        return "%s:queue:in_progress" % self.key_prefix

    @property
    def done_key(self):
        return "%s:queue:done" % self.key_prefix

    @property
    def failed_key(self):
        return "%s:queue:failed" % self.key_prefix

    @property
    def skipped_key(self):
        return "%s:queue:skipped" % self.key_prefix

    def enqueue(self, request):
        if not self.client.sadd(self.seen_key, request.url):
            return False
        payload = _encode_request(request)
        if request.priority > 0:
            self.client.lpush(self.pending_key, payload)
        else:
            self.client.rpush(self.pending_key, payload)
        return True

    def next_batch(self, limit):
        batch = []
        for _ in range(max(0, int(limit or 0))):
            payload = self.client.lpop(self.pending_key)
            if payload is None:
                break
            request = _decode_request(payload)
            self.client.hset(
                self.in_progress_key,
                request.url,
                json.dumps(
                    {
                        "request": _request_data(request),
                        "claimed_at": time.time(),
                    },
                    sort_keys=True,
                ),
            )
            batch.append(request)
        return batch

    def mark_done(self, request):
        self.client.hdel(self.in_progress_key, request.url)
        self.client.sadd(self.done_key, request.url)

    def mark_failed(self, request, error):
        self.client.hdel(self.in_progress_key, request.url)
        self.client.hset(self.failed_key, request.url, error or "")

    def mark_skipped(self, request, reason=None):
        self.client.hdel(self.in_progress_key, request.url)
        self.client.hset(self.skipped_key, request.url, reason or "")

    def recover_stale(self, stale_after_seconds):
        if stale_after_seconds is None or stale_after_seconds <= 0:
            return 0
        cutoff = time.time() - float(stale_after_seconds)
        recovered = 0
        for url, payload in self.client.hgetall(self.in_progress_key).items():
            text = _to_text(payload)
            try:
                data = json.loads(text)
            except ValueError:
                data = {"request": {"url": _to_text(url)}, "claimed_at": 0}
            if float(data.get("claimed_at") or 0) >= cutoff:
                continue
            request = _decode_request(json.dumps(data.get("request") or {"url": _to_text(url)}))
            self.client.hdel(self.in_progress_key, request.url)
            self.client.rpush(self.pending_key, _encode_request(request))
            recovered += 1
        return recovered


class PostgresQueueBackend(QueueBackend):
    def __init__(self, postgres_dsn=None, table="vcrawl_crawl_queue", connection=None, initialize_schema=True):
        if connection is None:
            if not postgres_dsn:
                raise RuntimeError("Postgres queue backend requires queue.postgres_dsn")
            connection = _connect_postgres(postgres_dsn)
        self.connection = connection
        self.table = _quote_identifier(table or "vcrawl_crawl_queue")
        if initialize_schema:
            self._init_schema()

    def _init_schema(self):
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS {table} (
                url TEXT PRIMARY KEY,
                depth INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                referer TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """.format(table=self.table)
        )
        self._execute(
            """
            CREATE INDEX IF NOT EXISTS {index_name}
            ON {table} (status, priority DESC, created_at ASC)
            """.format(index_name=_quote_identifier(_index_name(self.table)), table=self.table)
        )
        self._commit()

    def enqueue(self, request):
        now = time.time()
        self._execute(
            """
            INSERT INTO {table}(url, depth, priority, referer, status, attempts, error, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'pending', 0, NULL, %s, %s)
            ON CONFLICT (url) DO UPDATE SET
                depth=LEAST({table}.depth, EXCLUDED.depth),
                priority=GREATEST({table}.priority, EXCLUDED.priority),
                referer=COALESCE({table}.referer, EXCLUDED.referer),
                updated_at=EXCLUDED.updated_at
            """.format(table=self.table),
            (request.url, request.depth, request.priority, request.referer, now, now),
        )
        self._commit()
        return True

    def next_batch(self, limit):
        cursor = self._execute(
            """
            WITH claimed AS (
                SELECT url
                FROM {table}
                WHERE status='pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE {table} AS queue
            SET status='in_progress',
                attempts=queue.attempts + 1,
                updated_at=%s
            FROM claimed
            WHERE queue.url=claimed.url
            RETURNING queue.url, queue.depth, queue.priority, queue.referer
            """.format(table=self.table),
            (max(0, int(limit or 0)), time.time()),
        )
        rows = cursor.fetchall()
        self._commit()
        return [
            CrawlRequest(
                url=_row_get(row, "url", 0),
                depth=int(_row_get(row, "depth", 1) or 0),
                priority=int(_row_get(row, "priority", 2) or 0),
                referer=_row_get(row, "referer", 3),
            )
            for row in rows
        ]

    def mark_done(self, request):
        self._mark_status(request.url, "fetched")

    def mark_failed(self, request, error):
        self._mark_status(request.url, "failed", error=error)

    def mark_skipped(self, request, reason=None):
        self._mark_status(request.url, "skipped", error=reason)

    def recover_stale(self, stale_after_seconds):
        if stale_after_seconds is None or stale_after_seconds <= 0:
            return 0
        cutoff = time.time() - float(stale_after_seconds)
        cursor = self._execute(
            """
            UPDATE {table}
            SET status='pending', error=NULL, updated_at=%s
            WHERE status='in_progress' AND updated_at < %s
            """.format(table=self.table),
            (time.time(), cutoff),
        )
        self._commit()
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    def _mark_status(self, url, status, error=None):
        self._execute(
            """
            UPDATE {table}
            SET status=%s, error=%s, updated_at=%s
            WHERE url=%s
            """.format(table=self.table),
            (status, error, time.time(), url),
        )
        self._commit()

    def _execute(self, sql, params=()):
        if hasattr(self.connection, "execute"):
            return self.connection.execute(sql, params)
        cursor = self.connection.cursor()
        cursor.execute(sql, params)
        return cursor

    def _commit(self):
        if hasattr(self.connection, "commit"):
            self.connection.commit()


def make_queue_backend(config, store=None, client=None):
    backend = (config.queue.backend or "sqlite").lower()
    if backend == "sqlite":
        if store is None:
            raise RuntimeError("SQLite queue backend requires a SQLiteStore")
        return SQLiteQueueBackend(store)
    if backend == "redis":
        return RedisQueueBackend(
            redis_url=config.queue.redis_url,
            key_prefix=config.queue.redis_key_prefix,
            client=client,
        )
    if backend in ("postgres", "postgresql", "pg"):
        return PostgresQueueBackend(
            postgres_dsn=config.queue.postgres_dsn,
            table=config.queue.postgres_table,
            connection=client,
        )
    raise RuntimeError("Unsupported queue backend: %s" % config.queue.backend)


def _request_data(request):
    return {
        "url": request.url,
        "depth": request.depth,
        "priority": request.priority,
        "referer": request.referer,
    }


def _encode_request(request):
    return json.dumps(_request_data(request), sort_keys=True)


def _decode_request(payload):
    data = json.loads(_to_text(payload))
    return CrawlRequest(
        url=data["url"],
        depth=int(data.get("depth") or 0),
        priority=int(data.get("priority") or 0),
        referer=data.get("referer"),
    )


def _to_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _connect_postgres(postgres_dsn):
    try:
        import psycopg

        return psycopg.connect(postgres_dsn)
    except ImportError:
        try:
            import psycopg2

            return psycopg2.connect(postgres_dsn)
        except ImportError as exc:
            raise RuntimeError(
                "Postgres queue backend requires optional dependency: pip install 'vcrawl[postgres]'"
            ) from exc


def _quote_identifier(identifier):
    parts = str(identifier).split(".")
    if not parts or any(not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part or "") for part in parts):
        raise RuntimeError("Invalid Postgres identifier: %s" % identifier)
    return ".".join('"%s"' % part for part in parts)


def _index_name(quoted_table):
    return "idx_%s_status_priority_created" % re.sub(r"[^A-Za-z0-9_]+", "_", quoted_table).strip("_")


def _row_get(row, name, index):
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (TypeError, KeyError, IndexError):
        return row[index]
