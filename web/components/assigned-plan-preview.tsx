import type { CustomPlan } from "@/lib/api-schema.generated";
import { doseSummary } from "@/lib/custom-plan-dose";
import { formatAppDate } from "@/lib/date-format";
import styles from "./coach-workspace.module.css";

export function AssignedPlanPreview({ plan }: { plan: CustomPlan }) {
  return <div className={styles.planPreview}>{plan.sessions.map(session => {
    const count = session.exercises.length;
    return <section key={session.id} className={styles.planPreviewDay}>
      <header className={styles.planPreviewHead}>
        <h3><span>{session.scheduled_date ? formatAppDate(session.scheduled_date) : session.weekday ?? "Day not set"}</span> · {session.name}</h3>
        {count ? <span className={styles.planPreviewCount}>{count} {count === 1 ? "exercise" : "exercises"}</span> : null}
      </header>
      {!count ? <p className={styles.planPreviewEmpty}>{session.session_type === "rest" ? "Rest day" : "Coach-led session"}</p> : <ol className={styles.planPreviewList}>{session.exercises.map(exercise => {
        const notes = exercise.origin === "manual" ? exercise.notes : "";
        const hasDetails = Boolean(notes || exercise.coaching_cues?.length || exercise.stop_rules?.length);
        return <li key={exercise.id}>
          <div className={styles.planPreviewRow}>
            <strong>{exercise.origin === "manual" ? exercise.name : exercise.snapshot.name}</strong>
            <span>{doseSummary(exercise.dose)}</span>
          </div>
          {hasDetails ? <details className={styles.planPreviewDetails}>
            <summary>Notes</summary>
            {notes ? <p>{notes}</p> : null}
            {exercise.coaching_cues?.length ? <p>Cues: {exercise.coaching_cues.join(" · ")}</p> : null}
            {exercise.stop_rules?.length ? <p>Stop: {exercise.stop_rules.join(" · ")}</p> : null}
          </details> : null}
        </li>;
      })}</ol>}
    </section>;
  })}</div>;
}
