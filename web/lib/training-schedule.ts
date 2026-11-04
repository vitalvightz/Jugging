export type AvailabilityConsistency = {
  hardError: string | null;
  softWarning: string | null;
};

export type SparringConsistency = {
  hardError: string | null;
  softWarning: string | null;
};

export type HardSparringWarning = {
  acknowledgementContextKey: string;
  message: string | null;
  requiresAcknowledgement: boolean;
};

export const HARD_SPARRING_DAY_CAP = 4;

function getSortedUniqueDays(days: string[]): string[] {
  return [...new Set(days)].sort((left, right) => left.localeCompare(right));
}

export function getAvailabilityConsistency(
  trainingAvailability: string[],
  weeklyTrainingFrequency: number | null | undefined,
): AvailabilityConsistency {
  const availableDays = trainingAvailability.length;
  const plannedDays = weeklyTrainingFrequency ?? 0;
  const unusedDays = Math.max(availableDays - plannedDays, 0);

  if (!availableDays || !plannedDays) {
    return { hardError: null, softWarning: null };
  }

  if (plannedDays > availableDays) {
    return {
      hardError: `You selected ${availableDays} available day${availableDays === 1 ? "" : "s"} but planned ${plannedDays} training days per week.`,
      softWarning: null,
    };
  }

  if (unusedDays >= 3 && plannedDays <= 3) {
    return {
      hardError: null,
      softWarning: `You have ${availableDays} days available but only ${plannedDays} planned training day${plannedDays === 1 ? "" : "s"}. That's fine if some days are optional.`,
    };
  }

  return { hardError: null, softWarning: null };
}

export function getSparringConsistency(
  trainingAvailability: string[],
  hardSparringDays: string[],
  supportWorkDays: string[],
  requireCombatSession = false,
  weeklyTrainingFrequency?: number | null,
): SparringConsistency {
  if (requireCombatSession && !hardSparringDays.length && !supportWorkDays.length) {
    return {
      hardError: "Add at least one hard sparring or light/technical combat day to build a Fight Camp. If none is scheduled, use Open Plan.",
      softWarning: null,
    };
  }

  const normalizedHardSparringDays = getSortedUniqueDays(hardSparringDays);
  if (normalizedHardSparringDays.length > HARD_SPARRING_DAY_CAP) {
    return {
      hardError: `Hard sparring days cap is ${HARD_SPARRING_DAY_CAP}; reduce to ${HARD_SPARRING_DAY_CAP} or fewer to continue.`,
      softWarning: null,
    };
  }

  const available = new Set(trainingAvailability);
  const invalidHard = hardSparringDays.filter((day) => !available.has(day));
  if (invalidHard.length) {
    return {
      hardError: `Hard sparring days must also be selected as available days: ${invalidHard.join(", ")}.`,
      softWarning: null,
    };
  }

  const invalidSupport = supportWorkDays.filter((day) => !available.has(day));
  if (invalidSupport.length) {
    return {
      hardError: `Support Work Days (Light Combat / S&C-compatible slots) must also be selected as available days: ${invalidSupport.join(", ")}.`,
      softWarning: null,
    };
  }

  const overlap = hardSparringDays.filter((day) => supportWorkDays.includes(day));
  if (overlap.length) {
    return {
      hardError: `A day cannot be both hard sparring and Support Work (Light Combat): ${overlap.join(", ")}.`,
      softWarning: null,
    };
  }

  // Count distinct combat days, not multiple workouts on the same day.
  const combatDays = new Set([...hardSparringDays, ...supportWorkDays]).size;
  if (weeklyTrainingFrequency && combatDays > weeklyTrainingFrequency) {
    return {
      hardError: `Your schedule doesn't add up: you selected ${combatDays} combat day${combatDays === 1 ? "" : "s"} but entered ${weeklyTrainingFrequency} training day${weeklyTrainingFrequency === 1 ? "" : "s"} per week. Update your total or adjust your combat days.`,
      softWarning: null,
    };
  }

  if (!hardSparringDays.length && supportWorkDays.length) {
    return {
      hardError: null,
      softWarning: "Light Combat days are set, but hard sparring days are blank. That's fine if sparring is light or not fixed yet.",
    };
  }

  return { hardError: null, softWarning: null };
}

export function getHardSparringWarning(
  hardSparringDays: string[],
  weeklyTrainingFrequency: number | null | undefined,
): HardSparringWarning {
  const normalizedHardSparringDays = getSortedUniqueDays(hardSparringDays);
  const plannedDays = weeklyTrainingFrequency ?? 0;
  const acknowledgementContextKey = `hard-sparring:${plannedDays}:${normalizedHardSparringDays.join("|")}`;

  if (normalizedHardSparringDays.length < 3) {
    return {
      acknowledgementContextKey,
      message: null,
      requiresAcknowledgement: false,
    };
  }

  const trainingLabel = plannedDays
    ? `${plannedDays} planned training day${plannedDays === 1 ? "" : "s"} per week`
    : "the current weekly training plan";

  return {
    acknowledgementContextKey,
    message: `You selected ${normalizedHardSparringDays.length} hard sparring days inside ${trainingLabel}. Confirm recovery planning before continuing.`,
    requiresAcknowledgement: true,
  };
}
