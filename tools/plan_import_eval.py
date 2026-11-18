"""Measure how accurately the real model reads coach programmes.

Each case is a programme as the coach wrote it (``<case>.txt``, or screenshots
``<case>-1.jpg``, ``<case>-2.jpg`` ...) next to a hand-checked reading of it
(``<case>.json``, in the model's own output shape). The script sends each
programme to the configured model once, turns both readings into Custom Build
drafts with the same code the app uses, and counts the differences:

* sessions and exercises missed or invented,
* wrong days, session types and session notes,
* exercise fields that differ (dose, cues, stop rules, equipment, notes),
* numbers the app's own source check flags in the model's reading.

Run it before launch and after any change to the import prompt or model:

    OPENAI_API_KEY=... python tools/plan_import_eval.py [--cases DIR] [--json]

or, on GitHub, run the "Plan Import Accuracy" workflow (it needs an
OPENAI_API_KEY repository secret).

It calls the model, so it counts toward the API bill (one call per case), and
it is not part of the test suite.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.plan_import import ReadPlan, build_draft, get_plan_reader  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "plan_import"
START = date(2026, 11, 23)
FIELDS = ("dose", "coaching_cues", "stop_rules", "equipment", "notes")


def cases(folder: Path):
    """(name, text, images, truth) for each hand-checked programme."""
    found = [(FIXTURES / "coach_programme.txt", FIXTURES / "coach_programme.model.json")]
    found += [(text, text.with_suffix(".json")) for text in sorted(folder.glob("*.txt"))]
    found += [(None, truth) for truth in sorted(folder.glob("*.json")) if not truth.with_suffix(".txt").exists()]
    for text, truth in found:
        name = truth.stem.removesuffix(".model")
        images = [f"data:image/jpeg;base64,{base64.b64encode(p.read_bytes()).decode()}"
                  for p in sorted(truth.parent.glob(f"{name}-*.jpg"))]
        yield name, text.read_text() if text else "", images, ReadPlan.model_validate_json(truth.read_text())


def sessions(reading: ReadPlan) -> dict[tuple[int, str], dict]:
    """The first week's sessions, keyed by order and name, as the app would draft them."""
    draft = build_draft(reading, START)
    waiting = {sid for group in draft.needs_day for sid in group.session_ids}
    # The first week with training (a rest week first has none).
    size = next((len(week.sessions) for week in reading.weeks if week.sessions), 0)
    first = draft.plan["sessions"][:size]
    return {(i, s["name"].lower()): {**s, "day": None if s["id"] in waiting else s["weekday"]} for i, s in enumerate(first)}


def compare(truth: ReadPlan, read: ReadPlan, source: str = "") -> dict:
    errors: list[str] = []
    counts = {"sessions": 0, "exercises": 0, "fields": 0, "errors": 0}
    want, got = sessions(truth), sessions(read)
    by_name = {name: value for (_, name), value in got.items()}
    weeks = (sum(max(1, w.repeat or 1) for w in truth.weeks), sum(max(1, w.repeat or 1) for w in read.weeks))
    if weeks[0] != weeks[1]:
        errors.append(f"weeks: expected {weeks[0]}, read {weeks[1]}")
    for (_, name), expected in want.items():
        counts["sessions"] += 1
        actual = by_name.pop(name, None)
        if actual is None:
            errors.append(f"missed session {expected['name']!r}")
            continue
        for key in ("day", "session_type", "notes"):
            if expected.get(key) != actual.get(key):
                errors.append(f"{expected['name']}: {key} expected {expected.get(key)!r}, read {actual.get(key)!r}")
        have = {e["name"].lower(): e for e in actual["exercises"]}
        for exercise in expected["exercises"]:
            counts["exercises"] += 1
            match = have.pop(exercise["name"].lower(), None)
            if match is None:
                errors.append(f"{expected['name']}: missed exercise {exercise['name']!r}")
                continue
            for key in FIELDS:
                counts["fields"] += 1
                left = {k: v for k, v in exercise[key].items() if v is not None} if key == "dose" else exercise.get(key)
                right = {k: v for k, v in match[key].items() if v is not None} if key == "dose" else match.get(key)
                if left != right:
                    errors.append(f"{expected['name']} · {exercise['name']}: {key} expected {left!r}, read {right!r}")
        errors += [f"{expected['name']}: invented exercise {extra['name']!r}" for extra in have.values()]
    errors += [f"invented session {extra['name']!r}" for extra in by_name.values()]
    # What the app itself would flag after checking the model's numbers against the text.
    if source:
        errors += [f"source check: {flag.where}: {flag.message}" for flag in build_draft(read, START, source).flags
                   if flag.message.startswith(("Check the dose", "Couldn't find"))]
    counts["errors"] = len(errors)
    return {"counts": counts, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=FIXTURES / "eval")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()
    reader = get_plan_reader()
    if reader is None:
        print("No plan reader is configured. Set OPENAI_API_KEY (and UNLXCK_PLAN_IMPORT_MODEL if needed).")
        return 2
    report, total = {}, {"sessions": 0, "exercises": 0, "fields": 0, "errors": 0}
    for name, text, images, truth in cases(args.cases):
        try:
            result = compare(truth, reader.read(text.strip(), images), text)
        except Exception as exc:  # A failed read is a result too.
            result = {"counts": {"sessions": 0, "exercises": 0, "fields": 0, "errors": 1}, "errors": [f"read failed: {exc}"]}
        report[name] = result
        for key in total:
            total[key] += result["counts"][key]
        if not args.json:
            print(f"\n{name}: {result['counts']['errors']} differences "
                  f"({result['counts']['sessions']} sessions, {result['counts']['exercises']} exercises checked)")
            for line in result["errors"]:
                print(f"  - {line}")
    checked = total["sessions"] * 3 + total["exercises"] + total["fields"]
    accuracy = 1 - total["errors"] / checked if checked else 0
    if args.json:
        print(json.dumps({"cases": report, "total": total, "accuracy": round(accuracy, 4)}, indent=2))
    else:
        print(f"\nTotal: {total['errors']} differences in {checked} checked values ({accuracy:.1%} exact).")
    return 0 if total["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
