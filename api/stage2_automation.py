from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from fightcamp.goal_preservation import validate_goal_preservation
from fightcamp.planner_authority_integrity import AUTHORITY_RELEASE_HOLD_CODES
from fightcamp.stage2_pipeline import (
    apply_structural_integrity_hold,
    build_stage2_package,
    build_stage2_retry,
    repair_stage2_structural_text,
    review_stage2_output,
    structural_integrity_findings,
)
from fightcamp.stage2_policy import (
    admin_review_blocking_findings,
    apply_stage2_release_policy,
    athlete_release_with_flags_findings,
    stage1_fallback_status,
)
from fightcamp.stage2_repair import (
    conditioning_render_repair_integrity_findings,
    reconcile_selected_conditioning_assignments,
)

from .generation.time_utils import utc_now_iso as _utc_now_iso
from .structured_card_lifecycle import (
    clear_structured_card_attempt_started,
    mark_structured_card_attempt_started,
)
from .structured_plan_generation import (
    StructuredPlanOutcome,
    build_structured_plan_outcome,
    build_structured_plan_prompt,
    has_clean_structured_card,
    parse_structured_json,
    should_attempt_structured_plan,
)
from .structured_plan_faithfulness import repair_locked_tactical_watch_source_text
from .structured_plan_calendar_spine import reconcile_calendar_spine
from .structured_plan_models import build_strict_structured_plan_schema
from .structured_plan_sparring_reconcile import reconcile_coach_led_sparring_days
from .settings import env_int

_MODEL_WAIT_LOG_INTERVAL_SECONDS = 30.0

# The central policy owns release. Ready/flagged plans are athlete-displayable;
# review_required retains the rendered text for admins without releasing it.
# See docs/state_machine.md > "Stage 2 outcomes".
_APP_STATUS_READY = "ready"
_APP_STATUS_PUBLISHABLE_WITH_FLAGS = "publishable_with_flags"
_STAGE2_PASS = "stage2_pass"
_STAGE2_FAILED = "stage2_failed"
# Legacy audit value retained for compatibility with historical rows/tests.
# New generations must never publish Stage 1 after a technical Stage 2 failure.
# The plan viewer still recognises it on old plans (shared/stage2-policy.json).
STAGE2_STAGE1_FALLBACK = stage1_fallback_status()
# Legacy report key retained for compatibility with historical rows/tests.
STAGE2_FALLBACK_REPORT_KEY = "stage2_fallback"

logger = logging.getLogger(__name__)
_DEFAULT_FIRST_PASS_CHAR_LIMIT = 400_000
_DEFAULT_OPENAI_MAX_RETRIES = 0
_DEFAULT_MAX_OUTPUT_TOKENS = 0


class Stage2AutomationError(RuntimeError):
    """Raised when Stage 2 automation cannot complete successfully.

    Carries an optional ``stage2_cost`` payload (token/cost telemetry captured up
    to the point of failure) so the orchestrator can still persist what is known
    about a failed attempt.
    """

    stage2_cost: dict[str, Any] | None = None


def _with_stage2_cost(
    error: Stage2AutomationError, cost: dict[str, Any]
) -> Stage2AutomationError:
    error.stage2_cost = cost
    return error


class Stage2CallDeadlineError(Stage2AutomationError):
    """One model call exceeded its total wall-clock deadline."""


class Stage2AutomationUnavailableError(Stage2AutomationError):
    """Raised when Stage 2 automation is not configured for runtime use."""


class Stage2GoalPreservationError(Stage2AutomationError):
    """Historical compatibility only; the central policy decides release."""

    def __init__(self, findings: list[dict]):
        self.findings = findings
        goals = sorted({str(item.get("goal") or item.get("requirement") or "selected goal") for item in findings})
        super().__init__("goal_preservation_failed: deterministic repair/regeneration required for " + ", ".join(goals))


class Stage2Automator(Protocol):
    async def finalize(
        self, *, stage1_result: dict[str, Any], log_context: dict[str, str] | None = None
    ) -> dict[str, Any]: ...


def _stage2_char_limit(attempt_label: str) -> int:
    return env_int(
        "UNLXCK_STAGE2_MAX_FIRST_PASS_CHARS",
        _DEFAULT_FIRST_PASS_CHAR_LIMIT,
        minimum=1,
    )


def _stage2_openai_max_retries() -> int:
    return env_int(
        "UNLXCK_STAGE2_OPENAI_MAX_RETRIES",
        _DEFAULT_OPENAI_MAX_RETRIES,
        minimum=0,
    )


def _stage2_max_output_tokens() -> int:
    # Bounds output token count (and therefore cost/latency) on the Stage 2 call.
    # This budget is SHARED with the model's reasoning tokens (gpt-5-* burn a
    # large, hidden share of it before emitting any plan text), so a positive cap
    # set too low can truncate the plan mid-output. If the provider marks a
    # response incomplete but still returns usable Stage 2 text, that text is
    # retained for review; Stage 1 is never substituted. Default
    # is 0 = no cap (provider default), while the Stage 2 timeout still bounds
    # runtime. Set a positive value only when cost/latency needs a hard ceiling.
    return env_int(
        "UNLXCK_STAGE2_MAX_OUTPUT_TOKENS",
        _DEFAULT_MAX_OUTPUT_TOKENS,
        minimum=0,
    )


def _stage2_timeout_seconds() -> float:
    default_timeout = 210.0
    raw_value = os.getenv("UNLXCK_STAGE2_TIMEOUT_SECONDS", str(default_timeout)).strip()
    try:
        return max(1.0, float(raw_value))
    except ValueError:
        logger.warning("[stage2] invalid float env UNLXCK_STAGE2_TIMEOUT_SECONDS=%r; using %s", raw_value, default_timeout)
        return default_timeout


def _stage2_structured_timeout_seconds() -> float:
    # Per-request timeout for the structured-card calls only (structured_first /
    # structured_repair). Card conversion regularly runs longer than the plan-text
    # pass, so it gets a wider budget than the client default above. Raising this
    # is only effective while APP_STAGE2_FINALIZE_TIMEOUT_SECONDS (the whole-finalize
    # budget in api/generation/timeouts.py) leaves room for it.
    default_timeout = 600.0
    raw_value = os.getenv("UNLXCK_STAGE2_STRUCTURED_TIMEOUT_SECONDS", str(default_timeout)).strip()
    try:
        return max(1.0, float(raw_value))
    except ValueError:
        logger.warning(
            "[stage2] invalid float env UNLXCK_STAGE2_STRUCTURED_TIMEOUT_SECONDS=%r; using %s",
            raw_value,
            default_timeout,
        )
        return default_timeout


def _env_deadline_seconds(name: str, default: float) -> float | None:
    raw_value = os.getenv(name, str(default)).strip()
    if raw_value in {"", "0", "none", "None", "NONE"}:
        return None
    try:
        return max(1.0, float(raw_value))
    except ValueError:
        logger.warning("[stage2] invalid float env %s=%r; using %s", name, raw_value, default)
        return default


def _stage2_call_deadline_seconds(attempt_label: str) -> float | None:
    """Total wall-clock cap for ONE model call, including the whole stream.

    The SDK timeouts above only bound the gap between streamed events, so a
    response that keeps the stream alive but never finishes could otherwise
    hold the job until the whole-finalize cap. Successful plans complete every
    pass well inside these defaults (slowest observed whole Stage 2 response:
    ~510s across all passes; GPT-6 Luna ~200s). ``0``/``none`` disables.
    """
    if attempt_label.startswith("structured"):
        return _env_deadline_seconds("UNLXCK_STAGE2_STRUCTURED_CALL_DEADLINE_SECONDS", 420.0)
    return _env_deadline_seconds("UNLXCK_STAGE2_CALL_DEADLINE_SECONDS", 360.0)


