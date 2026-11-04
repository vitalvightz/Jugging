"""Single-process replay server: real FastAPI app, in-memory store, per-call latency.

Usage (repo root): APP_ENV=test python scripts/replay_latency_server.py PORT LATENCY_MS
Each *outermost* store call sleeps LATENCY_MS (sleep releases the GIL like a
socket read), standing in for one Supabase Data API round trip as measured in
production edge logs. Nested store calls are not charged twice. Set ARGS=1 to
record call arguments at /__calls. Never connects to a database.
"""
import functools, sys, threading, time, os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("REPO", Path.cwd()))
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("APP_ENV", "test")

import test_today_read_amplification as amp  # noqa: E402
from support import _build_client  # noqa: E402

PORT, LATENCY = int(sys.argv[1]), float(sys.argv[2]) / 1000
H = {"Authorization": "Bearer athlete-token"}
client, store, _ = _build_client(enable_in_process_generation=False)
app = client.app
ATH = "athlete-1"
today = datetime.now(timezone.utc).date()

# Active camp around today (production athlete: week 1 of 5, fight in ~26 days),
# plus an older non-active "latest" plan like the recording's athlete.
amp.CAMP_START = today - timedelta(days=2)
PLAN = "11111111-1111-1111-1111-111111111111"
amp._add_camp(store, athlete_id=ATH, plan_id=PLAN, weeks=5)
store.intakes.setdefault(ATH, []).append({
    "id": "intake-1", "athlete_id": ATH, "created_at": f"{today - timedelta(days=3)}T00:00:00+00:00",
    "intake": {"injuries": "left ankle sprain, right shoulder impingement"}})
store.plans[PLAN]["intake_id"] = "intake-1"

# Injury history shaped by production aggregates (131 flags, ~200 episode events
# for the 22:55 athlete): open flags through the real endpoint, resolved history.
for area, text in (("knee", "left knee pain when squatting"), ("lower back", "lower back tightness")):
    r = client.post("/api/injury-flags", headers=H, json={"body_area": area, "description": text, "severity": "mild"})
    assert r.status_code < 300, r.text
for i in range(60):
    store.create_injury_flag(ATH, {"body_area": "wrist", "description": f"old wrist {i}", "severity": "mild",
                                   "status": "resolved"})
# Check-ins over recent days and today, completions and manual sessions.
for d in range(1, 12):
    day = (today - timedelta(days=d)).isoformat()
    store.upsert_today_checkin(ATH, {"plan_id": PLAN, "training_day": day, "sleep": "good", "body": "normal",
                                     "pain": "none", "phase": "build", "readiness_tier": "go"})
    store.upsert_session_completion(ATH, {"plan_id": PLAN, "session_id": f"s-{d}", "training_day": day, "status": "done"})
for d in range(20):
    store.create_external_training_session(ATH, {"training_day": (today - timedelta(days=d)).isoformat(),
                                                  "session_type": "run", "duration_minutes": 30, "rpe": 6,
                                                  "client_request_id": f"00000000-0000-4000-8000-{d:012d}"})
r = client.get("/api/today", headers=H)
assert r.status_code == 200, r.text
print("fixture ok", r.json()["active_plan"]["id"] if r.json().get("active_plan") else None, flush=True)

# ---- latency injection -------------------------------------------------------
local = threading.local()
CALLS = []
def charge(fn, name):
    @functools.wraps(fn)
    def wrapped(*a, **k):
        depth = getattr(local, "d", 0)
        if depth == 0:
            CALLS.append(name if not os.environ.get('ARGS') else f'{name}{a[1:] if a and a[0] is store else a}{k}')
            time.sleep(LATENCY)
        local.d = depth + 1
        try:
            return fn(*a, **k)
        finally:
            local.d = depth
    return wrapped
for name in dir(type(store)):
    if name.startswith("_") or name in {"is_admin_email"}:
        continue
    attr = getattr(store, name)
    if callable(attr):
        setattr(store, name, charge(attr, name))

@app.get("/__calls")
def calls(reset: int = 0):
    out = {"n": len(CALLS), "list": list(CALLS)}
    if reset:
        CALLS.clear()
    return out

@app.post("/__forget")
def forget():
    from api.services.today_command_cache import forget_today_command
    forget_today_command(app.state.store, ATH)
    return {}

import uvicorn  # noqa: E402
uvicorn.run(app, host="127.0.0.1", port=PORT, workers=1, log_level="warning")
