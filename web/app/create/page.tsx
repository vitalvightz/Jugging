"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { RequireAuth } from "@/components/auth-guard";
import { useFadeNavigate } from "@/components/fade-through";
import { hasInternalPreviousPage } from "@/lib/internal-back";
import styles from "./create.module.css";

// The one place to start any kind of plan, reached from + Create. Each option
// opens its existing flow behind the same fade through black; this screen only
// routes, it saves nothing.
const OPTIONS = [
  { href: "/quick-build", title: "Quick Build", body: "A personalised programme in minutes." },
  { href: "/onboarding", title: "Full Camp Setup", body: "A detailed programme around your fight and schedule." },
  { href: "/plans/custom", title: "Build Your Own", body: "Create a programme or add the one you already follow." },
] as const;

export default function CreatePage() {
  const router = useRouter();
  const fadeNavigate = useFadeNavigate();

  function goBack() {
    const internal = hasInternalPreviousPage({
      documentUrl: performance.getEntriesByType("navigation")[0]?.name,
      currentUrl: window.location.href,
      referrer: document.referrer,
    });
    if (internal) router.back();
    else router.push("/");
  }

  return (
    <RequireAuth>
      <section className={styles.root} aria-labelledby="create-heading">
        <div className={styles.top}>
          <button className={styles.back} type="button" onClick={goBack}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5m6-6-6 6 6 6" /></svg>
            Back
          </button>
          <p className={styles.trail} aria-hidden="true">UNLXCK / <span>Create</span></p>
        </div>
        <header className={styles.intro}>
          <p className="kicker">Your next move</p>
          <h1 id="create-heading">What would you like to create?</h1>
          <p>Choose how you want to build your programme.</p>
        </header>
        <ol className={styles.options} aria-label="Create options">
          {OPTIONS.map((option, index) => (
            <li key={option.href}>
              <Link
                href={option.href}
                className={styles.option}
                onClick={(event) => {
                  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  fadeNavigate(() => router.push(option.href));
                }}
              >
                <span className={`kicker ${styles.index}`} aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
                <span className={styles.copy}><strong>{option.title}</strong><span>{option.body}</span></span>
                <span className={styles.arrow} aria-hidden="true">
                  <svg viewBox="0 0 24 24"><path d="M7 17 17 7M8 7h9v9" /></svg>
                </span>
              </Link>
            </li>
          ))}
        </ol>
      </section>
    </RequireAuth>
  );
}
