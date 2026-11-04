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
