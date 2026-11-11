"""Keep UNLXCK's editable Notion follow-up DM synced with its calculated copy.

Run every two minutes using a single server-side timer. Reuse the existing
server-side outreach qualification worker's Notion integration and credentials.
"""

import hashlib
from pathlib import Path
import json
import os
import random
import sys
import time
import traceback
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

DATA_SOURCE_ID = os.getenv("NOTION_OUTREACH_DATA_SOURCE_ID", "08d8b476129a42b0b980102b08ce4bd8")
BASE = "https://api.notion.com/v1"


def notion_token():
    # The existing outreach qualification worker may use a different env name.
    # Reuse its token; do not create another integration or log the value.
    return (os.getenv("NOTION_TOKEN") or os.getenv("NOTION_API_KEY")
            or os.getenv("NOTION_INTEGRATION_TOKEN"))


ACTIVE_STAGES = ("Contacted", "Replied", "Accepted", "Activated")
WARM_STAGES = ("Replied", "Accepted", "Activated")
# New warm follow-up counters are independent of the original cold DM counter.
WARM_COUNTERS = (
    ("Replied", "Signup Nudges Sent", "Synced Signup Nudges Sent", "Signup Nudge 1 At"),
)

# Approved scripts from the outreach SOP; select by shared sequence position.
WARM_MESSAGES = {
    "Replied": (
        "Hey mate, just checking you got the beta link. Any issues signing up? Happy to help.",
        "Final check mate. Still keen to test Unlxck? Here is the link again: https://unlxck.com/signup. No pressure either way.",
    ),
    "Accepted": (
        "Hey mate, were you able to get into Unlxck? If anything is stopping you getting started, let me know.",
        "Quick final check mate. Still keen to give Unlxck a try? I can help you get started. No worries if timing is off.",
    ),
}
# Opaque hashes keep historical CRM identifiers out of the public source tree.
WARM_REVIEW_IDS = frozenset(json.loads(
    Path(__file__).with_name("notion_warm_review.json").read_text())["page_id_sha256"])
WARM_REVIEW = "Needs Review: historical warm count requires Instagram verification"


def warm_review(page):
    identity = page["id"].replace("-", "").lower()
    return hashlib.sha256(identity.encode()).hexdigest() in WARM_REVIEW_IDS


# Only the Contacted stage produces automatic follow-up sends.
# A blank due date for an exception/terminal stage is intentional; Timing explains why.
MISSING_SEND_DATE = "NEEDS DATE: verify the first follow-up's actual Instagram send date."
MISSING_CONTACT_DATE = "NEEDS DATE: record the Instagram initial contact date."
BAD_COUNT = "NEEDS REVIEW: follow-up count conflicts with recorded send history."
LONDON = ZoneInfo("Europe/London")


def date_value(properties, name):
    return (properties.get(name, {}).get("date") or {}).get("start")


def date_patch(value):
    return {"date": {"start": value} if value else None}


def message_patch(value):
    return {"rich_text": [{"text": {"content": value}}] if value else []}


def parse_stamp(value):
    if not value:
        return None
    if "T" not in value:
        return datetime.combine(date.fromisoformat(value), datetime_time.min, LONDON)
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp.replace(tzinfo=LONDON) if stamp.tzinfo is None else stamp.astimezone(LONDON)


def same_date_value(left, right):
    """Compare real timestamps, not Notion's differing timezone/precision renderings."""
    if not left or not right:
        return left == right
    if ("T" in left) != ("T" in right):
        return False
    return parse_stamp(left) == parse_stamp(right)


def now_london(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, datetime_time.min, LONDON)
    return value.replace(tzinfo=LONDON) if value.tzinfo is None else value.astimezone(LONDON)


