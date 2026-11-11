import { compactWeightAge, type WeightRecommendation as Recommendation } from "@/lib/weight-recommendation";
import styles from "./weight-recommendation.module.css";

/** Compact guidance only; never a record of completed measurements. */
export function WeightRecommendation({ value, subtle = false }: { value: Recommendation; subtle?: boolean }) {
  const same = value.last && value.suggested
    && value.last.value === value.suggested.value && value.last.unit === value.suggested.unit;
  const suggested = value.calculated || value.tryReps || !same ? value.suggested : null;
  if (!value.last && !suggested && !value.tryReps) return null;
  const format = (load: NonNullable<Recommendation["last"]>) => `${load.value} ${load.unit}`;
  const age = compactWeightAge(value.lastDay);
  return <span className={styles.card} data-subtle={subtle || undefined}>
    {suggested ? <span className={styles.primary}>
      {value.calculated && !value.tryReps ? "Suggested " : ""}<strong>{format(suggested)}</strong>
    </span> : null}
    {suggested && (value.tryReps || value.last) ? " · " : null}
    {value.tryReps ? <span className={styles.secondary}>Try {value.tryReps} reps</span>
      : value.last ? <span className={styles.secondary}>{`Last ${format(value.last)}`}
        {age ? <span className={styles.age}> · {age}</span> : null}</span> : null}
  </span>;
}
