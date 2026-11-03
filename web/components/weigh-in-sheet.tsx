"use client";

import { type ReactNode, useEffect, useId, useRef, useState, useTransition } from "react";

import { formatAppDate } from "@/lib/date-format";
import { localDateValue, localTimeValue, type TargetAndWeighIn, toNumber } from "@/lib/nutrition-workspace";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import type { NutritionBodyweightLogEntry, WeighInType } from "@/lib/types";
import { useSheetExit } from "@/lib/use-sheet-exit";
import { weighInMoment } from "@/lib/weight-cut-status";
import sheet from "./session-details.module.css";
import summary from "./training-summary.module.css";
import styles from "./weigh-in-sheet.module.css";

/** The bottom sheet both weight sheets share: modal, scroll lock, slide-away close. */
function Sheet({ title, quiet, busy, onClose, children }: {
  title: string; quiet: string; busy: boolean; onClose: () => void; children: ReactNode;
}) {
  // Slides away before the parent removes it.
  const { closing, close: dismiss } = useSheetExit(onClose);
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();

  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    const release = lockOverlayScroll();
    element.querySelector<HTMLInputElement>("input")?.focus({ preventScroll: true });
    return () => { element.close(); release(); };
  }, []);

  return (
    <dialog ref={dialog} className={sheet.dialog} aria-labelledby={heading} data-closing={closing || undefined} onCancel={event => { event.preventDefault(); if (!busy) dismiss(); }}>
      <header className={sheet.header}>
        <div>
          <p className={sheet.eyebrow}>Weight tracker</p>
          <h2 id={heading}>{title}</h2>
          <p className={sheet.quiet}>{quiet}</p>
        </div>
        <button type="button" className={sheet.close} aria-label="Close" onClick={() => dismiss()} disabled={busy}>×</button>
      </header>
      {children}
    </dialog>
  );
}

/** A large kg field with 0.1 kg steppers that start from `base` when empty. */
function KgField({ id, label, value, base, onChange }: {
  id: string; label: string; value: string; base: number | null; onChange: (value: string) => void;
}) {
  const step = (by: number) => onChange(Math.max(0, Math.round(((toNumber(value) ?? base ?? 0) + by) * 10) / 10).toFixed(1));
  return (
    <div>
      <label htmlFor={id} className={styles.fieldLabel}>{label}</label>
      <div className={styles.weightField}>
        <button type="button" aria-label="Decrease by 0.1 kg" onClick={() => step(-0.1)}>−</button>
        <span className={styles.weightValue}>
          <input id={id} type="number" inputMode="decimal" step="0.1" min="0"
            placeholder={base != null ? base.toFixed(1) : "0.0"} value={value}
            onChange={event => onChange(event.target.value)} />
          <span aria-hidden="true">kg</span>
        </span>
        <button type="button" aria-label="Increase by 0.1 kg" onClick={() => step(0.1)}>+</button>
      </div>
    </div>
  );
}

function useSaving() {
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  function run(action: () => Promise<void>) {
    setError(null);
    startTransition(async () => {
      try {
        await action();
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Couldn't save. Try again.");
      }
    });
  }
  return { error, setError, isPending, run };
}

type Draft = { weight: string; date: string; time: string; fasted: boolean | null; notes: string };

