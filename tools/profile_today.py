"""Read-only profile of one athlete's Today build against the live store.

Run inside the API container (it reads the same environment as the API):

    python tools/profile_today.py --athlete <uuid> --now 2026-11-12T19:04:00Z

Prints timings, function names, store-call counts and JSON field sizes only,
never athlete content. It cannot change production data: every HTTP request
other than GET/HEAD or a verified read-only RPC is refused before it is sent
(whatever code path built it), and non-read store methods are refused too.
Both are reported. Works on
code from before #3260 as well, so the same instant and data can be compared
across versions.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

READ_PREFIXES = ("get_", "list_", "count_", "fetch_", "is_", "has_")
# Reads whose names do not start with a read prefix.
READ_METHODS = {"latest_sparring_log_day", "xp_award_exists", "private_media_worker_healthy", "is_admin_email"}
REPO_MARKERS = ("/api/", "/fightcamp/")


# Read-only RPCs Today may call. Each was checked in the production catalogue:
# STABLE functions cannot write, and pending_delayed_rehab is a single SELECT
# (volatile only because it does not declare otherwise).
READ_ONLY_RPCS = {
    "pending_delayed_rehab",
    "get_generation_job_status_v2",
    "get_latest_generation_job_status_v2",
    "get_visible_active_generation_job_status_v2",
}


def install_http_read_only_guard() -> Counter:
    """Refuse, before it is sent, every HTTP request that could write.

    Installed on ``httpx.Client.send`` so it covers every client in the
    process, whatever code path built the request: only GET/HEAD, and POSTs to
    the read-only RPCs above, leave the process. Returns refusals by request.
    """
    import httpx

    refused: Counter = Counter()
    original = httpx.Client.send

    def guarded(self, request, *args, **kwargs):
        method = request.method.upper()
        path = request.url.path
        rpc = path.rsplit("/rpc/", 1)[1] if "/rpc/" in path else None
        if method in {"GET", "HEAD"} or (method == "POST" and rpc in READ_ONLY_RPCS):
            return original(self, request, *args, **kwargs)
        label = f"{method} {'rpc/' + rpc if rpc else path.rsplit('/', 1)[-1]}"
        refused[label] += 1
        raise RuntimeError(f"profile_today is read-only: refused {label}")

    httpx.Client.send = guarded
    return refused


class ReadOnlyTimedStore:
    """Delegates reads, refuses writes, and times every store call."""

    def __init__(self, store):
        self._store = store
        self.calls: Counter = Counter()
        self.ms: defaultdict = defaultdict(float)
        self.main_thread_calls: Counter = Counter()
        self.refused: Counter = Counter()
        self._lock = threading.Lock()

    def __getattr__(self, name):
        attr = getattr(self._store, name)
        if not callable(attr) or name.startswith("__"):
            return attr
        if not name.startswith(READ_PREFIXES) and name not in READ_METHODS:
            def refused(*args, **kwargs):
                with self._lock:
                    self.refused[name] += 1
                raise RuntimeError(f"profile_today is read-only: refused {name}")
            return refused

        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return attr(*args, **kwargs)
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                with self._lock:
                    self.calls[name] += 1
                    self.ms[name] += elapsed
                    if threading.current_thread() is threading.main_thread():
                        self.main_thread_calls[name] += 1
        return timed

    def reset(self):
        self.calls.clear(); self.ms.clear(); self.main_thread_calls.clear()


def _sizes(value, depth=0, max_depth=2):
    """Byte size of each JSON field (and list lengths), never the values."""
    encoded = len(json.dumps(value, separators=(",", ":"), default=str))
    if depth >= max_depth:
        return {"bytes": encoded, **({"items": len(value)} if isinstance(value, list) else {})}
    if isinstance(value, dict):
        children = {key: _sizes(child, depth + 1, max_depth) for key, child in value.items()}
        return {"bytes": encoded, "fields": dict(sorted(children.items(), key=lambda kv: -kv[1]["bytes"]))}
    if isinstance(value, list):
        out = {"bytes": encoded, "items": len(value)}
        if value and isinstance(value[0], dict):
            out["first_item"] = _sizes(value[0], depth + 1, max_depth)
        return out
    return {"bytes": encoded}


def _stage_timings(timings) -> dict:
    out = {}
    for name, duration, _ in getattr(timings, "entries", []):
        out[name] = round(out.get(name, 0) + duration, 1)
    return out


def _profile_rows(profiler: cProfile.Profile, sort: str, limit: int) -> list[str]:
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats(sort)
    rows = []
    for func, (cc, nc, tt, ct, _) in sorted(stats.stats.items(), key=lambda kv: -(kv[1][3] if sort == "cumulative" else kv[1][2])):
        filename, line, name = func
        if sort == "cumulative" and not any(marker in filename for marker in REPO_MARKERS):
            continue
        short = filename.split("/app/")[-1] if "/app/" in filename else filename.rsplit("/", 2)[-1]
        rows.append(f"{ct*1000:8.1f}ms cum {tt*1000:8.1f}ms own {nc:>7} calls  {short}:{line}({name})")
        if len(rows) >= limit:
            break
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--athlete", required=True)
    parser.add_argument("--now", required=True, help="ISO instant, e.g. 2026-11-12T19:04:00Z")
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument("--label", default=os.getenv("PROFILE_LABEL", ""))
    args = parser.parse_args()

    http_refused = install_http_read_only_guard()
    import_started = time.perf_counter()
    from api import server_timing
    from api.services.today_readiness_boundary import build_today_command_view
    from api.store import SupabaseAppStore
    import_ms = (time.perf_counter() - import_started) * 1000

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    raw_store = SupabaseAppStore.from_env()
    profile_row = raw_store.get_profile(args.athlete) or {}
    timezone_name = profile_row.get("athlete_timezone")
    store = ReadOnlyTimedStore(raw_store)

    def build():
        store.reset()
        timings, token = server_timing.begin()
        started = time.perf_counter()
        try:
            view = build_today_command_view(store, athlete_id=args.athlete, athlete_timezone=timezone_name, now=now)
        finally:
            server_timing.end(token)
        return view, (time.perf_counter() - started) * 1000, _stage_timings(timings)

    cold_view, cold_ms, cold_stages = build()
    cold_calls = dict(store.calls)

    warm = [build() for _ in range(max(args.warm_runs, 1))]
    view = warm[-1][0]

    profiler = cProfile.Profile()
    profiler.enable()
    build()
    profiler.disable()
    store_calls = {name: {"calls": store.calls[name], "ms": round(store.ms[name], 1),
                          "sequential": store.main_thread_calls[name]} for name in sorted(store.calls)}

    payload = view.model_dump(mode="json")
    response_json = view.model_dump_json()
    cold_json = cold_view.model_dump_json()
    report = {
        "label": args.label,
        "now": args.now,
        "athlete_timezone_set": bool(timezone_name),
        "import_ms": round(import_ms),
        "cold": {"total_ms": round(cold_ms), "stages": cold_stages, "store_calls": sum(cold_calls.values())},
        "warm": {
            "total_ms": [round(ms) for _, ms, _ in warm],
            "median_total_ms": round(statistics.median(ms for _, ms, _ in warm)),
            "median_stages": {stage: round(statistics.median(s.get(stage, 0) for _, _, s in warm), 1)
                              for stage in sorted({k for _, _, s in warm for k in s})},
        },
        "store_calls_one_warm_build": store_calls,
        "refused_writes": dict(store.refused),
        "refused_http_requests": dict(http_refused),
        "response_bytes": len(response_json),
        "cold_and_warm_responses_identical": cold_json == response_json,
        "response_fields": _sizes(payload)["fields"],
        "profile_top_cumulative_repo": _profile_rows(profiler, "cumulative", 40),
        "profile_top_own_time": _profile_rows(profiler, "tottime", 25),
    }
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
