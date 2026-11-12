"""Request-local ``Server-Timing`` entries.

The HTTP middleware opens a recorder per request; dependencies add named
durations (auth, profile) and the middleware appends the total ``app`` time.
The header carries durations and fixed descriptions only, never ids or data.
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

_current: ContextVar["ServerTimings | None"] = ContextVar("server_timings", default=None)
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass
class ServerTimings:
    entries: list[tuple[str, float, str | None]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, name: str, duration_ms: float, desc: str | None = None) -> None:
        if not _SAFE_TOKEN.match(name) or (desc is not None and not _SAFE_TOKEN.match(desc)):
            return
        with self._lock:
            self.entries.append((name, duration_ms, desc))

    def header_value(self) -> str:
        with self._lock:
            parts = []
            for name, duration_ms, desc in self.entries:
                part = f"{name};dur={duration_ms:.1f}"
                if desc:
                    part += f';desc="{desc}"'
                parts.append(part)
            return ", ".join(parts)


def begin() -> tuple[ServerTimings, object]:
    timings = ServerTimings()
    return timings, _current.set(timings)


def end(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]


# Caddy stamps ``X-Request-Start: t=<unix ms>`` as it hands the request to the
# app (it overwrites any client value). The gap to the app's own start is time
# spent between the proxy and the first middleware: the upstream connection,
# uvicorn parsing, and a busy event loop. Proxy and app share one host clock.
_REQUEST_START = re.compile(r"^t=(\d{10,16})$")
_MAX_QUEUE_MS = 60_000.0


def proxy_queue_ms(header: str | None, now_ms: float | None = None) -> float | None:
    """Milliseconds from the proxy's stamp to now, or None when absent or implausible."""
    match = _REQUEST_START.match((header or "").strip())
    if not match:
        return None
    queued = (time.time() * 1000 if now_ms is None else now_ms) - int(match.group(1))
    # A skewed or forged stamp is dropped rather than reported.
    return queued if 0 <= queued <= _MAX_QUEUE_MS else None


def record(name: str, duration_ms: float, desc: str | None = None) -> None:
    timings = _current.get()
    if timings is not None:
        timings.add(name, duration_ms, desc)


@contextmanager
def measure(name: str, desc: str | None = None) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        record(name, (time.perf_counter() - started) * 1000, desc)
