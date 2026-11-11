"use client";

import { useState, type ReactNode } from "react";
import type { ExerciseChangeDose } from "@/lib/types";
import { finishedDose, validDose, type DoseDraft } from "@/lib/exercise-dose";
import type { PersonalExercise } from "@/lib/api-schema.generated";
import { pastLoad, weighedFormat } from "@/lib/load-entry";
import { LoadField } from "./load-field";
import styles from "./exercise-customisation.module.css";

/** The coaching copy a rendered card shows: cues, Easier, Progress and the stop rule. */
export type ExerciseCoaching = {
  coaching_cues: string[];
  regression_options: string[];
  progression_rule: string | null;
  stop_rules: string[];
};

function TimeField({ label, value, onChange, rest = false }: {
  label: string; value?: number | null; onChange: (value: number | null) => void; rest?: boolean;
}) {
  const [unit, setUnit] = useState(!rest && value && value >= 60 && value % 60 === 0 ? 60 : 1);
  return <label className={styles.doseField}><span>{label}</span><div className={styles.unitField}>
    <input aria-label={label} type="number" inputMode="decimal" min={rest ? 0 : 1 / unit}
      max={(rest ? 3600 : 36000) / unit} step="any" value={value == null ? "" : value / unit}
      onChange={e => onChange(e.target.value === "" ? null : Math.round(Number(e.target.value) * unit))} />
    <select aria-label={`${label} unit`} value={unit} onChange={e => setUnit(Number(e.target.value))}>
      <option value={1}>sec</option><option value={60}>min</option>
    </select>
  </div></label>;
}

const lines = (text: string) => text.split("\n").map(s => s.trim()).filter(Boolean);

