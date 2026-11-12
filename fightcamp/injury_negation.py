import re
from collections.abc import Iterable
from functools import lru_cache

from .normalization import strip_surrounding_punctuation as _strip_surrounding_punct
from .regex_config import compile_regex

NEGATION_CUES = {
    "no",
    "not",
    "never",
    "without",
    "deny",
    "denies",
    "denied",
    "neither",
    "ruled out",
}

_NEGATION_CUE_PATTERN = compile_regex("injury_synonyms", "negation_cue_pattern")

_NEGATION_TARGETS: list[str] = sorted(
    {
        term.strip()
        for term in ["injury", "injured", "issue", "issues", "problem", "problems"]
        if term and term.strip()
    },
    key=len,
    reverse=True,
)


def register_negation_targets(extra_terms: Iterable[str]) -> None:
    global _NEGATION_TARGETS
    merged = {
        term.strip()
        for term in ([*_NEGATION_TARGETS, *list(extra_terms)])
        if term and term.strip()
    }
    _NEGATION_TARGETS = sorted(merged, key=len, reverse=True)


def _normalize_injury_text_separators(text: str) -> str:
    _INJURY_TEXT_SEPARATORS = [
        ",",
        ";",
        "\n",
        " - ",
        f" {chr(0x2013)} ",
        f" {chr(0x2014)} ",
        " then ",
        " + ",
        "+",
        "/",
        "|",
    ]
    _LEGACY_MOJIBAKE_DASH_SEPARATORS = [
        f" {chr(0x00e2)}{chr(0x20ac)}{chr(0x201c)} ",
        f" {chr(0x00e2)}{chr(0x20ac)}{chr(0x201d)} ",
    ]
    normalized = text
    for sep in [*_INJURY_TEXT_SEPARATORS, *_LEGACY_MOJIBAKE_DASH_SEPARATORS]:
        normalized = normalized.replace(sep, ". ")
    return normalized


# Strong injury-negation cues. When one of these opens a clause it negates the
# whole clause on its own ("no shoulder pain", "denies knee pain", "never had
# knee issues") — these words carry no benign clinical reading.
_STRONG_LEADING_CUES = {"no", "never", "neither", "deny", "denies", "denied"}

# Soft cues. "not" and "without" frequently describe uncertainty ("not sure"),
# severity ("not severe"), progression ("not improving"), equipment ("without
# brace") or timing ("without warning") rather than negating an injury. They
# only strip a clause when they sit directly in front of a symptom/injury word.
_SOFT_LEADING_CUES = {"not", "without"}

# Determiners/connectors that may sit between a soft cue and the symptom it
# negates ("not any swelling", "without a torn ligament") — skipped when we look
# for the first content token after the cue.
_NEGATION_DETERMINERS = {
    "a", "an", "any", "the", "my", "his", "her", "their", "your", "our", "of",
}

# Symptom/injury head-words. A soft cue ("not"/"without") only negates when the
# first content token after it names a symptom or injury; otherwise the cue is
# qualifying something benign and the clause is kept.
_INJURY_SYMPTOM_TOKENS = {
    "pain", "painful", "pains", "ache", "aches", "aching", "achy",
    "sore", "soreness", "swelling", "swollen", "swell",
    "stiff", "stiffness", "tight", "tightness",
    "sprain", "sprained", "strain", "strained",
    "tear", "torn", "tears", "tearing",
    "tendonitis", "tendinitis", "tendinopathy", "tendinosis",
    "fracture", "fractured", "fractures", "break", "broken", "breaking",
    "dislocation", "dislocated", "dislocate", "dislocates",
    "rupture", "ruptured", "ruptures",
    "injury", "injured", "injuries", "issue", "issues", "problem", "problems",
    "numb", "numbness", "tingling", "tingle", "spasm", "spasms",
    "cramp", "cramps", "cramping", "weak", "weakness",
    "tender", "tenderness", "inflammation", "inflamed",
    "bruise", "bruised", "bruising", "contusion",
    "splint", "splints", "instability", "unstable", "wobbly",
    "click", "clicking", "pop", "popping", "lock", "locking",
    "concussion", "nerve", "hyperextension", "hyperextended",
    "dizzy", "dizziness", "lightheaded", "lightheadedness", "headache", "vision",
    "neurological", "neurologic",
}


def _is_injury_symptom_token(token: str) -> bool:
    return token.strip("().,;:'\"-").replace("-", "") in _INJURY_SYMPTOM_TOKENS


def _soft_cue_negates(rest_words: list[str]) -> bool:
    """Whether a leading soft cue ("not"/"without") negates an injury.

    The cue only negates when its first content token (skipping determiners)
    is itself a symptom/injury word. This keeps benign uses intact: "not severe
    ankle swelling", "not sure ...", "not improving Achilles pain", "without
    warning ...", "without brace ..." all describe severity/uncertainty/
    progression/equipment/timing, not an absent injury.
    """
    from .injury_danger_terms import MEDICAL_SYMPTOM_PHRASES

    content = [word.strip("().,;:'\"-") for word in rest_words]
    while content and (not content[0] or content[0] in _NEGATION_DETERMINERS):
        content.pop(0)
    rest = " ".join(content).replace("-", " ")
    if any(rest == phrase or rest.startswith(phrase + " ") for phrase in MEDICAL_SYMPTOM_PHRASES):
        return True
    for word in content:
        token = word.strip("().,;:'\"-")
        if not token:
            continue
        if token in _NEGATION_DETERMINERS:
            continue
        return _is_injury_symptom_token(token)
    return False


