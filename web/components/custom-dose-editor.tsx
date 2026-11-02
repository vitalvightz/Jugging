"use client";
import { useState } from "react";
import type { CustomDose } from "@/lib/api-schema.generated";
import styles from "./custom-plan-builder.module.css";
import { blankDose } from "@/lib/custom-plan-dose";
import type { PersonalExercise } from "@/lib/api-schema.generated";
import { pastLoad, weighedFormat } from "@/lib/load-entry";
import { LoadField } from "./load-field";
function DurationField({
  value,
  label,
  onChange,
}: {
  value: number | null;
  label: string;
  onChange: (value: number | null) => void;
}) {
  const [unit, setUnit] = useState(value && value % 60 === 0 ? 60 : 1);
  return (
    <label>
      <span>{label}</span>
      <div className={styles.row}>
        <input
          aria-label={label}
          type="number"
          inputMode="decimal"
          min={1 / unit}
          max={36000 / unit}
          step="any"
          value={value == null ? "" : value / unit}
          onChange={(e) => onChange(e.target.value === "" ? null : Math.round(Number(e.target.value) * unit))}
        />
        <select aria-label={`${label} unit`} value={unit} onChange={(e) => setUnit(Number(e.target.value))}>
          <option value={1}>sec</option>
          <option value={60}>min</option>
        </select>
      </div>
    </label>
  );
}
export function CustomDoseEditor({ dose, onChange, movement, past, essential = false }: {
  dose: CustomDose;
  onChange: (dose: CustomDose) => void;
  /** The catalogue movement, whose format says whether it is weighed. */
  movement?: object | null;
  /** The athlete's own record of this exercise, for the last weight. */
  past?: PersonalExercise | null;
  /** Only the fields that complete the dose, for quick entry while adding exercises. */
  essential?: boolean;
}) {
  function number(
    field: "sets" | "reps" | "rest_seconds" | "rounds" | "rpe" | "distance",
    label: string,
    min: number,
    max: number,
    step = 1,
  ) {
    return (
      <label>
        <span>{label}</span>
        <input
          aria-label={label}
          type="number"
          inputMode="decimal"
          min={min}
          max={max}
          step={step}
          value={dose[field] ?? ""}
          onChange={(event) =>
            onChange({
              ...dose,
              [field]: event.target.value === "" ? null : Number(event.target.value),
            })
          }
        />
      </label>
    );
  }
  return (
    <div className={styles.dose}>
      {essential ? null : <label>
        <span>Format</span>
        <select
          aria-label="Dose format"
          value={dose.mode}
          onChange={(event) => onChange(blankDose(event.target.value as CustomDose["mode"]))}
        >
          <option value="reps">Sets × reps</option>
          <option value="duration">Continuous</option>
          <option value="intervals">Intervals / rounds</option>
          <option value="distance">Distance / carries</option>
          <option value="text">Text prescription</option>
        </select>
      </label>}
      {dose.mode === "reps" ? (
        <>
          {number("sets", "Sets", 1, 100)}
          {number("reps", "Reps", 1, 1000)}
          {essential ? null : number("rest_seconds", "Rest (sec)", 0, 3600)}
        </>
      ) : dose.mode === "duration" || dose.mode === "distance" ? (
        <>
          {dose.mode === "duration" ? <DurationField
            value={dose.duration_seconds}
            label="Duration"
            onChange={(value) => onChange({ ...dose, duration_seconds: value })}
          /> : null}
          {essential ? null : number("sets", "Sets (optional)", 1, 100)}
          {essential && dose.mode === "duration" ? null : <>
          {number("distance", dose.mode === "distance" ? "Distance" : "Distance (optional)", 0.001, 100000, 0.001)}
          <label>
            <span>Distance unit</span>
            <select
              aria-label="Distance unit"
              value={dose.distance_unit ?? ""}
              onChange={(e) =>
                onChange({
                  ...dose,
                  distance_unit: (e.target.value || null) as CustomDose["distance_unit"],
                })
              }
            >
              <option value="">Choose unit</option>
              {["m", "km", "mi", "yd"].map((unit) => (
                <option key={unit}>{unit}</option>
              ))}
            </select>
          </label>
          </>}
          {essential ? null : number("rest_seconds", "Rest (sec)", 0, 3600)}
        </>
      ) : dose.mode === "intervals" ? (
        <>
          {number("rounds", "Rounds", 1, 100)}
          <DurationField
            value={dose.work_seconds}
            label="Work duration"
            onChange={(value) => onChange({ ...dose, work_seconds: value })}
          />
          {number("rest_seconds", "Rest between (sec)", 0, 3600)}
        </>
      ) : (
        <label className={styles.wide}>
          <span>Prescription</span>
          <textarea
            aria-label="Prescription"
            maxLength={500}
            value={dose.prescription ?? ""}
            onChange={(e) => onChange({ ...dose, prescription: e.target.value })}
          />
        </label>
      )}
      {essential ? null : <>
      <LoadField
        // A new format starts the field over on its own default.
        key={dose.mode}
        className={styles.wide}
        value={dose.load_or_intensity}
        weighed={weighedFormat(dose.mode, movement, past)}
        last={pastLoad(past)}
        onChange={(text) => onChange({ ...dose, load_or_intensity: text || null })}
      />
      {/* Per side changes how the reps read, so it stays with them, not behind the fold. */}
      {dose.mode === "reps" ? (
        <label className={`${styles.row} ${styles.wide}`}>
          <input
            type="checkbox"
            checked={dose.per_side ?? false}
            onChange={(e) => onChange({ ...dose, per_side: e.target.checked })}
          />
          Per side
        </label>
      ) : null}
      {/* Optional extras stay one tap away; a value already set keeps them open. */}
      <details className={`${styles.advanced} ${styles.wide}`} open={dose.rpe != null || Boolean(dose.tempo) || undefined}>
        <summary><span>Effort{dose.mode === "reps" ? " & tempo" : ""}</span>
          {dose.rpe != null || dose.tempo ? <small>{[dose.rpe != null ? `RPE ${dose.rpe}` : null, dose.tempo ? `tempo ${dose.tempo}` : null].filter(Boolean).join(" · ")}</small> : null}
        </summary>
        <div className={`${styles.dose} ${styles.groupBody}`}>
          {number("rpe", "RPE (optional)", 1, 10, 0.5)}
          {dose.mode === "reps" ? (
            <label>
              <span>Tempo (optional)</span>
              <input
                maxLength={40}
                value={dose.tempo ?? ""}
                onChange={(e) => onChange({ ...dose, tempo: e.target.value || null })}
              />
            </label>
          ) : null}
        </div>
      </details>
      </>}
    </div>
  );
}
