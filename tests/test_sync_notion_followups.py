"""Minimal regression tests for the scheduled Notion sync."""

import importlib.util
import copy
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from urllib.error import HTTPError

import pytest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools" / "sync_notion_followups.py"
spec = importlib.util.spec_from_file_location("sync_notion_followups", MODULE)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def row(*, stage="Replied", sent="0", synced=None, copied="old DM", formula="next DM",
        contacted="2026-10-13", last_sent=None, first=None, second=None, safe_due=None,
        timing=""):
    def event(value):
        return {"date": {"start": value} if value else None}

    return {
        "id": "sample",
        "properties": {
            "Stage (AI Fills First)": {"select": {"name": stage}},
            "Follow-Ups Sent": {"select": {"name": sent} if sent else None},
            "Synced Follow-Ups Sent": {"number": synced},
            "Follow-Up DM": {"formula": {"type": "string", "string": formula}},
            "Warm Next DM": {"formula": {"type": "string", "string": "WAIT: due 23 Oct 2026"}},
            "Next DM to Copy": {"formula": {"type": "string", "string": ""}},
            "Copy Follow-Up": {"rich_text": [{"plain_text": copied}]},
            "Date Contacted": event(contacted),
            "Last Follow-Up Sent": event(last_sent),
            "Follow-Up 1 Sent At": event(first),
            "Follow-Up 2 Sent At": event(second),
            "Safe Next Due": event(safe_due),
            "Follow-Up Timing": {"rich_text": [{"plain_text": timing}] if timing else []},
            "Details Sent": {"checkbox": False},
            "Details Sent At": event(None),
            "Onboarding Sent": {"checkbox": False},
            "Onboarding Sent At": event(None),
            "Signup Nudges Sent": {"select": None},
            "Synced Signup Nudges Sent": {"number": None},
            "Onboarding Nudges Sent": {"select": None},
            "Synced Onboarding Nudges Sent": {"number": None},
            "Signup Nudge 1 At": event(None),
            "Onboarding Nudge 1 At": event(None),
            "Warm Last Nudge At": event(None),
            "Feedback Requested": {"checkbox": False},
            "Feedback Requested At": event(None),
            "Feedback Reminder Sent": {"checkbox": False},
            "Feedback Submitted": {"number": 0},
            "Onboarding Complete": {"checkbox": False},
            "Registered At": event(None),
            "Observed Stage": {"rich_text": [{"plain_text": stage}]},
            "Observed Since": event("2026-10-13T12:00:00+01:00"),
        },
    }


def get_copy(changed):
    return changed["Copy Follow-Up"]["rich_text"][0]["text"]["content"]


def test_old_baseline_requires_manual_first_send_date():
    changed = sync.get_changes(row(stage="Contacted", sent="1"), date(2026, 10, 19))
    assert changed["Synced Follow-Ups Sent"] == {"number": 1}
    assert changed["Copy Follow-Up"] == {"rich_text": []}
    assert changed["Follow-Up Timing"]["rich_text"][0]["text"]["content"] == "Needs first send date"
    assert "Follow-Up 1 Sent At" not in changed
    assert "Last Follow-Up Sent" not in changed
    assert sync.missing_send_date(row(stage="Contacted", sent="1")["properties"])


def test_first_dropdown_send_records_observation_not_legacy_guess():
    observed = datetime.fromisoformat("2026-10-19T20:01:45+01:00")
    changed = sync.get_changes(row(stage="Contacted", sent="1", synced=0), observed)
    stamp = "2026-10-19T20:01:45+01:00"
    assert changed["Follow-Up 1 Sent At"] == {"date": {"start": stamp}}
    assert changed["Last Follow-Up Sent"] == {"date": {"start": stamp}}
    assert changed["Safe Next Due"] == {"date": {"start": "2026-10-24T20:01:45+01:00"}}
    assert get_copy(changed) == "next DM"


def test_verified_native_button_first_timestamp_survives_poll():
    changed = sync.get_changes(
        row(stage="Contacted", sent="1", synced=0,
            first="2026-10-19T19:59:00+01:00", last_sent="2026-10-19T19:59:00+01:00"),
        datetime.fromisoformat("2026-10-19T20:01:45+01:00"),
    )
    assert "Follow-Up 1 Sent At" not in changed
    assert "Last Follow-Up Sent" not in changed
    assert changed["Synced Follow-Ups Sent"] == {"number": 1}
    assert changed["Safe Next Due"]["date"]["start"] == "2026-10-24T19:59:00+01:00"


def test_legacy_first_date_backfills_without_inventing_new_date():
    changed = sync.get_changes(
        row(stage="Contacted", sent="1", synced=1, last_sent="2026-10-16"),
        date(2026, 10, 19),
    )
    assert changed["Follow-Up 1 Sent At"] == {"date": {"start": "2026-10-16"}}
    assert changed["Safe Next Due"] == {"date": {"start": "2026-10-22T00:00:00+01:00"}}
    assert "Last Follow-Up Sent" not in changed


def test_date_only_history_waits_through_full_fifth_day():
    page = row(stage="Contacted", sent="1", synced=1, first="2026-10-16")
    due, status, message = sync.due_and_copy(
        page["properties"], 1, "2026-10-16", None,
        datetime.fromisoformat("2026-10-21T17:00:00+01:00"), "second follow-up text")
    assert due == "2026-10-22T00:00:00+01:00"
    assert status == "Follow-up 2: waiting"
    assert message == "second follow-up text"


def test_elapsed_gap_is_120_hours_through_spring_dst_change():
    page = row(stage="Contacted", sent="1", synced=1,
               contacted="2027-03-20", first="2027-03-26T20:00:00+00:00")
    due, _, _ = sync.due_and_copy(
        page["properties"], 1, "2027-03-26T20:00:00+00:00", None,
        datetime.fromisoformat("2027-03-31T20:30:00+01:00"), "second DM")
    assert due == "2027-03-31T21:00:00+01:00"


def test_full_five_day_gap_recomputed_on_repeated_poll():
    instant = datetime.fromisoformat("2026-10-19T18:00:00+01:00")
    prior_due = "2026-10-24T18:00:00+01:00"
    page = row(stage="Contacted", sent="1", synced=1, first=instant.isoformat(),
               copied="WAIT: next follow-up is due 24 Oct 2026, 18:00 BST.",
               safe_due=prior_due, timing="Follow-up 2: waiting")
    assert sync.get_changes(page, datetime.fromisoformat("2026-10-20T18:00:00+01:00")) == {"Copy Follow-Up": sync.message_patch("next DM")}


def test_equivalent_utc_and_local_date_representation_does_not_resync():
    assert sync.same_date_value("2026-10-24T18:00:00+01:00", "2026-10-24T17:00:00.000Z")
    page = row(stage="Contacted", sent="1", synced=1,
               first="2026-10-19T18:00:00+01:00",
               last_sent="2026-10-19T18:00:00+01:00",
               copied="WAIT: next follow-up is due 24 Oct 2026, 18:00 BST.",
               safe_due="2026-10-24T17:00:00.000Z",
               timing="Follow-up 2: waiting")
    assert sync.get_changes(page, datetime.fromisoformat("2026-10-20T18:00:00+01:00")) == {"Copy Follow-Up": sync.message_patch("next DM")}


def test_first_due_date_and_early_copy_are_consistent():
    page = row(stage="Contacted", sent="0", synced=0)
    due, status, message = sync.due_and_copy(
        page["properties"], 0, None, None,
        datetime.fromisoformat("2026-10-15T12:00:00+01:00"), "follow-up 1 text")
    assert due == "2026-10-16T00:00:00+01:00"
    assert status == "Follow-up 1: waiting"
    assert message == "follow-up 1 text"


def test_second_due_respects_day_eight_and_five_days():
    page = row(stage="Contacted", sent="1", synced=1, first="2026-10-14T09:00:00+01:00")
    due, status, message = sync.due_and_copy(
        page["properties"], 1, "2026-10-14T09:00:00+01:00", None,
        datetime.fromisoformat("2026-10-20T11:00:00+01:00"), "second DM")
    assert due == "2026-10-21T00:00:00+01:00"  # Day 8 wins over 5-day gap.
    assert status.endswith("waiting")
    assert message == "second DM"


def test_ready_followup_displays_message_at_eligibility():
    page = row(stage="Contacted", sent="1", synced=1, first="2026-10-19T20:00:00+01:00")
    due, status, message = sync.due_and_copy(
        page["properties"], 1, "2026-10-19T20:00:00+01:00", None,
        datetime.fromisoformat("2026-10-24T20:00:00+01:00"), "second DM")
    assert due == "2026-10-24T20:00:00+01:00"
    assert status == "Follow-up 2: ready"
    assert message == "second DM"


