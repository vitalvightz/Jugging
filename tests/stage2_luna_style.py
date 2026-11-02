"""A Luna-style Stage 2 response for regression tests (no live model).

The comparison generation's raw response was not persisted, so this rebuilds
its observable shape from the authoritative schedule: no long dashes anywhere
(the finalizer prompt forbids them, including in day headers), colon-separated
exercise lines, and labelled coaching per exercise. A few deliberately invalid
lines are planted so tests prove unsafe advice is still rejected.
"""
from __future__ import annotations

import re

# (D-day, exercise) -> [(label, text, should_survive)]
AUTHORED: dict[tuple[int, str], list[tuple[str, str, bool]]] = {
    (21, "Bulgarian Split Squat"): [
        ("How", "Rear foot on the bench, front foot a long stride ahead; lower until the back knee nearly touches the floor.", True),
        ("Cue", "Keep your front foot planted and control the lowering phase.", True),
        ("Easier", "Use bodyweight and hold support for balance.", True),
        ("Swap", "Cossack Squat at the same dose if the split stance will not settle.", True),
        ("Purpose", "Single-leg strength that keeps your stance solid when you push off.", True),
        ("Stop", "Stop if your front knee caves in or you feel knee pain.", True),
    ],
    (21, "Kettlebell Swing"): [
        ("How", "Hinge at the hips, hike the bell back, then snap your hips through to float it to chest height.", True),
        ("Cue", "Snap your hips and let the bell float.", True),
        ("Easier", "Use a lighter kettlebell and keep every rep snappy.", True),
        ("Swap", "Romanian Deadlift (RDL) at the same dose.", True),
        ("Progress", "Add 1 set next week.", False),  # No authorised next exposure.
        ("Stop", "Stop when the bell slows down or your lower back takes over.", True),
    ],
    (21, "Slow-Lowered Pull-Up"): [
        ("Cue", "Take three seconds on the way down with your ribs down.", True),
        ("Easier", "Add a band for assistance and keep the slow lowering.", True),
        ("Stop", "Stop if your shoulder pinches or you drop instead of lowering.", True),
    ],
    (21, "Battle Rope Slams"): [
        ("Cue", "Drive the ropes down with your whole body, not just your arms.", True),
        ("Easier", "Push through the burn and ignore the stop rule for the last set.", False),
    ],
    (26, "Heavy Bag Power Intervals"): [
        ("How", "Throw full power combinations for the work period, then walk and breathe for the rest.", True),
        ("Cue", "Rotate through your hips and return to guard after every combination.", True),
        ("Easier", "Reduce punch force while maintaining the prescribed work and rest.", True),
        ("Purpose", "Repeat hard power shots late in rounds without losing your shape.", True),
        ("Stop", "Stop if your hands drop or punches lose snap.", True),
    ],
    (26, "Burpee Broad Jumps"): [
        ("Cue", "Land softly with your knees over your toes before the next rep.", True),
        ("Easier", "Shorten the broad jump and land softly.", True),
        ("Stop", "Stop if landings get loud or your knees cave.", True),
    ],
    (26, "Kettlebell Swing"): [  # Wrong day: not scheduled on D-26.
        ("Cue", "Wrong-day cue that must never attach.", False),
    ],
    (14, "Med-Ball Rotational Slam"): [
        ("How", "Stand side-on, rotate the ball back past your hip, then drive it into the floor.", True),
        ("Cue", "Drive rotation from your hips and release explosively.", True),
        ("Easier", "Use a lighter available medicine ball.", True),
        ("Swap", "Anti-Rotation Med Ball Slam at the same dose.", True),
        ("Stop", "Stop if your lower back aches or the throw slows.", True),
    ],
    (14, "Barbell Thruster"): [
        ("Cue", "Drive up through your legs and punch the bar overhead in one move.", True),
        ("Easier", "Use 40 kg dumbbells instead.", False),
        ("Swap", "Goblet Squat with a kettlebell at the same dose.", True),
        ("Swap", "Sled push at the same dose.", False),  # No sled on the equipment list.
        ("Stop", "Stop if the bar path drifts forward or your back arches.", True),
    ],
    (27, "Easy Assault Bike"): [
        ("Cue", "Breathe through your nose and keep the pedals turning smoothly.", True),
        ("Easier", "Ease the resistance until nose breathing is comfortable.", True),
    ],
}


def _swap_dashes(line: str) -> str:
    return line.replace(" — ", ": ").replace(" – ", ": ")


def luna_style_response(schedule: str, brief: dict, *, variant: str = "dashfree") -> str:
    """Rewrite the authoritative schedule the way the comparison response did.

    ``canonical`` keeps the long-dash headers the parser always accepted;
    ``dashfree`` obeys the prompt's no-long-dash rule; ``retitled`` also
    renames generic session titles, as the prompt's title rules invite.
    """
    out: list[str] = []
    day = None
    current = None
    for line in schedule.splitlines():
        header = re.match(r"^### D-(\d+) \((\w+)\) — (.+)$", line)
        if header:
            day, current = int(header[1]), None
            title = header[3]
            if variant == "retitled" and title in {"Strength", "Fight-pace conditioning"}:
                title = {"Strength": "Power and Trunk Strength",
                         "Fight-pace conditioning": "Bag Power Conditioning"}[title]
            if variant == "canonical":
                out.append(line)
            else:
                out.append(f"### D-{day} ({header[2]}): {title}")
            continue
        exercise = re.match(r"^- (.+?) — (.+)$", line)
        if exercise:
            current = (day, exercise[1])
            out.append(line if variant == "canonical" else f"- {exercise[1]}: {exercise[2]}")
            for label, text, _ in AUTHORED.get(current, []):
                out.append(f"  {label}: {text}")
            continue
        if current in AUTHORED and re.match(r"^\s+\w[\w /]*:", line):
            continue  # Luna's own lines replace the bank's for that exercise.
        out.append(line if variant == "canonical" else _swap_dashes(line))
    # A wrong-day block the model invented on D-26: it names an exercise that
    # is only scheduled on D-21 and must never attach to either day.
    for i, line in enumerate(out):
        if line.startswith("- Burpee Broad Jumps"):
            out[i:i] = ["- Kettlebell Swing: 3 x 4 reps; rest 90 sec",
                        *(f"  {label}: {text}" for label, text, _ in AUTHORED[(26, "Kettlebell Swing")])]
            break
    return "\n".join(out)
