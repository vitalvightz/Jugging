"use client";

import { useEffect, useState } from "react";
import type { PersonalExercise } from "@/lib/api-schema.generated";
import { getExerciseLibrary } from "@/lib/api";
import { EXERCISE_HISTORY_UPDATED, pastExercisesByKey } from "@/lib/exercise-library";
import { formatLoad, parseLoad, startsAsWeight, type LoadUnit } from "@/lib/load-entry";
import styles from "./load-field.module.css";

/** The athlete's own exercises by key, or null while loading (and without a token). */
export function usePastExercises(token: string | null | undefined): ReadonlyMap<string, PersonalExercise> | null {
  const [loaded, setLoaded] = useState<{ token: string; map: ReadonlyMap<string, PersonalExercise> } | null>(null);
  useEffect(() => {
    if (!token) return;
    let live = true;
    let revision = 0;
    const refresh = () => {
      const current = ++revision;
      void pastExercisesByKey(token, getExerciseLibrary).then(map => {
        if (live && revision === current) setLoaded({ token, map });
      });
    };
    refresh();
    window.addEventListener(EXERCISE_HISTORY_UPDATED, refresh);
    return () => { live = false; window.removeEventListener(EXERCISE_HISTORY_UPDATED, refresh); };
  }, [token]);
  return token && loaded?.token === token ? loaded.map : null;
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
      <path d="M4 20h4L18.5 9.5a2.12 2.12 0 0 0-3-3L5 17v3Z" />
      <path d="m14.5 7.5 3 3" />
    </svg>
  );
}

/**
 * Weight as a number with its unit picked, never typed, so it is tracked. The
 * pencil switches to free text for what is not a weight ("light band", "fast").
 */
export function LoadField({ value, onChange, weighed, last, className }: {
  value: string | null | undefined;
  onChange: (text: string) => void;
  /** Sets × reps work opens on a weight; everything else opens on text. */
  weighed: boolean;
  /** Last time's weight: the placeholder and the starting unit. */
  last?: { value: number; unit: LoadUnit } | null;
  /** The surrounding form's field class, for the label. */
  className?: string;
}) {
  const parsed = parseLoad(value);
  const [asWeight, setAsWeight] = useState(() => startsAsWeight(value, weighed || Boolean(last)));
  const [unit, setUnit] = useState<LoadUnit>(parsed?.unit ?? last?.unit ?? "kg");
  const [amount, setAmount] = useState(parsed ? String(parsed.value) : "");
  // A value set from outside (a suggestion applied) replaces what is shown.
  const [seen, setSeen] = useState(value ?? "");
  if ((value ?? "") !== seen) {
    setSeen(value ?? "");
    if (parsed) {
      setAsWeight(true);
      setUnit(parsed.unit);
      setAmount(String(parsed.value));
    } else if (value?.trim()) setAsWeight(false);
    else setAmount("");
  }

  function emit(text: string) {
    setSeen(text);
    onChange(text);
  }

  function changeAmount(text: string, nextUnit = unit) {
    setAmount(text);
    const number = Number(text.replace(",", "."));
    emit(text.trim() && Number.isFinite(number) && number > 0 ? formatLoad(number, nextUnit) : "");
  }

  function switchMode() {
    if (asWeight) {
      // Whatever weight was set stays, now as editable text.
      setAsWeight(false);
      return;
    }
    const weight = parseLoad(value);
    if (weight) setUnit(weight.unit);
    setAsWeight(true);
    changeAmount(weight ? String(weight.value) : "", weight?.unit ?? unit);
  }

  return (
    <div className={`${styles.field} ${className ?? ""}`}>
      <span>{asWeight ? "Weight" : "Intensity"}</span>
      <div className={styles.box} data-mode={asWeight ? "weight" : "text"}>
        {asWeight ? (
          <>
            <input aria-label="Weight" type="number" inputMode="decimal" min={0} max={1000} step="any"
              placeholder={last ? String(last.value) : "0"} value={amount}
              onChange={e => changeAmount(e.target.value)} />
            <select aria-label="Weight unit" value={unit} onChange={e => {
              const next = e.target.value as LoadUnit;
              setUnit(next);
              if (amount) changeAmount(amount, next);
            }}>
              <option value="kg">kg</option>
              <option value="lb">lb</option>
            </select>
          </>
        ) : (
          <input aria-label="Intensity" maxLength={80} placeholder="e.g. light band, fast"
            value={value ?? ""} onChange={e => emit(e.target.value)} />
        )}
        <button type="button" className={styles.toggle} onClick={switchMode}
          aria-label={asWeight ? "Describe it instead of a weight" : "Enter a weight instead"}
          title={asWeight ? "Describe it instead" : "Enter a weight"}>
          {asWeight ? <PencilIcon /> : <span>{unit}</span>}
        </button>
      </div>
    </div>
  );
}