def _clause_negates_injury(clause: str) -> bool:
    """Whether a single clause should be dropped as a negated injury.

    Forward-scoped: only a cue that *opens* the clause (or an explicit "ruled
    out") negates it, so trailing negations like "shoulder clicking no pain" and
    "knee pain without brace" keep their non-negated injury content.
    """
    cleaned = clause.strip()
    if not cleaned:
        return False
    if re.search(r"\bruled\s+out\b", cleaned):
        return True
    words = cleaned.lower().split()
    if not words:
        return False
    first = words[0].strip("().,;:'\"-")
    if first in _STRONG_LEADING_CUES:
        return True
    if first in _SOFT_LEADING_CUES:
        return _soft_cue_negates(words[1:])
    return False


def _has_negated_injury(text: str) -> bool:
    lowered = text.lower()
    if not _NEGATION_CUE_PATTERN.search(lowered):
        return False
    if re.search(r"\bruled\s+out\s+\w+", lowered):
        return True
    # A negation cue at the start of a phrase negates it even when no generic
    # injury word ("injury"/"issue") follows. This catches symptom-level
    # negations like "no shoulder pain" or "no shin splints" that the
    # entity-based Negex pass misses. Strong cues fire unconditionally; "not"/
    # "without" only fire when they sit in front of a symptom, so "not severe
    # ankle swelling" or "without warning my Achilles popped" stay intact.
    if _clause_negates_injury(lowered):
        return True
    # Negation scopes forward: only treat a generic target word as negated when a
    # cue actually precedes it. This keeps "knee pain without brace" intact (the
    # cue follows the symptom) while still catching "never had knee issues".
    # Only strong cues scope across intervening words here; "not"/"without" are
    # deliberately excluded so "not severe ankle swelling" or "without warning my
    # Achilles popped" are not falsely flagged when a registered symptom word
    # ("swelling") appears later in the clause. Those soft cues only negate via
    # the adjacency check in _clause_negates_injury.
    cue_alt = r"\b(?:no|never|neither|deny|denies|denied|ruled\s+out)\b"
    for term in _NEGATION_TARGETS:
        if len(term) < 3 or term not in lowered:
            continue
        if re.search(cue_alt + r"[\w\s,'\"-]*?\b" + re.escape(term) + r"\b", lowered):
            return True
    return False


def negation_detection_available() -> bool:
    from .injury_synonyms import _NEGSPACY_AVAILABLE, get_nlp

    return bool(get_nlp() and _NEGSPACY_AVAILABLE)


def contains_negated_injury(text: str) -> bool:
    if not text:
        return False
    return _has_negated_injury(text)


def remove_negated_phrases(text: str) -> str:
    """Strip words marked as negated by Negex from the text."""
    from .injury_synonyms import _NEGSPACY_AVAILABLE, get_nlp

    if not text:
        return ""
    nlp = get_nlp()
    if nlp and _NEGSPACY_AVAILABLE:
        return _remove_negex_phrases(nlp, text)
    return _strip_negated_chunks_fallback(text)


@lru_cache(maxsize=4096)
def _remove_negex_phrases(nlp, text: str) -> str:
    """Negex filtering for one text with one loaded pipeline.

    The result depends only on the pipeline and the text, and is an immutable
    string, so repeats are served from memory. One Today build asks about the
    same few injury phrases (area, description, "area pain") around two hundred
    times, and each spaCy pass costs milliseconds.
    """
    doc = nlp(text)
    if any(tok._.negex for tok in doc):
        tokens = [tok.text for tok in doc if not tok._.negex]
        return " ".join(tokens).strip()
    # Negex only marks named entities, so symptom-level negations such as
    # "no shoulder pain" slip through. Drop only chunks that *begin* with a
    # negation cue (clinical forward-scope) instead of returning the text
    # untouched. Embedded negations like "shoulder clicking no pain" are
    # left for Negex so we never discard the non-negated half of a chunk.
    return _strip_leading_negation_chunks(text)


def _strip_leading_negation_chunks(text: str) -> str:
    normalized = text.lower()
    normalized = re.sub(r"[()]", " ", normalized)
    normalized = re.sub(r"\b(and|but|also|however|except)\b,?", ". ", normalized)
    normalized = _normalize_injury_text_separators(normalized)
    chunks = [
        cleaned
        for chunk in re.split(r"\.\s*", normalized)
        if (cleaned := _strip_surrounding_punct(chunk))
    ]
    kept: list[str] = []
    for cleaned in chunks:
        # Ignore a leading field label ("notes: no fracture ...") so the cue is
        # still seen at the start of the clause.
        label_stripped = re.sub(r"^\w+:\s*", "", cleaned)
        # Drop a clause only when its negation scopes the clause itself: it
        # begins with a cue ("no shoulder pain") or explicitly rules an injury
        # out ("ruled out fracture"). Trailing negations such as "shoulder
        # clicking no pain" keep their non-negated injury content, and soft cues
        # that merely qualify ("not severe ankle swelling") are preserved.
        if _clause_negates_injury(label_stripped):
            continue
        kept.append(cleaned)
    # If nothing was negated, return the text untouched so we never reflow
    # punctuation/casing for callers that rely on the original structure (e.g.
    # swelling-context severity escalation).
    if len(kept) == len(chunks):
        return text
    return ". ".join(kept).strip()


def _strip_negated_chunks_fallback(text: str) -> str:
    normalized = text.lower()
    normalized = re.sub(r"[()]", " ", normalized)
    normalized = re.sub(r"\b(and|but|also|however|except)\b,?", ". ", normalized)
    normalized = _normalize_injury_text_separators(normalized)
    phrases = [
        cleaned
        for chunk in re.split(r"\.\s*", normalized)
        if (cleaned := _strip_surrounding_punct(chunk))
    ]
    if not phrases:
        return ""
    kept = [phrase for phrase in phrases if not _has_negated_injury(phrase)]
    return ". ".join(kept).strip()