def test_second_dropdown_send_preserves_first_and_records_second():
    page = row(stage="Contacted", sent="2", synced=1, first="2026-10-17T10:00:00+01:00",
               last_sent="2026-10-17T10:00:00+01:00", formula="")
    changed = sync.get_changes(page, datetime.fromisoformat("2026-10-24T18:01:00+01:00"))
    assert "Follow-Up 1 Sent At" not in changed
    assert changed["Follow-Up 2 Sent At"] == {"date": {"start": "2026-10-24T18:01:00+01:00"}}
    assert changed["Last Follow-Up Sent"] == {"date": {"start": "2026-10-24T18:01:00+01:00"}}
    assert changed["Copy Follow-Up"] == {"rich_text": []}


def test_second_button_timestamp_is_not_overwritten():
    page = row(stage="Contacted", sent="2", synced=1,
               first="2026-10-17T10:00:00+01:00",
               second="2026-10-24T17:59:50+01:00", last_sent="2026-10-24T17:59:50+01:00")
    page["last_edited_time"] = "2026-10-24T16:59:50Z"
    changed = sync.get_changes(page, datetime.fromisoformat("2026-10-24T18:01:00+01:00"))
    assert "Follow-Up 2 Sent At" not in changed
    assert "Last Follow-Up Sent" not in changed


def test_early_second_send_is_visible_as_timing_review():
    page = row(stage="Contacted", sent="2", synced=2,
               first="2026-10-19T20:00:00+01:00",
               second="2026-10-22T19:00:00+01:00")
    changed = sync.get_changes(page, date(2026, 10, 25))
    assert changed["Follow-Up Timing"]["rich_text"][0]["text"]["content"] == "Complete (timing review)"
    assert sync.date_value({**page["properties"], **changed}, "Safe Next Due") is None
    assert changed["Copy Follow-Up"] == {"rich_text": []}


def test_two_sent_without_first_date_marks_incomplete_history():
    page = row(stage="Contacted", sent="2", synced=2,
               second="2026-10-24T20:00:00+01:00")
    changed = sync.get_changes(page, date(2026, 10, 25))
    assert changed["Follow-Up Timing"]["rich_text"][0]["text"]["content"] == (
        "Complete (send history incomplete)")


def test_counter_reversal_keeps_history_and_blocks_copy():
    page = row(stage="Contacted", sent="1", synced=2,
               first="2026-10-19", second="2026-10-24", last_sent="2026-10-24")
    changed = sync.get_changes(page, date(2026, 10, 25))
    assert "Follow-Up 1 Sent At" not in changed
    assert "Follow-Up 2 Sent At" not in changed
    assert "Last Follow-Up Sent" not in changed
    assert changed["Copy Follow-Up"] == {"rich_text": []}
    assert sync.date_value({**page["properties"], **changed}, "Safe Next Due") is None


def test_missed_multiple_counter_increments_hold_for_review():
    changed = sync.get_changes(row(stage="Contacted", sent="2", synced=0), date(2026, 10, 25))
    assert "Follow-Up 1 Sent At" not in changed
    assert "Follow-Up 2 Sent At" not in changed
    assert changed["Follow-Up Timing"]["rich_text"][0]["text"]["content"] == "Needs count review"


def test_missing_contact_date_blocks_copy():
    changed = sync.get_changes(
        row(stage="Contacted", sent="0", synced=0, contacted=None), date(2026, 10, 19))
    assert changed["Copy Follow-Up"] == {"rich_text": []}


def test_replied_stage_copies_early_preview_before_due():
    page = row(stage="Replied", sent="1", synced=1)
    p = page["properties"]
    p["Next DM to Copy"]["formula"]["string"] = "Hey mate, just checking you got the beta link."
    changes = sync.get_changes(page, date(2026, 10, 19))
    assert changes["Copy Follow-Up"] == {"rich_text": []}

    p["Details Sent"]["checkbox"] = True
    p["Details Sent At"] = sync.date_patch("2026-10-18T12:00:00+01:00")
    p["Warm Next DM"]["formula"]["string"] = "WAIT: due 21 Oct 2026"
    changes = sync.get_changes(page, date(2026, 10, 19))
    assert get_copy(changes) == sync.WARM_MESSAGES["Replied"][0]


def test_accepted_and_activated_preview_appears_before_due():
    accepted = row(stage="Accepted", copied="")
    p = accepted["properties"]
    p["Details Sent"]["checkbox"] = True
    p["Details Sent At"] = sync.date_patch("2026-10-19T12:00:00+01:00")
    p["Next DM to Copy"]["formula"]["string"] = "Hey mate, were you able to get into Unlxck?"
    assert get_copy(sync.get_changes(accepted, date(2026, 10, 20))).startswith("Hey mate,")

    activated = row(stage="Activated", copied="")
    activated["properties"]["Next DM to Copy"]["formula"]["string"] = (
        "How have your first few days on Unlxck been?"
    )
    assert get_copy(sync.get_changes(activated, date(2026, 10, 14))).startswith("How have your")

    activated["properties"]["Next DM to Copy"]["formula"]["string"] = "WAIT: due 17 Oct 2026"
    assert sync.plain({**activated["properties"], **sync.get_changes(activated, date(2026, 10, 14))}, "Copy Follow-Up") == ""


def test_undated_accepted_does_not_copy_early_preview():
    page = row(stage="Accepted", copied="")
    p = page["properties"]
    p["Details Sent"]["checkbox"] = True
    p["Next DM to Copy"]["formula"]["string"] = "Hey mate, were you able to get into Unlxck?"
    changes = sync.get_changes(page, date(2026, 10, 20))
    assert "Copy Follow-Up" not in changes


def test_idempotent_no_change_when_fields_synced():
    page = row(stage="Replied", sent="1", synced=1, copied="", timing="Signup: waiting for details sent")
    page["properties"]["Synced Signup Nudges Sent"]["number"] = 0
    page["properties"]["Synced Onboarding Nudges Sent"]["number"] = 0
    assert sync.get_changes(page, date(2026, 10, 19)) == {}



def test_paginates_all_outreach_rows(monkeypatch):
    calls = []

    def fake_request(method, path, body=None):
        calls.append(body)
        return {"results": [row()], "has_more": len(calls) == 1, "next_cursor": "more" if len(calls) == 1 else None}

    monkeypatch.setattr(sync, "request", fake_request)
    assert len(list(sync.pages())) == 2
    assert "start_cursor" not in calls[0]
    assert calls[1]["start_cursor"] == "more"


def test_changed_stage_during_final_read_skips_write(monkeypatch):
    original = row(copied="old")
    newer = row(stage="Contacted", copied="old")
    actions = []
    def fake_request(method, path, body=None):
        actions.append(method)
        assert method == "GET", "Do not PATCH a row edited after the fresh read"
        return original if len(actions) == 1 else newer
    monkeypatch.setattr(sync, "pages", lambda: iter([original]))
    monkeypatch.setattr(sync, "request", fake_request)
    assert sync.sync(date(2026, 10, 16)) == (1, 0)
    assert actions == ["GET", "GET"]


def test_retries_rate_limit_529(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(req, timeout):
        calls.append(req)
        if len(calls) == 1:
            raise HTTPError(req.full_url, 529, "Overloaded", {"Retry-After": "1"}, None)
        return BytesIO(b'{"ok": true}')

    monkeypatch.setenv("NOTION_TOKEN", "fake-secret")
    monkeypatch.setattr(sync, "urlopen", fake_urlopen)
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)
    monkeypatch.setattr(sync.random, "uniform", lambda *_: 0)
    assert sync.request("GET", "/pages/example") == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [0.35, 1.0, 0.35]


def test_does_not_retry_failed_patch(monkeypatch):
    attempts = []

    def fail(req, timeout):
        attempts.append(req)
        raise HTTPError(req.full_url, 500, "Server failure", {}, None)

    monkeypatch.setenv("NOTION_TOKEN", "fake-secret")
    monkeypatch.setattr(sync, "urlopen", fail)
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)
    with pytest.raises(HTTPError):
        sync.request("PATCH", "/pages/example", {"properties": {}})
    assert len(attempts) == 1


