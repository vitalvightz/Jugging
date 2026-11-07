"use client";

import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { ApiError, deleteManualExercise, getExerciseLibrary, listManualExercises, saveManualExercise, searchTrainingLibrary } from "@/lib/api";
import type { LibraryEntry, ManualExerciseEntry, PersonalExercise } from "@/lib/api-schema.generated";
import { formatAppDate } from "@/lib/date-format";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import {
  blankDraft, draftFromEntry, EXTRA_FIELDS, fieldLabel, FORMATS, formatLabel, NO_MEASUREMENTS, optionChoice,
  optionFormat, optionName, PRIMARY_FIELDS, readDraft, recordedOptions, searchOptions, summarise, UNIT_CHOICES, UNIT_LABEL,
  type Draft, type ExerciseChoice, type ExerciseOption, type FieldId, type MeasurementFormat,
} from "@/lib/manual-exercises";
import type { TrainingHistorySession } from "@/lib/training-history";
import { trainingTitle } from "@/lib/training-disciplines";
import sheet from "./log-training.module.css";
import styles from "./manual-exercise-logger.module.css";
import { SetRows } from "./set-rows";
import { useSheetExit } from "@/lib/use-sheet-exit";
import { useSeenOnce } from "./once-hint";

type Picked = { name: string; choice: ExerciseChoice; format: MeasurementFormat | null; reference: string | null };
/**
 * `original` is the saved entry being edited, to tell real changes from none.
 * `revision` is the saved revision this edit is based on (0 for a new entry);
 * the server refuses it if the entry has moved on since.
 */
type Editor = { entryId: string; revision: number; editing: boolean; picked: Picked | null; draft: Draft; formatMenu: boolean; original: string | null };

const fresh = (unit: "kg" | "lb"): Editor => ({ entryId: crypto.randomUUID(), revision: 0, editing: false, picked: null, draft: blankDraft("strength", unit), formatMenu: false, original: null });
const snapshot = (format: MeasurementFormat | null, draft: Draft) => JSON.stringify([format, draft.values, draft.units, draft.notes, draft.setRows]);
const choiceOf = (entry: ManualExerciseEntry): ExerciseChoice => entry.exercise_id.startsWith("custom:")
  ? { personal_exercise_id: entry.exercise_id.slice(7) } : { exercise_key: entry.exercise_id.slice(4) };

function saveError(reason: unknown): string {
  if (reason instanceof ApiError && reason.code === "health_data_consent_required") return "Health-data use is off. Turn it on in Settings to record exercises.";
  if (reason instanceof ApiError && reason.status === 401) return "Your session expired. Sign in again to save.";
  if (reason instanceof ApiError && (reason.status === 422 || reason.status === 409) && reason.message) return reason.message;
  return "Not saved. Check your connection and try again.";
}

function PencilIcon() {
  return <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false"><path d="M4 20h4L18.5 9.5a2.12 2.12 0 0 0-3-3L5 17v3Z" /><path d="m14.5 7.5 3 3" /></svg>;
}

/**
 * Optional exercise results for one saved manual session. Each exercise is
 * saved when added, so closing the sheet never loses a finished row. The
 * session itself, its workload and its other follow-ups are untouched.
 */
