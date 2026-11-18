"use client";
import { CustomPlanCalendar } from './custom-plan-calendar';
import { addCalendarDays, calendarWeekday, calendarMonday, datedCopy, duplicateCalendarWeek, copySessionsToDays, moveSessionsToDay, nextCalendarMonday } from '@/lib/custom-plan-calendar';
import type { CustomPlan } from '@/lib/api-schema.generated';
import { ExerciseLogProvider } from "./exercise-log";
import { personalPerformance } from "@/lib/weight-recommendation";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { formatAppDate, formatAppDateRange } from "@/lib/date-format";
import { customPlanInput } from "@/lib/custom-plan-share";
import { trackProductEvent, productEventIdentity, deleteCustomPlan, getCommunityExercise, getCustomDoseSuggestion, getCustomPlan, getExerciseLibrary, getTrainingLibraryEntry, publishCustomPlan, saveCustomPlan } from "@/lib/api";
import type { MeResponse, StructuredBlock } from "@/lib/types";
import { ExerciseRow } from "./structured-plan-renderer";
import { DotsIcon } from "./exercise-customisation";
import { ExerciseMediaProvider } from "./exercise-demo";
import { customSchedulePrefill, customScheduleIssues, prepareCustomSession, customPlanError, sessionNames } from "@/lib/custom-plan-schedule";
import type {
  CommunityEntry,
  CustomDose,
  CustomDoseSuggestion,
  CustomExercise,
  CustomSession,
  CustomPlanInput,
  LibraryEntry,
  LibraryPreview,
  ManualExercise,
} from "@/lib/api-schema.generated";
import { InjuryDetailSheet } from "./today/injury-detail-sheet";
import { DrillPreview, TrainingLibrary } from "./training-library";
import { CustomDoseEditor } from "./custom-dose-editor";
import { usePastExercises } from "./load-field";
import { pastExercisesByKey } from "@/lib/exercise-library";
import { CommunityContributionSheet, CommunityExercisePreview, communityDetailsChanged } from "./community-exercises";
import { PrivateExerciseMediaProvider, PrivateExerciseVideo, PrivateExerciseVideoSheet } from "./private-exercise-video";
import { blankDose, doseSummary, doseIncomplete, exerciseIncomplete, libraryPrefill } from "@/lib/custom-plan-dose";
import { CustomExercisePicker } from "./custom-exercise-picker";
import styles from "./custom-plan-builder.module.css";
import { useFadeNavigate } from "@/components/fade-through";
import { ImportFlags, type ImportedPlan } from "./plan-import";
import type { ImportFlag } from "@/lib/api-schema.generated";
type Exercise = (Omit<CustomExercise, "snapshot"> & { snapshot: LibraryEntry }) | ManualExercise;
type Session = Omit<CustomSession, "exercises" | "session_type"> & { exercises: Exercise[]; session_type?: CustomSession["session_type"] };
type Draft = { id?: string; name: string; sessions: Session[]; incomplete?: boolean; schedule_mode?: "weekly" | "dated"; start_date?: string | null; end_date?: string | null };
const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const;
const REST_DAY_REMINDER = "Keep this day for recovery.";
type Weekday = (typeof weekdays)[number];
/** The day strip shows one dot per day, coloured by the day's first non-rest session type. */
function dayType(sessions: Session[]) {
  const working = sessions.find(s => s.session_type !== "rest");
  return working ? working.session_type ?? "mixed" : "rest";
}
/** Opens on the first scheduled day so a reopened plan always lands on real content. */
function firstScheduledDay(sessions: Session[]): Weekday {
  return weekdays.find(day => sessions.some(s => s.weekday === day)) ?? "Monday";
}
/** A new session starts on the day the athlete is looking at; its name follows its type until they rename it. */
function newSession(weekday: Session["weekday"]): Session {
  return { id: crypto.randomUUID(), name: sessionNames.mixed, weekday, session_type: "mixed", scheduled_date: null, exercises: [] };
}
/** Types that are built from exercises, so adding one goes straight on to choosing them. */
const EXERCISE_TYPES: Array<CustomSession["session_type"]> = ["mixed", "strength_power", "conditioning"];
const SESSION_TYPES: Array<[CustomSession["session_type"], string]> = [
  ["mixed", "Workout"], ["strength_power", "Strength / power"], ["conditioning", "Conditioning"],
  ["skill", "Light technical / skill"], ["sparring", "Hard sparring"], ["recovery", "Recovery"], ["rest", "Off / Rest"],
];
/** A draft read from a plan file: every exercise is as written (manual), nothing saved yet.
 * A dose the coach left open is marked unfinished, the same as one added by hand. */
