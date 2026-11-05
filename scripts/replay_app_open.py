"""App-open replay: the web app's launch burst against the real API process.

Usage (repo root):
    APP_ENV=test python scripts/replay_app_open.py PLAN_ROW.json OUT.json [--runs N]
        [--latency-ms 40] [--library-parse] [--plan-warmup]

PLAN_ROW.json is one ``plans`` row as PostgREST returns it (``select=*``). It
is never committed: export the athlete's active plan to a local file to replay
production-sized data (the 2026-11-04 active plan is ~1.3 MB).

What is real: the FastAPI app, its routes and services, uvicorn with one
worker (as deployed), concurrent requests from client threads, and the
supabase-py PostgREST response parse of every store result (serialized to JSON
with the column projection the real store requests). What is simulated: each
outermost store call sleeps ``--latency-ms`` (network + Supabase origin, from
production edge logs) and data comes from the in-memory test store.

``--library-parse`` uses supabase-py's own response parse (the pre-change
behaviour); otherwise the installed fast row parser runs. ``--plan-warmup``
adds Home's former ``GET /api/plans/{id}`` warm-up for an undated plan.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("APP_ENV", "test")

parser = argparse.ArgumentParser()
parser.add_argument("plan_row")
parser.add_argument("out")
parser.add_argument("--runs", type=int, default=7)
parser.add_argument("--latency-ms", type=float, default=40.0)
parser.add_argument("--library-parse", action="store_true")
parser.add_argument("--plan-warmup", action="store_true")
parser.add_argument("--port", type=int, default=8765)
args = parser.parse_args()

import httpx  # noqa: E402
from postgrest.base_request_builder import APIResponse  # noqa: E402

LIBRARY_PARSE = APIResponse.from_http_request_response  # before api.store installs the fast path

import test_today_read_amplification as amp  # noqa: E402
from support import _build_client  # noqa: E402

import api.store as real_store  # noqa: E402
from api.services.today_command_cache import forget_today_command  # noqa: E402

PARSE = LIBRARY_PARSE if args.library_parse else APIResponse.from_http_request_response
LATENCY = args.latency_ms / 1000

client, store, _ = _build_client(enable_in_process_generation=False)
app = client.app
H = {"Authorization": "Bearer athlete-token"}
ATH = "athlete-1"
PLAN = "11111111-1111-1111-1111-111111111111"
today = datetime.now(timezone.utc).date()

amp.CAMP_START = today - timedelta(days=2)
amp._add_camp(store, athlete_id=ATH, plan_id=PLAN, weeks=5)
row = json.loads(Path(args.plan_row).read_text())
row.update(id=PLAN, athlete_id=ATH, intake_id="intake-1")
store.plans[PLAN] = row
store.intakes.setdefault(ATH, []).append({
    "id": "intake-1", "athlete_id": ATH, "created_at": f"{today - timedelta(days=3)}T00:00:00+00:00",
    "intake": {"injuries": "left ankle sprain, right shoulder impingement"}})
for area, text in (("knee", "left knee pain when squatting"), ("lower back", "lower back tightness")):
    r = client.post("/api/injury-flags", headers=H, json={"body_area": area, "description": text, "severity": "mild"})
    assert r.status_code < 300, r.text
for d in range(1, 12):
    day = (today - timedelta(days=d)).isoformat()
    store.upsert_today_checkin(ATH, {"plan_id": PLAN, "training_day": day, "sleep": "good", "body": "normal",
                                     "pain": "none", "phase": "build", "readiness_tier": "go"})
    store.upsert_session_completion(ATH, {"plan_id": PLAN, "session_id": f"s-{d}", "training_day": day, "status": "done"})
for d in range(20):
    store.create_external_training_session(ATH, {"training_day": (today - timedelta(days=d)).isoformat(),
                                                  "session_type": "run", "duration_minutes": 30, "rpe": 6,
                                                  "client_request_id": f"00000000-0000-4000-8000-{d:012d}"})


def _columns(select: str) -> list[str]:
    return [part.strip() for part in select.split(",") if part.strip()]


PROJECTIONS = {
    "get_training_plan_for_athlete": _columns(real_store.TRAINING_PLAN_SELECT),
    "list_user_plans": _columns(real_store.PLAN_SUMMARY_SELECT),
    "get_plan_identity_for_athlete": ["id", "athlete_id"],
}
_REQUEST = httpx.Request("GET", "https://replay.invalid/rest/v1/x")
local = threading.local()
CALLS: list[str] = []
PARSE_MS: list[float] = []


def _project(name, value):
    cols = PROJECTIONS.get(name)
    if cols is None:
        return value
    pick = lambda item: {k: item.get(k) for k in cols} if isinstance(item, dict) else item  # noqa: E731
    return [pick(item) for item in value] if isinstance(value, list) else pick(value)


def _rows(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def charge(fn, name):
    @functools.wraps(fn)
    def wrapped(*a, **k):
        depth = getattr(local, "d", 0)
        local.d = depth + 1
        try:
            result = fn(*a, **k)
        finally:
            local.d = depth
        if depth == 0:
            CALLS.append(name)
            time.sleep(LATENCY)
            try:
                body = json.dumps(_rows(_project(name, result)), default=str).encode()
            except (TypeError, ValueError):
                body = b"[]"
            started = time.perf_counter()
            PARSE(httpx.Response(200, content=body, request=_REQUEST))
            PARSE_MS.append((time.perf_counter() - started) * 1000)
        return result
    return wrapped


for attr_name in dir(type(store)):
    if attr_name.startswith("_") or attr_name in {"is_admin_email"}:
        continue
    attr = getattr(store, attr_name)
    if callable(attr):
        setattr(store, attr_name, charge(attr, attr_name))

import uvicorn  # noqa: E402

server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, workers=1, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.05)
http = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", headers=H, timeout=60)


def app_open() -> dict[str, float]:
    """The browser's launch burst. Home is usable when /api/me and /api/today return."""
    forget_today_command(app.state.store, ATH)
    CALLS.clear(); PARSE_MS.clear()
    t0 = time.perf_counter(); marks: dict[str, float] = {}

    def get(name, method, path):
        r = http.request(method, path)
        assert r.status_code < 300, (path, r.status_code, r.text[:200])
        marks[name] = (time.perf_counter() - t0) * 1000

    threads = [threading.Thread(target=get, args=a) for a in (
        ("me", "GET", "/api/me"),
        ("today", "GET", "/api/today"),
        ("generation", "GET", "/api/generation-jobs/active"),
    )]
    for t in threads: t.start()
    threads[0].join()
    # After /api/me: XP activity + progress (XpProvider) and Home's plan warm-up.
    later = [threading.Thread(target=get, args=("xp_activity", "POST", "/api/xp/activity")),
             threading.Thread(target=get, args=("xp_progress", "GET", "/api/xp/progress"))]
    if args.plan_warmup:
        later.append(threading.Thread(target=get, args=("plan", "GET", f"/api/plans/{PLAN}")))
    for t in later: t.start()
    for t in threads + later: t.join()
    marks["home_primary"] = max(marks["me"], marks["today"])
    marks["all_done"] = max(marks.values())
    marks["store_calls"] = len(CALLS)
    marks["parse_ms_total"] = round(sum(PARSE_MS), 1)
    return marks


