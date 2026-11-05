"""Read-only live Data API probe; no auth, writes, or endpoint/browser timings.

Pass an eligible active plan and its owner. Results contain timings only.
Run cold in a fresh interpreter; later iterations reuse its connection pool.
"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.store import SupabaseAppStore
from api.services.active_plan import resolve_active_plan
from api.plan_mappers import _map_plan_detail

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--plan', required=True)
parser.add_argument('--owner', required=True)
parser.add_argument('--day', required=True)
parser.add_argument('--runs', type=int, default=3)
args = parser.parse_args()
store = SupabaseAppStore.from_env()
for run in range(args.runs):
    for mode in ('before', 'after'):
        started = time.perf_counter()
        row = store.get_plan_for_athlete(args.plan, args.owner)
        row_ms = (time.perf_counter() - started) * 1000
        started, cpu = time.perf_counter(), time.thread_time()
        resolution = resolve_active_plan(
            store, args.owner, current_training_day=args.day, auto_activate=False,
            known_plan=row if mode == 'after' else None,
        )
        resolve_ms = (time.perf_counter() - started) * 1000
        resolve_cpu = (time.thread_time() - cpu) * 1000
        assert resolution.plan_id == args.plan, 'Probe requires the eligible active plan'
        started, cpu = time.perf_counter(), time.thread_time()
        detail = _map_plan_detail(row, include_admin=False, current_training_day=args.day)
        map_ms = (time.perf_counter() - started) * 1000
        map_cpu = (time.thread_time() - cpu) * 1000
        started = time.perf_counter()
        body = detail.model_dump_json().encode()
        print(json.dumps(dict(run=run, mode=mode, row_ms=row_ms,
            resolve_ms=resolve_ms, resolve_cpu_ms=resolve_cpu,
            mapping_ms=map_ms, mapping_cpu_ms=map_cpu,
            serialization_ms=(time.perf_counter()-started)*1000, bytes=len(body))), flush=True)
