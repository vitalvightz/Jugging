"""Thin session-completion contract (Block 4 §5).

Today needs a minimal completion model immediately — the "session completed
today" landing state (§1) and the ``completion_status`` in the command view
(§7) depend on it. Block-level logging is intentionally out of scope here.

This module ships the shared types/contracts and the landing-state mapping.
A DB migration is deliberately *not* included in this PR: the executable
contract (status values, required fields, the one-record-per-key rule, and the
landing mapping) is what Today/Overview build against, and it can be backed by
storage in a focused follow-up.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CompletionStatus = Literal[
    "not_started",
    "started",
    "done",
    "modified",
    "skipped",
]

COMPLETION_STATUSES: frozenset[str] = frozenset(
    {"not_started", "started", "done", "modified", "skipped"}
)

# Statuses that mean "the athlete finished engaging with today's session".
TERMINAL_COMPLETION_STATUSES: frozenset[str] = frozenset({"done", "modified", "skipped"})

# Landing state derived from a completion record (consumed by the landing
# resolver, §1): an unfinished session resumes, a terminal one is "completed".
LandingSessionState = Literal["none", "resume", "completed"]


class SessionCompletionRecord(BaseModel):
    """One athlete's completion record for one session on one training day.

    Contract: exactly one record per ``(user_id, session_id, training_day)``.
    """

    user_id: str
    plan_id: str | None
    session_id: str
    # Athlete-local training day (§3), ``YYYY-MM-DD``.
    training_day: str
    status: CompletionStatus = "not_started"
    session_rpe: int | None = Field(default=None, ge=1, le=10)
    pain_after: int | None = Field(default=None, ge=0, le=10)
    # Required (non-empty) for both ``modified`` and ``skipped`` — every
    # non-completed session carries an explanation. Write-time rule only:
    # stored rows are never re-validated on read.
    modification_reason: str = ""
    notes: str = ""
    started_at: str | None = None
    completed_at: str | None = None

    @model_validator(mode="after")
    def _check_status_fields(self) -> "SessionCompletionRecord":
        if self.status == "started" and not self.started_at:
            raise ValueError("started completion requires started_at")
        if self.status in {"done", "modified"} and not self.completed_at:
            raise ValueError(f"{self.status} completion requires completed_at")
        if self.status == "modified" and not self.modification_reason.strip():
            raise ValueError("modified completion requires a modification_reason")
        if self.status == "skipped" and not self.modification_reason.strip():
            raise ValueError("skipped completion requires a reason")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        """The uniqueness key for this completion record."""
        return (self.user_id, self.session_id, self.training_day)


# Same cap as the completion request's notes.
HISTORY_NOTE_MAX_CHARS = 2000


class HistoryCompletionEdit(BaseModel):
    """The few fields of a finished plan session that History may change.

    Status, reason, plan, day and exercise results stay as logged.
    """

    model_config = ConfigDict(extra="forbid")
    session_rpe: int | None = Field(default=None, ge=1, le=10)
    pain_after: int | None = Field(default=None, ge=0, le=10)
    notes: str = Field(default="", max_length=HISTORY_NOTE_MAX_CHARS)

    @field_validator("notes", mode="before")
    @classmethod
    def _clean_notes(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


def completion_status_of(completion: SessionCompletionRecord | Mapping[str, Any] | None) -> CompletionStatus:
    """Read a completion ``status`` from a record/mapping, degrading gracefully.

    A missing record, missing field, or unknown value resolves to
    ``not_started`` rather than crashing.
    """
    if not completion:
        return "not_started"
    if isinstance(completion, SessionCompletionRecord):
        raw = str(completion.status or "").strip()
    else:
        raw = str(completion.get("status") or "").strip()
    if raw in COMPLETION_STATUSES:
        return raw  # type: ignore[return-value]
    return "not_started"


def completion_landing_state(status: str | None) -> LandingSessionState:
    """Map a completion status to the landing session-state (§1).

    * ``started`` → ``resume`` (session started but unfinished)
    * ``done`` / ``modified`` / ``skipped`` → ``completed`` (session done today)
    * anything else → ``none``
    """
    if status == "started":
        return "resume"
    if status in TERMINAL_COMPLETION_STATUSES:
        return "completed"
    return "none"


def completion_key(
    completion: SessionCompletionRecord | Mapping[str, Any],
) -> tuple[str, str, str]:
    """The ``(user_id, session_id, training_day)`` key for a record or mapping."""
    if isinstance(completion, SessionCompletionRecord):
        return completion.key
    return (
        str(completion.get("user_id") or ""),
        str(completion.get("session_id") or ""),
        str(completion.get("training_day") or ""),
    )


def find_completion(
    completions: Sequence[SessionCompletionRecord | Mapping[str, Any]],
    *,
    user_id: str,
    session_id: str,
    training_day: str,
) -> SessionCompletionRecord | Mapping[str, Any] | None:
    """Return the completion record matching the uniqueness key, or ``None``."""
    target = (user_id, session_id, training_day)
    for record in completions:
        if completion_key(record) == target:
            return record
    return None