def test_reuses_existing_qualification_worker_token_names(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_INTEGRATION_TOKEN", raising=False)
    assert sync.notion_token() is None
    monkeypatch.setenv("NOTION_API_KEY", "existing-qualification-token")
    assert sync.notion_token() == "existing-qualification-token"
    monkeypatch.setenv("NOTION_TOKEN", "explicit-token")
    assert sync.notion_token() == "explicit-token"


def test_scorecard_rows_include_all_contacted_stages_and_skip_counters(monkeypatch):
    calls = []
    def fake_request(method, path, body=None):
        calls.append(body)
        def item(stage, source=""):
            return {"properties": {
                "Stage (AI Fills First)": {"select": {"name": stage}},
                "Source Detail": {"rich_text": [{"plain_text": source}] if source else []},
            }}
        if len(calls) == 1:
            return {"results": [item("Contacted"), item("Rejected", "SYSTEM_COUNTER")],
                    "has_more": True, "next_cursor": "page-2"}
        return {"results": [item("Inactive"), item("Activated")], "has_more": False}
    monkeypatch.setattr(sync, "request", fake_request)
    rows = list(sync.scorecard_rows())
    assert [r["properties"]["Stage (AI Fills First)"]["select"]["name"] for r in rows] == [
        "Contacted", "Inactive", "Activated",
    ]
    assert calls[0]["filter"] == {"property": "Date Contacted",
                                  "date": {"is_not_empty": True}}
    assert calls[1]["start_cursor"] == "page-2"


@pytest.fixture
def scorecard_tree(monkeypatch):
    def block(identifier, kind, value="", children=False):
        return {"id": identifier, "type": kind, "has_children": children,
                kind: {"rich_text": [{"plain_text": value}]}}
    tree = {"page": [block("columns", "column_list", children=True)],
            "columns": [block("column-" + str(i), "column", children=True) for i in range(3)]}
    labels = ["REPLY RATE", "ACCEPT RATE", "ACTIVATION RATE"]
    for i, label in enumerate(labels):
        tree["column-" + str(i)] = [block("card-" + str(i), "callout", children=True)]
        tree["card-" + str(i)] = [
            block("headline-" + str(i), "heading_2", "0%"),
            block("label-" + str(i), "paragraph", label),
            block("count-" + str(i), "paragraph",
                  "Awaiting verified app-event integration" if i == 2 else "0 of 0 fighters"),
        ]

    monkeypatch.setattr(sync, "SCORECARD_PAGE_ID", "page")
    monkeypatch.setattr(sync, "block_children", lambda identifier: iter(tree[identifier]))
    return tree


def test_scorecard_updates_three_rates_without_touching_other_blocks(monkeypatch, scorecard_tree):
    def item(stage):
        # Initial baselines have no milestone dates; Inactive keeps previous milestones.
        past = {"date": {"start": "2026-10-17"}} if stage == "Inactive" else {"date": None}
        return {"properties": {
            "Stage (AI Fills First)": {"select": {"name": stage}},
            "First Reply Confirmed At": past,
            "Accepted At": past,
            "Registered At": {"date": None},
            "First Meaningful Action At": {"date": None},
        }}
    monkeypatch.setenv("UNLXCK_APP_EVENTS_VERIFIED", "0")  # Must not gate CRM activation.
    monkeypatch.setattr(sync, "scorecard_rows", lambda: iter(
        [item("Contacted")] * 47 + [item("Rejected")] + [item("Replied")] * 11
        + [item("Accepted")] * 2 + [item("Activated")] * 2 + [item("Inactive")]
    ))
    writes = []
    monkeypatch.setattr(sync, "request", lambda method, path, body=None: writes.append((method, path, body)))
    assert sync.sync_scorecard() == (64, 6)
    assert len(writes) == 7
    assert all(method == "PATCH" and path.startswith("/blocks/") for method, path, _ in writes)
    data = {path: body for _, path, body in writes}
    assert data["/blocks/headline-0"]["heading_2"]["rich_text"][0]["text"]["content"] == "26.6%"
    assert data["/blocks/headline-1"]["heading_2"]["rich_text"][0]["text"]["content"] == "7.8%"
    assert data["/blocks/headline-2"]["heading_2"]["rich_text"][0]["text"]["content"] == "3.1%"
    assert data["/blocks/count-0"]["paragraph"]["rich_text"][0]["text"]["content"] == "17 of 64 fighters"
    assert data["/blocks/count-1"]["paragraph"]["rich_text"][0]["text"]["content"] == "5 of 64 fighters"
    assert data["/blocks/count-2"]["paragraph"]["rich_text"][0]["text"]["content"] == "2 of 64 fighters"
    assert "/blocks/label-0" not in data
    method, path, body = writes[-1]
    assert path == "/blocks/page/children"
    stamp_text = body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]
    assert stamp_text.startswith(sync.SCORECARD_SUCCESS_PREFIX)
    stamp = datetime.fromisoformat(stamp_text.removeprefix(sync.SCORECARD_SUCCESS_PREFIX))
    assert stamp.utcoffset() == timedelta(0)


def test_scorecard_refreshes_existing_success_stamp_when_counts_unchanged(monkeypatch, scorecard_tree):
    monkeypatch.setattr(sync, "scorecard_rows", lambda: iter([]))
    for i in range(3):
        scorecard_tree[f"card-{i}"][2]["paragraph"]["rich_text"] = [{"plain_text": "0 of 0 fighters"}]
    scorecard_tree["page"].append({"id": "success", "type": "paragraph", "paragraph": {
        "rich_text": [{"plain_text": sync.SCORECARD_SUCCESS_PREFIX + "2026-10-01T12:00:00+00:00"}]}})
    writes = []
    monkeypatch.setattr(sync, "request", lambda *args: writes.append(args))
    assert sync.sync_scorecard() == (0, 0)
    assert len(writes) == 1
    assert writes[0][:2] == ("PATCH", "/blocks/success")


@pytest.mark.parametrize("label", ["ACTIVATION RATE ", "\tACTIVATION RATE\n", "\u00a0ACTIVATION RATE\u00a0"])
def test_scorecard_accepts_label_whitespace_without_rewriting_label(monkeypatch, scorecard_tree, label):
    # Live Notion's bold activation label has a trailing space. Rich-text
    # formatting can split the label into multiple spans without changing it.
    paragraph = scorecard_tree["card-2"][1]["paragraph"]
    paragraph["rich_text"] = [{"plain_text": label[:10]}, {"plain_text": label[10:]}]
    monkeypatch.setattr(sync, "scorecard_rows", lambda: iter([]))
    writes = []
    monkeypatch.setattr(sync, "request", lambda *args: writes.append(args))
    assert sync.sync_scorecard() == (0, 1)
    assert [path for _, path, _ in writes] == ["/blocks/count-2", "/blocks/page/children"]
    assert sync.block_text(scorecard_tree["card-2"][1]) == label


def test_scorecard_rejects_duplicate_card_with_padded_label(monkeypatch, scorecard_tree):
    duplicate = copy.deepcopy(scorecard_tree["card-2"])
    duplicate[1]["paragraph"]["rich_text"] = [{"plain_text": "ACTIVATION RATE "}]
    scorecard_tree["column-2"].append({"id": "duplicate", "type": "callout"})
    scorecard_tree["duplicate"] = duplicate
    monkeypatch.setattr(sync, "scorecard_rows", lambda: iter([]))
    monkeypatch.setattr(sync, "request", lambda *_: pytest.fail("Duplicate cards must prevent all writes"))
    with pytest.raises(ValueError, match="Duplicate scorecard card: ACTIVATION RATE"):
        sync.sync_scorecard()


@pytest.mark.parametrize("failure", ["query", "card", "timestamp", "duplicate"])
def test_failed_scorecard_does_not_publish_success(monkeypatch, scorecard_tree, failure):
    old_text = sync.SCORECARD_SUCCESS_PREFIX + "2026-10-01T12:00:00+00:00"
    for i in range(3):
        scorecard_tree[f"card-{i}"][2]["paragraph"]["rich_text"] = [{"plain_text": "1 of 5 fighters"}]
    scorecard_tree["page"].append({"id": "success", "type": "paragraph", "paragraph": {
        "rich_text": [{"plain_text": old_text}]}})
    if failure == "duplicate":
        scorecard_tree["page"].append(copy.deepcopy(scorecard_tree["page"][-1]))

    def rows():
        if failure == "query":
            raise RuntimeError("query failed")
        return iter([])

    writes = []
    def request(method, path, body):
        writes.append(path)
        if (failure == "card" and path == "/blocks/count-1") or path == "/blocks/success":
            raise RuntimeError("write failed")

    monkeypatch.setattr(sync, "scorecard_rows", rows)
    monkeypatch.setattr(sync, "request", request)
    with pytest.raises((RuntimeError, ValueError)):
        sync.sync_scorecard()
    if failure != "timestamp":
        assert "/blocks/success" not in writes
    if failure == "card":
        assert writes == ["/blocks/count-0", "/blocks/count-1"]
    assert sync.block_text(scorecard_tree["page"][-1]) == old_text


def test_scorecard_fails_closed_when_card_missing(monkeypatch):
    monkeypatch.setattr(sync, "scorecard_rows", lambda: iter([]))
    monkeypatch.setattr(sync, "SCORECARD_PAGE_ID", "page")
    monkeypatch.setattr(sync, "block_children", lambda identifier: iter([]))
    monkeypatch.setattr(sync, "request", lambda *_: pytest.fail("No writes if cards are missing"))
    with pytest.raises(ValueError, match="Missing scorecard cards"):
        sync.sync_scorecard()