export function WeighInSheet({ entry, lastWeight, onClose, onSave, onDelete }: {
  entry: NutritionBodyweightLogEntry | null;
  lastWeight: number | null;
  onClose: () => void;
  onSave: (entry: NutritionBodyweightLogEntry) => Promise<void>;
  onDelete?: () => Promise<void>;
}) {
  const fieldId = useId();
  const [draft, setDraft] = useState<Draft>(() => entry ? {
    weight: String(entry.weight_kg), date: entry.date, time: entry.time ?? "", fasted: entry.is_fasted ?? null, notes: entry.notes ?? "",
  } : { weight: "", date: localDateValue(), time: localTimeValue(), fasted: null, notes: "" });
  const { error, setError, isPending, run } = useSaving();

  function save() {
    const weight = toNumber(draft.weight);
    if (weight == null || weight <= 0) {
      setError("Enter your weight in kg.");
      document.getElementById(`${fieldId}-weight`)?.focus();
      return;
    }
    if (!draft.date.trim()) {
      setError("Choose the date of this entry.");
      return;
    }
    if (draft.date.trim() > localDateValue()) {
      setError("A weight can't be dated in the future.");
      return;
    }
    run(() => onSave({
      date: draft.date.trim(),
      weight_kg: weight,
      time: draft.time.trim() || null,
      is_fasted: draft.fasted,
      notes: draft.notes.trim() || null,
    }));
  }

  return (
    <Sheet title={entry ? "Edit weight" : "Log today's weight"} quiet={entry ? formatAppDate(entry.date) : "Same time each day keeps your trend honest."}
      busy={isPending} onClose={onClose}>
      <form className={`${sheet.body} ${styles.form}`} noValidate onSubmit={event => { event.preventDefault(); save(); }}>
        <KgField id={`${fieldId}-weight`} label="Weight" value={draft.weight} base={lastWeight}
          onChange={weight => { setDraft(current => ({ ...current, weight })); setError(null); }} />

        <div>
          <p className={styles.fieldLabel} id={`${fieldId}-fasted`}>Before or after food?</p>
          <div className={`${summary.toggle} ${styles.fasted}`} role="group" aria-labelledby={`${fieldId}-fasted`}>
            {([[true, "Fasted"], [false, "Fed"]] as const).map(([value, label]) => (
              <button key={label} type="button" aria-pressed={draft.fasted === value}
                onClick={() => setDraft(current => ({ ...current, fasted: current.fasted === value ? null : value }))}>
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className={styles.when}>
          <div className="field">
            <label htmlFor={`${fieldId}-date`}>Date</label>
            <input id={`${fieldId}-date`} type="date" max={localDateValue()} value={draft.date} onChange={event => setDraft(current => ({ ...current, date: event.target.value }))} />
          </div>
          <div className="field">
            <label htmlFor={`${fieldId}-time`}>Time</label>
            <input id={`${fieldId}-time`} type="time" value={draft.time} onChange={event => setDraft(current => ({ ...current, time: event.target.value }))} />
          </div>
        </div>

        <div className="field">
          <label htmlFor={`${fieldId}-notes`}>Notes <span className="muted">(optional)</span></label>
          <textarea id={`${fieldId}-notes`} rows={2} value={draft.notes} onChange={event => setDraft(current => ({ ...current, notes: event.target.value }))} />
        </div>

        {error ? <p className={styles.formError} role="alert">{error}</p> : null}

        <div className={styles.actions}>
          <button type="submit" className="cta" disabled={isPending}>{isPending ? "Saving…" : entry ? "Save changes" : "Save weight"}</button>
          {onDelete ? (
            <button type="button" className="ghost-button danger-button" disabled={isPending}
              onClick={() => { if (window.confirm("Delete this weight entry?")) run(onDelete); }}>
              Delete entry
            </button>
          ) : null}
        </div>
      </form>
    </Sheet>
  );
}

const WEIGH_IN_TYPES: Array<{ value: WeighInType; label: string }> = [
  { value: "day_before", label: "Day before" },
  { value: "same_day", label: "Same day" },
  { value: "informal", label: "Informal" },
];

/**
 * Sets the target weight and weigh-in. The newest target saved wins, including
 * over one Camp Setup started with, so an existing target is prefilled to edit.
 */
export function TargetSheet({ existingTargetKg, campWeightKg, fightDate, weighInType, weighInTime, onClose, onSave }: {
  existingTargetKg: number | null;
  campWeightKg: number | null;
  fightDate: string | null;
  weighInType: WeighInType | null;
  weighInTime: string | null;
  onClose: () => void;
  onSave: (values: TargetAndWeighIn) => Promise<void>;
}) {
  const fieldId = useId();
  const [target, setTarget] = useState(existingTargetKg != null ? existingTargetKg.toFixed(1) : "");
  const [type, setType] = useState<WeighInType | null>(weighInType);
  const [time, setTime] = useState(weighInTime ?? "");
  const { error, setError, isPending, run } = useSaving();
  const moment = weighInMoment(fightDate, type, time);

  function save() {
    const targetKg = toNumber(target);
    if (targetKg == null || targetKg <= 0) {
      setError("Enter your target weight in kg.");
      document.getElementById(`${fieldId}-target`)?.focus();
      return;
    }
    run(() => onSave({ target_weight_kg: targetKg, weigh_in_type: type, weigh_in_time: time.trim() || null }));
  }

  return (
    <Sheet title={existingTargetKg == null ? "Add target weight" : "Target and weigh-in"}
      quiet="Your gap to target and cut status use this." busy={isPending} onClose={onClose}>
      <form className={`${sheet.body} ${styles.form}`} noValidate onSubmit={event => { event.preventDefault(); save(); }}>
        <KgField id={`${fieldId}-target`} label="Target weight" value={target} base={campWeightKg} onChange={value => { setTarget(value); setError(null); }} />

        <div>
          <p className={styles.fieldLabel} id={`${fieldId}-type`}>Weigh-in</p>
          <div className={`${summary.toggle} ${styles.weighInType}`} role="group" aria-labelledby={`${fieldId}-type`}>
            {WEIGH_IN_TYPES.map(option => (
              <button key={option.value} type="button" aria-pressed={type === option.value}
                onClick={() => setType(current => (current === option.value ? null : option.value))}>
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <div className="field">
          <label htmlFor={`${fieldId}-time`}>Weigh-in time <span className="muted">(optional)</span></label>
          <input id={`${fieldId}-time`} type="time" value={time} onChange={event => setTime(event.target.value)} />
          {moment ? <p className={styles.hint}>Weigh-in {moment}</p> : null}
        </div>

        {error ? <p className={styles.formError} role="alert">{error}</p> : null}

        <div className={styles.actions}>
          <button type="submit" className="cta" disabled={isPending}>{isPending ? "Saving…" : "Save"}</button>
        </div>
      </form>
    </Sheet>
  );
}
