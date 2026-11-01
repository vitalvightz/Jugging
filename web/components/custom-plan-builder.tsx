"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { trackProductEvent, productEventIdentity, deleteCustomPlan, getCommunityExercise, getCustomDoseSuggestion, getCustomPlan, getExerciseLibrary, publishCustomPlan, saveCustomPlan } from "@/lib/api";
import type { MeResponse, StructuredBlock } from "@/lib/types";
import { ExerciseRow } from "./structured-plan-renderer";
import { DotsIcon } from "./exercise-customisation";
import { ExerciseMediaProvider } from "./exercise-demo";
import { customSchedulePrefill, customScheduleIssues, prepareCustomSession, customPlanError, sessionNames } from "@/lib/custom-plan-schedule";
import type {
  CommunityEntry,
  CustomDoseSuggestion,
  CustomExercise,
  CustomSession,
  LibraryEntry,
  ManualExercise,
} from "@/lib/api-schema.generated";
import { InjuryDetailSheet } from "./today/injury-detail-sheet";
import { DrillPreview, TrainingLibrary } from "./training-library";
import { CustomDoseEditor } from "./custom-dose-editor";
import { usePastExercises } from "./load-field";
import { pastExercisesByKey } from "@/lib/exercise-library";
import { withPastDose } from "@/lib/load-entry";
import { CommunityContributionSheet, CommunityExercisePreview, communityDetailsChanged } from "./community-exercises";
import { PrivateExerciseMediaProvider, PrivateExerciseVideo, PrivateExerciseVideoSheet } from "./private-exercise-video";
import { blankDose, doseSummary, doseIncomplete } from "@/lib/custom-plan-dose";
import styles from "./custom-plan-builder.module.css";
import { useFadeNavigate } from "@/components/fade-through";
type Exercise = (Omit<CustomExercise, "snapshot"> & { snapshot: LibraryEntry }) | ManualExercise;
type Session = Omit<CustomSession, "exercises" | "session_type"> & { exercises: Exercise[]; session_type?: CustomSession["session_type"] };
type Draft = { id?: string; name: string; sessions: Session[]; incomplete?: boolean };
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
function orderedDraft(draft: Draft): Draft {
  const day = (session: Session) => session.weekday ? weekdays.indexOf(session.weekday) : 7;
  return { ...draft, sessions: [...draft.sessions].sort((a, b) => day(a) - day(b)) };
}
const nameOf = (e: Exercise) => (e.origin === "manual" ? e.name : e.snapshot.name);
function draftBlock(exercise: Exercise, session: Session): StructuredBlock {
  const dose = exercise.dose;
  const snapshot = exercise.origin === "catalogue" ? exercise.snapshot : null;
  const type = snapshot?.content_type;
  const blockType = ({ conditioning: "conditioning", skill: "skill", strength: "strength", power: "plyometric_power", speed: "speed" } as Record<string, string>)[type ?? ""]
    ?? ({ strength_power: "strength", conditioning: "conditioning", skill: "skill", sparring: "sparring", recovery: "cooldown_recovery" } as Record<string, string>)[session.session_type ?? "mixed"]
    ?? "accessory";
  const weight = dose.load_or_intensity?.trim().match(/^(\d+(?:\.\d+)?)\s*(kg|lb|lbs)$/i);
  return {
    block_id: exercise.id, display_name: nameOf(exercise), prescription_source: "athlete",
    block_type: blockType,
    exercise_key: snapshot?.exercise_key, catalogue_id: exercise.origin === "catalogue" ? exercise.catalogue_id : null,
    private_media_id: exercise.origin === "manual" ? exercise.media_id : null,
    community_revision_id: exercise.origin === "manual" ? exercise.community_revision_id : null,
    athlete_dose: dose, prescription_text: doseSummary(dose),
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
export function CustomPlanBuilder({ accessToken, planId, addSessionDay, onboarding = null }: { accessToken: string; planId?: string; addSessionDay?: string | null; onboarding?: MeResponse | null }) {
  const router = useRouter();
  const fadeNavigate = useFadeNavigate();
  const [draft, setDraft] = useState<Draft>(() => ({
    name: "My Training Plan",
    sessions: planId ? [] : customSchedulePrefill(onboarding),
  }));
  const [loading, setLoading] = useState(!!planId),
    [retry, setRetry] = useState(0),
    [pending, setPending] = useState(false),
    [dirty, setDirty] = useState(false);
  const [opened, setOpened] = useState(!planId);
  const [chosenDay, setChosenDay] = useState<Weekday>();
  const [adding, setAdding] = useState<string>();
  const [openExercise, setOpenExercise] = useState<string | null>(null);
  const [error, setError] = useState<string>(),
    [message, setMessage] = useState<string>();
  const [selecting, setSelecting] = useState<string>(),
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
    [sessionFresh, setSessionFresh] = useState(false),
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
  // Read only once an exercise is open, for last time's weight.
  const past = usePastExercises(editing ? accessToken : null);
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
      JSON.stringify(sessionEdit) !== JSON.stringify(draft.sessions.find((s) => s.id === sessionEdit.id)),
    );
  const unsaved = dirty || sheetDirty;
  const releaseGuard = useRef<() => Promise<void>>(async () => {});
  const loaded = useRef(false),
    heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!planId || loaded.current) return;
    let active = true;
    void getCustomPlan(accessToken, planId)
      .then((plan) => {
        if (active) {
          loaded.current = true;
          setOpened(true);
          void productEventIdentity(planId).then(flow => trackProductEvent(accessToken, "plan_editor_opened", "plans", undefined, flow)).catch(() => {});
          setError(undefined);
          const repaired = orderedDraft({ ...plan, sessions: plan.sessions.map(s => prepareCustomSession(s, onboarding)) });
          setDraft(repaired);
          const day = weekdays.find(value => value.slice(0, 3).toLowerCase() === addSessionDay?.toLowerCase());
          if (day) {
            setChosenDay(day);
            setSessionFresh(true);
            setSessionEdit({ id: crypto.randomUUID(), name: "Training", weekday: day, session_type: "mixed", exercises: [] });
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
  }, [accessToken, planId, retry, onboarding, addSessionDay]);
  useEffect(() => {
    if (!loading && loaded.current) heading.current?.focus({ preventScroll: true });
  }, [loading]);
  useEffect(() => {
    if (!unsaved) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    // Keep a same-URL entry while dirty so Back can confirm without losing edits.
    const state = { ...window.history.state, customDraftGuard: true };
    window.history.pushState(state, "", window.location.href);
    const back = () => {
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
  function change(next: Draft) {
    setDraft(orderedDraft(next));
    setDirty(true);
    setMessage(undefined);
  }
  function sessionChange(id: string, update: (s: Session) => Session) {
    change({
      ...draft,
      sessions: draft.sessions.map((s) => (s.id === id ? update(s) : s)),
    });
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
    setSessionFresh(true);
    setSessionEdit({
      id: crypto.randomUUID(),
      name: "Training",
      weekday,
      session_type: "mixed",
      exercises: [],
    });
  }
  function openSession(session: Session) {
    setSessionFresh(false);
    setSessionEdit(session);
  }
  function removeSession(session: Session) {
    if (!window.confirm(`Remove ${session.name}${session.exercises.length ? " and its exercises" : ""}?`)) return false;
    change({ ...draft, sessions: draft.sessions.filter((s) => s.id !== session.id) });
    return true;
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
  const draftValid = Boolean(draft.name.trim()) && draft.sessions.every(s => s.name.trim() && s.exercises.every(e => nameOf(e).trim()));
  const publishable = draftValid && issues.length === 0;
  const selectedDay = chosenDay ?? firstScheduledDay(draft.sessions);
  const daySessions = draft.sessions.filter(s => s.weekday === selectedDay);
  const unassigned = draft.sessions.filter(s => !s.weekday);
  const issueSessions = new Set(issues.map(issue => issue.sessionId).filter(Boolean));
  function fixIssue(sessionId: string) {
    const session = draft.sessions.find(s => s.id === sessionId)!;
    if (session.weekday) setChosenDay(session.weekday);
    const exercise = session.exercises.find(e => doseIncomplete(e.dose));
    if (exercise) edit(session.id, exercise); else openSession(session);
  }
  function createManual(sessionId: string) {
    edit(sessionId, {
      id: crypto.randomUUID(), origin: "manual", name: "", equipment: null, notes: null, media_id: null,
      community_id: null, community_revision_id: null, dose: blankDose(), incomplete: true,
      coaching_cues: null, stop_rules: null,
    }, true);
  }
  function renderSession(s: Session) {
    const i = draft.sessions.indexOf(s);
    return (
      <section key={s.id} className={`sp-session ${styles.session}`} aria-label={`Session ${i + 1}`}>
        <div className="sp-session-head">
          <div>
            <small className={styles.weekday} data-attention={s.weekday ? undefined : "true"}>{s.weekday ?? "No day set"}</small>
            <h2 className="sp-session-title">{s.name}</h2>
          </div>
          <div className={styles.sessionActions}>
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
        <div className="sp-session-meta"><span className="sp-tag">{sessionNames[s.session_type ?? "mixed"]}</span></div>
        {s.session_type !== "rest" ? <PrivateExerciseMediaProvider token={accessToken}>
          <ExerciseMediaProvider media={Object.fromEntries(s.exercises.flatMap(e => e.origin === "catalogue" && e.snapshot.media && e.snapshot.exercise_key
            ? [[`exercise:${e.snapshot.exercise_key}`, e.snapshot.media]] : []))}>
            <ol className={`sp-blocks ex-rows ${styles.exercises}`}>
              {s.exercises.map((e) => (
                <li key={e.id}>
                  <ExerciseRow block={draftBlock(e, s)} open={openExercise === e.id}
                    onToggle={() => setOpenExercise(current => current === e.id ? null : e.id)} actions={
                      <button type="button" className={`${styles.editButton} ${styles.cardAction}`}
                        data-exercise-actions
                        aria-label={`Edit dose for ${nameOf(e)}`} title="Edit exercise" onClick={() => edit(s.id, e)}>
                        <DotsIcon />
                      </button>
                    } />
                </li>
              ))}
            </ol></ExerciseMediaProvider></PrivateExerciseMediaProvider> : <p className="muted">{REST_DAY_REMINDER}</p>}
        {!s.exercises.length && s.session_type !== "rest" ? <p className="muted">{s.session_type === "recovery" ? REST_DAY_REMINDER : ["sparring", "skill"].includes(s.session_type ?? "mixed") ? "Coach-led day — Add editable rounds if useful." : "Workout needs exercises."}</p> : null}
        {s.session_type === "recovery" && s.exercises.length ? <p className="muted">Optional recovery work. No completion required.</p> : null}
        {s.session_type !== "rest" ? (
          <button type="button" className={styles.addExercise} aria-label={`Add exercise to ${s.name}`}
            disabled={s.exercises.length >= 100} onClick={() => setAdding(s.id)}>
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
  async function save(viewPlan = false) {
    if (!draftValid || (viewPlan && !publishable)) return;
    setPending(true);
    setError(undefined);
    try {
      const saved = await saveCustomPlan(
        accessToken,
        {
          name: draft.name,
          sessions: draft.sessions.map((s) => ({
            id: s.id,
            name: s.name,
            weekday: s.weekday,
            session_type: s.session_type ?? "mixed",
            exercises: s.exercises.map((e) =>
              e.origin === "manual"
                ? {
                    id: e.id,
                    origin: e.origin,
                    name: e.name,
                    equipment: e.equipment,
                    notes: e.notes,
                    media_id: e.media_id,
                    community_id: e.community_id,
                    community_revision_id: e.community_revision_id,
                    dose: e.dose,
                    coaching_cues: e.coaching_cues,
                    stop_rules: e.stop_rules,
                  }
                : {
                    id: e.id,
                    origin: "catalogue" as const,
                    catalogue_id: e.catalogue_id,
                    dose: e.dose,
                    coaching_cues: e.coaching_cues,
                    stop_rules: e.stop_rules,
                  },
            ),
          })),
        },
        draft.id,
      );
      if (!mounted.current) return;
      await releaseGuard.current();
      setDraft(orderedDraft(saved));
      setOpened(true);
      setDirty(false);
      setMessage(saved.incomplete ? "Draft saved. Finish the workouts listed below when ready." : "Plan saved. View your plan to activate it.");
      window.history.replaceState(window.history.state, "", `/plans/custom?plan=${saved.id}`);
      loaded.current = true;
      if (viewPlan) {
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
        <Link href="/plans#custom-plans">← Saved plans</Link>
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
        onBack={() => setSelecting(undefined)}
        onSelectCommunity={(entry) => {
          sessionChange(selecting, s => ({ ...s, exercises: [...s.exercises, { id: crypto.randomUUID(), origin: 'manual', name: entry.name,
            equipment: entry.equipment, notes: entry.notes, media_id: null, community_id: null, community_revision_id: entry.revision_id,
            incomplete: true, dose: blankDose(), coaching_cues: null, stop_rules: null }] }));
          setSelecting(undefined);
        }}
        onSelect={(entry) => {
          if (entry.category !== "training") return;
          const exerciseId = crypto.randomUUID();
          sessionChange(selecting, (s) => ({
            ...s,
            exercises: [
              ...s.exercises,
              {
                id: exerciseId,
                origin: "catalogue",
                catalogue_id: entry.catalogue_id,
                snapshot: entry,
                incomplete: true,
                coaching_cues: entry.guidance.Cues ?? null, stop_rules: entry.guidance.Stop ?? null,
                dose: blankDose(),
              },
            ],
          }));
          setSelecting(undefined);
          // The library's format first; the athlete's last session fills what it leaves open.
          void Promise.all([
            getCustomDoseSuggestion(accessToken, entry.catalogue_id).catch(() => null),
            pastExercisesByKey(accessToken, getExerciseLibrary),
          ]).then(([result, history]) => {
            const format = result?.dose?.mode !== "text" && result?.dose ? result.dose : result?.format_dose;
            const suggested = withPastDose(format ?? blankDose(), entry.exercise_key ? history.get(entry.exercise_key) : null);
            if (JSON.stringify(suggested) === JSON.stringify(blankDose())) return;
            setDraft(current => ({ ...current, sessions: current.sessions.map(session => ({ ...session,
              exercises: session.exercises.map(exercise => exercise.id === exerciseId && doseIncomplete(exercise.dose)
                && JSON.stringify(exercise.dose) === JSON.stringify(blankDose())
                ? { ...exercise, dose: suggested, incomplete: doseIncomplete(suggested) } : exercise),
            })) }));
          }).catch(() => undefined);
        }}
      />
    );
  return (
    <section className={styles.builder} aria-label="Custom training plan builder">
      <header className={styles.header}>
        <Link href="/plans#custom-plans">← Saved plans</Link>
        <div className={styles.row}>
          <h1 ref={heading} tabIndex={-1}>
            {draft.name}
          </h1>
          <span className={styles.badge}>{dirty ? "Unsaved" : draft.id ? "Saved" : "New"}</span>
        </div>
        <p className="muted">Choose each day’s work. Unselected days are rest.</p>
      </header>
      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}
      {message ? (
        <p role="status">
          {message} <Link href="/plans#custom-plans">View saved plans</Link>
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
              onChange={(e) => change({ ...draft, name: e.target.value })}
            />
          </label>
          <div className={styles.calendar} role="group" aria-label="Weekly training calendar">
            {weekdays.map(day => {
              const sessions = draft.sessions.filter(s => s.weekday === day);
              const label = sessions.length ? sessions.map(s => s.name).join(" / ") : "Rest";
              return <button key={day} type="button" className={styles.dayTile} aria-label={`${day}, ${label}`}
                aria-pressed={day === selectedDay} onClick={() => setChosenDay(day)}>
                <strong>{day.slice(0, 3)}</strong>
                <small>{label}</small>
                <span className={styles.dayDot} data-type={sessions.length ? dayType(sessions) : "none"}
                  data-attention={sessions.some(s => issueSessions.has(s.id)) ? "true" : undefined} aria-hidden="true" />
              </button>;
            })}
          </div>
          <div className={styles.dayView}>
            {daySessions.length ? daySessions.map(renderSession) : (
              <div className={styles.emptyDay}>
                <small className={styles.weekday}>{selectedDay}</small>
                <h2>Rest day</h2>
                <p className="muted">{REST_DAY_REMINDER}</p>
                <button type="button" className={`secondary-button ${styles.addSession}`} aria-label={`Add session on ${selectedDay}`}
                  disabled={draft.sessions.length >= 28}
                  onClick={() => addSessionOnDay(selectedDay)}>
                  <PlusIcon /> Add session
                </button>
              </div>
            )}
          </div>
          {daySessions.length ? (
            <button type="button" className={`secondary-button ${styles.addSession}`} aria-label={`Add another session on ${selectedDay}`}
              disabled={draft.sessions.length >= 28} onClick={() => addSessionOnDay(selectedDay)}>
              <PlusIcon /> Add another session on {selectedDay}
            </button>
          ) : null}
          {/* Only older drafts can hold a session without a day; new sessions always start on the selected day. */}
          {unassigned.length ? <div className={styles.dayView} role="group" aria-label="Sessions without a day">
            {unassigned.map(renderSession)}
          </div> : null}
          <div className={styles.saveBar} ref={saveBar}>
            <p className={styles.saveStatus} role="status" data-attention={issues.length ? "true" : undefined}>
              {issues.length ? issues[0].message : "Ready to activate"}
              {dirty ? " · Unsaved changes" : ""}
            </p>
            {issues[0]?.sessionId ? <button type="button" className={`ghost-button ${styles.fixButton}`} onClick={() => fixIssue(issues[0].sessionId!)}>
              Fix
            </button> : null}
            {issues.length > 1 ? <details className={styles.issues}>
              <summary>{issues.length - 1} more to fix</summary>
              <ul>{issues.slice(1).map((issue, index) => <li key={index}>
                {issue.message} {issue.sessionId ? <button type="button" className="ghost-button" onClick={() => fixIssue(issue.sessionId!)}>
                  Fix
                </button> : null}
              </li>)}</ul>
            </details> : null}
            <div className={styles.saveActions}>
              {/* One primary at a time: Save draft until the plan can be published, then Save & view plan. */}
              <button type="submit" className={publishable ? "ghost-button" : "cta"} disabled={!draftValid}>
                {pending ? "Saving…" : "Save draft"}
              </button>
              <button type="button" className={publishable ? "cta" : "ghost-button"} disabled={!publishable} onClick={() => void save(true)}>
                Save &amp; view plan
              </button>
            </div>
          </div>
        </fieldset>
      </form>
      {draft.id ? (
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
      {sessionEdit ? (
        <InjuryDetailSheet
          title={sessionFresh ? "New session" : "Edit session"}
          panelClassName={styles.editorPanel}
          onClose={() => {
            const original = draft.sessions.find((s) => s.id === sessionEdit.id);
            if (sessionFresh || JSON.stringify(original) === JSON.stringify(sessionEdit) || window.confirm("Discard session edits?")) {
              if (sheetDirty && !dirty) void releaseGuard.current().then(() => setSessionEdit(undefined));
              else setSessionEdit(undefined);
            }
          }}
        >
          <form
            className={`${styles.sheet} ${styles.sessionSheet}`}
            onSubmit={(e) => {
              e.preventDefault();
              if (sessionFresh) change({ ...draft, sessions: [...draft.sessions, sessionEdit] });
              else sessionChange(sessionEdit.id, () => sessionEdit);
              // Follow the session to its day so it never disappears from view.
              if (sessionEdit.weekday) setChosenDay(sessionEdit.weekday);
              setSessionEdit(undefined);
            }}
          >
            <label>
              <span>Session name</span>
              <input
                required
                maxLength={100}
                value={sessionEdit.name}
                onChange={(e) => setSessionEdit({ ...sessionEdit, name: e.target.value })}
              />
            </label>
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
            <label>
              <span>Session type</span>
              <select aria-label="Session type" value={sessionEdit.session_type ?? "mixed"}
                onChange={(e) => setSessionEdit(prepareCustomSession({ ...sessionEdit, session_type: e.target.value as CustomSession["session_type"] }, onboarding, sessionEdit.session_type ?? "mixed"))}>
                <option value="mixed">Workout</option>
                <option value="strength_power">Strength / power</option>
                <option value="conditioning">Conditioning</option>
                <option value="skill">Light technical / skill</option>
                <option value="sparring">Hard sparring</option>
                <option value="recovery">Recovery</option>
                <option value="rest">Off / Rest</option>
              </select>
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
      {adding ? (
        <InjuryDetailSheet title="Add exercise" onClose={() => setAdding(undefined)}>
          <div className={styles.sheet}>
            <button type="button" className={styles.addChoice} aria-label="Add from library" aria-describedby="add-choice-library"
              onClick={() => { setSelecting(adding); setAdding(undefined); }}>
              <strong>Add from library</strong>
              <small id="add-choice-library">Search drills and exercises.</small>
            </button>
            <button type="button" className={styles.addChoice} aria-label="Create my exercise" aria-describedby="add-choice-manual"
              onClick={() => { createManual(adding); setAdding(undefined); }}>
              <strong>Create my exercise</strong>
              <small id="add-choice-manual">Name it, set the dose, attach a video.</small>
            </button>
          </div>
        </InjuryDetailSheet>
      ) : null}
      {editing ? (
        <InjuryDetailSheet
          title={editing.fresh ? "Create my exercise" : nameOf(editing.exercise)}
          panelClassName={`${styles.editorPanel} ${styles.exercisePanel}`}
          onClose={() => {
            if (
              (JSON.stringify(editing.original) === JSON.stringify(editing.exercise) &&
                editing.originalPosition === editing.position) ||
              window.confirm("Discard exercise edits?")
            ) {
              suggestionRequest.current++;
              if (sheetDirty && !dirty) void releaseGuard.current().then(() => setEditing(undefined));
              else setEditing(undefined);
            }
          }}
        >
          <form
            className={`${styles.sheet} ${styles.exerciseEditor}`}
            onSubmit={(event) => {
              event.preventDefault();
              const { sessionId, exercise, fresh, position } = editing;
              sessionChange(sessionId, (s) => {
                if (fresh) return { ...s, exercises: [...s.exercises, exercise] };
                const exercises = s.exercises.filter((e) => e.id !== exercise.id);
                exercises.splice(position, 0, exercise);
                return { ...s, exercises };
              });
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
                {editing.exercise.community_revision_id ? <button type="button" className="ghost-button" onClick={() => setCommunityPreview(editing.exercise.origin === 'manual' ? editing.exercise.community_revision_id ?? undefined : undefined)}>View community exercise</button> :
                  <div className={styles.visibility}>
                    <p><strong>{editing.exercise.community_id ? 'Shared with community' : 'Only you can see this exercise'}</strong>
                      <span className="muted">{editing.exercise.community_id ? 'Public once an admin approves it.' : 'Sharing is optional and needs admin approval before it goes public.'}</span></p>
                    <button type="button" className="ghost-button" disabled={!editing.exercise.name.trim()} onClick={() => setCommunityOpen(true)}>{editing.exercise.community_id ? 'Manage community submission' : 'Share with community'}</button>
                  </div>}
                {editing.exercise.community_id && linkedCommunity?.id === editing.exercise.community_id && linkedCommunity.entry &&
                  communityDetailsChanged(linkedCommunity.entry, editing.exercise) ? <div role="status" className={styles.communityNotice}>
                    {linkedCommunity.entry.media_id !== editing.exercise.media_id
                      ? 'Video not included in community submission yet.' : 'Your community submission has older exercise details.'}
                    <button type="button" className="secondary-button" onClick={() => setCommunityOpen(true)}>Update community submission</button>
                  </div> : null}
                {editing.exercise.community_id && linkedCommunity?.id === editing.exercise.community_id && linkedCommunity.error
                  ? <button type="button" className="ghost-button" onClick={() => setCommunityOpen(true)}>Check community submission</button> : null}
                {editing.exercise.media_id ? <PrivateExerciseVideo token={accessToken} id={editing.exercise.media_id} name={editing.exercise.name} /> : null}
                <button type="button" className="ghost-button" onClick={() => setVideoOpen(true)}>
                  {editing.exercise.media_id ? "Replace video" : "Add YouTube demo (optional)"}
                </button>
                {editing.exercise.media_id ? <button type="button" className="ghost-button" onClick={() => setEditing((current) =>
                  current?.exercise.origin === "manual" ? { ...current, exercise: { ...current.exercise, media_id: null } } : current,
                )}>Remove video</button> : null}
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
                    <p>{suggestion.original || (suggestedDose ? "The library suggests this format:" : "The library has no set format for this exercise. Enter your own dose.")}</p>
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
              movement={editing.exercise.origin === "catalogue" ? editing.exercise.snapshot : null}
              past={editing.exercise.origin === "catalogue" && editing.exercise.snapshot.exercise_key
                ? past?.get(editing.exercise.snapshot.exercise_key) : null}
              onChange={(dose) =>
                setEditing({
                  ...editing,
                  exercise: { ...editing.exercise, dose },
                })
              }
            />
            {(["coaching_cues", "stop_rules"] as const).map(field => <label key={field}>
              <span>{field === "coaching_cues" ? "Cues" : "Stop rules"}</span>
              <textarea aria-label={field === "coaching_cues" ? "Cues" : "Stop rules"} rows={2} maxLength={2000}
                value={(editing.exercise[field] ?? []).join("\n")}
                onChange={event => setEditing({ ...editing, exercise: {
                  ...editing.exercise, [field]: event.target.value.split("\n"),
                } })} />
            </label>)}
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
        <DrillPreview entry={preview} token={accessToken} onClose={() => setPreview(undefined)} selfDirected />
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
