/** Session activities can be arranged by the gym without a numeric prescription.
 * Match the exercise itself, not its parent session (which may also contain lifts).
 * Keep in sync with session_activity in api/services/custom_plans.py. */
export function sessionActivity(name: string | null | undefined, blockType?: string | null): string | null {
  const text = name ?? "";
  if (blockType === "sparring") return "Sparring";
  if (["strength", "plyometric_power", "preparation", "cooldown_recovery", "rehab"].includes(blockType ?? "")) return null;
  if (/\b(?:hard|full[ -]?contact|live)\b/i.test(text) && /\bsparring\b/i.test(text)) return "Sparring";
  if (/\b(?:light[\s/-]+technical|technical[\s/-]+(?:sparring|rounds)|light[\s/-]+sparring|skill\s+session)\b/i.test(text)) return "Light technical";
  if (/\b(?:pads?|pad[\s-]*work|mitts?)\b/i.test(text)) return "Pads";
  if (/\bsparring\b/i.test(text)) return "Sparring";
  if (/\b(?:boxing|box[\s-]*ing|bag[\s-]+work|shadow[\s-]*boxing)\b/i.test(text)) return "Boxing";
  if (/\b(?:road[\s-]*work|easy\s+run)\b/i.test(text)) return "Roadwork";
  if (/\btechnical\s+(?:session|work|drills?)\b/i.test(text)) return "Light technical";
  return null;
}
