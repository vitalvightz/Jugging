"""Drive scripts/replay_latency_server.py the way the web app does.

Usage: python scripts/replay_latency_client.py PORT RUNS OUT.json
"""
import json, statistics, sys, threading, time
import httpx

PORT, RUNS, OUT = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
B = f"http://127.0.0.1:{PORT}"
H = {"Authorization": "Bearer athlete-token"}
PLAN = "11111111-1111-1111-1111-111111111111"
http = httpx.Client(base_url=B, headers=H, timeout=60)


def calls(reset=False):
    return http.get("/__calls", params={"reset": int(reset)}).json()["n"]


def timed(method, path, **kw):
    t = time.perf_counter()
    r = http.request(method, path, **kw)
    return r, (time.perf_counter() - t) * 1000


def app_open():
    """Cold-visit burst as issued by the web app on Home.

    AuthProvider: GET /api/me; then Home: GET /api/today and the plan warm-up
    (GET /api/plans/{id}) in parallel. XpProvider in parallel: POST
    /api/xp/activity then GET /api/xp/progress. Home is usable when /api/today
    returns.
    """
    http.post("/__forget")  # no Today view remembered from an earlier visit
    t0 = time.perf_counter(); marks = {}
    def home():
        http.get("/api/me"); marks["me"] = time.perf_counter() - t0
        th = threading.Thread(target=lambda: (http.get(f"/api/plans/{PLAN}"), marks.__setitem__("plan", time.perf_counter() - t0)))
        th.start()
        r = http.get("/api/today"); assert r.status_code == 200, r.text
        marks["today"] = time.perf_counter() - t0
        th.join()
    def xp():
        http.post("/api/xp/activity")
        r = http.get("/api/xp/progress"); assert r.status_code == 200, r.text
        marks["xp"] = time.perf_counter() - t0
    ts = [threading.Thread(target=home), threading.Thread(target=xp)]
    [t.start() for t in ts]; [t.join() for t in ts]
    marks["all"] = time.perf_counter() - t0
    return {k: round(v * 1000) for k, v in marks.items()}


def run(name, fn):
    out = []
    for _ in range(RUNS):
        calls(reset=True)
        res = fn()
        res = res if isinstance(res, dict) else {"ms": round(res)}
        res["store_calls"] = calls()
        out.append(res)
        time.sleep(0.2)
    keys = [k for k in out[0] if k != "store_calls"]
    summary = {k: statistics.median(r[k] for r in out) for k in keys}
    summary["store_calls"] = statistics.median(r["store_calls"] for r in out)
    print(f"{name:28s} median {summary}  ranges " + ", ".join(f"{k}={min(r[k] for r in out)}–{max(r[k] for r in out)}" for k in keys), flush=True)
    return {"runs": out, "median": summary}


results = {}
http.get("/api/today")  # warm process
results["today_alone"] = run("GET /api/today (Home nav)", lambda: timed("GET", "/api/today")[1])
results["xp_alone"] = run("GET /api/xp/progress (cold)", lambda: (http.post("/__forget"), timed("GET", "/api/xp/progress")[1])[1])
results["app_open"] = run("app open burst", app_open)

def app_open_plan_after_today():
    """Same burst, but the active plan's detail is read once Today names it."""
    http.post("/__forget")
    t0 = time.perf_counter(); marks = {}
    def home():
        http.get("/api/me"); marks["me"] = time.perf_counter() - t0
        r = http.get("/api/today"); assert r.status_code == 200, r.text
        marks["today"] = time.perf_counter() - t0
        http.get(f"/api/plans/{PLAN}"); marks["plan"] = time.perf_counter() - t0
    def xp():
        http.post("/api/xp/activity")
        r = http.get("/api/xp/progress"); assert r.status_code == 200, r.text
        marks["xp"] = time.perf_counter() - t0
    ts = [threading.Thread(target=home), threading.Thread(target=xp)]
    [t.start() for t in ts]; [t.join() for t in ts]
    marks["all"] = time.perf_counter() - t0
    return {k: round(v * 1000) for k, v in marks.items()}
results["app_open_plan_after"] = run("app open, plan after Today", app_open_plan_after_today)


def home_after_xp_refresh():
    """Repeat Home visit: Today plus the XP refresh a reward/feedback triggers."""
    http.post("/__forget")
    t0 = time.perf_counter(); marks = {}
    def a():
        http.get("/api/today"); marks["today"] = time.perf_counter() - t0
    def b():
        http.get("/api/xp/progress"); marks["xp"] = time.perf_counter() - t0
    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return {k: round(v * 1000) for k, v in marks.items()}
results["today_plus_xp"] = run("Today + XP concurrently", home_after_xp_refresh)

checkin = {"plan_id": PLAN, "sleep": "good", "body": "normal", "pain": "none", "phase": "GPP"}
def save_checkin():
    r, ms = timed("POST", "/api/today/checkin", json=checkin)
    assert r.status_code < 300, r.text
    return ms
results["save_checkin"] = run("POST check-in", save_checkin)

today = http.get("/api/today").json()
def find(o, key):
    if isinstance(o, dict):
        if key in o and o[key]: return o
        for v in o.values():
            if (f := find(v, key)) is not None: return f
    elif isinstance(o, list):
        for v in o:
            if (f := find(v, key)) is not None: return f
sess = find(today, "prescription_revision") or {}
sid, rev = sess.get("session_id"), sess.get("prescription_revision")
print("completion target", sid, bool(rev))
def save_completion():
    r, ms = timed("POST", "/api/today/session-completion", json={"plan_id": PLAN, "session_id": sid, "status": "done", "session_rpe": 6, "prescription_revision": rev})
    assert r.status_code < 300, r.text
    return ms
if sid:
    results["save_completion"] = run("POST session completion", save_completion)
else:
    print("no session id for completion save", list((today.get("today") or {}).keys()))

json.dump(results, open(OUT, "w"), indent=1)
