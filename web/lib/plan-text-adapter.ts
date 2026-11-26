// Deterministic plan_text -> StructuredPlan adapter.
//
// Pure, non-visual parsing logic extracted from components/plan-viewer.tsx so
// consumers (Plan Detail's fallback renderer, the Today hook) can share it
// without importing the whole Plan Viewer component and its UI/API/admin
// dependency graph. It adapts the approved athlete-facing plan_text into the
// structured renderer's shape without another model call; every field comes
// from the saved text, so a missing server-side structured payload can never
// force the legacy renderer.
import type {
  ExercisePrescription,
  StructuredBlock,
  StructuredDay,
  StructuredPlan,
  StructuredRedFlagRule,
  StructuredSession,
  StructuredWeek,
} from "@/lib/types";

/** One labelled line inside a session block ("Purpose", "Progress", …). */
export type PlanTextDetail = { label: string | null; text: string };

/** A single exercise/prescription inside a session card. */
export type PlanTextBlock = {
  code?: string | null;
  prescribedSets?: ExercisePrescription[];
  groupType?: StructuredBlock["group_type"];
  name: string;
  dose: string | null;
  details: PlanTextDetail[];
  /** Block-group label (Rehab, Mobility, Activation…) shown as a tag, mirroring the structured card. */
  tag: string | null;
};

/** A dated training day rendered as a session card. */
export type PlanTextSession = {
  kind: "session";
  countdown: string | null;
  weekday: string | null;
  title: string;
  objective: string | null;
  coachNote: string | null;
  blocks: PlanTextBlock[];
  notes: string[];
};

/** Lead/Final notes, rendered as a context card. */
export type PlanTextNotes = { kind: "notes"; title: string; lines: string[] };

/** A phase/week header that owns the session cards rendered beneath it. */
export type PlanTextWeek = {
  kind: "week";
  title: string;
  phase: string | null;
  sessions: PlanTextSession[];
};

export type PlanTextGroup = PlanTextNotes | PlanTextWeek | PlanTextSession;

export function humanizeStatus(value: string) {
  return value.replace(/_/g, " ");
}