def repeat_today() -> float:
    t = time.perf_counter(); r = http.get("/api/today"); assert r.status_code == 200
    return (time.perf_counter() - t) * 1000


def save_checkin() -> float:
    body = {"plan_id": PLAN, "sleep": "good", "body": "normal", "pain": "none", "phase": "GPP"}
    t = time.perf_counter(); r = http.post("/api/today/checkin", json=body); assert r.status_code < 300, r.text[:200]
    return (time.perf_counter() - t) * 1000


def log_training(index: int) -> float:
    body = {"activity": "boxing", "duration_minutes": 45, "effort": "moderate",
            "client_request_id": f"00000000-0000-4000-9000-{index:012d}"}
    t = time.perf_counter(); r = http.post("/api/training/sessions", json=body); assert r.status_code < 300, r.text[:200]
    return (time.perf_counter() - t) * 1000


app_open()  # warm imports and caches
runs = [app_open() for _ in range(args.runs)]
repeat = [repeat_today() for _ in range(args.runs)]
checkins = [save_checkin() for _ in range(args.runs)]
trainings = [log_training(i) for i in range(args.runs)]
keys = runs[0].keys()
summary = {key: round(statistics.median(run[key] for run in runs), 1) for key in keys}
summary["repeat_today_ms"] = round(statistics.median(repeat), 1)
summary["save_checkin_ms"] = round(statistics.median(checkins), 1)
summary["log_training_ms"] = round(statistics.median(trainings), 1)
result = {"config": {k: v for k, v in vars(args).items() if k not in {"plan_row", "out"}},
          "plan_row_bytes": len(json.dumps(row)), "median": summary, "runs": runs, "repeat_today_ms": repeat}
Path(args.out).write_text(json.dumps(result, indent=2))
print(json.dumps(summary))
server.should_exit = True
