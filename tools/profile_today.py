"""Read-only profile of one athlete's Today build, calendar or History, live store.

Run inside the API container (it reads the same environment as the API):

    python tools/profile_today.py --athlete <uuid> --now 2026-11-12T19:04:00Z
    python tools/profile_today.py --athlete <uuid> --target calendar
    python tools/profile_today.py --athlete <uuid> --target history

Calendar and History are served through the real app, signed in as the athlete
without any credential; the app's startup tasks never run.

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
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

READ_PREFIXES = ("get_", "list_", "count_", "fetch_", "is_", "has_")
# Reads whose names do not start with a read prefix.
READ_METHODS = {"latest_sparring_log_day", "xp_award_exists", "private_media_worker_healthy", "is_admin_email",
                # Every signed-in request: reads the existing profile, and would
                # only create one if missing, a write the HTTP guard refuses.
                "ensure_profile"}
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
        self.timeline: list[tuple[float, float, str]] = []
        self.t0 = time.perf_counter()
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
                    self.timeline.append(((started - self.t0) * 1000, elapsed, name))
                    if threading.current_thread() is threading.main_thread():
                        self.main_thread_calls[name] += 1
        return timed

    def reset(self):
        self.calls.clear(); self.ms.clear(); self.main_thread_calls.clear()
        self.timeline.clear(); self.t0 = time.perf_counter()

    def timeline_rows(self) -> list[str]:
        """Each store call in start order: offset, duration, method (no arguments)."""
        return [f"+{start:7.1f}ms {duration:7.1f}ms  {name}" for start, duration, name in sorted(self.timeline)]


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
    stats = profiler if isinstance(profiler, pstats.Stats) else pstats.Stats(profiler, stream=io.StringIO())
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


class _ThreadProfiles:
    """cProfile for request handlers, which run on worker threads.

    Python 3.12+ has one process-wide profiler that already sees every thread
    (a second one raises), so a single profile is enabled around the request.
    Python 3.11 (the production image) profiles per thread, so each handler and
    pool task gets its own profile, merged afterwards.
    """

    PROCESS_WIDE = sys.version_info >= (3, 12)

    def __init__(self):
        self.enabled = False
        self.profiles: list[cProfile.Profile] = []
        self._lock = threading.Lock()
        self._process_profile: cProfile.Profile | None = None

    def start(self):
        if self.PROCESS_WIDE:
            self._process_profile = cProfile.Profile()
            self._process_profile.enable()
        self.enabled = True

    def stop(self):
        self.enabled = False
        if self._process_profile is not None:
            self._process_profile.disable()
            self.profiles.append(self._process_profile)
            self._process_profile = None

    def wrap(self, fn):
        def run(*args, **kwargs):
            if not self.enabled or self.PROCESS_WIDE:
                return fn(*args, **kwargs)
            profile = cProfile.Profile()
            profile.enable()
            try:
                return fn(*args, **kwargs)
            finally:
                profile.disable()
                with self._lock:
                    self.profiles.append(profile)
        return run

    def install(self):
        import fastapi.dependencies.utils as dependency_utils
        import fastapi.routing as routing
        from api.services import today_reads

        for module in (routing, dependency_utils):
            original = module.run_in_threadpool

            async def profiled(func, *args, _original=original, **kwargs):
                return await _original(self.wrap(func), *args, **kwargs)

            module.run_in_threadpool = profiled
        pool = getattr(today_reads, "_PREFETCH_POOL", None)
        if pool is not None:
            submit = pool.submit
            pool.submit = lambda fn, *args, **kwargs: submit(self.wrap(fn), *args, **kwargs)

    def merged(self) -> pstats.Stats | None:
        if not self.profiles:
            return None
        stats = pstats.Stats(self.profiles[0], stream=io.StringIO())
        for profile in self.profiles[1:]:
            stats.add(profile)
        return stats


def profile_route(args, raw_store, store, http_refused, import_ms) -> dict:
    """Serve one GET through the real app, signed in as the athlete; never runs startup tasks."""
    profiles = _ThreadProfiles()
    profiles.install()  # before the app is built, so every handler thread is covered
    from fastapi.testclient import TestClient

    from api.auth import AuthenticatedUser

    # Importing api.app builds the production app at module level: a second
    # store and auth service, plus startup schema validation (an RPC the HTTP
    # guard refuses). Hide the credentials for the import only, so that build
    # takes its existing "missing configuration" path; the profiler's own store
    # already exists, and only create_app is used.
    credentials = {key: os.environ.pop(key) for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
                   if key in os.environ}
    try:
        from api.app import create_app
    finally:
        os.environ.update(credentials)

    profile_row = raw_store.get_profile(args.athlete) or {}
    user = AuthenticatedUser(user_id=args.athlete, email=str(profile_row.get("email") or ""),
                             full_name=str(profile_row.get("full_name") or ""), metadata={})

    class _SignedIn:
        def get_user_from_token(self, token: str):
            return user

    app = create_app(store=store, auth_service=_SignedIn(), enable_in_process_generation=False)
    # create_app configures logging to stdout; the report must be the only output.
    import logging

    logging.disable(logging.CRITICAL)
    client = TestClient(app)  # no context manager: the app's startup (lifespan) never runs
    if args.target == "calendar":
        plan_id = raw_store.get_active_plan_id(args.athlete)
        if not plan_id:
            raise SystemExit("athlete has no active plan")
        path = f"/api/plans/{plan_id}/calendar"
    else:
        path = "/api/training/history?include_skipped=true&limit=200"  # the web app's first page

    def call():
        store.reset()
        started = time.perf_counter()
        response = client.get(path, headers={"Authorization": "Bearer profile"})
        return response, (time.perf_counter() - started) * 1000

    cold, cold_ms = call()
    cold_calls = sum(store.calls.values())
    warm = [call() for _ in range(max(args.warm_runs, 1))]
    profiles.start()
    last, _ = call()
    profiles.stop()
    store_calls = {name: {"calls": store.calls[name], "ms": round(store.ms[name], 1)} for name in sorted(store.calls)}
    payload = last.json() if last.status_code == 200 else None
    merged = profiles.merged()
    return {
        "label": args.label,
        "target": args.target,
        "path": "/api/plans/:id/calendar" if args.target == "calendar" else path,
        "import_ms": round(import_ms),
        "status_codes": sorted({cold.status_code, *(r.status_code for r, _ in warm), last.status_code}),
        "cold": {"total_ms": round(cold_ms), "store_calls": cold_calls},
        "warm": {"total_ms": [round(ms) for _, ms in warm],
                 "median_total_ms": round(statistics.median(ms for _, ms in warm))},
        "store_calls_one_warm_request": store_calls,
        "store_call_timeline_one_warm_request": store.timeline_rows(),
        "refused_writes": dict(store.refused),
        "refused_http_requests": {k: v for k, v in http_refused.items()},
        "response_bytes": len(last.content),
        "response_rows": len(payload.get("sessions", [])) if isinstance(payload, dict) and "sessions" in payload else None,
        "response_fields": _sizes(payload)["fields"] if isinstance(payload, dict) else None,
        "profile_top_cumulative_repo": _profile_rows(merged, "cumulative", 40) if merged else [],
        "profile_top_own_time": _profile_rows(merged, "tottime", 25) if merged else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--athlete", required=True)
    parser.add_argument("--target", choices=("today", "calendar", "history"), default="today")
    parser.add_argument("--now", help="ISO instant, e.g. 2026-11-12T19:04:00Z (required for today)")
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument("--label", default=os.getenv("PROFILE_LABEL", ""))
    args = parser.parse_args()

    http_refused = install_http_read_only_guard()
    import_started = time.perf_counter()
    from api import server_timing
    from api.services.today_readiness_boundary import build_today_command_view
    from api.store import SupabaseAppStore
    import_ms = (time.perf_counter() - import_started) * 1000

    raw_store = SupabaseAppStore.from_env()
    if args.target != "today":
        print(json.dumps(profile_route(args, raw_store, ReadOnlyTimedStore(raw_store), http_refused, import_ms), indent=1))
        return
    if not args.now:
        raise SystemExit("--now is required for --target today")
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
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
        "store_call_timeline_one_warm_build": store.timeline_rows(),
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