def observed_row(stage="Rejected", observed="Contacted", since=None, confirmed="Contacted",
                 reply=None, history=""):
    def txt(value):
        return {"rich_text": [{"plain_text": value}] if value else []}
    return {"id": "sample", "properties": {
        "Stage (AI Fills First)": {"select": {"name": stage}},
        "Observed Stage": txt(observed),
        "Observed Since": {"date": {"start": since} if since else None},
        "Confirmed Stage": txt(confirmed),
        "Stage Confirmed At": {"date": None},
        "Date Contacted": {"date": {"start": "2026-10-17"}},
        "Stage History": txt(history),
        "First Reply Confirmed At": {"date": {"start": reply} if reply else None},
        "Interested At": {"date": None},
        "Accepted At": {"date": None},
        "Rejected At": {"date": None},
        "Registered At": {"date": None},
        "First Meaningful Action At": {"date": None},
    }}


def test_missing_confirmed_stage_skips_history_safely():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Accepted", observed="Accepted",
                        since=(now - timedelta(minutes=10)).isoformat())
    del page["properties"]["Confirmed Stage"]
    assert sync.stage_changes(page, now) == {}


def test_missing_confirmed_stage_warns_without_inventing_milestones(monkeypatch, capsys):
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Accepted", observed="Accepted",
                        since=(now - timedelta(minutes=10)).isoformat())
    del page["properties"]["Confirmed Stage"]
    monkeypatch.setattr(sync, "all_pipeline_rows", lambda: iter([page]))
    monkeypatch.setattr(sync, "request", lambda *_: pytest.fail("No writes expected"))
    assert sync.sync_stage_history(now) == (1, 0)
    assert "1 examined rows lack Confirmed Stage (rich_text)" in capsys.readouterr().err


def test_debounces_stage_and_remembers_rejection_as_reply():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    initial = sync.stage_changes(observed_row(), now)
    assert initial["Observed Stage"]["rich_text"][0]["text"]["content"] == "Rejected"
    pending = observed_row(observed="Rejected", since=(now - timedelta(minutes=4)).isoformat())
    assert sync.stage_changes(pending, now) == {}
    stable = observed_row(observed="Rejected", since=(now - timedelta(minutes=6)).isoformat())
    change = sync.stage_changes(stable, now)
    assert "First Reply Confirmed At" in change
    assert "Rejected At" in change
    assert "Registered At" not in change
    assert "baseline" not in change["Stage History"]["rich_text"][0]["text"]["content"]


def test_first_seen_stage_does_not_fabricate_historical_transition_time():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    baseline = observed_row(stage="Activated", observed="Activated",
                            confirmed="", since=(now - timedelta(minutes=6)).isoformat())
    change = sync.stage_changes(baseline, now)
    assert "baseline -> Activated" in change["Stage History"]["rich_text"][0]["text"]["content"]
    assert change["First Reply Confirmed At"]["date"]["start"] == now.isoformat(timespec="seconds")
    assert change["Accepted At"]["date"]["start"] == now.isoformat(timespec="seconds")
    assert "Interested At" not in change
    assert "Registered At" not in change and "First Meaningful Action At" not in change


def test_previous_reply_survives_later_inactive_stage():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Inactive", observed="Inactive", confirmed="Replied",
                        reply="2026-10-14T10:00:00+00:00",
                        since=(now - timedelta(minutes=6)).isoformat())
    change = sync.stage_changes(page, now)
    assert "First Reply Confirmed At" not in change
    assert "Confirmed Stage" in change
    assert "Stage History" in change


def test_scorecard_with_pending_stage_still_validates_cards(monkeypatch):
    monkeypatch.setattr(sync, "scorecard_rows", lambda: iter([
        observed_row(stage="Replied", observed="Replied",
                     since="2026-10-18T16:59:00+00:00", confirmed="")
    ]))
    monkeypatch.setattr(sync, "SCORECARD_PAGE_ID", "page")
    monkeypatch.setattr(sync, "block_children", lambda *_: iter([]))
    with pytest.raises(ValueError, match="Missing scorecard cards"):
        sync.sync_scorecard()


def test_recent_external_edit_blocks_confirmation():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Rejected", observed="Rejected", confirmed="Contacted",
                        since=(now - timedelta(minutes=8)).isoformat())
    page["last_edited_time"] = (now - timedelta(minutes=2)).isoformat()
    assert sync.stage_changes(page, now) == {}


def test_initial_baseline_records_observation_time_milestones():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Accepted", observed="Accepted", confirmed="",
                        since=(now - timedelta(minutes=7)).isoformat())
    changes = sync.stage_changes(page, now)
    assert "Confirmed Stage" in changes
    assert changes["First Reply Confirmed At"]["date"]["start"] == now.isoformat(timespec="seconds")
    assert changes["Accepted At"]["date"]["start"] == now.isoformat(timespec="seconds")


@pytest.mark.parametrize("stage,accepted", [("Replied", False), ("Accepted", True),
                                           ("Activated", True)])
def test_baseline_captures_existing_crm_evidence_not_original_dm_time(stage, accepted):
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage=stage, observed=stage, confirmed="",
                        since=(now - timedelta(minutes=7)).isoformat())
    changes = sync.stage_changes(page, now)
    assert changes["First Reply Confirmed At"]["date"]["start"] == now.isoformat(timespec="seconds")
    assert ("Accepted At" in changes) == accepted
    assert "baseline ->" in changes["Stage History"]["rich_text"][0]["text"]["content"]
    assert "Registered At" not in changes
    assert "First Meaningful Action At" not in changes


def test_repairs_previously_confirmed_baseline_without_overwriting_history():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Activated", observed="Activated", confirmed="Activated",
                        since=(now - timedelta(minutes=7)).isoformat())
    changes = sync.stage_changes(page, now)
    assert set(changes) == {"First Reply Confirmed At", "Accepted At"}
    page["properties"]["First Reply Confirmed At"] = changes["First Reply Confirmed At"]
    page["properties"]["Accepted At"] = changes["Accepted At"]
    assert sync.stage_changes(page, now) == {}


def test_initial_rejection_is_not_automatically_treated_as_reply():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Rejected", observed="Rejected", confirmed="",
                        since=(now - timedelta(minutes=7)).isoformat())
    changes = sync.stage_changes(page, now)
    assert "Confirmed Stage" in changes
    assert "First Reply Confirmed At" not in changes
    assert "Rejected At" not in changes


def test_stage_history_queries_entire_pipeline(monkeypatch):
    pages = [observed_row(stage="Inactive", observed="Inactive", confirmed="Inactive",
                          since="2026-10-18T16:00:00+00:00")]
    monkeypatch.setattr(sync, "all_pipeline_rows", lambda: iter(pages))
    monkeypatch.setattr(sync, "request", lambda *_: pytest.fail("No changes expected"))
    assert sync.sync_stage_history() == (1, 0)


def test_accepted_transition_is_reply_but_not_automatically_interested():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Accepted", observed="Accepted", confirmed="Replied",
                        since=(now - timedelta(minutes=7)).isoformat())
    page["properties"]["Date Contacted"] = {"date": {"start": "2026-10-17"}}
    changes = sync.stage_changes(page, now)
    assert "First Reply Confirmed At" in changes
    assert "Accepted At" in changes
    assert "Interested At" not in changes


def test_uncontacted_rejection_does_not_count_as_reply():
    now = datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    page = observed_row(stage="Rejected", observed="Rejected", confirmed="Contacted",
                        since=(now - timedelta(minutes=7)).isoformat())
    page["properties"]["Date Contacted"] = {"date": None}
    changes = sync.stage_changes(page, now)
    assert "Confirmed Stage" in changes
    assert "First Reply Confirmed At" not in changes


# Stage changes are timestamped by stage_changes; warm events only require
# one operator checkbox/dropdown after the Instagram action, never a date field.
def test_warm_initial_poll_baselines_counters_without_inventing_send_dates():
    page = row(stage="Replied")
    changes = sync.get_warm_changes(page, datetime(2026, 10, 20, 20, tzinfo=timezone.utc))
    assert changes == {
        "Synced Signup Nudges Sent": {"number": 0},
    }


def test_ticking_details_sent_stamps_once_in_replied():
    page = row(stage="Replied")
    previous = copy.deepcopy(page["properties"])
    page["properties"]["Details Sent"]["checkbox"] = True
    now = datetime(2026, 10, 20, 20, 3, tzinfo=timezone.utc)
    changes = sync.get_warm_changes(page, now, previous=previous)
    assert changes["Details Sent At"]["date"]["start"] == now.astimezone(sync.LONDON).isoformat(timespec="seconds")
    page["properties"]["Details Sent At"] = changes["Details Sent At"]
    again = sync.get_warm_changes(page, now + timedelta(days=1))
    assert "Details Sent At" not in again


