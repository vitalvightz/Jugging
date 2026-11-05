"""Importable, lightweight callables for real spawn-process runtime checks."""
import time


def under_threshold_stage1_planner(payload, *, result, progress_callback=None):
    time.sleep(0.2)
    return result
