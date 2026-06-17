from collections import deque

from .models import CrawlRequest
from .scope import canonicalize_url


class Frontier:
    def __init__(self, seeds=None):
        self._queue = deque()
        self._seen = set()
        for seed in seeds or []:
            self.add(seed, depth=0)

    def add(self, url, depth=0, priority=0, referer=None):
        canonical = canonicalize_url(url)
        if canonical in self._seen:
            return False
        self._seen.add(canonical)
        request = CrawlRequest(url=canonical, depth=depth, priority=priority, referer=referer)
        if priority > 0:
            self._queue.appendleft(request)
        else:
            self._queue.append(request)
        return True

    def pop(self):
        if not self._queue:
            return None
        return self._queue.popleft()

    def __len__(self):
        return len(self._queue)

    @property
    def seen_count(self):
        return len(self._seen)
