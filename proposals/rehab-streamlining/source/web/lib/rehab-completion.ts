import type { StructuredSession } from "./types";

/** Only standalone Calm/Restore work skips the training start/review flow. */
export function usesSimpleRehabCompletion(session: StructuredSession | null | undefined): boolean {
  const blocks = session?.blocks;
  if (session?.session_type !== "rehab" || !blocks?.length) return false;
  return blocks.every(block => block.block_type === "rehab" &&
    (block.drill_snapshot?.rehab_stage === "calm" || block.drill_snapshot?.rehab_stage === "restore"));
}

export function simpleRehabOutcome(status: "done" | "modified" | "skipped") {
  return {
    rehabPerformance: status === "done" ? "done_as_shown" as const : status === "modified" ? "changed" as const : undefined,
    // The existing completion contract requires text for modified/skipped.
    // Preserve the reported outcome without inventing a reason or symptoms.
    modificationReason: status === "modified" ? "Rehab marked modified; no further details supplied."
      : status === "skipped" ? "Rehab marked not done; no further details supplied." : "",
  };
}
