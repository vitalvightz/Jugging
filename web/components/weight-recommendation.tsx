import type { WeightRecommendation as Recommendation } from "@/lib/weight-recommendation";
import styles from "./weight-recommendation.module.css";

/** The same hierarchy and spacing on rows, sheets, pickers and rest screens. */
export function WeightRecommendation({ value, dose }: { value: Recommendation; dose?: string }) {
  if (!value.last && !value.suggested) return null;
  const format = (load: Recommendation["last"]) => load ? `${load.value} ${load.unit}` : "—";
  return <span className={styles.card}>
    <span className={styles.numbers}>
      <span className={styles.primary}><span className={styles.label}>Suggested</span><strong>{format(value.suggested)}</strong></span>
      <span className={styles.secondary}><span className={styles.label}>Last</span><span>{format(value.last)}</span></span>
    </span>
    {value.tryReps ? <span className={styles.reps}>Try {value.tryReps} reps</span> : null}
    {dose ? <span className={styles.dose}>{dose} · Editable</span> : null}
  </span>;
}
