"""Choose a concrete working point inside an already authorised prescription.

No range is widened and no additional set, exercise or progression is authored.
"""
import re

_RANGE = re.compile(r"(?<![\d\-–—])(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)(?!\s*[-–—]\s*\d)")


def working_dose(text: str, *, duration_max: bool = False) -> str:
    # A template's optional throw count is not an additional working dose.
    text = re.sub(r"\s*\(or\s+[^)]*\)", "", str(text or ""), flags=re.I)
    def choose(match):
        before = text[max(0, match.start() - 20):match.start()].lower()
        after = text[match.end():match.end() + 16].lower()
        # Use the minimum authorised work/effort and full rest. Conditioning
        # duration follows its existing total_minutes workload authority.
        upper = bool(re.search(r"\brest\s*$", before) or re.match(r"\s*(?:s|secs?|seconds?)?\s+rest\b", after)
                     or (duration_max and re.match(r"\s*(?:min|sec|s\b)", after)))
        return match[2] if upper else match[1]
    return _RANGE.sub(choose, text)
