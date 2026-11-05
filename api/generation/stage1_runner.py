"""Stage 1 planner execution: default planner, invocation, and subprocess runner.

The deterministic Stage 1 planner runs in a child process (start method is
configurable via ``UNLXCK_STAGE1_MP_START_METHOD``). Progress events are relayed
back to the parent through a queue; the parent enforces the Stage 1 timeout and
terminates/kills the child if it overruns.
"""
from __future__ import annotations

import asyncio
import inspect
import multiprocessing as mp
import logging
import os
import queue
import time
import traceback
from contextlib import suppress
from typing import Any

from .types import Planner, ProgressCallback

logger = logging.getLogger(__name__)


def generate_plan_sync(payload: dict[str, Any], *, progress_callback: ProgressCallback | None = None) -> dict[str, Any]:
    # A spawned custom planner must not pay for importing the unused default
    # planner and its model graph before its own callable can even start.
    from fightcamp.main import generate_plan_sync as generate

    return generate(payload, progress_callback=progress_callback)


class Stage1PlannerError(RuntimeError):
    """Raised when the Stage 1 planner subprocess reports a failure.

    Subclasses ``RuntimeError`` so existing handlers keep catching it. It also
    carries the child-process traceback (when the subprocess captured one) so
    the parent can surface it in structured logs for admin diagnostics instead
    of discarding it.
    """

    def __init__(self, message: str, *, child_traceback: str | None = None) -> None:
        super().__init__(message)
        self.child_traceback = child_traceback


def default_planner(
    payload: dict[str, Any],
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    if progress_callback is not None:
        progress_callback(
            "stage1_default_planner_entered",
            "Stage 1 default planner entered",
            "",
            {},
        )
        progress_callback(
            "stage1_generate_plan_sync_entering",
            "Stage 1 generate_plan_sync entering",
            "",
            {},
        )
    return generate_plan_sync(payload, progress_callback=progress_callback)


def _planner_accepts_progress_callback(planner_fn: Planner) -> bool:
    try:
        signature = inspect.signature(planner_fn)
    except (TypeError, ValueError):
        return True

    parameters = signature.parameters
    if "progress_callback" in parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _invoke_planner(
    planner_fn: Planner,
    payload: dict[str, Any],
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    """Call a planner that may or may not accept a ``progress_callback`` kwarg."""
    if progress_callback is not None:
        progress_callback(
            "stage1_planner_callable_entering",
            "Stage 1 planner callable entering",
            "",
            {},
        )
    if progress_callback is None:
        return planner_fn(payload)

    planner_supports_progress_callback = _planner_accepts_progress_callback(planner_fn)
    if planner_supports_progress_callback:
        progress_callback(
            "stage1_planner_callable_supports_progress_callback",
            "Stage 1 planner callable supports progress callback",
            "",
            {},
        )
        return planner_fn(payload, progress_callback=progress_callback)
    return planner_fn(payload)


def _run_planner_in_subprocess(
    planner_fn: Planner,
    payload: dict[str, Any],
    result_queue: Any,
    progress_queue: Any,
) -> None:
    def _child_progress_callback(code: str, label: str, detail: str, meta: dict[str, Any]) -> None:
        progress_queue.put((code, label, detail, meta))

    try:
        result = _invoke_planner(planner_fn, payload, _child_progress_callback)
        result_queue.put(("ok", result))
    except Exception as exc:  # pragma: no cover - defensive relay
        result_queue.put(("error", {"message": str(exc), "traceback": traceback.format_exc()}))


async def _drain_stage1_progress_queue(
    progress_queue: Any,
    progress_callback: ProgressCallback | None,
) -> None:
    while True:
        try:
            code, label, detail, meta = progress_queue.get_nowait()
        except queue.Empty:
            return
        if progress_callback is not None:
            progress_callback(code, label, detail, meta)


def _stage1_mp_start_method() -> str:
    raw_value = os.getenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn").strip().lower()
    if raw_value in {"spawn", "fork", "forkserver"}:
        return raw_value
    logger.warning(
        "[jobs] generation:invalid_stage1_mp_start_method value=%r; falling back to spawn",
        raw_value,
    )
    return "spawn"


def _stop_stage1_process(process: Any) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)


def _read_stage1_result_queue_nowait(result_queue: Any) -> tuple[str, Any] | None:
    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return None


def _handle_stage1_result_message(message: tuple[str, Any]) -> dict[str, Any]:
    status, payload_or_error = message

    if status == "ok":
        if not isinstance(payload_or_error, dict):
            raise RuntimeError("Stage 1 planner returned a non-dict result.")
        return payload_or_error

    if isinstance(payload_or_error, dict):
        raise Stage1PlannerError(
            payload_or_error.get("message") or "Stage 1 planner failed",
            child_traceback=payload_or_error.get("traceback"),
        )

    raise Stage1PlannerError("Stage 1 planner failed")


async def run_stage1_planner(
    planner_fn: Planner,
    payload: dict[str, Any],
    *,
    progress_callback: ProgressCallback | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    ctx = mp.get_context(_stage1_mp_start_method())
    result_queue = ctx.Queue()
    progress_queue = ctx.Queue()
    process = ctx.Process(
        target=_run_planner_in_subprocess,
        args=(planner_fn, payload, result_queue, progress_queue),
        daemon=True,
    )
    process.start()

    start = time.monotonic()
    try:
        while True:
            await _drain_stage1_progress_queue(progress_queue, progress_callback)
            result_message = _read_stage1_result_queue_nowait(result_queue)
            if result_message is not None:
                if progress_callback is not None:
                    progress_callback(
                        "stage1_result_queue_received",
                        "Stage 1 result queue received",
                        "Parent runtime received the Stage 1 planner result from the subprocess.",
                        {},
                    )
                return _handle_stage1_result_message(result_message)
            if not process.is_alive():
                await _drain_stage1_progress_queue(progress_queue, progress_callback)
                try:
                    result_message = result_queue.get(timeout=1)
                except queue.Empty as exc:
                    raise RuntimeError(
                        f"Stage 1 planner process exited without result. exitcode={process.exitcode}"
                    ) from exc
                return _handle_stage1_result_message(result_message)
            if timeout_seconds is not None and (time.monotonic() - start) >= timeout_seconds:
                await _drain_stage1_progress_queue(progress_queue, progress_callback)
                result_message = _read_stage1_result_queue_nowait(result_queue)
                if result_message is not None:
                    return _handle_stage1_result_message(result_message)
                _stop_stage1_process(process)
                raise asyncio.TimeoutError
            await asyncio.sleep(0.05)
    finally:
        _stop_stage1_process(process)
        for q in (result_queue, progress_queue):
            with suppress(Exception):
                q.close()
            with suppress(Exception):
                q.join_thread()
