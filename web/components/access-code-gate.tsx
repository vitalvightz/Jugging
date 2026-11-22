"use client";

import Image from "next/image";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type CSSProperties, type ReactNode, useActionState, useEffect, useRef, useState } from "react";

import { type AccessCodeState, submitAccessCode } from "@/app/signup/actions";
import { UnlockGlitchText } from "@/components/unlock-glitch-text";
import { usePrefersReducedMotion } from "@/lib/reduced-motion";

import authStyles from "./auth-form.module.css";
import styles from "./access-code-gate.module.css";

type CodeShape = { length: number; numeric: boolean };

// Invites are requested by Instagram DM; ig.me/m opens a DM thread directly
// (in the app on phones, the inbox on desktop).
const INSTAGRAM_HANDLE = "unlxckperformance";
const INSTAGRAM_DM_HREF = `https://ig.me/m/${INSTAGRAM_HANDLE}`;

// How long "Access granted" holds before the account form takes over.
const GRANTED_HOLD_MS = 1600;

/**
 * /signup's invite-only front door. Shows the access-code screen until the
 * server grants access; when access arrives during this visit it holds the
 * "Access granted" moment briefly, then reveals `children` (the account form).
 */
export function SignupAccess({
  unlocked,
  codeShape,
  inviteRequired = false,
  children,
}: Readonly<{ unlocked: boolean; codeShape: CodeShape; inviteRequired?: boolean; children: ReactNode }>) {
  const reduceMotion = usePrefersReducedMotion();
  // Adjust state while rendering when access flips on (React's pattern for
  // reacting to a prop change), so the gate never flashes the form early.
  const [wasUnlocked, setWasUnlocked] = useState(unlocked);
  const [celebrating, setCelebrating] = useState(false);
  if (unlocked !== wasUnlocked) {
    setWasUnlocked(unlocked);
    if (unlocked) setCelebrating(true);
  }

  useEffect(() => {
    if (!celebrating) return;
    const timer = window.setTimeout(() => setCelebrating(false), reduceMotion ? 500 : GRANTED_HOLD_MS);
    return () => window.clearTimeout(timer);
  }, [celebrating, reduceMotion]);

  if (unlocked && !celebrating) return <>{children}</>;
  return <AccessCodeGate codeShape={codeShape} granted={unlocked} inviteRequired={inviteRequired} />;
}

