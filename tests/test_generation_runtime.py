from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException

import api.app as app_module
from api import generation_runtime
from api import worker as worker_module
from api.generation import persistence, stage1_runner
from api.generation.stage2_runner import finalize_stage2_with_timeout, retry_structured_card_before_release
from api.generation_runtime import (
    _invoke_planner,
    _stage1_mp_start_method,
    _stage1_planner_timeout_seconds,
    _stage2_finalize_timeout_seconds,
    default_planner,
    generation_status_from_plan_status,
    is_in_process_generation_enabled,
    run_stage1_planner,
)
from api.stage2_automation import Stage2AutomationError, Stage2AutomationUnavailableError
from support import (
    FakeStage2Automator,
    FakeStore,
    _build_request,
    finalized_result,
    grant_default_compliance,
    seed_default_profiles,
)


_ENVIRONMENT_VARS = ("APP_ENV", "ENVIRONMENT", "UNLXCK_ENV", "NODE_ENV")

# With the "spawn" start method the planner child cold-imports this test module
# (and its api.app import chain), which can take a couple of seconds in a slow
# CI sandbox. These tests exercise result/error delivery, not startup latency,
# so they need a timeout that comfortably clears spawn import cost. Production
# uses a 600s Stage 1 timeout, so import overhead is irrelevant there.
_SPAWN_TEST_TIMEOUT_SECONDS = 30.0


def _spawn_planner_returns(payload):
    return {"ok": payload["value"]}


def _spawn_planner_with_progress(payload, *, progress_callback=None):
    assert progress_callback is not None
    progress_callback("planner_started", "Planner started", "", {})
    return {"ok": True}


def _spawn_planner_raises(payload):
    raise RuntimeError("boom")


def _spawn_planner_hangs(payload):
    import time
    time.sleep(2)
    return {"ok": True}


def _spawn_planner_exits(payload):
    import os
    os._exit(0)


def _spawn_planner_returns_then_sleeps(payload):
    import time

    if payload.get("progress_callback"):
        payload["progress_callback"]("handoff_ready", "handoff ready", "", {})
    result = {"status": "ok", "value": payload["value"]}
    time.sleep(0.3)
    return result


def _spawn_planner_returns_large_result(payload):
    blob = "x" * 2_000_000
    return {"status": "ok", "blob": blob, "value": payload["value"]}


def _spawn_planner_returns_ready_plan(payload):
    return {"status": "ready", "plan_text": "draft"}


def _spawn_planner_returns_empty_plan(payload):
    return {"status": "ready", "plan_text": ""}


def _clear_environment_markers(monkeypatch):
    for var in _ENVIRONMENT_VARS:
        monkeypatch.delenv(var, raising=False)


