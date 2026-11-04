from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import suppress
from typing import TypedDict

from fightcamp.logging_utils import configure_logging

from .environment import apply_production_environment_defaults, should_default_to_production
from .error_sanitizer import sanitize_error_text
from .generation_runtime import default_planner, run_generation_job, utc_now_iso
from .generation_config import generation_job_stale_after_seconds
from .stage2_automation import build_default_stage2_automator
from .store import AppStore, SupabaseAppStore, is_pre_start_stale_generation_job
from .worker_recovery import recover_stale_generation_jobs
from .settings import env_flag, env_float, env_int

logger = logging.getLogger(__name__)


def _worker_stale_after_seconds() -> int:
    return generation_job_stale_after_seconds()


def _worker_max_concurrent_jobs() -> int:
    return env_int("UNLXCK_GENERATION_WORKER_MAX_CONCURRENT_JOBS", 1, minimum=1)


def _worker_shutdown_grace_seconds() -> int:
    return env_int("UNLXCK_GENERATION_WORKER_SHUTDOWN_GRACE_SECONDS", 25, minimum=1)


def _worker_recovery_sweep_interval_seconds() -> int:
    # A job only counts as stale after generation_job_stale_after_seconds
    # (300 s by default), so a 15 s sweep re-listed running jobs ~20 times per
    # stale window. Once a minute still recovers within 20% of that window.
    return env_int("UNLXCK_GENERATION_WORKER_RECOVERY_SWEEP_SECONDS", 60, minimum=5)


def _contribution_xp_replay_idle_seconds() -> int:
    return env_int("UNLXCK_CONTRIBUTION_XP_REPLAY_IDLE_SECONDS", 900, minimum=60)


def _morning_push_sweep_interval_seconds() -> int:
    return env_int("UNLXCK_MORNING_PUSH_SWEEP_INTERVAL_SECONDS", 600, minimum=60)