def test_legacy_onboarding_checkbox_does_not_create_a_send_timestamp():
    page = row(stage="Accepted")
    page["properties"]["Details Sent"]["checkbox"] = True
    page["properties"]["Onboarding Sent"]["checkbox"] = True
    changed = sync.get_warm_changes(page, datetime(2026, 10, 20, 20, tzinfo=timezone.utc))
    assert "Onboarding Sent At" not in changed
    assert "Details Sent At" not in changed


def test_activated_means_used_app_and_feedback_checkbox_stamps():
    page = row(stage="Activated")
    page["properties"]["Feedback Requested"]["checkbox"] = True
    changed = sync.get_warm_changes(page, datetime(2026, 10, 20, 20, tzinfo=timezone.utc))
    assert "Feedback Requested At" in changed
    assert "Details Sent At" not in changed


def test_signup_nudge_progression_stamps_first_and_last_automatically():
    page = row(stage="Replied")
    p = page["properties"]
    p["Synced Signup Nudges Sent"]["number"] = 0
    p["Synced Onboarding Nudges Sent"]["number"] = 0
    p["Signup Nudges Sent"]["select"] = {"name": "1"}
    now = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    first = sync.get_warm_changes(page, now)
    assert first["Synced Signup Nudges Sent"] == {"number": 1}
    assert first["Signup Nudge 1 At"] == first["Warm Last Nudge At"]
    p["Synced Signup Nudges Sent"]["number"] = 1
    p["Signup Nudge 1 At"] = first["Signup Nudge 1 At"]
    p["Warm Last Nudge At"] = first["Warm Last Nudge At"]
    p["Signup Nudges Sent"]["select"] = {"name": "2"}
    last = sync.get_warm_changes(page, now + timedelta(days=4))
    assert last["Synced Signup Nudges Sent"] == {"number": 2}
    assert "Signup Nudge 1 At" not in last
    assert last["Warm Last Nudge At"]["date"]["start"] != first["Warm Last Nudge At"]["date"]["start"]


def test_legacy_onboarding_counter_is_read_only_and_requires_review():
    page = warm_row("Accepted")
    p = page["properties"]
    p["Onboarding Nudges Sent"]["select"] = {"name": "1"}
    p["Synced Onboarding Nudges Sent"]["number"] = 0
    changes = sync.get_changes(page, OBSERVED_NOW)
    assert "Synced Onboarding Nudges Sent" not in changes
    assert "Onboarding Nudge 1 At" not in changes
    assert sync.date_value({**p, **changes}, "Safe Next Due") is None
    assert "legacy onboarding" in sync.plain(changes, "Follow-Up Timing")


def test_historical_warm_count_or_skipped_increment_never_fabricates_dates():
    page = row(stage="Replied")
    p = page["properties"]
    p["Signup Nudges Sent"]["select"] = {"name": "1"}
    baseline = sync.get_warm_changes(page, datetime(2026, 10, 20, 20, tzinfo=timezone.utc))
    assert baseline["Synced Signup Nudges Sent"] == {"number": 1}
    assert "Warm Last Nudge At" not in baseline
    assert "Signup Nudge 1 At" not in baseline
    p["Synced Signup Nudges Sent"]["number"] = 0
    p["Signup Nudges Sent"]["select"] = {"name": "2"}
    missed = sync.get_warm_changes(page, datetime(2026, 10, 20, 20, tzinfo=timezone.utc))
    assert "Synced Signup Nudges Sent" not in missed
    assert "Warm Last Nudge At" not in missed
    assert "Signup Nudge 1 At" not in missed


def test_poll_writes_warm_timestamp_without_touching_cold_followup(monkeypatch):
    page = row(stage="Replied", copied="next DM", timing="Not in follow-up queue")
    p = page["properties"]
    p["Synced Signup Nudges Sent"]["number"] = 0
    p["Synced Onboarding Nudges Sent"]["number"] = 0
    fresh = copy.deepcopy(page)
    fresh["properties"]["Details Sent"]["checkbox"] = True
    writes = []
    def fake_request(method, path, body=None):
        if method == "GET":
            return fresh
        writes.append(body["properties"])
        return {}
    monkeypatch.setattr(sync, "pages", lambda: iter([page]))
    monkeypatch.setattr(sync, "request", fake_request)
    assert sync.sync(datetime(2026, 10, 20, 20, tzinfo=timezone.utc)) == (1, 1)
    assert "Details Sent At" in writes[0]
    assert "Follow-Ups Sent" not in writes[0]
    assert "Follow-Up 1 Sent At" not in writes[0]


OBSERVED_NOW = datetime.fromisoformat("2026-10-20T20:00:00+01:00")


def apply_patch(page, changes):
    page["properties"].update(copy.deepcopy(changes))
    return page


def warm_row(stage="Replied", *, count="0", checkpoint=0, first=None):
    page = row(stage=stage, synced=0)
    p = page["properties"]
    prefix = "Signup"
    flag = "Details Sent"
    p[flag]["checkbox"] = True
    p[flag + " At"] = sync.date_patch("2026-10-15T12:00:00+01:00")
    p[prefix + " Nudges Sent"]["select"] = {"name": count}
    p["Synced " + prefix + " Nudges Sent"]["number"] = checkpoint
    p[prefix + " Nudge 1 At"] = sync.date_patch(first)
    return page


@pytest.mark.parametrize("stage", ["Replied", "Accepted", "Activated"])
def test_stage_change_records_observed_date_without_rewriting_history(stage):
    page = observed_row(stage=stage, observed="Contacted", confirmed="Contacted",
                        since="2026-10-13T10:00:00+01:00", reply="2026-10-14", history="verified history")
    before = copy.deepcopy(page)
    changes = sync.stage_sync_changes(page, OBSERVED_NOW)
    assert sync.plain(changes, "Observed Stage") == stage
    assert changes["Observed Since"] == sync.date_patch(OBSERVED_NOW.isoformat())
    assert "Stage History" not in changes
    assert "First Reply Confirmed At" not in changes
    assert page == before
    # Confirmation and KPI milestones retain the existing five-minute policy.
    apply_patch(page, changes)
    assert not sync.stage_changes(page, OBSERVED_NOW + timedelta(minutes=4))


def test_new_contact_stage_is_dated_without_manual_input_but_old_baseline_is_not():
    page = observed_row(stage="Contacted", observed="Ready to Send", confirmed="Ready to Send")
    page["properties"]["Date Contacted"] = sync.date_patch(None)
    changes = sync.stage_sync_changes(page, OBSERVED_NOW)
    assert changes["Date Contacted"] == sync.date_patch(OBSERVED_NOW.isoformat())
    page["properties"]["Observed Stage"] = sync.rich("")
    assert "Date Contacted" not in sync.stage_sync_changes(page, OBSERVED_NOW)
    page["properties"]["Date Contacted"] = sync.date_patch("2026-10-10")
    assert "Date Contacted" not in sync.stage_sync_changes(page, OBSERVED_NOW)


@pytest.mark.parametrize("stage,flag", [("Replied", "Details Sent"), ("Accepted", "Details Sent")])
def test_marking_initial_action_sets_timestamp_and_due_in_one_patch(stage, flag):
    page = row(stage=stage, synced=0)
    apply_patch(page, sync.get_changes(page, OBSERVED_NOW))  # Worker observes unchecked flag.
    page["properties"][flag]["checkbox"] = True
    changes = sync.get_changes(page, OBSERVED_NOW)
    assert changes[flag + " At"] == sync.date_patch(OBSERVED_NOW.isoformat())
    assert changes["Safe Next Due"] == sync.date_patch("2026-10-23T20:00:00+01:00")
    assert "Signup Nudge 1 At" not in changes
    assert "Onboarding Nudge 1 At" not in changes


@pytest.mark.parametrize("stage", ["Replied", "Accepted"])
def test_stage_entry_alone_does_not_claim_details_or_instructions_sent(stage):
    page = row(stage=stage, synced=0)
    changes = sync.get_changes(page, OBSERVED_NOW)
    assert "Details Sent At" not in changes
    assert "Onboarding Sent At" not in changes
    assert sync.date_value({**page["properties"], **changes}, "Safe Next Due") is None


