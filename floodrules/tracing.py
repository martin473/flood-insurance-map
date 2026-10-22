"""Request tracing without a tracing backend.

Each API request gets a trace: a span for the request and a child span for every call to a
data source (FEMA, URI, USFWS, Nominatim), with whether it was a cache hit, how many attempts
it took and how it ended. When the request finishes, the whole trace is written as one JSON log
line and kept in a small in-memory ring for the status page.

Ids follow the W3C Trace Context format, so a `traceparent` header from a caller is continued and
the response carries one back. Swapping in the OpenTelemetry SDK later would keep the same ids.
"""

from __future__ import annotations

import contextvars
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

RECENT_TRACES = 50


@dataclass
class Span:
    name: str
    span_id: str
    parent_id: str | None
    start: float                     # seconds since the trace started
    ms: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "span_id": self.span_id, "parent_id": self.parent_id,
                             "start_ms": round(self.start * 1000, 1), "ms": self.ms, **({"attrs": self.attrs} if self.attrs else {})}
        if self.error:
            d["error"] = self.error
        return d


class Trace:
    def __init__(self, name: str, traceparent: str | None = None, request_id: str | None = None):
        parsed = parse_traceparent(traceparent)
        self.trace_id = parsed[0] if parsed else secrets.token_hex(16)
        remote_parent = parsed[1] if parsed else None
        self.request_id = request_id
        self._t0 = time.monotonic()
        self.wall_start = time.time()
        self._lock = threading.Lock()
        self.spans: list[Span] = []
        self.root = self._new(name, remote_parent)

    def _new(self, name: str, parent_id: str | None) -> Span:
        s = Span(name=name, span_id=secrets.token_hex(8), parent_id=parent_id, start=time.monotonic() - self._t0)
        with self._lock:
            self.spans.append(s)
        return s

    def end(self, span: Span) -> None:
        span.ms = round((time.monotonic() - self._t0 - span.start) * 1000, 1)

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.root.span_id}-01"

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            spans = [s.as_dict() for s in self.spans]
        return {"trace_id": self.trace_id, "request_id": self.request_id, "at": round(self.wall_start, 3),
                "name": self.root.name, "ms": self.root.ms, "attrs": self.root.attrs, "spans": spans}


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """(trace_id, parent span id) from a W3C traceparent header, or None if it's absent or malformed."""
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    try:
        int(parts[1], 16), int(parts[2], 16)
    except ValueError:
        return None
    if set(parts[1]) == {"0"} or set(parts[2]) == {"0"}:
        return None
    return parts[1], parts[2]


_current: contextvars.ContextVar[tuple[Trace, Span] | None] = contextvars.ContextVar("trace", default=None)
_recent: deque[dict[str, Any]] = deque(maxlen=RECENT_TRACES)
_recent_lock = threading.Lock()


@contextmanager
def use(trace: Trace | None, parent: Span | None = None) -> Iterator[None]:
    """Make `trace` current in this thread (worker threads don't inherit it on their own)."""
    if trace is None:
        yield
        return
    token = _current.set((trace, parent or trace.root))
    try:
        yield
    finally:
        _current.reset(token)


def current() -> tuple[Trace, Span] | None:
    return _current.get()


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Span | None]:
    """A child span of whatever span is current. A no-op outside a trace (CLI, tests)."""
    cur = _current.get()
    if cur is None:
        yield None
        return
    trace, parent = cur
    s = trace._new(name, parent.span_id)
    s.attrs.update(attrs)
    token = _current.set((trace, s))
    try:
        yield s
    except Exception as e:
        s.error = f"{type(e).__name__}: {e}"[:200]
        raise
    finally:
        _current.reset(token)
        trace.end(s)


def finish(trace: Trace) -> dict[str, Any]:
    """End the root span and return the trace. Traces with child spans (requests that called a data
    source) are kept for the status page; the page's own polling isn't."""
    trace.end(trace.root)
    d = trace.as_dict()
    if len(d["spans"]) > 1:
        with _recent_lock:
            _recent.append(d)
    return d


def recent(limit: int = 20, path: str | None = None) -> list[dict[str, Any]]:
    with _recent_lock:
        items = list(_recent)
    if path:
        items = [t for t in items if t["attrs"].get("path") == path]
    return list(reversed(items))[:limit]


def clear() -> None:
    with _recent_lock:
        _recent.clear()