async def _run_morning_push_sweep_if_due(
    *,
    store: AppStore,
    state: dict[str, float],
    interval_seconds: int,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Piggyback the morning check-in push sweep on the worker's tick loop.

    The sweep itself is idempotent (per-device local-day dedupe), so the cadence
    only bounds delivery latency after the local morning hour. It runs in a
    worker thread (sync store + HTTP calls) and never raises into the loop.
    Like the media sweep it is detached: a sweep visits every subscribed
    athlete, so awaiting it here stopped job claiming for its whole duration.
    A tick that finds the previous sweep still running skips. Once shutdown is
    requested the sweep stops before its next athlete.
    """

    now = time.monotonic()
    if now - state.get("last_sweep_at", 0.0) < interval_seconds:
        return
    running = _MORNING_PUSH_SWEEP_TASK.get("task")
    if running is not None and not running.done():
        return
    state["last_sweep_at"] = now

    async def _sweep() -> None:
        try:
            from .services.morning_push import morning_push_enabled, run_morning_push_sweep

            if not morning_push_enabled():
                return
            await asyncio.to_thread(
                run_morning_push_sweep,
                store,
                should_stop=shutdown_event.is_set if shutdown_event is not None else None,
            )
        except Exception:  # noqa: BLE001 - the nudge sweep must never disturb generation
            logger.exception("[worker] morning push sweep failed")

    _MORNING_PUSH_SWEEP_TASK["task"] = asyncio.create_task(_sweep())


_MORNING_PUSH_SWEEP_TASK: dict[str, asyncio.Task[None] | None] = {"task": None}


_PLAN_READY_EMAIL_TASK: dict[str, asyncio.Task[None] | None] = {"task": None}


async def _run_plan_ready_email_sweep_if_due(
    *, store: AppStore, state: dict[str, float], shutdown_event: asyncio.Event,
) -> None:
    """Deliver queued plan-ready emails independently of generation jobs."""
    if shutdown_event.is_set() or time.monotonic() - state.get("last_sweep_at", 0.0) < 30:
        return
    running = _PLAN_READY_EMAIL_TASK["task"]
    if running is not None and not running.done():
        return
    state["last_sweep_at"] = time.monotonic()

    async def _sweep() -> None:
        try:
            from .services.plan_ready_email import deliver_pending_plan_ready_emails
            await asyncio.to_thread(deliver_pending_plan_ready_emails, store)
        except Exception:  # noqa: BLE001 - email must never affect job processing
            logger.exception("[worker] plan-ready email sweep failed")

    _PLAN_READY_EMAIL_TASK["task"] = asyncio.create_task(_sweep())


class ContributionReplayState(TypedDict, total=False):
    last_sweep_at: float
    after_id: str | None


_CONTRIBUTION_XP_REPLAY_TASK: dict[str, asyncio.Task[None] | None] = {"task": None}


async def _run_contribution_xp_replay_if_due(
    *, store: AppStore, state: ContributionReplayState,
    interval_seconds: int = 60, idle_interval_seconds: int = 900,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Retry missed approvals without holding up generation or requiring push.

    Mid-walk (a cursor is held) batches follow ``interval_seconds`` so a
    backlog drains quickly. A pass that wrapped with nothing left to reward
    (cursor ``None``) waits ``idle_interval_seconds``: approvals reconcile
    inline, so the replay only catches rare misses and its discovery query
    cannot use an index.
    """
    if shutdown_event is not None and shutdown_event.is_set():
        return
    now = time.monotonic()
    last = state.get("last_sweep_at")
    caught_up = "after_id" in state and state["after_id"] is None
    wait = max(interval_seconds, idle_interval_seconds) if caught_up else interval_seconds
    if last is not None and now - last < wait:
        return
    running = _CONTRIBUTION_XP_REPLAY_TASK.get("task")
    if running is not None and not running.done():
        return
    state["last_sweep_at"] = now

    async def _replay() -> None:
        try:
            from .services.tracking_xp import replay_contribution_xp

            state["after_id"] = await asyncio.to_thread(
                replay_contribution_xp, store, after_id=state.get("after_id"),
                should_stop=shutdown_event.is_set if shutdown_event is not None else None,
            )
        except Exception:  # noqa: BLE001 - reward recovery must not interrupt generation
            logger.exception("[worker] contribution XP replay failed")

    _CONTRIBUTION_XP_REPLAY_TASK["task"] = asyncio.create_task(_replay())


def _exercise_media_sweep_interval_seconds() -> int:
    return env_int("UNLXCK_EXERCISE_MEDIA_SWEEP_INTERVAL_SECONDS", 86400, minimum=3600)


async def _run_exercise_media_sweep_if_due(
    *,
    store: AppStore,
    state: dict[str, float],
    interval_seconds: int,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Daily availability check for exercise demo videos.

    Third-party videos get deleted, made private, have embedding disabled or
    are re-marked as made for kids. A failed check flips the row to
    'unavailable' so the plan stops serving it and the athlete sees the
    cues-only row instead of a dead player; a later passing check restores it.
    Needs YOUTUBE_DATA_API_KEY. Runs in a worker thread and never raises into
    the loop. The first check runs as soon as the worker starts. Once shutdown
    is requested the thread stops before its next batch of 50 videos, so exit
    waits for at most one batch.
    """

    now = time.monotonic()
    last_sweep_at = state.get("last_sweep_at")
    if last_sweep_at is not None and now - last_sweep_at < interval_seconds:
        return
    if not env_flag("UNLXCK_EXERCISE_MEDIA_SWEEP_ENABLED", True):
        return
    running = _MEDIA_SWEEP_TASK.get("task")
    if running is not None and not running.done():
        return
    state["last_sweep_at"] = now

    async def _sweep() -> None:
        try:
            from .services.exercise_media import run_media_verification_sweep

            await asyncio.to_thread(
                run_media_verification_sweep,
                store,
                should_stop=shutdown_event.is_set if shutdown_event is not None else None,
            )
        except Exception:  # noqa: BLE001 - media checks must never disturb generation
            logger.exception("[worker] exercise media sweep failed")

    # Detached: one HTTP check per stored video must not hold up job claiming.
    _MEDIA_SWEEP_TASK["task"] = asyncio.create_task(_sweep())


_MEDIA_SWEEP_TASK: dict[str, asyncio.Task[None] | None] = {"task": None}


async def _run_generation_recovery_sweep_if_due(
    *,
    store: AppStore,
    active_tasks: set[str],
    stale_after_seconds: int,
    state: dict[str, float],
    interval_seconds: int,
) -> None:
    """Resolve non-claimable stale jobs on a bounded worker cadence."""

    now = time.monotonic()
    if now - state.get("last_sweep_at", 0.0) < interval_seconds:
        return
    state["last_sweep_at"] = now
    await recover_stale_generation_jobs(
        store=store,
        active_tasks=active_tasks,
        stale_after_seconds=stale_after_seconds,
    )


def _install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
) -> None:
    """Trip ``shutdown_event`` on SIGTERM/SIGINT so the worker can drain cleanly."""

    def _request_shutdown(signal_name: str) -> None:
        if not shutdown_event.is_set():
            logger.info("[worker] shutdown:signal received=%s", signal_name)
            shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError, ValueError):
            # Signal handlers may be unavailable (e.g. non-main thread or some
            # platforms). Fall back to the default behaviour in that case.
            logger.debug("[worker] shutdown:add_signal_handler unsupported sig=%s", sig.name)


async def _drain_active_tasks(
    *,
    detached_tasks: set[asyncio.Task[None]],
    active_tasks: set[str],
    grace_seconds: int,
) -> None:
    """Give in-flight generation tasks a grace period, then cancel stragglers.

    Leaves both ``detached_tasks`` and ``active_tasks`` empty so shutdown does
    not pollute in-memory state. Jobs are intentionally not marked failed here:
    cancelled work is recovered via the heartbeat/stale-job path on restart.
    """
    pending = {task for task in detached_tasks if not task.done()}
    if pending:
        logger.info(
            "[worker] shutdown:draining pending_tasks=%s grace_seconds=%s",
            len(pending),
            grace_seconds,
        )
        _, still_pending = await asyncio.wait(pending, timeout=grace_seconds)
        if still_pending:
            logger.warning(
                "[worker] shutdown:cancelling unfinished_tasks=%s after grace period",
                len(still_pending),
            )
            for task in still_pending:
                task.cancel()
            with suppress(Exception):
                # Use a short timeout to prevent hanging indefinitely if a cancelled task blocks during cleanup
                await asyncio.wait(still_pending, timeout=5.0)

    detached_tasks.clear()
    active_tasks.clear()


async def _mark_job_failed_before_runtime(
    *,
    store: AppStore,
    job_id: str,
    error: str,
) -> None:
    with suppress(Exception):
        job = await asyncio.to_thread(store.get_generation_job, job_id)
        if not job:
            return
        failed_at = utc_now_iso()
        await asyncio.to_thread(
            store.fail_generation_job,
            job_id,
            expected_status=str(job.get("status") or "queued"),
            expected_attempt_count=int(job.get("attempt_count") or 0),
            error=error,
            failed_at=failed_at,
            heartbeat_at=failed_at,
        )


async def _run_claimed_job(
    *,
    job_id: str,
    store: AppStore,
    active_tasks: set[str],
    detached_tasks: set[asyncio.Task[None]] | None = None,
) -> None:
    try:
        stage2 = build_default_stage2_automator()
        await run_generation_job(
            job_id=job_id,
            store=store,
            planner_fn=default_planner,
            stage2=stage2,
            active_tasks=active_tasks,
        )
        # A failed inline enhanced card is retried inside run_generation_job,
        # before the job completes, so the athlete never leaves the generation
        # screen for the text fallback while a card is still being built.
    except Exception as exc:
        logger.exception("[worker] job failed before generation runtime job_id=%s", job_id)
        # The full traceback is in the server log above. The stored job error is
        # athlete/admin-visible, so sanitize it (redacts tokens/PII/payloads and
        # truncates) to match the orchestrator's error-handling convention rather
        # than persisting the raw exception text.
        await _mark_job_failed_before_runtime(
            store=store,
            job_id=job_id,
            error=f"Worker failed before generation runtime: {sanitize_error_text(exc)}",
        )
        active_tasks.discard(job_id)


def _cleanup_worker_task(
    task: asyncio.Task[None],
    *,
    detached_tasks: set[asyncio.Task[None]],
) -> None:
    detached_tasks.discard(task)

    if task.cancelled():
        return

    with suppress(Exception):
        task.result()


async def _tick(
    *,
    store: AppStore,
    active_tasks: set[str],
    detached_tasks: set[asyncio.Task[None]],
    stale_after_seconds: int,
    max_concurrent_jobs: int,
    recovery_state: dict[str, float] | None = None,
    recovery_interval_seconds: int = 60,
) -> None:
    recovery_state = recovery_state if recovery_state is not None else {}
    await _run_generation_recovery_sweep_if_due(
        store=store,
        active_tasks=active_tasks,
        stale_after_seconds=stale_after_seconds,
        state=recovery_state,
        interval_seconds=recovery_interval_seconds,
    )

    remaining_capacity = max_concurrent_jobs - len(active_tasks)
    if remaining_capacity <= 0:
        return

    try:
        candidates = await asyncio.to_thread(
            store.poll_claimable_generation_jobs,
            limit=remaining_capacity,
            stale_after_seconds=stale_after_seconds,
        )
    except Exception:
        logger.exception("[worker] failed to list claimable generation jobs")
        return

    for job in candidates:
        if len(active_tasks) >= max_concurrent_jobs:
            break

        job_id = str(job.get("id") or "")
        if not job_id or job_id in active_tasks:
            continue

        status = str(job.get("status") or "").strip().lower()
        if status == "running" and not is_pre_start_stale_generation_job(
            job,
            stale_after_seconds=stale_after_seconds,
        ):
            # Defense in depth: even if the compact RPC regresses, a job that
            # already reached worker-claim, Stage 1, mid-pipeline or persistence
            # must never be sent back through the claim/start path.
            continue
        if status not in {"", "queued", "running"}:
            continue

        active_tasks.add(job_id)

        try:
            task = asyncio.create_task(
                _run_claimed_job(
                    job_id=job_id,
                    store=store,
                    active_tasks=active_tasks,
                    detached_tasks=detached_tasks,
                )
            )
        except Exception:
            logger.exception("[worker] failed to create task job_id=%s", job_id)
            active_tasks.discard(job_id)
            await _mark_job_failed_before_runtime(
                store=store,
                job_id=job_id,
                error="Generation worker failed to schedule.",
            )
            continue

        detached_tasks.add(task)
        task.add_done_callback(
            lambda completed_task: _cleanup_worker_task(
                completed_task,
                detached_tasks=detached_tasks,
            )
        )


async def run_worker() -> None:
    configure_logging()
    if should_default_to_production():
        apply_production_environment_defaults()

    store = SupabaseAppStore.from_env()
    store.validate_runtime_schema()
    mode = "supabase"

    interval_seconds = env_float("UNLXCK_GENERATION_WORKER_INTERVAL_SECONDS", 3.0, minimum=1.0, positive=True)
    stale_after_seconds = _worker_stale_after_seconds()
    max_concurrent_jobs = _worker_max_concurrent_jobs()
    shutdown_grace_seconds = _worker_shutdown_grace_seconds()
    recovery_interval_seconds = _worker_recovery_sweep_interval_seconds()

    active_tasks: set[str] = set()
    detached_tasks: set[asyncio.Task[None]] = set()

    shutdown_event = asyncio.Event()
    _install_shutdown_handlers(asyncio.get_running_loop(), shutdown_event)

    logger.info(
        "[worker] started mode=%s interval_seconds=%s stale_after_seconds=%s max_concurrent_jobs=%s shutdown_grace_seconds=%s recovery_interval_seconds=%s",
        mode,
        interval_seconds,
        stale_after_seconds,
        max_concurrent_jobs,
        shutdown_grace_seconds,
        recovery_interval_seconds,
    )
    if not env_flag("UNLXCK_ENABLE_IN_PROCESS_GENERATION"):
        logger.info("[worker] generation:worker_only_mode enabled")

    recovery_sweep_state: dict[str, float] = {}
    morning_sweep_state: dict[str, float] = {}
    plan_ready_email_sweep_state: dict[str, float] = {}
    morning_sweep_interval = _morning_push_sweep_interval_seconds()
    contribution_replay_state: ContributionReplayState = {}
    contribution_replay_idle_seconds = _contribution_xp_replay_idle_seconds()
    media_sweep_state: dict[str, float] = {}
    media_sweep_interval = _exercise_media_sweep_interval_seconds()

    try:
        while not shutdown_event.is_set():
            await _tick(
                store=store,
                active_tasks=active_tasks,
                detached_tasks=detached_tasks,
                stale_after_seconds=stale_after_seconds,
                max_concurrent_jobs=max_concurrent_jobs,
                recovery_state=recovery_sweep_state,
                recovery_interval_seconds=recovery_interval_seconds,
            )
            await _run_plan_ready_email_sweep_if_due(
                store=store, state=plan_ready_email_sweep_state, shutdown_event=shutdown_event,
            )
            await _run_morning_push_sweep_if_due(
                store=store,
                state=morning_sweep_state,
                interval_seconds=morning_sweep_interval,
                shutdown_event=shutdown_event,
            )
            await _run_contribution_xp_replay_if_due(
                store=store, state=contribution_replay_state,
                idle_interval_seconds=contribution_replay_idle_seconds,
                shutdown_event=shutdown_event,
            )
            await _run_exercise_media_sweep_if_due(
                store=store,
                state=media_sweep_state,
                interval_seconds=media_sweep_interval,
                shutdown_event=shutdown_event,
            )
            # Wake early if shutdown is requested mid-interval; otherwise this
            # preserves the existing poll cadence between ticks.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
    finally:
        logger.info(
            "[worker] shutdown:start active_tasks=%s grace_seconds=%s",
            len(active_tasks),
            shutdown_grace_seconds,
        )
        await _drain_active_tasks(
            detached_tasks=detached_tasks,
            active_tasks=active_tasks,
            grace_seconds=shutdown_grace_seconds,
        )
        logger.info("[worker] shutdown:complete")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
