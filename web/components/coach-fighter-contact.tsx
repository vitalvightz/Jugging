"use client";
// On a fighter's profile: open the chat, and set a check-in nudge. The fighter
// is nudged in their own time zone, only on days they have not checked in,
// never in their quiet hours, and never under 18.
import Link from "next/link";
import { useEffect, useState } from "react";
import {
  chatLink, describeDays, EVERY_DAY, fetchCheckinNudge, hasDay, NUDGE_DAYS, saveCheckinNudge, toggleDay, WEEKDAYS,
  type CheckinNudge,
} from "@/lib/coach-chat";
import styles from "./coach-fighter-contact.module.css";

export function CoachFighterContact({ token, athleteId, name, age, healthConsent }: {
  token: string; athleteId: string; name: string; age: number | null | undefined; healthConsent: boolean;
}) {
  const [saved, setSaved] = useState<CheckinNudge | null | undefined>(undefined);
  const [days, setDays] = useState(WEEKDAYS);
  const [time, setTime] = useState("08:00");
  const [enabled, setEnabled] = useState(true);
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");
  const [error, setError] = useState("");
  const first = name.split(" ")[0] || "This fighter";
  const blocked = age != null && age < 18
    ? "Check-in nudges are for fighters aged 18 and over."
    : !healthConsent ? `${first} has not allowed health-data use, so check-ins can't be requested.` : "";

  useEffect(() => {
    const controller = new AbortController();
    fetchCheckinNudge(token, athleteId, controller.signal).then(row => {
      setSaved(row);
      if (row) { setDays(row.days); setTime(row.local_time.slice(0, 5)); setEnabled(row.enabled); }
    }).catch(() => { if (!controller.signal.aborted) setSaved(null); });
    return () => controller.abort();
  }, [token, athleteId]);

  async function save(nextEnabled = enabled) {
    setState("saving");
    setError("");
    try {
      const row = await saveCheckinNudge(token, athleteId, days, time, nextEnabled);
      setSaved(row);
      setEnabled(row.enabled);
      setState("saved");
    } catch (e) {
      setState("idle");
      setError(e instanceof Error ? e.message : "Not saved. Please retry.");
    }
  }

  const changed = !saved || saved.days !== days || saved.local_time.slice(0, 5) !== time || saved.enabled !== enabled;
  return (
    <section className={styles.panel} aria-label="Contact and check-ins">
      <div className={styles.head}>
        <div>
          <p className={styles.kicker}>Check-in nudge</p>
          <p className={styles.status}>
            {saved === undefined ? "Loading…" : saved?.enabled
              ? `${describeDays(saved.days)} at ${saved.local_time.slice(0, 5)}, their time`
              : "Off"}
          </p>
        </div>
        <Link className="cta" href={chatLink("coach", athleteId)}>Message</Link>
      </div>
      {blocked ? <p className={styles.note}>{blocked}</p> : (
        <>
          <div className={styles.days} role="group" aria-label="Nudge days">
            {NUDGE_DAYS.map((day, index) => (
              <button key={day} type="button" aria-pressed={hasDay(days, index)}
                onClick={() => { const next = toggleDay(days, index); if (next) { setDays(next); setState("idle"); } }}>
                {day.slice(0, 2)}
              </button>
            ))}
            <button type="button" className={styles.preset} onClick={() => { setDays(days === EVERY_DAY ? WEEKDAYS : EVERY_DAY); setState("idle"); }}>
              {days === EVERY_DAY ? "Weekdays" : "Every day"}
            </button>
          </div>
          <div className={styles.controls}>
            <label className={styles.time}>
              <span>Time</span>
              <input type="time" min="05:00" max="21:00" step={900} value={time}
                onChange={event => { setTime(event.target.value); setState("idle"); }} />
            </label>
            <label className={styles.toggle}>
              <input type="checkbox" checked={enabled} onChange={event => { setEnabled(event.target.checked); setState("idle"); }} />
              <span>{enabled ? "On" : "Off"}</span>
            </label>
            <button type="button" className="ghost-button" disabled={!changed || state === "saving"} onClick={() => void save()}>
              {state === "saving" ? "Saving…" : state === "saved" && !changed ? "Saved" : "Save"}
            </button>
          </div>
          <p className={styles.note}>
            Only sent if {first} hasn&apos;t checked in that day, in their time zone and outside their quiet hours.
            You&apos;ll see &ldquo;Missed check-in&rdquo; in Chats when they don&apos;t.
          </p>
        </>
      )}
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
    </section>
  );
}