@pytest.mark.parametrize("stage", ["Replied", "Accepted"])
def test_first_nudge_records_time_and_next_due_four_days_later(stage):
    page = warm_row(stage, count="1", checkpoint=0)
    changes = sync.get_changes(page, OBSERVED_NOW)
    prefix = "Signup"
    assert changes[prefix + " Nudge 1 At"] == sync.date_patch(OBSERVED_NOW.isoformat())
    assert changes["Warm Last Nudge At"] == sync.date_patch(OBSERVED_NOW.isoformat())
    assert changes["Safe Next Due"] == sync.date_patch("2026-10-24T20:00:00+01:00")
    assert not any(key.startswith("Follow-Up ") and key.endswith("Sent At") for key in changes)
    apply_patch(page, changes)
    assert sync.get_changes(page, OBSERVED_NOW + timedelta(minutes=2)) == {}


@pytest.mark.parametrize("stage", ["Replied", "Accepted"])
def test_final_nudge_records_latest_send_preserves_first_and_stops(stage):
    page = warm_row(stage, count="2", checkpoint=1, first="2026-10-16T12:00:00+01:00")
    page["properties"]["Safe Next Due"] = sync.date_patch("2026-10-20T12:00:00+01:00")
    changes = sync.get_changes(page, OBSERVED_NOW)
    assert changes["Warm Last Nudge At"] == sync.date_patch(OBSERVED_NOW.isoformat())
    assert changes["Safe Next Due"] == sync.date_patch(None)
    assert "Signup Nudge 1 At" not in changes
    assert "Onboarding Nudge 1 At" not in changes
    apply_patch(page, changes)
    assert sync.get_changes(page, OBSERVED_NOW + timedelta(days=10)) == {}


@pytest.mark.parametrize("count,checkpoint,first", [("1", None, None), ("1", 1, None),
                                                   ("2", 0, None), ("0", 1, "2026-10-17"),
                                                   ("2", 2, None)])
def test_unknown_or_inconsistent_history_has_no_next_due(count, checkpoint, first):
    page = warm_row(count=count, checkpoint=checkpoint, first=first)
    page["properties"]["Safe Next Due"] = sync.date_patch("2026-10-25")
    for _ in range(2):
        changes = sync.get_changes(page, OBSERVED_NOW)
        assert "Needs Review" in sync.plain({**page["properties"], **changes}, "Follow-Up Timing")
        assert sync.date_value({**page["properties"], **changes}, "Safe Next Due") is None
        assert "Signup Nudge 1 At" not in changes
        apply_patch(page, changes)


def test_contacted_records_baseline_warm_counters_before_stage_entry():
    page = row(stage="Contacted")
    changes = sync.get_warm_changes(page, OBSERVED_NOW)
    assert changes == {"Synced Signup Nudges Sent": {"number": 0}}
    apply_patch(page, changes)
    page["properties"]["Stage (AI Fills First)"]["select"] = {"name": "Accepted"}
    page["properties"]["Signup Nudges Sent"]["select"] = {"name": "1"}
    changes = sync.get_warm_changes(page, OBSERVED_NOW)
    assert "Signup Nudge 1 At" in changes  # Record observed send despite progression.
    assert "Onboarding Nudge 1 At" not in changes


def test_first_poll_counter_race_uses_observed_zero_without_inventing_history(monkeypatch):
    original = warm_row(checkpoint=None)
    fresh = copy.deepcopy(original)
    fresh["properties"]["Signup Nudges Sent"]["select"] = {"name": "1"}
    writes = []
    def request(method, path, body=None):
        if method == "GET":
            return copy.deepcopy(fresh)
        writes.append(body["properties"])
    monkeypatch.setattr(sync, "pages", lambda: iter([original]))
    monkeypatch.setattr(sync, "request", request)
    assert sync.sync(OBSERVED_NOW) == (1, 1)
    assert writes[0]["Signup Nudge 1 At"] == sync.date_patch(OBSERVED_NOW.isoformat())
    assert writes[0]["Synced Signup Nudges Sent"] == {"number": 1}


def test_activated_due_uses_observed_app_use_stage_date_not_signup():
    page = row(stage="Activated", synced=0)
    page["properties"]["Observed Since"] = sync.date_patch("2026-10-16T12:00:00+01:00")
    changes = sync.get_changes(page, OBSERVED_NOW)
    assert changes["Safe Next Due"] == sync.date_patch("2026-10-20T12:00:00+01:00")
    assert "Feedback Requested At" not in changes
    assert "Registered At" not in changes
    page["properties"]["Observed Stage"] = sync.rich("Accepted")
    assert sync.date_value({**page["properties"], **sync.get_changes(page, OBSERVED_NOW)}, "Safe Next Due") is None


def test_feedback_request_then_one_reminder_records_dates_and_stops():
    page = row(stage="Activated", synced=0)
    p = page["properties"]
    p["Feedback Requested"]["checkbox"] = True
    changes = sync.get_changes(page, OBSERVED_NOW)
    assert changes["Feedback Requested At"] == sync.date_patch(OBSERVED_NOW.isoformat())
    assert changes["Safe Next Due"] == sync.date_patch("2026-10-25T19:00:00+00:00")
    apply_patch(page, changes)
    p["Feedback Reminder Sent"]["checkbox"] = True
    changes = sync.get_changes(page, OBSERVED_NOW + timedelta(days=6))
    assert "Warm Last Nudge At" in changes
    assert changes["Safe Next Due"] == sync.date_patch(None)
    assert "Feedback Requested At" not in changes
    apply_patch(page, changes)
    assert sync.get_changes(page, OBSERVED_NOW + timedelta(days=10)) == {}


@pytest.mark.parametrize("anchor,days,expected", [
    ("2026-10-23T20:00:00+01:00", 3, "2026-10-26T19:00:00+00:00"),
    ("2027-03-26T20:00:00+00:00", 3, "2027-03-29T21:00:00+01:00"),
    ("2026-10-23T20:00:00+01:00", 4, "2026-10-27T19:00:00+00:00"),
    ("2027-03-26T20:00:00+00:00", 5, "2027-03-31T21:00:00+01:00"),
    ("2026-10-23", 3, "2026-10-27T00:00:00+00:00"),
])
def test_followup_intervals_handle_uk_dst_and_date_only_history(anchor, days, expected):
    assert sync.elapsed_due(anchor, days).isoformat() == expected


def test_existing_timestamp_and_due_do_not_change_on_overdue_retry():
    page = warm_row(count="1", checkpoint=1, first="2026-10-16T12:00:00+01:00")
    page["properties"]["Warm Last Nudge At"] = sync.date_patch("2026-10-16T12:00:00+01:00")
    changes = sync.get_changes(page, OBSERVED_NOW + timedelta(days=10))
    assert changes["Safe Next Due"] == sync.date_patch("2026-10-20T12:00:00+01:00")
    assert "Signup Nudge 1 At" not in changes
    assert "Details Sent At" not in changes
    assert "Warm Last Nudge At" not in changes


@pytest.mark.parametrize("stage", ["Rejected", "Inactive", "Review Hold"])
def test_stage_pass_clears_future_schedule_on_stop_stage_without_erasing_history(stage):
    page = observed_row(stage=stage, since="2026-10-13T10:00:00+01:00", history="verified history")
    page["properties"]["Safe Next Due"] = sync.date_patch("2026-10-25")
    changes = sync.stage_sync_changes(page, OBSERVED_NOW)
    assert changes["Safe Next Due"] == sync.date_patch(None)
    assert "Stage History" not in changes
    assert "First Reply Confirmed At" not in changes


def test_existing_worker_observes_stage_dates_before_followup_scheduling(monkeypatch):
    calls = []
    monkeypatch.setenv("NOTION_TOKEN", "test")
    for name in ("sync_stage_history", "sync", "sync_scorecard"):
        def record(name=name):
            calls.append(name)
            return 0, 0
        monkeypatch.setattr(sync, name, record)
    sync.run()
    assert calls == ["sync_scorecard", "sync_stage_history", "sync"]


@pytest.mark.parametrize("failures", [
    {"sync_scorecard"}, {"sync_stage_history"}, {"sync"}, {"sync_scorecard", "sync"},
])
def test_worker_isolates_scorecard_failure_and_reports_failed_run(monkeypatch, capsys, failures):
    calls = []
    monkeypatch.setenv("NOTION_TOKEN", "test")
    for name in ("sync_scorecard", "sync_stage_history", "sync"):
        def record(name=name):
            calls.append(name)
            if name in failures:
                raise RuntimeError(f"{name} failed")
            return 0, 0
        monkeypatch.setattr(sync, name, record)
    with pytest.raises(SystemExit) as exc:
        sync.run()
    assert exc.value.code == 1
    expected = ["sync_scorecard", "sync_stage_history"]
    if "sync_stage_history" not in failures:
        expected.append("sync")
    assert calls == expected
    captured = capsys.readouterr()
    for name in failures:
        assert f"{name} failed" in captured.err
    if "sync_scorecard" not in failures:
        assert "Notion conversion: contacted 0" in captured.out


