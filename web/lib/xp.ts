import xpLevelContract from "../../shared/xp-levels.json";

export type XpAction =
  | "daily_login"
  | "training_logged"
  | "planned_session_completed"
  | "recommended_fighter_content_watched"
  | "full_training_week_completed"
  | "profile_completed"
  | "first_intake_completed"
  | "first_plan_ready"
  | "first_checkin_completed"
  | "readiness_checkin_completed"
  | "injury_update_completed"
  | "stop_decision_followed"
  | "feedback_submitted"
  | "feedback_with_comment"
  | "first_plan_completed"
  | "phase_completed"
  | "camp_completed"
  | "manual_training_logged"
  | "first_session_logged"
  | "first_exercise_logged"
  | "training_days_10"
  | "training_days_25"
  | "training_days_50"
  | "training_days_100"
  | "exercise_contribution_approved";

export type XpActionConfig = {
  label: string;
  xp: number;
};

export type XpLevelConfig = {
  level: number;
  title: string;
  threshold: number;
};

export type XpAwardRecord = {
  id: string;
  action: XpAction;
  amount: number;
  awardedAt: string;
  calendarDate?: string;
};

export type XpState = {
  totalXp: number;
  lastDailyLoginDate: string | null;
  recentAwards: XpAwardRecord[];
};

export type XpAwardResult = {
  state: XpState;
  previousTotalXp: number;
  awarded: boolean;
  award: XpAwardRecord | null;
};

export type XpLevelProgress = {
  totalXp: number;
  currentLevel: XpLevelConfig;
  nextLevel: XpLevelConfig | null;
  percentage: number;
  xpWithinLevel: number;
  xpForNextLevel: number;
  xpRemaining: number;
};

export const XP_RECENT_AWARDS_LIMIT = 20;

/**
 * Ledger values accepted from the backend. Daily login remains 10 here only so
 * historical awards can still be displayed; new daily-login awards are retired.
 */
export const XP_ACTIONS: Record<XpAction, XpActionConfig> = {
  daily_login: { label: "Daily login", xp: 10 },
  training_logged: { label: "Training logged", xp: 25 },
  planned_session_completed: { label: "Planned session complete", xp: 50 },
  recommended_fighter_content_watched: { label: "Recommended fighter content watched", xp: 10 },
  full_training_week_completed: { label: "Training week complete", xp: 100 },
  profile_completed: { label: "Profile complete", xp: 25 },
  first_intake_completed: { label: "Camp setup complete", xp: 50 },
  first_plan_ready: { label: "First plan ready", xp: 100 },
  first_checkin_completed: { label: "First check-in complete", xp: 25 },
  readiness_checkin_completed: { label: "Check-in complete", xp: 10 },
  injury_update_completed: { label: "Injury update saved", xp: 10 },
  stop_decision_followed: { label: "STOP decision followed", xp: 15 },
  feedback_submitted: { label: "Feedback submitted", xp: 1 },
  feedback_with_comment: { label: "Feedback with comment", xp: 3 },
  first_plan_completed: { label: "Training plan complete", xp: 250 },
  phase_completed: { label: "Training phase complete", xp: 200 },
  camp_completed: { label: "Fight camp complete", xp: 500 },
  manual_training_logged: { label: "Manual training logged", xp: 25 },
  first_session_logged: { label: "First session logged", xp: 50 },
  first_exercise_logged: { label: "First exercise logged", xp: 25 },
  training_days_10: { label: "10 training days down", xp: 100 },
  training_days_25: { label: "25 training days down", xp: 150 },
  training_days_50: { label: "50 training days down", xp: 250 },
  training_days_100: { label: "100 training days down", xp: 500 },
  exercise_contribution_approved: { label: "Exercise contribution approved", xp: 100 },
};

function parseXpLevelContract(value: unknown): readonly XpLevelConfig[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error("XP level contract must be a non-empty list.");
  }

  let previousThreshold = -1;
  const levels = value.map((entry, index) => {
    if (!entry || typeof entry !== "object") {
      throw new Error("XP level contract entries must be objects.");
    }
    const candidate = entry as Record<string, unknown>;
    const expectedLevel = index + 1;
    const level = candidate.level;
    const title = candidate.title;
    const threshold = candidate.threshold;
    if (
      level !== expectedLevel ||
      typeof title !== "string" ||
      !title.trim() ||
      typeof threshold !== "number" ||
      !Number.isSafeInteger(threshold) ||
      threshold < 0 ||
      (expectedLevel === 1 && threshold !== 0) ||
      threshold <= previousThreshold
    ) {
      throw new Error("XP level contract is invalid.");
    }
    previousThreshold = threshold;
    return Object.freeze({ level, title: title.trim(), threshold });
  });

  return Object.freeze(levels);
}

