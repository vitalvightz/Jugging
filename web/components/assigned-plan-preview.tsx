import type { CustomPlan } from "@/lib/api-schema.generated";
import { doseSummary } from "@/lib/custom-plan-dose";
import styles from "./coach-workspace.module.css";

export function AssignedPlanPreview({ plan }: { plan: CustomPlan }) {
  return <div>{plan.sessions.map(session => <section key={session.id} className={styles.panel}>
    <h3>{session.scheduled_date ?? session.weekday ?? "Day not set"} · {session.name}</h3>
    {!session.exercises.length ? <p>{session.session_type === "rest" ? "Rest day" : "Coach-led session"}</p> : <ol>{session.exercises.map(exercise => <li key={exercise.id}>
      <strong>{exercise.origin === "manual" ? exercise.name : exercise.snapshot.name}</strong>
      <p>{doseSummary(exercise.dose)}</p>
      {exercise.origin === "manual" && exercise.notes ? <p>{exercise.notes}</p> : null}
      {exercise.coaching_cues?.length ? <p>Cues: {exercise.coaching_cues.join(" · ")}</p> : null}
      {exercise.stop_rules?.length ? <p>Stop: {exercise.stop_rules.join(" · ")}</p> : null}
    </li>)}</ol>}
  </section>)}</div>;
}