def test_stage1_planner_timeout_default_is_600(monkeypatch):
    _clear_environment_markers(monkeypatch)
    monkeypatch.delenv("STAGE1_PLANNER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("APP_STAGE1_PLANNER_TIMEOUT_SECONDS", raising=False)
    assert _stage1_planner_timeout_seconds() == 600.0


@pytest.mark.parametrize("sentinel", ["", "0", "none", "None", "NONE"])
def test_stage1_planner_timeout_sentinels_disable_timeout_outside_production(monkeypatch, sentinel):
    _clear_environment_markers(monkeypatch)
    monkeypatch.setenv("STAGE1_PLANNER_TIMEOUT_SECONDS", sentinel)
    assert _stage1_planner_timeout_seconds() is None


def test_stage1_planner_timeout_sentinel_does_not_disable_in_production(monkeypatch):
    _clear_environment_markers(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("STAGE1_PLANNER_TIMEOUT_SECONDS", "0")
    assert _stage1_planner_timeout_seconds() == 600.0


def test_stage1_planner_timeout_invalid_falls_back_to_600(monkeypatch):
    _clear_environment_markers(monkeypatch)
    monkeypatch.setenv("STAGE1_PLANNER_TIMEOUT_SECONDS", "not-a-number")
    assert _stage1_planner_timeout_seconds() == 600.0


def test_stage1_planner_timeout_respects_valid_override(monkeypatch):
    _clear_environment_markers(monkeypatch)
    monkeypatch.setenv("STAGE1_PLANNER_TIMEOUT_SECONDS", "60")
    assert _stage1_planner_timeout_seconds() == 60.0


def test_stage1_planner_timeout_respects_fractional_positive_override(monkeypatch):
    _clear_environment_markers(monkeypatch)
    monkeypatch.setenv("STAGE1_PLANNER_TIMEOUT_SECONDS", "0.5")
    assert _stage1_planner_timeout_seconds() == 0.5


def test_stage1_planner_timeout_prefers_new_env_over_legacy(monkeypatch):
    _clear_environment_markers(monkeypatch)
    monkeypatch.setenv("STAGE1_PLANNER_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("APP_STAGE1_PLANNER_TIMEOUT_SECONDS", "60")
    assert _stage1_planner_timeout_seconds() == 600.0


def test_stage1_mp_start_method_defaults_to_spawn(monkeypatch):
    monkeypatch.delenv("UNLXCK_STAGE1_MP_START_METHOD", raising=False)
    assert _stage1_mp_start_method() == "spawn"


def test_stage1_mp_start_method_invalid_falls_back_to_spawn(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "invalid")
    assert _stage1_mp_start_method() == "spawn"


def test_stage1_run_planner_returns_result(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn")
    result = asyncio.run(
        run_stage1_planner(_spawn_planner_returns, {"value": 3}, timeout_seconds=_SPAWN_TEST_TIMEOUT_SECONDS)
    )
    assert result == {"ok": 3}


def test_stage1_run_planner_relays_progress(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn")
    codes: list[str] = []

    def callback(code, label, detail, meta):
        codes.append(code)

    result = asyncio.run(
        run_stage1_planner(
            _spawn_planner_with_progress,
            {"value": 1},
            progress_callback=callback,
            timeout_seconds=_SPAWN_TEST_TIMEOUT_SECONDS,
        )
    )
    assert result == {"ok": True}
    assert "planner_started" in codes


def test_stage1_run_planner_raises_controlled_runtime_error(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run_stage1_planner(_spawn_planner_raises, {}, timeout_seconds=_SPAWN_TEST_TIMEOUT_SECONDS))


def test_stage1_run_planner_preserves_child_traceback(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn")

    with pytest.raises(stage1_runner.Stage1PlannerError) as exc_info:
        asyncio.run(run_stage1_planner(_spawn_planner_raises, {}, timeout_seconds=_SPAWN_TEST_TIMEOUT_SECONDS))

    # The child-process stack trace must survive the parent re-raise so it can
    # be surfaced in structured logs for admin diagnostics.
    child_traceback = exc_info.value.child_traceback
    assert child_traceback is not None
    assert "Traceback" in child_traceback
    assert "boom" in child_traceback


def test_stage1_run_planner_timeout(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn")

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_stage1_planner(_spawn_planner_hangs, {}, timeout_seconds=0.1))


def test_stage1_run_planner_child_exit_without_result_raises_controlled_error(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn")

    with pytest.raises(RuntimeError, match="Stage 1 planner process exited without result"):
        asyncio.run(run_stage1_planner(_spawn_planner_exits, {}, timeout_seconds=_SPAWN_TEST_TIMEOUT_SECONDS))


def test_stage1_run_planner_returns_after_planner_returns(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn")
    result = asyncio.run(
        run_stage1_planner(
            _spawn_planner_returns_then_sleeps, {"value": 7}, timeout_seconds=_SPAWN_TEST_TIMEOUT_SECONDS
        )
    )
    assert result == {"status": "ok", "value": 7}


def test_stage1_run_planner_reads_large_result_from_queue(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE1_MP_START_METHOD", "spawn")
    codes: list[str] = []

    def callback(code, label, detail, meta):
        codes.append(code)

    result = asyncio.run(
        run_stage1_planner(
            _spawn_planner_returns_large_result,
            {"value": 9},
            progress_callback=callback,
            timeout_seconds=_SPAWN_TEST_TIMEOUT_SECONDS,
        )
    )
    assert result["status"] == "ok"
    assert result["value"] == 9
    assert len(result["blob"]) == 2_000_000
    assert "stage1_result_queue_received" in codes


def test_in_process_generation_default_is_worker_only(monkeypatch):
    monkeypatch.delenv("UNLXCK_ENABLE_IN_PROCESS_GENERATION", raising=False)
    assert is_in_process_generation_enabled() is False


def test_in_process_generation_env_override_enabled(monkeypatch):
    monkeypatch.setenv("UNLXCK_ENABLE_IN_PROCESS_GENERATION", "1")
    assert is_in_process_generation_enabled() is True


def test_worker_stale_timeout_default_is_300_when_env_unset(monkeypatch):
    monkeypatch.delenv("UNLXCK_GENERATION_WORKER_STALE_AFTER_SECONDS", raising=False)
    assert worker_module._worker_stale_after_seconds() == 300


def test_worker_stale_timeout_default_ignores_stage1_timeout(monkeypatch):
    monkeypatch.delenv("UNLXCK_GENERATION_WORKER_STALE_AFTER_SECONDS", raising=False)
    monkeypatch.setenv("STAGE1_PLANNER_TIMEOUT_SECONDS", "720")
    assert worker_module._worker_stale_after_seconds() == 300


def test_worker_stale_timeout_invalid_falls_back_to_300(monkeypatch):
    monkeypatch.setenv("UNLXCK_GENERATION_WORKER_STALE_AFTER_SECONDS", "not-a-number")
    assert worker_module._worker_stale_after_seconds() == 300


def test_worker_max_concurrency_default_is_1(monkeypatch):
    monkeypatch.delenv("UNLXCK_GENERATION_WORKER_MAX_CONCURRENT_JOBS", raising=False)
    assert worker_module._worker_max_concurrent_jobs() == 1


def test_worker_max_concurrency_invalid_falls_back_to_1(monkeypatch):
    monkeypatch.setenv("UNLXCK_GENERATION_WORKER_MAX_CONCURRENT_JOBS", "not-a-number")
    assert worker_module._worker_max_concurrent_jobs() == 1


def test_worker_tick_processes_queued_job_to_terminal_status():
    store = FakeStore()
    seed_default_profiles(store)
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="worker-processes-queued",
        source="self_serve",
        request_payload=_build_request().model_dump(mode="json"),
    )
    active_tasks: set[str] = set()
    detached_tasks: set[asyncio.Task[None]] = set()

    # Drive the tick and drain the detached job task inside a single event loop.
    # _tick() schedules the job as a detached task and returns without awaiting
    # it; if each step ran in its own asyncio.run(), that call's shutdown would
    # cancel the still-pending task before it could claim and process the job.
    # Production keeps one long-lived loop across ticks, so this mirrors it.
    async def _drive_until_drained() -> None:
        await worker_module._tick(
            store=store,
            active_tasks=active_tasks,
            detached_tasks=detached_tasks,
            stale_after_seconds=660,
            max_concurrent_jobs=1,
        )
        while detached_tasks:
            await asyncio.gather(*list(detached_tasks))
            await asyncio.sleep(0)

    original_builder = worker_module.build_default_stage2_automator
    worker_module.build_default_stage2_automator = lambda: FakeStage2Automator(result=finalized_result())
    try:
        asyncio.run(_drive_until_drained())
    finally:
        worker_module.build_default_stage2_automator = original_builder

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] in {"completed", "review_required"}
    assert job["completed_at"] is not None


def test_generation_job_request_parse_failure_is_diagnostic(caplog):
    store = FakeStore()
    seed_default_profiles(store)
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="worker-parse-failure",
        source="self_serve",
        request_payload={"_triage_resume_override": {"approved": True}},
    )
    planner_called = False

    def planner(payload: dict) -> dict:
        nonlocal planner_called
        planner_called = True
        return {"status": "ready"}

    with caplog.at_level("INFO"):
        asyncio.run(
            generation_runtime.run_generation_job(
                job_id=created["id"],
                store=store,
                planner_fn=planner,
                stage2=FakeStage2Automator(result_factory=finalized_result),
                active_tasks=set(),
            )
        )

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] == "failed"
    assert str(job["error"]).startswith("request_parse_failed:")
    assert planner_called is False
    milestone_codes = [milestone["code"] for milestone in job["progress_milestones"]]
    assert milestone_codes == [
        "job_loaded",
        "request_payload_parse_started",
        "request_payload_parse_failed",
    ]
    assert "worker:job_loaded" in caplog.text
    assert "worker:payload_raw" in caplog.text
    assert "worker:before_request_parse" in caplog.text
    assert "worker:request_parse_failed" in caplog.text
    assert "worker:after_request_parse" not in caplog.text


def test_stage2_finalize_timeout_default_is_1500(monkeypatch):
    # Must cover the worst-case sum of the per-request Stage 2 timeouts:
    # plan-text pass (210s) + structured first + repair (600s each).
    monkeypatch.delenv("APP_STAGE2_FINALIZE_TIMEOUT_SECONDS", raising=False)
    assert _stage2_finalize_timeout_seconds() == 1500.0


@pytest.mark.parametrize("sentinel", ["", "0", "none", "None", "NONE"])
def test_stage2_finalize_timeout_sentinels_disable_timeout(monkeypatch, sentinel):
    monkeypatch.setenv("APP_STAGE2_FINALIZE_TIMEOUT_SECONDS", sentinel)
    assert _stage2_finalize_timeout_seconds() is None


def test_stage2_finalize_timeout_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("APP_STAGE2_FINALIZE_TIMEOUT_SECONDS", "not-a-number")
    assert _stage2_finalize_timeout_seconds() == 1500.0


def test_stage2_finalize_timeout_respects_valid_override(monkeypatch):
    monkeypatch.setenv("APP_STAGE2_FINALIZE_TIMEOUT_SECONDS", "60")
    assert _stage2_finalize_timeout_seconds() == 60.0


def test_stage2_finalize_timeout_enforces_minimum_of_1(monkeypatch):
    monkeypatch.setenv("APP_STAGE2_FINALIZE_TIMEOUT_SECONDS", "0.5")
    assert _stage2_finalize_timeout_seconds() == 1.0


# ---------------------------------------------------------------------------
# Technical Stage 2 failures never publish the Stage 1 draft.
#
# A Stage 2 body that exists can still release with validator flags. A technical
# failure that produces no usable Stage 2 body fails the generation attempt and
# creates no plan row. Stage 1 remains internal planner input only.
# ---------------------------------------------------------------------------


def _run_stage2_failure_job(store, stage2, *, client_request_id, planner_fn=None):
    job = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id=client_request_id,
        source="self_serve",
        request_payload=_build_request().model_dump(mode="json"),
    )
    asyncio.run(
        generation_runtime.run_generation_job(
            job_id=job["id"],
            store=store,
            planner_fn=planner_fn or _spawn_planner_returns_ready_plan,
            stage2=stage2,
            active_tasks=set(),
        )
    )
    return store.get_generation_job(job["id"])


_STAGE2_RESPONSE_MILESTONES = (
    "stage2_model_response_received",
    "stage2_response_parse_started",
    "stage2_response_parsed",
    "stage2_result_ready",
    "stage2_validated",
    "stage2_flagged",
    "stage2_review_required",
)


def _assert_stage2_failure_without_plan(store, saved):
    assert saved["status"] == "failed"
    assert saved.get("plan_id") is None
    assert store.plans == {}
    codes = [milestone["code"] for milestone in saved["progress_milestones"]]
    assert "stage2_stage1_fallback" not in codes
    assert [code for code in _STAGE2_RESPONSE_MILESTONES if code in codes] == []
    return codes


def test_stage2_timeout_fails_without_publishing_stage1(monkeypatch):
    class HangingStage2:
        async def finalize(self, *, stage1_result: dict, log_context: dict | None = None) -> dict:
            await asyncio.sleep(1)
            return finalized_result()

    monkeypatch.setenv("APP_STAGE2_FINALIZE_TIMEOUT_SECONDS", "0.01")
    store = FakeStore()
    seed_default_profiles(store)
    saved = _run_stage2_failure_job(store, HangingStage2(), client_request_id="stage2-timeout")
    codes = _assert_stage2_failure_without_plan(store, saved)
    assert "stage2_drafting" in codes
    assert "stage2_model_call_started" in codes
    assert "stage2_finalizer_timeout" in codes


@pytest.mark.parametrize("configured_timeout", ["1500", "0"])
def test_stage2_hanging_request_uses_remaining_job_budget(monkeypatch, caplog, configured_timeout):
    monkeypatch.setenv("APP_STAGE2_FINALIZE_TIMEOUT_SECONDS", configured_timeout)
    cancelled = []

    class HangingStage2:
        async def finalize(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await finalize_stage2_with_timeout(
                stage2=HangingStage2(), stage1_result={},
                log_context={"job_id": "budget-job"}, remaining_runtime_seconds=0.01,
            )

    asyncio.run(scenario())
    assert cancelled == [True]
    assert "job_id=budget-job" in caplog.text
    assert "timeout_seconds=0.01" in caplog.text


def test_stage2_handoff_is_durable_before_provider_is_awaited(monkeypatch):
    # Force the normal milestone writes to coalesce: the handoff still must
    # be visible in storage while the provider request is in flight.
    monkeypatch.setenv("UNLXCK_PROGRESS_PERSIST_MIN_INTERVAL_SECONDS", "30")
    store = FakeStore()
    seed_default_profiles(store)
    observed = []

    class InspectingStage2:
        async def finalize(self, *, log_context, **kwargs):
            durable = store.get_generation_job(log_context["job_id"])
            handoff = [entry for entry in durable["progress_milestones"] if entry["code"] == "stage2_model_call_started"]
            observed.extend(handoff)
            raise Stage2AutomationError("test provider failure")

    saved = _run_stage2_failure_job(store, InspectingStage2(), client_request_id="durable-handoff")
    assert saved["status"] == "failed"
    assert len(observed) == 1
    assert observed[0]["meta"]["attempt_count"] == saved["attempt_count"]
    assert observed[0]["meta"]["remaining_runtime_seconds"] > 0


def test_stage2_provider_error_fails_without_publishing_stage1():
    store = FakeStore()
    seed_default_profiles(store)
    saved = _run_stage2_failure_job(
        store,
        FakeStage2Automator(error=Stage2AutomationError("Stage 2 model request failed. Check server logs.")),
        client_request_id="stage2-provider-error",
    )
    _assert_stage2_failure_without_plan(store, saved)
    assert "Stage 2 model request failed" in saved["error"]


def test_stage2_unavailable_fails_without_publishing_stage1():
    store = FakeStore()
    seed_default_profiles(store)
    saved = _run_stage2_failure_job(
        store,
        FakeStage2Automator(
            error=Stage2AutomationUnavailableError(
                "OPENAI_API_KEY is required for automated Stage 2 finalization."
            )
        ),
        client_request_id="stage2-unavailable",
    )
    _assert_stage2_failure_without_plan(store, saved)
    assert "OPENAI_API_KEY" in saved["error"]


def test_stage2_empty_incomplete_output_fails_without_publishing_stage1():
    store = FakeStore()
    seed_default_profiles(store)
    saved = _run_stage2_failure_job(
        store,
        FakeStage2Automator(
            error=Stage2AutomationError(
                "Stage 2 model response was incomplete and contained no usable plan text."
            )
        ),
        client_request_id="stage2-incomplete",
    )
    _assert_stage2_failure_without_plan(store, saved)
    assert "incomplete" in saved["error"]


def test_unexpected_stage2_exception_fails_without_publishing_stage1():
    class ExplodingStage2:
        async def finalize(self, *, stage1_result: dict, log_context: dict | None = None) -> dict:
            raise TypeError("unexpected crash inside the finalizer")

    store = FakeStore()
    seed_default_profiles(store)
    saved = _run_stage2_failure_job(
        store, ExplodingStage2(), client_request_id="stage2-unexpected"
    )
    _assert_stage2_failure_without_plan(store, saved)


def test_prompt_budget_failure_fails_without_publishing_stage1():
    store = FakeStore()
    seed_default_profiles(store)
    error = Stage2AutomationError("Stage 2 first_pass prompt too large: 214880 chars > 180000")
    saved = _run_stage2_failure_job(
        store, FakeStage2Automator(error=error), client_request_id="stage2-prompt-budget"
    )
    _assert_stage2_failure_without_plan(store, saved)


def test_flagged_release_does_not_claim_a_review_is_required():
    # publishable_with_flags releases to the athlete; the flags are for
    # asynchronous admin audit. Nothing is waiting on a review, so the run must
    # not emit a milestone saying one is needed.
    store = FakeStore()
    seed_default_profiles(store)

    saved = _run_stage2_failure_job(
        store,
        FakeStage2Automator(
            result=finalized_result(
                status="publishable_with_flags",
                stage2_status="stage2_failed",
            )
        ),
        client_request_id="stage2-flagged",
    )

    assert saved["status"] == "completed"
    plan = next(iter(store.plans.values()))
    assert plan["status"] == "publishable_with_flags"
    codes = [milestone["code"] for milestone in saved["progress_milestones"]]
    assert "stage2_flagged" in codes
    assert "stage2_review_required" not in codes
    # The response/parse milestones are true here — Stage 2 did return a plan.
    assert "stage2_model_response_received" in codes
    assert "stage2_response_parsed" in codes


def test_stage2_failure_still_fails_job_when_stage1_has_no_plan_text():
    # Stage 2 technical failure is terminal regardless of whether Stage 1 had text.
    store = FakeStore()
    seed_default_profiles(store)

    saved = _run_stage2_failure_job(
        store,
        FakeStage2Automator(error=Stage2AutomationError("Stage 2 model request failed")),
        client_request_id="stage2-no-stage1-plan",
        planner_fn=_spawn_planner_returns_empty_plan,
    )

    assert saved["status"] == "failed"
    assert "Stage 2 model request failed" in saved["error"]
    assert not store.plans


def test_invoke_planner_passes_progress_callback_when_supported():
    received: list[object] = []

    def planner(payload, *, progress_callback=None):
        received.append(progress_callback)
        return payload

    def callback(code, label, detail, meta):
        return None

    result = _invoke_planner(planner, {"ok": True}, callback)
    assert result == {"ok": True}
    assert received == [callback]


def test_invoke_planner_skips_progress_callback_for_legacy_planner():
    received: list[dict[str, bool]] = []

    def planner(payload):
        received.append(payload)
        return payload

    def callback(code, label, detail, meta):
        return None

    result = _invoke_planner(planner, {"ok": True}, callback)
    assert result == {"ok": True}
    assert received == [{"ok": True}]


def test_invoke_planner_does_not_swallow_internal_type_error():
    def planner(payload, *, progress_callback=None):
        raise TypeError("internal bug")

    with pytest.raises(TypeError, match="internal bug"):
        _invoke_planner(planner, {"ok": True}, lambda *args: None)


def test_default_planner_emits_diagnostic_milestones_before_generate_plan_sync(monkeypatch):
    emitted_codes: list[str] = []
    generate_plan_called = {"value": False}

    def fake_generate_plan_sync(payload, *, progress_callback=None):
        generate_plan_called["value"] = True
        return {"plan": "ok"}

    monkeypatch.setattr(stage1_runner, "generate_plan_sync", fake_generate_plan_sync)

    def callback(code, label, detail, meta):
        assert generate_plan_called["value"] is False
        emitted_codes.append(code)

    result = default_planner({"athlete": "x"}, progress_callback=callback)

    assert result == {"plan": "ok"}
    assert emitted_codes[:2] == [
        "stage1_default_planner_entered",
        "stage1_generate_plan_sync_entering",
    ]


def test_invoke_planner_with_app_default_planner_emits_full_stage1_diagnostics(monkeypatch):
    emitted_codes: list[str] = []

    def fake_generate_plan_sync(payload, *, progress_callback=None):
        return {"plan": "ok"}

    monkeypatch.setattr(stage1_runner, "generate_plan_sync", fake_generate_plan_sync)

    def callback(code, label, detail, meta):
        emitted_codes.append(code)

    result = _invoke_planner(app_module._default_planner, {"athlete": "x"}, callback)

    assert result == {"plan": "ok"}
    assert emitted_codes[:4] == [
        "stage1_planner_callable_entering",
        "stage1_planner_callable_supports_progress_callback",
        "stage1_default_planner_entered",
        "stage1_generate_plan_sync_entering",
    ]


def test_admin_latest_intake_job_fails_when_intake_id_is_missing():
    store = FakeStore()
    seed_default_profiles(store)
    request = _build_request().model_dump(mode="json")
    store.generation_jobs["job-1"] = {
        "id": "job-1",
        "athlete_id": "athlete-1",
        "status": "queued",
        "source": "admin_latest_intake",
        "request_payload": request,
        "intake_id": None,
    }
    asyncio.run(
        generation_runtime.run_generation_job(
            job_id="job-1",
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )
    assert store.generation_jobs["job-1"]["status"] == "failed"
    assert store.generation_jobs["job-1"]["error"] == "admin latest intake job is missing intake_id"


def test_admin_latest_intake_job_fails_when_linked_intake_is_for_different_athlete():
    store = FakeStore()
    seed_default_profiles(store)
    request = _build_request().model_dump(mode="json")
    intake = store.create_intake("athlete-2", _build_request())
    store.generation_jobs["job-1"] = {
        "id": "job-1",
        "athlete_id": "athlete-1",
        "status": "queued",
        "source": "admin_latest_intake",
        "request_payload": request,
        "intake_id": intake["id"],
    }
    asyncio.run(
        generation_runtime.run_generation_job(
            job_id="job-1",
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )
    assert store.generation_jobs["job-1"]["status"] == "failed"
    assert store.generation_jobs["job-1"]["error"] == "admin latest intake job intake belongs to a different athlete"


def test_admin_latest_intake_job_accepts_semantically_equivalent_linked_payload():
    store = FakeStore()
    seed_default_profiles(store)
    request = _build_request().model_dump(mode="json")
    intake = store.create_intake("athlete-1", _build_request())
    linked_payload = dict(intake["intake"])
    linked_payload.pop("no_scheduled_fight", None)
    linked_payload.pop("open_camp_weeks", None)
    store.update_intake(
        intake["id"],
        intake=linked_payload,
        fight_date=intake["fight_date"],
        technical_style=intake["technical_style"],
    )
    store.generation_jobs["job-1"] = {
        "id": "job-1",
        "athlete_id": "athlete-1",
        "status": "queued",
        "source": "admin_latest_intake",
        "request_payload": request,
        "intake_id": intake["id"],
    }
    asyncio.run(
        generation_runtime.run_generation_job(
            job_id="job-1",
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )
    assert store.generation_jobs["job-1"]["status"] == "completed"


def test_admin_latest_intake_job_fails_when_linked_payload_differs_from_request_payload():
    store = FakeStore()
    seed_default_profiles(store)
    request = _build_request().model_dump(mode="json")
    linked_request = _build_request({"fatigue_level": "high"})
    intake = store.create_intake("athlete-1", linked_request)
    store.generation_jobs["job-1"] = {
        "id": "job-1",
        "athlete_id": "athlete-1",
        "status": "queued",
        "source": "admin_latest_intake",
        "request_payload": request,
        "intake_id": intake["id"],
    }
    asyncio.run(
        generation_runtime.run_generation_job(
            job_id="job-1",
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )
    assert store.generation_jobs["job-1"]["status"] == "failed"
    assert store.generation_jobs["job-1"]["error"] == "admin latest intake job request_payload does not match linked intake payload"


def test_terminal_success_without_plan_id_is_downgraded_to_failed_with_error_message():
    store = FakeStore()
    seed_default_profiles(store)
    request_payload = _build_request().model_dump(mode="json")
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="terminal-missing-plan-id",
        source="self_serve",
        request_payload=request_payload,
    )

    original_update_generation_job = store.update_generation_job

    def flaky_update_generation_job(job_id: str, **changes: dict) -> dict:
        updated = original_update_generation_job(job_id, **changes)
        if "final_result" in changes:
            updated = original_update_generation_job(job_id, plan_id=None)
        return updated

    store.update_generation_job = flaky_update_generation_job  # type: ignore[assignment]

    asyncio.run(
        generation_runtime.run_generation_job(
            job_id=created["id"],
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] == "failed"
    assert isinstance(job.get("error"), str)
    assert job["error"].strip() == "Plan was saved but the generation job lost its plan_id. Open plan history or contact support."


def test_terminal_success_with_deleted_plan_row_is_downgraded_to_failed():
    store = FakeStore()
    seed_default_profiles(store)
    request_payload = _build_request().model_dump(mode="json")
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="terminal-deleted-plan-row",
        source="self_serve",
        request_payload=request_payload,
    )

    original_get_plan = store.get_plan

    def flaky_get_plan(plan_id: str):  # type: ignore[no-untyped-def]
        row = original_get_plan(plan_id)
        if row is not None:
            # Simulate the plan row being deleted right after it was persisted.
            return None
        return row

    store.get_plan = flaky_get_plan  # type: ignore[assignment]

    asyncio.run(
        generation_runtime.run_generation_job(
            job_id=created["id"],
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] == "failed"


@pytest.mark.parametrize(
    ("plan_status", "expected_generation_status"),
    [
        ("generated", "completed"),
        ("ready", "completed"),
        ("publishable_with_flags", "completed"),
        ("triage_blocked", "review_required"),
        ("archived", "completed"),
        ("held_for_review", "review_required"),
        ("review_required", "review_required"),
        ("medical_hold", "review_required"),
        ("restricted_rehab_only", "review_required"),
        ("needs_review", "review_required"),
    ],
)
def test_generation_status_from_plan_status_mapper(plan_status: str, expected_generation_status: str):
    assert generation_status_from_plan_status(plan_status) == expected_generation_status


def test_generation_job_status_never_uses_legacy_plan_status_values():
    for legacy_status in ("publishable_with_flags", "held_for_review"):
        mapped = generation_status_from_plan_status(legacy_status)
        assert mapped != legacy_status


def test_generation_status_from_unknown_plan_status_requires_review():
    assert generation_status_from_plan_status("new_clinical_hold") == "review_required"


# ---------------------------------------------------------------------------
# Direct seam coverage added ahead of the generation_runtime split refactor.
# These pin behaviour that was previously only exercised indirectly, so later
# code-movement PRs have a tight regression net. No production code changes.
# ---------------------------------------------------------------------------


class _RecordingStore:
    """Minimal store stub that records update_generation_job calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refresh_flags: list[bool] = []

    def update_generation_job(
        self, job_id: str, *, refresh: bool = True, **changes: object
    ) -> dict:
        self.calls.append((job_id, dict(changes)))
        self.refresh_flags.append(refresh)
        return {"id": job_id, **changes} if refresh else {}


class _FailingUpdateStore:
    """Store stub whose update_generation_job always raises."""

    def update_generation_job(
        self, job_id: str, *, refresh: bool = True, **changes: object
    ) -> dict:
        raise RuntimeError("db down")


class _CapacityStore:
    """Store stub controlling count_active_generation_jobs for scheduler tests."""

    def __init__(self, *, count_result: int = 0, count_exc: Exception | None = None) -> None:
        self._count_result = count_result
        self._count_exc = count_exc

    def count_active_generation_jobs(self, *, stale_after_seconds: int) -> int:
        if self._count_exc is not None:
            raise self._count_exc
        return self._count_result


# --- should_skip_stage2 (triage skip decision) -----------------------------


def test_should_skip_stage2_blocks_triage_blocked_status():
    assert generation_runtime.should_skip_stage2({"status": "triage_blocked"}) is True


def test_should_skip_stage2_triage_blocked_status_allows_with_override():
    assert (
        generation_runtime.should_skip_stage2(
            {"status": "triage_blocked"}, allow_triage_resume_override=True
        )
        is False
    )


def test_should_skip_stage2_blocks_when_injury_triage_should_block():
    stage1 = {"status": "ready", "injury_triage": {"should_block_stage2": True}}
    assert generation_runtime.should_skip_stage2(stage1) is True
    assert generation_runtime.should_skip_stage2(stage1, allow_triage_resume_override=True) is False


@pytest.mark.parametrize("mode", ["medical_hold", "restricted_rehab_only", "needs_review"])
def test_should_skip_stage2_blocks_on_injury_triage_mode(mode: str):
    stage1 = {"status": "ready", "injury_triage": {"mode": mode}}
    assert generation_runtime.should_skip_stage2(stage1) is True
    assert generation_runtime.should_skip_stage2(stage1, allow_triage_resume_override=True) is False


def test_should_skip_stage2_blocks_on_why_log_injury_triage():
    stage1 = {"why_log": {"injury_triage": {"should_block_stage2": True}}}
    assert generation_runtime.should_skip_stage2(stage1) is True


def test_should_skip_stage2_allows_clean_ready_result():
    assert generation_runtime.should_skip_stage2({"status": "ready"}) is False


# --- is_openai_quota_error (admin-safe error mapping) ----------------------


@pytest.mark.parametrize(
    "message",
    [
        "insufficient_quota",
        "You exceeded your current quota, please check your plan",
        "OpenAI quota/rate limit reached",
        "Error 429: quota exceeded for this org",
    ],
)
def test_is_openai_quota_error_detects_quota_messages(message: str):
    assert generation_runtime.is_openai_quota_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "connection reset by peer",
        "429 Too Many Requests",  # rate limit without quota wording
        "validation failed",
    ],
)
def test_is_openai_quota_error_ignores_non_quota_messages(message: str):
    assert generation_runtime.is_openai_quota_error(Exception(message)) is False


# --- _compact_generation_job_final_result (final_result shaping) -----------


def test_compact_final_result_drops_non_essential_keys_for_real_plan():
    final_result = {
        "status": "ready",
        "stage2_status": "stage2_pass",
        "plan_text": "# big plan text",
        "why_log": {"x": 1},
        "full_name": "Jordan",
    }
    compact = generation_runtime._compact_generation_job_final_result(final_result)
    assert compact == {"status": "ready", "stage2_status": "stage2_pass"}


def test_compact_final_result_preserves_triage_context_for_held_outcome():
    final_result = {
        "status": "medical_hold",
        "stage2_status": "skipped",
        "plan_text": "# should be dropped",
        "why_log": {"injury_triage": {"mode": "medical_hold"}},
        "injury_triage": {"mode": "medical_hold"},
        "full_name": "Jordan",
    }
    compact = generation_runtime._compact_generation_job_final_result(final_result)
    assert compact == {
        "status": "medical_hold",
        "stage2_status": "skipped",
        "why_log": {"injury_triage": {"mode": "medical_hold"}},
        "injury_triage": {"mode": "medical_hold"},
        "full_name": "Jordan",
    }


# --- build_progress_recorder (milestone persistence) -----------------------


def test_build_progress_recorder_persists_milestone_and_heartbeat():
    store = _RecordingStore()
    milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store
    )
    callback("stage1_planner_invoked", "Stage 1 planner invoked", "detail", {"k": "v"})

    assert len(store.calls) == 1
    job_id, changes = store.calls[0]
    assert job_id == "job-x"
    assert changes["heartbeat_at"]
    persisted = changes["progress_milestones"]
    assert persisted[-1]["code"] == "stage1_planner_invoked"
    assert persisted[-1]["meta"] == {"k": "v"}
    assert milestones[-1]["code"] == "stage1_planner_invoked"


def test_build_progress_recorder_swallows_persist_failures():
    store = _FailingUpdateStore()
    milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store
    )

    # Must not raise even though the store write fails.
    callback("stage1_planner_invoked", "label", "detail", {})

    assert milestones[-1]["code"] == "stage1_planner_invoked"


def test_build_progress_recorder_respects_should_persist_guard():
    store = _RecordingStore()
    milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store, should_persist=lambda: False
    )
    callback("stage1_planner_invoked", "label", "detail", {})

    assert store.calls == []
    assert milestones == []


def test_build_progress_recorder_caps_persisted_milestones():
    store = _RecordingStore()
    cap = generation_runtime._MAX_PERSISTED_MILESTONES
    milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store, min_persist_interval_seconds=0
    )
    for index in range(cap + 5):
        callback(f"code-{index}", "label", "detail", {})

    assert len(milestones) == cap
    last_snapshot = store.calls[-1][1]["progress_milestones"]
    assert len(last_snapshot) == cap
    assert milestones[-1]["code"] == f"code-{cap + 4}"


# --- progress-path write amplification (2026-09-15 outage regression) ------
#
# A single generation drove ~280 PATCHes plus ~285 full-row GETs against
# generation_jobs in five minutes, saturating the database. Two causes: the
# recorder persisted on every emit, and update_generation_job re-read the whole
# row (select="*", so every TOASTed blob) after every write.


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_progress_persist_never_refreshes_the_full_job_row():
    store = _RecordingStore()
    _milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store
    )
    callback("stage1_planner_invoked", "label", "detail", {})

    assert store.refresh_flags == [False]


def test_progress_persist_writes_only_milestones_and_heartbeat():
    """Progress writes must never carry heavyweight generation payloads."""
    store = _RecordingStore()
    _milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store
    )
    callback("stage1_planner_invoked", "label", "detail", {})

    _job_id, changes = store.calls[0]
    assert set(changes) == {"progress_milestones", "heartbeat_at"}
    for heavy in ("stage1_result", "stage2_payload", "final_result", "request_payload"):
        assert heavy not in changes


def test_repeated_progress_events_produce_bounded_writes():
    """200 rapid emits must not become 200 database writes."""
    clock = _FakeClock()
    store = _RecordingStore()
    _milestones, callback, flush = generation_runtime.build_progress_recorder(
        job_id="job-x",
        store=store,
        min_persist_interval_seconds=5.0,
        monotonic=clock,
    )

    # 200 emits spread over 10 simulated seconds — the old code wrote 200 times.
    for index in range(200):
        callback(f"code-{index}", "label", "detail", {})
        clock.advance(0.05)
    flush()

    # 1 leading write + 1 at the 5s boundary + 1 flush.
    assert len(store.calls) <= 4
    assert all(refresh is False for refresh in store.refresh_flags)


def test_throttled_progress_events_are_coalesced_not_lost():
    """A skipped write must still be represented in the next snapshot."""
    clock = _FakeClock()
    store = _RecordingStore()
    _milestones, callback, flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store, min_persist_interval_seconds=5.0, monotonic=clock
    )

    callback("first", "label", "detail", {})
    callback("throttled", "label", "detail", {})
    assert len(store.calls) == 1  # second emit was throttled

    flush()
    persisted = [entry["code"] for entry in store.calls[-1][1]["progress_milestones"]]
    assert persisted == ["first", "throttled"]


def test_progress_flush_is_a_noop_when_nothing_is_pending():
    clock = _FakeClock()
    store = _RecordingStore()
    _milestones, callback, flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store, min_persist_interval_seconds=5.0, monotonic=clock
    )
    callback("first", "label", "detail", {})
    assert len(store.calls) == 1

    flush()
    flush()
    assert len(store.calls) == 1


def test_failing_persists_are_rate_limited_too():
    """A database outage must not turn every emit into another PATCH attempt.

    Throttling on success alone leaves last_attempt_at unset while the store is
    down, so each subsequent milestone retries immediately — full-rate writes
    aimed at an already-struggling database.
    """

    class _AlwaysFailingStore:
        def __init__(self) -> None:
            self.attempts = 0

        def update_generation_job(self, job_id, *, refresh=True, **changes):
            self.attempts += 1
            raise RuntimeError("db down")

    clock = _FakeClock()
    store = _AlwaysFailingStore()
    _milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store, min_persist_interval_seconds=5.0, monotonic=clock
    )

    # 200 emits across 10 simulated seconds, every single write raising.
    for index in range(200):
        callback(f"code-{index}", "label", "detail", {})
        clock.advance(0.05)

    # Bounded by the interval (t=0 and t=5), not by the emit count.
    assert store.attempts <= 3
    assert 200 // store.attempts > 50  # sanity: nowhere near one attempt per emit


def test_failing_persists_still_flush_the_full_backlog_once_the_store_recovers():
    """Rate-limiting failures must not discard what those failures did not write."""

    class _RecoveringStore(_RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.down = True

        def update_generation_job(self, job_id, *, refresh=True, **changes):
            if self.down:
                raise RuntimeError("db down")
            return super().update_generation_job(job_id, refresh=refresh, **changes)

    clock = _FakeClock()
    store = _RecoveringStore()
    _milestones, callback, flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store, min_persist_interval_seconds=5.0, monotonic=clock
    )
    for code in ("first", "second", "third"):
        callback(code, "label", "detail", {})
        clock.advance(0.05)
    assert store.calls == []  # nothing landed while the store was down

    store.down = False
    flush()

    persisted = [entry["code"] for entry in store.calls[-1][1]["progress_milestones"]]
    assert persisted == ["first", "second", "third"]


def test_progress_flush_retries_after_a_failed_persist():
    """A failed write must leave the milestones pending for the flush."""

    class _FlakyStore(_RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next = True

        def update_generation_job(self, job_id, *, refresh=True, **changes):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("db down")
            return super().update_generation_job(job_id, refresh=refresh, **changes)

    clock = _FakeClock()
    store = _FlakyStore()
    _milestones, callback, flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store, min_persist_interval_seconds=5.0, monotonic=clock
    )
    callback("first", "label", "detail", {})
    assert store.calls == []  # the write raised and was swallowed

    flush()
    persisted = [entry["code"] for entry in store.calls[-1][1]["progress_milestones"]]
    assert persisted == ["first"]


def test_flush_still_persists_milestones_recorded_before_a_timeout():
    """A timeout after a throttled emit must not lose that milestone.

    should_persist closes when the job times out or is cancelled. The pending
    milestones were all accepted while it was still open, so they must still
    reach the row — but heartbeat_at must NOT be bumped, or a timed-out job
    would look freshly alive to the staleness sweep.
    """
    clock = _FakeClock()
    store = _RecordingStore()
    allow = {"value": True}
    _milestones, callback, flush = generation_runtime.build_progress_recorder(
        job_id="job-x",
        store=store,
        should_persist=lambda: allow["value"],
        min_persist_interval_seconds=5.0,
        monotonic=clock,
    )
    callback("first", "label", "detail", {})
    callback("throttled", "label", "detail", {})
    allow["value"] = False  # stage 1 times out here

    flush()

    _job_id, changes = store.calls[-1]
    persisted = [entry["code"] for entry in changes["progress_milestones"]]
    assert persisted == ["first", "throttled"]
    assert "heartbeat_at" not in changes


@pytest.mark.parametrize(
    "scenario",
    [
        "no_guard",
        "guard_still_open",  # _fail_claimed_job: job failed, guard reads "live"
        "guard_closed",      # stage 1 timeout / cancel
    ],
)
def test_flush_never_writes_heartbeat_whatever_the_guard_says(scenario):
    """Liveness belongs to the heartbeat loop, not the end-of-run flush.

    _fail_claimed_job marks a job failed without setting stage1_timed_out or
    cancelled, so a guard-based check still reads "live" and would stamp a
    fresh heartbeat onto an already-failed job.
    """
    clock = _FakeClock()
    store = _RecordingStore()
    open_flag = {"value": True}
    guard = None if scenario == "no_guard" else (lambda: open_flag["value"])
    _milestones, callback, flush = generation_runtime.build_progress_recorder(
        job_id="job-x",
        store=store,
        should_persist=guard,
        min_persist_interval_seconds=5.0,
        monotonic=clock,
    )
    callback("first", "label", "detail", {})
    callback("throttled", "label", "detail", {})
    if scenario == "guard_closed":
        open_flag["value"] = False

    flush()

    flush_changes = store.calls[-1][1]
    assert "heartbeat_at" not in flush_changes
    assert [entry["code"] for entry in flush_changes["progress_milestones"]] == [
        "first",
        "throttled",
    ]


def test_ordinary_progress_writes_still_refresh_heartbeat():
    """Only the flush drops heartbeat_at; in-run writes keep it."""
    store = _RecordingStore()
    _milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store
    )
    callback("first", "label", "detail", {})

    assert store.calls[0][1]["heartbeat_at"]


def test_guard_still_blocks_new_milestones_after_a_timeout():
    """Post-timeout emits must not be recorded at all."""
    clock = _FakeClock()
    store = _RecordingStore()
    allow = {"value": True}
    milestones, callback, flush = generation_runtime.build_progress_recorder(
        job_id="job-x",
        store=store,
        should_persist=lambda: allow["value"],
        min_persist_interval_seconds=5.0,
        monotonic=clock,
    )
    callback("first", "label", "detail", {})
    allow["value"] = False
    callback("after_timeout", "label", "detail", {})
    flush()

    assert [entry["code"] for entry in milestones] == ["first"]
    for _job_id, changes in store.calls:
        codes = [entry["code"] for entry in changes["progress_milestones"]]
        assert "after_timeout" not in codes


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity", "-Infinity"])
def test_non_finite_persist_interval_env_falls_back_to_default(monkeypatch, raw):
    """float() accepts these; NaN would silently disable the throttle entirely.

    Every comparison against NaN is False, so `elapsed < interval` never holds
    and each emit writes again — the exact behaviour this module prevents.
    """
    monkeypatch.setenv("UNLXCK_PROGRESS_PERSIST_MIN_INTERVAL_SECONDS", raw)

    assert (
        generation_runtime._persist_min_interval_seconds()
        == generation_runtime._DEFAULT_PERSIST_MIN_INTERVAL_SECONDS
    )


def test_nan_persist_interval_env_does_not_disable_throttling(monkeypatch):
    """End-to-end guard: the NaN fallback must actually bound writes."""
    monkeypatch.setenv("UNLXCK_PROGRESS_PERSIST_MIN_INTERVAL_SECONDS", "nan")
    clock = _FakeClock()
    store = _RecordingStore()
    _milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x", store=store, monotonic=clock
    )
    for index in range(100):
        callback(f"code-{index}", "label", "detail", {})
        clock.advance(0.05)

    assert len(store.calls) <= 3


def test_valid_persist_interval_env_is_still_honoured(monkeypatch):
    monkeypatch.setenv("UNLXCK_PROGRESS_PERSIST_MIN_INTERVAL_SECONDS", "12.5")

    assert generation_runtime._persist_min_interval_seconds() == 12.5


def test_progress_persist_interval_is_capped():
    """An oversized env value must not push writes past the staleness window."""
    clock = _FakeClock()
    store = _RecordingStore()
    _milestones, callback, _flush = generation_runtime.build_progress_recorder(
        job_id="job-x",
        store=store,
        min_persist_interval_seconds=10_000,
        monotonic=clock,
    )
    callback("first", "label", "detail", {})
    clock.advance(generation_runtime._MAX_PERSIST_MIN_INTERVAL_SECONDS + 1)
    callback("second", "label", "detail", {})

    assert len(store.calls) == 2


# --- schedule_generation_job_if_needed (isolated branches) -----------------


def test_schedule_worker_only_mode_does_not_schedule_or_consume_capacity():
    store = _CapacityStore(count_result=0)
    background_tasks = BackgroundTasks()
    active_tasks: set[str] = set()
    job = {"id": "job-1", "status": "queued"}

    returned = asyncio.run(
        generation_runtime.schedule_generation_job_if_needed(
            job=job,
            background_tasks=background_tasks,
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=active_tasks,
            enable_in_process_generation=False,
            stale_job_checker=lambda j, **kw: False,
            stale_after_seconds=300,
        )
    )

    assert returned == job
    assert background_tasks.tasks == []
    assert active_tasks == set()


def test_schedule_defers_when_capacity_reached(monkeypatch):
    monkeypatch.setenv("APP_GENERATION_MAX_CONCURRENT_JOBS", "1")
    store = _CapacityStore(count_result=1)
    background_tasks = BackgroundTasks()
    active_tasks: set[str] = set()
    job = {"id": "job-1", "status": "queued"}

    returned = asyncio.run(
        generation_runtime.schedule_generation_job_if_needed(
            job=job,
            background_tasks=background_tasks,
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=active_tasks,
            enable_in_process_generation=True,
            stale_job_checker=lambda j, **kw: False,
            stale_after_seconds=300,
        )
    )

    assert returned == job
    assert background_tasks.tasks == []
    assert "job-1" not in active_tasks


def test_schedule_defers_when_capacity_count_unavailable(monkeypatch):
    monkeypatch.setenv("APP_GENERATION_MAX_CONCURRENT_JOBS", "1")
    store = _CapacityStore(
        count_exc=HTTPException(status_code=503, detail="capacity check unavailable")
    )
    background_tasks = BackgroundTasks()
    active_tasks: set[str] = set()
    job = {"id": "job-1", "status": "queued"}

    returned = asyncio.run(
        generation_runtime.schedule_generation_job_if_needed(
            job=job,
            background_tasks=background_tasks,
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=active_tasks,
            enable_in_process_generation=True,
            stale_job_checker=lambda j, **kw: False,
            stale_after_seconds=300,
        )
    )

    assert returned == job
    assert background_tasks.tasks == []
    assert "job-1" not in active_tasks


# --- Stage 2 token/cost metadata persistence -------------------------------


_STAGE2_COST_FIXTURE = {
    "stage2_model": "gpt-5-mini",
    "stage2_input_tokens": 1000,
    "stage2_output_tokens": 2000,
    "stage2_total_tokens": 3000,
    "stage2_estimated_cost_usd": 0.123456,
    "stage2_attempt_count": 1,
    "stage2_response_id": "resp_abc",
    "stage2_cost_recorded_at": "2026-06-06T00:00:00+00:00",
}


def test_successful_generation_records_stage2_cost_on_job():
    store = FakeStore()
    seed_default_profiles(store)
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="stage2-cost-success",
        source="self_serve",
        request_payload=_build_request().model_dump(mode="json"),
    )

    asyncio.run(
        generation_runtime.run_generation_job(
            job_id=created["id"],
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(
                result=finalized_result(stage2_cost=dict(_STAGE2_COST_FIXTURE))
            ),
            active_tasks=set(),
        )
    )

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] == "completed"
    assert job["stage2_model"] == "gpt-5-mini"
    assert job["stage2_total_tokens"] == 3000
    assert job["stage2_estimated_cost_usd"] == 0.123456
    assert job["stage2_response_id"] == "resp_abc"


def test_failed_stage2_records_available_cost_on_job():
    store = FakeStore()
    seed_default_profiles(store)
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="stage2-cost-failure",
        source="self_serve",
        request_payload=_build_request().model_dump(mode="json"),
    )

    error = Stage2AutomationError("Stage 2 model request failed. Check server logs.")
    error.stage2_cost = {
        "stage2_model": "gpt-5-mini",
        "stage2_input_tokens": 500,
        "stage2_output_tokens": None,
        "stage2_total_tokens": None,
        "stage2_estimated_cost_usd": 0.0,
        "stage2_attempt_count": 1,
        "stage2_response_id": None,
        "stage2_cost_recorded_at": "2026-06-06T00:00:00+00:00",
    }

    asyncio.run(
        generation_runtime.run_generation_job(
            job_id=created["id"],
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(error=error),
            active_tasks=set(),
        )
    )

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] == "failed"
    assert job["stage2_model"] == "gpt-5-mini"
    assert job["stage2_input_tokens"] == 500
    assert job["stage2_output_tokens"] is None


def test_missing_stage2_cost_metadata_does_not_crash_generation():
    # A finalized result without stage2_cost (e.g. legacy automator) must still
    # complete the job; the cost columns simply stay unset.
    store = FakeStore()
    seed_default_profiles(store)
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="stage2-cost-absent",
        source="self_serve",
        request_payload=_build_request().model_dump(mode="json"),
    )

    asyncio.run(
        generation_runtime.run_generation_job(
            job_id=created["id"],
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result=finalized_result()),
            active_tasks=set(),
        )
    )

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] == "completed"
    assert job.get("stage2_model") is None


# --- post-terminal cleanup must not undo saved plan/job state --------------


def test_cleanup_failure_after_terminal_persist_preserves_plan_and_job():
    store = FakeStore()
    seed_default_profiles(store)
    request_payload = _build_request().model_dump(mode="json")
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="cleanup-failure",
        source="self_serve",
        request_payload=request_payload,
    )

    def boom_clear(athlete_id: str) -> None:
        raise RuntimeError("cleanup boom")

    store.clear_onboarding_draft = boom_clear  # type: ignore[assignment]

    asyncio.run(
        generation_runtime.run_generation_job(
            job_id=created["id"],
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] == "completed"
    plan_id = job.get("plan_id")
    assert plan_id
    assert store.get_plan(plan_id) is not None


def test_cleanup_timeout_after_terminal_persist_preserves_plan_and_job(monkeypatch):
    monkeypatch.setattr(persistence, "_POST_PERSIST_CLEANUP_TIMEOUT_SECONDS", 0.05)
    store = FakeStore()
    seed_default_profiles(store)
    request_payload = _build_request().model_dump(mode="json")
    created = store.create_or_get_generation_job(
        athlete_id="athlete-1",
        client_request_id="cleanup-timeout",
        source="self_serve",
        request_payload=request_payload,
    )

    def slow_clear(athlete_id: str) -> None:
        time.sleep(0.3)

    store.clear_onboarding_draft = slow_clear  # type: ignore[assignment]

    asyncio.run(
        generation_runtime.run_generation_job(
            job_id=created["id"],
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )

    job = store.get_generation_job(created["id"])
    assert job is not None
    assert job["status"] == "completed"
    plan_id = job.get("plan_id")
    assert plan_id
    assert store.get_plan(plan_id) is not None


# ---------------------------------------------------------------------------
# Canonical age: the worker re-derives it from the profile it reloads
# ---------------------------------------------------------------------------
#
# The worker replaces `request_body.athlete.age` once, immediately after parsing
# the stored payload, and everything downstream reads that one object: the
# profile draft refreshed from it, the intake created from it, and the Stage 1
# payload built from it. The intake and the draft are the two observable copies
# (Stage 1 runs in a separate process, so its payload cannot be captured here),
# and they are written on either side of the planner call — so agreeing on the
# corrected age is what shows the replacement happened before any of it.


def _dob_for_age(years: int) -> str:
    today = datetime.now(timezone.utc).date()
    return today.replace(year=today.year - years).isoformat()


def _queue_legacy_job(store: FakeStore, *, stored_age: int | None) -> None:
    """A job queued with an age the route never normalised."""
    request = _build_request({"athlete": {"age": stored_age}}).model_dump(mode="json")
    store.generation_jobs["job-1"] = {
        "id": "job-1",
        "athlete_id": "athlete-1",
        "status": "queued",
        "source": "self_serve",
        "request_payload": request,
        "intake_id": None,
    }


def _run_legacy_job(store: FakeStore) -> None:
    asyncio.run(
        generation_runtime.run_generation_job(
            job_id="job-1",
            store=store,
            planner_fn=app_module._noop_planner,
            stage2=FakeStage2Automator(result_factory=finalized_result),
            active_tasks=set(),
        )
    )


@pytest.mark.parametrize(("stored_age", "profile_age"), [(25, 15), (15, 25)])
def test_worker_corrects_a_legacy_queued_job_age_from_the_current_profile(stored_age, profile_age):
    """A payload stored before this rule existed cannot outvote the profile.

    Old queued jobs, admin retries and generations replayed from an old intake
    all reach the worker without passing the route's normalisation, so the
    worker re-derives the age itself before any of it is used. The correction
    runs in both directions: an over-stated age is not merely clamped down.
    """
    store = FakeStore()
    seed_default_profiles(store)
    grant_default_compliance(store, date_of_birth=_dob_for_age(profile_age))
    _queue_legacy_job(store, stored_age=stored_age)

    _run_legacy_job(store)

    assert store.generation_jobs["job-1"]["status"] == "completed"
    assert store.get_latest_intake("athlete-1")["intake"]["athlete"]["age"] == profile_age


def test_worker_corrects_the_age_before_creating_a_new_intake():
    # The intake this job creates is a record a future generation can be
    # replayed from, so a stale age written here would outlive the job and
    # contaminate the next one.
    store = FakeStore()
    seed_default_profiles(store)
    grant_default_compliance(store, date_of_birth=_dob_for_age(15))
    _queue_legacy_job(store, stored_age=25)
    assert store.generation_jobs["job-1"]["intake_id"] is None

    _run_legacy_job(store)

    created_intake_id = store.generation_jobs["job-1"]["intake_id"]
    assert created_intake_id
    assert store.get_intake(created_intake_id)["intake"]["athlete"]["age"] == 15


def test_worker_drops_a_stored_age_it_cannot_vouch_for():
    # An unreadable date of birth makes the age unknown. Unknown is a usable
    # answer; the client's claim is not, so it is never the fallback.
    store = FakeStore()
    seed_default_profiles(store)
    grant_default_compliance(store)
    store.profiles["athlete-1"]["date_of_birth"] = "not-a-date"
    _queue_legacy_job(store, stored_age=25)

    _run_legacy_job(store)

    assert store.get_latest_intake("athlete-1")["intake"]["athlete"]["age"] is None


def test_worker_keeps_an_age_that_already_matches_the_profile():
    # The normal path: the route already normalised this payload, so the worker's
    # second pass is a no-op rather than a second source of drift.
    store = FakeStore()
    seed_default_profiles(store)
    grant_default_compliance(store, date_of_birth=_dob_for_age(25))
    _queue_legacy_job(store, stored_age=25)

    _run_legacy_job(store)

    assert store.get_latest_intake("athlete-1")["intake"]["athlete"]["age"] == 25


# ---------------------------------------------------------------------------
# The enhanced card is attempted before the job completes, at most once per text.
#
# The job completing is what moves the athlete off the generation screen, so
# a card Stage 2 never attempted (held plan) or could not attempt (converter
# crash) is built first. A card the validators already rejected is not
# re-converted: the same text would roll the same dice for 4-7 more minutes,
# and the athlete is served the validated deterministic card instead.
# ---------------------------------------------------------------------------


_FAILED_INLINE_CARD_REPORT = {
    "errors": [],
    "warnings": [],
    "structured_plan": {
        "status": "invalid_fallback_used",
        "errors": ["faithfulness: LOCKED_CONTENT: 'Body Attack Opportunity' lost required source content"],
        "warnings": [],
        "schema_version": None,
    },
}

_CRASHED_INLINE_CARD_REPORT = {
    "errors": [],
    "warnings": [],
    "structured_plan": {
        "status": "not_attempted",
        "errors": ["structured generation error (see server logs)"],
        "warnings": [],
        "schema_version": None,
    },
}


class _CardConvertingStage2(FakeStage2Automator):
    """Finalizes with ``inline_report``; the pre-release attempt returns ``retry_outcome``."""

    def __init__(self, *, inline_report=None, retry_outcome=None, retry_error=None):
        super().__init__(
            result_factory=lambda: finalized_result(
                structured_plan=None,
                schema_version=None,
                stage2_validator_report=dict(inline_report or _CRASHED_INLINE_CARD_REPORT),
            )
        )
        self.retry_outcome = retry_outcome
        self.retry_error = retry_error
        self.conversions: list[str] = []

    async def _attempt_structured_plan(self, *, final_plan_text, planning_brief, source, log_context=None):
        self.conversions.append(source)
        if self.retry_error is not None:
            raise self.retry_error
        return self.retry_outcome, [{
            "stage2_total_tokens": 10,
            "stage2_estimated_cost_usd": 0.01,
            "stage2_calls": [{"label": "structured_first", "source": source}],
        }]


def test_card_retry_cannot_restart_the_job_runtime_budget(monkeypatch):
    monkeypatch.setenv("APP_STAGE2_FINALIZE_TIMEOUT_SECONDS", "1500")
    cancelled = []

    class HangingCard(_CardConvertingStage2):
        async def _attempt_structured_plan(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    result = finalized_result(
        structured_plan=None, schema_version=None,
        stage2_validator_report=dict(_CRASHED_INLINE_CARD_REPORT),
    )
    retried = asyncio.run(retry_structured_card_before_release(
        stage2=HangingCard(), final_result=result, emit_milestone=lambda *args: None,
        remaining_runtime_seconds=0.01,
    ))
    assert retried is result
    assert cancelled == [True]


def _milestone_codes(job):
    return [milestone["code"] for milestone in job["progress_milestones"]]


def test_crashed_inline_card_is_built_and_saved_before_the_job_completes():
    from api.stage2_automation import StructuredPlanOutcome

    store = FakeStore()
    seed_default_profiles(store)
    stage2 = _CardConvertingStage2(
        retry_outcome=StructuredPlanOutcome(
            status="valid", structured_plan={"weeks": [{"days": []}]}, schema_version="1.1"
        )
    )

    job = _run_stage2_failure_job(store, stage2, client_request_id="card-retry-succeeds")

    assert job["status"] == "completed"
    assert stage2.conversions == ["generation_card_retry"]
    plan = store.plans[job["plan_id"]]
    assert plan["structured_plan"] == {"weeks": [{"days": []}]}
    report = plan["stage2_validator_report"]
    assert report["structured_plan"]["status"] == "valid"
    # The extra conversion records why it ran.
    assert report["stage2_model_calls"][-1]["retry_reason"] == "not_attempted"
    codes = _milestone_codes(job)
    assert codes.index("structured_card_retry_started") < codes.index("structured_card_retry_finished")


def test_card_retry_that_fails_again_still_releases_the_plan():
    from api.stage2_automation import StructuredPlanOutcome

    store = FakeStore()
    seed_default_profiles(store)
    stage2 = _CardConvertingStage2(
        retry_outcome=StructuredPlanOutcome(status="invalid_fallback_used", errors=["still unfaithful"])
    )

    job = _run_stage2_failure_job(store, stage2, client_request_id="card-retry-fails")

    assert job["status"] == "completed"
    assert stage2.conversions == ["generation_card_retry"]
    plan = store.plans[job["plan_id"]]
    assert plan.get("structured_plan") is None
    assert plan["stage2_validator_report"]["structured_plan"]["errors"] == ["still unfaithful"]


def test_card_retry_crash_releases_on_the_inline_outcome():
    store = FakeStore()
    seed_default_profiles(store)
    stage2 = _CardConvertingStage2(retry_error=RuntimeError("converter blew up"))

    job = _run_stage2_failure_job(store, stage2, client_request_id="card-retry-crashes")

    assert job["status"] == "completed"
    plan = store.plans[job["plan_id"]]
    assert plan["stage2_validator_report"]["structured_plan"]["status"] == "not_attempted"
    assert "structured_card_retry_finished" in _milestone_codes(job)


@pytest.mark.parametrize("rejected_status", [
    "invalid_fallback_used", "blocked_by_safety_audit", "timed_out", "generation_failed",
])
def test_card_the_validators_rejected_is_not_converted_again(rejected_status):
    # Production job ec326d04: the inline card was rejected inside finalize and
    # the identical conversion ran again for 4m34s before release.
    store = FakeStore()
    seed_default_profiles(store)
    report = {
        **_FAILED_INLINE_CARD_REPORT,
        "structured_plan": {**_FAILED_INLINE_CARD_REPORT["structured_plan"], "status": rejected_status},
    }
    stage2 = _CardConvertingStage2(inline_report=report)

    job = _run_stage2_failure_job(store, stage2, client_request_id=f"card-rejected-{rejected_status}")

    assert job["status"] == "completed"
    assert stage2.conversions == []
    assert "structured_card_retry_started" not in _milestone_codes(job)
    plan = store.plans[job["plan_id"]]
    assert plan.get("structured_plan") is None
    assert plan["stage2_validator_report"]["structured_plan"]["status"] == rejected_status


def test_clean_inline_card_is_not_retried():
    store = FakeStore()
    seed_default_profiles(store)
    clean_report = {
        "errors": [],
        "warnings": [],
        "structured_plan": {"status": "valid", "errors": [], "warnings": [], "schema_version": "1.1"},
    }
    stage2 = _CardConvertingStage2(inline_report=clean_report)
    stage2.result_factory = lambda: finalized_result(
        structured_plan={"weeks": []},
        schema_version="1.1",
        stage2_validator_report=dict(clean_report),
    )

    job = _run_stage2_failure_job(store, stage2, client_request_id="card-clean")

    assert job["status"] == "completed"
    assert stage2.conversions == []
    assert "structured_card_retry_started" not in _milestone_codes(job)


def test_card_skipped_because_stage2_held_the_plan_is_built_before_release():
    """Stage 2 held the plan, so its inline gate never attempted the card.

    Persistence then releases the held plan with flags. The card must be
    attempted against that release status, before the job completes.
    """
    from api.stage2_automation import StructuredPlanOutcome

    store = FakeStore()
    seed_default_profiles(store)
    not_attempted_report = {
        "errors": [],
        "warnings": [],
        "structured_plan": {"status": "not_attempted", "errors": [], "warnings": [], "schema_version": None},
    }
    stage2 = _CardConvertingStage2(
        inline_report=not_attempted_report,
        retry_outcome=StructuredPlanOutcome(
            status="valid", structured_plan={"weeks": [{"days": []}]}, schema_version="1.1"
        ),
    )
    stage2.result_factory = lambda: finalized_result(
        status="review_required",
        stage2_status="stage2_failed",
        structured_plan=None,
        schema_version=None,
        stage2_validator_report=dict(not_attempted_report),
    )

    job = _run_stage2_failure_job(store, stage2, client_request_id="card-held-then-released")

    assert job["status"] == "completed"
    assert stage2.conversions == ["generation_card_retry"]
    plan = store.plans[job["plan_id"]]
    assert plan["status"] == "publishable_with_flags"
    assert plan["structured_plan"] == {"weeks": [{"days": []}]}
    assert plan["stage2_validator_report"]["structured_plan"]["status"] == "valid"