def test_accepted_details_sent_reuses_known_send_time_without_second_action():
    page = row(stage="Accepted", synced=0)
    p = page["properties"]
    p["Details Sent"]["checkbox"] = True
    p["Details Sent At"] = sync.date_patch("2026-10-15T12:00:00+01:00")
    changed = sync.get_changes(page, OBSERVED_NOW)
    assert "Onboarding Sent" not in changed
    assert "Onboarding Sent At" not in changed
    assert changed["Safe Next Due"] == sync.date_patch("2026-10-18T12:00:00+01:00")
    assert "Details Sent At" not in changed  # Never rewrite a recorded send.
    apply_patch(page, changed)
    assert sync.get_changes(page, OBSERVED_NOW + timedelta(minutes=2)) == {}


def test_accepted_details_without_historical_time_does_not_invent_send_date():
    page = row(stage="Accepted", synced=0)
    p = page["properties"]
    p["Details Sent"]["checkbox"] = True
    changed = sync.get_changes(page, OBSERVED_NOW)
    assert "Onboarding Sent" not in changed
    assert "Onboarding Sent At" not in changed
    assert "Details Sent At" not in changed
    assert sync.date_value({**p, **changed}, "Safe Next Due") is None
    assert "date unknown" in sync.plain({**p, **changed}, "Follow-Up Timing")
    # Repeat polling still must not create a fictional historical send date.
    apply_patch(page, changed)
    later = sync.get_changes(page, OBSERVED_NOW + timedelta(days=1))
    assert "Details Sent At" not in later
    assert "Onboarding Sent At" not in later


def test_accepted_legacy_onboarding_date_is_preserved_when_details_are_dated():
    page = row(stage="Accepted", synced=0)
    p = page["properties"]
    p["Details Sent"]["checkbox"] = True
    p["Details Sent At"] = sync.date_patch("2026-10-10T12:00:00+01:00")
    p["Onboarding Sent"]["checkbox"] = True
    p["Onboarding Sent At"] = sync.date_patch("2026-10-16T12:00:00+01:00")
    changed = sync.get_changes(page, OBSERVED_NOW)
    assert "Onboarding Sent" not in changed
    assert "Onboarding Sent At" not in changed
    assert "Details Sent At" not in changed
    assert changed["Safe Next Due"] == sync.date_patch("2026-10-13T12:00:00+01:00")


def test_replied_details_date_remains_independent_before_accepted_transition():
    page = row(stage="Replied", synced=0)
    p = page["properties"]
    p["Details Sent"]["checkbox"] = True
    p["Details Sent At"] = sync.date_patch("2026-10-15T12:00:00+01:00")
    first = sync.get_changes(page, OBSERVED_NOW)
    assert "Onboarding Sent" not in first
    assert "Onboarding Sent At" not in first
    assert first["Safe Next Due"] == sync.date_patch("2026-10-18T12:00:00+01:00")
    apply_patch(page, first)
    p["Stage (AI Fills First)"]["select"] = {"name": "Accepted"}
    accepted = sync.get_changes(page, OBSERVED_NOW)
    assert "Onboarding Sent" not in accepted
    assert "Onboarding Sent At" not in accepted
    assert sync.date_value({**p, **accepted}, "Safe Next Due") == "2026-10-18T12:00:00+01:00"
    assert "Details Sent At" not in accepted


@pytest.mark.parametrize("stage", ["Replied", "Accepted"])
def test_single_details_checkbox_works_without_legacy_onboarding_properties(stage):
    page = row(stage=stage, synced=0)
    p = page["properties"]
    p.pop("Onboarding Sent")
    p.pop("Onboarding Sent At")
    apply_patch(page, sync.get_changes(page, OBSERVED_NOW))
    p["Details Sent"]["checkbox"] = True
    changes = sync.get_changes(page, OBSERVED_NOW + timedelta(minutes=2))
    assert changes["Details Sent At"] == sync.date_patch("2026-10-20T20:02:00+01:00")
    assert changes["Safe Next Due"] == sync.date_patch("2026-10-23T20:02:00+01:00")
    assert "Onboarding Sent" not in changes
    assert "Onboarding Sent At" not in changes
    apply_patch(page, changes)
    assert sync.get_changes(page, OBSERVED_NOW + timedelta(minutes=4)) == {}


@pytest.mark.parametrize("stage", ["Replied", "Accepted"])
def test_prechecked_undated_details_never_start_a_historical_countdown(stage):
    page = row(stage=stage, synced=0)
    p = page["properties"]
    p["Details Sent"]["checkbox"] = True
    p["Date Contacted"] = sync.date_patch("2026-10-13")
    p["Accepted At"] = sync.date_patch("2026-10-15T10:00:00+01:00")
    p["Onboarding Sent"]["checkbox"] = True
    p["Onboarding Sent At"] = sync.date_patch("2026-10-16T10:00:00+01:00")
    p["Last Follow-Up Sent"] = sync.date_patch("2026-10-17T10:00:00+01:00")
    for days in (0, 1, 10):
        changes = sync.get_changes(page, OBSERVED_NOW + timedelta(days=days))
        assert "Details Sent At" not in changes
        assert "Onboarding Sent At" not in changes
        apply_patch(page, changes)
        assert sync.date_value(p, "Safe Next Due") is None
        assert sync.plain(p, "Copy Follow-Up") == ""
        assert sync.plain(p, "Follow-Up Timing") == "Needs Review: details sent date unknown"


@pytest.mark.parametrize("signup_count,first", [("0", None), ("1", "2026-10-19T12:00:00+01:00"),
                                                ("2", "2026-10-15T12:00:00+01:00")])
def test_stage_progression_carries_schedule_without_mirroring_send_history(signup_count, first):
    page = warm_row("Replied", count=signup_count, checkpoint=int(signup_count), first=first)
    p = page["properties"]
    apply_patch(page, sync.get_changes(page, OBSERVED_NOW))
    original = copy.deepcopy(p)
    p["Stage (AI Fills First)"]["select"] = {"name": "Accepted"}
    changes = sync.get_changes(page, OBSERVED_NOW)
    actual = {**p, **changes}
    assert sync.date_value(actual, "Safe Next Due") == sync.date_value(original, "Safe Next Due")
    assert "Onboarding Nudges Sent" not in changes
    assert "Onboarding Nudge 1 At" not in changes
    assert "Details Sent At" not in changes
    assert "Signup Nudge 1 At" not in changes
    assert "Warm Last Nudge At" not in changes
    if signup_count == "1":
        assert sync.date_value(actual, "Safe Next Due") == "2026-10-23T12:00:00+01:00"
        assert sync.plain(actual, "Follow-Up Timing").startswith("Onboarding final follow-up")
    elif signup_count == "2":
        assert sync.date_value(actual, "Safe Next Due") is None
        assert sync.plain(actual, "Copy Follow-Up") == ""


def test_one_signup_and_one_getting_started_followup_complete_the_shared_sequence():
    page = warm_row("Replied", count="1", checkpoint=0)
    apply_patch(page, sync.get_changes(page, OBSERVED_NOW))
    p = page["properties"]
    original_first = copy.deepcopy(p["Signup Nudge 1 At"])
    original_details = copy.deepcopy(p["Details Sent At"])
    p["Stage (AI Fills First)"]["select"] = {"name": "Accepted"}
    apply_patch(page, sync.get_changes(page, OBSERVED_NOW + timedelta(minutes=2)))
    p["Signup Nudges Sent"]["select"] = {"name": "2"}
    changes = sync.get_changes(page, OBSERVED_NOW + timedelta(days=4))
    assert "Onboarding Nudge 1 At" not in changes
    assert changes["Warm Last Nudge At"] == sync.date_patch("2026-10-24T20:00:00+01:00")
    assert changes["Safe Next Due"] == sync.date_patch(None)
    apply_patch(page, changes)
    assert p["Signup Nudge 1 At"] == original_first
    assert p["Details Sent At"] == original_details
    assert p["Signup Nudges Sent"]["select"]["name"] == "2"
    assert p["Onboarding Nudges Sent"]["select"] is None
    assert sync.get_changes(page, OBSERVED_NOW + timedelta(days=10)) == {}


def test_returning_to_replied_does_not_restart_an_accepted_followup():
    page = warm_row("Accepted", count="1", checkpoint=1, first="2026-10-19T12:00:00+01:00")
    apply_patch(page, sync.get_changes(page, OBSERVED_NOW))
    page["properties"]["Stage (AI Fills First)"]["select"] = {"name": "Replied"}
    changes = sync.get_changes(page, OBSERVED_NOW)
    actual = {**page["properties"], **changes}
    assert sync.date_value(actual, "Safe Next Due") == "2026-10-23T12:00:00+01:00"
    assert sync.plain(actual, "Follow-Up Timing").startswith("Signup final follow-up")
    assert "Signup Nudges Sent" not in changes
    assert "Signup Nudge 1 At" not in changes


