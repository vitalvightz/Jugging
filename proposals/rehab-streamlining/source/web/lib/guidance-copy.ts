/** Remove repeated messages while preserving every distinct instruction. */
export function distinctGuidanceLines(...values: Array<string | null | undefined>): string[] {
  const seen = new Set<string>();
  return values.filter((value): value is string => {
    const key = value?.trim().toLowerCase().replace(/\s+/g, " ").replace(/[.!]+$/, "");
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).map(value => value.trim());
}