/** The same prescription editor for Add, Swap and an existing rendered card. */
export function ExercisePrescriptionEditor({ initial, action, busy, coaching, past, onSubmit, children }: {
  initial: DoseDraft; action: string; busy: boolean; coaching?: ExerciseCoaching;
  /** The athlete's own record of this exercise: last weight and whether it is weighed. */
  past?: PersonalExercise | null;
  onSubmit: (dose: ExerciseChangeDose, coaching?: ExerciseCoaching) => void;
  /** Rendered just above the submit button (e.g. which workouts to apply to). */
  children?: ReactNode;
}) {
  const [dose, setDose] = useState(initial);
  // A later starting dose (history arriving after the editor opened) is taken
  // only while the athlete has not changed anything; their input is never reset.
  const [touched, setTouched] = useState(false);
  const [baseline, setBaseline] = useState(initial);
  if (!touched && initial !== baseline && JSON.stringify(initial) !== JSON.stringify(baseline)) {
    setBaseline(initial);
    setDose(initial);
  }
  const [cueText, setCueText] = useState((coaching?.coaching_cues ?? []).join("\n"));
  const [easierText, setEasierText] = useState((coaching?.regression_options ?? []).join("\n"));
  const [progressText, setProgressText] = useState(coaching?.progression_rule ?? "");
  const [stopText, setStopText] = useState((coaching?.stop_rules ?? []).join("\n"));
  const update = (field: keyof DoseDraft, value: DoseDraft[keyof DoseDraft]) => {
    setTouched(true);
    setDose(current => ({ ...current, [field]: value }));
  };
  function number(field: "sets" | "reps" | "rounds" | "rpe", label: string, max: number, optional = false) {
    return <label className={styles.doseField}><span>{label}</span><input aria-label={label}
      type="number" inputMode="decimal" min={1} max={max} step={field === "rpe" ? 0.5 : 1}
      required={!optional} value={dose[field] ?? ""}
      onChange={e => update(field, e.target.value === "" ? null : Number(e.target.value))} /></label>;
  }
  // What is set inside each collapsed group, so nothing hides behind a fold.
  const effortSummary = [dose.mode !== "reps" ? dose.load_or_intensity?.trim() : null, dose.rpe != null ? `RPE ${dose.rpe}` : null,
    dose.mode === "reps" && dose.tempo?.trim() ? `Tempo ${dose.tempo.trim()}` : null].filter(Boolean).join(" · ");
  const coachingSummary = [lines(cueText).length ? `${lines(cueText).length} cue${lines(cueText).length === 1 ? "" : "s"}` : null,
    lines(easierText).length ? "Easier" : null, progressText.trim() ? "Progress" : null,
    lines(stopText).length ? "Stop rule" : null].filter(Boolean).join(" · ");
  return <form className={styles.prescriptionEditor} onSubmit={e => {
    e.preventDefault();
    if (validDose(dose)) onSubmit(finishedDose(dose), coaching ? {
      coaching_cues: lines(cueText), regression_options: lines(easierText),
      // "" clears a rule the athlete emptied; null would leave it unchanged.
      progression_rule: progressText.trim(), stop_rules: lines(stopText),
    } : undefined);
  }}>
    <label className={styles.formatField}><span className="sr-only">Dose format</span>
      <select aria-label="Dose format" value={dose.mode} onChange={e => {
        setTouched(true);
        setDose({
          mode: e.target.value as DoseDraft["mode"], rest_seconds: dose.rest_seconds,
          load_or_intensity: dose.load_or_intensity, rpe: dose.rpe, distance_unit: "m",
        });
      }}>
        <option value="reps">Sets × reps</option><option value="duration">Time / hold</option>
        <option value="intervals">Intervals / rounds</option><option value="distance">Distance</option>
        <option value="text">Drill instructions</option>
      </select>
    </label>
    <div className={styles.doseGrid}>
      {dose.mode === "reps" ? <>{number("sets", "Sets", 100)}{number("reps", "Reps", 1000)}</>
        : dose.mode === "intervals" ? <>{number("rounds", "Rounds", 100)}<TimeField label="Work" value={dose.work_seconds} onChange={v => update("work_seconds", v)} /></>
        : dose.mode === "duration" ? <><TimeField label="Duration" value={dose.duration_seconds} onChange={v => update("duration_seconds", v)} />{number("sets", "Sets (optional)", 100, true)}</>
        : dose.mode === "distance" ? <><label className={styles.doseField}><span>Distance</span><div className={styles.unitField}>
          <input aria-label="Distance" type="number" inputMode="decimal" min={0.001} max={100000} step="any"
            value={dose.distance ?? ""} onChange={e => update("distance", e.target.value === "" ? null : Number(e.target.value))} />
          <select aria-label="Distance unit" value={dose.distance_unit ?? "m"} onChange={e => update("distance_unit", e.target.value as DoseDraft["distance_unit"])}>
            {["m", "km", "mi", "yd"].map(unit => <option key={unit}>{unit}</option>)}
          </select></div></label>{number("sets", "Sets (optional)", 100, true)}</>
        : <label className={`${styles.doseField} ${styles.wideField}`}><span>Drill instructions</span>
          <textarea aria-label="Drill instructions" maxLength={500} value={dose.prescription ?? ""} onChange={e => update("prescription", e.target.value)} />
        </label>}
      <TimeField label="Rest" rest value={dose.rest_seconds} onChange={v => update("rest_seconds", v)} />
    </div>
    {dose.mode === "reps" ? <LoadField className={styles.doseField}
      value={dose.load_or_intensity} weighed={weighedFormat(dose.mode, past)}
      last={pastLoad(past)} onChange={text => update("load_or_intensity", text)} /> : null}
    {/* Per side changes how the reps read, so it sits with them, not behind a fold. */}
    {dose.mode === "reps" ? <label className={styles.toggleRow}>
      <span><strong>Per side</strong><small>Reps are done on each side</small></span>
      <input aria-label="Per side" type="checkbox" role="switch" className={styles.switch}
        checked={dose.per_side ?? false} onChange={e => update("per_side", e.target.checked)} />
    </label> : null}
    <details className={styles.advanced}>
      <summary><span>More options</span>{effortSummary ? <small>{effortSummary}</small> : null}</summary>
      <div className={styles.groupBody}>
        {dose.mode !== "reps" ? <LoadField className={styles.doseField} value={dose.load_or_intensity} weighed={weighedFormat(dose.mode, past)}
          last={pastLoad(past)} onChange={text => update("load_or_intensity", text)} /> : null}
        <div className={styles.pairGrid}>
          {number("rpe", "RPE (optional)", 10, true)}
          {dose.mode === "reps" ? <label className={`${styles.doseField} ${styles.textField}`}><span>Tempo</span>
            <input aria-label="Tempo" maxLength={40} placeholder="e.g. 3-1-1" value={dose.tempo ?? ""} onChange={e => update("tempo", e.target.value)} /></label> : null}
        </div>
      </div>
    </details>
    {coaching ? <details className={styles.advanced}>
      <summary><span>Coaching</span>{coachingSummary ? <small>{coachingSummary}</small> : null}</summary>
      <div className={styles.groupBody}>
        <label className={styles.coachingField}><span>Cues</span>
          <textarea aria-label="Cues" maxLength={2000} rows={2} placeholder="One cue per line" value={cueText} onChange={e => setCueText(e.target.value)} /></label>
        <label className={styles.coachingField}><span>Easier</span>
          <textarea aria-label="Easier" maxLength={2000} rows={2} placeholder="A lighter option, one per line" value={easierText} onChange={e => setEasierText(e.target.value)} /></label>
        <label className={styles.coachingField}><span>Progress</span>
          <textarea aria-label="Progress" maxLength={500} rows={2} placeholder="When and how to make it harder" value={progressText} onChange={e => setProgressText(e.target.value)} /></label>
        <label className={styles.coachingField}><span className={styles.stopLabel}>Stop rules</span>
          <textarea aria-label="Stop rules" maxLength={2000} rows={2} placeholder="One rule per line" value={stopText} onChange={e => setStopText(e.target.value)} /></label>
      </div>
    </details> : null}
    {children}
    <button type="submit" className={styles.primary} disabled={!validDose(dose) || busy}>{busy ? "Saving…" : action}</button>
  </form>;
}
