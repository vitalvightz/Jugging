"""Choose a concrete working point inside an already authorised prescription.

No range is widened and no additional set, exercise or progression is authored.
"""
import math
import re

_RANGE = re.compile(r"(?<![\d\-–—])(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)(?!\s*[-–—]\s*\d)")


def working_dose(text: str, *, duration_max: bool = False, development: bool = False) -> str:
    """``development`` chooses the middle of each authorised work/effort range
    instead of its floor: a development stimulus, never beyond what the bank
    authorised. Integer ranges round the middle up ("6-10" -> 8, "3-5" -> 4);
    a decimal range keeps its exact middle ("1.5-2" -> 1.75). Rest still takes
    its upper bound."""
    # A template's optional throw count is not an additional working dose.
    text = re.sub(r"\s*\(or\s+[^)]*\)", "", str(text or ""), flags=re.I)
    def choose(match):
        before = text[max(0, match.start() - 20):match.start()].lower()
        after = text[match.end():match.end() + 16].lower()
        # Use the minimum authorised work/effort and full rest. Conditioning
        # duration follows its existing total_minutes workload authority.
        upper = bool(re.search(r"\brest\s*$", before) or re.match(r"\s*(?:s|secs?|seconds?)?\s+rest\b", after)
                     or (duration_max and re.match(r"\s*(?:min|sec|s\b)", after)))
        if upper:
            return match[2]
        if development:
            low, high = float(match[1]), float(match[2])
            middle = math.ceil((low + high) / 2) if low.is_integer() and high.is_integer() else (low + high) / 2
            return f"{middle:g}"
        return match[1]
    return _RANGE.sub(choose, text)