export function titleizeToken(value: string) {
  const normalized = humanizeStatus(value || "").trim();
  if (!normalized) {
    return "";
  }
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

// Leading list/bullet markers stripped off a line before it is parsed. Includes
// the unicode bullets the rehab renderer emits ("• [Drill] — [Dose]", see
// fightcamp/stage2_payload.py) so the glyph never leaks into an exercise title.
const LEADING_BULLET_RE = /^[-*•‣▪◦·]\s+/;

function stripPlanMarkup(value: string): string {
  return value
    .replace(/^#{1,6}\s+/, "")
    .replace(LEADING_BULLET_RE, "")
    .replace(/^\d+\.\s+/, "")
    .replace(/\*\*/g, "")
    .trim();
}

function normalizePlanTextForCards(rawText: string): string {
  return rawText
    .replace(/\r\n/g, "\n")
    .replace(/(^|\n)(Lead notes|Active notes)\s+[-–—]?\s*/gi, "$1$2\n")
    .replace(/\s+(?=(?:GPP|SPP|TAPER|FIGHT[ _]WEEK|REINTEGRATION)\s*[—–\-:]\s*Week\b)/gi, "\n")
    .replace(/\s+(?=D-\d+\s*(?:\([^)]+\))?\s*[—–\-:](?!\s*D-\d)\s*\S)/g, "\n")
    .replace(
      /\s+(?=(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:sday)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b[^()]*\(\s*D-\d+\s*\)\s*[—–\-:]\s*\S)/gi,
      "\n",
    )
    .replace(/\s+(?=(?:Final notes|End of plan notes)\b)/gi, "\n")
    .replace(/(^|\n)(Final notes|End of plan notes)\s+[-–—]?\s*/gi, "$1$2\n");
}

// Labelled sub-lines that fall *inside* a session block. The plan text packs a
// lot of structure behind these prefixes ("Purpose: …", "Progress/regress: …",
// "Stop rule: …"), so we surface each as its own kicker+text line instead of
// flattening them into one wall of prose. Longest variants must come first so
// "Progress/regress/stop" wins over "Progress".
const SESSION_DETAIL_LABELS: { match: RegExp; label: string }[] = [
  { match: /^how$/i, label: "How" },
  { match: /^cue$/i, label: "Cue" },
  { match: /^purpose$/i, label: "Purpose" },
  { match: /^purpose\s*&\s*why$/i, label: "Purpose" },
  { match: /^why today$/i, label: "Why" },
  { match: /^why$/i, label: "Why" },
  { match: /^duration$/i, label: "Duration" },
  { match: /^prescription$/i, label: "Prescription" },
  { match: /^output$/i, label: "Output" },
  { match: /^intensity$/i, label: "Intensity" },
  { match: /^coach call$/i, label: "Coach call" },
  { match: /^week note$/i, label: "Week note" },
  { match: /^final coach call(?:\s*\(one line\))?$/i, label: "Coach call" },
  { match: /^progress\/regress\/stop$/i, label: "Progress" },
  { match: /^progression\/regression\/stop$/i, label: "Progress" },
  { match: /^progress\/regress$/i, label: "Progress" },
  { match: /^progression\/regression$/i, label: "Progress" },
  { match: /^progression$/i, label: "Progress" },
  { match: /^progress$/i, label: "Progress" },
  { match: /^regression$/i, label: "Regress" },
  { match: /^regress$/i, label: "Regress" },
  { match: /^stop rule$/i, label: "Stop" },
  { match: /^stop$/i, label: "Stop" },
  { match: /^easier$/i, label: "Easier" },
  { match: /^swaps?$/i, label: "Swaps" },
  { match: /^rest$/i, label: "Rest" },
  { match: /^note$/i, label: "Note" },
];

const SESSION_LABEL_SPLIT_RE =
  /\b(How|Cue|Purpose\s*&\s*why|Purpose|Why today|Why|Duration|Prescription|Output|Intensity|Coach call|Week note|Final coach call(?:\s*\(one line\))?|Progression\/regression\/stop|Progress\/regress\/stop|Progression\/regression|Progress\/regress|Progression|Progress|Regression|Regress|Stop rule|Stop|Easier|Swaps?|Rest|Note)\s*:/gi;

function normalizeSessionLabel(raw: string): string {
  const trimmed = raw.trim();
  for (const { match, label } of SESSION_DETAIL_LABELS) {
    if (match.test(trimmed)) {
      return label;
    }
  }
  return titleizeToken(trimmed);
}

/**
 * Split a body line into its labelled segments. "Purpose: raise the base.
 * Progress/regress: add 5 min. Stop rule: stop if dizzy." becomes three
 * labelled details; an unlabelled line returns a single `label: null` segment.
 */
// Splitting on a label leaves the separator that introduced it dangling on the
// end of the previous segment ("… full recovery 90-120 s;" before "intensity:").
// Trim it so doses and details never render with a trailing semicolon or comma.
function trimTrailingSeparator(value: string): string {
  return value.replace(/[;,]+$/, "").trim();
}

export function splitLabeledSegments(text: string): PlanTextDetail[] {
  const clean = text.trim();
  if (!clean) {
    return [];
  }
  const matches = [...clean.matchAll(SESSION_LABEL_SPLIT_RE)];
  if (!matches.length) {
    return [{ label: null, text: clean }];
  }
  const segments: PlanTextDetail[] = [];
  const lead = trimTrailingSeparator(clean.slice(0, matches[0].index ?? 0).trim());
  if (lead) {
    segments.push({ label: null, text: lead });
  }
  for (let i = 0; i < matches.length; i += 1) {
    const match = matches[i];
    const start = (match.index ?? 0) + match[0].length;
    const end = i + 1 < matches.length ? matches[i + 1].index ?? clean.length : clean.length;
    const body = trimTrailingSeparator(
      clean
        .slice(start, end)
        .trim()
        .replace(/^[-–—]\s*/, ""),
    );
    if (body) {
      segments.push({ label: normalizeSessionLabel(match[1]), text: body });
    }
  }
  return segments.length ? segments : [{ label: null, text: clean }];
}

type PlanTextHeading =
  | { kind: "notes"; title: string; remainder: string | null }
  | { kind: "week"; title: string; phase: string | null }
  | { kind: "session"; countdown: string | null; weekday: string | null; title: string; remainder: string | null };

// Deterministic plan_text contract (mirrors fightcamp/weekly_plan_render.py +
// the stage2 validator's countdown-header rule). Phase labels are the canonical
// camp phases; the header separator is em-dash, en-dash, hyphen, or colon.
const PHASE_TOKEN = "GPP|SPP|TAPER|FIGHT[ _]WEEK|REINTEGRATION";
const WEEKDAY_TOKEN =
  "mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:sday)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?";
const HEADER_SEP = "[—–\\-:]";
const PHASE_FIRST_WEEK_RE = new RegExp(`^(${PHASE_TOKEN})\\s*${HEADER_SEP}\\s*(Week\\s+\\d+.*)$`, "i");
const BARE_WEEK_RE = /^Week\s+\d+\b/i;
const PHASE_ANYWHERE_RE = new RegExp(`\\b(${PHASE_TOKEN})\\b`, "i");
// "D-33 (Wednesday) — Aerobic support" (final/validated countdown-first form).
// The weekday parenthetical is OPTIONAL: a scheduled item can be rendered with
// the countdown alone ("D-10 — Joint Prep"). Requiring the weekday made such a
// heading invisible to the parser, so its title and body were swallowed into the
// previous day's session and the countdown gap-filler then manufactured a rest
// row on a day that carried authoritative work. The title must not itself be
// another countdown, so a range line ("D-14 — D-7") is never read as a session.
const SESSION_COUNTDOWN_FIRST_RE = new RegExp(
  `^D-(\\d+)\\s*(?:\\(([^)]+)\\))?\\s*${HEADER_SEP}(?!\\s*D-\\d)\\s*(.+)$`,
  "i",
);
// "Wednesday (D-33) — Aerobic support" (Stage 1 deterministic weekday-first form).
const SESSION_WEEKDAY_FIRST_RE = new RegExp(
  `^(${WEEKDAY_TOKEN})[^()]*\\(\\s*D-(\\d+)\\s*\\)\\s*${HEADER_SEP}\\s*(.+)$`,
  "i",
);
// Renewable open plans have no countdown. Their saved athlete text uses plain
// weekday headings ("Monday — Support Strength"), which are a complete and
// simpler timing contract than D-X. This matcher is enabled only inside the
// explicit Weekly Rhythm / Session Cards sections so ordinary prose cannot be
// mistaken for a scheduled day.
const SESSION_WEEKDAY_ONLY_RE = new RegExp(
  `^(${WEEKDAY_TOKEN})\\s*${HEADER_SEP}\\s*(.+)$`,
  "i",
);
// Plan-level context sections that live outside any week. Only the always-on
// note labels live here (recognised with or without a leading "#"); generic
// markdown sections like "## Nutrition" are handled by the markdown-header
// branch so a session body line such as "Recovery: light spin" is never
// mistaken for a new section.
const NOTE_SECTION_RE =
  /^(Lead notes|Final notes|Active notes|End of plan notes)(?:\s*[—–\-:]\s*(.+))?$/i;
// Late-fight output can begin with a compact bulleted summary rather than an
// explicit "Lead notes" heading. These labels are part of the saved text
// contract, so preserve each one as a separate note card instead of flattening
// the whole summary into one anonymous "Plan" paragraph.
const LEAD_NOTE_BULLET_RE =
  /^[-*•‣▪◦·]\s+(Injury|Missing target weight|Sparring note|Week shape)\s*:\s*(.+)$/i;
// Renewable open-plan system sections (the section contract in
// fightcamp/stage2_payload_open_ongoing.py). Each opens its own titled context
// group so buildStructuredPlanFromText can route it — red-flag triggers become
// red_flag_rules, progression rules become progression_notes, the remaining
// coach/system prose stays out of Active Notes — instead of accreting into one
// untitled blob that dumps the whole plan text into the note cards.
const OPEN_PLAN_SECTION_RE = new RegExp(
  "^(Immediate Coach Summary|Current Training Rules|Progression Rules|Priority Hierarchy|Adjustment Rules" +
    "|Rehab\\s*/\\s*Red Flags|\\d+-Week Reassessment Gate|End notes(?:\\s*\\(coach-facing\\))?" +
    "|Red-?flags? triggers?(?:\\s*\\([^)]*\\))?)" +
    `(?:\\s*${HEADER_SEP}\\s*(.+))?$`,
  "i",
);

function normalizePhaseToken(value: string): string {
  return value.replace(/_/g, " ").toUpperCase();
}

// On a run-on plan_text line the session metadata (the "Why:" objective, a
// labelled note, or a coach-led freshness note) can sit inline after the
// heading. Split it off the title so it is parsed as body instead of being
// buried in (or lost from) the title. Markers require a colon or a distinctive
// coach phrase, so ordinary title words ("Stop-and-go", a leading "Coach-led
// boxing session") are never split.
const SESSION_TITLE_BODY_MARKER =
  /\s(?=(?:How|Why|Purpose|Progress\/regress\/stop|Progress\/regress|Progression|Progress|Regress|Stop rule|Stop|Easier|Swaps?|Rest|Note)\s*:|No (?:extra|app) S\s?&?\s?C|Coach owns this session|Train with your coach)/i;

function splitSessionTitle(title: string): { title: string; remainder: string | null } {
  const markerIndex = title.search(SESSION_TITLE_BODY_MARKER);
  if (markerIndex > -1) {
    return { title: title.slice(0, markerIndex).trim(), remainder: title.slice(markerIndex).trim() || null };
  }
  return { title, remainder: null };
}

function classifyPlanTextHeading(
  line: string,
  allowWeekdayOnlySession = false,
): PlanTextHeading | null {
  const isMarkdownHeader = /^\s*#{1,6}\s+/.test(line);
  const clean = stripPlanMarkup(line);
  if (!clean || /^#+$/.test(clean)) {
    return null;
  }

  const noteMatch = clean.match(NOTE_SECTION_RE);
  if (noteMatch) {
    return { kind: "notes", title: titleizeToken(noteMatch[1].trim()), remainder: noteMatch[2]?.trim() || null };
  }
  const openSectionMatch = clean.match(OPEN_PLAN_SECTION_RE);
  if (openSectionMatch) {
    return {
      kind: "notes",
      title: titleizeToken(openSectionMatch[1].trim()),
      remainder: openSectionMatch[2]?.trim() || null,
    };
  }

  // Phase-prefixed week ("GPP — Week 1 (D-33 to D-27) — Build base"), or a bare
  // "Week N — …" header that may carry its phase token mid-line. Phase renders
  // as a tag, the rest as the title.
  const phaseWeek = clean.match(PHASE_FIRST_WEEK_RE);
  if (phaseWeek) {
    return { kind: "week", title: phaseWeek[2].trim(), phase: normalizePhaseToken(phaseWeek[1]) };
  }
  if (BARE_WEEK_RE.test(clean)) {
    const phase = clean.match(PHASE_ANYWHERE_RE);
    return { kind: "week", title: clean, phase: phase ? normalizePhaseToken(phase[1]) : null };
  }

  if (/^(?:Day|Session)\s+\d+\b/i.test(clean)) {
    return { kind: "session", countdown: null, weekday: null, title: clean, remainder: null };
  }

  const session = clean.match(SESSION_COUNTDOWN_FIRST_RE);
  if (session) {
    const split = splitSessionTitle(session[3].trim());
    return {
      kind: "session",
      countdown: `D-${session[1]}`,
      weekday: session[2]?.trim() || null,
      title: split.title,
      remainder: split.remainder,
    };
  }
  const weekdaySession = clean.match(SESSION_WEEKDAY_FIRST_RE);
  if (weekdaySession) {
    const split = splitSessionTitle(weekdaySession[3].trim());
    return {
      kind: "session",
      countdown: `D-${weekdaySession[2]}`,
      weekday: titleizeToken(weekdaySession[1].trim()),
      title: split.title,
      remainder: split.remainder,
    };
  }
  if (allowWeekdayOnlySession || SESSION_WEEKDAY_ONLY_RE.test(clean)) {
    const openSession = clean.match(SESSION_WEEKDAY_ONLY_RE);
    if (openSession) {
      const split = splitSessionTitle(openSession[2].trim());
      return {
        kind: "session",
        countdown: null,
        weekday: titleizeToken(openSession[1].trim()),
        title: split.title,
        remainder: split.remainder,
      };
    }
  }

  // Any other markdown header (## Nutrition, ## Progression, …) opens its own
  // context card rather than being swallowed into the previous session.
  if (isMarkdownHeader && !/^[A-Za-z][1-9]\d*[.):]?\s+\S/.test(clean)) {
    return { kind: "notes", title: clean, remainder: null };
  }

  return null;
}

