import type { QuickBuildInput } from "@/lib/quick-build";
import type { PlanRequest } from "@/lib/types";

export const HEALTH_CONSENT_BLOCKED_MESSAGE =
  "Health data consent required. Manage it in Settings → Privacy.";

/** Remove health inputs from client state before it can be saved or generated. */
export function withoutIntakeHealthData(input: PlanRequest): PlanRequest {
  const {
    fatigue_level: _fatigueLevel,
    injuries: _injuries,
    guided_injury: _guidedInjury,
    guided_injuries: _guidedInjuries,
    ...nonHealthInput
  } = input;
  const {
    weight_kg: _weightKg,
    target_weight_kg: _targetWeightKg,
    ...nonHealthAthlete
  } = input.athlete;

  return {
    ...nonHealthInput,
    athlete: nonHealthAthlete,
  };
}

export function withoutQuickBuildHealthData(input: QuickBuildInput): QuickBuildInput {
  return { ...input, injuries: "", injury_impact: "", guided_injuries: [], target_weight_kg: null };
}