def due_and_copy(properties, count, first, second, now, formula):
    stage = (properties["Stage (AI Fills First)"].get("select") or {}).get("name")
    if stage != "Contacted":
        return None, "Not in follow-up queue", formula

    contacted = date_value(properties, "Date Contacted")
    if not contacted:
        return None, "Needs contact date", ""
    if count not in (0, 1, 2):
        return None, "Invalid follow-up count", ""
    if count == 2:
        if not first or not second:
            return None, "Complete (send history incomplete)", ""
        # Historical send records cannot be undone, but early second sends
        # should remain visible for investigation rather than silently pass.
        first_stamp = parse_stamp(first)
        after_first = (datetime.combine(first_stamp.date() + timedelta(days=6),
                       datetime_time.min, LONDON) if "T" not in first
                       else (first_stamp.astimezone(timezone.utc) + timedelta(days=5)).astimezone(LONDON))
        day_eight = datetime.combine(
            parse_stamp(contacted).date() + timedelta(days=8), datetime_time.min, LONDON)
        if parse_stamp(second) < max(day_eight, after_first):
            return None, "Complete (timing review)", ""
        return None, "Complete (2 sent)", ""  # Never issue follow-up 3.
    if (count == 0 and (first or second)) or (count == 1 and second):
        return None, "Needs count review", ""

    contacted_local = parse_stamp(contacted).date()
    if count == 0:
        due = datetime.combine(contacted_local + timedelta(days=3), datetime_time.min, LONDON)
        status = "Follow-up 1"
    else:
        if not first:
            return None, "Needs first send date", ""
        first_stamp = parse_stamp(first)
        # Date-only historical evidence has unknown time of day. Wait through day 5.
        after_first = (datetime.combine(first_stamp.date() + timedelta(days=6),
                       datetime_time.min, LONDON) if "T" not in first
                       else (first_stamp.astimezone(timezone.utc) + timedelta(days=5)).astimezone(LONDON))
        day_eight = datetime.combine(contacted_local + timedelta(days=8), datetime_time.min, LONDON)
        due = max(day_eight, after_first)
        status = "Follow-up 2"

    if now < due:
        return due.isoformat(timespec="seconds"), f"{status}: waiting", formula
    return due.isoformat(timespec="seconds"), f"{status}: ready", formula


def missing_send_date(properties):
    return (
        (properties["Stage (AI Fills First)"].get("select") or {}).get("name") == "Contacted"
        and (properties["Follow-Ups Sent"].get("select") or {}).get("name") == "1"
        and not (date_value(properties, "Follow-Up 1 Sent At")
                 or date_value(properties, "Last Follow-Up Sent"))
        and properties["Synced Follow-Ups Sent"].get("number") not in (0, 0.0)
    )