const COACH_LED_RE =
  /no (?:extra|app) s\s?&?\s?c|coach owns this session|coach-owned (?:combat )?session|train with your coach/i;
const TECHNICAL_ONLY_CONTACT_NOTE_RE =
  /^Technical-only contact today\s*[—–-]\s*no hard sparring and no extra S\s?&?\s?C\.\s*Keep freshness priority\.?$/i;

// Standalone session sub-headings the rehab/accessory renderer emits before a
// group of bulleted items (RULE 12 and its suppressed-heading alternatives in
// fightcamp/stage2_payload.py). On their own line these are a block-group label,
// not an exercise, so we surface them as a tag on the grouped blocks — mirroring
// the structured card's block_type chip — instead of leaking the bare word
// (e.g. "Rehab") as a stray note on the previous block.
const BLOCK_GROUP_LABELS = [
  "Rehab",
  "Prehab",
  "Activation",
  "Movement prep",
  "Mobility",
  "Warm-up",
  "Warmup",
  "Cool-down",
  "Cooldown",
  "Reset",
  "Recovery",
  "Strength",
  "Power",
  "Conditioning",
  "Skill",
  "Accessory",
] as const;
const BLOCK_GROUP_LABEL_RE = new RegExp(`^(?:${BLOCK_GROUP_LABELS.join("|")})\\s*:?\\s*$`, "i");
// The same labels used as an INLINE prefix on a bulleted exercise, e.g.
// "Rehab - YTW Raise Sequence (light DBs) - 2 sets x 8 reps". Group 1 is the
// label, group 2 the real exercise heading (name + dose) that follows it.
const BLOCK_GROUP_PREFIX_RE = new RegExp(
  `^(${BLOCK_GROUP_LABELS.join("|")})\\s*[-–—]\\s+(.+)$`,
  "i",
);

/** A line that is *only* a block-group label → its canonical tag, else null. */
function matchBlockGroupLabel(line: string): string | null {
  const clean = stripPlanMarkup(line);
  if (!BLOCK_GROUP_LABEL_RE.test(clean)) {
    return null;
  }
  return titleizeToken(clean.replace(/:\s*$/, ""));
}

