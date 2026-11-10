from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import api.stage2_automation as stage2_module
from api.stage2_automation import (
    OpenAIStage2Automator,
    Stage2AutomationError,
)


class _Stream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def __aiter__(self):
        # Drain/hang/raise like the underlying request, without invented events.
        await self.get_final_response()
        return
        yield

    async def get_final_response(self):
        return self.response


class _Responses:
    def __init__(self, response):
        self.response = response
        self.requests: list[dict] = []

    def stream(self, **request):
        self.requests.append(request)
        return _Stream(self.response)


class _Client:
    def __init__(self, response):
        self.responses = _Responses(response)


def test_provider_logs_correlate_start_stream_and_cancellation_without_prompt(caplog, monkeypatch):
    monkeypatch.setattr(stage2_module, "_MODEL_WAIT_LOG_INTERVAL_SECONDS", 0.001)
    class HangingStream(_Stream):
        async def get_final_response(self):
            await asyncio.Event().wait()

    client = _Client(None)
    client.responses.stream = lambda **kwargs: HangingStream(None)
    automator = OpenAIStage2Automator(client=client, model="test-model")

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(automator._generate_text(
                "private athlete prompt", attempt_label="first_pass", source="self_serve",
                log_context={"job_id": "job-logging"},
            ), timeout=0.01)

    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    assert "model_request_started" in caplog.text
    assert "model_stream_opened job_id=job-logging" in caplog.text
    assert "model_request_waiting job_id=job-logging" in caplog.text
    assert "phase=awaiting_final_response" in caplog.text
    assert "model_request_cancelled job_id=job-logging" in caplog.text
    assert "private athlete prompt" not in caplog.text


def _incomplete_response(text: str):
    return SimpleNamespace(
        id="resp_incomplete",
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
        output_text=text,
        usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    )


# Regression contract: usable Stage 2 output stays athlete-facing; Stage 1 is never promoted.
# These tests exist specifically to prevent the old technical-failure fallback from returning.
def test_incomplete_stage2_response_keeps_usable_stage2_text():
    response = _incomplete_response("# Partial Stage 2 plan\n\nDo the Stage 2 work.")
    automator = OpenAIStage2Automator(client=_Client(response), model="test-model")

    text, cost = asyncio.run(
        automator._generate_text(
            "handoff",
            attempt_label="first_pass",
            source="self_serve",
        )
    )

    assert text.startswith("# Partial Stage 2 plan")
    assert cost["stage2_incomplete_response"] is True
    assert cost["stage2_output_tokens"] == 50


def test_incomplete_stage2_response_without_text_still_fails():
    response = _incomplete_response("")
    automator = OpenAIStage2Automator(client=_Client(response), model="test-model")

    with pytest.raises(Stage2AutomationError, match="incomplete and contained no usable plan text"):
        asyncio.run(
            automator._generate_text(
                "handoff",
                attempt_label="first_pass",
                source="self_serve",
            )
        )


def test_incomplete_stage2_plan_is_released_flagged_not_replaced_by_stage1(monkeypatch):
    response = _incomplete_response("# Partial Stage 2 plan\n\nKeep this athlete-facing.")
    automator = OpenAIStage2Automator(client=_Client(response), model="test-model")

    monkeypatch.setattr(
        stage2_module,
        "build_stage2_package",
        lambda **_: {
            "draft_plan_text": "# ugly Stage 1 draft",
            "handoff_text": "handoff",
            "planning_brief": {},
        },
    )
    monkeypatch.setattr(
        stage2_module,
        "review_stage2_output",
        lambda **_: {
            "status": "PASS",
            "needs_retry": False,
            "validator_report": {
                "errors": [],
                "warnings": [],
                "blocking_warnings": [],
                "review_flags": [],
            },
        },
    )
    # The real release policy decides: a truncated-but-usable response is a
    # quality finding, so the plan releases flagged rather than clean.
    monkeypatch.setattr(stage2_module, "athlete_release_with_flags_findings", lambda _report: [])
    monkeypatch.setattr(stage2_module, "admin_review_blocking_findings", lambda _report: [])
    monkeypatch.setattr(stage2_module, "_structured_plan_enabled", lambda: False)

    result = asyncio.run(
        automator.finalize(
            stage1_result={"status": "ready", "plan_text": "# ugly Stage 1 draft"}
        )
    )

    assert result["status"] == "publishable_with_flags"
    assert result["plan_text"].startswith("# Partial Stage 2 plan")
    assert result["plan_text"] != "# ugly Stage 1 draft"
    # Truncation is recorded on the report (as an error, not a warning) so admins
    # can see it, without replacing the plan or holding the release.
    assert any(
        error.get("code") == "stage2_output_truncated"
        for error in result["stage2_validator_report"]["errors"]
    )
    assert result["stage2_validator_report"]["release_decision"] == "publish_with_flags"


# Per-call deadline: the SDK timeout only bounds gaps between streamed events,
# so a call whose stream never finishes must still end on its own deadline
# instead of holding the job until the whole-finalize cap.
class _HangingStream(_Stream):
    async def get_final_response(self):
        await asyncio.Event().wait()


