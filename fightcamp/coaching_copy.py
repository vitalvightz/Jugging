"""Concise exercise guidance; prescription authority stays with the planner."""
import re
from copy import deepcopy

_NULL_PROGRESSION_RE = re.compile(
    r"^\s*(?:\*\*)?(?:progress(?:ion)?\s*:\s*)?(?:\*\*)?\s*(?:none|n/?a|no(?:ne)?\s+progression|no\s+change|hold|not\s+applicable)"
    r"(?:\s+(?:this|for\s+this)\s+(?:block|week|phase|camp))?\s*[.!]?\s*$",
    re.I,
)


def is_null_progression(text) -> bool:
    """A written-out absence ("Progress: none this block.") is no instruction."""
    return isinstance(text, str) and bool(_NULL_PROGRESSION_RE.match(text))

# Authored replacements, rather than word slicing: every stop trigger survives.
_COMPACT_COPY = {
    "Keep the stroke relaxed and breathe regularly; avoid breath holds.": "Relax your stroke; breathe regularly without breath holds.",
    "Slow the stroke and relax the pull while keeping breathing comfortable.": "Slow your stroke; keep breathing comfortable.",
    "End the swim if shoulder pain, dizziness or breathing difficulty appears.": "Stop: shoulder pain, dizziness or breathing difficulty.",
    "Keep a relaxed rhythm and breathing comfortable throughout the aerobic work.": "Keep a relaxed rhythm and comfortable breathing.",
    "Ease the pace or resistance until breathing settles; do not chase the effort target.": "Ease pace until breathing settles; ignore the effort target.",
    "End the work if dizziness, chest discomfort or unusual breathlessness appears.": "Stop: dizziness, chest discomfort or unusual breathlessness.",
    "Sit into the working hip with the heel grounded and knee tracking the toes.": "Sit into your hip; heel grounded, knee tracking toes.",
    "Use a shallower pain-free depth; keep the heel grounded.": "Use shallower pain-free depth; keep your heel grounded.",
    "End the set if knee or groin pain appears or the heel lifts.": "Stop: knee/groin pain or heel lifting.",
    "Keep the pelvis level and use the straps to steady the single-leg position.": "Keep your pelvis level; steady yourself with straps.",
    "Take more support through the straps without extending the hold.": "Use more strap support; keep the same hold time.",
    "End the hold if joint pain appears or the pelvis drops despite strap support.": "Stop: joint pain or pelvis dropping despite strap support.",
    "Set a balanced stance with soft knees and ribs stacked over the pelvis.": "Balance your stance; soft knees, ribs over pelvis.",
    "Use a more comfortable stance width and keep the position relaxed.": "Use a comfortable stance width; stay relaxed.",
    "End the hold if pain appears or you cannot keep a balanced stance.": "Stop: pain or loss of stance balance.",
    "Land quietly, control the knee and reset the stance before the next bound.": "Land quietly; control your knee, then reset your stance.",
    "Shorten the bound to a distance you can land and balance cleanly.": "Shorten bounds until you can land and balance cleanly.",
    "End the set if knee or ankle pain appears or landings become unstable.": "Stop: knee/ankle pain or unstable landings.",
    "Stick each landing with the knee tracking the toes; reset before the next take-off.": "Stick landings; knees track toes, reset before take-off.",
    "Reduce jump distance or height until each landing is quiet and stable.": "Reduce height/distance until landings stay quiet and stable.",
    "End the set if pain appears, jump quality drops or you cannot stick the landing.": "Stop: pain, poorer jumps or unstable landings.",
    "Drive the sled with quick steps and a braced trunk; keep each acceleration crisp.": "Brace your trunk; drive quick steps and crisp accelerations.",
    "Use less sled resistance so steps stay quick without changing the work or rest.": "Lower sled resistance; keep steps quick, work/rest unchanged.",
    "End the effort if speed drops, the trunk collapses or pain appears.": "Stop: slower speed, trunk collapse or pain.",
    "Keep the trunk braced and steps steady; move the load without twisting or jerking.": "Brace; step steadily without twisting or jerking the load.",
    "Reduce the carried or sled load until steps are steady.": "Reduce load until steps stay steady.",
    "End the effort if pain appears or you cannot keep the trunk and steps controlled.": "Stop: pain or loss of trunk/step control.",
    "Drive through the legs and keep the trunk braced; finish each rep without grinding.": "Drive through legs; brace your trunk, avoid grinding reps.",
    "Use a lighter load so the same movement stays crisp and controlled.": "Lower load until reps stay crisp and controlled.",
    "End the set if speed drops, trunk control fails or pain appears.": "Stop: slower speed, lost trunk control or pain.",
    "Keep punches balanced, return to guard and reset the stance between exchanges.": "Keep punches balanced; return to guard.",
    "Ease punch force and simplify the exchange until balance and guard return are clean.": "Ease punches; simplify exchanges until balance/guard return stay clean.",
    "End the effort if punch speed, guard return or balance deteriorates, or pain appears.": "Stop: slower punches, poorer guard/balance or pain.",
    "Push off a stable stance and regain balance before the next effort.": "Push from a stable stance; regain balance between efforts.",
    "Ease acceleration until you can brake and reset under control.": "Ease acceleration until braking and resets stay controlled.",
    "End the effort if speed drops, braking becomes uncontrolled or pain appears.": "Stop: slower speed, uncontrolled braking or pain.",
    "Drive the throw from the legs and trunk; reset your balance before the next release.": "Drive throws through legs/trunk; reset balance before each release.",
    "Use a lighter ball and reduce throw force until the release stays controlled.": "Use lighter balls; ease force until releases stay controlled.",
    "End the set if throw speed drops, balance is lost or shoulder or back pain appears.": "Stop: slower throws, lost balance or shoulder/back pain.",
    "Keep the trunk braced and the movement controlled; avoid grinding repetitions.": "Brace your trunk; control movement without grinding reps.",
    "Use a lighter load or more assistance through the same pain-free movement.": "Lower load/add assistance; keep the same pain-free movement.",
    "End the set if pain appears or you lose trunk or joint control.": "Stop: pain or lost trunk/joint control.",
}
_COMPACT_VALUES = frozenset(_COMPACT_COPY.values())


