import time

from .engine import CrawlEngine
from .storage import SQLiteStore


def run_schedule(config, max_pages=100, interval_seconds=None, max_runs=None, on_event=None):
    interval = config.schedule.interval_seconds if interval_seconds is None else interval_seconds
    runs = config.schedule.max_runs if max_runs is None else max_runs
    runs = max(1, int(runs or 1))
    results = []
    for run_number in range(1, runs + 1):
        if on_event:
            on_event("schedule", "run %s/%s" % (run_number, runs))
        store = SQLiteStore(config.storage.state)
        try:
            stats = CrawlEngine(config, store=store).crawl(max_pages=max_pages, on_event=on_event)
            results.append(stats)
        finally:
            store.close()
        if run_number < runs and interval > 0:
            time.sleep(interval)
    return results