def _estimated_input_tokens(prompt: str) -> int:
    if not prompt:
        return 0
    return max(1, (len(prompt) + 3) // 4)


def _token_rate_per_1m(name: str) -> float:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return 0.0
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        logger.warning("[stage2] invalid token cost env %s=%r; using 0", name, raw_value)
        return 0.0


def _estimated_cost_usd(input_tokens: int, output_tokens: int = 0) -> float:
    input_rate = _token_rate_per_1m("UNLXCK_STAGE2_INPUT_COST_PER_1M")
    output_rate = _token_rate_per_1m("UNLXCK_STAGE2_OUTPUT_COST_PER_1M")
    return round((input_tokens / 1_000_000 * input_rate) + (output_tokens / 1_000_000 * output_rate), 6)


def _get_usage_value(usage: Any, key: str) -> int | None:
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    if isinstance(value, int):
        return value
    return None


def _extract_response_usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump(mode="python")
    output_details = None
    if isinstance(usage, dict):
        output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details")
    elif usage is not None:
        output_details = getattr(usage, "output_tokens_details", None) or getattr(
            usage, "completion_tokens_details", None
        )
    return {
        "input_tokens": _get_usage_value(usage, "input_tokens") or _get_usage_value(usage, "prompt_tokens"),
        "output_tokens": _get_usage_value(usage, "output_tokens") or _get_usage_value(usage, "completion_tokens"),
        "total_tokens": _get_usage_value(usage, "total_tokens"),
        "reasoning_tokens": _get_usage_value(output_details, "reasoning_tokens"),
    }


def _build_stage2_cost(
    model: str,
    *,
    prompt: str,
    response: Any = None,
    text: str | None = None,
    attempt_count: int = 1,
    attempt_label: str | None = None,
    elapsed_seconds: float | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Build the Stage 2 token/cost telemetry row persisted to generation_jobs.

    Captures *actual* usage from the OpenAI response when present, falling back
    to char-based estimates only when the provider omits a usage field. Safe to
    call on the failure path: anything genuinely unknown (e.g. output tokens
    when the request failed before a response) is recorded as ``None`` rather
    than fabricated, and the function never raises. The dict keys match the
    generation_jobs columns so the store can persist it directly. This carries
    no plan text or raw response body — only counts, the model id, the response
    id, and a timestamp.
    """
    usage = (
        _extract_response_usage(response)
        if response is not None
        else {"input_tokens": None, "output_tokens": None, "total_tokens": None, "reasoning_tokens": None}
    )

    input_tokens = usage["input_tokens"]
    if input_tokens is None:
        input_tokens = _estimated_input_tokens(prompt)

    output_tokens = usage["output_tokens"]
    if output_tokens is None and text is not None:
        output_tokens = _estimated_input_tokens(text)

    total_tokens = usage["total_tokens"]
    if total_tokens is None and output_tokens is not None:
        total_tokens = (input_tokens or 0) + output_tokens

    response_id = getattr(response, "id", None) if response is not None else None
    if not response_id or response_id == "unknown":
        response_id = None

    elapsed = round(elapsed_seconds, 2) if elapsed_seconds is not None else None
    cost = {
        "stage2_model": model,
        "stage2_input_tokens": input_tokens,
        "stage2_output_tokens": output_tokens,
        "stage2_total_tokens": total_tokens,
        "stage2_estimated_cost_usd": _estimated_cost_usd(input_tokens or 0, output_tokens or 0),
        "stage2_attempt_count": attempt_count,
        "stage2_response_id": response_id,
        "stage2_cost_recorded_at": _utc_now_iso(),
        "stage2_reasoning_tokens": usage["reasoning_tokens"],
        "stage2_elapsed_seconds": elapsed,
    }
    if attempt_label is not None:
        # Per-call breakdown so a slow job shows which call (plan text vs
        # structured card vs repair) spent the time and the hidden reasoning.
        cost["stage2_calls"] = [
            {
                "label": attempt_label,
                "elapsed_seconds": elapsed,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": usage["reasoning_tokens"],
                "source": source,
                # Sizes only: no prompt or response content is ever recorded.
                "prompt_chars": len(prompt or ""),
                "response_chars": len(text) if text is not None else None,
            }
        ]
    return cost


def _merge_stage2_costs(*costs: dict[str, Any] | None) -> dict[str, Any]:
    """Aggregate token/cost telemetry across multiple Stage 2 model calls.

    Stage 2 normally makes a single model call, but when structured-plan
    generation is enabled it makes one or two more. Token counts and the
    estimated USD cost are summed so the persisted cost row reflects *total*
    Stage 2 spend; metadata (model, response_id, attempt_count, recorded_at) is
    taken from the last call. ``None``/empty entries are ignored.
    """

    token_keys = (
        "stage2_input_tokens",
        "stage2_output_tokens",
        "stage2_total_tokens",
        "stage2_reasoning_tokens",
    )
    merged: dict[str, Any] = {}
    for cost in costs:
        if not cost:
            continue
        # Compute running totals from the prior accumulation *before* adopting the
        # current call's metadata, so the current call is counted exactly once.
        running_tokens = {
            key: (merged.get(key) or 0) + (cost.get(key) or 0) for key in token_keys
        }
        if merged.get("stage2_reasoning_tokens") is None and cost.get("stage2_reasoning_tokens") is None:
            # Unknown stays unknown: a provider that reports no reasoning
            # breakdown must not read as zero reasoning.
            running_tokens["stage2_reasoning_tokens"] = None
        running_usd = (merged.get("stage2_estimated_cost_usd") or 0.0) + (
            cost.get("stage2_estimated_cost_usd") or 0.0
        )
        # The calls run sequentially, so summed elapsed time is the Stage 2
        # model wall-clock spend.
        running_elapsed = round(
            (merged.get("stage2_elapsed_seconds") or 0.0) + (cost.get("stage2_elapsed_seconds") or 0.0),
            2,
        )
        running_calls = [*(merged.get("stage2_calls") or []), *(cost.get("stage2_calls") or [])]
        incomplete_response_seen = bool(merged.get("stage2_incomplete_response")) or bool(
            cost.get("stage2_incomplete_response")
        )
        merged = dict(cost)
        merged.update(running_tokens)
        merged["stage2_estimated_cost_usd"] = running_usd
        merged["stage2_elapsed_seconds"] = running_elapsed
        if running_calls:
            merged["stage2_calls"] = running_calls
        if incomplete_response_seen:
            # Incomplete-response audit is sticky across repair/structured calls.
            # A later successful call must not erase that an earlier Stage 2
            # response was provider-truncated.
            merged["stage2_incomplete_response"] = True
    return merged


def _annotate_call(cost: dict[str, Any] | None, **fields: Any) -> None:
    """Attach a validation outcome / retry reason to a cost row's own call."""
    calls = (cost or {}).get("stage2_calls")
    if isinstance(calls, list) and calls and isinstance(calls[-1], dict):
        calls[-1].update({key: value for key, value in fields.items() if value is not None})


def record_stage2_model_calls(result: dict[str, Any]) -> None:
    """Persist the per-call breakdown on the validator report.

    The job's ``final_result`` is compacted to the release fields before it is
    stored, but the validator report survives on both the job and the plan, so
    each model call (operation, tokens, time, sizes, outcome) stays auditable.
    Counts and labels only: never prompt or response content.
    """
    cost = result.get("stage2_cost")
    report = result.get("stage2_validator_report")
    calls = cost.get("stage2_calls") if isinstance(cost, dict) else None
    if isinstance(report, dict) and isinstance(calls, list):
        result["stage2_validator_report"] = {
            **report,
            "stage2_model_calls": [dict(call) for call in calls if isinstance(call, dict)],
        }


def _is_quota_or_rate_limit_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return (
        "insufficient_quota" in message
        or "exceeded your current quota" in message
        or "rate_limit_exceeded" in message
        or "too many requests" in message
        or "429" in message
    )


def _structured_plan_enabled() -> bool:
    """Whether Stage 2 should also attempt structured-plan generation.

    On by default: the structured card is the athlete-facing plan view, so the
    second conversion call is part of the standard Stage 2 flow. Set
    ``UNLXCK_STAGE2_STRUCTURED_PLAN`` to a falsey value (``0``/``false``/``no``/
    ``off``/empty) to disable it; the structured outcome is then recorded as
    ``not_attempted`` and the raw ``plan_text`` flow is the fallback.
    """

    raw = os.getenv("UNLXCK_STAGE2_STRUCTURED_PLAN")
    if raw is None:
        return True  # unset → default on
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _structured_repair_enabled() -> bool:
    """Whether a failed first structured pass gets one repair retry.

    Off by default. Production telemetry (40 completed jobs, 2026-10-05 to
    2026-10-14) showed the repair rescuing 1 card in ~12 failed first passes
    while each attempt cost 150-200 s of a 5-7 minute generation. A rejected
    card is not lost work for the athlete: the validated deterministic card is
    served instead (``resolve_effective_structured_plan``), and the first pass
    may still be salvaged by pruning content its source does not back. Set
    ``UNLXCK_STAGE2_STRUCTURED_REPAIR`` truthy to restore the repair call.
    """

    raw = os.getenv("UNLXCK_STAGE2_STRUCTURED_REPAIR")
    if raw is None:
        return False  # unset → default off
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Responses-API output-format object requesting a single JSON object. Applied
# only to the structured-card calls (never the markdown plan-text pass).
_STRUCTURED_JSON_FORMAT = {"type": "json_object"}


def _stage2_structured_json_mode() -> bool:
    """Whether the structured-card model calls request JSON output mode.

    On by default. The structured conversion must return one JSON object; asking
    the provider for JSON output mode (Responses API ``text.format`` =
    ``json_object``) guarantees syntactically valid JSON and removes the
    "structured model output was not valid JSON" failure class - the schema,
    normalizer, faithfulness gate, and repair retry still enforce shape/content.
    Set ``UNLXCK_STAGE2_STRUCTURED_JSON_MODE`` to a falsey value to disable it if
    the configured model/endpoint does not accept the parameter (the plan-text
    first pass never uses it, so free-form markdown is unaffected).
    """

    raw = os.getenv("UNLXCK_STAGE2_STRUCTURED_JSON_MODE")
    if raw is None:
        return True  # unset → default on
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _stage2_structured_schema_mode() -> bool:
    """Whether the structured-card calls use strict json_schema (vs json_object).

    OFF by default. When enabled, the calls send the strict json_schema built
    from ``StructuredTrainingPlan`` (via
    :func:`build_strict_structured_plan_schema`) instead of plain JSON object
    mode, so the provider guarantees the response *conforms to the schema* - which
    in turn makes the repair retry unnecessary (disable it with
    ``UNLXCK_STAGE2_STRUCTURED_REPAIR=0`` once schema mode is validated).

    It is off by default deliberately: the strict schema is verified only for
    structural compliance in-repo, and whether the live endpoint accepts it and
    the model output round-trips through validation must be confirmed in staging
    with a real API key first. Set ``UNLXCK_STAGE2_STRUCTURED_SCHEMA_MODE`` to a
    truthy value to opt in. JSON object mode remains the validated default.
    """

    raw = os.getenv("UNLXCK_STAGE2_STRUCTURED_SCHEMA_MODE")
    if raw is None:
        return False  # unset → default off (opt-in)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _structured_response_format() -> dict[str, Any] | None:
    """Pick the Responses-API output-format for the structured-card calls.

    Strict json_schema when schema mode is opted into; otherwise JSON object mode
    when enabled; otherwise ``None`` (free-form). Never applied to the plan-text
    pass. A schema-build failure degrades to JSON object mode rather than raising.
    """

    if _stage2_structured_schema_mode():
        try:
            return {
                "type": "json_schema",
                "name": "structured_training_plan",
                "schema": build_strict_structured_plan_schema(),
                "strict": True,
            }
        except Exception:  # never let schema generation break the call
            # Degrade deterministically to JSON object mode (still valid JSON),
            # not to free-form: schema mode opted into structured output, so a
            # build failure must not silently reintroduce non-JSON responses even
            # when json-object mode is toggled off.
            logger.exception("[stage2] strict schema build failed; falling back to json_object")
            return _STRUCTURED_JSON_FORMAT
    if _stage2_structured_json_mode():
        return _STRUCTURED_JSON_FORMAT
    return None


def _structured_attempt_status(result: dict[str, Any]) -> str:
    """The recorded structured-plan attempt status on a Stage 2 result, or ''."""
    report = result.get("stage2_validator_report")
    debug = report.get("structured_plan") if isinstance(report, dict) else None
    status = debug.get("status") if isinstance(debug, dict) else None
    return str(status or "")


def _record_structured_outcome(
    result: dict[str, Any], outcome: StructuredPlanOutcome
) -> dict[str, Any]:
    """Attach a structured-plan outcome to a Stage 2 result dict.

    The validated plan (or ``None``) and its schema version go to dedicated
    ``structured_plan`` / ``schema_version`` keys that persistence maps to plan
    columns. The status/errors are nested in the existing validator report under
    a ``structured_plan`` key so admins can see them without new storage.
    """

    # Every outcome passed here is terminal, including ``not_attempted``. Clear
    # any durable/in-memory building marker before recording the final debug so a
    # completed conversion can never remain stuck in the building state.
    clear_structured_card_attempt_started(result)
    result["structured_plan"] = outcome.structured_plan
    result["schema_version"] = outcome.schema_version
    report = result.get("stage2_validator_report")
    if not isinstance(report, dict):
        report = {}
        result["stage2_validator_report"] = report
    report["structured_plan"] = outcome.as_debug()
    return result


def _recover_coverage_failed_card(
    outcome: StructuredPlanOutcome, *, planning_brief: Any,
    plan_text: str, computed_support: Any = None,
) -> StructuredPlanOutcome:
    """Recover exact planner sessions without trusting a misplaced model card.

    A converted block on the wrong countdown must never be silently accepted.
    Rebuild directly from Stage 1's closed session membership plus the actual
    athlete-facing text, then run the same schema, faithfulness, coverage and
    safety checks before writing a persisted enhanced card.
    """
    if (
        outcome.status != "invalid_fallback_used"
        or not outcome.errors
        or not all(str(error).startswith("planner_coverage:") for error in outcome.errors)
        or not isinstance(planning_brief, dict)
        or not plan_text.strip()
    ):
        return outcome
    from .structured_plan_deterministic_fallback import build_deterministic_structured_plan
    from .structured_plan_faithfulness import check_structured_faithfulness, PRESCRIPTION
    from .structured_plan_generation import planner_sc_coverage_losses
    from .structured_plan_models import safe_parse_structured_plan, SCHEMA_VERSION
    from .structured_plan_safety import audit_structured_plan, split_findings

    candidate = build_deterministic_structured_plan(planning_brief, plan_text)
    if not isinstance(candidate, dict):
        return outcome
    parsed = safe_parse_structured_plan(candidate, raw_markdown=plan_text)
    if not parsed.ok:
        return outcome
    if planner_sc_coverage_losses(candidate, planning_brief, plan_text):
        return outcome
    unfaithful = check_structured_faithfulness(candidate, plan_text, planning_brief)
    if any(not str(finding).startswith(f"{PRESCRIPTION}:") for finding in unfaithful):
        return outcome
    blocking, advisory = split_findings(audit_structured_plan(candidate, computed_support))
    if blocking:
        return outcome
    return StructuredPlanOutcome(
        status="valid",
        structured_plan=candidate,
        schema_version=SCHEMA_VERSION,
        warnings=[
            "deterministic_source_recovery: repaired converter countdown/coverage loss",
            *[str(item) for item in outcome.warnings],
            *[f"faithfulness: {item}" for item in unfaithful],
            *[str(item) for item in advisory],
        ],
    )


async def attempt_structured_plan_for_result(
    result: dict[str, Any],
    *,
    planning_brief: Any,
    automator: Any,
    source: str,
    log_context: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Centralized structured-plan trigger for any plan result.

    Single entry point used by both the automated Stage 2 path and the admin
    approval/manual paths, so the decision lives in one place. Gates on the
    canonical :func:`should_attempt_structured_plan` predicate (env flag +
    athlete-displayable status + final plan_text + not already converted), then
    runs the conversion via ``automator`` and records the outcome (and debug)
    onto ``result``. Always returns ``(result, structured_costs)`` and never
    raises — a failure leaves the plan_text fallback intact.
    """

    if not should_attempt_structured_plan(result, _structured_plan_enabled()):
        _record_structured_outcome(result, StructuredPlanOutcome(status="not_attempted"))
        return result, []
    # The automator must expose the conversion method (OpenAIStage2Automator).
    # A disabled/auxiliary automator skips, keeping plan_text the source — but
    # this plan WAS eligible, so record why the conversion could not run: a bare
    # "not_attempted" here previously left admins staring at an unexplained
    # missing card while every plan silently stayed on the markdown fallback.
    converter = getattr(automator, "_attempt_structured_plan", None)
    if converter is None:
        reason = str(getattr(automator, "reason", "") or "").strip() or (
            f"Stage 2 automator {type(automator).__name__} cannot convert structured plans."
        )
        logger.warning(
            "[stage2] structured_plan skipped: converter unavailable source=%s automator=%s reason=%s",
            source,
            type(automator).__name__,
            reason,
        )
        _record_structured_outcome(
            result,
            StructuredPlanOutcome(
                status="not_attempted",
                errors=[f"structured conversion unavailable: {reason}"],
            ),
        )
        return result, []
    _apply_locked_tactical_watch_source_repair(
        result,
        planning_brief=planning_brief,
        source=source,
    )
    # The worker result is not persisted until finalization completes, but it
    # still carries the same lifecycle marker as existing-row conversions. This
    # keeps the canonical result contract consistent and guarantees that any
    # terminal outcome recorded below clears the marker.
    mark_structured_card_attempt_started(result)
    outcome, costs = await converter(
        final_plan_text=str(result.get("final_plan_text") or result.get("plan_text") or ""),
        planning_brief=planning_brief,
        source=source,
        log_context=log_context,
    )
    try:
        outcome = _recover_coverage_failed_card(
            outcome,
            planning_brief=planning_brief,
            plan_text=str(result.get("final_plan_text") or result.get("plan_text") or ""),
            computed_support=result.get("computed_support"),
        )
    except Exception:
        # A failed optional reconstruction must retain the rejected outcome.
        logger.exception("[stage2] source-backed card recovery failed")
    _record_structured_outcome(result, outcome)
    report_structured_card_outcome(outcome, source=source, log_context=log_context)
    return result, costs


_CARD_FAILED_STATUSES = frozenset({"invalid_fallback_used", "blocked_by_safety_audit"})
_SALVAGED_PREFIX = "salvaged: "
# Schema containment drops converter nodes too; a card that shipped after it is
# just as much a salvaged card as one pruned for faithfulness.
_SCHEMA_SALVAGE_PREFIX = "schema_salvage: "


def _card_finding_kind(finding: str) -> str:
    """"COUNTDOWN" / "INTRODUCED" / "schema" ... for grouping, never the detail."""
    text = str(finding or "")
    if text.startswith(_SCHEMA_SALVAGE_PREFIX):
        return "schema_salvage"
    if text.startswith("schema_salvage_refused:"):
        return "schema_salvage_refused"
    for prefix in (_SALVAGED_PREFIX, "faithfulness: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    labelled = re.match(r"^([A-Z][A-Z_]+):", text)
    if labelled:
        return labelled.group(1)
    if re.match(r"^weeks\.\d|^[a-z_]+(?:\.\d+)?\.", text):
        return "schema"
    return "other"


def report_structured_card_outcome(
    outcome: StructuredPlanOutcome,
    *,
    source: str,
    log_context: dict[str, str] | None = None,
) -> str | None:
    """Alert when an enhanced card failed or shipped only after salvage.

    A failed card silently drops the athlete onto the rebuilt fallback, which is
    how a regression can break every card for hours unnoticed. Each failure is
    logged at ERROR and sent to Sentry grouped by status and finding kind, so a
    systematic break is one loud, growing issue. A salvaged card is a WARNING:
    it shipped, but content was dropped. Returns ``"failed"`` / ``"salvaged"``
    or ``None``. Never raises.
    """
    try:
        salvaged = [
            w
            for w in outcome.warnings
            if str(w).startswith((_SALVAGED_PREFIX, _SCHEMA_SALVAGE_PREFIX))
        ]
        failed = outcome.status in _CARD_FAILED_STATUSES or (
            outcome.status == "not_attempted" and bool(outcome.errors)
        )
        if not failed and not salvaged:
            return None
        kind = "failed" if failed else "salvaged"
        findings = list(outcome.errors) if failed else salvaged
        kinds = sorted({_card_finding_kind(item) for item in findings}) or ["unknown"]
        context = log_context or {}
        logger.log(
            logging.ERROR if failed else logging.WARNING,
            "[stage2] structured_card_%s status=%s source=%s job_id=%s athlete_id=%s kinds=%s count=%d first=%s",
            kind,
            outcome.status,
            source,
            context.get("job_id", "unknown"),
            context.get("athlete_id", "unknown"),
            ",".join(kinds),
            len(findings),
            findings[0] if findings else "",
        )
        try:
            import sentry_sdk

            with sentry_sdk.push_scope() as scope:
                scope.set_level("error" if failed else "warning")
                scope.set_tag("structured_card_outcome", kind)
                scope.set_tag("structured_card_status", outcome.status)
                scope.set_tag("structured_card_kinds", ",".join(kinds))
                scope.set_tag("stage2_source", source)
                scope.fingerprint = ["structured-card", kind, outcome.status, *kinds]
                scope.set_context(
                    "structured_card",
                    {
                        "job_id": context.get("job_id"),
                        "findings": findings[:20],
                        "finding_count": len(findings),
                    },
                )
                sentry_sdk.capture_message(
                    f"Enhanced card {kind}: {outcome.status} ({', '.join(kinds)})"
                )
        except Exception:
            # Observability must not become another availability dependency.
            logger.debug("Unable to report structured card outcome to Sentry", exc_info=True)
        return kind
    except Exception:
        logger.debug("structured card outcome report failed", exc_info=True)
        return None


def _stage2_source(stage1_result: dict[str, Any]) -> str:
    explicit = str(stage1_result.get("_generation_source") or "").strip()
    if explicit:
        return explicit
    why_log = stage1_result.get("why_log") if isinstance(stage1_result.get("why_log"), dict) else {}
    if isinstance(why_log.get("injury_triage_resume_override"), dict):
        return "admin_triage_resume"
    return "unknown"


def _apply_locked_tactical_watch_source_repair(
    result: dict[str, Any],
    *,
    planning_brief: Any,
    source: str,
) -> None:
    source_text = str(result.get("final_plan_text") or result.get("plan_text") or "")
    repaired_source = repair_locked_tactical_watch_source_text(source_text, planning_brief)
    if repaired_source.source_markdown == source_text:
        if repaired_source.unresolved:
            logger.warning(
                "[stage2] locked Tactical Watch source repair unresolved source=%s count=%d",
                source,
                len(repaired_source.unresolved),
            )
        return

    if repaired_source.unresolved:
        logger.warning(
            "[stage2] rejected partial locked Tactical Watch source repair source=%s applied=%d unresolved=%d",
            source,
            len(repaired_source.applied),
            len(repaired_source.unresolved),
        )
        return

    try:
        repaired_review = review_stage2_output(
            planning_brief=planning_brief,
            final_plan_text=repaired_source.source_markdown,
        )
    except Exception:
        logger.exception("[stage2] repaired locked Tactical Watch source failed Stage 2 revalidation")
        return

    repaired_report = repaired_review.get("validator_report")
    if not isinstance(repaired_report, dict):
        logger.warning("[stage2] repaired locked Tactical Watch source produced malformed validator report")
        return
    if repaired_report.get("release_decision") == "hold":
        logger.warning(
            "[stage2] rejected locked Tactical Watch source repair after revalidation status=%s applied=%d unresolved=%d",
            repaired_review.get("status") or "unknown",
            len(repaired_source.applied),
            len(repaired_source.unresolved),
        )
        return

    original_stage2_status = str(result.get("stage2_status") or "").strip()
    original_app_status = str(result.get("status") or "").strip()
    original_report = result.get("stage2_validator_report")
    prior_errors = list(original_report.get("errors") or []) if isinstance(original_report, dict) else []
    prior_blocking = (
        list(original_report.get("blocking_warnings") or []) if isinstance(original_report, dict) else []
    )

    repair_audit = {
        "locked_tactical_watch": {
            "status": "applied",
            "applied": list(repaired_source.applied),
            "unresolved_count": len(repaired_source.unresolved),
            "previous_stage2_status": original_stage2_status or None,
            "previous_app_status": original_app_status or None,
            "previous_error_count": len(prior_errors),
            "previous_blocking_warning_count": len(prior_blocking),
            "revalidated_status": repaired_review.get("status") or None,
            "revalidated_release_decision": repaired_report.get("release_decision") or None,
        }
    }
    repaired_report["source_repair"] = repair_audit

    result["final_plan_text"] = repaired_source.source_markdown
    if str(result.get("plan_text") or "").strip():
        result["plan_text"] = repaired_source.source_markdown
    result["stage2_validator_report"] = repaired_report
    release_decision = str(repaired_report.get("release_decision") or "").strip()
    if release_decision == "publish_with_flags":
        result["status"] = _APP_STATUS_PUBLISHABLE_WITH_FLAGS
        result["stage2_status"] = _STAGE2_PASS
    elif release_decision == "publish":
        result["status"] = _APP_STATUS_READY
        result["stage2_status"] = _STAGE2_PASS
    else:
        if original_app_status:
            result["status"] = original_app_status
        if original_stage2_status:
            result["stage2_status"] = original_stage2_status
    logger.info(
        "[stage2] repaired locked Tactical Watch source before structured conversion source=%s applied=%d unresolved=%d",
        source,
        len(repaired_source.applied),
        len(repaired_source.unresolved),
    )


def _apply_structural_source_repair_and_hold(
    result: dict[str, Any],
    *,
    planning_brief: Any,
    source: str,
) -> None:
    """Repair known Stage 1 structural omissions before release/card conversion.

    Missing phase/week/session-role structure is not an ordinary advisory flag:
    the UI can otherwise present a complete-looking camp with the wrong owner.
    Deterministic repair gets one chance to restore known role-map content in
    place. The release handling is strictly fail-closed: a failed repair, any
    unresolved mandatory role, a failed or invalid revalidation, or any
    structural loss that survives the repair all preserve the ORIGINAL plan text
    and produce a structural hold rather than continuing to publication. The
    original validator report and a before/after repair record are retained so
    the structural repair can never erase unrelated failures or silently upgrade
    the Stage 2 audit.
    """
    if not isinstance(planning_brief, dict):
        return

    original_report = result.get("stage2_validator_report")
    original_report = original_report if isinstance(original_report, dict) else {}

    # A plan already held upstream stays held: a structural repair must never
    # upgrade another layer's hold into a release.
    if str(original_report.get("release_decision") or "").strip() == "hold":
        return

    # Only act when the plan actually carries Stage 1 structural loss.
    if not structural_integrity_findings(original_report):
        return

    original_text = str(result.get("final_plan_text") or result.get("plan_text") or "")

    def _repair_audit(
        *,
        status: str,
        repaired: dict[str, Any] | None,
        revalidated_report: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "applied": list((repaired or {}).get("applied") or []),
            "unresolved": list((repaired or {}).get("unresolved") or []),
            "unresolved_count": len((repaired or {}).get("unresolved") or []),
            "previous_release_decision": original_report.get("release_decision"),
            "previous_error_count": len(original_report.get("errors") or []),
            "previous_blocking_warning_count": len(original_report.get("blocking_warnings") or []),
            "revalidated_release_decision": (revalidated_report or {}).get("release_decision"),
        }

    def _hold(
        *,
        status: str,
        repaired: dict[str, Any] | None,
        revalidated_report: dict[str, Any] | None,
    ) -> None:
        # Fail-closed: keep the ORIGINAL plan text and failure history, append
        # the structural hold, and never release. The rendered text is retained
        # in final_plan_text for admin repair only.
        held = apply_structural_integrity_hold(original_report)
        audit = _repair_audit(
            status=status, repaired=repaired, revalidated_report=revalidated_report
        )
        existing_audit = held.get("source_repair") if isinstance(held.get("source_repair"), dict) else {}
        held = {**held, "source_repair": {**existing_audit, "structural_source_repair": audit}}
        result["stage2_validator_report"] = held
        result["final_plan_text"] = original_text
        result["plan_text"] = ""
        result["status"] = "review_required"
        result["stage2_status"] = _STAGE2_FAILED
        logger.warning(
            "[stage2] structural integrity hold source=%s status=%s applied=%d unresolved=%d",
            source,
            status,
            len(audit["applied"]),
            audit["unresolved_count"],
        )

    try:
        repaired = repair_stage2_structural_text(
            planning_brief=planning_brief,
            final_plan_text=original_text,
            validator_report=original_report,
        )
    except Exception:
        logger.exception("[stage2] structural source repair raised; holding")
        _hold(status="repair_failed", repaired=None, revalidated_report=None)
        return

    repaired_text = str(repaired.get("text") or original_text)

    # Unresolved mandatory loss, or nothing safely restorable, holds immediately.
    if list(repaired.get("unresolved") or []) or repaired_text == original_text:
        _hold(status="unresolved_structural_loss", repaired=repaired, revalidated_report=None)
        return

    # Revalidate the repaired text through the full review pipeline.
    try:
        review = review_stage2_output(
            planning_brief=planning_brief,
            final_plan_text=repaired_text,
        )
        revalidated_report = review.get("validator_report")
    except Exception:
        logger.exception("[stage2] structural source repair failed revalidation; holding")
        _hold(status="revalidation_failed", repaired=repaired, revalidated_report=None)
        return

    if not isinstance(revalidated_report, dict):
        _hold(status="revalidation_invalid", repaired=repaired, revalidated_report=None)
        return

    # The repair must actually clear the structural loss and the central policy
    # must not itself hold; otherwise preserve the original plan and hold.
    if (
        structural_integrity_findings(revalidated_report)
        or revalidated_report.get("release_decision") == "hold"
    ):
        _hold(status="unresolved_after_repair", repaired=repaired, revalidated_report=revalidated_report)
        return

    release_decision = str(revalidated_report.get("release_decision") or "").strip()
    if release_decision not in {"publish", "publish_with_flags"}:
        # Unknown decision -> fail closed.
        _hold(status="unknown_release_decision", repaired=repaired, revalidated_report=revalidated_report)
        return

    # Success: the repaired text is structurally whole. Record a before/after
    # audit (the central policy already recomputed the release decision inside
    # review_stage2_output); unrelated failures in the revalidated report are
    # preserved as-is and never erased by the repair.
    audit = _repair_audit(
        status="applied", repaired=repaired, revalidated_report=revalidated_report
    )
    audit["revalidated_status"] = review.get("status")
    existing_audit = (
        revalidated_report.get("source_repair")
        if isinstance(revalidated_report.get("source_repair"), dict)
        else {}
    )
    revalidated_report = {
        **revalidated_report,
        "source_repair": {**existing_audit, "structural_source_repair": audit},
    }
    result["final_plan_text"] = repaired_text
    if str(result.get("plan_text") or "").strip():
        result["plan_text"] = repaired_text
    result["stage2_validator_report"] = revalidated_report
    if release_decision == "publish_with_flags":
        result["status"] = _APP_STATUS_PUBLISHABLE_WITH_FLAGS
    else:
        result["status"] = _APP_STATUS_READY
    result["stage2_status"] = _STAGE2_PASS
    logger.info(
        "[stage2] structural source repair applied source=%s roles=%d decision=%s",
        source,
        len(repaired.get("applied") or []),
        release_decision,
    )


def _log_stage2_prompt_budget(prompt: str, *, attempt_label: str, source: str, will_send: bool) -> None:
    estimated_tokens = _estimated_input_tokens(prompt)
    logger.info(
        "[stage2] prompt_budget stage2_attempt=%s source=%s prompt_chars=%s "
        "estimated_input_tokens=%s max_allowed_chars=%s will_send=%s estimated_cost_usd=%s",
        attempt_label,
        source,
        len(prompt),
        estimated_tokens,
        _stage2_char_limit(attempt_label),
        will_send,
        _estimated_cost_usd(estimated_tokens),
    )


def _enforce_stage2_prompt_budget(prompt: str, *, attempt_label: str, source: str) -> None:
    limit = _stage2_char_limit(attempt_label)
    will_send = len(prompt) <= limit
    _log_stage2_prompt_budget(prompt, attempt_label=attempt_label, source=source, will_send=will_send)
    if not will_send:
        raise Stage2AutomationError(
            f"Stage 2 {attempt_label} prompt too large: {len(prompt)} chars > {limit}"
        )


def _strip_wrapping_code_fence(text: str) -> str:
    normalized = text.strip()
    if not normalized.startswith("```") or not normalized.endswith("```"):
        return normalized
    first_newline = normalized.find("\n")
    if first_newline == -1:
        return normalized.strip("`").strip()
    return normalized[first_newline + 1 : -3].strip()


def _response_is_incomplete(response: Any) -> bool:
    payload = response.model_dump(mode="python") if hasattr(response, "model_dump") else response
    is_dict = isinstance(payload, dict)

    status = str((payload.get("status") if is_dict else getattr(response, "status", "")) or "").strip().lower()
    if status == "incomplete":
        return True

    details = payload.get("incomplete_details") if is_dict else getattr(response, "incomplete_details", None)
    if details is None:
        return False
    if hasattr(details, "model_dump"):
        details = details.model_dump(mode="python")

    if isinstance(details, dict):
        detail_text = " ".join(str(value) for value in details.values() if value is not None).lower()
    else:
        detail_text = str(details).lower()

    return any(marker in detail_text for marker in ("max_output_tokens", "output", "token", "length"))


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return _strip_wrapping_code_fence(output_text)

    payload = response.model_dump(mode="python") if hasattr(response, "model_dump") else response
    if not isinstance(payload, dict):
        raise Stage2AutomationError("Stage 2 model returned an unreadable response payload.")

    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)

    combined = "\n".join(parts).strip()
    if not combined:
        raise Stage2AutomationError("Stage 2 model returned no plan text.")
    return _strip_wrapping_code_fence(combined)


def _base_result(
    stage1_result: dict[str, Any],
    *,
    draft_plan_text: str,
    stage2_cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **stage1_result,
        "draft_plan_text": draft_plan_text,
        # Keep the legacy response field null; plans are displayed in-app, not exported.
        "pdf_url": None,
        "stage2_retry_text": "",
        "stage2_validator_report": {},
        "stage2_attempt_count": 0,
        "stage2_status": "",
        "final_plan_text": "",
        # Structured plan output (schema-first). Defaults to absent; populated by
        # the structured-generation attempt on a passing plan when enabled.
        # Persistence maps these to the plans.structured_plan / schema_version
        # columns and drops them gracefully on legacy schemas.
        "structured_plan": None,
        "schema_version": None,
        # Token/cost telemetry captured from the Stage 2 model call. Persistence
        # maps this to dedicated generation_jobs columns; it is not the plan body.
        "stage2_cost": stage2_cost or {},
    }


def _approved_result(
    stage1_result: dict[str, Any],
    *,
    draft_plan_text: str,
    final_plan_text: str,
    validator_report: dict[str, Any],
    attempt_count: int,
    stage2_status: str,
    retry_text: str = "",
    app_status: str = _APP_STATUS_READY,
    stage2_cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **_base_result(stage1_result, draft_plan_text=draft_plan_text, stage2_cost=stage2_cost),
        "status": app_status,
        "plan_text": final_plan_text,
        "final_plan_text": final_plan_text,
        "stage2_status": stage2_status,
        "stage2_validator_report": validator_report,
        "stage2_retry_text": retry_text,
        "stage2_attempt_count": attempt_count,
    }


def _reviewed_result(stage1_result, *, validator_report, **kwargs):
    """Store the central decision identically for automatic and manual review."""
    decision = validator_report["release_decision"]
    held = decision == "hold"
    result = _approved_result(
        stage1_result,
        validator_report=validator_report,
        stage2_status=_STAGE2_FAILED if held else _STAGE2_PASS,
        app_status=(
            "review_required"
            if held
            else _APP_STATUS_PUBLISHABLE_WITH_FLAGS
            if decision == "publish_with_flags"
            else _APP_STATUS_READY
        ),
        **kwargs,
    )
    if held:
        # Keep rendered content in final_plan_text for admin repair only.
        result["plan_text"] = ""
    return result


@dataclass
class DisabledStage2Automator:
    reason: str

    async def finalize(
        self, *, stage1_result: dict[str, Any], log_context: dict[str, str] | None = None
    ) -> dict[str, Any]:
        raise Stage2AutomationUnavailableError(self.reason)


@dataclass
class OpenAIStage2Automator:
    client: Any
    model: str

    @classmethod
    def from_env(cls) -> Stage2Automator:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return DisabledStage2Automator(
                "OPENAI_API_KEY is required for automated Stage 2 finalization."
            )

        try:
            from openai import AsyncOpenAI
        except ImportError:
            return DisabledStage2Automator(
                "The openai package is required for automated Stage 2 finalization."
            )

        model = os.getenv("UNLXCK_STAGE2_MODEL", "gpt-5-mini").strip() or "gpt-5-mini"
        timeout_seconds = _stage2_timeout_seconds()
        client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=_stage2_openai_max_retries(),
        )
        return cls(
            client=client,
            model=model,
        )

    async def _generate_text(
        self,
        prompt: str,
        *,
        attempt_label: str,
        source: str,
        log_context: dict[str, str] | None = None,
        timeout: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        _enforce_stage2_prompt_budget(prompt, attempt_label=attempt_label, source=source)
        request: dict[str, Any] = {
            "model": self.model,
            "input": prompt,
            # Background mode: the provider keeps working on the response
            # server-side, so a dropped connection cannot kill a long generation.
            "background": True,
        }
        if response_format is not None:
            # Constrain the provider's output format (e.g. JSON object mode for the
            # structured-card calls) via the Responses API text.format field. The
            # plan-text first pass leaves this unset so it stays free-form markdown.
            request["text"] = {"format": response_format}
        max_output_tokens = _stage2_max_output_tokens()
        if max_output_tokens > 0:
            request["max_output_tokens"] = max_output_tokens
        if timeout is not None:
            # Per-request override. Do NOT pass None through: the SDK treats an
            # explicit None as "no timeout at all" rather than the client default.
            request["timeout"] = timeout
        context = log_context or {}
        logger.info(
            "[stage2] model_request_started attempt=%s model=%s chars=%s max_output_tokens=%s timeout=%s format=%s job_id=%s source=%s",
            attempt_label,
            self.model,
            len(prompt),
            max_output_tokens or "unset",
            timeout if timeout is not None else "client_default",
            (response_format or {}).get("type", "text"),
            context.get("job_id") or "unknown",
            source,
        )
        call_cost: dict[str, Any] = {"attempt_label": attempt_label, "source": source}
        started = time.monotonic()
        request_phase = "opening_stream"

        async def log_waiting() -> None:
            # This reports where we are waiting, never claims model progress.
            # It remains useful when the provider sends no response at all.
            while True:
                await asyncio.sleep(_MODEL_WAIT_LOG_INTERVAL_SECONDS)
                logger.info(
                    "[stage2] model_request_waiting job_id=%s attempt=%s model=%s phase=%s elapsed_seconds=%.3f",
                    context.get("job_id") or "unknown", attempt_label, self.model,
                    request_phase, time.monotonic() - started,
                )

        async def consume_stream() -> Any:
            nonlocal request_phase
            # Stream instead of a single blocking create: the HTTP read timeout
            # then applies to the gap between events, not the whole generation,
            # so long responses stop tripping idle-connection timeouts.
            # get_final_response() drains the stream and returns the same
            # Response object create() would have.
            async with self.client.responses.stream(**request) as stream:
                request_phase = "awaiting_final_response"
                logger.info(
                    "[stage2] model_stream_opened job_id=%s attempt=%s model=%s elapsed_seconds=%.3f phase=awaiting_final_response",
                    context.get("job_id") or "unknown", attempt_label, self.model, time.monotonic() - started,
                )
                return await stream.get_final_response()

        deadline_seconds = _stage2_call_deadline_seconds(attempt_label)
        wait_logger = asyncio.create_task(log_waiting())
        try:
            if deadline_seconds is None:
                response = await consume_stream()
            else:
                # Leaving the stream context closes the connection; the
                # background response is not retrieved after this point.
                response = await asyncio.wait_for(consume_stream(), timeout=deadline_seconds)
        except asyncio.TimeoutError as exc:
            elapsed = time.monotonic() - started
            logger.warning(
                "[stage2] model_request_deadline_exceeded job_id=%s attempt=%s model=%s phase=%s elapsed_seconds=%.3f deadline_seconds=%s",
                context.get("job_id") or "unknown", attempt_label, self.model,
                request_phase, elapsed, deadline_seconds,
            )
            raise _with_stage2_cost(
                Stage2CallDeadlineError(
                    f"Stage 2 {attempt_label} model call exceeded its {deadline_seconds:.0f}s deadline."
                ),
                _build_stage2_cost(
                    self.model, prompt=prompt, response=None, elapsed_seconds=elapsed, **call_cost,
                ),
            ) from exc
        except asyncio.CancelledError:
            logger.warning(
                "[stage2] model_request_cancelled job_id=%s attempt=%s model=%s elapsed_seconds=%.3f",
                context.get("job_id") or "unknown", attempt_label, self.model, time.monotonic() - started,
            )
            raise
        except Exception as exc:  # pragma: no cover - provider failure surfaces via integration
            # No response means no actual usage; record the estimated input cost
            # so a failed attempt still leaves an auditable cost row.
            failure_cost = _build_stage2_cost(
                self.model,
                prompt=prompt,
                response=None,
                elapsed_seconds=time.monotonic() - started,
                **call_cost,
            )
            if _is_quota_or_rate_limit_error(exc):
                raise _with_stage2_cost(
                    Stage2AutomationError(
                        "Stage 2 stopped: OpenAI quota/rate limit hit. No further retry attempted."
                    ),
                    failure_cost,
                ) from exc
            # Provider exceptions can carry payloads or credentials. Record only
            # correlation metadata and the exception type, including in logs.
            logger.warning(
                "[stage2] model_request_failed job_id=%s attempt=%s model=%s elapsed_seconds=%.3f error_type=%s",
                context.get("job_id") or "unknown", attempt_label, self.model,
                time.monotonic() - started, type(exc).__name__,
            )
            raise _with_stage2_cost(
                Stage2AutomationError("Stage 2 model request failed. Check server logs."),
                failure_cost,
            ) from exc
        finally:
            wait_logger.cancel()
            with suppress(asyncio.CancelledError):
                await wait_logger
        call_cost["elapsed_seconds"] = time.monotonic() - started
        response_id = getattr(response, "id", None) or "unknown"
        response_incomplete = _response_is_incomplete(response)
        try:
            text = _extract_response_text(response)
        except Stage2AutomationError as extraction_error:
            error = (
                Stage2AutomationError(
                    "Stage 2 model response was incomplete and contained no usable plan text."
                )
                if response_incomplete
                else extraction_error
            )
            raise _with_stage2_cost(
                error, _build_stage2_cost(self.model, prompt=prompt, response=response, **call_cost)
            )
        cost = _build_stage2_cost(self.model, prompt=prompt, response=response, text=text, **call_cost)
        if response_incomplete:
            # Preserve the Stage 2 body instead of silently replacing it with the
            # deterministic Stage 1 draft. The finalizer forces this result onto
            # the flagged release path below so admins can see that it was partial.
            cost["stage2_incomplete_response"] = True
            logger.warning(
                "[stage2] incomplete %s response id=%s chars=%s; validating partial Stage 2 text",
                attempt_label,
                response_id,
                len(text),
            )
        # Attribute cost to the job/athlete so it can be aggregated per user from
        # the logs. Falls back to "unknown" when the caller does not supply it.
        context = log_context or {}
        logger.info(
            "[stage2] received %s response id=%s job_id=%s athlete_id=%s chars=%s "
            "actual_input_tokens=%s actual_output_tokens=%s reasoning_tokens=%s total_tokens=%s "
            "estimated_cost_usd=%s elapsed_seconds=%s",
            attempt_label,
            response_id,
            context.get("job_id") or "unknown",
            context.get("athlete_id") or "unknown",
            len(text),
            cost["stage2_input_tokens"],
            cost["stage2_output_tokens"],
            cost["stage2_reasoning_tokens"],
            cost["stage2_total_tokens"],
            cost["stage2_estimated_cost_usd"],
            cost["stage2_elapsed_seconds"],
        )
        return text, cost

    async def finalize(
        self,
        *,
        stage1_result: dict[str, Any],
        log_context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        package = build_stage2_package(stage1_result=stage1_result)
        preflight_findings = [
            finding
            for finding in (package.get("planner_preflight_findings") or [])
            if isinstance(finding, dict)
        ]
        # Only a finding the planner-authority gate would actually hold on may
        # prevent the Stage 2 model call. AUTHORITY_RELEASE_HOLD_CODES is that
        # canonical classification: an unsafe scheduling decision. Every other
        # preflight finding -- an informational safe omission, or a missing
        # exposure the same authority routes to admin review rather than a hold
        # -- is recorded on the report below while Stage 2 still runs.
        #
        # Skipping the model for a non-hold finding also broke the release
        # invariant: the report published with flags, so the row read
        # stage2_pass with attempt_count=0 and no Stage 2 text -- the Stage 1
        # draft wearing a Stage 2 status. Keeping the skip set identical to the
        # hold set means a skipped Stage 2 is always recorded as held/failed.
        preflight_blockers = [
            finding
            for finding in preflight_findings
            if str(finding.get("code") or "").strip() in AUTHORITY_RELEASE_HOLD_CODES
        ]
        preflight_flags = [
            finding for finding in preflight_findings if finding not in preflight_blockers
        ]
        if preflight_blockers:
            report = apply_stage2_release_policy({
                "errors": preflight_blockers,
                "review_flags": preflight_flags,
                "is_valid": False,
                "planner_preflight_failed": True,
            })
            return _reviewed_result(
                stage1_result, validator_report=report,
                draft_plan_text=str(package.get("draft_plan_text") or ""),
                final_plan_text="", attempt_count=0,
            )
        goal_errors = validate_goal_preservation(package["planning_brief"])
        draft_plan_text = str(package.get("draft_plan_text") or "")
        handoff_text = str(package["handoff_text"])
        source = _stage2_source(stage1_result)
        logger.info(
            "[stage2] package ready model=%s source=%s handoff_chars=%s draft_chars=%s max_model_calls=2",
            self.model,
            source,
            len(handoff_text),
            len(draft_plan_text),
        )

        try:
            first_pass_text, first_pass_cost = await self._generate_text(
                handoff_text,
                attempt_label="first_pass",
                source=source,
                log_context=log_context,
            )
        except Stage2CallDeadlineError:
            # A hung plan-text call is retried once. The whole-finalize cap in
            # api/generation/timeouts.py still bounds the job; a second hang
            # fails with the deadline error instead of waiting out that cap.
            logger.warning(
                "[stage2] first_pass deadline exceeded; retrying once job_id=%s source=%s",
                (log_context or {}).get("job_id") or "unknown", source,
            )
            first_pass_text, first_pass_cost = await self._generate_text(
                handoff_text,
                attempt_label="first_pass_retry",
                source=source,
                log_context=log_context,
            )
        from fightcamp.programme_fidelity import reconcile_authoritative_programme
        programme_reconciliation = reconcile_authoritative_programme(package["planning_brief"], first_pass_text)
        first_pass_text = programme_reconciliation["text"]
        first_review = review_stage2_output(
            planning_brief=package["planning_brief"],
            final_plan_text=first_pass_text,
        )

        def reviewed_report(review):
            report = dict(review["validator_report"])
            report["programme_reconciliation"] = {key: value for key, value in programme_reconciliation.items() if key != "text"}
            errors = report.get("errors")
            if isinstance(errors, list):
                errors = list(errors)
                errors.extend(
                    finding for finding in goal_errors if finding not in errors
                )
                report["errors"] = errors
            if preflight_flags:
                # Non-blocking preflight findings stay visible for QA/admin review
                # even though they no longer withhold the Stage 2 model call.
                flags = report.get("review_flags")
                flags = list(flags) if isinstance(flags, list) else []
                flags.extend(
                    finding for finding in preflight_flags if finding not in flags
                )
                report["review_flags"] = flags
            return {**review, "validator_report": apply_stage2_release_policy(report)}

        first_review = reviewed_report(first_review)
        final_text = first_pass_text

        # Stage 2 makes one plan-text model call. A conditioning render gap is
        # repaired deterministically from the selected assignments; anything that
        # repair cannot fix is held for admin review below, never re-rendered by a
        # second model call (that prompt carried the whole planning brief and was
        # always over the Stage 2 prompt budget, so it could only fail the build).
        first_codes = {
            str(item.get("code") or "")
            for item in [
                *(first_review["validator_report"].get("errors") or []),
                *(first_review["validator_report"].get("blocking_warnings") or []),
            ]
            if isinstance(item, dict)
        }
        original_review_report = dict(first_review["validator_report"])
        repair_source_report: dict[str, Any] | None = None
        conditioning_repair_audit: dict[str, Any] | None = None
        requires_planner_regeneration = bool(
            goal_errors or "goal_preservation_failed" in first_codes
        )
        if first_codes & {
            "late_camp_effective_prescription_exceeded",
            "goal_preservation_render_mismatch",
            "missing_selected_conditioning_assignment",
            "selected_conditioning_effective_prescription_mismatch",
        }:
            retry = build_stage2_retry(
                stage1_result=stage1_result,
                final_plan_text=first_pass_text,
                validator_report=first_review["validator_report"],
            )
            requires_planner_regeneration = bool(
                requires_planner_regeneration or retry.get("requires_planner_regeneration")
            )
            missing_conditioning = bool(
                first_codes
                & {
                    "missing_selected_conditioning_assignment",
                    "selected_conditioning_effective_prescription_mismatch",
                }
            )
            deterministic_repair = None
            if missing_conditioning:
                try:
                    deterministic_repair = reconcile_selected_conditioning_assignments(
                        planning_brief=package["planning_brief"],
                        failed_plan_text=first_pass_text,
                        validator_report=first_review["validator_report"],
                    )
                except Exception:
                    logger.exception(
                        "[stage2] deterministic conditioning render reconciliation failed; holding for review"
                    )
                    deterministic_repair = {
                        "text": first_pass_text,
                        "applied": [],
                        "unresolved": [],
                    }
                deterministic_text = str(deterministic_repair.get("text") or first_pass_text)
                if deterministic_text != first_pass_text and deterministic_repair.get("applied"):
                    deterministic_review = reviewed_report(
                        review_stage2_output(
                            planning_brief=package["planning_brief"],
                            final_plan_text=deterministic_text,
                        )
                    )
                    remaining_codes = {
                        str(item.get("code") or "")
                        for field in ("errors", "blocking_warnings")
                        for item in deterministic_review["validator_report"].get(field, []) or []
                        if isinstance(item, dict)
                    }
                    if not remaining_codes & {
                        "missing_selected_conditioning_assignment",
                        "selected_conditioning_effective_prescription_mismatch",
                    }:
                        final_text = deterministic_text
                        first_review = deterministic_review
                        repair_source_report = original_review_report
                        conditioning_repair_audit = {
                            "status": "applied",
                            "source": "authoritative_selected_assignments",
                            "applied": list(deterministic_repair.get("applied") or []),
                            "unresolved": list(deterministic_repair.get("unresolved") or []),
                            "model_call_used": False,
                        }

            deterministic_complete = bool(
                conditioning_repair_audit
                and conditioning_repair_audit.get("status") == "applied"
            )
            if missing_conditioning and not deterministic_complete:
                conditioning_repair_audit = {
                    "status": "unresolved",
                    "source": "authoritative_selected_assignments",
                    "applied": list((deterministic_repair or {}).get("applied") or []),
                    "unresolved": list((deterministic_repair or {}).get("unresolved") or []),
                    "model_call_used": False,
                }
        plan_text_cost = first_pass_cost

        conditioning_integrity_findings: list[dict[str, Any]] = []
        if conditioning_repair_audit is not None and final_text != first_pass_text:
            conditioning_integrity_findings = conditioning_render_repair_integrity_findings(
                planning_brief=package["planning_brief"],
                before_text=first_pass_text,
                after_text=final_text,
            )
            conditioning_repair_audit["integrity_findings"] = conditioning_integrity_findings
            if conditioning_integrity_findings:
                report = dict(first_review["validator_report"])
                report["errors"] = [
                    *(report.get("errors") or []),
                    *conditioning_integrity_findings,
                ]
                first_review = reviewed_report({**first_review, "validator_report": report})

        if repair_source_report is not None:
            first_review["validator_report"]["repair_source_report"] = repair_source_report
        if conditioning_repair_audit is not None:
            first_review["validator_report"]["conditioning_render_repair"] = conditioning_repair_audit

        if plan_text_cost.get("stage2_incomplete_response"):
            report = dict(first_review["validator_report"])
            report["errors"] = [
                *(report.get("errors") or []),
                {
                    "code": "stage2_output_truncated",
                    "message": "Provider marked the final Stage 2 response incomplete.",
                },
            ]
            first_review = reviewed_report({**first_review, "validator_report": report})

        final_codes = {
            str(item.get("code") or "")
            for field in ("errors", "blocking_warnings")
            for item in first_review["validator_report"].get(field, []) or []
            if isinstance(item, dict)
        }
        unresolved_conditioning_render = bool(
            final_codes
            & {
                "missing_selected_conditioning_assignment",
                "selected_conditioning_effective_prescription_mismatch",
            }
        )
        incomplete_conditioning_repair = bool(
            conditioning_repair_audit
            and plan_text_cost.get("stage2_incomplete_response")
        )
        conditioning_repair_integrity_failure = bool(conditioning_integrity_findings)
        if requires_planner_regeneration:
            report = dict(first_review["validator_report"])
            report["requires_planner_regeneration"] = True
            first_review = {**first_review, "validator_report": report}
        planner_hold_after_render_repair = bool(
            requires_planner_regeneration and conditioning_repair_audit
        )
        if (
            planner_hold_after_render_repair
            or unresolved_conditioning_render
            or incomplete_conditioning_repair
            or conditioning_repair_integrity_failure
        ):
            report = dict(first_review["validator_report"])
            if (
                unresolved_conditioning_render
                or incomplete_conditioning_repair
                or conditioning_repair_integrity_failure
            ):
                report["conditioning_render_hold"] = True
            report["release_decision"] = "hold"
            report["is_athlete_releasable"] = False
            report["is_publishable"] = False
            report["validator_findings_observational"] = False
            first_review = {**first_review, "status": "FAIL", "needs_retry": False, "validator_report": report}

        quality_findings = athlete_release_with_flags_findings(first_review["validator_report"])
        admin_blocking_findings = admin_review_blocking_findings(first_review["validator_report"])
        release_decision = first_review["validator_report"].get("release_decision")
        logger.info(
            "[stage2] first_pass review status=%s needs_retry=%s release_decision=%s quality_flags=%s admin_blockers=%s",
            first_review["status"],
            first_review["needs_retry"],
            release_decision,
            len(quality_findings),
            len(admin_blocking_findings),
        )

        # The central policy is final: callers never upgrade HOLD to publication.
        result = _reviewed_result(
            stage1_result,
            draft_plan_text=draft_plan_text,
            final_plan_text=final_text,
            validator_report=first_review["validator_report"],
            attempt_count=1,
            stage2_cost=plan_text_cost,
        )
        if first_review["validator_report"].get("requires_planner_regeneration"):
            result["requires_planner_regeneration"] = True

        _apply_structural_source_repair_and_hold(
            result,
            planning_brief=package["planning_brief"],
            source=source,
        )

        # Structured-plan conversion is triggered by the canonical state-machine
        # predicate (athlete-displayable plans only), not a hardcoded Stage 2
        # status. Any failure degrades to the plan_text fallback and is recorded
        # for admin debug — a missing or rejected card never holds the plan.
        result, structured_costs = await attempt_structured_plan_for_result(
            result,
            planning_brief=package["planning_brief"],
            automator=self,
            source=source,
            log_context=log_context,
        )
        if _structured_plan_enabled() and not has_clean_structured_card(result):
            # Logged, not held: the raw Stage 2 plan_text is the athlete-facing
            # fallback and the card status stays on the report for admins.
            logger.warning(
                "[stage2] no clean structured card; releasing on the plan_text fallback (card_status=%s)",
                _structured_attempt_status(result) or "unknown",
            )

        _annotate_call(plan_text_cost, outcome=release_decision)
        # Roll the structured calls' tokens into the persisted cost row so it
        # reflects total Stage 2 spend, not just the plan-text pass.
        result["stage2_cost"] = _merge_stage2_costs(plan_text_cost, *structured_costs)
        record_stage2_model_calls(result)
        return result

    async def _attempt_structured_plan(
        self,
        *,
        final_plan_text: str,
        planning_brief: Any,
        source: str,
        log_context: dict[str, str] | None = None,
    ) -> tuple[StructuredPlanOutcome, list[dict[str, Any]]]:
        """Best-effort structured-plan generation. Never raises.

        The enable flag and displayable-status gating live in
        :func:`should_attempt_structured_plan`; this method just runs the
        conversion (first pass + one repair retry) and returns the outcome plus
        the cost telemetry of any structured model calls made.
        """

        if not final_plan_text.strip():
            return StructuredPlanOutcome(status="not_attempted"), []
        costs: list[dict[str, Any]] = []
        try:
            return await self._generate_structured_outcome(
                final_plan_text=final_plan_text,
                planning_brief=planning_brief,
                source=source,
                log_context=log_context,
                costs=costs,
            )
        except Exception:  # never block the raw plan on structured failure
            logger.exception("[stage2] structured_plan attempt failed; using raw fallback")
            # Preserve costs from any calls that did complete before the failure.
            return (
                StructuredPlanOutcome(
                    status="not_attempted",
                    errors=["structured generation error (see server logs)"],
                ),
                costs,
            )

    @staticmethod
    def _reconcile_coach_led(outcome: StructuredPlanOutcome, planning_brief: Any) -> StructuredPlanOutcome:
        """Guarantee declared sparring/coach-led days render as cards.

        The converted card derives a day's coach-led status from the LLM headline
        alone, so a dropped or mislabelled day silently becomes "Rest day.". The
        deterministic role map already knows every sparring day, so stamp/insert
        those cards from it. No-op unless the outcome actually carries a plan.
        """
        if outcome.structured_plan is None:
            return outcome
        notes = reconcile_coach_led_sparring_days(outcome.structured_plan, planning_brief)
        if notes:
            outcome.warnings = list(outcome.warnings) + [
                f"coach_led_reconcile: {note}" for note in notes
            ]
        return outcome

    @staticmethod
    def _reconcile_calendar_spine(
        outcome: StructuredPlanOutcome, planning_brief: Any
    ) -> StructuredPlanOutcome:
        """Restore every no-session calendar day a dated camp dropped.

        The converter mirrors the sparse Stage 2 text, so uneventful D-days vanish
        from ``weeks[*].days`` and the athlete view rebuilds week boundaries /
        counts / phases from only the surviving session days. Rebuild the calendar
        from the deterministic role-map spine so every countdown day exists (as a
        session day or a no-session rest day). Runs AFTER the faithfulness gate —
        it deliberately introduces D-days the source text never spelled out — and
        AFTER the coach-led reconcile, whose contact days it preserves. No-op
        unless the outcome carries a plan and a day was actually missing.
        """
        if outcome.structured_plan is None:
            return outcome
        rebuilt = reconcile_calendar_spine(outcome.structured_plan, planning_brief)
        if rebuilt is not outcome.structured_plan and isinstance(rebuilt, dict):
            outcome.structured_plan = rebuilt
            outcome.warnings = list(outcome.warnings) + [
                "calendar_spine_reconcile: rebuilt continuous countdown calendar"
            ]
        return outcome

    @classmethod
    def _reconcile_structured_calendar(
        cls, outcome: StructuredPlanOutcome, planning_brief: Any
    ) -> StructuredPlanOutcome:
        """Coach-led contact reconcile, calendar-spine reconcile, then contact again.

        The spine restores scheduled roles a day lacks, which can turn a
        sessionless contact day into a session day. The contact must then be
        written to ``coach_led_contact`` against that final membership, or the
        renderer hides the sparring behind the restored session.
        """
        return cls._reconcile_coach_led(
            cls._reconcile_calendar_spine(
                cls._reconcile_coach_led(outcome, planning_brief), planning_brief
            ),
            planning_brief,
        )

    async def _generate_structured_outcome(
        self,
        *,
        final_plan_text: str,
        planning_brief: Any,
        source: str,
        log_context: dict[str, str] | None = None,
        costs: list[dict[str, Any]],
    ) -> tuple[StructuredPlanOutcome, list[dict[str, Any]]]:
        if not isinstance(planning_brief, dict):
            planning_brief = None
        event_date = ""
        if planning_brief:
            event_date = str(planning_brief.get("fight_date") or "")

        json_format = _structured_response_format()

        first_prompt = build_structured_plan_prompt(
            plan_markdown=final_plan_text,
            planning_brief=planning_brief,
            event_date=event_date,
        )
        first_text, first_cost = await self._generate_text(
            first_prompt,
            attempt_label="structured_first",
            source=source,
            log_context=log_context,
            timeout=_stage2_structured_timeout_seconds(),
            response_format=json_format,
        )
        costs.append(first_cost)
        first_json = parse_structured_json(first_text)
        if first_json is None:
            _annotate_call(first_cost, outcome="invalid_fallback_used")
        if first_json is None:
            return (
                StructuredPlanOutcome(
                    status="invalid_fallback_used",
                    errors=["structured model output was not valid JSON"],
                ),
                costs,
            )

        # build_structured_plan_outcome strips biometrics + conservatively
        # normalizes before validating, so a near-miss first pass can succeed
        # without spending a repair call.
        computed_support = (
            planning_brief.get("computed_support") if isinstance(planning_brief, dict) else None
        )
        # With a model repair still to come, a complete repaired card beats a
        # pruned first pass, so salvage waits for the repaired outcome.
        first_outcome = build_structured_plan_outcome(
            first_json,
            raw_markdown=final_plan_text,
            computed_support=computed_support,
            planning_brief=planning_brief,
            allow_salvage=not _structured_repair_enabled(),
        )
        _annotate_call(first_cost, outcome=first_outcome.status)
        # A safety-blocked card is terminal: it was schema-valid, so the repair
        # path would re-validate the same JSON and hit the same blocking
        # findings — never spend the repair call on it.
        if first_outcome.status in ("valid", "blocked_by_safety_audit"):
            return self._reconcile_structured_calendar(first_outcome, planning_brief), costs

        # The repair retry is the second sequential model call (the dominant cost
        # on worst-case latency). It is gated so it can be dropped via env once
        # telemetry shows how rarely it rescues a card.
        if not _structured_repair_enabled():
            logger.info(
                "[stage2] structured_repair skipped (disabled) first_status=%s",
                first_outcome.status,
            )
            return self._reconcile_structured_calendar(first_outcome, planning_brief), costs

        # Single repair retry: re-prompt with the validation errors and the
        # broken JSON, then let build_structured_plan_outcome score the result.
        repair_prompt = build_structured_plan_prompt(
            plan_markdown=final_plan_text,
            planning_brief=planning_brief,
            event_date=event_date,
            repair_errors=first_outcome.errors,
            broken_json=first_text,
        )
        repaired_text, repair_cost = await self._generate_text(
            repair_prompt,
            attempt_label="structured_repair",
            source=source,
            log_context=log_context,
            timeout=_stage2_structured_timeout_seconds(),
            response_format=json_format,
        )
        costs.append(repair_cost)
        repaired_json = parse_structured_json(repaired_text)
        repaired_outcome = build_structured_plan_outcome(
            first_json,
            raw_markdown=final_plan_text,
            repair_fn=lambda _data, _errors: repaired_json,
            computed_support=computed_support,
            planning_brief=planning_brief,
        )
        _annotate_call(repair_cost, outcome=repaired_outcome.status, reason=first_outcome.status)
        # Telemetry for the repair lever: did the second call actually rescue a
        # card the first pass could not produce? Aggregated over time this tells
        # us whether the retry is worth its latency (see _structured_repair_enabled).
        logger.info(
            "[stage2] structured_repair first_status=%s repaired_status=%s rescued=%s",
            first_outcome.status,
            repaired_outcome.status,
            repaired_outcome.status in {"valid", "repair_attempted_valid"},
        )
        return self._reconcile_structured_calendar(repaired_outcome, planning_brief), costs


def build_default_stage2_automator() -> Stage2Automator:
    return OpenAIStage2Automator.from_env()
