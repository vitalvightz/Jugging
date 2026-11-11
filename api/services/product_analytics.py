"""UTC calendar-day analytics over SQL-aggregated, privacy-safe evidence."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from api.datetimes import parse_utc_datetime

FEATURES = (
    "plans",
    "strength",
    "timer",
    "sparring",
    "history",
    "recovery",
    "nutrition",
    "manual",
    "feedback",
)
EVENT_FEATURES = {
    "app_opened": "onboarding",
    "plan_opened": "plans",
    "plan_editor_opened": "plans",
    "plan_edited": "plans",
    "plan_replaced": "plans",
    "timer_started": "timer",
    "timer_round_completed": "timer",
    "timer_completed": "timer",
    "timer_exited": "timer",
    "history_opened": "history",
    "nutrition_opened": "nutrition",
    "nutrition_saved": "nutrition",
    "feedback_prompt_shown": "feedback",
    "checkin_started": "recovery",
    "sparring_log_started": "sparring",
    # Reminder activation: offer shown -> enable tapped -> subscription saved.
    "push_prompt_shown": "onboarding",
    "push_prompt_accepted": "onboarding",
    "push_subscribed": "onboarding",
}


def ratio(n, d):
    return round(100 * n / d, 2) if d else None


def summarize(
    data, *, days=30, now=None, professional_status=None, sport=None, plan_mode=None
):
    now = now or datetime.now(timezone.utc)
    today = now.date()
    start = today - timedelta(days=days - 1) if days else datetime.min.date()
    athletes = [
        p
        for p in data["athletes"]
        if (
            not professional_status
            or (p.get("professional_status") or "unknown") == professional_status
        )
        and (not sport or sport in (p.get("technical_style") or ["unknown"]))
        and (
            not plan_mode
            or ("plan" if p.get("first_usable_plan_at") else "plan_free") == plan_mode
        )
    ]
    ids = {str(p["id"]) for p in athletes}
    facts = [
        f
        for f in data["facts"]
        if str(f["athlete_id"]) in ids and f["day"] <= today.isoformat()
    ]
    by_athlete = defaultdict(list)
    for f in facts:
        by_athlete[str(f["athlete_id"])].append(f)
    active = defaultdict(set)
    first, last = {}, {}
    for f in facts:
        if f["meaningful"]:
            aid = str(f["athlete_id"])
            active[aid].add(f["day"])
            first[aid] = min(first.get(aid, f["first_at"]), f["first_at"])
            last[aid] = max(last.get(aid, f["last_at"]), f["last_at"])

    # Browser opens are not the only evidence of use. A page view or persisted
    # user action also proves a visit; generated plans and errors do not.
    visit_facts = [
        f
        for f in facts
        if f["meaningful"] or f["action"] in (*EVENT_FEATURES, "page_viewed")
    ]
    seen, latest_action = {}, {}
    visit_days = defaultdict(set)
    for f in visit_facts:
        aid = str(f["athlete_id"])
        visit_days[aid].add(f["day"])
        if aid not in seen or parse_utc_datetime(f["last_at"]) > parse_utc_datetime(
            seen[aid]
        ):
            seen[aid] = f["last_at"]
            latest_action[aid] = {"feature": f["feature"], "action": f["action"]}

    def visitors(n):
        since = (today - timedelta(days=n - 1)).isoformat()
        return len({str(f["athlete_id"]) for f in visit_facts if f["day"] >= since})

    browser_facts = [
        f
        for f in facts
        if f["action"] in (*EVENT_FEATURES, "page_viewed", "frontend_error")
    ]

    def active_users(since, until=today):
        return {
            aid
            for aid, dates in active.items()
            if any(since.isoformat() <= d <= until.isoformat() for d in dates)
        }

    selected = [f for f in facts if start.isoformat() <= f["day"]]
    selected_active = active_users(start)
    signups = [
        p
        for p in athletes
        if start <= parse_utc_datetime(p["created_at"]).date() <= today
    ]

    def retention(n, cohort):
        eligible = [
            p
            for p in cohort
            if parse_utc_datetime(p["created_at"]).date() + timedelta(days=n) < today
        ]
        returned = sum(
            (parse_utc_datetime(p["created_at"]).date() + timedelta(days=n)).isoformat()
            in active[str(p["id"])]
            for p in eligible
        )
        return {
            "eligible": len(eligible),
            "returned": returned,
            "percent": ratio(returned, len(eligible)),
        }

    coverage_start = max(
        datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        min((parse_utc_datetime(p["created_at"]) for p in athletes), default=now),
    )
    enabled_since = {
        str(c["athlete_id"]): parse_utc_datetime(c["since"])
        for c in data.get("optional_coverage", [])
        if str(c["athlete_id"]) in ids and parse_utc_datetime(c["since"]) <= now
    }
    event_ids = {
        str(f["athlete_id"])
        for f in selected
        if f["action"]
        in (*EVENT_FEATURES, "page_viewed", "frontend_error", "operation_failed")
    }
    event_ids.update(
        str(t["athlete_id"])
        for t in data.get("timer_flows", [])
        if str(t["athlete_id"]) in ids
        and t.get("started_at")
        and start <= parse_utc_datetime(t["started_at"]).date() <= today
        and parse_utc_datetime(t["started_at"]) <= now
    )
    measurable = set(enabled_since) | event_ids
    fully_measured = {
        aid
        for aid, since in enabled_since.items()
        if since <= coverage_start and coverage_start >= now - timedelta(days=180)
    }
    coverage = {
        "status": "insufficient"
        if not athletes
        else (
            "not_measured"
            if not measurable
            else ("measured" if len(fully_measured) == len(athletes) else "partial")
        ),
        "measurable_athletes": len(measurable),
        "eligible_athletes": len(athletes),
        "fully_measured_athletes": len(fully_measured),
    }
    optional_available = bool(measurable)

    conversions = []

    def conversion(label, eligible, converted, durations=None):
        conversions.append(
            {
                "label": label,
                "eligible": len(eligible),
                "converted": len(converted),
                "percent": ratio(len(converted), len(eligible)),
                "median_seconds": median(durations) if durations else None,
            }
        )

    staged = defaultdict(dict)
    for f in facts:
        staged[str(f["athlete_id"])][f["action"]] = min(
            staged[str(f["athlete_id"])].get(f["action"], f["first_at"]), f["first_at"]
        )
    started = [p for p in signups if "onboarding_started" in staged[str(p["id"])]]
    onboarded = [p for p in signups if "onboarding_completed" in staged[str(p["id"])]]
    activated = [p for p in signups if str(p["id"]) in first]
    conversion("Signup → onboarding started (observed)", signups, started)
    conversion("Signup → onboarding completed (observed)", signups, onboarded)
    plan_requested = [p for p in onboarded if p.get("starting_path") == "plan_first"]
    conversion(
        "Onboarded plan-first → usable plan",
        plan_requested,
        [p for p in plan_requested if p.get("first_usable_plan_at")],
    )
    conversion(
        "Signup → first meaningful action",
        signups,
        activated,
        [
            max(
                0,
                (
                    parse_utc_datetime(first[str(p["id"])])
                    - parse_utc_datetime(p["created_at"])
                ).total_seconds(),
            )
            for p in activated
        ],
    )
    mature_first = [
        p for p in activated if parse_utc_datetime(first[str(p["id"])]).date() < today
    ]
    conversion(
        "First action → second active day",
        mature_first,
        [p for p in mature_first if len(active[str(p["id"])]) >= 2],
    )
    funnel = []
    stages = [
        ("signup", "onboarding_started"),
        ("onboarding_started", "onboarding_completed"),
        ("onboarding_completed", "first_action"),
        ("first_action", "repeat_activity"),
    ]
    for before, after in stages:
        eligible, converted, durations = [], [], []
        for p in signups:
            aid = str(p["id"])
            stage = dict(staged[aid])
            stage["signup"] = p["created_at"]
            if aid in first:
                stage["first_action"] = first[aid]
                dates = sorted(active[aid])
                if len(dates) > 1:
                    # Exact second active timestamp comes from SQL daily facts.
                    stage["repeat_activity"] = min(
                        f["first_at"]
                        for f in by_athlete[aid]
                        if str(f["athlete_id"]) == aid
                        and f["meaningful"]
                        and f["day"] == dates[1]
                    )
            if not stage.get(before):
                continue
            if (
                before == "first_action"
                and parse_utc_datetime(stage[before]).date() >= today
            ):
                continue  # A second UTC active day has not yet been observable.
            eligible.append(p)
            if stage.get(after) and parse_utc_datetime(
                stage[after]
            ) >= parse_utc_datetime(stage[before]):
                converted.append(p)
                durations.append(
                    (
                        parse_utc_datetime(stage[after])
                        - parse_utc_datetime(stage[before])
                    ).total_seconds()
                )
        funnel.append(
            {
                "label": before.replace("_", " ") + " → " + after.replace("_", " "),
                "eligible": len(eligible),
                "converted": len(converted),
                "percent": ratio(len(converted), len(eligible)),
                "median_seconds": median(durations) if durations else None,
            }
        )
    if signups:
        for metric in (*conversions, *funnel):
            if (
                "onboarding" in metric["label"].lower()
                or "onboarded" in metric["label"].lower()
            ):
                if not started:
                    metric.update(
                        converted=None,
                        percent=None,
                        measurement_note="Onboarding workflow timestamps are not measured for this signup cohort.",
                    )
                elif len(started) < len(signups):
                    metric["measurement_note"] = (
                        "Partial workflow timestamp coverage; historical completion may be unmeasured."
                    )
    sessions = data.get("sessions", [])
    sessions = list(
        {
            s.get("id", i): s
            for i, s in enumerate(sessions)
            if str(s["athlete_id"]) in ids
        }.values()
    )
    known_plans = {
        (str(p["athlete_id"]), str(p["id"]))
        for p in data.get("plans_used", [])
        if p.get("id")
    }
    feature_rows = []
    for feature in FEATURES:
        rows = [f for f in selected if f["feature"] == feature]
        if feature == "plans":
            # A verified completed planned session is evidence of using its
            # plan, not proof of visiting the plan screen. Do not copy events.
            rows += [
                dict(
                    athlete_id=s["athlete_id"],
                    day=parse_utc_datetime(s["completed_at"]).date().isoformat(),
                    action="plan_used_in_training",
                    meaningful=True,
                    count=1,
                )
                for s in sessions
                if (str(s["athlete_id"]), str(s.get("plan_id"))) in known_plans
                and s["status"] in ("done", "modified")
                and s.get("completed_at")
                and start <= parse_utc_datetime(s["completed_at"]).date() <= today
                and parse_utc_datetime(s["completed_at"]) <= now
            ]
        used = {str(f["athlete_id"]) for f in rows if f["meaningful"]}
        counts = defaultdict(int)
        actions = defaultdict(int)
        feature_days = defaultdict(set)
        for f in rows:
            actions[f["action"]] += f["count"]
            if f["meaningful"]:
                counts[str(f["athlete_id"])] += f["count"]
                feature_days[str(f["athlete_id"])].add(f["day"])
        usage_measured = feature != "history" and (
            feature not in ("timer", "nutrition") or optional_available
        )
        feature_rows.append(
            {
                "feature": feature,
                "coverage": coverage
                if feature in ("plans", "timer", "history", "nutrition", "feedback")
                else None,
                "athletes": len(used) if usage_measured else None,
                "percent": ratio(len(used), len(athletes)) if usage_measured else None,
                "uses": sum(counts.values()) if usage_measured else None,
                "repeat_athletes": sum(len(v) >= 2 for v in feature_days.values())
                if usage_measured
                else None,
                "discovered": len(
                    {
                        str(f["athlete_id"])
                        for f in rows
                        if f["action"].endswith("_opened")
                        or f["action"] == "page_viewed"
                    }
                )
                if optional_available
                else None,
                "actions": dict(actions),
            }
        )
    # Session completion rate is matched by persisted completion identity, supplied by SQL separately below.
    starts = [
        s
        for s in sessions
        if s.get("started_at")
        and start <= parse_utc_datetime(s["started_at"]).date() <= today
        and parse_utc_datetime(s["started_at"]) <= now
    ]
    done = [
        s
        for s in starts
        if s.get("completed_at")
        and s["status"] in ("done", "modified")
        and parse_utc_datetime(s["started_at"])
        <= parse_utc_datetime(s["completed_at"])
        <= now
    ]
    abandoned = [
        s
        for s in starts
        if s not in done
        and parse_utc_datetime(s["started_at"]) < now - timedelta(hours=24)
    ]
    conversion("Observed session starts → completed", starts, done)
    timers = [
        t
        for t in data.get("timer_flows", [])
        if str(t["athlete_id"]) in ids
        and t.get("started_at")
        and start <= parse_utc_datetime(t["started_at"]).date() <= today
    ]
    timer_done = [
        t
        for t in timers
        if t.get("completed_at")
        and parse_utc_datetime(t["started_at"])
        <= parse_utc_datetime(t["completed_at"])
        <= now
    ]
    timer_exit = [
        t
        for t in timers
        if t.get("exited_at")
        and parse_utc_datetime(t["started_at"])
        <= parse_utc_datetime(t["exited_at"])
        <= now
    ]
    conversion("Observed free timer starts → completed", timers, timer_done)
    conversions[-1]["coverage"] = coverage
    if not optional_available and athletes:
        conversions[-1].update(eligible=None, converted=None)
    generated = [
        p
        for p in data.get("plans_used", [])
        if str(p["athlete_id"]) in ids
        and start <= parse_utc_datetime(p["created_at"]).date() <= today
        and parse_utc_datetime(p["created_at"]) <= now
    ]
    usable = [
        p for p in generated if p.get("status") in ("ready", "publishable_with_flags")
    ]
    mature = [
        p
        for p in usable
        if parse_utc_datetime(p["created_at"]) + timedelta(days=7) <= now
    ]
    trained = [p for p in mature if p.get("completed_within_7d") is True]
    plan_funnel = {
        "generated": len(generated),
        "generating_athletes": len({str(p["athlete_id"]) for p in generated}),
        "usable": len(usable),
        "review_required": sum(
            p.get("status") in ("review_required", "held_for_review", "needs_review")
            for p in generated
        ),
        "pending": sum(p.get("status") == "generated" for p in generated),
        "unusable": sum(
            p.get("status") not in ("generated", "ready", "publishable_with_flags")
            for p in generated
        ),
        "immature_usable": len(usable) - len(mature),
        "observed_opened": sum(bool(p.get("opened_at")) for p in generated)
        if optional_available
        else None,
        "mature_usable": len(mature),
        "trained_within_7d": len(trained),
        "percent": ratio(len(trained), len(mature)),
    }
    conversion("Generated plans → currently usable", generated, usable)
    conversion("Mature usable plans → training within 7 days", mature, trained)
    completed = [
        s
        for s in sessions
        if s.get("completed_at")
        and s["status"] in ("done", "modified")
        and start <= parse_utc_datetime(s["completed_at"]).date() <= today
        and parse_utc_datetime(s["completed_at"]) <= now
    ]
    manual_logged = sum(
        f["count"]
        for f in selected
        if f["feature"] == "manual" and f["action"] == "session_completed"
    )
    session_metrics = {
        "planned_started": len(starts),
        "planned_completed": sum(s["status"] == "done" for s in completed),
        "planned_modified": sum(s["status"] == "modified" for s in completed),
        "manual_logged": manual_logged,
        "distinct_recorded": len(
            {
                r["id"]
                for r in data.get("training_records", [])
                if str(r["athlete_id"]) in ids
                and start <= parse_utc_datetime(r["occurred_at"]).date() <= today
                and parse_utc_datetime(r["occurred_at"]) <= now
            }
        ),
        "matched_completed": len(done),
    }
    for feature in feature_rows:
        feature["completion"] = None
        if feature["feature"] == "feedback":
            prompts = {}
            for f in selected:
                if f["action"] == "feedback_prompt_shown":
                    aid = str(f["athlete_id"])
                    prompts[aid] = min(prompts.get(aid, f["first_at"]), f["first_at"])
            submitted = {
                str(f["athlete_id"])
                for f in selected
                if f["action"] == "feedback_submitted"
                and str(f["athlete_id"]) in prompts
                and f["last_at"] >= prompts[str(f["athlete_id"])]
            }
            feature["completion"] = {
                "started": len(prompts),
                "completed": len(submitted),
                "percent": ratio(len(submitted), len(prompts)),
            }
            feature["completion_label"] = (
                "Prompted athletes subsequently submitting feedback"
            )

        if feature["feature"] == "feedback" and not optional_available:
            feature["completion"] = None
        if feature["feature"] == "strength":
            feature["completion"] = {
                "started": len(starts),
                "completed": len(done),
                "percent": ratio(len(done), len(starts)),
            }
        if feature["feature"] == "timer":
            feature["completion"] = {
                "started": len(timers),
                "completed": len(timer_done),
                "percent": ratio(len(timer_done), len(timers)),
            }
            feature["observed_early_exits"] = len(timer_exit)
        if feature["feature"] == "timer" and not optional_available:
            feature["completion"] = None
            feature["observed_early_exits"] = None
        if feature["feature"] == "plans":
            feature["completion"] = {
                "started": len(mature),
                "completed": len(trained),
                "percent": ratio(len(trained), len(mature)),
            }
            feature["completion_label"] = (
                "Mature usable plans with training within 7 days"
            )

    cohorts = defaultdict(list)
    for p in signups:
        d = parse_utc_datetime(p["created_at"]).date()
        cohorts[(d - timedelta(days=d.weekday())).isoformat()].append(p)
    errors = defaultdict(lambda: {"count": 0, "athletes": set()})
    for f in selected:
        if f["action"] in ("operation_failed", "frontend_error"):
            k = (f["feature"], f["action"])
            errors[k]["count"] += f["count"]
            errors[k]["athletes"].add(str(f["athlete_id"]))
    for f in data.get("generation_errors", []):
        if (
            str(f["athlete_id"]) in ids
            and start.isoformat() <= f["day"] <= today.isoformat()
        ):
            errors[("plans", "generation_failed")]["count"] += f["count"]
            errors[("plans", "generation_failed")]["athletes"].add(str(f["athlete_id"]))
    dau, wau, mau = (
        len(active_users(today - timedelta(days=n - 1))) for n in (1, 7, 30)
    )
    previous = None
    if days:
        previous = len(
            active_users(start - timedelta(days=days), start - timedelta(days=1))
        )
    new = {
        aid for aid in selected_active if parse_utc_datetime(first[aid]).date() >= start
    }
    trend_counts = defaultdict(int)
    for dates in active.values():
        for d in dates:
            trend_counts[d] += 1
    trend_start = (
        start
        if days
        else min(
            (parse_utc_datetime(p["created_at"]).date() for p in athletes),
            default=today,
        )
    )
    trend = [
        {
            "day": (trend_start + timedelta(days=i)).isoformat(),
            "athletes": trend_counts[(trend_start + timedelta(days=i)).isoformat()],
        }
        for i in range((today - trend_start).days + 1)
    ]
    friction_followup = []
    for (feature, category), group in errors.items():
        eligible_ids, no_return_ids = set(), set()
        for aid in group["athletes"]:
            failures = [
                f
                for f in selected
                if str(f["athlete_id"]) == aid
                and f["feature"] == feature
                and f["action"] == category
            ]
            if not failures:
                continue  # Generation job aggregates lack exact terminal error timestamps.
            failure_at = max(parse_utc_datetime(f["last_at"]) for f in failures)
            if failure_at + timedelta(days=7) > now:
                continue
            observed_return = next(
                (
                    r["returned"]
                    for r in data.get("error_returns", [])
                    if str(r["athlete_id"]) == aid
                    and r["feature"] == feature
                    and r["action"] == category
                    and parse_utc_datetime(r["error_at"]) == failure_at
                ),
                None,
            )
            # Daily first/last facts cannot prove absence inside a seven-day interval.
            if observed_return is None:
                continue
            eligible_ids.add(aid)
            if not observed_return:
                no_return_ids.add(aid)
        friction_followup.append(
            {
                "feature": feature,
                "category": category,
                "eligible_athletes": len(eligible_ids),
                "no_recorded_return_7d": len(no_return_ids),
            }
        )

    def athlete_signals(aid):
        signals = []
        stages = staged[aid]
        if (
            "onboarding_started" in stages
            and parse_utc_datetime(stages["onboarding_started"])
            < now - timedelta(hours=24)
            and "onboarding_completed" not in stages
        ):
            signals.append("Observed onboarding start unresolved for 24h")
        if (
            "onboarding_completed" in stages
            and parse_utc_datetime(stages["onboarding_completed"])
            < now - timedelta(hours=24)
            and aid not in first
        ):
            signals.append("Onboarded; no recorded meaningful action after 24h")
        if (
            aid in first
            and len(active[aid]) == 1
            and parse_utc_datetime(first[aid]).date() < today - timedelta(days=7)
        ):
            signals.append("No recorded return after first active day (7d observed)")
        if any(str(s["athlete_id"]) == aid for s in abandoned):
            signals.append("Observed session start unresolved for 24h")
        for feature in FEATURES:
            if feature == "history":
                continue  # History is discovery-only; it has no completion flow.
            rows = [f for f in by_athlete[aid] if f["feature"] == feature]
            if sum(
                f["count"]
                for f in rows
                if f["action"].endswith("_opened")
                and parse_utc_datetime(f["last_at"]) < now - timedelta(hours=24)
            ) >= 2 and not any(f["meaningful"] for f in rows):
                signals.append(
                    feature + ": repeated discovery without recorded meaningful use"
                )
        return signals

    return {
        "tracking_health": data.get("tracking_health"),
        "tracking_since": data.get("tracking_since"),
        "latest_browser_event": max(
            (f["last_at"] for f in browser_facts), key=parse_utc_datetime, default=None
        ),
        "browser_reporting_athletes": len(
            {
                str(f["athlete_id"])
                for f in browser_facts
                if start.isoformat() <= f["day"]
            }
        ),
        "seen_recently": sum(
            parse_utc_datetime(t) >= now - timedelta(minutes=30) for t in seen.values()
        ),
        "no_visit_7d": len(athletes) - visitors(7),
        "visitors_today": visitors(1),
        "visitors_7d": visitors(7),
        "visitors_30d": visitors(30),
        "friction_followup": friction_followup,
        "as_of": now.isoformat(),
        "coverage": coverage,
        "plan_funnel": plan_funnel,
        "session_metrics": session_metrics,
        "days": days,
        "registered": len(athletes),
        "ever_active": len(first),
        "active": len(selected_active),
        "previous_active": previous,
        "dau": dau,
        "wau": wau,
        "mau": mau,
        "stickiness": ratio(dau, mau),
        "new_active": len(new),
        "returning_active": len(selected_active - new),
        "average_active_days": round(
            sum(sum(d >= start.isoformat() for d in dates) for dates in active.values())
            / len(athletes),
            2,
        )
        if athletes
        else None,
        "sessions_started": len(starts),
        "sessions_completed": len(completed),
        "observed_session_abandonments": len(abandoned),
        "inactive": {
            str(n): sum(
                parse_utc_datetime(last[aid]) <= now - timedelta(days=n) for aid in last
            )
            for n in (7, 14, 30)
        },
        "never_active": len(athletes) - len(first),
        "retention": {str(n): retention(n, signups) for n in (1, 7, 30)},
        "cohorts": [
            {
                "week": w,
                "signups": len(ps),
                "retention": {str(n): retention(n, ps) for n in (1, 7, 30)},
            }
            for w, ps in sorted(cohorts.items())
        ],
        "trend": trend,
        "funnel": funnel,
        "features": sorted(feature_rows, key=lambda f: -(f["athletes"] or 0)),
        "conversions": conversions,
        "dropoffs": {
            "observed_onboarding_not_completed_24h": sum(
                "onboarding_started" in staged[str(p["id"])]
                and parse_utc_datetime(staged[str(p["id"])]["onboarding_started"])
                < now - timedelta(hours=24)
                and "onboarding_completed" not in staged[str(p["id"])]
                for p in signups
            ),
            "observed_onboarded_no_action_24h": sum(
                str(p["id"]) not in first
                and parse_utc_datetime(staged[str(p["id"])]["onboarding_completed"])
                < now - timedelta(hours=24)
                for p in onboarded
            ),
            "repeated_feature_discovery_no_action_24h": sum(
                any(
                    sum(
                        f["count"]
                        for f in selected
                        if str(f["athlete_id"]) == str(p["id"])
                        and f["feature"] == feature
                        and f["action"].endswith("_opened")
                        and parse_utc_datetime(f["last_at"]) < now - timedelta(hours=24)
                    )
                    >= 2
                    and not any(
                        f["meaningful"] and f["feature"] == feature
                        for f in by_athlete[str(p["id"])]
                    )
                    for feature in FEATURES
                    if feature != "history"
                )
                for p in athletes
            )
            if optional_available
            else None,
            "observed_plan_opened_no_action": sum(
                any(
                    f["action"] == "plan_opened"
                    and f["day"] >= start.isoformat()
                    and parse_utc_datetime(f["last_at"]) < now - timedelta(hours=24)
                    for f in by_athlete[str(p["id"])]
                )
                and str(p["id"]) not in first
                for p in athletes
            )
            if optional_available
            else None,
            "first_day_no_return_7d": sum(
                len(active[aid]) == 1
                and parse_utc_datetime(first[aid]).date() >= start
                and parse_utc_datetime(first[aid]).date() < today - timedelta(days=7)
                for aid in first
            ),
        },
        "errors": [
            {
                "feature": k[0],
                "category": k[1],
                "count": v["count"],
                "affected_athletes": len(v["athletes"]),
                "errors_per_athlete": round(v["count"] / len(v["athletes"]), 2),
            }
            for k, v in errors.items()
        ],
        "segments": {
            "professional_status": sorted(
                {p.get("professional_status") or "unknown" for p in data["athletes"]}
            ),
            "sports": sorted(
                {
                    s
                    for p in data["athletes"]
                    for s in (p.get("technical_style") or ["unknown"])
                }
            ),
            "acquisition": "Unavailable",
        },
        "athletes": [
            {
                "id": str(p["id"]),
                "full_name": (p.get("full_name") or "").strip() or None,
                "email": (p.get("email") or "").strip() or None,
                "registered_at": p["created_at"],
                "last_seen": seen.get(str(p["id"])),
                "latest_action": latest_action.get(str(p["id"])),
                "visit_days_7": sum(
                    d >= (today - timedelta(days=6)).isoformat()
                    for d in visit_days[str(p["id"])]
                ),
                "visit_days_30": sum(
                    d >= (today - timedelta(days=29)).isoformat()
                    for d in visit_days[str(p["id"])]
                ),
                "signals": athlete_signals(str(p["id"])),
                "first_activity": first.get(str(p["id"])),
                "last_activity": last.get(str(p["id"])),
                "active_days_7": sum(
                    d >= (today - timedelta(days=6)).isoformat()
                    for d in active[str(p["id"])]
                ),
                "active_days_30": sum(
                    d >= (today - timedelta(days=29)).isoformat()
                    for d in active[str(p["id"])]
                ),
                "inactivity_days": (
                    today - parse_utc_datetime(last[str(p["id"])]).date()
                ).days
                if str(p["id"]) in last
                else None,
            }
            for p in athletes
        ],
    }