export function ManualExerciseLogger({ token, session, reference, onClose }: {
  token: string; session: TrainingHistorySession;
  /** An earlier manual session whose exercise names may be reused (never its values). */
  reference?: TrainingHistorySession | null;
  onClose: (entries: ManualExerciseEntry[] | null) => void;
}) {
  // Slides away before the parent removes it.
  const { closing, close: exit } = useSheetExit(onClose);
  const fieldId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const saving = useRef(false);
  const [entries, setEntries] = useState<ManualExerciseEntry[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [recorded, setRecorded] = useState<PersonalExercise[]>([]);
  const [query, setQuery] = useState("");
  const [catalogue, setCatalogue] = useState<{ query: string; entries: LibraryEntry[] } | null>(null);
  const [catalogueError, setCatalogueError] = useState(false);
  const [loadUnit, setLoadUnit] = useState<"kg" | "lb">("kg");
  const [editor, setEditor] = useState<Editor>(() => fresh("kg"));
  const [errors, setErrors] = useState<Partial<Record<FieldId, string>>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [unitHint, dismissUnitHint] = useSeenOnce("discovery-weight-unit-seen");
  const [reuse, setReuse] = useState<ManualExerciseEntry[] | null>(null);
  const [reuseState, setReuseState] = useState<"idle" | "loading" | "error">("idle");

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const previous = document.activeElement as HTMLElement | null;
    const release = lockOverlayScroll();
    element.showModal();
    search.current?.focus();
    const update = () => {
      const viewport = window.visualViewport;
      element.style.setProperty("--sheet-height", `${viewport?.height ?? window.innerHeight}px`);
      element.style.setProperty("--sheet-bottom", `${viewport ? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop) : 0}px`);
    };
    update();
    window.visualViewport?.addEventListener("resize", update);
    window.visualViewport?.addEventListener("scroll", update);
    return () => {
      window.visualViewport?.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("scroll", update);
      element.close(); release(); previous?.focus();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    listManualExercises(token, session.id).then(result => { if (!cancelled) { setEntries(result.entries); setLoadError(false); } })
      .catch(() => { if (!cancelled) setLoadError(true); });
    return () => { cancelled = true; };
  }, [token, session.id, attempt]);

  useEffect(() => {
    let cancelled = false;
    // Previously recorded movements rank first; search still works without them.
    getExerciseLibrary(token).then(library => {
      if (cancelled) return;
      setRecorded(library.exercises);
      const unit = [...library.exercises].sort((a, b) => b.latest_day.localeCompare(a.latest_day)).find(item => item.latest_values.load)?.latest_values.load?.unit;
      if (unit === "lb") { setLoadUnit("lb"); setEditor(current => current.picked ? current : fresh("lb")); }
    }).catch(() => { /* Catalogue search and new names remain available. */ });
    return () => { cancelled = true; };
  }, [token]);

  const trimmed = query.trim();
  useEffect(() => {
    if (trimmed.length < 2) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ q: trimmed, category: "training", limit: "6", for_log: "true" });
      searchTrainingLibrary(token, params, controller.signal)
        .then(result => { setCatalogue({ query: trimmed, entries: result.entries }); setCatalogueError(false); })
        .catch(reason => { if (!controller.signal.aborted && (reason as Error)?.name !== "AbortError") setCatalogueError(true); });
    }, 220);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [token, trimmed]);

  const searching = trimmed.length >= 2 && catalogue?.query !== trimmed && !catalogueError;
  const options = trimmed ? searchOptions(recorded, catalogue?.query === trimmed ? catalogue.entries : [], trimmed) : [];
  const recent = trimmed ? [] : recordedOptions(recorded, "", 6);
  const picked = editor.picked;
  const format = picked?.format ?? null;
  // A picked movement is worth keeping even with no values: the name records it.
  const unsaved = Boolean(picked && (!editor.editing || snapshot(format, editor.draft) !== editor.original));

  function focusFirstField() {
    requestAnimationFrame(() => dialog.current?.querySelector<HTMLElement>("[data-measure], [data-format]")?.focus());
  }

  function pick(option: ExerciseOption) {
    const last = option.kind === "recorded" ? summarise(option.exercise.latest_values, option.exercise.log_format) : "";
    const chosen = optionFormat(option);
    setEditor(current => ({ ...current, picked: {
      name: optionName(option), choice: optionChoice(option), format: chosen,
      reference: option.kind === "recorded" && last ? `Last time · ${formatAppDate(option.exercise.latest_day)} · ${last}` : null,
    }, draft: blankDraft(chosen ?? "strength", loadUnit), formatMenu: false, original: null }));
    setQuery(""); setErrors({}); setError(null);
    focusFirstField();
  }

  function pickReuse(entry: ManualExerciseEntry) {
    const last = summarise(entry.actual, entry.format);
    setEditor(current => ({ ...current, picked: { name: entry.name, choice: choiceOf(entry), format: entry.format,
      reference: `Last time · ${formatAppDate(entry.training_day)} · ${last || "no measurements"}` },
    draft: blankDraft(entry.format, entry.actual.load?.unit === "lb" ? "lb" : loadUnit), formatMenu: false, original: null }));
    setErrors({}); setError(null);
    focusFirstField();
  }

  function startEdit(entry: ManualExerciseEntry) {
    if (unsaved && !window.confirm("Discard this exercise?")) return;
    const draft = draftFromEntry(entry);
    setEditor({ entryId: entry.entry_id, revision: entry.revision, editing: true, picked: { name: entry.name, choice: choiceOf(entry), format: entry.format, reference: null },
      draft, formatMenu: false, original: snapshot(entry.format, draft) });
    setErrors({}); setError(null);
    focusFirstField();
  }

  function reset(focus = true) {
    setEditor(fresh(loadUnit)); setErrors({}); setError(null); setQuery("");
    if (focus) requestAnimationFrame(() => search.current?.focus());
  }

  function setValue(field: FieldId, value: string) {
    setEditor(current => ({ ...current, draft: { ...current.draft, values: { ...current.draft.values, [field]: value } } }));
    if (errors[field]) setErrors(current => ({ ...current, [field]: undefined }));
  }

  function cycleUnit(field: FieldId) {
    if (field === "load") dismissUnitHint();
    const choices = UNIT_CHOICES[field]!;
    setEditor(current => {
      const next = choices[(choices.indexOf(current.draft.units[field]!) + 1) % choices.length];
      if (field === "load") setLoadUnit(next as "kg" | "lb");
      return { ...current, draft: { ...current.draft, units: { ...current.draft.units, [field]: next } } };
    });
  }

  /** The session's exercises after saving, or null when nothing was saved. */
  async function save(): Promise<ManualExerciseEntry[] | null> {
    if (!picked || saving.current) return null;
    if (!format) { setError("Choose how to record it."); return null; }
    const { actual, errors: found } = readDraft(format, editor.draft);
    setErrors(found);
    if (Object.keys(found).length) {
      dialog.current?.querySelector<HTMLInputElement>(`[data-measure="${Object.keys(found)[0]}"]`)?.focus();
      return null;
    }
    saving.current = true; setBusy(true); setError(null);
    try {
      const saved = await saveManualExercise(token, session.id, editor.entryId,
        { exercise: picked.choice, format, actual, notes: editor.draft.notes.trim(), revision: editor.revision });
      const list = entries ?? [];
      const next = list.some(item => item.entry_id === saved.entry_id) ? list.map(item => item.entry_id === saved.entry_id ? saved : item) : [...list, saved];
      setEntries(next);
      setReuse(current => current?.filter(item => item.exercise_id !== saved.exercise_id) ?? null);
      setStatus(`${saved.name} ${editor.editing ? "updated" : "added"}.`);
      return next;
    } catch (reason) {
      // Changed elsewhere: show the stored values rather than overwrite them.
      if (reason instanceof ApiError && reason.status === 409 && editor.editing) { reset(false); setAttempt(value => value + 1); }
      setError(saveError(reason));
      return null;
    } finally { saving.current = false; setBusy(false); }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!picked) {
      if (options[0]) pick(options[0]);
      return;
    }
    if (await save()) reset();
  }

  async function remove(entry: ManualExerciseEntry) {
    if (removing || !window.confirm(`Remove ${entry.name}?`)) return;
    setRemoving(entry.entry_id); setError(null);
    try {
      await deleteManualExercise(token, session.id, entry.entry_id);
      setEntries(current => current?.filter(item => item.entry_id !== entry.entry_id) ?? null);
      if (editor.entryId === entry.entry_id) reset(false);
      setStatus(`${entry.name} removed.`);
    } catch (reason) { setError(saveError(reason)); }
    finally { setRemoving(null); }
  }

  async function loadReuse() {
    if (!reference || reuseState === "loading") return;
    setReuseState("loading");
    try {
      const result = await listManualExercises(token, reference.id);
      const have = new Set((entries ?? []).map(item => item.exercise_id));
      const seen = new Set<string>();
      setReuse(result.entries.filter(item => !have.has(item.exercise_id) && !seen.has(item.exercise_id) && seen.add(item.exercise_id)));
      setReuseState("idle");
    } catch { setReuseState("error"); }
  }

  function close() {
    if (busy) return;
    if (unsaved && !window.confirm("Discard this exercise?")) return;
    exit(entries);
  }

  async function done() {
    // Done keeps the exercise in progress rather than dropping it.
    if (!unsaved) { exit(entries); return; }
    const next = await save();
    if (next) exit(next);
  }

  function measure(field: FieldId) {
    const units = UNIT_CHOICES[field];
    const unit = editor.draft.units[field];
    const id = `${fieldId}-${field}`;
    const integer = field === "sets" || field === "reps" || field === "rounds";
    return <div key={field} className={styles.measure} data-invalid={errors[field] ? "true" : undefined}>
      <label htmlFor={id}>{fieldLabel(field, format!)}</label>
      <div className={styles.inputRow}>
        <input id={id} data-measure={field} type="text" inputMode={integer ? "numeric" : "decimal"} autoComplete="off" enterKeyHint="done"
          value={editor.draft.values[field] ?? ""} placeholder="–" disabled={busy}
          aria-invalid={errors[field] ? true : undefined} aria-describedby={errors[field] ? `${id}-error` : undefined}
          onChange={event => setValue(field, event.target.value)} />
        {units && unit ? <button type="button" className={styles.unit} disabled={busy} onClick={() => cycleUnit(field)}
          aria-label={`${fieldLabel(field, format!)} unit: ${UNIT_LABEL[unit]}. Change unit`}>{UNIT_LABEL[unit]}</button> : null}
      </div>
      {errors[field] ? <span className={styles.fieldError} id={`${id}-error`}>{errors[field]}</span> : null}
    </div>;
  }

  const individualSets = Boolean(editor.draft.setRows && (format === "strength" || format === "bodyweight"));
  const extras = format ? EXTRA_FIELDS[format].filter(field => !individualSets || !["load", "effort"].includes(field)) : [];
  const extraFilled = extras.some(field => editor.draft.values[field]?.trim()) || Boolean(editor.draft.notes.trim());
  const hasLoadEntry = Boolean(editor.draft.values.load?.trim() || editor.draft.setRows?.some(row => row.load.trim()));

  return <dialog ref={dialog} className={`${sheet.dialog} ${styles.dialog}`} aria-labelledby={`${fieldId}-title`} data-closing={closing || undefined}
    onCancel={event => { event.preventDefault(); close(); }}>
    <header className={sheet.header}>
      <div><h2 id={`${fieldId}-title`}>Add exercises</h2>
        <p className={sheet.source}>{[trainingTitle(session), session.duration_minutes ? `${session.duration_minutes} min` : null, formatAppDate(session.training_day)].filter(Boolean).join(" · ")}</p>
      </div>
      <button className={sheet.close} type="button" aria-label="Close exercises" disabled={busy} onClick={close}>×</button>
    </header>
    <form className={sheet.form} onSubmit={event => void submit(event)} aria-label="Add exercises" noValidate>
      <div className={`${sheet.body} ${styles.body}`}>
        <p className="sr-only" role="status" aria-live="polite">{status}</p>
        {loadError ? <div className={styles.notice} role="alert"><span>Your saved exercises couldn’t load.</span>
          <button type="button" className={styles.textButton} onClick={() => setAttempt(value => value + 1)}>Retry</button></div> : null}
        {entries === null && !loadError ? <p className={styles.quiet} role="status">Loading exercises…</p> : null}
        {entries?.length ? <ul className={styles.list} aria-label="Exercises in this session">
          {entries.map(entry => <li key={entry.entry_id} className={styles.row} data-editing={editor.editing && editor.entryId === entry.entry_id ? "true" : undefined}>
            <span className={styles.rowText}><strong>{entry.name}</strong>
              <span className={styles.rowValues}>{summarise(entry.actual, entry.format) || NO_MEASUREMENTS}</span></span>
            <button type="button" className={styles.iconButton} aria-label={`Edit ${entry.name}`} disabled={busy || removing !== null} onClick={() => startEdit(entry)}><PencilIcon /></button>
            <button type="button" className={styles.iconButton} aria-label={`Remove ${entry.name}`} disabled={busy || removing !== null} onClick={() => void remove(entry)}>
              {removing === entry.entry_id ? "…" : <span aria-hidden="true">×</span>}</button>
          </li>)}
        </ul> : null}

        {reference && entries && !entries.length && reuse === null && !picked ? <button type="button" className={styles.reuse} disabled={reuseState === "loading"} onClick={() => void loadReuse()}>
          <span><strong>Reuse exercises from {formatAppDate(reference.training_day)}</strong><span className={styles.rowValues}>Names only. You enter today’s values.</span></span>
          <span aria-hidden="true">{reuseState === "loading" ? "…" : "+"}</span>
        </button> : null}
        {reuseState === "error" ? <p className={styles.fieldError} role="alert">Those exercises couldn’t load. Try again.</p> : null}
        {reuse?.length && !picked ? <ul className={styles.list} aria-label={`Exercises from ${formatAppDate(reference!.training_day)}`}>
          {reuse.map(entry => <li key={entry.entry_id} className={styles.row}>
            <button type="button" className={styles.reuseOption} onClick={() => pickReuse(entry)} aria-label={`Log ${entry.name} again`}>
              <span className={styles.rowText}><strong>{entry.name}</strong>
                <span className={styles.rowValues}>Last time · {summarise(entry.actual, entry.format) || "no measurements"}</span></span>
              <span aria-hidden="true" className={styles.plus}>+</span>
            </button>
          </li>)}
        </ul> : null}

        {!picked ? <div className={styles.search}>
          <label className="sr-only" htmlFor={`${fieldId}-search`}>Exercise</label>
          <input ref={search} id={`${fieldId}-search`} className={styles.searchInput} type="search" value={query} maxLength={100}
            placeholder="Search or enter exercise name" autoComplete="off" enterKeyHint="go" disabled={busy}
            aria-controls={trimmed ? `${fieldId}-options` : undefined} onChange={event => setQuery(event.target.value)} />
          {recent.length ? <div className={styles.recent} role="group" aria-label="Recent exercises">
            {recent.map(exercise => <button key={exercise.id} type="button" onClick={() => pick({ kind: "recorded", exercise })}>{exercise.name}</button>)}
          </div> : null}
          {trimmed ? <ul className={styles.options} id={`${fieldId}-options`} aria-label="Matching exercises" aria-busy={searching}>
            {options.map(option => <li key={option.kind === "recorded" ? option.exercise.id : option.kind === "catalogue" ? option.entry.catalogue_id : "new"}>
              <button type="button" className={styles.option} onClick={() => pick(option)}>
                {option.kind === "new" ? <><span className={styles.optionName}>Add “{option.name}”</span><span className={styles.optionMeta}>New exercise · only you see it</span></>
                  : <><span className={styles.optionName}>{optionName(option)}</span>
                    <span className={styles.optionMeta}>{option.kind === "recorded" ? `Yours · ${summarise(option.exercise.latest_values, option.exercise.log_format) || "recorded"}` : "Library"}</span></>}
              </button>
            </li>)}
          </ul> : null}
          {searching ? <p className={styles.quiet} role="status">Searching library…</p> : null}
          {catalogueError && trimmed ? <p className={styles.quiet} role="status">Library search unavailable. Add it by name instead.</p> : null}
        </div> : <div className={styles.editor}>
          <div className={styles.pickedHead}>
            <strong className={styles.pickedName}>{picked.name}</strong>
            {editor.editing ? <button type="button" className={styles.textButton} disabled={busy} onClick={() => reset()}>Cancel edit</button>
              : <button type="button" className={styles.textButton} disabled={busy} onClick={() => reset()}
                aria-label={`Change exercise. Currently ${picked.name}`}>Change</button>}
          </div>
          {format && !editor.formatMenu ? <button type="button" className={styles.formatSwitch} disabled={busy} aria-expanded="false"
            onClick={() => setEditor(current => ({ ...current, formatMenu: true }))}
            aria-label={`Change how this is recorded. Currently ${formatLabel(format)}`}>{formatLabel(format)} · Change</button> : null}
          {!format || editor.formatMenu ? <div className={styles.formats} role="group" aria-label="How to record it">
            {FORMATS.map(item => <button key={item.id} type="button" data-format aria-pressed={format === item.id} disabled={busy}
              onClick={() => { setEditor(current => ({ ...current, formatMenu: false, picked: current.picked && { ...current.picked, format: item.id } })); setError(null); focusFirstField(); }}>{item.label}</button>)}
          </div> : null}
          {picked.reference ? <p className={styles.reference}>{picked.reference}</p> : null}
          {unitHint && hasLoadEntry && (format === "strength" || format === "bodyweight") ? <div className={styles.discoveryHint} role="note">
            <span>Tap the load unit to switch between kg and lb.</span>
            <button type="button" onClick={dismissUnitHint} aria-label="Dismiss weight unit tip">×</button>
          </div> : null}
          {format ? <>
            {editor.draft.setRows && (format === "strength" || format === "bodyweight") ? <SetRows
              rows={editor.draft.setRows} unit={editor.draft.units.load ?? "kg"} onUnitChange={() => cycleUnit("load")}
              showLoad={format === "strength" || editor.draft.values.load !== undefined || editor.draft.setRows.some(row => row.load.trim())}
              disabled={busy} invalid={errors.reps ? editor.draft.setRows.map((_, index) => index) : []}
              onChange={setRows => { setEditor(current => ({ ...current, draft: { ...current.draft, setRows } })); setErrors({}); }} />
              : <>
                <div className={styles.measures} data-count={PRIMARY_FIELDS[format].length}>{PRIMARY_FIELDS[format].map(measure)}</div>
                {format === "strength" || format === "bodyweight" ? <button type="button" className={styles.textButton} disabled={busy} onClick={() => {
                  const count = Number(editor.draft.values.sets || 1);
                  if (!Number.isInteger(count) || count < 1 || count > 50) { setErrors({ sets: "Enter 1–50 sets" }); return; }
                  setEditor(current => ({ ...current, draft: { ...current.draft, setRows: Array.from({ length: count }, () => ({ reps: current.draft.values.reps ?? "", load: current.draft.values.load ?? "", ...(current.draft.values.effort ? { rpe: current.draft.values.effort } : {}) })) } }));
                }}>Edit sets individually</button> : null}
              </>}
            {editor.draft.setRows && format === "bodyweight" && editor.draft.values.load === undefined && !editor.draft.setRows.some(row => row.load.trim())
              ? <button type="button" className={styles.textButton} disabled={busy} onClick={() => setValue("load", "")}>Add load per set</button> : null}
            <details className={styles.more} open={extraFilled || undefined}>
              <summary>More details</summary>
              <div className={styles.measures} data-count={Math.min(3, extras.length)}>{extras.map(measure)}</div>
              <label className={styles.notes}>Notes
                <input type="text" value={editor.draft.notes} maxLength={500} disabled={busy} autoComplete="off"
                  onChange={event => setEditor(current => ({ ...current, draft: { ...current.draft, notes: event.target.value } }))} />
              </label>
            </details>
          </> : null}
        </div>}
      </div>
      <footer className={`${sheet.actions} ${styles.actions}`}>
        {error ? <p className={sheet.error} role="alert">{error}</p> : null}
        <div className={styles.buttons}>
          {/* One primary at a time: Add while an exercise is in progress, otherwise Done. */}
          <button type="button" className={picked || trimmed ? "ghost-button" : "cta"} disabled={busy} onClick={() => void done()}>Done</button>
          <button type="submit" className={picked || trimmed ? "cta" : "ghost-button"} disabled={busy || (!picked && !options.length)}>
            {busy ? "Saving…" : editor.editing ? "Save changes" : "Add exercise"}</button>
        </div>
      </footer>
    </form>
  </dialog>;
}
