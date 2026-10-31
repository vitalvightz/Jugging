"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAppSession } from "./auth-provider";
import { updateMe } from "@/lib/api";
import { getAuthenticatedLandingHref, needsStartingPath } from "@/lib/auth-routing";
import { requiresComplianceAcceptance } from "@/lib/compliance";
import { athleteSport, sportLabel, TRAINING_SPORTS, type TrainingSport } from "@/lib/training-disciplines";
import styles from "./starting-path-screen.module.css";
import { useFadeNavigate } from "@/components/fade-through";

export function StartingPathScreen() {
  const { session, me, isMeHydrated, hasTransientMeError, replaceMe, refreshMe, signOut } = useAppSession();
  const router = useRouter();
  const fadeNavigate = useFadeNavigate();
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSigningOut, setSigningOut] = useState(false);
  const locked = useRef(false);
  const ready = isMeHydrated && me && !hasTransientMeError;
  // The sport is part of the same choice; "other" logs without one. A sport already on the profile is kept.
  const known = athleteSport(me?.profile.technical_style);
  const [picked, setPicked] = useState<TrainingSport | "other" | null>(null);
  const sport = picked ?? known;
  const sportGroup = useRef<HTMLDivElement>(null);
  const hintId = useId();
  const mustAccept = requiresComplianceAcceptance(me);

  useEffect(() => {
    if (ready && (mustAccept || !needsStartingPath(me))) {
      router.replace(getAuthenticatedLandingHref(me));
    }
  }, [ready, mustAccept, me, router]);

  async function choose(path: "tracking_first" | "plan_first") {
    if (locked.current || isSigningOut || !session || !ready || mustAccept) return;
    if (!sport) {
      setError("Pick your sport first.");
      sportGroup.current?.querySelector("button")?.focus();
      return;
    }
    locked.current = true;
    setSaving(path); setError(null);
    try {
      const result = await updateMe(session.access_token, {
        starting_path: path, ...(sport && sport !== "other" && sport !== known ? { technical_style: [sport] } : {}),
      });
      if (result.profile.starting_path !== path) throw new Error("Your choice could not be saved. Try again.");
      replaceMe(result);
      fadeNavigate(() => router.replace(path === "tracking_first" ? "/" : "/quick-build"));
    } catch {
      setError("Your choice could not be saved. Try again.");
      locked.current = false; setSaving(null);
    }
  }

  if (!ready) return <section className="panel" aria-busy={!hasTransientMeError}>
    <h1>{hasTransientMeError ? "Couldn’t load your account" : "Unlxcking your account…"}</h1>
    {hasTransientMeError ? <button className="secondary-button" onClick={() => void refreshMe()}>Retry</button> : null}
  </section>;
  if (mustAccept || !needsStartingPath(me)) return <p role="status">Opening your workspace…</p>;

  return <section className={styles.root} aria-labelledby="starting-heading">
    <header className={styles.brand}><span>UNLXCK</span><button className={styles.accountAction} type="button" disabled={Boolean(saving) || isSigningOut} onClick={async () => {
      setSigningOut(true);
      await signOut();
      router.replace("/login");
    }}>{isSigningOut ? "Signing out…" : "Sign out"}</button></header>
    <div className={styles.intro}>
      <h1 id="starting-heading">How would you like to start?</h1>
      <p>Pick your sport, then how you&apos;ll use UNLXCK.</p>
    </div>
    <p className={styles.label} id={`${hintId}-sport`}>Your sport</p>
    <div className={styles.sports} role="group" aria-labelledby={`${hintId}-sport`} ref={sportGroup}>
      {[...TRAINING_SPORTS, "other" as const].map(value => <button key={value} className={styles.sport} type="button"
        aria-pressed={sport === value} disabled={Boolean(saving) || isSigningOut}
        onClick={() => { setPicked(value); setError(null); }}>{value === "other" ? "Other" : sportLabel(value)}</button>)}
    </div>
    <p className={styles.label}>Start with</p>
    <div className={styles.choices} aria-busy={Boolean(saving)}>
      <button className={styles.choice} type="button" disabled={Boolean(saving) || isSigningOut} onClick={() => void choose("plan_first")}>
        <span className={styles.copy}><strong>Generate my S&amp;C plan</strong><span>Personalised S&amp;C around your sport, schedule and goals.</span></span>
        <span className={styles.action} aria-hidden="true">{saving === "plan_first" ? "Saving…" : "›"}</span>
      </button>
      <button className={styles.choice} type="button" disabled={Boolean(saving) || isSigningOut} onClick={() => void choose("tracking_first")}>
        <span className={styles.copy}><strong>I already have a coach or plan</strong><span>Track your training without building a programme.</span></span>
        <span className={styles.action} aria-hidden="true">{saving === "tracking_first" ? "Saving…" : "›"}</span>
      </button>
    </div>
    {error ? <p role="alert" className={styles.error}>{error}</p> : null}
  </section>;
}