def test_model_call_past_its_total_deadline_fails_with_deadline_error(monkeypatch, caplog):
    from api.stage2_automation import Stage2CallDeadlineError

    monkeypatch.setenv("UNLXCK_STAGE2_CALL_DEADLINE_SECONDS", "0.05")
    client = _Client(None)
    client.responses.stream = lambda **kwargs: _HangingStream(None)
    automator = OpenAIStage2Automator(client=client, model="test-model")

    with caplog.at_level("INFO"), pytest.raises(Stage2CallDeadlineError) as raised:
        asyncio.run(automator._generate_text(
            "private athlete prompt", attempt_label="first_pass", source="self_serve",
            log_context={"job_id": "job-deadline"},
        ))
    assert "model_request_deadline_exceeded job_id=job-deadline attempt=first_pass" in caplog.text
    assert "private athlete prompt" not in caplog.text
    # A failed call still leaves an auditable cost row.
    assert raised.value.stage2_cost["stage2_elapsed_seconds"] >= 0.05


def test_structured_calls_use_their_own_deadline(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_CALL_DEADLINE_SECONDS", "111")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_CALL_DEADLINE_SECONDS", "222")
    assert stage2_module._stage2_call_deadline_seconds("first_pass") == 111.0
    assert stage2_module._stage2_call_deadline_seconds("first_pass_retry") == 111.0
    assert stage2_module._stage2_call_deadline_seconds("structured_first") == 222.0
    assert stage2_module._stage2_call_deadline_seconds("structured_repair") == 222.0
    monkeypatch.setenv("UNLXCK_STAGE2_CALL_DEADLINE_SECONDS", "0")
    assert stage2_module._stage2_call_deadline_seconds("first_pass") is None


def test_default_model_call_deadlines(monkeypatch):
    monkeypatch.delenv("UNLXCK_STAGE2_CALL_DEADLINE_SECONDS", raising=False)
    monkeypatch.delenv("UNLXCK_STAGE2_STRUCTURED_CALL_DEADLINE_SECONDS", raising=False)
    assert stage2_module._stage2_call_deadline_seconds("first_pass") == 180.0
    assert stage2_module._stage2_call_deadline_seconds("structured_first") == 480.0
    assert stage2_module._stage2_call_deadline_seconds("structured_repair") == 480.0


def test_hung_first_pass_is_retried_once_and_the_retry_is_released(monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_CALL_DEADLINE_SECONDS", "0.05")
    good = SimpleNamespace(
        id="resp_retry", status="completed", output_text="# Stage 2 plan from the retry",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    streams = [_HangingStream(None), _Stream(good)]
    client = _Client(good)
    labels: list[str] = []
    client.responses.stream = lambda **kwargs: streams.pop(0)
    automator = OpenAIStage2Automator(client=client, model="test-model")
    original = automator._generate_text

    async def recording(prompt, **kwargs):
        labels.append(kwargs["attempt_label"])
        return await original(prompt, **kwargs)

    monkeypatch.setattr(automator, "_generate_text", recording)
    monkeypatch.setattr(
        stage2_module, "build_stage2_package",
        lambda **_: {"draft_plan_text": "# draft", "handoff_text": "handoff", "planning_brief": {}},
    )
    monkeypatch.setattr(
        stage2_module, "review_stage2_output",
        lambda **_: {"status": "PASS", "needs_retry": False, "validator_report": {
            "errors": [], "warnings": [], "blocking_warnings": [], "review_flags": []}},
    )
    monkeypatch.setattr(stage2_module, "athlete_release_with_flags_findings", lambda _report: [])
    monkeypatch.setattr(stage2_module, "admin_review_blocking_findings", lambda _report: [])
    monkeypatch.setattr(stage2_module, "_structured_plan_enabled", lambda: False)

    result = asyncio.run(automator.finalize(stage1_result={"status": "ready", "plan_text": "# draft"}))

    assert labels == ["first_pass", "first_pass_retry"]
    assert result["plan_text"].startswith("# Stage 2 plan from the retry")


def test_second_first_pass_hang_fails_cleanly_without_a_third_call(monkeypatch):
    from api.stage2_automation import Stage2CallDeadlineError

    monkeypatch.setenv("UNLXCK_STAGE2_CALL_DEADLINE_SECONDS", "0.05")
    calls: list[int] = []
    client = _Client(None)

    def hanging(**kwargs):
        calls.append(1)
        return _HangingStream(None)

    client.responses.stream = hanging
    automator = OpenAIStage2Automator(client=client, model="test-model")
    monkeypatch.setattr(
        stage2_module, "build_stage2_package",
        lambda **_: {"draft_plan_text": "# draft", "handoff_text": "handoff", "planning_brief": {}},
    )
    with pytest.raises(Stage2CallDeadlineError):
        asyncio.run(automator.finalize(stage1_result={"status": "ready", "plan_text": "# draft"}))
    assert len(calls) == 2


@pytest.mark.parametrize("failure_kind", ["deadline", "sdk_timeout"])
def test_timed_out_card_is_recorded_and_not_retried_before_release(monkeypatch, failure_kind):
    from api.generation.stage2_runner import retry_structured_card_before_release

    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_CALL_DEADLINE_SECONDS", "0.05")
    monkeypatch.setattr(stage2_module, "_structured_plan_enabled", lambda: True)
    client = _Client(None)
    client.responses.stream = lambda **kwargs: (
        client.responses.requests.append(kwargs) or _HangingStream(None)
    )
    if failure_kind == "sdk_timeout":
        import httpx
        from openai import APITimeoutError

        def timeout_stream(**kwargs):
            client.responses.requests.append(kwargs)
            raise APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))

        client.responses.stream = timeout_stream
    automator = OpenAIStage2Automator(client=client, model="test-model")

    async def generate_outcome(**kwargs):
        await automator._generate_text(
            "private prompt", attempt_label="structured_first", source="quick_build",
        )
        pytest.fail("hanging conversion must time out")

    monkeypatch.setattr(automator, "_generate_structured_outcome", generate_outcome)

    async def scenario():
        outcome, costs = await automator._attempt_structured_plan(
            final_plan_text="# final plan", planning_brief={}, source="quick_build",
        )
        assert outcome.status == "timed_out"
        assert len(costs) == 1
        assert costs[0]["stage2_calls"][0]["outcome"] == "timed_out"
        assert costs[0]["stage2_elapsed_seconds"] >= (0.05 if failure_kind == "deadline" else 0)
        result = {
            "status": "publishable_with_flags",
            "final_plan_text": "# final plan",
            "stage2_validator_report": {"structured_plan": outcome.as_debug()},
        }
        returned = await retry_structured_card_before_release(
            stage2=automator, final_result=result,
            emit_milestone=lambda *a, **kw: pytest.fail("must not retry timed out card"),
        )
        assert returned is result

    asyncio.run(scenario())
    assert len(client.responses.requests) == 1