export const XP_LEVELS = parseXpLevelContract(xpLevelContract);

const XP_ACTION_NAMES = new Set<XpAction>(Object.keys(XP_ACTIONS) as XpAction[]);

function sanitizeTotalXp(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function isValidCalendarDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return false;
  }
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

function isXpAction(value: unknown): value is XpAction {
  return typeof value === "string" && XP_ACTION_NAMES.has(value as XpAction);
}

function parseApiAward(value: unknown): XpAwardRecord | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const candidate = value as Record<string, unknown>;
  const action = candidate.action;
  const awardedAt = candidate.awarded_at;
  if (
    typeof candidate.id !== "string" ||
    !candidate.id.trim() ||
    !isXpAction(action) ||
    candidate.amount !== XP_ACTIONS[action].xp ||
    typeof awardedAt !== "string" ||
    !Number.isFinite(Date.parse(awardedAt))
  ) {
    return null;
  }

  const calendarDate = isValidCalendarDate(candidate.calendar_date)
    ? candidate.calendar_date
    : undefined;
  return {
    id: candidate.id.trim(),
    action,
    amount: XP_ACTIONS[action].xp,
    awardedAt: new Date(awardedAt).toISOString(),
    ...(calendarDate ? { calendarDate } : {}),
  };
}

export function createFreshXpState(): XpState {
  return {
    totalXp: 0,
    lastDailyLoginDate: null,
    recentAwards: [],
  };
}

/** Convert and validate the server response before it reaches presentation. */
export function parseXpAwardResponse(value: unknown): XpAwardResult {
  if (!value || typeof value !== "object") {
    throw new Error("Server returned invalid XP data.");
  }
  const candidate = value as Record<string, unknown>;
  const rawState = candidate.state;
  if (!rawState || typeof rawState !== "object") {
    throw new Error("Server returned invalid XP data.");
  }
  const stateCandidate = rawState as Record<string, unknown>;
  const totalXp = sanitizeTotalXp(stateCandidate.total_xp);
  const previousTotalXp = sanitizeTotalXp(candidate.previous_total_xp);
  const recentAwards = Array.isArray(stateCandidate.recent_awards)
    ? stateCandidate.recent_awards.map(parseApiAward).filter((award): award is XpAwardRecord => award !== null)
    : [];
  const award = candidate.award === null ? null : parseApiAward(candidate.award);

  if (
    typeof candidate.awarded !== "boolean" ||
    totalXp !== stateCandidate.total_xp ||
    previousTotalXp !== candidate.previous_total_xp ||
    !Array.isArray(stateCandidate.recent_awards) ||
    recentAwards.length !== stateCandidate.recent_awards.length ||
    recentAwards.length > XP_RECENT_AWARDS_LIMIT ||
    (candidate.awarded && !award) ||
    (!candidate.awarded && candidate.award !== null)
  ) {
    throw new Error("Server returned invalid XP data.");
  }

  const lastDailyLoginDate = isValidCalendarDate(stateCandidate.last_daily_login_date)
    ? stateCandidate.last_daily_login_date
    : null;
  if (stateCandidate.last_daily_login_date !== null && lastDailyLoginDate === null) {
    throw new Error("Server returned invalid XP data.");
  }

  return {
    state: {
      totalXp,
      lastDailyLoginDate,
      recentAwards,
    },
    previousTotalXp,
    awarded: candidate.awarded,
    award,
  };
}

export function resolveXpLevel(totalXpInput: unknown): XpLevelProgress {
  const totalXp = sanitizeTotalXp(totalXpInput);
  let currentIndex = 0;
  for (let index = 1; index < XP_LEVELS.length; index += 1) {
    if (totalXp < XP_LEVELS[index].threshold) {
      break;
    }
    currentIndex = index;
  }

  const currentLevel = XP_LEVELS[currentIndex];
  const nextLevel = XP_LEVELS[currentIndex + 1] ?? null;
  if (!nextLevel) {
    return {
      totalXp,
      currentLevel,
      nextLevel: null,
      percentage: 100,
      xpWithinLevel: Math.max(0, totalXp - currentLevel.threshold),
      xpForNextLevel: 0,
      xpRemaining: 0,
    };
  }

  const xpWithinLevel = totalXp - currentLevel.threshold;
  const xpForNextLevel = nextLevel.threshold - currentLevel.threshold;
  const xpRemaining = Math.max(0, nextLevel.threshold - totalXp);
  return {
    totalXp,
    currentLevel,
    nextLevel,
    percentage: Math.max(0, Math.min(100, (xpWithinLevel / xpForNextLevel) * 100)),
    xpWithinLevel,
    xpForNextLevel,
    xpRemaining,
  };
}