function importedPlanDraft({ plan, library }: ImportedPlan): Draft {
  return orderedDraft({ ...plan, sessions: (plan.sessions ?? []).map(session => ({
    ...session, scheduled_date: session.scheduled_date ?? null,
    weekday: session.weekday ?? (session.scheduled_date ? calendarWeekday(session.scheduled_date) as Session["weekday"] : null),
    // An exercise matched to the library arrives with its library details (demo video, guidance).
    exercises: (session.exercises ?? []).map(exercise => ({ ...exercise, ...(exercise.origin === "catalogue" ? { snapshot: library?.[exercise.id] } : {}),
      incomplete: exerciseIncomplete(exercise as Parameters<typeof exerciseIncomplete>[0]) }) as Exercise),
  })) });
}
function orderedDraft(draft: Draft): Draft {
  const day = (session: Session) => session.weekday ? weekdays.indexOf(session.weekday) : 7;
  return { ...draft, sessions: [...draft.sessions].sort((a, b) => (a.scheduled_date ?? "").localeCompare(b.scheduled_date ?? "") || day(a) - day(b)) };
}
const nameOf = (e: Exercise) => (e.origin === "manual" ? e.name : e.snapshot.name);
function draftBlock(exercise: Exercise, session: Session): StructuredBlock {
  const dose = exercise.dose;
  const snapshot = exercise.origin === "catalogue" ? exercise.snapshot : null;
  const type = snapshot?.content_type ?? (exercise.origin === "manual"
    ? ({ strength: "strength", bodyweight: "strength", power: "power", other: "" }[exercise.exercise_type ?? "other"]) : undefined);
  const blockType = exercise.block_type ?? ({ conditioning: "conditioning", skill: "skill", strength: "strength", power: "plyometric_power", speed: "speed" } as Record<string, string>)[type ?? ""]
    ?? ({ strength_power: "strength", conditioning: "conditioning", skill: "skill", sparring: "sparring", recovery: "cooldown_recovery" } as Record<string, string>)[session.session_type ?? "mixed"]
    ?? "accessory";
  const weight = dose.load_or_intensity?.trim().match(/^(\d+(?:\.\d+)?)\s*(kg|lb|lbs)$/i);
  return {
    block_id: exercise.id, display_name: nameOf(exercise), prescription_source: "athlete",
    block_type: blockType,
    exercise_type: exercise.origin === "manual" ? exercise.exercise_type : undefined,
    exercise_key: snapshot?.exercise_key, catalogue_id: exercise.origin === "catalogue" ? exercise.catalogue_id : null,
    private_media_id: exercise.origin === "manual" ? exercise.media_id : null,
    community_revision_id: exercise.origin === "manual" ? exercise.community_revision_id : null,
    athlete_dose: dose, prescription_text: doseSummary(dose, nameOf(exercise)),
    notes: exercise.origin === "manual" ? exercise.notes : null,
    intensity: dose.load_or_intensity,
    sets: dose.sets, reps: dose.reps, rounds: dose.rounds,
    duration: dose.duration_seconds ? { value: dose.duration_seconds, unit: "seconds" } : null,
    work: dose.work_seconds ? { value: dose.work_seconds, unit: "seconds" } : null,
    rest: dose.rest_seconds != null ? { value: dose.rest_seconds, unit: "seconds" } : null,
    distance: dose.distance && dose.distance_unit ? { value: dose.distance, unit: dose.distance_unit } : null,
    effort: dose.rpe ? { method: "RPE", value: dose.rpe } : null,
    load: weight ? { method: "absolute", value: Number(weight[1]), unit: weight[2].toLowerCase() === "kg" ? "kg" : "lb", display: dose.load_or_intensity } : null,
    coaching_cues: exercise.coaching_cues ?? [], stop_rules: exercise.stop_rules ?? [],
  };
}
function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}
function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />
    </svg>
  );
}
function PencilIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  );
}
export function CustomPlanBuilder({ accessToken, planId, addSessionDay, addSessionDate, onboarding = null, coachFighterId, onCoachClose, initialPlan, importedDraft, athleteTimezone, fightDate }: { importedDraft?: ImportedPlan; fightDate?: string | null; athleteTimezone?: string; initialPlan?: CustomPlan; onCoachClose?: () => void; coachFighterId?: string; accessToken: string; planId?: string; addSessionDay?: string | null; addSessionDate?: string | null; onboarding?: MeResponse | null }) {
  const router = useRouter();
  const requestId = useRef(crypto.randomUUID());
  const savedPlansHref = coachFighterId ? "/coach/plans" : "/plans#custom-plans";
  const fighterDate = fightDate !== undefined ? fightDate : onboarding?.latest_plan
    ? onboarding.latest_plan.fight_date
    : onboarding?.latest_intake?.no_scheduled_fight ? null : onboarding?.latest_intake?.fight_date;
  const scheduledFightDate = /^\d{4}-\d{2}-\d{2}$/.test(fighterDate ?? '') ? fighterDate : null;
  const fadeNavigate = useFadeNavigate();
  const [draft, setDraft] = useState<Draft>(() => {
    if (initialPlan) return orderedDraft({ ...initialPlan, id: undefined, name: `${initialPlan.name} revision`.slice(0, 100) });
    const start = nextCalendarMonday(athleteTimezone ?? onboarding?.profile.athlete_timezone ?? 'UTC');
    if (importedDraft) {
      const imported = importedPlanDraft(importedDraft);
      // Calendar is the default view: an undated import is laid out as dated weeks.
      if (imported.schedule_mode === 'dated' && imported.start_date && imported.end_date) return imported;
      return { ...imported, schedule_mode: 'dated', start_date: start, end_date: addCalendarDays(start, 27), sessions: datedCopy(imported.sessions, start, addCalendarDays(start, 27)) };
    }
    return { name: "My Training Plan", schedule_mode: 'dated', start_date: start, end_date: addCalendarDays(start, 27),
      sessions: planId ? [] : datedCopy(customSchedulePrefill(onboarding), start, addCalendarDays(start, 27)) };
  });
  const [selectedDate, setSelectedDate] = useState(draft.start_date ?? nextCalendarMonday(athleteTimezone ?? onboarding?.profile.athlete_timezone ?? 'UTC'));
  const isDated = draft.schedule_mode === 'dated';
  const sessionLimit = isDated ? 336 : 28;
  const [copying, setCopying] = useState<{ ids: string[]; move?: boolean } | null>(null);
  const [exerciseCopy, setExerciseCopy] = useState<Exercise>();
  const [exerciseCutSource, setExerciseCutSource] = useState<string>();
  const calendarRef = useRef<HTMLDivElement>(null);
  const [pasteDay, setPasteDay] = useState<string>();
  const [copyToDay, setCopyToDay] = useState('');
  const [copyNote, setCopyNote] = useState('');
  const [loading, setLoading] = useState(!!planId),
    [retry, setRetry] = useState(0),
    [pending, setPending] = useState(false),
    // An imported plan is unsaved until it is saved here.
    [dirty, setDirty] = useState(!!importedDraft);
  const [opened, setOpened] = useState(!planId);
  const [chosenDay, setChosenDay] = useState<Weekday>();
  // Tapping an import flag's title opens its session (and exercise) in the builder.
  const openFlag = (flag: ImportFlag) => {
    const session = draft.sessions.find(s => s.id === flag.session_id);
    if (!session) return;
    if (session.scheduled_date) setSelectedDate(session.scheduled_date);
    else if (session.weekday) setChosenDay(session.weekday as Weekday);
    window.setTimeout(() => {
      const target = (flag.exercise_id && document.getElementById(`exercise-${flag.exercise_id}`)) || document.getElementById(`session-${session.id}`);
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
      target?.classList.add(styles.flagged);
      window.setTimeout(() => target?.classList.remove(styles.flagged), 1600);
    }, 60);
  };
  // The add sheet remembers what it added this visit, so their doses can be set right there.
  const [adding, setAdding] = useState<{ sessionId: string; added: string[]; query?: string }>();
  // One added exercise is open at a time: the newest, so its dose can be set at once; the rest stay one line each.
  const [openAdded, setOpenAdded] = useState<string>();
  const [reviewOpen, setReviewOpen] = useState(false);
  const [quickDose, setQuickDose] = useState<string | null>(null);
  // A typed date passes through partial values; commit once on leaving the field, not per keystroke.
  const [startInput, setStartInput] = useState<string>();
  const [openExercise, setOpenExercise] = useState<string | null>(null);
  const [error, setError] = useState<string>(),
    [message, setMessage] = useState<string>();
  const [selecting, setSelecting] = useState<{ sessionId: string; query: string }>(),
    [editing, setEditing] = useState<{
      sessionId: string;
      exercise: Exercise;
      fresh?: boolean;
      position: number;
      original: Exercise;
      originalPosition: number;
    }>();
  const [sessionEdit, setSessionEdit] = useState<Session>(),
    // A new session joins the draft only when the sheet is confirmed, so closing it leaves nothing behind.
    // It holds the session as it opened, so an untouched new session is not an unsaved change.
    [sessionFresh, setSessionFresh] = useState<Session>(),
    [preview, setPreview] = useState<LibraryEntry>();
  const [videoOpen, setVideoOpen] = useState(false), [communityOpen, setCommunityOpen] = useState(false), [communityPreview, setCommunityPreview] = useState<string>();
  const [linkedCommunity, setLinkedCommunity] = useState<{ id: string; entry?: CommunityEntry; error?: boolean }>();
  const editingCommunityId = editing?.exercise.origin === "manual" ? editing.exercise.community_id : null;
  useEffect(() => {
    if (!editingCommunityId) return;
    const request = new AbortController();
    void getCommunityExercise(accessToken, editingCommunityId, 'owned', request.signal).then(entry => {
      if (!request.signal.aborted) setLinkedCommunity({ id: editingCommunityId, entry });
    }).catch(() => {
      if (!request.signal.aborted) setLinkedCommunity({ id: editingCommunityId, error: true });
    });
    return () => request.abort();
  }, [accessToken, editingCommunityId, communityOpen]);
  const [suggestion, setSuggestion] = useState<CustomDoseSuggestion>(),
    [suggestionError, setSuggestionError] = useState<string>(),
    [suggesting, setSuggesting] = useState(false);
  const suggestionRequest = useRef(0);
  // Where each added exercise's starting dose came from: undefined while loading, null when nothing had one.
  const [prefills, setPrefills] = useState<Record<string, string | null>>({});
  // Exercises the athlete has edited. A late prefill never lands on these.
  const touched = useRef(new Set<string>());
  const latest = useRef(draft);
  const undoStack = useRef<Draft[]>([]), redoStack = useRef<Draft[]>([]);
  const savedSnapshot = useRef(importedDraft ? "" : JSON.stringify(draft));
  const [checkedFlags, setCheckedFlags] = useState<number[]>([]);
  const flagsLeft = importedDraft && !draft.id ? importedDraft.flags.length - checkedFlags.length : 0;
  const importNoteRef = useRef<HTMLDivElement>(null);
  const [historyVersion, setHistoryVersion] = useState(0);
  useEffect(() => { latest.current = draft; });
  // Read only once an exercise is open, for last time's weight.
  const past = usePastExercises(!coachFighterId && (adding || editing) ? accessToken : null);
  useEffect(() => {
    if (!planId) void trackProductEvent(accessToken, "plan_editor_opened", "plans");
  }, [accessToken, planId]);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const sheetDirty =
    Boolean(
      editing &&
      (JSON.stringify(editing.exercise) !== JSON.stringify(editing.original) ||
        editing.position !== editing.originalPosition),
    ) ||
    Boolean(
      sessionEdit &&
      JSON.stringify(sessionEdit) !== JSON.stringify(sessionFresh ?? draft.sessions.find((s) => s.id === sessionEdit.id)),
    );
  const unsaved = dirty || sheetDirty;
  const releaseGuard = useRef<() => Promise<void>>(async () => {});
  const loaded = useRef(false),
    heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!planId || loaded.current) return;
    let active = true;
    void getCustomPlan(accessToken, planId, coachFighterId)
      .then((plan) => {
        if (active) {
          loaded.current = true;
          setOpened(true);
          void productEventIdentity(planId).then(flow => trackProductEvent(accessToken, "plan_editor_opened", "plans", undefined, flow)).catch(() => {});
          setError(undefined);
          const repaired = orderedDraft({ ...plan, sessions: plan.sessions.map(s => prepareCustomSession(s, onboarding)) });
          setDraft(repaired);
          savedSnapshot.current = JSON.stringify(repaired);
          if (repaired.schedule_mode === 'dated') setSelectedDate(repaired.sessions[0]?.scheduled_date ?? repaired.start_date!);
          const day = weekdays.find(value => value.slice(0, 3).toLowerCase() === addSessionDay?.toLowerCase());
          // A dated session needs the tapped date; a weekday alone would leave it off the calendar and unsaveable.
          const dated = repaired.schedule_mode === 'dated';
          const date = dated && addSessionDate && addSessionDate >= repaired.start_date! && addSessionDate <= repaired.end_date! ? addSessionDate : null;
          const limit = dated ? 336 : 28;
          if (day && repaired.sessions.length >= limit) setError(`This plan already has the maximum ${limit} sessions.`);
          else if (day && (!dated || date)) {
            setChosenDay(day);
            if (date) setSelectedDate(date);
            const session = { ...newSession(date ? calendarWeekday(date) : day), scheduled_date: date };
            setSessionFresh(session);
            setSessionEdit(session);
          }
          setDirty(JSON.stringify(repaired.sessions) !== JSON.stringify(plan.sessions));
          window.scrollTo(0, 0);
        }
      })
      .catch((reason) => {
        if (active) setError(customPlanError(reason, "Unable to open plan."));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [accessToken, planId, retry, onboarding, addSessionDay, addSessionDate, coachFighterId]);
  useEffect(() => {
    if (!loading && loaded.current) heading.current?.focus({ preventScroll: true });
  }, [loading]);
  // Back inside the builder closes the open sheet or library screen; only Back from the plan itself leaves.
  const layer = useRef<{ open: boolean; close: () => void }>({ open: false, close: () => {} });
  useEffect(() => {
    layer.current = {
      open: Boolean(videoOpen || communityOpen || communityPreview || preview || editing || sessionEdit || adding || selecting || reviewOpen || pasteDay),
      close: () => {
        if (pasteDay) setPasteDay(undefined);
        else if (videoOpen) setVideoOpen(false);
        else if (communityOpen) setCommunityOpen(false);
        else if (communityPreview) setCommunityPreview(undefined);
        else if (preview) setPreview(undefined);
        else if (editing) closeEditing();
        else if (sessionEdit) closeSession();
        // The full library covers the add sheet it was opened from, so it closes first.
        else if (selecting) setSelecting(undefined);
        else if (adding) setAdding(undefined);
        else setReviewOpen(false);
      },
    };
  });
  useEffect(() => {
    if (!unsaved) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    // Keep one same-URL entry while dirty so Back can confirm without losing edits.
    // Reuse it when it is already there: stacked entries made Back prompt again on the same page.
    const state = { ...window.history.state, customDraftGuard: true };
    if (!window.history.state?.customDraftGuard) window.history.pushState(state, "", window.location.href);
    const back = () => {
      if (layer.current.open) {
        window.history.pushState(state, "", window.location.href);
        layer.current.close();
        return;
      }
      if (!window.confirm("Discard unsaved changes?")) {
        window.history.pushState(state, "", window.location.href);
        return;
      }
      window.removeEventListener("popstate", back);
      setDirty(false);
      window.history.back();
    };
    const release = () => {
      window.removeEventListener("popstate", back);
      if (!window.history.state?.customDraftGuard) return Promise.resolve();
      return new Promise<void>((resolve) => {
        window.addEventListener("popstate", () => resolve(), { once: true });
        window.history.back();
      });
    };
    releaseGuard.current = release;
    window.addEventListener("popstate", back);
    const click = (e: MouseEvent) => {
      const anchor = (e.target as Element)?.closest<HTMLAnchorElement>("a[href]");
      if (!anchor || e.ctrlKey || e.metaKey || anchor.target === "_blank") return;
      e.preventDefault();
      e.stopPropagation();
      if (window.confirm("Discard unsaved changes?"))
        void release().then(() => {
          setDirty(false);
          router.push(anchor.href);
        });
    };
    window.addEventListener("beforeunload", warn);
    document.addEventListener("click", click, true);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("click", click, true);
      window.removeEventListener("popstate", back);
      releaseGuard.current = async () => {};
    };
  }, [unsaved, router]);
  // Updates build on the latest draft, so a prefill landing between taps is never lost.
  function change(next: (current: Draft) => Draft) {
    const current = latest.current;
    const updated = orderedDraft(next(current));
    if (JSON.stringify(updated) === JSON.stringify(current)) return;
    undoStack.current.push(current);
    if (undoStack.current.length > 100) undoStack.current.shift();
    redoStack.current = [];
    latest.current = updated;
    setDraft(updated);
    setHistoryVersion(n => n + 1);
    setDirty(JSON.stringify(updated) !== savedSnapshot.current);
    setMessage(undefined);
  }
  function travelHistory(direction: 'undo' | 'redo') {
    const source = direction === 'undo' ? undoStack : redoStack;
    const destination = direction === 'undo' ? redoStack : undoStack;
    const next = source.current.pop();
    if (!next) return;
    destination.current.push(latest.current);
    // Stay on the day being viewed unless the restored range no longer contains it.
    if (next.schedule_mode === 'dated' && (selectedDate < next.start_date! || selectedDate > next.end_date!)) setSelectedDate(next.start_date!);
    latest.current = next; setDraft(next); setDirty(JSON.stringify(next) !== savedSnapshot.current);
    setHistoryVersion(n => n + 1); setMessage(undefined);
  }
  function selectScheduleMode(mode: 'weekly' | 'dated') {
    if (mode === draft.schedule_mode) return;
    if (mode === 'weekly') {
      if (draft.sessions.length && !window.confirm('Create a repeating copy using only the first week? This does not change any saved plan.')) return;
      requestId.current = crypto.randomUUID();
      const first = draft.start_date ?? selectedDate;
      change(current => ({ ...current, id: undefined, schedule_mode: 'weekly', start_date: null, end_date: null,
        sessions: current.sessions.filter(s => !s.scheduled_date || s.scheduled_date < addCalendarDays(first, 7)).map(s => ({ ...s, scheduled_date: null })) }));
    } else {
      if (draft.id && !window.confirm('Create a dated copy? Your saved weekly plan will stay unchanged.')) return;
      requestId.current = crypto.randomUUID();
      const start = nextCalendarMonday(athleteTimezone ?? onboarding?.profile.athlete_timezone ?? 'UTC');
      change(current => ({ ...current, id: undefined, schedule_mode: 'dated', start_date: start, end_date: addCalendarDays(start, 27), sessions: datedCopy(current.sessions, start, addCalendarDays(start, 27)) }));
      setSelectedDate(start);
    }
  }
  function resizeDates(start: string, weeks: number) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !Number.isInteger(weeks) || weeks < 1 || weeks > 12) return;
    const end = addCalendarDays(start, weeks * 7 - 1);
    const offset = Math.round((Date.parse(start) - Date.parse(draft.start_date!)) / 86400000);
    if (offset && draft.sessions.length && !window.confirm('Move all draft sessions by the same number of days to keep their relative schedule?')) return;
    const shifted = draft.sessions.map(s => ({ ...s, scheduled_date: addCalendarDays(s.scheduled_date!, offset), weekday: calendarWeekday(addCalendarDays(s.scheduled_date!, offset)) }));
    const outside = shifted.filter(s => s.scheduled_date < start || s.scheduled_date > end);
    if (outside.length && !window.confirm(`Remove ${outside.length} sessions outside the new range? Cancel to move them first.`)) return;
    change(current => ({ ...current, start_date: start, end_date: end, sessions: shifted.filter(s => s.scheduled_date >= start && s.scheduled_date <= end) }));
    setSelectedDate(start);
  }
  function moveSession(id: string, day: string) {
    if (day < draft.start_date! || day > draft.end_date!) { setError('Choose a date inside this plan.'); return; }
    const week = calendarMonday(day);
    if (draft.sessions.filter(s => s.id !== id && s.scheduled_date && calendarMonday(s.scheduled_date) === week).length >= 28) { setError('Each week can hold at most 28 sessions.'); return; }
    sessionChange(id, s => ({ ...s, scheduled_date: day, weekday: calendarWeekday(day) })); setSelectedDate(day);
  }
  function copyTo(sources: Session[], days: string[]) {
    try {
      const sessions = copySessionsToDays(draft.sessions, sources, days, draft.start_date!, draft.end_date!, sessionLimit);
      change(current => ({ ...current, sessions })); setError(''); return true;
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to copy session.'); return false; }
  }
  function moveTo(ids: string[], day: string) {
    try {
      const sessions = moveSessionsToDay(draft.sessions, ids, day, draft.start_date!, draft.end_date!);
      change(current => ({ ...current, sessions })); setError(''); setSelectedDate(day); setCopying(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to move sessions.'); }
  }
  function startExerciseCopy(exercise: Exercise, source?: string) {
    if (exerciseCopy?.id === exercise.id && exerciseCutSource === source) { setExerciseCopy(undefined); setExerciseCutSource(undefined); return; }
    setCopying(null); setExerciseCopy(exercise); setExerciseCutSource(source); setError(undefined);
    requestAnimationFrame(() => calendarRef.current?.scrollIntoView({ block: 'start' }));
  }
  function pasteExercise(day: string, sessionId?: string) {
    if (!exerciseCopy) return;
    const currentDraft = latest.current;
    const weekday = isDated ? calendarWeekday(day) : day as Weekday;
    if (isDated && (day < currentDraft.start_date! || day > currentDraft.end_date!)) return;
    const source = exerciseCutSource ? currentDraft.sessions.find(s => s.id === exerciseCutSource) : undefined;
    if (exerciseCutSource && !source?.exercises.some(e => e.id === exerciseCopy.id)) { setError('The original exercise is no longer available.'); return; }
    if (exerciseCutSource && exerciseCutSource === sessionId) { setPasteDay(undefined); setExerciseCopy(undefined); setExerciseCutSource(undefined); return; }
    const exercise = { ...(source?.exercises.find(e => e.id === exerciseCopy.id) ?? exerciseCopy), id: exerciseCutSource ? exerciseCopy.id : crypto.randomUUID() };
    if (sessionId) {
      const session = currentDraft.sessions.find(s => s.id === sessionId);
      if (!session || session.session_type === 'rest' || session.exercises.length >= 100) { setError('Choose a session with room for another exercise.'); return; }
      change(current => ({ ...current, sessions: current.sessions.map(s => s.id === sessionId ? { ...s, exercises: [...s.exercises, exercise] } : s.id === exerciseCutSource ? { ...s, exercises: s.exercises.filter(e => e.id !== exercise.id) } : s) }));
    } else {
      if (currentDraft.sessions.length >= sessionLimit || (isDated && currentDraft.sessions.filter(s => s.scheduled_date && calendarMonday(s.scheduled_date) === calendarMonday(day)).length >= 28)) { setError('This plan or week has reached its session limit.'); return; }
      const session = { ...newSession(weekday), scheduled_date: isDated ? day : null, exercises: [exercise] };
      change(current => ({ ...current, sessions: [...current.sessions.map(s => s.id === exerciseCutSource ? { ...s, exercises: s.exercises.filter(e => e.id !== exercise.id) } : s), session] }));
    }
    if (isDated) setSelectedDate(day); else setChosenDay(weekday);
    setPasteDay(undefined); setError(undefined);
    requestAnimationFrame(() => calendarRef.current?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus({ preventScroll: true }));
    if (exerciseCutSource) { setExerciseCopy(undefined); setExerciseCutSource(undefined); }
  }
  function choosePasteDay(day: string) {
    const sessions = latest.current.sessions.filter(s => (isDated ? s.scheduled_date === day : s.weekday === day) && s.session_type !== 'rest');
    if (sessions.length) setPasteDay(day); else pasteExercise(day);
  }
  function moveWeeklySession(day: Weekday) {
    if (!copying?.move) return;
    change(current => ({ ...current, sessions: current.sessions.map(s => copying.ids.includes(s.id) ? { ...s, weekday: day } : s) }));
    setChosenDay(day); setCopying(null);
  }
  function addDatedSession(day: string) {
    setSelectedDate(day);
    const session = { ...newSession(calendarWeekday(day)), scheduled_date: day };
    setSessionFresh(session); setSessionEdit(session);
  }
  function sessionChange(id: string, update: (s: Session) => Session) {
    change(current => ({ ...current, sessions: current.sessions.map((s) => (s.id === id ? update(s) : s)) }));
  }
  function edit(sessionId: string, exercise: Exercise, fresh = false) {
    suggestionRequest.current++;
    setSuggesting(false);
    setSuggestion(undefined);
    setSuggestionError(undefined);
    setEditing({
      sessionId,
      exercise,
      fresh,
      position: draft.sessions.find((s) => s.id === sessionId)?.exercises.findIndex((e) => e.id === exercise.id) ?? 0,
      original: exercise,
      originalPosition:
        draft.sessions.find((s) => s.id === sessionId)?.exercises.findIndex((e) => e.id === exercise.id) ?? 0,
    });
  }

  function addSessionOnDay(weekday: Session["weekday"]) {
    const session = { ...newSession(weekday), scheduled_date: isDated ? selectedDate : null };
    setSessionFresh(session);
    setSessionEdit(session);
  }
  function openSession(session: Session) {
    setSessionFresh(undefined);
    setCopyNote(''); setCopyToDay('');
    setSessionEdit(session);
  }
  function removeSession(session: Session) {
    if (!window.confirm(`Remove ${session.name}${session.exercises.length ? " and its exercises" : ""}?`)) return false;
    change(current => ({ ...current, sessions: current.sessions.filter((s) => s.id !== session.id) }));
    return true;
  }
  function closeSession() {
    if (!sessionEdit) return;
    const original = draft.sessions.find((s) => s.id === sessionEdit.id);
    if (sessionFresh || JSON.stringify(original) === JSON.stringify(sessionEdit) || window.confirm("Discard session edits?")) {
      if (sheetDirty && !dirty) void releaseGuard.current().then(() => setSessionEdit(undefined));
      else setSessionEdit(undefined);
    }
  }
  function closeEditing() {
    if (!editing) return;
    if (
      (JSON.stringify(editing.original) === JSON.stringify(editing.exercise) &&
        editing.originalPosition === editing.position) ||
      window.confirm("Discard exercise edits?")
    ) {
      suggestionRequest.current++;
      if (sheetDirty && !dirty) void releaseGuard.current().then(() => setEditing(undefined));
      else setEditing(undefined);
    }
  }
  /** Remembers an exercise added while the add sheet is open, so its dose shows there. */
  function noteAdded(sessionId: string, exerciseId: string) {
    setAdding(current => current?.sessionId === sessionId ? { ...current, added: [...current.added, exerciseId] } : current);
    setOpenAdded(exerciseId);
  }
  function canAdd(sessionId: string): boolean {
    const session = latest.current.sessions.find(s => s.id === sessionId);
    return Boolean(session && session.exercises.length < 100);
  }
  function addLibrary(sessionId: string, entry: LibraryEntry | LibraryPreview): boolean {
    if (entry.category !== "training" || !canAdd(sessionId)) return false;
    const id = crypto.randomUUID();
    const guidance = "guidance" in entry ? entry.guidance : null;
    sessionChange(sessionId, s => s.exercises.length >= 100 ? s : {
      ...s,
      exercises: [...s.exercises, {
        id, origin: "catalogue", catalogue_id: entry.catalogue_id, snapshot: entry, incomplete: true,
        coaching_cues: guidance?.Cues ?? null, stop_rules: guidance?.Stop ?? null, dose: blankDose(),
      }],
    });
    noteAdded(sessionId, id);
    prefill(id, entry, Boolean(guidance));
    return true;
  }
  /**
   * The library's format first; the athlete's last session fills what it leaves open.
   * Each exercise has its own request, and a result is dropped once the athlete has
   * edited that exercise or it has left the plan.
   */
  function prefill(id: string, entry: LibraryEntry, hasGuidance: boolean) {
    void Promise.all([
      getCustomDoseSuggestion(accessToken, entry.catalogue_id).catch(() => null),
      coachFighterId ? new Map() : pastExercisesByKey(accessToken, getExerciseLibrary),
      // Search results carry no coaching guidance; the preview already did.
      hasGuidance ? null : getTrainingLibraryEntry(accessToken, entry.catalogue_id).then(detail => detail.guidance).catch(() => null),
    ]).then(([suggestion, history, guidance]) => {
      if (!mounted.current) return;
      if (touched.current.has(id) || !latest.current.sessions.some(s => s.exercises.some(e => e.id === id))) return;
      const found = libraryPrefill(suggestion, entry.exercise_key ? history.get(entry.exercise_key) : null);
      setPrefills(current => ({ ...current, [id]: found?.source ?? null }));
      const cues = guidance?.Cues ?? null, stop = guidance?.Stop ?? null;
      const fill = <T extends Exercise>(exercise: T): T => ({
        ...exercise,
        ...(found && JSON.stringify(exercise.dose) === JSON.stringify(blankDose()) ? { dose: found.dose, incomplete: doseIncomplete(found.dose) } : {}),
        ...((cues || stop) && exercise.coaching_cues == null && exercise.stop_rules == null ? { coaching_cues: cues, stop_rules: stop } : {}),
      });
      const exercise = latest.current.sessions.flatMap(s => s.exercises).find(e => e.id === id)!;
      if (JSON.stringify(fill(exercise)) === JSON.stringify(exercise)) return;
      change(current => ({ ...current, sessions: current.sessions.map(session => ({
        ...session, exercises: session.exercises.map(exercise => exercise.id === id ? fill(exercise) : exercise),
      })) }));
      // An editor open on this exercise, still untouched, shows the same values instead of saving blanks over them.
      setEditing(current => current && current.exercise.id === id && JSON.stringify(current.exercise) === JSON.stringify(current.original)
        ? { ...current, exercise: fill(current.exercise), original: fill(current.original) } : current);
    });
  }
  function markTouched(exerciseId: string) {
    touched.current.add(exerciseId);
    setPrefills(current => ({ ...current, [exerciseId]: "Your dose" }));
  }
  function setDose(sessionId: string, exerciseId: string, dose: CustomExercise["dose"]) {
    markTouched(exerciseId);
    sessionChange(sessionId, s => ({ ...s, exercises: s.exercises.map(e => e.id === exerciseId ? { ...e, dose } : e) }));
  }

  async function suggest() {
    if (!editing || editing.exercise.origin !== "catalogue") return;
    const request = ++suggestionRequest.current;
    setSuggesting(true);
    setSuggestionError(undefined);
    try {
      const result = await getCustomDoseSuggestion(accessToken, editing.exercise.catalogue_id);
      if (request === suggestionRequest.current) setSuggestion(result);
    } catch (e) {
      if (request === suggestionRequest.current)
        setSuggestionError(customPlanError(e, "Unable to load source suggestion."));
    } finally {
      if (request === suggestionRequest.current) setSuggesting(false);
    }
  }
  const issues = customScheduleIssues(draft.sessions);
  const addingSession = adding ? draft.sessions.find(s => s.id === adding.sessionId) : undefined;
  const addedHere = addingSession ? addingSession.exercises.filter(e => adding!.added.includes(e.id)) : [];
  const draftValid = Boolean(draft.name.trim()) && draft.sessions.every(s => s.name.trim() && s.exercises.every(e => nameOf(e).trim()));
  const publishable = draftValid && issues.length === 0;
  const selectedDay = isDated ? calendarWeekday(selectedDate) : chosenDay ?? firstScheduledDay(draft.sessions);
  const daySessions = draft.sessions.filter(s => isDated ? s.scheduled_date === selectedDate : s.weekday === selectedDay);
  const unassigned = draft.sessions.filter(s => !s.weekday);
  // The count and Fix entries share the exercise data, independently of session-level validation.
  const reviewSessions = draft.sessions.map(session => ({
    session,
    incomplete: session.session_type === "rest" ? [] : session.exercises.filter(e => exerciseIncomplete(e)),
    sessionIssues: issues.filter(issue => issue.sessionId === session.id && !issue.prescription),
  })).filter(group => group.incomplete.length || group.sessionIssues.length);
  const issueSessions = new Set(reviewSessions.map(group => group.session.id));
  const incompleteCount = reviewSessions.reduce((count, group) => count + group.incomplete.length, 0);
  function createManual(sessionId: string, name = "") {
    edit(sessionId, {
      id: crypto.randomUUID(), origin: "manual", exercise_type: null, name, equipment: null, notes: null, media_id: null,
      community_id: null, community_revision_id: null, dose: blankDose(), incomplete: true,
      coaching_cues: null, stop_rules: null,
    }, true);
  }
  function renderSession(s: Session) {
    const i = draft.sessions.indexOf(s);
    return (
      <section key={s.id} id={`session-${s.id}`} className={`sp-session ${styles.session}`} aria-label={`Session ${i + 1}`}>
        <div className="sp-session-head">
          <div>
            <small className={styles.weekday} data-attention={s.weekday ? undefined : "true"}>{s.scheduled_date ? `${calendarWeekday(s.scheduled_date)} · ${formatAppDate(s.scheduled_date)}` : s.weekday ?? "No day set"}</small>
            <h2 className="sp-session-title">{s.name}</h2>
          </div>
          <div className={styles.sessionActions}>
            <button type="button" className="ghost-button" disabled={draft.sessions.length >= sessionLimit} aria-pressed={isDated ? copying?.ids.length === 1 && copying.ids[0] === s.id && !copying.move : undefined} onClick={() => { setExerciseCopy(undefined); setExerciseCutSource(undefined); if (isDated) { setCopying(copying?.ids[0] === s.id && copying.ids.length === 1 && !copying.move ? null : { ids: [s.id] }); requestAnimationFrame(() => calendarRef.current?.scrollIntoView({ block: 'start' })); } else change(current => ({ ...current, sessions: [...current.sessions, { ...s, id: crypto.randomUUID(), exercises: s.exercises.map(e => ({ ...e, id: crypto.randomUUID() })) }] })); }}>Copy session</button>
            <button type="button" className={styles.editButton} aria-label={`Cut session ${s.name}`} title="Move session" aria-pressed={copying?.move === true && copying.ids.includes(s.id)} onClick={() => { setExerciseCopy(undefined); setExerciseCutSource(undefined); setCopying(current => current?.move && current.ids.length === 1 && current.ids[0] === s.id ? null : { ids: [s.id], move: true }); requestAnimationFrame(() => calendarRef.current?.scrollIntoView({ block: 'start' })); }}><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.5 7.5 20 18M8.5 16.5 20 6"/></svg></button>
            <button type="button" className={styles.editButton} aria-label={`Edit session ${i + 1}`} title="Edit session"
              onClick={() => openSession(s)}>
              <PencilIcon />
            </button>
            <button type="button" className={`${styles.editButton} ${styles.deleteButton}`} aria-label={`Remove ${s.name}`} title="Remove session"
              onClick={() => removeSession(s)}>
              <TrashIcon />
            </button>
          </div>
        </div>
        <div className="sp-session-meta"><span className="sp-tag">{sessionNames[s.session_type ?? "mixed"]}</span><span className={styles.badge}>{s.exercises.length} {s.exercises.length === 1 ? "exercise" : "exercises"}{issueSessions.has(s.id) ? " · Incomplete" : ""}</span></div>
        {s.notes?.trim() ? <p className={styles.sessionNotes}>{s.notes}</p> : null}
        {s.session_type !== "rest" ? <PrivateExerciseMediaProvider token={accessToken}>
          <ExerciseMediaProvider media={Object.fromEntries(s.exercises.flatMap(e => e.origin === "catalogue" && e.snapshot.media && e.snapshot.exercise_key
            ? [[`exercise:${e.snapshot.exercise_key}`, e.snapshot.media]] : []))}>
            <ExerciseLogProvider logging={{ logs: {}, canWrite: false, painReasonAllowed: false, save: async () => {},
              recentPerformances: [...(past?.values() ?? [])].map(entry => personalPerformance(entry)),
              allowProgression: [...(past?.values() ?? [])].some(entry => entry.allow_progression === true) }}>
            <ol className={`sp-blocks ex-rows ${styles.exercises}`}>
              {s.exercises.map((e) => (
                <li key={e.id} id={`exercise-${e.id}`}>
                  <ExerciseRow block={{ ...draftBlock(e, s),
                    progression_hold: e.origin === "manual" && (e.exercise_type === "power" || e.exercise_type === "other") ? true : past?.get(e.origin === "catalogue" ? e.snapshot.exercise_key ?? "" : `custom-name:${e.name.trim().toLowerCase().replace(/\s+/g, " ")}`)?.progression_hold,
                    bodyweight_progression: past?.get(e.origin === "catalogue" ? e.snapshot.exercise_key ?? "" : `custom-name:${e.name.trim().toLowerCase().replace(/\s+/g, " ")}`)?.bodyweight_progression,
                  }} open={openExercise === e.id}
                    onToggle={() => setOpenExercise(current => current === e.id ? null : e.id)} actions={
                      <button type="button" className={`${styles.editButton} ${styles.cardAction}`}
                        data-exercise-actions
                        aria-label={`Edit dose for ${nameOf(e)}`} title="Edit exercise" onClick={() => edit(s.id, e)}>
                        <DotsIcon />
                      </button>
                    } />
                  <div className={styles.quickControls}>
                    <button type="button" className={styles.copyExerciseButton} aria-label={`Cut ${nameOf(e)} to calendar`} title="Move exercise to calendar" aria-pressed={exerciseCutSource === s.id && exerciseCopy?.id === e.id} onClick={() => startExerciseCopy(e, s.id)}><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.5 7.5 20 18M8.5 16.5 20 6"/></svg></button>
                    <button type="button" className={styles.copyExerciseButton} aria-label={`Copy ${nameOf(e)} to calendar`} title="Copy exercise to calendar" aria-pressed={exerciseCopy?.id === e.id && !exerciseCutSource} onClick={() => startExerciseCopy(e)}><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="5" width="13" height="16" rx="2"/><path d="M9 5.5V3h8v2.5M4 16V6a2 2 0 0 1 2-2"/></svg></button>
                    <button type="button" aria-label={`Quick dose for ${nameOf(e)}`} aria-expanded={quickDose === e.id} onClick={() => setQuickDose(quickDose === e.id ? null : e.id)}>Quick dose</button>
                    {(['up', 'down'] as const).map(direction => <button key={direction} type="button" disabled={direction === 'up' ? s.exercises.indexOf(e) === 0 : s.exercises.indexOf(e) === s.exercises.length - 1} aria-label={`Move ${nameOf(e)} ${direction}`} onClick={() => sessionChange(s.id, session => { const exercises = [...session.exercises]; const index = exercises.findIndex(item => item.id === e.id); const target = index + (direction === 'up' ? -1 : 1); [exercises[index], exercises[target]] = [exercises[target], exercises[index]]; return { ...session, exercises }; })}>Move {direction}</button>)}
                  </div>
                  {quickDose === e.id ? <div className={styles.quickDose} role="group" aria-label={`Quick dose for ${nameOf(e)}`}><CustomDoseEditor dose={e.dose} movement={e.origin === 'catalogue' ? e.snapshot : e} onChange={dose => sessionChange(s.id, session => ({ ...session, exercises: session.exercises.map(item => item.id === e.id ? { ...item, dose, incomplete: exerciseIncomplete({ ...item, dose }) } : item) }))} /><button type="button" className="ghost-button" onClick={() => setQuickDose(null)}>Done editing dose</button></div> : null}
                </li>
              ))}
            </ol></ExerciseLogProvider></ExerciseMediaProvider></PrivateExerciseMediaProvider> : <p className="muted">{REST_DAY_REMINDER}</p>}
        {!s.exercises.length && s.session_type !== "rest" ? <p className="muted">{s.session_type === "recovery" ? REST_DAY_REMINDER : ["sparring", "skill"].includes(s.session_type ?? "mixed") ? "Coach-led day. Rounds optional." : "Workout needs exercises."}</p> : null}
        {s.session_type === "recovery" && s.exercises.length ? <p className="muted">Optional recovery. No need to complete.</p> : null}
        {s.session_type !== "rest" ? (
          <button type="button" className={styles.addExercise} aria-label={`Add exercise to ${s.name}`}
            disabled={s.exercises.length >= 100} onClick={() => setAdding({ sessionId: s.id, added: [] })}>
            <span aria-hidden="true">+</span> Add exercise
          </button>
        ) : null}
      </section>
    );
  }
  const saveBar = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const bar = saveBar.current;
    if (!bar || typeof ResizeObserver === "undefined") return;
    // The root copy drives scroll-padding, so focus and scroll-into-view stop
    // above the fixed save bar instead of underneath it.
    const root = document.documentElement;
    const observer = new ResizeObserver(() => {
      bar.closest<HTMLElement>(`.${styles.builder}`)?.style.setProperty("--custom-save-height", `${bar.offsetHeight}px`);
      root.style.setProperty("--custom-save-height", `${bar.offsetHeight}px`);
    });
    observer.observe(bar);
    return () => {
      observer.disconnect();
      root.style.removeProperty("--custom-save-height");
    };
  }, [loading, selecting]);
  async function closeCoach() {
    if (unsaved && !window.confirm("Discard unsaved changes?")) return;
    await releaseGuard.current();
    onCoachClose?.();
  }
  async function save(viewPlan = false) {
    if (!draftValid || (viewPlan && !publishable)) return;
    // An imported plan keeps its "things to check" until each is ticked as fixed or checked.
    if (flagsLeft) {
      setError(`Tick the ${flagsLeft === 1 ? "last thing" : `${flagsLeft} things`} to check above before saving.`);
      importNoteRef.current?.querySelector("details")?.setAttribute("open", "");
      importNoteRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    setPending(true);
    setError(undefined);
    try {
      const saved = await saveCustomPlan(
        accessToken,
        customPlanInput(draft),
        draft.id,
        coachFighterId,
        requestId.current,
      );
      if (!mounted.current) return;
      await releaseGuard.current();
      setDraft(orderedDraft(saved));
      savedSnapshot.current = JSON.stringify(orderedDraft(saved)); undoStack.current = []; redoStack.current = []; setHistoryVersion(n => n + 1);
      setOpened(true);
      setDirty(false);
      setMessage(coachFighterId ? "Draft saved. Assign it from Plans." : saved.incomplete ? "Draft saved." : "Plan saved. View your plan to activate it.");
      if (!coachFighterId) window.history.replaceState(window.history.state, "", `/plans/custom?plan=${saved.id}`);
      loaded.current = true;
      if (viewPlan && !coachFighterId) {
        const published = await publishCustomPlan(accessToken, saved.id);
        fadeNavigate(() => router.push(`/plans/${published.plan_id}`));
      }
    } catch (e) {
      setError(customPlanError(e, "Unable to save plan."));
    } finally {
      setPending(false);
    }
  }
  if (loading)
    return (
      <section className={styles.builder} data-route-pending>
        <p role="status">Opening custom plan…</p>
      </section>
    );
  if (planId && !opened)
    return (
      <section className={styles.builder}>
        {onCoachClose ? <button type="button" className="ghost-button" onClick={() => void closeCoach()}>← Saved plans</button> : <Link href={savedPlansHref}>← Saved plans</Link>}
        <p role="alert">{error}</p>
        <button
          className="ghost-button"
          onClick={() => {
            setLoading(true);
            setError(undefined);
            setRetry((n) => n + 1);
          }}
        >
          Retry opening plan
        </button>
      </section>
    );
  if (selecting)
    return (
      <TrainingLibrary
        accessToken={accessToken}
        initialQuery={selecting.query}
        canAdd={canAdd(selecting.sessionId)}
        onBack={() => setSelecting(undefined)}
        onSelectCommunity={(entry) => {
          if (!canAdd(selecting.sessionId)) return;
          const id = crypto.randomUUID();
          sessionChange(selecting.sessionId, s => s.exercises.length >= 100 ? s : ({ ...s, exercises: [...s.exercises, { id, origin: 'manual', exercise_type: null, name: entry.name,
            equipment: entry.equipment, notes: entry.notes, media_id: null, community_id: null, community_revision_id: entry.revision_id,
            incomplete: true, dose: blankDose(), coaching_cues: null, stop_rules: null }] }));
          noteAdded(selecting.sessionId, id);
          setSelecting(undefined);
        }}
        onSelect={(entry) => {
          if (addLibrary(selecting.sessionId, entry)) setSelecting(undefined);
        }}
      />
    );
  return (
    <section className={styles.builder} aria-label="Custom training plan builder">
      <header className={styles.header}>
        {onCoachClose ? <button type="button" className="ghost-button" onClick={() => void closeCoach()}>← Saved plans</button> : <Link href={savedPlansHref}>← Saved plans</Link>}
        <div className={styles.row}>
          <h1 ref={heading} tabIndex={-1}>
            {draft.name}
          </h1>
          <span className={styles.badge}>{dirty ? "Unsaved" : draft.id ? "Saved" : "New"}</span>
        </div>
        <p className={`muted ${styles.scheduleHint}`}>{isDated ? 'Each date has its own workout.' : 'Repeats every week. Switch to Calendar to vary weeks.'}</p>
        <div className={styles.historyToolbar} data-version={historyVersion}>
          <button type="button" className="ghost-button" disabled={pending || sheetDirty || !undoStack.current.length} onClick={() => travelHistory('undo')} aria-label="Undo" title="Undo last plan change"><span aria-hidden="true">↶</span><span className={styles.historyLabel}> Undo</span></button>
          <button type="button" className="ghost-button" disabled={pending || sheetDirty || !redoStack.current.length} onClick={() => travelHistory('redo')} aria-label="Redo" title="Redo last plan change"><span aria-hidden="true">↷</span><span className={styles.historyLabel}> Redo</span></button>
          <span>{isDated ? formatAppDateRange(draft.start_date, draft.end_date) : 'Repeats every week'}</span>
        </div>
      </header>
      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}
      {importedDraft && !draft.id ? <div className={styles.importNote} ref={importNoteRef}>
        <p role="status">Imported from {importedDraft.name}. {importedDraft.flags.length ? "Tick each thing to check once it's fixed or checked, then save." : "Check each session, then save."}</p>
        {importedDraft.flags.length ? <ImportFlags flags={importedDraft.flags} open checked={checkedFlags} onOpen={openFlag}
          onCheck={(index, done) => { setCheckedFlags(current => done ? [...current, index] : current.filter(at => at !== index)); setError(undefined); }} /> : null}
      </div> : null}
      {message ? (
        <p role="status">
          {message} {onCoachClose ? <button type="button" className="ghost-button" onClick={() => void closeCoach()}>View saved plans</button> : <Link href={savedPlansHref}>View saved plans</Link>}
        </p>
      ) : null}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <fieldset className={styles.editor} disabled={pending}>
          <label>
            <span>Plan name</span>
            <input
              required
              maxLength={100}
              value={draft.name}
              onChange={(e) => { const name = e.target.value; change(current => ({ ...current, name })); }}
            />
          </label>
          <div className={styles.viewToggle} role="group" aria-label="Plan view">
            <button type="button" aria-pressed={isDated} onClick={() => selectScheduleMode('dated')}>Calendar</button>
            <button type="button" aria-pressed={!isDated} onClick={() => selectScheduleMode('weekly')}>Week</button>
          </div>
          <div className={styles.scheduleSettings}>
            {isDated ? <><label>Start date<input type="date" value={startInput ?? draft.start_date!} onChange={event => setStartInput(event.target.value)} onBlur={() => { const next = startInput; setStartInput(undefined); if (next && next !== draft.start_date) resizeDates(next, Math.ceil((Date.parse(draft.end_date!) - Date.parse(draft.start_date!) + 86400000) / 604800000)); }} /></label><label>Weeks<input aria-label="Plan duration in weeks" type="number" min={1} max={12} value={Math.ceil((Date.parse(draft.end_date!) - Date.parse(draft.start_date!) + 86400000) / 604800000)} onChange={event => resizeDates(draft.start_date!, Number(event.target.value))} /></label></> : null}
          </div>
          <div className={isDated ? styles.calendarEditorLayout : styles.weeklyEditorLayout}>
          <div ref={calendarRef}>
          {isDated ? <CustomPlanCalendar exerciseCopy={exerciseCopy ? nameOf(exerciseCopy) : undefined} onPasteExercise={choosePasteDay} exerciseMoving={Boolean(exerciseCutSource)} onStopExerciseCopy={() => { setExerciseCopy(undefined); setExerciseCutSource(undefined); }} fightDate={scheduledFightDate} sessions={draft.sessions} start={draft.start_date!} end={draft.end_date!} selected={selectedDate} onSelect={setSelectedDate} onAdd={addDatedSession} onMove={moveSession} onCopy={(ids, days) => copyTo(draft.sessions.filter(s => ids.includes(s.id)), days)} onMoveMany={moveTo} copying={copying ? { ...copying, sessions: draft.sessions.filter(s => copying.ids.includes(s.id)) } : undefined} onStartCopying={(ids, move) => { setExerciseCopy(undefined); setExerciseCutSource(undefined); setCopying({ ids, move }); }} onStopCopying={() => setCopying(null)}
            onOpen={id => openSession(draft.sessions.find(s => s.id === id)!)} onDuplicateWeek={() => { try { change(current => ({ ...current, sessions: duplicateCalendarWeek(current.sessions, selectedDate, current.start_date!, current.end_date!) })); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to copy week.'); } }} /> : <>{scheduledFightDate ? <p className={styles.calendarFightDate}>Fight day · {formatAppDate(scheduledFightDate)}</p> : null}<div className={styles.calendar} role="group" aria-label="Weekly training calendar">
            {weekdays.map(day => {
              const sessions = draft.sessions.filter(s => s.weekday === day);
              const label = sessions.length ? sessions.map(s => s.name).join(" / ") : "Rest";
              return <button key={day} type="button" className={styles.dayTile} aria-label={exerciseCopy ? `Paste ${nameOf(exerciseCopy)} on ${day}` : copying?.move ? `Move session to ${day}` : `${day}, ${label}`}
                aria-pressed={day === selectedDay} data-copying={Boolean(exerciseCopy || copying?.move)} onClick={() => exerciseCopy ? choosePasteDay(day) : copying?.move ? moveWeeklySession(day) : setChosenDay(day)}>
                <strong>{day.slice(0, 3)}</strong>
                <small>{label}</small>
                <span className={styles.dayDot} data-type={sessions.length ? dayType(sessions) : "none"}
                  data-attention={sessions.some(s => issueSessions.has(s.id)) ? "true" : undefined} aria-hidden="true" />
              </button>;
            })}
          </div>{exerciseCopy || copying?.move ? <div className={styles.copyBanner} role="status"><p>{exerciseCopy ? `${exerciseCutSource ? 'Moving' : 'Copying'} ${nameOf(exerciseCopy)}` : 'Moving session'} · Choose a day.</p><button type="button" onClick={() => { setExerciseCopy(undefined); setExerciseCutSource(undefined); setCopying(null); }}>Done</button></div> : null}</>}
          </div>
          <div className={styles.sessionPanel}>
          <div className={styles.dayView}>
            {daySessions.length ? daySessions.map(renderSession) : (
              <div className={styles.emptyDay}>
                <small className={styles.weekday}>{selectedDay}</small>
                <h2>Rest day</h2>
                <p className="muted">{REST_DAY_REMINDER}</p>
                <button type="button" className={`secondary-button ${styles.addSession}`} aria-label={`Add session on ${selectedDay}`}
                  disabled={draft.sessions.length >= sessionLimit}
                  onClick={() => addSessionOnDay(selectedDay)}>
                  <PlusIcon /> Add session
                </button>
              </div>
            )}
          </div>
          {daySessions.length ? (
            <button type="button" className={`secondary-button ${styles.addSession}`} aria-label={`Add another session on ${selectedDay}`}
              disabled={draft.sessions.length >= sessionLimit} onClick={() => addSessionOnDay(selectedDay)}>
              <PlusIcon /> Add another session on {selectedDay}
            </button>
          ) : null}
          </div></div>
          {/* Only older drafts can hold a session without a day; new sessions always start on the selected day. */}
          {unassigned.length ? <div className={styles.dayView} role="group" aria-label="Sessions without a day">
            {unassigned.map(renderSession)}
          </div> : null}
          <div className={styles.saveBar} ref={saveBar}>
            <p className={styles.saveStatus} role="status" data-attention={issues.length || incompleteCount ? "true" : undefined}>
              {incompleteCount ? `${incompleteCount} ${incompleteCount === 1 ? "prescription" : "prescriptions"} incomplete` : issues.length ? `${issues.length} ${issues.length === 1 ? "thing" : "things"} to review` : coachFighterId ? "Ready to assign" : "Ready to activate"}
              {dirty ? <span className={styles.unsavedStatus}> · Unsaved<span className={styles.unsavedDetail}> changes</span></span> : ""}
            </p>
            {issues.length || incompleteCount ? <button type="button" className={`ghost-button ${styles.fixButton}`} aria-haspopup="dialog" onClick={() => setReviewOpen(true)}>Review</button> : null}
            <div className={styles.saveActions}>
              {/* One primary at a time: Save draft until the plan can be published, then Save & view plan. */}
              <button type="submit" className={publishable && !coachFighterId ? "ghost-button" : "cta"} disabled={!draftValid}>
                {pending ? "Saving…" : "Save draft"}
              </button>
              {!coachFighterId && publishable ? <button type="button" className="cta" onClick={() => void save(true)}>
                Save &amp; view plan
              </button> : null}
            </div>
          </div>
        </fieldset>
      </form>
      {draft.id && !coachFighterId ? (
        <button
          className={`ghost-button ${styles.dangerButton}`}
          disabled={pending}
          onClick={async () => {
            if (!window.confirm("Delete this custom plan?")) return;
            setPending(true);
            try {
              await deleteCustomPlan(accessToken, draft.id!);
              setDirty(false);
              router.push("/plans#custom-plans");
            } catch (e) {
              setError(customPlanError(e, "Unable to delete plan."));
              setPending(false);
            }
          }}
        >
          Delete plan
        </button>
      ) : null}
      {reviewOpen ? <InjuryDetailSheet title="Review plan" panelClassName={styles.reviewPanel} inactive={Boolean(editing || sessionEdit || adding)} onClose={() => setReviewOpen(false)}>
        <div className={`${styles.sheet} ${styles.reviewContent}`}>
          <p>Save incomplete prescriptions as a draft. Complete them before activating your plan.</p>
          {reviewSessions.map(({ session, incomplete, sessionIssues }) => {
            return <section key={session.id} aria-label={`Review ${session.name}`}>
              <h3>{session.name}</h3>
              <p className={styles.badge}>{session.scheduled_date ? formatAppDate(session.scheduled_date) : session.weekday ?? "No day set"}</p>
              <ul className={styles.reviewIssues}>
                {incomplete.map(exercise => <li key={exercise.id}><span>{nameOf(exercise)}</span><button type="button" className="ghost-button" aria-label={`Fix ${nameOf(exercise)} in ${session.name}`} onClick={() => edit(session.id, exercise)}>Fix</button></li>)}
                {sessionIssues.map(issue => <li key={issue.message}><span>{issue.message}</span><button type="button" className="ghost-button" aria-label={`Fix ${issue.message}`} onClick={() => openSession(session)}>Fix</button></li>)}
              </ul>
            </section>;
          })}
          {issues.filter(issue => !issue.sessionId).map(issue => <p key={issue.message}>{issue.message}</p>)}
          {!issues.length && !incompleteCount ? <p role="status">All prescriptions complete. Your plan is ready.</p> : null}
          <button type="button" className="ghost-button" onClick={() => setReviewOpen(false)}>Back to programme</button>
        </div>
      </InjuryDetailSheet> : null}
      {pasteDay && exerciseCopy ? <InjuryDetailSheet title="Choose a session" onClose={() => setPasteDay(undefined)}><div className={styles.sheet}>
        <p>Paste {nameOf(exerciseCopy)} on {isDated ? formatAppDate(pasteDay) : pasteDay}.</p>
        {draft.sessions.filter(s => (isDated ? s.scheduled_date === pasteDay : s.weekday === pasteDay) && s.session_type !== 'rest').map(session => <button key={session.id} type="button" className="ghost-button" disabled={session.exercises.length >= 100} onClick={() => pasteExercise(pasteDay, session.id)}>{session.name} · {session.exercises.length} exercises</button>)}
        <button type="button" className="ghost-button" disabled={draft.sessions.length >= sessionLimit || (isDated && draft.sessions.filter(s => s.scheduled_date && calendarMonday(s.scheduled_date) === calendarMonday(pasteDay)).length >= 28)} onClick={() => pasteExercise(pasteDay)}>Create new session</button>
      </div></InjuryDetailSheet> : null}
      {sessionEdit ? (
        <InjuryDetailSheet
          title={sessionFresh ? "New session" : "Edit session"}
          panelClassName={styles.editorPanel}
          onClose={closeSession}
        >
          <form
            className={`${styles.sheet} ${styles.sessionSheet}`}
            onSubmit={(e) => {
              e.preventDefault();
              if (sessionFresh) change(current => ({ ...current, sessions: [...current.sessions, sessionEdit] }));
              else sessionChange(sessionEdit.id, () => sessionEdit);
              // Follow the session to its day so it never disappears from view.
              if (sessionEdit.scheduled_date) setSelectedDate(sessionEdit.scheduled_date);
              if (sessionEdit.weekday) setChosenDay(sessionEdit.weekday);
              setSessionEdit(undefined);
              // A new workout goes straight on to its exercises; closing the sheet still leaves it empty.
              if (sessionFresh && EXERCISE_TYPES.includes(sessionEdit.session_type ?? "mixed")) setAdding({ sessionId: sessionEdit.id, added: [] });
            }}
          >
            {/* The type comes first: one tap, and the name follows it until the athlete renames the session. */}
            <div className={styles.typeField}>
              <span id="custom-session-type">Session type</span>
              <div className={styles.typeChips} role="group" aria-labelledby="custom-session-type">
                {SESSION_TYPES.map(([type, label]) => <button key={type} type="button" aria-pressed={(sessionEdit.session_type ?? "mixed") === type}
                  onClick={() => setSessionEdit(prepareCustomSession({ ...sessionEdit, session_type: type }, onboarding, sessionEdit.session_type ?? "mixed"))}>
                  {label}
                </button>)}
              </div>
            </div>
            <label>
              <span>Session name</span>
              <input
                required
                maxLength={100}
                value={sessionEdit.name}
                onChange={(e) => setSessionEdit({ ...sessionEdit, name: e.target.value })}
              />
            </label>
            {isDated ? <><label>Date<input aria-label="Session date" type="date" min={draft.start_date!} max={draft.end_date!} required value={sessionEdit.scheduled_date ?? selectedDate} onChange={event => { const day = event.target.value; if (day) setSessionEdit({ ...sessionEdit, scheduled_date: day, weekday: calendarWeekday(day) }); }} /></label>
            {!sessionFresh ? <div className={styles.copyToRow}><label>Copy to<input aria-label="Copy session to date" type="date" min={draft.start_date!} max={draft.end_date!} value={copyToDay} onChange={event => setCopyToDay(event.target.value)} /></label>
              <button type="button" className="ghost-button" disabled={!copyToDay || draft.sessions.length >= sessionLimit} onClick={() => { if (copyTo([sessionEdit], [copyToDay])) { setCopyNote(`Copied to ${formatAppDate(copyToDay)}.`); setCopyToDay(''); } }}>Copy</button>
              <p role="status">{copyNote}</p></div> : null}</> : <>
            <label>
              <span>Day</span>
              <select
                aria-label="Day"
                value={sessionEdit.weekday ?? ""}
                onChange={(e) =>
                  setSessionEdit({
                    ...sessionEdit,
                    weekday: (e.target.value as Session["weekday"]) || null,
                  })
                }
              >
                {sessionEdit.weekday ? null : <option value="" disabled>Choose a day</option>}
                {weekdays.map((day) => (
                  <option key={day}>{day}</option>
                ))}
              </select>
            </label>
            </>}
            <label className={styles.wideField}>
              <span>Session notes <span className="muted">(optional)</span></span>
              <textarea
                rows={3}
                maxLength={2000}
                value={sessionEdit.notes ?? ""}
                placeholder="Focus or instructions for the whole session"
                onChange={(e) => setSessionEdit({ ...sessionEdit, notes: e.target.value || null })}
              />
            </label>
            <button className="cta" disabled={!sessionEdit.weekday}>{sessionFresh ? "Add session" : "Done"}</button>
            {sessionFresh ? null : <button
              type="button"
              className={`ghost-button ${styles.dangerButton}`}
              onClick={() => {
                const original = draft.sessions.find((s) => s.id === sessionEdit.id);
                if (original && removeSession(original)) setSessionEdit(undefined);
              }}
            >
              Remove session
            </button>}
          </form>
        </InjuryDetailSheet>
      ) : null}
      {addingSession && adding ? (
        <InjuryDetailSheet title={`Add to ${addingSession.name}`} panelClassName={styles.addPanel} onClose={() => setAdding(undefined)}>
          <div className={styles.sheet}>
            <CustomExercisePicker accessToken={accessToken} full={addingSession.exercises.length >= 100} initialQuery={adding.query}
              onAdd={entry => addLibrary(adding.sessionId, entry)}
              onPreview={entry => setPreview(entry)}
              onCreate={name => createManual(adding.sessionId, name)}
              onBrowse={query => {
                setAdding(current => current?.sessionId === adding.sessionId ? { ...current, query } : current);
                setSelecting({ sessionId: adding.sessionId, query });
              }} />
            {addedHere.length ? <section className={styles.added} aria-labelledby="custom-added-heading">
              <h3 id="custom-added-heading">Added · {addedHere.length}
                {/* Collapsed rows still say which doses are missing, so none get lost. */}
                {addedHere.some(e => exerciseIncomplete(e)) ? <span className={styles.addedTodo}> · {addedHere.filter(e => exerciseIncomplete(e)).length} to finish</span> : null}
              </h3>
              <ol>
                {addedHere.map(exercise => {
                  const open = openAdded === exercise.id;
                  return <li key={exercise.id} className={styles.addedRow} role="group" aria-label={nameOf(exercise)}
                    data-attention={exerciseIncomplete(exercise) ? "true" : undefined}>
                    <button type="button" className={styles.addedHead} aria-expanded={open}
                      onClick={() => setOpenAdded(open ? undefined : exercise.id)}>
                      <span className={styles.addedText}>
                        <strong>{nameOf(exercise)}</strong>
                        <small>{exerciseIncomplete(exercise) && /^Set /.test(doseSummary(exercise.dose)) ? "Prescription not set" : doseSummary(exercise.dose, nameOf(exercise)) || "No dose needed"}</small>
                      </span>
                      <span className={styles.chevron} aria-hidden="true" />
                    </button>
                    {open ? <div className={styles.addedBody}>
                      <CustomDoseEditor essential dose={exercise.dose}
                        movement={exercise.origin === "catalogue" ? exercise.snapshot : null}
                        past={exercise.origin === "catalogue" && exercise.snapshot.exercise_key ? past?.get(exercise.snapshot.exercise_key) : null}
                        onChange={dose => setDose(adding.sessionId, exercise.id, dose)} />
                      <div className={styles.addedFoot}>
                        <small>{exercise.origin === "manual" ? "Your exercise"
                          : prefills[exercise.id] === undefined ? "Checking the library and your history…"
                          : prefills[exercise.id] ?? "No set format. Enter your own."}</small>
                        <button type="button" className={styles.textButton} aria-label={`More options for ${nameOf(exercise)}`}
                          onClick={() => edit(adding.sessionId, exercise)}>More</button>
                        <button type="button" className={styles.textButton} aria-label={`Remove ${nameOf(exercise)}`}
                          onClick={() => sessionChange(adding.sessionId, s => ({ ...s, exercises: s.exercises.filter(e => e.id !== exercise.id) }))}>
                          Remove
                        </button>
                      </div>
                    </div> : null}
                  </li>;
                })}
              </ol>
            </section> : null}
            <button type="button" className="cta" onClick={() => setAdding(undefined)}>Done</button>
          </div>
        </InjuryDetailSheet>
      ) : null}
      {editing ? (
        <InjuryDetailSheet
          title={editing.fresh ? "Create my exercise" : nameOf(editing.exercise)}
          panelClassName={`${styles.editorPanel} ${styles.exercisePanel}`}
          onClose={closeEditing}
        >
          <form
            className={`${styles.sheet} ${styles.exerciseEditor}`}
            // A folded field the browser rejects opens its fold, so validation is never hidden.
            onInvalidCapture={(event) => (event.target as Element).closest("details")?.setAttribute("open", "")}
            onSubmit={(event) => {
              event.preventDefault();
              const { sessionId, exercise, fresh, position, original } = editing;
              if (fresh && !canAdd(sessionId)) return;
              if (JSON.stringify(exercise) !== JSON.stringify(original)) markTouched(exercise.id);
              sessionChange(sessionId, (s) => {
                if (fresh) return s.exercises.length >= 100 ? s : { ...s, exercises: [...s.exercises, exercise] };
                const exercises = s.exercises.filter((e) => e.id !== exercise.id);
                exercises.splice(position, 0, exercise);
                return { ...s, exercises };
              });
              if (fresh) noteAdded(sessionId, exercise.id);
              setEditing(undefined);
            }}
          >
            <section className={styles.exerciseContext} aria-label="Exercise details"><h3>Exercise details</h3>
            {editing.exercise.origin === "manual" ? (
              <>
                <label>
                  <span>Exercise name</span>
                  <input
                    required
                    maxLength={100}
                    value={editing.exercise.name}
                    onChange={(e) => {
                      const name = e.target.value;
                      setEditing(current => current?.exercise.origin === "manual"
                        ? { ...current, exercise: { ...current.exercise, name } } : current);
                    }}
                  />
                </label>
                <label><span>Exercise type (optional)</span>
                  <select value={editing.exercise.exercise_type ?? ""} onChange={event => {
                    const exercise_type = (event.target.value || null) as ManualExercise["exercise_type"];
                    setEditing(current => current?.exercise.origin === "manual"
                      ? { ...current, exercise: { ...current.exercise, exercise_type } } : current);
                  }}>
                    <option value="">Not classified</option><option value="strength">Strength</option>
                    <option value="bodyweight">Bodyweight strength</option><option value="power">Jumps / power</option>
                    <option value="other">Other</option>
                  </select>
                </label>
                {/* Video and sharing are optional extras: folded, with their state in the summary. */}
                {!coachFighterId ? <details className={styles.advanced}>
                  <summary><span>Video &amp; sharing</span>
                    <small>{[editing.exercise.media_id ? "Video" : null, editing.exercise.community_revision_id ? "From community" : editing.exercise.community_id ? "Shared" : "Private"].filter(Boolean).join(" · ")}</small>
                  </summary>
                  <div className={styles.groupBody}>
                {editing.exercise.community_revision_id ? <button type="button" className="ghost-button" onClick={() => setCommunityPreview(editing.exercise.origin === 'manual' ? editing.exercise.community_revision_id ?? undefined : undefined)}>View community exercise</button> :
                  <div className={styles.visibility}>
                    <p><strong>{editing.exercise.community_id ? 'Shared with community' : 'Only you can see this exercise'}</strong>
                      <span className="muted">{editing.exercise.community_id ? 'Public once an admin approves it.' : 'Sharing is optional and needs admin approval before it goes public.'}</span></p>
                    <button type="button" className="ghost-button" disabled={!editing.exercise.name.trim()} onClick={() => setCommunityOpen(true)}>{editing.exercise.community_id ? 'Manage community submission' : 'Share with community'}</button>
                  </div>}
                {editing.exercise.media_id ? <PrivateExerciseVideo token={accessToken} id={editing.exercise.media_id} name={editing.exercise.name} /> : null}
                <button type="button" className="ghost-button" onClick={() => setVideoOpen(true)}>
                  {editing.exercise.media_id ? "Replace video" : "Add YouTube demo (optional)"}
                </button>
                {editing.exercise.media_id ? <button type="button" className="ghost-button" onClick={() => setEditing((current) =>
                  current?.exercise.origin === "manual" ? { ...current, exercise: { ...current.exercise, media_id: null } } : current,
                )}>Remove video</button> : null}
                  </div>
                </details> : null}
                {editing.exercise.community_id && linkedCommunity?.id === editing.exercise.community_id && linkedCommunity.entry &&
                  communityDetailsChanged(linkedCommunity.entry, editing.exercise) ? <div role="status" className={styles.communityNotice}>
                    {linkedCommunity.entry.media_id !== editing.exercise.media_id
                      ? 'Video not included in community submission yet.' : 'Your community submission has older exercise details.'}
                    <button type="button" className="secondary-button" onClick={() => setCommunityOpen(true)}>Update community submission</button>
                  </div> : null}
                {editing.exercise.community_id && linkedCommunity?.id === editing.exercise.community_id && linkedCommunity.error
                  ? <button type="button" className="ghost-button" onClick={() => setCommunityOpen(true)}>Check community submission</button> : null}
              </>
            ) : (
              <>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() =>
                    setPreview(editing.exercise.origin === "catalogue" ? editing.exercise.snapshot : undefined)
                  }
                >
                  View library exercise
                </button>
                <button type="button" className="ghost-button" disabled={suggesting} onClick={() => void suggest()}>
                  {suggesting ? "Loading source…" : "Suggested format"}
                </button>
                {suggestionError ? <p role="alert">{suggestionError}</p> : null}
                {suggestion ? (() => {
                  // Same precedence as adding from the library: explicit source dose, else the library's format.
                  const candidate = suggestion.dose?.mode !== "text" && suggestion.dose ? suggestion.dose : suggestion.format_dose ?? suggestion.dose;
                  // A format with only a mode (no numbers or text) would just blank the fields.
                  const suggestedDose = candidate && Object.entries(candidate).some(([k, v]) => k !== "mode" && v != null) ? candidate : null;
                  return (
                  <div>
                    <p>{suggestion.original || (suggestedDose ? "The library suggests this format:" : "No library format. Enter your own dose.")}</p>
                    {suggestedDose ? (
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => {
                          if (
                            Object.entries(editing.exercise.dose).some(([k, v]) => k !== "mode" && v != null) &&
                            !window.confirm("Replace the current dose with the source suggestion?")
                          )
                            return;
                          setEditing({
                            ...editing,
                            exercise: {
                              ...editing.exercise,
                              dose: suggestedDose,
                            },
                          });
                        }}
                      >
                        {suggestion.dose ? "Use source suggestion" : "Use library format"}
                      </button>
                    ) : null}
                  </div>
                  );
                })() : null}
              </>
            )}
            </section>
            <section className={styles.exercisePrescription} aria-label="Exercise prescription"><h3>Training prescription</h3>
            {!editing.fresh && draft.sessions.find((s) => s.id === editing.sessionId)!.exercises.length > 1 ? (
              <label>
                <span>Position in session</span>
                <select
                  aria-label="Position in session"
                  value={editing.position}
                  onChange={(e) => setEditing({ ...editing, position: Number(e.target.value) })}
                >
                  {draft.sessions
                    .find((s) => s.id === editing.sessionId)!
                    .exercises.map((e, index) => (
                      <option key={e.id} value={index}>
                        {index + 1}
                      </option>
                    ))}
                </select>
              </label>
            ) : null}
            <CustomDoseEditor
              dose={editing.exercise.dose}
              movement={editing.exercise.origin === "catalogue" ? editing.exercise.snapshot : editing.exercise}
              past={past?.get(editing.exercise.origin === "catalogue" ? editing.exercise.snapshot.exercise_key ?? ""
                : `custom-name:${editing.exercise.name.trim().toLowerCase().replace(/\s+/g, " ")}`)}
              onChange={(dose) =>
                setEditing({
                  ...editing,
                  exercise: { ...editing.exercise, dose },
                })
              }
            />
            <details className={styles.advanced}>
              <summary><span>Cues &amp; stop rules</span>
                <small>{(["coaching_cues", "stop_rules"] as const).map(field => {
                  const count = (editing.exercise[field] ?? []).filter(line => line.trim()).length;
                  return count ? `${count} ${field === "coaching_cues" ? count === 1 ? "cue" : "cues" : count === 1 ? "stop rule" : "stop rules"}` : null;
                }).filter(Boolean).join(" · ")}</small>
              </summary>
              <div className={styles.groupBody}>
            {(["coaching_cues", "stop_rules"] as const).map(field => <label key={field}>
              <span>{field === "coaching_cues" ? "Cues" : "Stop rules"}</span>
              <textarea aria-label={field === "coaching_cues" ? "Cues" : "Stop rules"} rows={2} maxLength={2000}
                value={(editing.exercise[field] ?? []).join("\n")}
                onChange={event => setEditing({ ...editing, exercise: {
                  ...editing.exercise, [field]: event.target.value.split("\n"),
                } })} />
            </label>)}
              </div>
            </details>
            </section>
            <div className={styles.editorActions}>
            <button className="cta">{editing.fresh ? "Add exercise" : "Done"}</button>
            {!editing.fresh ? (
              <>
                <button
                  type="button"
                  className="ghost-button"
                  disabled={draft.sessions.find((s) => s.id === editing.sessionId)!.exercises.length >= 100}
                  onClick={() => {
                    sessionChange(editing.sessionId, (s) => ({
                      ...s,
                      exercises: [...s.exercises, { ...editing.exercise, id: crypto.randomUUID() }],
                    }));
                    setEditing(undefined);
                  }}
                >
                  Copy exercise
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => {
                    if (window.confirm("Remove this exercise?")) {
                      sessionChange(editing.sessionId, (s) => ({
                        ...s,
                        exercises: s.exercises.filter((e) => e.id !== editing.exercise.id),
                      }));
                      setEditing(undefined);
                    }
                  }}
                >
                  Remove exercise
                </button>
              </>
            ) : null}
            </div>
          </form>
        </InjuryDetailSheet>
      ) : null}
      {preview ? (
        <DrillPreview key={preview.catalogue_id} entry={preview} token={accessToken} onClose={() => setPreview(undefined)} selfDirected
          // Previewed from the add sheet, it can add; from an exercise's own editor it only shows the drill.
          onAdd={adding && !editing && canAdd(adding.sessionId) ? entry => { if (addLibrary(adding.sessionId, entry)) setPreview(undefined); } : undefined} />
      ) : null}
      {communityOpen && editing?.exercise.origin === 'manual' ? <CommunityContributionSheet key={accessToken} token={accessToken} exercise={editing.exercise} onClose={() => setCommunityOpen(false)}
        onIdentity={id => setEditing(current => current?.exercise.origin === 'manual' ? { ...current, exercise: { ...current.exercise, community_id: id } } : current)}
        onEquipment={equipment => setEditing(current => current?.exercise.origin === 'manual' ? { ...current, exercise: { ...current.exercise, equipment } } : current)} /> : null}
      {communityPreview ? <CommunityExercisePreview key={`${accessToken}:${communityPreview}`} token={accessToken} revision={communityPreview} onClose={() => setCommunityPreview(undefined)} /> : null}
      {videoOpen && editing?.exercise.origin === "manual" ? <PrivateExerciseVideoSheet
        token={accessToken} name={editing.exercise.name || "My exercise"} onClose={() => setVideoOpen(false)}
        onAttach={(media_id) => {
          setEditing((current) => current?.exercise.origin === "manual" ? {
            ...current, exercise: { ...current.exercise, media_id, community_revision_id: null },
          } : current);
          setVideoOpen(false);
          if (editingCommunityId) setCommunityOpen(true);
        }} /> : null}
    </section>
  );
}