/**
 * Parse athlete plan_text into structured groups (context notes, week sections,
 * and the session cards beneath them). This is the fallback used when no
 * machine-readable structured_plan is present, so it re-derives the same card
 * shape the structured renderer shows — weeks, dated sessions, the session
 * objective, and each exercise with its Purpose / Progress / Stop detail —
 * instead of dumping every line into one undifferentiated block.
 */
export function parsePlanText(rawText: string): PlanTextGroup[] {
  const lines = normalizePlanTextForCards(rawText)
    .replace(/^(Day\s+\d+[^\n]*?)\s+\b(Week\s+\d+)\s*$/gim, "$2\n$1")
    .split("\n");
  const groups: PlanTextGroup[] = [];
  let currentWeek: PlanTextWeek | null = null;
  let currentSession: PlanTextSession | null = null;
  let currentNotes: PlanTextNotes | null = null;
  // The block-group label (Rehab, Mobility…) currently in effect within a
  // session, applied as a tag to the blocks beneath it until the next group
  // header or the next session.
  let currentBlockTag: string | null = null;
  let allowWeekdayOnlySessions = false;
  let tableHeaders: string[] | null = null;
  let tableHasOuterPipes = false;
  let groupType: StructuredBlock["group_type"] = null;
  let groupLetter: string | null = null;

  const addExerciseRow = (name: string, code: string | null, stage: ExercisePrescription | null) => {
    if (!currentSession) return;
    const previous = currentSession.blocks.at(-1);
    const rowCode = code || (previous && (!name || name === previous.name) ? previous.code : null);
    const rowGroupType = groupLetter && rowCode && rowCode[0].toUpperCase() !== groupLetter ? null : groupType;
    // Table continuation/repeated rows are evidence of sets, not new cards.
    if (previous && previous.groupType === rowGroupType && previous.tag === currentBlockTag &&
      ((!name && (!code || code === previous.code)) ||
      (name === previous.name && (!code || code === previous.code)))) {
      if (stage) (previous.prescribedSets ||= []).push(stage);
      return;
    }
    if (!name) return;
    currentSession.blocks.push({ name, code, dose: null, details: [], tag: currentBlockTag,
      prescribedSets: stage ? [stage] : [],
      groupType: rowGroupType });
  };

  const pushSession = (session: PlanTextSession) => {
    if (currentWeek) {
      currentWeek.sessions.push(session);
    } else {
      groups.push(session);
    }
  };

  const addBodyLine = (line: string) => {
    if (!currentSession) {
      return;
    }
    const session = currentSession;
    const listItem = line.match(/^([-*•‣▪◦·]|\d+\.)\s+(.+)$/);
    const wasListItem = Boolean(listItem);
    const content = stripPlanMarkup(listItem ? listItem[2] : line);
    if (!content) {
      return;
    }
    const explicitGroup = content.match(/^(Superset|Circuit|Alternating sets)(?:\s+([A-Za-z]))?\s*:?$/i);
    if (explicitGroup) {
      groupType = explicitGroup[1].toLowerCase().replace(/ /g, "_") as StructuredBlock["group_type"];
      groupLetter = explicitGroup[2]?.toUpperCase() || null;
      return;
    }
    if (/^(?:Standalone|Straight sets)\s*:?$/i.test(content)) {
      groupType = null;
      groupLetter = null;
      return;
    }
    if (content.includes("|") || content.includes("\t")) {
      const hasOuterPipes = tableHeaders ? tableHasOuterPipes : content.startsWith("|") && content.endsWith("|");
      const cells = (hasOuterPipes ? content.replace(/^\||\|$/g, "") : content)
        .split(/\||\t/).map((cell) => cell.trim());
      if (cells.every((cell) => /^:?-+:?$/.test(cell) || !cell)) return;
      if (cells.some((cell) => /^exercise$/i.test(cell)) &&
        cells.some((cell) => /reps|sets|weight|wt|%|duration/i.test(cell))) {
        tableHeaders = cells;
        tableHasOuterPipes = hasOuterPipes;
        return;
      }
      if (tableHeaders) {
        let headers = tableHeaders;
        if (cells.length === headers.length + 1 && /^[A-Za-z][1-9]\d*[.)]?$/.test(cells[0])) {
          headers = ["Code", ...headers];
          tableHeaders = headers;
        }
        let name = "";
        let code: string | null = null;
        const dose: string[] = [];
        const stage: ExercisePrescription = {};
        headers.forEach((header, index) => {
          const cell = cells[index] || "";
          if (/^exercise$/i.test(header)) name = cell;
          else if (/^(?:code)?$/i.test(header) && /^[A-Za-z][1-9]\d*[.)]?$/.test(cell)) code = cell.replace(/[.)]$/, "");
          else if (cell) {
            if (/^(?:%|percentage)$/i.test(header) && /^\d+(?:\.\d+)?%?$/.test(cell)) {
              stage.percentage = Number(cell.replace("%", ""));
            } else if (/^(?:sets\s*[x×]\s*reps|reps\s*[x×]\s*sets|reps|sets|duration)$/i.test(header)) {
              const pair = cell.match(/^(\d+)\s*[x×]\s*(.+)$/i);
              const repsFirst = /^reps\s*[x×]\s*sets$/i.test(header);
              if (pair && (!repsFirst || /^\d+$/.test(pair[2]))) {
                stage.sets = Number(repsFirst ? pair[2] : pair[1]);
                stage.reps = repsFirst ? Number(pair[1]) : /^\d+$/.test(pair[2]) ? Number(pair[2]) : pair[2];
              } else if (/^sets$/i.test(header) && /^\d+$/.test(cell)) stage.sets = Number(cell);
              else stage.reps = /^\d+$/.test(cell) ? Number(cell) : cell;
            } else dose.push(`${header || "Value"}: ${cell}`);
          }
        });
        // Retain any extra/unknown columns too; never discard coach notation.
        cells.slice(headers.length).filter(Boolean).forEach((cell) => dose.push(cell));
        const codedName = name.match(/^([A-Za-z][1-9]\d*)[.):]?\s+(?:[-—–]\s+)?(.+)$/);
        if (codedName) { code = codedName[1]; name = codedName[2]; }
        // Keep exact header order/notation, including REPS X SETS, WT without
        // units, 4e and warm-up rows. The source row is lossless and readable.
        if (dose.length) stage.source_text = dose.join(" · ");
        addExerciseRow(name, code, Object.keys(stage).length ? stage : null);
        return;
      }
    }
    const coded = content.match(/^([A-Za-z][1-9]\d*)[.):]?\s+(?:[-—–]\s+)?(.+)$/);
    if (coded) {
      const separator = /\s+[-—–]\s+|:\s+|\s+(?=\d+(?:\.\d+)?\s*(?:%|[x×]))/.exec(coded[2]);
      const name = separator ? coded[2].slice(0, separator.index) : coded[2];
      const dose = separator ? coded[2].slice(separator.index + separator[0].length) : null;
      addExerciseRow(name, coded[1], dose ? { source_text: dose } : null);
      return;
    }
    const previous = session.blocks.at(-1);
    if (previous && (previous.code || previous.prescribedSets?.length) &&
      /^\d+(?:\.\d+)?\s*(?:%|[x×]|e\b|(?:reps?|sec(?:onds?)?)\b|$)/i.test(content)) {
      (previous.prescribedSets ||= []).push({ source_text: content });
      return;
    }
    // Keep the established dash guard for general prose. Only the canonical
    // technical-contact sentence may contain a dash and still be a coach note.
    if (
      (COACH_LED_RE.test(content) && !content.includes(" — ")) ||
      TECHNICAL_ONLY_CONTACT_NOTE_RE.test(content)
    ) {
      session.coachNote = content;
      return;
    }
    for (const segment of splitLabeledSegments(content)) {
      const block = session.blocks[session.blocks.length - 1];
      if (segment.label === "Why" && !block && !session.objective) {
        session.objective = segment.text;
        continue;
      }
      if (segment.label) {
        if (block) {
          block.details.push(segment);
        } else {
          session.notes.push(`${segment.label}: ${segment.text}`);
        }
        continue;
      }
      // Unlabelled: an exercise heading (Name — dose), or a bulleted exercise,
      // otherwise loose detail attached to the current block / session. The
      // matched separator is always exactly " - " / " — " (3 chars).
      //
      // A block-group label can arrive INLINE as a prefix rather than on its own
      // line ("- Rehab - YTW Raise Sequence (light DBs) - 2 sets x 8 reps"). It
      // is the block's group, not its name, so strip it to the tag first —
      // otherwise the exercise renders as "Rehab" with its real name buried at
      // the head of the dose.
      // Several of these labels ("Movement prep", "Mobility", "Reset") are also
      // legitimate exercise names, so the prefix is only a GROUP when a name and
      // a dose both remain after it — "Rehab - YTW Raise Sequence - 2 x 8" has
      // three parts, while "Movement prep - 5 minutes total…" is just name+dose
      // and must keep its name.
      let blockText = segment.text;
      let blockTag = currentBlockTag;
      const inlineGroup = wasListItem ? blockText.match(BLOCK_GROUP_PREFIX_RE) : null;
      if (inlineGroup && inlineGroup[2].search(/\s[-–—]\s/) > -1) {
        blockTag = titleizeToken(inlineGroup[1]);
        blockText = inlineGroup[2].trim();
      }
      const dashIndex = blockText.search(/\s[-–—]\s/);
      const colonBlock = blockText.match(/^([^:]{2,80}):\s+(.+)$/);
      if (wasListItem && colonBlock) {
        session.blocks.push({
          name: colonBlock[1].trim(),
          dose: colonBlock[2].trim() || null,
          details: [],
          tag: blockTag,
          groupType,
        });
      } else if (dashIndex > -1) {
        session.blocks.push({
          name: blockText.slice(0, dashIndex).trim(),
          dose: blockText.slice(dashIndex + 3).trim() || null,
          details: [],
          tag: blockTag,
          groupType,
        });
      } else if (wasListItem) {
        // A bulleted line is its own exercise heading (e.g. a rehab drill whose
        // dose sits on the next line), so it always opens a new block rather than
        // folding into the previous one.
        session.blocks.push({ name: blockText, dose: null, details: [], tag: blockTag, groupType });
      } else if (block) {
        block.details.push({ label: null, text: blockText });
      } else {
        session.notes.push(blockText);
      }
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || /^#+$/.test(line)) {
      continue;
    }

    const cleanLine = stripPlanMarkup(line);
    const leadNote = line.match(LEAD_NOTE_BULLET_RE);
    if (!currentSession && !currentWeek && leadNote) {
      currentNotes = {
        kind: "notes",
        title: titleizeToken(leadNote[1]),
        lines: [leadNote[2].trim()],
      };
      groups.push(currentNotes);
      continue;
    }
    if (/^(?:Weekly Rhythm|Session Cards):?$/i.test(cleanLine)) {
      allowWeekdayOnlySessions = true;
      currentSession = null;
      currentWeek = null;
      currentNotes = null;
      currentBlockTag = null;
      continue;
    }
    if (allowWeekdayOnlySessions && /^\d+-Week (?:Development )?Block:?$/i.test(cleanLine)) {
      allowWeekdayOnlySessions = false;
      currentSession = null;
      currentWeek = null;
      currentNotes = null;
      currentBlockTag = null;
      continue;
    }
    if (allowWeekdayOnlySessions && /^\(Format per session:/i.test(cleanLine)) {
      continue;
    }

    const heading = classifyPlanTextHeading(line, allowWeekdayOnlySessions);
    if (heading?.kind === "notes") {
      currentSession = null;
      currentWeek = null;
      currentNotes = { kind: "notes", title: heading.title, lines: [] };
      groups.push(currentNotes);
      if (heading.remainder) {
        currentNotes.lines.push(stripPlanMarkup(heading.remainder));
      }
      continue;
    }
    if (heading?.kind === "week") {
      tableHeaders = null;
      groupType = null;
      groupLetter = null;
      currentSession = null;
      currentNotes = null;
      currentWeek = { kind: "week", title: heading.title, phase: heading.phase, sessions: [] };
      groups.push(currentWeek);
      continue;
    }
    if (heading?.kind === "session") {
      tableHeaders = null;
      groupType = null;
      groupLetter = null;
      currentNotes = null;
      currentBlockTag = null;
      currentSession = {
        kind: "session",
        countdown: heading.countdown,
        weekday: heading.weekday,
        title: heading.title,
        objective: null,
        coachNote: null,
        blocks: [],
        notes: [],
      };
      pushSession(currentSession);
      // Inline metadata split off a run-on heading line is parsed as body.
      if (heading.remainder) {
        addBodyLine(heading.remainder);
      }
      continue;
    }

    if (!currentSession && (/^[A-Za-z][1-9]\d*[.):]?\s+\S/.test(cleanLine) ||
      ((cleanLine.includes("|") || cleanLine.includes("\t")) && /\bExercise\b/i.test(cleanLine)))) {
      currentNotes = null;
      currentSession = { kind: "session", countdown: null, weekday: null,
        title: "Training session", objective: null, coachNote: null, blocks: [], notes: [] };
      pushSession(currentSession);
    }
    if (currentSession) {
      // A standalone block-group header (e.g. "Rehab") tags the blocks beneath
      // it rather than rendering as a loose note.
      const blockGroup = matchBlockGroupLabel(line);
      if (blockGroup) {
        currentBlockTag = blockGroup;
        continue;
      }
      addBodyLine(line);
      continue;
    }
    if (currentNotes) {
      const content = stripPlanMarkup(line.replace(/^([-*]|\d+\.)\s+/, ""));
      if (content) {
        currentNotes.lines.push(content);
      }
      continue;
    }

    // Loose preamble before any heading: keep it in an intro notes card.
    const content = stripPlanMarkup(line.replace(/^([-*]|\d+\.)\s+/, ""));
    if (content) {
      currentNotes = { kind: "notes", title: "Plan", lines: [content] };
      groups.push(currentNotes);
    }
  }

  return groups;
}

