import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class Event:
    type: str
    message: str = ""
    url: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class EventBus:
    def __init__(self, sinks=None):
        self._sinks: List[Callable[[Event], None]] = list(sinks or [])
        self._lock = Lock()

    def subscribe(self, sink):
        with self._lock:
            self._sinks.append(sink)

    def emit(self, event_type, message="", url=None, **data):
        event = Event(type=event_type, message=message, url=url, data=data)
        with self._lock:
            sinks = list(self._sinks)
        for sink in sinks:
            sink(event)
        return event


class MemoryEventSink:
    def __init__(self, limit=500):
        self.limit = limit
        self.events: List[Event] = []
        self._lock = Lock()

    def __call__(self, event):
        with self._lock:
            self.events.append(event)
            if len(self.events) > self.limit:
                self.events = self.events[-self.limit :]

    def list_events(self):
        with self._lock:
            return list(self.events)


def callback_sink(callback):
    if callback is None:
        return None

    def sink(event):
        callback(event.type, event.message or event.url or "")

    return sink