def request(method, path, body=None):
    headers = {
        "Authorization": f"Bearer {notion_token()}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    payload = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        time.sleep(0.35)  # Stay below Notion's average three requests/second.
        try:
            with urlopen(Request(BASE + path, data=payload, headers=headers, method=method), timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            read_only = method == "GET" or path.endswith("/query")
            retryable = exc.code in (429, 529) or (read_only and exc.code in (500, 502, 503, 504))
            if not retryable or attempt == 3:
                raise
            delay = float(exc.headers.get("Retry-After") or min(2 ** attempt, 10))
            time.sleep(delay + random.uniform(0, 0.25))


def pages():
    cursor = None
    while True:
        body = {"page_size": 100, "filter": {"or": [
            {"property": "Stage (AI Fills First)", "select": {"equals": stage}}
            for stage in ACTIVE_STAGES
        ]}}
        if cursor:
            body["start_cursor"] = cursor
        result = request("POST", f"/data_sources/{DATA_SOURCE_ID}/query", body)
        yield from result["results"]
        cursor = result.get("next_cursor")
        if not result.get("has_more"):
            break


def get_changes(page, today, previous=None):
    """Synchronise state without guessing whether an editor or a button supplied a time."""
    p = page["properties"]
    count = int((p["Follow-Ups Sent"].get("select") or {}).get("name") or 0)
    synced = p["Synced Follow-Ups Sent"].get("number")
    calculated = p["Follow-Up DM"].get("formula") or {}
    if calculated.get("type") != "string" or calculated.get("string") is None:
        raise ValueError(f"Missing calculated follow-up for {page['id']}")
    now = now_london(today)
    observed = now.isoformat(timespec="seconds")
    first = date_value(p, "Follow-Up 1 Sent At")
    second = date_value(p, "Follow-Up 2 Sent At")
    legacy = date_value(p, "Last Follow-Up Sent")
    changes = {}

    # Backfill unambiguous existing evidence. Never use a last-send date as the
    # first-send date for a record already at 2: it may belong to follow-up 2.
    if count == 1 and not first and legacy:
        first = legacy
        changes["Follow-Up 1 Sent At"] = date_patch(first)
    elif count == 2 and not second and legacy and synced in (None, 2, 2.0):
        second = legacy
        changes["Follow-Up 2 Sent At"] = date_patch(second)

    increased = synced is not None and int(synced) < count
    consecutive = increased and count - int(synced) == 1
    if consecutive and count == 1 and not first:
        # Polling observation is a conservative upper bound on the send time,
        # not proof of an exact Instagram timestamp.
        first = observed
        changes["Follow-Up 1 Sent At"] = date_patch(first)
        if not legacy:
            changes["Last Follow-Up Sent"] = date_patch(first)
    elif consecutive and count == 2 and not second:
        second = observed
        changes["Follow-Up 2 Sent At"] = date_patch(second)
        changes["Last Follow-Up Sent"] = date_patch(second)
    elif count == 2 and second and not same_date_value(legacy, second):
        # Preserve first-send history in its own property, and maintain the
        # existing compatibility field as the *last* event.
        changes["Last Follow-Up Sent"] = date_patch(second)

    if synced is None or int(synced) != count:
        changes["Synced Follow-Ups Sent"] = {"number": count}

    due, status, message = due_and_copy(
        p, count, first, second, now, calculated["string"])
    if (p["Stage (AI Fills First)"].get("select") or {}).get("name") in WARM_STAGES:
        warm_changes = get_warm_changes(page, today, previous=previous)
        changes.update(warm_changes)
        due, status = warm_schedule({**p, **warm_changes}, now)
        stage = (p["Stage (AI Fills First)"].get("select") or {}).get("name")
        if stage in WARM_MESSAGES:
            if warm_review(page):
                due, status = None, WARM_REVIEW
            count, _, error = warm_nudge_progress({**p, **warm_changes})
            message = WARM_MESSAGES[stage][count] if due and not error and count in (0, 1) else ""
        else:
            # Activated keeps its existing feedback formula and safeguards.
            preview = p["Next DM to Copy"].get("formula") or {}
            if preview.get("type") != "string" or preview.get("string") is None:
                raise ValueError(f"Missing calculated next DM preview for {page['id']}")
            candidate = preview["string"]
            message = candidate if due and candidate.startswith(("How have your", "Any quick thoughts")) else ""
    # A poll missed one or more intermediate send events. Never infer their
    # dates, and never show potentially premature sendable copy.
    if (p["Stage (AI Fills First)"].get("select") or {}).get("name") == "Contacted" and increased and not consecutive:
        due, status, message = None, "Needs count review", ""
    if (p["Stage (AI Fills First)"].get("select") or {}).get("name") == "Contacted" and synced is not None and count < int(synced):
        due, status, message = None, "Needs count review", ""

    current_due = date_value(p, "Safe Next Due")
    if not same_date_value(current_due, due):
        changes["Safe Next Due"] = date_patch(due)
    current_status = plain(p, "Follow-Up Timing")
    if current_status != status:
        changes["Follow-Up Timing"] = message_patch(status)
    current_message = plain(p, "Copy Follow-Up")
    if current_message != message:
        changes["Copy Follow-Up"] = message_patch(message)
    return changes



def elapsed_due(value, days):
    """A full interval across UK DST; date-only history waits through the last day."""
    stamp = parse_stamp(value)
    if "T" not in value:
        return datetime.combine(stamp.date() + timedelta(days=days + 1), datetime_time.min, LONDON)
    return (stamp.astimezone(timezone.utc) + timedelta(days=days)).astimezone(LONDON)


def warm_schedule(properties, now):
    """Schedule shared warm nudges and the unchanged Activated feedback sequence."""
    stage = (properties["Stage (AI Fills First)"].get("select") or {}).get("name")
    if stage == "Activated":
        if (properties.get("Feedback Submitted", {}).get("number") or 0) > 0:
            return None, "Feedback complete"
        requested = date_value(properties, "Feedback Requested At")
        if properties.get("Feedback Reminder Sent", {}).get("checkbox"):
            return None, "Feedback complete (reminder sent)" if requested else "Needs Review: missing feedback request timestamp"
        if requested:
            due, label = elapsed_due(requested, 5), "Feedback reminder"
        else:
            # Activated is founder-confirmed app use, not signup. Reuse the
            # date the existing stage-history pass first observed that stage.
            observed = plain(properties, "Observed Stage")
            anchor = date_value(properties, "Observed Since") if observed == stage else None
            if not anchor:
                return None, "Waiting for activation stage timestamp"
            due, label = elapsed_due(anchor, 4), "Feedback request"
    else:
        signup = stage == "Replied"
        prefix = "Signup" if signup else "Onboarding"
        if signup and date_value(properties, "Registered At"):
            return None, "Signup complete"
        if not signup and properties.get("Onboarding Complete", {}).get("checkbox"):
            return None, "Onboarding complete"
        # One details DM starts one shared sequence across both warm stages.
        root = date_value(properties, "Details Sent At")
        if not root:
            if properties.get("Details Sent", {}).get("checkbox"):
                return None, "Needs Review: details sent date unknown"
            return None, f"{prefix}: waiting for details sent"
        count, first, error = warm_nudge_progress(properties)
        if error:
            return None, error
        if count >= 2:
            return None, f"{prefix} complete (2 nudges sent)"
        due = elapsed_due(first if count else root, 4 if count else 3)
        label = prefix + (" final follow-up" if count else " first follow-up")
    ready = now.astimezone(timezone.utc) >= due.astimezone(timezone.utc)
    return due.isoformat(timespec="seconds"), label + (": ready" if ready else ": waiting")



def warm_nudge_progress(properties):
    """Count actual signup/getting-started sends without mirroring history."""
    legacy = (properties.get("Onboarding Nudges Sent", {}).get("select") or {}).get("name")
    if legacy not in (None, "0") or date_value(properties, "Onboarding Nudge 1 At"):
        return None, None, "Needs Review: legacy onboarding history requires reconciliation"
    total = 0
    first_dates = []
    for _, counter, checkpoint, first_field in WARM_COUNTERS:
        name = (properties[counter].get("select") or {}).get("name") or "0"
        synced = properties[checkpoint].get("number")
        if name not in ("0", "1", "2") or synced not in (None, 0, 1, 2):
            return None, None, "Needs Review: invalid nudge count"
        count = int(name)
        if synced is not None and (count < synced or count > synced + 1):
            return None, None, "Needs Review: skipped or reversed nudge count"
        first = date_value(properties, first_field)
        if (count and not first) or (not count and first):
            return None, None, "Needs Review: nudge timestamp history conflicts with count"
        total += count
        if first:
            first_dates.append(first)
    first = min(first_dates, key=lambda value: parse_stamp(value).astimezone(timezone.utc)) if first_dates else None
    return total, first, None

def get_warm_changes(page, today, previous=None):
    """Reuse existing sent checkboxes/counters; preserve all dated history."""
    p = page["properties"]
    stage = (p["Stage (AI Fills First)"].get("select") or {}).get("name")
    now = now_london(today).isoformat(timespec="seconds")
    changes = {}
    if stage in ("Replied", "Accepted") and p["Details Sent"].get("checkbox") and not date_value(p, "Details Sent At"):
        # A saved waiting status proves the worker previously saw this checkbox
        # unchecked. Query/read false->true is also direct observation evidence.
        waiting = plain(p, "Follow-Up Timing") in (
            "Signup: waiting for details sent", "Onboarding: waiting for details sent")
        newly_checked = previous is not None and previous["Details Sent"].get("checkbox") is False
        erased_date = previous is not None and date_value(previous, "Details Sent At")
        if (waiting or newly_checked) and not erased_date:
            changes["Details Sent At"] = date_patch(now)
    if stage == "Activated" and p["Feedback Requested"].get("checkbox") and not date_value(p, "Feedback Requested At"):
        changes["Feedback Requested At"] = date_patch(now)

    def record_last(value):
        # This existing summary is the latest observed warm send, not a
        # replacement for the separate signup/onboarding first-send dates.
        last = date_value({**p, **changes}, "Warm Last Nudge At")
        if not last or parse_stamp(value).astimezone(timezone.utc) > parse_stamp(last).astimezone(timezone.utc):
            changes["Warm Last Nudge At"] = date_patch(value)

    for _, count_field, synced_field, first_field in (() if warm_review(page) else WARM_COUNTERS):
        count_name = (p[count_field].get("select") or {}).get("name") or "0"
        synced = p[synced_field].get("number")
        if count_name not in ("0", "1", "2") or synced not in (None, 0, 1, 2):
            continue
        count = int(count_name)
        if synced is None:
            # First sight of an existing nonzero counter proves no event time.
            changes[synced_field] = {"number": count}
        elif count == synced + 1:
            changes[synced_field] = {"number": count}
            first = date_value(p, first_field)
            if count == 1:
                if not first:
                    first = now
                    changes[first_field] = date_patch(first)
                record_last(first)
            elif count == 2 and first:
                record_last(now)
        # Do not checkpoint a jump/reversal: the inconsistency must persist.
        # The shared checkpoint survives stage changes and duplicate polls.

    requested = date_value({**p, **changes}, "Feedback Requested At")
    if stage == "Activated" and p.get("Feedback Reminder Sent", {}).get("checkbox") and requested:
        last = date_value({**p, **changes}, "Warm Last Nudge At")
        if not last or parse_stamp(last).astimezone(timezone.utc) <= parse_stamp(requested).astimezone(timezone.utc):
            record_last(now)
    return changes

def sync(today=None):
    fixed_time = today
    today = today or datetime.now(LONDON)
    total = changed = missing_dates = 0
    for page in pages():
        total += 1
        missing_dates += missing_send_date(page["properties"])
        changes = get_changes(page, today)
        changes.update(get_warm_changes(page, today))
        if not changes:
            continue
        # Avoid applying an out-of-date candidate if stage/count changed mid-poll.
        fresh = request("GET", f"/pages/{page['id']}")
        observed_at = fixed_time or datetime.now(LONDON)
        original = page["properties"]
        current = fresh["properties"]
        # Query/read evidence can establish 0->1 on the first poll, without
        # treating a historic nonzero value as a newly recorded send.
        baseline = {}
        for _, counter, checkpoint, _ in WARM_COUNTERS:
            if original[checkpoint].get("number") is None and current[checkpoint].get("number") is None:
                name = (original[counter].get("select") or {}).get("name") or "0"
                if name == "0":
                    baseline[checkpoint] = {"number": 0}
        evaluated = {**fresh, "properties": {**current, **baseline}}
        changes = {**baseline, **get_changes(evaluated, observed_at, previous=original), **get_warm_changes(evaluated, observed_at, previous=original)}
        if changes:
            # Include date/checkpoint edits, not just stage/counter edits.
            latest = request("GET", f"/pages/{page['id']}")
            if latest["properties"] != current or latest.get("last_edited_time") != fresh.get("last_edited_time"):
                continue
            request("PATCH", f"/pages/{page['id']}", {"properties": changes})
            changed += 1
    if missing_dates:
        print(f"Notion follow-ups: {missing_dates} contacted rows need historical send-date verification")
    return total, changed




# Reuse the existing cron and integration for dashboard totals, too.
SCORECARD_PAGE_ID = "3fcd6e718d27815f8509e69f573bcf27"
SCORECARD_SUCCESS_PREFIX = "Last successful scorecard refresh: "


def scorecard_rows():
    cursor = None
    while True:
        body = {"page_size": 100, "filter": {
            "property": "Date Contacted", "date": {"is_not_empty": True},
        }}
        if cursor:
            body["start_cursor"] = cursor
        result = request("POST", f"/data_sources/{DATA_SOURCE_ID}/query", body)
        for page in result["results"]:
            detail = "".join(part.get("plain_text", "") for part in
                             page["properties"]["Source Detail"].get("rich_text", []))
            if detail != "SYSTEM_COUNTER":
                yield page
        if not result.get("has_more"):
            break
        cursor = result["next_cursor"]


def all_pipeline_rows():
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        result = request("POST", f"/data_sources/{DATA_SOURCE_ID}/query", body)
        for page in result["results"]:
            detail = plain(page["properties"], "Source Detail")
            if detail != "SYSTEM_COUNTER":
                yield page
        if not result.get("has_more"):
            break
        cursor = result["next_cursor"]


# A repeated Instagram account must not appear in more than one sendable row.
# Notion does not enforce unique property values, so fail closed at the worker.
def outreach_handle(properties):
    handle = plain(properties, "Instagram Handle").strip().lower().lstrip("@")
    raw = (properties.get("Verified Profile URL", {}).get("url") or "").strip()
    parsed = urlsplit(raw)
    if parsed.hostname and parsed.hostname.lower().removeprefix("www.") in ("instagram.com", "m.instagram.com"):
        slug = unquote(parsed.path).strip("/").split("/", 1)[0].lower()
        if slug and slug not in ("p", "reel", "reels", "stories", "explore", "tv", "direct", "accounts"):
            return slug
    return handle


def quarantine_duplicate_profiles():
    grouped = {}
    for page in all_pipeline_rows():
        handle = outreach_handle(page["properties"])
        if handle:
            grouped.setdefault(handle, []).append(page)
    # Historical or already-held duplicates must not replace their original
    # record on the next poll. For uncontacted stages, keep the oldest row.
    priority = {
        "Activated": 0, "Accepted": 1, "Replied": 2, "Contacted": 3,
        "Rejected": 4, "Screened Out": 4, "Inactive": 4,
    }
    held = 0
    for handle, pages in grouped.items():
        if len(pages) < 2:
            continue
        def order(page):
            stage = (page["properties"]["Stage (AI Fills First)"].get("select") or {}).get("name")
            return priority.get(stage, 5), page.get("created_time") or "", page["id"]
        keep = min(pages, key=order)
        for page in pages:
            if page["id"] == keep["id"]:
                continue
            stage = (page["properties"]["Stage (AI Fills First)"].get("select") or {}).get("name")
            if stage not in ("Ready to Send", "Needs Research", "AI Queue", "Review Hold", None):
                continue  # Never overwrite human-owned post-contact or terminal history.
            if stage == "Review Hold":
                continue
            fresh = request("GET", f"/pages/{page['id']}")
            actual = (fresh["properties"]["Stage (AI Fills First)"].get("select") or {}).get("name")
            if actual != stage or outreach_handle(fresh["properties"]) != handle:
                continue  # Changed while scanning: retry next poll, never patch stale data.
            notes = plain(fresh["properties"], "Notes")
            marker = f"Duplicate Instagram @{handle}; primary record {keep['id']}. Review before outreach."
            note = notes + ("\n" if notes else "") + marker if marker not in notes else notes
            request("PATCH", f"/pages/{page['id']}", {"properties": {
                "Stage (AI Fills First)": {"select": {"name": "Review Hold"}},
                "Notes": rich(note),
            }})
            held += 1
    return held


def block_children(parent_id):
    cursor = None
    while True:
        path = f"/blocks/{parent_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        result = request("GET", path)
        yield from result["results"]
        if not result.get("has_more"):
            break
        cursor = result["next_cursor"]


def block_text(block):
    return "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                   for item in block[block["type"]].get("rich_text", []))


# Record only stable, observed post-contact stage changes; do not invent historical dates.
STAGE_STABILITY_SECONDS = 5 * 60


def plain(p, name):
    return "".join(v.get("plain_text", v.get("text", {}).get("content", ""))
                   for v in p[name].get("rich_text", []))


def rich(value):
    return {"rich_text": [{"text": {"content": value[i:i + 1900]}}
                          for i in range(0, len(value), 1900)]}


def stage_changes(page, now):
    p = page["properties"]
    stage = ((p["Stage (AI Fills First)"].get("select") or {}).get("name") or "")
    if not stage:
        return {}
    observed = plain(p, "Observed Stage")
    if stage != observed or not p["Observed Since"].get("date"):
        return {"Observed Stage": rich(stage),
                "Observed Since": {"date": {"start": now.isoformat(timespec="seconds")}}}
    # Skip stage-history writes if the legacy schema field is absent.
    if "Confirmed Stage" not in p:
        return {}
    confirmed = plain(p, "Confirmed Stage")
    first_seen = datetime.fromisoformat(
        p["Observed Since"]["date"]["start"].replace("Z", "+00:00"))
    # General page edits can delay confirmation; they cannot prove stage continuity.
    edited = page.get("last_edited_time")
    if edited:
        last_edit = datetime.fromisoformat(edited.replace("Z", "+00:00"))
        if last_edit > first_seen and (now - last_edit).total_seconds() < STAGE_STABILITY_SECONDS:
            return {}
    if (now - first_seen).total_seconds() < STAGE_STABILITY_SECONDS:
        return {}
    timestamp = now.isoformat(timespec="seconds")
    contacted = bool(p["Date Contacted"].get("date"))
    rejection_transition = stage == "Rejected" and bool(confirmed) and confirmed != "Rejected"
    # These dates are when the CRM stage was confirmed, not historical DM dates.
    milestones = (
        ("First Reply Confirmed At", contacted and (stage in ("Replied", "Accepted", "Activated")
                                                  or rejection_transition)),
        ("Accepted At", contacted and stage in ("Accepted", "Activated")),
        ("Rejected At", contacted and rejection_transition),
    )
    changes = {prop: {"date": {"start": timestamp}} for prop, reached in milestones
               if reached and not p[prop].get("date")}
    # Repair baselines confirmed by #2925 before milestones existed.
    if confirmed == stage:
        return changes
    previous = confirmed or "baseline"
    history = plain(p, "Stage History")
    entry = f"{first_seen.isoformat(timespec='seconds')} {previous} -> {stage} (confirmed {timestamp})"
    changes.update({
        "Confirmed Stage": rich(stage),
        "Stage Confirmed At": {"date": {"start": timestamp}},
        "Stage History": rich(history + ("\n" if history else "") + entry),
    })
    return changes



def stage_sync_changes(page, now):
    """Keep stage observations and materialized scheduling in the existing pass."""
    changes = stage_changes(page, now)
    p = page["properties"]
    stage = (p["Stage (AI Fills First)"].get("select") or {}).get("name")
    previous = plain(p, "Observed Stage")
    if stage == "Contacted" and previous and previous != stage and not date_value(p, "Date Contacted"):
        # A newly observed contact, not an invented date for a historic baseline.
        changes["Date Contacted"] = date_patch(now_london(now).isoformat(timespec="seconds"))
    if stage not in ACTIVE_STAGES and date_value(p, "Safe Next Due"):
        changes["Safe Next Due"] = date_patch(None)
    return changes

def sync_stage_history(now=None, max_updates=20):
    now = now or datetime.now(timezone.utc)
    checked = updated = 0
    missing_confirmation = 0
    for page in all_pipeline_rows():
        checked += 1
        if "Confirmed Stage" not in page["properties"]:
            missing_confirmation += 1
        changes = stage_sync_changes(page, now)
        if not changes:
            continue
        # Re-read before writing to avoid confirming a stage changed mid-poll.
        fresh = request("GET", f"/pages/{page['id']}")
        if ((page["properties"]["Stage (AI Fills First)"].get("select") or {}).get("name")
                != (fresh["properties"]["Stage (AI Fills First)"].get("select") or {}).get("name")):
            continue
        changes = stage_sync_changes(fresh, now)
        if not changes:
            continue
        request("PATCH", f"/pages/{page['id']}", {"properties": changes})
        updated += 1
        if updated >= max_updates:
            break
    if missing_confirmation:
        print(f"Notion stages: WARNING: {missing_confirmation} examined rows lack "
              "Confirmed Stage (rich_text); historical milestone confirmation is disabled "
              "for those rows", file=sys.stderr)
    return checked, updated


def sync_scorecard():
    rows = list(scorecard_rows())
    total = len(rows)
    counts = {"REPLY RATE": 0, "ACCEPT RATE": 0, "ACTIVATION RATE": 0}
    for page in rows:
        p = page["properties"]
        stage = ((p["Stage (AI Fills First)"].get("select") or {}).get("name") or "")
        if p["First Reply Confirmed At"].get("date") or stage in ("Replied", "Accepted", "Activated", "Rejected"):
            counts["REPLY RATE"] += 1
        if p["Accepted At"].get("date") or stage in ("Accepted", "Activated"):
            counts["ACCEPT RATE"] += 1
        if stage == "Activated":
            counts["ACTIVATION RATE"] += 1

    cards = {}
    success_blocks = []
    def inspect(parent_id):
        for block in block_children(parent_id):
            if (parent_id == SCORECARD_PAGE_ID and block["type"] == "paragraph"
                    and block_text(block).startswith(SCORECARD_SUCCESS_PREFIX)):
                success_blocks.append(block)
            elif block["type"] in ("column_list", "column"):
                inspect(block["id"])
            elif block["type"] == "callout":
                parts = list(block_children(block["id"]))
                # Notion editors can leave spaces inside a bold label span.
                # Match the label without its edge whitespace; preserve its text.
                names = [block_text(part).strip() for part in parts if part["type"] == "paragraph"]
                for name in counts:
                    if name not in names:
                        continue
                    if name in cards:
                        raise ValueError(f"Duplicate scorecard card: {name}")
                    headline = [part for part in parts if part["type"].startswith("heading_")]
                    detail = [part for part in parts if part["type"] == "paragraph"
                              and ((" of " in block_text(part) and block_text(part).endswith(" fighters"))
                                  or block_text(part) == "Awaiting verified app-event integration")]
                    if len(headline) != 1 or len(detail) != 1:
                        raise ValueError(f"Unexpected scorecard layout: {name}")
                    cards[name] = (headline[0], detail[0])

    inspect(SCORECARD_PAGE_ID)
    if set(cards) != set(counts):
        raise ValueError(f"Missing scorecard cards: {set(counts) - set(cards)}")
    if len(success_blocks) > 1:
        raise ValueError("Duplicate scorecard last-success timestamps")

    updated = 0
    for name, (headline, detail) in cards.items():
        percent = f"{100 * counts[name] / total:.1f}%" if total else "0%"
        description = f"{counts[name]} of {total} fighters"
        for block, value in ((headline, percent), (detail, description)):
            if block_text(block) != value:
                request("PATCH", f"/blocks/{block['id']}",
                        {block["type"]: {"rich_text": [{"type": "text", "text": {"content": value}}]}})
                updated += 1
    # Publish success only after every card write returns successfully. Keep the
    # previous timestamp on query/layout/write errors, including partial writes.
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    paragraph = {"rich_text": [{"type": "text", "text": {
        "content": SCORECARD_SUCCESS_PREFIX + stamp}}]}
    if success_blocks:
        request("PATCH", f"/blocks/{success_blocks[0]['id']}", {"paragraph": paragraph})
    else:
        request("PATCH", f"/blocks/{SCORECARD_PAGE_ID}/children", {"children": [
            {"object": "block", "type": "paragraph", "paragraph": paragraph},
        ]})
    return total, updated

def run():
    import fcntl  # Host timer lock; keep calculation/helpers importable on Windows.

    if not notion_token():
        sys.exit("Expose the existing qualification worker Notion token as NOTION_TOKEN or NOTION_API_KEY (never commit it).")
    # Keep overlapping invocations of the existing worker from dating one send twice.
    with open("/tmp/unlxck-notion-followups.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        failed = False
        # Refresh before potentially slow per-row processing. Each branch gets
        # an attempt even if the other fails, while the timer still sees failure.
        try:
            contacted, updated = sync_scorecard()
            print(f"Notion conversion: contacted {contacted}, updated {updated} blocks", flush=True)
        except Exception:
            failed = True
            print("Notion conversion: refresh failed", file=sys.stderr, flush=True)
            traceback.print_exc()
        try:
            # Scheduling depends on the stage observations from this poll.
            held = quarantine_duplicate_profiles()
            print(f"Notion duplicates: held {held} repeated profiles", flush=True)
            checked, updated = sync_stage_history()
            print(f"Notion stages: examined {checked}, updated {updated}", flush=True)
            total, changed = sync()
            print(f"Notion follow-ups: checked {total}, updated {changed}", flush=True)
        except Exception:
            failed = True
            print("Notion stages/follow-ups: sync failed", file=sys.stderr, flush=True)
            traceback.print_exc()
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    run()