function countdownDays(value: string | null): number | null {
  const match = value?.match(/^D-(\d+)$/i);
  if (!match) {
    return null;
  }
  const parsed = Number.parseInt(match[1], 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function dateFromCountdown(
  fightDate: string | null | undefined,
  countdown: string | null,
): string | null {
  const match = fightDate?.trim().match(/^(\d{4})-(\d{2})-(\d{2})/);
  const days = countdownDays(countdown);
  if (!match || days === null) {
    return null;
  }
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  date.setUTCDate(date.getUTCDate() - days);
  return date.toISOString().slice(0, 10);
}

function detailText(block: PlanTextBlock, labels: string[]): string[] {
  const wanted = new Set(labels.map((label) => label.toLowerCase()));
  return block.details
    .filter((detail) => detail.label && wanted.has(detail.label.toLowerCase()))
    .map((detail) => detail.text);
}

// Session types a block may inherit when its own name says nothing about what it
// is. Deliberately excludes "rehab" and "conditioning": those drive extra
// renderer behaviour (isRehabOrMobilityBlock's always-visible summary), so a
// whole session's worth of blocks must never acquire them by inheritance.
const INHERITABLE_SESSION_BLOCK_TYPES = new Set(["skill", "sparring"]);

// Only the plan's own group label (tag) says what kind of work a block is. The
// exercise name is never read: "Bike" or "Plank lift" says nothing reliable.
function inferBlockType(block: PlanTextBlock, sessionType: string | null = null): string {
  const value = (block.tag || "").toLowerCase();
  if (/rehab|prehab|mobility|activation|warm-?up|cool-?down|reset|recovery/.test(value)) {
    return "rehab";
  }
  if (/condition|aerobic|anaerobic|bike|run|sprint|interval/.test(value)) {
    return "conditioning";
  }
  if (/strength|power|throw|jump|plyo|lift/.test(value)) {
    return "strength_power";
  }
  if (/skill|technical|tactical|shadowbox|footwork|cue card|film|watch/.test(value)) {
    return "skill";
  }
  // An untagged block in a skill or sparring session takes the session's type;
  // anything else is unknown, so it is simply an exercise.
  const inherited = sessionType && INHERITABLE_SESSION_BLOCK_TYPES.has(sessionType) ? sessionType : null;
  return block.tag?.toLowerCase().replace(/\s+/g, "_") || inherited || "exercise";
}

function inferSessionType(session: PlanTextSession): string {
  // The session's title and its blocks' group labels, never exercise names.
  const blocks = session.blocks
    .map((block) => block.tag || "")
    .join(" ");
  const value = `${session.title} ${blocks}`.toLowerCase();
  if (/spar|coach-led|boxing|grappling|wrestling|muay thai|kickbox/.test(value)) {
    return "skill";
  }
  if (/recover|reset|mobility|breath|rest/.test(value)) {
    return "recovery";
  }
  if (/condition|aerobic|anaerobic|bike|run|sprint|interval/.test(value)) {
    return "conditioning";
  }
  if (/strength|power|throw|jump|plyo|lift/.test(value)) {
    return "strength_power";
  }
  if (/tactical|technical|skill|shadowbox|footwork|cue|film|watch|visuali/.test(value)) {
    return "skill";
  }
  return "mixed";
}

function toStructuredBlock(
  block: PlanTextBlock,
  index: number,
  sessionType: string | null = null,
): StructuredBlock {
  // "Purpose" and "Why today" are two DIFFERENT claims — what the drill does,
  // and why it earns a slot on this specific day. Joining them into one
  // unlabelled `purpose` string dropped both labels and rendered them as a
  // single run-on grey paragraph. A real structured card carries them as
  // labelled coaching cues, so the fallback emits the same shape and the two
  // paths render identically.
  const labelledContext = [
    ...detailText(block, ["Purpose"]).map((text) => `Purpose: ${text}`),
    ...detailText(block, ["Why"]).map((text) => `Why today: ${text}`),
  ];
  const progression = detailText(block, ["Progress"]).join(" ") || null;
  const regressions = detailText(block, ["Regress", "Regression", "Easier"]);
  const substitutions = detailText(block, ["Swap", "Swaps"]);
  const mappedLabels = new Set([
    "purpose",
    "how",
    "why",
    "progress",
    "regress",
    "regression",
    "easier",
    "swap",
    "swaps",
  ]);
  const coachingCues = [
    ...labelledContext,
    ...block.details
      .filter((detail) => !detail.label || !mappedLabels.has(detail.label.toLowerCase()))
      .map((detail) => (detail.label ? `${detail.label}: ${detail.text}` : detail.text)),
  ];

  return {
    block_id: `text-block-${index + 1}`,
    block_type: inferBlockType(block, sessionType),
    display_name: block.name,
    exercise_code: block.code || null,
    exercise_block: block.code ? block.code[0].toUpperCase() : null,
    group_type: block.groupType || null,
    prescribed_sets: block.prescribedSets || [],
    order_index: index,
    load: block.dose ? { display: block.dose } : null,
    purpose: null,
    execution_instructions: detailText(block, ["How"]).join(" ") || null,
    coaching_cues: coachingCues,
    regression_options: regressions,
    substitutions,
    progression_rule: progression,
  };
}

function toStructuredSession(session: PlanTextSession, index: number): StructuredSession {
  const notes = session.notes.join(" ").trim();
  const objective = [session.objective, notes].filter(Boolean).join(" ") || null;
  const sessionType = inferSessionType(session);
  return {
    session_id: `text-session-${index + 1}`,
    session_type: sessionType,
    title: session.title,
    objective,
    blocks: session.blocks.map((block, blockIndex) =>
      toStructuredBlock(block, blockIndex, sessionType),
    ),
  };
}

function toStructuredDays(
  sessions: PlanTextSession[],
  fightDate: string | null | undefined,
  phase: string | null,
): StructuredDay[] {
  const grouped = new Map<string, PlanTextSession[]>();
  sessions.forEach((session, index) => {
    const key = session.countdown || shortWeekday(session.weekday)?.toLowerCase() || "session-" + (index + 1);
    grouped.set(key, [...(grouped.get(key) || []), session]);
  });

  return [...grouped.values()].map((daySessions, dayIndex) => {
    const countdown = daySessions[0]?.countdown || null;
    const coachLed = daySessions.find((session) => session.coachNote);
    const appSessions = daySessions.filter(
      (session) => session !== coachLed || session.blocks.length > 0,
    );
    const coachOnly = Boolean(coachLed && appSessions.length === 0);
    return {
      date: dateFromCountdown(fightDate, countdown),
      weekday: shortWeekday(daySessions[0]?.weekday),
      countdown_label: countdown,
      phase_label: phase,
      day_type: "moderate",
      today_card: coachLed
        ? {
            headline: coachOnly ? coachLed.title : null,
            coach_led_contact: coachOnly ? null : coachLed.coachNote || coachLed.title,
          }
        : null,
      sessions: coachOnly
        ? []
        : appSessions.map((session, sessionIndex) =>
            toStructuredSession(session, dayIndex * 100 + sessionIndex),
          ),
    };
  });
}

const WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

function shortWeekday(value: string | null | undefined): StructuredDay["weekday"] {
  const short = titleizeToken(value || "").slice(0, 3) as StructuredDay["weekday"];
  return WEEKDAY_ORDER.includes(short as (typeof WEEKDAY_ORDER)[number]) ? short : null;
}

function renewableOpenSessions(sessions: PlanTextSession[]): PlanTextSession[] {
  const byWeekday = new Map<string, PlanTextSession>();
  for (const session of sessions) {
    if (session.countdown) {
      continue;
    }
    const weekday = shortWeekday(session.weekday);
    if (!weekday) {
      continue;
    }
    const current = byWeekday.get(weekday);
    // Session Cards follow Weekly Rhythm in the saved plan. Prefer the later,
    // detailed card when it carries executable work; otherwise the later coach
    // card is still the cleaner source for its title/note.
    if (!current || session.blocks.length > 0 || current.blocks.length === 0) {
      byWeekday.set(weekday, session);
    }
  }
  return WEEKDAY_ORDER.flatMap((weekday) => {
    const session = byWeekday.get(weekday);
    return session ? [session] : [];
  });
}

function weekIndex(value: string, fallback: number): number {
  const match = value.match(/\bWeek\s+(\d+)\b/i);
  return match ? Number.parseInt(match[1], 10) : fallback;
}

// Week headings carry navigation metadata as well as the actual coaching goal,
// for example "Week 1 (D-53 to D-47) — Restore structural tolerance". The
// renderer already supplies "Week N", so only the meaningful trailing focus
// belongs in week_goal. Keeping the metadata here caused visible labels such as
// "Week 1 — Week 1 (D-53...)" and made synthetic fallback weeks read
// "Week 2 — Week 1".
function weekGoal(value: string, phase: string | null): string | null {
  let goal = value
    .replace(/^\s*Week\s+\d+\b/i, "")
    .replace(/^\s*\(\s*D-?\d+\s*(?:to|through|[-–—→])\s*D-?\d+\s*\)/i, "")
    .replace(/^\s*[-–—:|]+\s*/, "")
    .trim();

  if (phase) {
    const escapedPhase = phase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    goal = goal
      .replace(new RegExp(`^${escapedPhase}\\b`, "i"), "")
      .replace(/^\s*[-–—:|]+\s*/, "")
      .trim();
  }

  return goal || null;
}

function toStructuredWeek(
  week: PlanTextWeek,
  index: number,
  fightDate: string | null | undefined,
): StructuredWeek {
  const days = toStructuredDays(week.sessions, fightDate, week.phase);
  const dates = days
    .map((day) => day.date)
    .filter((date): date is string => Boolean(date))
    .sort();
  const countdowns = days
    .map((day) => day.countdown_label)
    .filter((countdown): countdown is string => Boolean(countdown));
  return {
    week_id: `text-week-${index + 1}`,
    week_index: weekIndex(week.title, index + 1),
    phase_label: week.phase,
    week_goal: weekGoal(week.title, week.phase),
    start_date: dates[0] || null,
    end_date: dates[dates.length - 1] || null,
    countdown_start: countdowns[0] || null,
    countdown_end: countdowns[countdowns.length - 1] || null,
    days,
  };
}

function noteCategory(title: string): string {
  const normalized = title.toLowerCase();
  if (normalized.includes("injur") || normalized.includes("safety")) return "injury";
  if (normalized.includes("nutrition") || normalized.includes("weight")) return "nutrition";
  if (normalized.includes("recover")) return "recovery";
  return "general";
}

// Open-plan note groups that are system mechanics rather than athlete
// reminders. "Plan" is the untitled loose-prose catch-all: in an open plan
// that loose prose IS the rendered rule sections (legacy saved texts run the
// headings into the paragraphs), so it is excluded from plan_notes as well.
// Genuine note sections (Lead/Active/Final notes) keep rendering.
const OPEN_PLAN_SYSTEM_NOTE_RE = new RegExp(
  "^(?:plan|immediate coach summary|current training rules|progression rules|priority hierarchy|adjustment rules" +
    "|rehab\\s*/\\s*red flags|\\d+-week reassessment gate|end notes(?:\\s*\\(coach-facing\\))?" +
    "|red-?flags? triggers?(?:\\s*\\([^)]*\\))?)$",
  "i",
);

const RED_FLAG_SECTION_TITLE_RE = /red-?flag|rehab/i;
// A line that is ONLY rendering meta ("No rehab headings will be used…") and
// never a trigger. Matched at the start of the line only: legacy run-on prose
// appends this meta AFTER the real trigger sentence on the same line, and
// cleanRedFlagText already cuts that tail off, so a mid-line mention must not
// disqualify the whole line.
const RED_FLAG_META_LINE_RE = /^no rehab headings\b/i;
const RED_FLAG_TRIGGER_LABEL_RE = /red-?flags? triggers?\s*(?:\([^)]*\))?\s*:?\s*/i;
// Where the red-flag sentence ends inside run-on legacy prose: the next system
// section (or rendering meta) that follows it on the same line.
const RED_FLAG_CUT_RE =
  /\s+(?:No rehab headings\b|(?:\d+-Week Reassessment Gate|Priority Hierarchy|Adjustment Rules|Progression Rules|Current Training Rules|End notes)\b(?=\s+[A-Z]))/i;

function cleanRedFlagText(value: string): string {
  let text = value.trim();
  const label = text.match(RED_FLAG_TRIGGER_LABEL_RE);
  if (label?.index === 0) {
    text = text.slice(label[0].length);
  }
  const cut = text.search(RED_FLAG_CUT_RE);
  if (cut > -1) {
    text = text.slice(0, cut);
  }
  return text.trim();
}

/**
 * Deterministically lift the open plan's stop-and-report triggers out of the
 * parsed note groups: a titled red-flag section contributes its trigger lines,
 * and legacy run-on prose contributes the sentence following its "Red-flag
 * triggers:" label. Emitting these as red_flag_rules makes the Red Flags card
 * render the real stop rules instead of falling back to whole-plan note dumps.
 */
function extractOpenPlanRedFlags(noteGroups: PlanTextNotes[]): StructuredRedFlagRule[] {
  const texts: string[] = [];
  const push = (value: string) => {
    const text = cleanRedFlagText(value);
    if (text && !texts.includes(text)) {
      texts.push(text);
    }
  };
  for (const group of noteGroups) {
    if (RED_FLAG_SECTION_TITLE_RE.test(group.title)) {
      for (const line of group.lines) {
        if (!RED_FLAG_META_LINE_RE.test(line.trim())) {
          push(line);
        }
      }
      continue;
    }
    for (const line of group.lines) {
      const labelIndex = line.search(RED_FLAG_TRIGGER_LABEL_RE);
      if (labelIndex > -1) {
        push(line.slice(labelIndex));
      }
    }
  }
  return texts.map((display_text, index) => ({
    rule_id: `text-red-flag-${index + 1}`,
    display_text,
  }));
}

/**
 * Adapt approved plan_text into the full structured renderer without another
 * model call. Every field comes from the saved athlete-facing text, so a missing
 * server-side structured payload can never force the legacy renderer.
 */
export function buildStructuredPlanFromText(
  rawText: string,
  fightDate?: string | null,
): StructuredPlan {
  const groups = parsePlanText(rawText);
  const noteGroups = groups
    .filter((group): group is PlanTextNotes => group.kind === "notes")
    .filter((group) => group.lines.length > 0);
  const explicitWeeks = groups.filter(
    (group): group is PlanTextWeek => group.kind === "week",
  );
  const looseSessions = groups.filter(
    (group): group is PlanTextSession => group.kind === "session",
  );
  const openSessions = !fightDate ? renewableOpenSessions(looseSessions) : [];
  const isOpenTextPlan = openSessions.length > 0;
  // In an open plan the schedule spine and week goals already carry the
  // athlete-facing content; the remaining prose sections are system mechanics.
  // Route safety triggers to red_flag_rules and progression rules to
  // progression_notes, and keep the system/loose prose out of plan_notes so
  // Active Notes and the Red Flags fallback never dump the whole plan text.
  const redFlagRules = isOpenTextPlan ? extractOpenPlanRedFlags(noteGroups) : [];
  const progressionSection = isOpenTextPlan
    ? noteGroups.find((group) => /^progression rules$/i.test(group.title))
    : undefined;
  const notes = noteGroups
    .filter((group) => !isOpenTextPlan || !OPEN_PLAN_SYSTEM_NOTE_RE.test(group.title))
    .map((group) => ({
      category: noteCategory(group.title),
      label: group.title,
      text: group.lines.join(" "),
    }));
  const openWeekMap = new Map<number, PlanTextWeek>();
  if (openSessions.length > 0) {
    const explicitWeekMap = new Map<number, PlanTextWeek>();
    for (const week of explicitWeeks) {
      const index = weekIndex(week.title, 0);
      if (index >= 1 && index <= 4) {
        explicitWeekMap.set(index, week);
      }
    }
    for (let index = 1; index <= 4; index += 1) {
      const explicitWeek = explicitWeekMap.get(index);
      openWeekMap.set(
        index,
        explicitWeek
          ? { ...explicitWeek, sessions: openSessions }
          : {
              kind: "week",
              title: `Week ${index}`,
              phase: null,
              sessions: openSessions,
            },
      );
    }
  }
  const weekGroups = openSessions.length > 0
    ? [...openWeekMap.values()]
    : [...explicitWeeks];
  if (openSessions.length === 0 && looseSessions.length > 0) {
    weekGroups.push({
      kind: "week",
      title: explicitWeeks.length > 0 ? `Week ${explicitWeeks.length + 1}` : "Week 1",
      phase: null,
      sessions: looseSessions,
    });
  }
  if (weekGroups.length === 0) {
    weekGroups.push({ kind: "week", title: "Week 1", phase: null, sessions: [] });
  }

  return {
    schema_version: "text-adapter.v1",
    plan_metadata: {
      title: isOpenTextPlan ? "Open training plan" : "Fight Camp",
      plan_type: isOpenTextPlan ? "open_ongoing_system" : "fight_camp",
      status: "ready",
    },
    event_context: fightDate ? { fight_date: fightDate } : null,
    red_flag_rules: redFlagRules.length > 0 ? redFlagRules : null,
    plan_notes: notes,
    weeks: weekGroups.map((week, index) => toStructuredWeek(week, index, fightDate)),
    progression_notes: progressionSection ? progressionSection.lines.join(" ") : null,
    raw_markdown_fallback: rawText,
  };
}