function AccessCodeGate({
  codeShape,
  granted,
  inviteRequired,
}: Readonly<{ codeShape: CodeShape; granted: boolean; inviteRequired: boolean }>) {
  const t = useTranslations("AccessGate");
  const authT = useTranslations("Auth");
  const [state, formAction, isPending] = useActionState<AccessCodeState, FormData>(submitAccessCode, {
    status: "idle",
    attempt: 0,
  });
  const [code, setCode] = useState("");
  const [focused, setFocused] = useState(true);
  const formRef = useRef<HTMLFormElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Clear the boxes after each wrong code so the next try starts fresh.
  const [seenAttempt, setSeenAttempt] = useState(0);
  if (state.attempt !== seenAttempt) {
    setSeenAttempt(state.attempt);
    if (state.status === "error") setCode("");
  }
  const wrong = state.status === "error" && !isPending && code.length === 0;

  useEffect(() => {
    if (state.status === "error") inputRef.current?.focus();
  }, [state]);

  // Keystrokes that land before the page finishes loading go straight into
  // the input; pick them up once React takes over so nothing typed is lost.
  useEffect(() => {
    const early = inputRef.current?.value ?? "";
    if (early) handleChange(early);
    // Runs once on mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleChange(value: string) {
    const pattern = codeShape.numeric ? /[^0-9]/g : /[^0-9a-z]/gi;
    const next = value.replace(pattern, "").toUpperCase().slice(0, codeShape.length);
    setCode(next);
    // A full code submits itself, like a real keypad.
    if (next.length === codeShape.length && !isPending) {
      queueMicrotask(() => formRef.current?.requestSubmit());
    }
  }

  const activeSlot = Math.min(code.length, codeShape.length - 1);

  return (
    <section className={authStyles.layout} aria-labelledby="access-title">
      <div className={`auth-card ${authStyles.content}`} data-granted={granted || undefined}>
        <div className={styles.header}>
          <div className={styles.mark} data-state={granted ? "granted" : "locked"} aria-hidden="true">
            <Image src="/brand/unlxck-mark.webp" alt="" width={40} height={40} priority />
          </div>
          <p className={styles.badge}>
            {granted ? (
              <>
                <span className="sr-only">{t("granted")}</span>
                <UnlockGlitchText key="granted" text={t("granted")} durationMs={600} />
              </>
            ) : (
              t("badge")
            )}
          </p>
          <h1 id="access-title" className={authStyles.title}>
            {granted ? t("grantedTitle") : t("title")}
          </h1>
          <p className={`muted ${styles.lead}`} aria-live="polite">
            {granted ? t("grantedLead") : t("lead")}
          </p>
        </div>

        {/* Sent here after signing in with an account that has no invite yet. */}
        {inviteRequired && !granted ? (
          <p className={styles.notice} role="status">
            {t("inviteRequired")}
          </p>
        ) : null}

        <form ref={formRef} action={formAction} className={styles.form}>
          <label htmlFor="access-code" className={styles.label}>
            {t("label")}
          </label>
          <div
            key={wrong ? `wrong-${state.attempt}` : "slots"}
            className={styles.slots}
            data-wrong={wrong || undefined}
            data-granted={granted || undefined}
            style={{ gridTemplateColumns: `repeat(${codeShape.length}, minmax(0, 1fr))` }}
          >
            <input
              ref={inputRef}
              id="access-code"
              name="code"
              className={styles.slotInput}
              value={granted ? "•".repeat(codeShape.length) : code}
              onChange={(event) => handleChange(event.target.value)}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
              inputMode={codeShape.numeric ? "numeric" : "text"}
              pattern={codeShape.numeric ? "[0-9]*" : undefined}
              autoComplete="one-time-code"
              autoCapitalize="characters"
              spellCheck={false}
              maxLength={codeShape.length}
              required
              autoFocus
              readOnly={granted || isPending}
              aria-invalid={wrong || undefined}
              aria-describedby={wrong ? "access-code-error" : "access-code-hint"}
            />
            {Array.from({ length: codeShape.length }, (_, index) => (
              <span
                key={index}
                className={styles.slot}
                data-filled={granted || index < code.length || undefined}
                data-active={(focused && !granted && !isPending && index === activeSlot) || undefined}
                style={{ "--slot": index } as CSSProperties}
                aria-hidden="true"
              >
                {granted ? "" : code[index] ?? ""}
              </span>
            ))}
          </div>
          <p id="access-code-hint" className="sr-only">
            {t("hint", { length: codeShape.length })}
          </p>
          {wrong ? (
            <p id="access-code-error" className={styles.error} role="alert">
              {t("error")}
            </p>
          ) : null}
          {granted ? null : (
            <button type="submit" className={`cta ${styles.submit}`} disabled={isPending || code.length === 0}>
              {isPending ? t("checking") : t("submit")}
            </button>
          )}
        </form>

        {granted ? null : (
          <>
            {/* No public application: invites go out by Instagram DM. */}
            <div className={styles.apply}>
              <span className={styles.divider}>{t("noCode")}</span>
              <p className={styles.applyNote}>{t("dmUs")}</p>
              <a
                href={INSTAGRAM_DM_HREF}
                target="_blank"
                rel="noopener noreferrer"
                className={styles.instagram}
                aria-label={t("dmButton", { handle: INSTAGRAM_HANDLE })}
              >
                <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8">
                  <rect x="3" y="3" width="18" height="18" rx="5" />
                  <circle cx="12" cy="12" r="4.2" />
                  <circle cx="17.4" cy="6.6" r="1.1" fill="currentColor" stroke="none" />
                </svg>
              </a>
            </div>

            <Link href="/login" className={`auth-text-link ${authStyles.accountLink}`}>
              {authT("alreadyAccount")}
            </Link>
          </>
        )}
      </div>
    </section>
  );
}

