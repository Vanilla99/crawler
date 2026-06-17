import time
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from .discovery import expand_discovery_seeds
from .events import EventBus
from .extractors import VideoExtractor
from .fetchers import BrowserFetcher, HttpFetcher
from .frontier import Frontier
from .models import CrawlRequest, CrawlStats
from .pipelines import PipelineRunner
from .queue_backends import make_queue_backend
from .robots import RobotsPolicy
from .scope import domain_of, in_scope
from .stages import CrawlStageRunner


class CrawlEngine:
    def __init__(
        self,
        config,
        store=None,
        fetcher=None,
        extractor=None,
        event_bus=None,
        pipeline_runner=None,
        stage_runner=None,
        should_stop=None,
    ):
        self.config = config
        self.store = store
        self.fetcher = fetcher or self._make_fetcher()
        self.extractor = extractor or VideoExtractor(
            plugin_paths=self.config.extract.plugin_paths,
            builtin_plugins=self.config.extract.builtin_plugins,
            plugin_configs=self.config.extract.plugin_configs,
        )
        self.event_bus = event_bus or EventBus()
        self.pipeline_runner = pipeline_runner or PipelineRunner()
        self.should_stop = should_stop or (lambda: False)
        self.stage_runner = stage_runner or CrawlStageRunner(
            config,
            extractor=self.extractor,
            pipeline_runner=self.pipeline_runner,
            store=self.store,
            event_bus=self.event_bus,
        )
        self.stats = CrawlStats()
        self._last_fetch_by_domain = defaultdict(float)
        self._politeness_lock = Lock()
        self.robots = RobotsPolicy(
            user_agent=self.config.fetch.user_agent,
            enabled=self.config.scope.respect_robots,
        )

    def _make_fetcher(self):
        if self.config.fetch.default == "browser":
            return BrowserFetcher(
                timeout_seconds=self.config.fetch.timeout_seconds,
                headless=self.config.fetch.browser_headless,
                profile=self.config.fetch.browser_profile,
            )
        return HttpFetcher(
            timeout_seconds=self.config.fetch.timeout_seconds,
            user_agent=self.config.fetch.user_agent,
            proxy_url=self.config.network.proxy_url,
            headers=self.config.network.headers,
            cookies_file=self.config.network.cookies_file,
            http_cache=self.config.network.http_cache,
            cache_dir=self._http_cache_dir(),
            session_pool=self.config.network.session_pool,
            session_pool_size=self.config.network.session_pool_size,
        )

    def _http_cache_dir(self):
        if not self.config.network.http_cache:
            return None
        if self.config.network.http_cache_dir:
            return self.config.network.http_cache_dir
        if self.config.storage.state:
            return os.path.join(os.path.dirname(self.config.storage.state), "http-cache")
        return os.path.join(".vcrawl", "http-cache")

    def crawl(self, max_pages=100, on_event=None, resume=False):
        if resume:
            if not self.store:
                raise RuntimeError("resume mode requires a SQLiteStore")
            return self._crawl_persistent(max_pages=max_pages, on_event=on_event)
        seeds = expand_discovery_seeds(self.config)
        frontier = Frontier(seeds)
        run_id = self.store.start_run(note="memory") if self.store else None
        run_status = "completed"
        self._emit(on_event, "run_started", "memory crawl started")
        try:
            status = self._crawl_frontier(frontier, max_pages=max_pages, on_event=on_event)
            if status:
                run_status = status
        except Exception:
            run_status = "failed"
            self._emit(on_event, "run_failed", "memory crawl failed")
            raise
        finally:
            if self.store and run_id:
                self.store.finish_run(run_id, run_status, _stats_dict(self.stats))
            self._emit(on_event, "run_finished", run_status)
        return self.stats

    def _crawl_frontier(self, frontier, max_pages=100, on_event=None):
        while len(frontier) and self.stats.fetched < max_pages:
            if self.should_stop():
                self._emit(on_event, "run_paused", "crawl paused")
                return "paused"
            batch = self._next_batch(frontier, max_pages)
            if not batch:
                break
            results = self._fetch_batch(batch, on_event)
            for request, result in results:
                self._handle_fetch_result(frontier, request, result, on_event)
        return None

    def _next_batch(self, frontier, max_pages):
        batch = []
        concurrency = max(1, int(self.config.fetch.concurrency or 1))
        while len(frontier) and len(batch) < concurrency and self.stats.fetched + len(batch) < max_pages:
            request = frontier.pop()
            if request is None:
                break
            if request.depth > self.config.scope.max_depth or not in_scope(request.url, self.config.scope):
                self.stats.skipped += 1
                continue
            if self._domain_blocked(request.url):
                self.stats.skipped += 1
                continue
            if not self.robots.allowed(request.url):
                self.stats.skipped += 1
                continue
            batch.append(request)
        return batch

    def _fetch_batch(self, batch, on_event=None):
        if len(batch) == 1:
            request = batch[0]
            return [(request, self._fetch_request(request, on_event))]
        results = []
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {executor.submit(self._fetch_request, request, on_event): request for request in batch}
            for future in as_completed(futures):
                request = futures[future]
                results.append((request, future.result()))
        return results

    def _fetch_request(self, request, on_event=None):
        self._emit(on_event, "fetch", request.url, url=request.url)
        attempts = max(0, int(self.config.fetch.retries or 0)) + 1
        last_result = None
        for attempt in range(1, attempts + 1):
            self._polite_wait(request.url)
            started = time.monotonic()
            result = self.fetcher.fetch(request.url)
            result.duration_ms = (time.monotonic() - started) * 1000.0
            self._record_fetch_policy_result(request.url, result, on_event=on_event)
            last_result = result
            if not _should_retry(result):
                return result
            if attempt < attempts:
                self._emit(
                    on_event,
                    "retry",
                    "%s attempt=%s status=%s" % (request.url, attempt, result.status_code),
                    url=request.url,
                    attempt=attempt,
                    status_code=result.status_code,
                )
            time.sleep(float(self.config.fetch.retry_backoff_seconds) * attempt)
        return last_result

    def _handle_fetch_result(self, frontier, request, result, on_event=None):
        outcome = self.stage_runner.process(request, result, on_event=on_event)
        self._apply_stage_outcome(outcome)
        if not result.error and outcome.extracted:
            for link in outcome.links:
                if request.depth + 1 <= self.config.scope.max_depth and in_scope(link, self.config.scope):
                    frontier.add(link, depth=request.depth + 1, referer=outcome.extracted.url)

    def _crawl_persistent(self, max_pages=100, on_event=None):
        seeds = expand_discovery_seeds(self.config)
        queue = make_queue_backend(self.config, store=self.store)
        for seed in seeds:
            queue.enqueue(CrawlRequest(url=seed, depth=0))
        recovered = queue.recover_stale(self.config.queue.stale_after_seconds)
        if recovered:
            self._emit(on_event, "queue_recovered", "recovered %s stale requests" % recovered, recovered=recovered)
        run_id = self.store.start_run(note="resume")
        run_status = "completed"
        self._emit(on_event, "run_started", "resume crawl started")
        try:
            while self.stats.fetched < max_pages:
                if self.should_stop():
                    run_status = "paused"
                    self._emit(on_event, "run_paused", "crawl paused")
                    break
                batch = self._next_persistent_batch(queue, max_pages=max_pages)
                if not batch:
                    break
                results = self._fetch_batch(batch, on_event)
                for request, result in results:
                    self._handle_persistent_result(queue, request, result, on_event)
        except Exception:
            run_status = "failed"
            self._emit(on_event, "run_failed", "resume crawl failed")
            raise
        finally:
            self.store.finish_run(run_id, run_status, _stats_dict(self.stats))
            self._emit(on_event, "run_finished", run_status)
        return self.stats

    def _next_persistent_batch(self, queue, max_pages):
        concurrency = max(1, int(self.config.fetch.concurrency or 1))
        remaining = max(0, max_pages - self.stats.fetched)
        rows = queue.next_batch(limit=min(concurrency, remaining))
        requests = []
        for request in rows:
            if request.depth > self.config.scope.max_depth or not in_scope(request.url, self.config.scope):
                self.stats.skipped += 1
                queue.mark_skipped(request)
                continue
            if self._domain_blocked(request.url):
                self.stats.skipped += 1
                queue.mark_skipped(request, reason="domain failure threshold reached")
                continue
            if not self.robots.allowed(request.url):
                self.stats.skipped += 1
                queue.mark_skipped(request, reason="blocked by robots.txt")
                continue
            requests.append(request)
        return requests

    def _handle_persistent_result(self, queue, request, result, on_event=None):
        outcome = self.stage_runner.process(request, result, on_event=on_event)
        self._apply_stage_outcome(outcome)
        if result.error:
            queue.mark_failed(request, result.error)
        else:
            for link in outcome.links:
                if request.depth + 1 <= self.config.scope.max_depth and in_scope(link, self.config.scope):
                    queue.enqueue(
                        CrawlRequest(url=link, depth=request.depth + 1, referer=outcome.extracted.url)
                    )
            queue.mark_done(request)

    def inspect(self, url):
        result = self.fetcher.fetch(url)
        extracted = self.extractor.extract(result) if not result.error else None
        return result, extracted

    def _polite_wait(self, url):
        domain = domain_of(url)
        delay = self._domain_delay(domain)
        if delay <= 0:
            return
        with self._politeness_lock:
            elapsed = time.time() - self._last_fetch_by_domain[domain]
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last_fetch_by_domain[domain] = time.time()

    def _domain_delay(self, domain):
        base = self._configured_domain_delay(domain)
        if not self.store or not getattr(self.config.fetch, "auto_throttle", False):
            return base
        state = self.store.get_domain_state(domain)
        if not state or state.get("dynamic_delay_seconds") is None:
            return max(base, self._auto_throttle_min_delay())
        minimum = max(base, self._auto_throttle_min_delay())
        return _clamp_delay(
            float(state["dynamic_delay_seconds"]),
            minimum,
            max(minimum, self._auto_throttle_max_delay()),
        )

    def _configured_domain_delay(self, domain):
        return max(
            0.0,
            float(self.config.fetch.per_domain_delay_seconds.get(domain, self.config.fetch.delay_per_domain_seconds)),
        )

    def _domain_blocked(self, url):
        if not self.store:
            return False
        domain = domain_of(url)
        threshold = self._domain_failure_threshold(domain)
        if threshold <= 0:
            return False
        state = self.store.get_domain_state(domain)
        blocked = bool(state and int(state["consecutive_failures"]) >= threshold)
        if blocked:
            self._emit(
                None,
                "domain_blocked",
                "%s consecutive_failures=%s threshold=%s" % (domain, state["consecutive_failures"], threshold),
                url=url,
                domain=domain,
                threshold=threshold,
            )
        return blocked

    def _domain_failure_threshold(self, domain):
        thresholds = getattr(self.config.fetch, "per_domain_failure_thresholds", {}) or {}
        if domain in thresholds:
            return int(thresholds[domain] or 0)
        return int(self.config.fetch.domain_failure_threshold or 0)

    def _record_fetch_policy_result(self, url, result, on_event=None):
        if not self.store:
            return
        domain = domain_of(result.final_url or url)
        failed = bool(result.error or result.status_code == 0 or result.status_code >= 400 or result.challenge_detected)
        dynamic_delay = self._auto_throttle_delay(domain, result, failed)
        self.store.record_domain_result(
            domain,
            result.status_code,
            error=result.error,
            challenge_detected=result.challenge_detected,
            latency_ms=result.duration_ms,
            dynamic_delay_seconds=dynamic_delay,
        )
        if dynamic_delay is not None:
            self._emit(
                on_event,
                "policy_autothrottle",
                "%s auto_throttle delay=%.3fs latency=%.1fms" % (
                    result.final_url or url,
                    dynamic_delay,
                    result.duration_ms or 0.0,
                ),
                url=result.final_url or url,
                domain=domain,
                dynamic_delay_seconds=dynamic_delay,
                latency_ms=result.duration_ms or 0.0,
            )
        self.store.record_session_health(
            result.session_id or "default-%s" % result.fetcher,
            kind=result.fetcher,
            status="degraded" if failed else "healthy",
            domain=domain,
            success=not failed,
            metadata={
                "proxy_configured": bool(self.config.network.proxy_url or self.config.network.proxy_urls),
                "cookies_file": self.config.network.cookies_file or "",
                "browser_profile": self.config.fetch.browser_profile or "",
                "http_cache": self.config.network.http_cache,
                "session_pool": self.config.network.session_pool,
                "session_pool_size": self.config.network.session_pool_size,
                "auto_throttle": self.config.fetch.auto_throttle,
            },
        )

    def _auto_throttle_delay(self, domain, result, failed):
        if not getattr(self.config.fetch, "auto_throttle", False):
            return None
        latency_ms = result.duration_ms
        if latency_ms is None:
            return None
        base = self._configured_domain_delay(domain)
        minimum = max(base, self._auto_throttle_min_delay())
        maximum = max(minimum, self._auto_throttle_max_delay())
        current = base
        state = self.store.get_domain_state(domain) if self.store else None
        if state and state.get("dynamic_delay_seconds") is not None:
            current = float(state["dynamic_delay_seconds"])
        target_concurrency = max(0.1, float(self.config.fetch.auto_throttle_target_concurrency or 1.0))
        latency_delay = max(0.0, float(latency_ms) / 1000.0) / target_concurrency
        if failed:
            fallback = current * 2 if current > 0 else max(minimum, latency_delay, 1.0)
            target = max(current, latency_delay, fallback)
        else:
            target = (current + latency_delay) / 2.0
        return _clamp_delay(target, minimum, maximum)

    def _auto_throttle_min_delay(self):
        return max(0.0, float(getattr(self.config.fetch, "auto_throttle_min_delay_seconds", 0.0) or 0.0))

    def _auto_throttle_max_delay(self):
        minimum = self._auto_throttle_min_delay()
        configured = float(getattr(self.config.fetch, "auto_throttle_max_delay_seconds", 30.0) or 30.0)
        return max(minimum, configured)

    def _apply_stage_outcome(self, outcome):
        self.stats.fetched += outcome.fetched
        self.stats.failed += outcome.failed
        self.stats.challenge_pages += outcome.challenge_pages
        self.stats.videos += len(outcome.videos)
        self.stats.download_queued += outcome.download_queued
        self.stats.downloaded += outcome.downloaded
        self.stats.download_failed += outcome.download_failed

    def _emit(self, on_event, event_type, message="", url=None, **data):
        self.event_bus.emit(event_type, message=message, url=url, **data)
        if on_event:
            on_event(event_type, message or url or "")


def _should_retry(result):
    if result.challenge_detected:
        return False
    return bool(result.error or result.status_code == 0 or result.status_code in (408, 429) or result.status_code >= 500)


def _stats_dict(stats):
    return {
        "fetched": stats.fetched,
        "failed": stats.failed,
        "skipped": stats.skipped,
        "challenge_pages": stats.challenge_pages,
        "videos": stats.videos,
        "download_queued": stats.download_queued,
        "downloaded": stats.downloaded,
        "download_failed": stats.download_failed,
    }


def _clamp_delay(value, minimum, maximum):
    return min(max(float(value), float(minimum)), float(maximum))
