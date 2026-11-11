import type { WeightRecommendation as Recommendation } from "@/lib/weight-recommendation";
import styles from "./weight-recommendation.module.css";

/** Compact guidance only; never a record of completed measurements. */
export function WeightRecommendation({ value, subtle = false }: { value: Recommendation; subtle?: boolean }) {
  if (!value.last && !value.suggested && !value.tryReps) return null;
  const format = (load: NonNullable<Recommendation["last"]>) => `${load.value} ${load.unit}`;
  const same = value.last && value.suggested
    && value.last.value === value.suggested.value && value.last.unit === value.suggested.unit;
  return <span className={styles.card} data-subtle={subtle || undefined}>
    {value.suggested ? <span className={styles.primary}>
      {value.tryReps ? "" : "Suggested "}<strong>{format(value.suggested)}</strong>
    </span> : null}
    {value.suggested && (value.tryReps || value.last) ? " · " : null}
    {value.tryReps ? <span className={styles.secondary}>Try {value.tryReps} reps</span>
      : value.last ? <span className={styles.secondary}>{same ? "Same as last" : `Last ${format(value.last)}`}</span> : null}
  </span>;
}