def compact_instruction(text):
    text = _text(text)
    return _COMPACT_COPY.get(text, text)


def _text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _unique(values):
    seen = set()
    result = []
    for value in values or []:
        text = _text(value)
        key = re.sub(r"[\W_]+", " ", text.casefold()).strip()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def short_cue(text):
    """The cue to show: known bank wording in its authored compact form.

    Any other cue is validated coaching and is kept whole. Clipping it to a
    seven-word clause deleted technique ("...and control the lowering phase")
    and, for long prose, could leave an unsafe fragment.
    """
    text = re.sub(r"^Cue:\s*", "", _text(text), flags=re.I)
    return _COMPACT_COPY.get(text, text)


def readable_stop(rules):
    rules = _unique([re.sub(r"^Stop(?: rule)?:\s*", "", compact_instruction(rule), flags=re.I)
                     for rule in rules or []])
    if len(rules) <= 1:
        return rules
    conditions = []
    for rule in rules:
        # Keep exceptional instructions (e.g. seek medical help) verbatim.
        if re.search(r"\b(?:seek|call|medical|emergency)\b", rule, re.I):
            return [" ".join(re.sub(r"^(if|when)\b", lambda m: "Stop " + m[1].lower(), r,
                                    flags=re.I).rstrip(". ") + "." for r in rules)]
        condition = re.sub(r"^(?:stop(?:\s+(?:the\s+)?(?:set|effort|hold|swim|work))?\s+)?"
                           r"(?:if|when|for|on)\s+", "", rule, flags=re.I).rstrip(". ")
        if re.match(r"^(?:stop|end|seek|call|do not|never)\b", condition, re.I):
            return [" ".join(r.rstrip(". ") + "." for r in rules)]
        conditions.append(condition if condition.split()[0].isupper() else condition[0].lower() + condition[1:])
    conditions = _unique(conditions)
    conditions = [c for c in conditions if not any(
        c != other and re.search(r"(?:^|, | or )" + re.escape(c) + r"(?:$|,| or )", other, re.I)
        for other in conditions)]
    joined = " or ".join(conditions) if len(conditions) <= 2 else ", ".join(conditions[:-1]) + ", or " + conditions[-1]
    return ["Stop if " + joined + "."]


def concise_coaching(fields):
    """One execution cue, source-authored Easier options, and lossless stops.

    Idempotent by construction: recovery never appends an invented alternative.
    Preserve each distinct AI-authored option without rewriting its wording.
    """
    result = deepcopy(fields)
    cues = []
    stops = list(result.get("stop_rules") or [])
    for value in result.get("coaching_cues") or []:
        cue = re.sub(r"^Cue:\s*", "", _text(value), flags=re.I)
        # Legacy converters can put safety into a second cue. Route it before
        # selecting the one execution instruction; never discard the trigger.
        if re.match(r"^Stop\b", cue, re.I):
            stops.append(cue)
            continue
        embedded = re.search(r"[.;]\s*(Stop\b.+)$", cue, re.I)
        if embedded:
            stops.append(embedded[1])
            cue = cue[:embedded.start()].strip()
        if cue:
            cues.append(cue)
    result["coaching_cues"] = []
    if cues:
        # A legacy coaching array may start with dose-only evidence. Doses stay
        # in the prescription fields; choose the actual execution instruction.
        execution = [cue for cue in cues if not re.match(
            r"^\d+(?:[–-]\d+|\.\d+)?\s*(?:sets?\s*[x×]|[x×]\s*\d|(?:s|sec|seconds)\s+work\b|(?:min|minutes)\b)",
            cue, re.I)]
        lead = short_cue((execution or cues)[0])
        result["coaching_cues"] = [lead]
    options = _unique(result.get("regression_options"))
    result["regression_options"] = options  # Validated AI wording is the source; never rewrite it.
    result["stop_rules"] = readable_stop(stops)
    return result
