import type { CustomPlan, CustomPlanInput, CustomSession, CustomExercise, ManualExercise, CoachPlan } from "./api-schema.generated";
import { assignCoachPlan, saveCustomPlan } from "./api";

type SessionLike = Omit<CustomSession, "session_type" | "scheduled_date" | "exercises"> & Partial<Pick<CustomSession, "session_type" | "scheduled_date">>
  & { exercises: (Omit<CustomExercise, "snapshot"> | ManualExercise)[] };
type PlanLike = Pick<CustomPlan, "name"> & Partial<Pick<CustomPlan, "schedule_mode" | "start_date" | "end_date">> & { sessions: SessionLike[] };

/** The save payload for a plan. With `fresh`, session and exercise ids are regenerated so the copy is independent. */
export function customPlanInput(plan: PlanLike, fresh = false): CustomPlanInput {
  const id = (value: string) => fresh ? crypto.randomUUID() : value;
  return {
    name: plan.name,
    schedule_mode: plan.schedule_mode ?? "weekly", start_date: plan.start_date ?? null, end_date: plan.end_date ?? null,
    sessions: plan.sessions.map(s => ({
      id: id(s.id),
      name: s.name,
      weekday: s.weekday, scheduled_date: s.scheduled_date ?? null,
      session_type: s.session_type ?? "mixed",
      exercises: s.exercises.map(e => e.origin === "manual"
        ? {
            id: id(e.id), origin: e.origin, name: e.name, equipment: e.equipment, exercise_type: e.exercise_type, notes: e.notes,
            media_id: e.media_id, community_id: e.community_id, community_revision_id: e.community_revision_id,
            dose: e.dose, coaching_cues: e.coaching_cues, stop_rules: e.stop_rules,
          }
        : { id: id(e.id), origin: "catalogue" as const, catalogue_id: e.catalogue_id, dose: e.dose, coaching_cues: e.coaching_cues, stop_rules: e.stop_rules }),
    })),
  };
}

export type ShareResult = { fighterId: string; plan?: CoachPlan | CustomPlan; error?: true };

/** Gives each fighter their own copy of the plan; assigns it too unless `assign` is false. One failure never blocks the others. */
export async function sharePlanWithFighters(token: string, plan: PlanLike, fighterIds: string[], assign: boolean): Promise<ShareResult[]> {
  return Promise.all(fighterIds.map(async fighterId => {
    try {
      const copy = await saveCustomPlan(token, customPlanInput(plan, true), undefined, fighterId, crypto.randomUUID());
      return { fighterId, plan: assign ? await assignCoachPlan(token, fighterId, copy.id) : copy };
    } catch {
      return { fighterId, error: true as const };
    }
  }));
}