def test_stream_timing_records_first_output_last_event_and_completion(caplog):
    response = SimpleNamespace(
        id="resp_timing", status="completed", output_text="# final plan",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )

    class EventStream(_Stream):
        async def __aiter__(self):
            yield SimpleNamespace(type="response.created")
            yield SimpleNamespace(type="response.output_text.delta", delta="PRIVATE OUTPUT")
            yield SimpleNamespace(type="response.completed")

    client = _Client(response)
    client.responses.stream = lambda **kwargs: EventStream(response)
    automator = OpenAIStage2Automator(client=client, model="test-model")
    with caplog.at_level("INFO"):
        text, _ = asyncio.run(automator._generate_text(
            "PRIVATE PROMPT", attempt_label="structured_first", source="quick_build",
            log_context={"job_id": "timing-job"},
        ))
    assert text == "# final plan"
    assert "model_first_output job_id=timing-job" in caplog.text
    summary = next(r.message for r in caplog.records if "model_stream_timing" in r.message)
    assert "outcome=completed" in summary
    assert "event_count=3" in summary
    assert "stream_open_seconds=None" not in summary
    assert "first_output_seconds=None" not in summary
    assert "last_event_seconds=None" not in summary
    assert "PRIVATE OUTPUT" not in caplog.text
    assert "PRIVATE PROMPT" not in caplog.text


@pytest.mark.parametrize("phase", ["opening", "before_output", "after_output"])
def test_stream_timeout_timings_identify_where_it_stalled(monkeypatch, caplog, phase):
    monkeypatch.setattr(stage2_module, "_stage2_call_deadline_seconds", lambda _: 0.02)

    class StalledStream(_Stream):
        closed = False

        async def __aenter__(self):
            if phase == "opening":
                await asyncio.Event().wait()
            return self

        async def __aiter__(self):
            yield SimpleNamespace(type="response.created")
            if phase == "after_output":
                yield SimpleNamespace(type="response.output_text.delta", delta="PRIVATE")
            await asyncio.Event().wait()

        async def __aexit__(self, *args):
            self.closed = True

    stream = StalledStream(None)
    client = _Client(None)
    client.responses.stream = lambda **kwargs: stream
    automator = OpenAIStage2Automator(client=client, model="test-model")
    with caplog.at_level("INFO"), pytest.raises(stage2_module.Stage2CallDeadlineError):
        asyncio.run(automator._generate_text(
            "PRIVATE", attempt_label="structured_first", source="quick_build",
        ))
    summary = next(r.message for r in caplog.records if "model_stream_timing" in r.message)
    assert "outcome=timed_out" in summary
    assert ("stream_open_seconds=None" in summary) == (phase == "opening")
    assert ("first_output_seconds=None" in summary) == (phase != "after_output")
    assert ("last_event_seconds=None" in summary) == (phase == "opening")
    assert stream.closed == (phase != "opening")
    assert "PRIVATE" not in caplog.text