def test_invalid_signup_history_cannot_be_reset_by_moving_to_accepted():
    page = warm_row("Replied", count="1", checkpoint=1)
    page["properties"]["Stage (AI Fills First)"]["select"] = {"name": "Accepted"}
    changed = sync.get_changes(page, OBSERVED_NOW)
    actual = {**page["properties"], **changed}
    assert sync.date_value(actual, "Safe Next Due") is None
    assert "Needs Review" in sync.plain(actual, "Follow-Up Timing")
    assert sync.plain(actual, "Copy Follow-Up") == ""


def test_new_accepted_send_observed_between_query_and_read_is_stamped_once(monkeypatch):
    page = row(stage="Accepted", synced=0)
    fresh = copy.deepcopy(page)
    fresh["properties"]["Details Sent"]["checkbox"] = True
    writes = []
    def request(method, path, body=None):
        if method == "GET":
            return copy.deepcopy(fresh)
        writes.append(body["properties"])
    monkeypatch.setattr(sync, "pages", lambda: iter([page]))
    monkeypatch.setattr(sync, "request", request)
    assert sync.sync(OBSERVED_NOW) == (1, 1)
    assert writes[0]["Details Sent At"] == sync.date_patch(OBSERVED_NOW.isoformat())
    assert writes[0]["Safe Next Due"] == sync.date_patch("2026-10-23T20:00:00+01:00")
    assert "Onboarding Sent" not in writes[0]
    assert "Onboarding Sent At" not in writes[0]


def test_accepted_stage_change_does_not_misdate_an_existing_checked_details_flag(monkeypatch):
    page = row(stage="Replied", synced=0)
    page["properties"]["Details Sent"]["checkbox"] = True
    fresh = copy.deepcopy(page)
    fresh["properties"]["Stage (AI Fills First)"]["select"] = {"name": "Accepted"}
    writes = []
    def request(method, path, body=None):
        if method == "GET":
            return copy.deepcopy(fresh)
        writes.append(body["properties"])
    monkeypatch.setattr(sync, "pages", lambda: iter([page]))
    monkeypatch.setattr(sync, "request", request)
    assert sync.sync(OBSERVED_NOW) == (1, 1)
    assert "Details Sent At" not in writes[0]
    assert "Needs Review" in sync.plain(writes[0], "Follow-Up Timing")


def test_erasing_a_known_details_date_during_a_checkbox_edit_does_not_stamp_today():
    page = row(stage="Accepted", synced=0)
    previous = copy.deepcopy(page["properties"])
    previous["Details Sent At"] = sync.date_patch("2026-10-15T10:00:00+01:00")
    page["properties"]["Details Sent"]["checkbox"] = True
    changes = sync.get_changes(page, OBSERVED_NOW, previous=previous)
    assert "Details Sent At" not in changes
    assert "Needs Review" in sync.plain(changes, "Follow-Up Timing")


@pytest.mark.parametrize("stage", ["Replied", "Accepted"])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_shared_count_selects_approved_copy_despite_stale_formula(stage, count):
    page = warm_row(stage, count=str(count), checkpoint=count,
                    first="2026-10-16T12:00:00+01:00" if count else None)
    page["properties"]["Next DM to Copy"]["formula"] = {"type": "string", "string": "stale first message"}
    changes = sync.get_changes(page, OBSERVED_NOW)
    actual = {**page["properties"], **changes}
    assert sync.plain(actual, "Copy Follow-Up") == (sync.WARM_MESSAGES[stage][count] if count < 2 else "")


@pytest.mark.parametrize("stage", ["Replied", "Accepted"])
def test_historical_mismatch_quarantine_survives_retries_and_stage_switch(stage, monkeypatch):
    page = warm_row(stage, count="1", checkpoint=0)
    identity = sync.hashlib.sha256(page["id"].encode()).hexdigest()
    monkeypatch.setattr(sync, "WARM_REVIEW_IDS", {identity})
    for next_stage in (stage, "Accepted", "Replied"):
        page["properties"]["Stage (AI Fills First)"]["select"] = {"name": next_stage}
        changes = sync.get_changes(page, OBSERVED_NOW)
        assert "Signup Nudge 1 At" not in changes
        assert "Warm Last Nudge At" not in changes
        assert "Synced Signup Nudges Sent" not in changes
        apply_patch(page, changes)
        assert sync.date_value(page["properties"], "Safe Next Due") is None
        assert sync.plain(page["properties"], "Copy Follow-Up") == ""
        assert sync.plain(page["properties"], "Follow-Up Timing") == sync.WARM_REVIEW


def test_shared_counter_works_without_retired_onboarding_fields():
    page = warm_row("Accepted", count="1", checkpoint=0)
    for name in ("Onboarding Nudges Sent", "Synced Onboarding Nudges Sent", "Onboarding Nudge 1 At"):
        page["properties"].pop(name)
    changes = sync.get_changes(page, OBSERVED_NOW)
    assert changes["Synced Signup Nudges Sent"] == {"number": 1}
    assert changes["Signup Nudge 1 At"] == sync.date_patch(OBSERVED_NOW.isoformat())


def duplicate_candidate(identifier, name, handle, verified, stage="Ready to Send", created="2026-11-01T00:00:00Z"):
    page = row(stage=stage)
    page["id"] = identifier
    page["created_time"] = created
    page["properties"].update({
        "Candidate": {"title": [{"plain_text": name}]},
        "Instagram Handle": {"rich_text": [{"plain_text": handle}]},
        "Verified Profile URL": {"url": verified},
        "Notes": {"rich_text": []},
    })
    return page


def test_duplicate_instagram_url_variants_and_misspelling_are_held(monkeypatch):
    old = duplicate_candidate("old", "Austin Lenehan", "lenehandz",
                              "https://www.instagram.com/lenehandz/#", created="2026-10-30T10:00:00Z")
    new = duplicate_candidate("new", "Austin lenehab", "lenehandz",
                              "https://www.instagram.com/lenehandz?rpxt=123", created="2026-11-07T10:00:00Z")
    pages = {"old": old, "new": new}
    writes = []
    def request(method, path, body=None):
        identifier = path.rsplit("/", 1)[-1]
        if method == "GET":
            return copy.deepcopy(pages[identifier])
        assert method == "PATCH"
        writes.append((identifier, body["properties"]))
        pages[identifier]["properties"].update(body["properties"])
    monkeypatch.setattr(sync, "all_pipeline_rows", lambda: list(pages.values()))
    monkeypatch.setattr(sync, "request", request)
    assert sync.quarantine_duplicate_profiles() == 1
    assert writes[0][0] == "new"
    assert writes[0][1]["Stage (AI Fills First)"] == {"select": {"name": "Review Hold"}}
    assert "old" in sync.plain(writes[0][1], "Notes")
    assert sync.quarantine_duplicate_profiles() == 0  # Idempotent.


def test_existing_reply_wins_over_older_sendable_duplicate(monkeypatch):
    sendable = duplicate_candidate("older", "Christian Paez", "210.sonic",
                                   "https://instagram.com/210.sonic/", created="2026-10-01T00:00:00Z")
    replied = duplicate_candidate("replied", "Christian Sonic Paez", "210.sonic",
                                  "https://instagram.com/210.sonic", stage="Replied",
                                  created="2026-11-01T00:00:00Z")
    writes = []
    monkeypatch.setattr(sync, "all_pipeline_rows", lambda: [sendable, replied])
    def request(method, path, body=None):
        if method == "GET":
            return copy.deepcopy(sendable)
        writes.append((path, body))
    monkeypatch.setattr(sync, "request", request)
    assert sync.quarantine_duplicate_profiles() == 1
    assert writes[0][0] == "/pages/older"
    assert writes[0][1]["properties"]["Stage (AI Fills First)"]["select"]["name"] == "Review Hold"


def test_duplicate_guard_does_not_patch_changed_or_distinct_profiles(monkeypatch):
    a = duplicate_candidate("a", "Person A", "person_a", "https://instagram.com/person_a")
    b = duplicate_candidate("b", "Person B", "person_a", "https://instagram.com/person_a?x=1",
                            created="2026-11-02T00:00:00Z")
    changed = copy.deepcopy(b)
    changed["properties"]["Verified Profile URL"]["url"] = "https://instagram.com/someone_else"
    monkeypatch.setattr(sync, "all_pipeline_rows", lambda: [a, b])
    writes = []
    def request(method, path, body=None):
        if method == "GET":
            return changed
        writes.append(body)
    monkeypatch.setattr(sync, "request", request)
    assert sync.quarantine_duplicate_profiles() == 0
    assert writes == []
    monkeypatch.setattr(sync, "all_pipeline_rows", lambda: [a, changed])
    assert sync.quarantine_duplicate_profiles() == 0
