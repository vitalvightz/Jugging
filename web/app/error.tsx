"use client";

import * as Sentry from "@sentry/nextjs";
import Image from "next/image";
import { useEffect } from "react";
import styles from "@/components/connection-recovery-card.module.css";
import { useConnectionRecovery } from "@/lib/use-connection-recovery";

import { isBrowserOffline, isNetworkFailure } from "@/lib/connectivity";

/**
 * Route-level crash boundary. Without it any render error — most commonly a
 * code-split chunk that could not download because the connection dropped
 * mid-navigation — fell through to Next's unbranded "Application error" page.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const connectionProblem = isNetworkFailure(error) || isBrowserOffline();
  const { retrying, retryFailed, retryConnection } = useConnectionRecovery(connectionProblem);

  useEffect(() => {
    // Connection drops are expected field conditions, not bugs.
    if (!connectionProblem) {
      Sentry.captureException(error);
    }
  }, [connectionProblem, error]);

  function retry() {
    if (connectionProblem) void retryConnection();
    else reset();
  }

  return (
    <section className={styles.card} role="alert">
      <div className={styles.header}>
        <Image src="/brand/unlxck-one-angle-48.png" alt="" unoptimized width={28} height={28} />
        <p className={styles.label}>{connectionProblem ? "CONNECTION" : "UNLXCK"}</p>
      </div>
      <h1><span className={styles.brand}>UNLXCK</span><span className={styles.headline}>{connectionProblem ? "your connection." : "hit a snag."}</span></h1>
      <p className={styles.description}>
        {connectionProblem
          ? "Your plan and logs are safe. Reconnect to pick up where you left off."
          : "Your data is safe. Try again, or head back to your dashboard."}
      </p>
      {retryFailed ? <p className={styles.description} role="status">Still can’t reach UNLXCK. Try again shortly.</p> : null}
      <div>
        <button type="button" className={styles.retry} onClick={retry} disabled={retrying}>
          {retrying ? "Checking connection…" : "Try again"}<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 12 12 4M4 4h8v8" stroke="currentColor" strokeWidth="1.5" /></svg>
        </button>
        {/* Plain anchor: a full navigation is the reliable path out of a broken route. */}
        <a href="/dashboard" className={styles.secondary}>
          Back to dashboard
        </a>
      </div>
    </section>
  );
}
