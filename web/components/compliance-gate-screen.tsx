"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useId, useState } from "react";

import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { recordCompliance } from "@/lib/api";
import { getAuthenticatedLandingHref } from "@/lib/auth-routing";
import {
  hasHealthDataConsent,
  consentCopyForBand,
  provisionalAgeBand,
  requiresComplianceAcceptance,
  validateDateOfBirth,
} from "@/lib/compliance";
import { useFadeNavigate } from "@/components/fade-through";

/**
 * The consent step for an account that does not already have one on record.
 *
 * A signup completed through the current form arrives here already satisfied and
 * passes straight through. It exists for the accounts that predate the consent
 * fields, and for a session that lost its acceptance mid-signup — those athletes
 * would otherwise be stuck against a 403 from the onboarding gate with no way to
 * clear it.
 *
 * Deliberately shaped like the private-trial gate it sits next to: same profile-
 * backed marker, same "redirect once satisfied" fail-safe.
 */
function ComplianceAcceptance() {
  const router = useRouter();
  const choice = useSearchParams().get("account_type");
  const fadeNavigate = useFadeNavigate();
  const { me, session, replaceMe, signOut } = useAppSession();
  const headingId = useId();
  const [dateOfBirth, setDateOfBirth] = useState("");
  const [acceptedTerms, setAcceptedTerms] = useState(false);
  const [healthDataConsent, setHealthDataConsent] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const token = session?.access_token ?? "";
  const role = me?.profile.role;
  const needsDateOfBirth = !me?.profile.date_of_birth || !me?.profile.meets_minimum_age;
  const needsTerms = !me?.profile.terms_accepted;
  const needsHealthConsent = !hasHealthDataConsent(me);
  const isPending = requiresComplianceAcceptance(me);
  const dateOfBirthError = needsDateOfBirth ? validateDateOfBirth(dateOfBirth) : null;
  const consentCopy = consentCopyForBand(provisionalAgeBand(dateOfBirth || me?.profile.date_of_birth || ""));
  const canContinue =
    !isSubmitting &&
    !dateOfBirthError &&
    (!needsTerms || acceptedTerms);

  // A non-athlete has no onboarding to gate; send them to their own workspace
  // rather than stranding them on a screen whose only action does not apply.
  useEffect(() => {
    if (role && role !== "athlete") {
      router.replace("/");
    }
  }, [role, router]);

  // Once the durable profile markers exist this route must never gate again —
  // protecting against stale links, bookmarks and a double submit.
  useEffect(() => {
    if (role === "athlete" && !isPending) {
      router.replace(getAuthenticatedLandingHref(me));
    }
  }, [isPending, me, role, router]);

  async function submit() {
    if (isSubmitting) {
      return;
    }
    if (dateOfBirthError) {
      setError(dateOfBirthError);
      return;
    }
    if (needsTerms && !acceptedTerms) {
      setError("Accept the Terms of Use to continue.");
      return;
    }
    setIsSubmitting(true);
    setError(null);
    try {
      const nextMe = await recordCompliance(token, {
        ...(choice === "coach" || choice === "athlete" ? { account_type: choice } : {}),
        ...(needsDateOfBirth ? { date_of_birth: dateOfBirth } : {}),
        ...(needsTerms ? { accept_terms: acceptedTerms } : {}),
        // Only sent when ticked. Leaving it unticked must not be recorded as a
        // withdrawal — the athlete has not consented yet, which is a different
        // fact from having consented and changed their mind.
        ...(needsHealthConsent && healthDataConsent ? { health_data_consent: true } : {}),
      });
      replaceMe(nextMe);
      fadeNavigate(() => router.replace(getAuthenticatedLandingHref(nextMe)));
    } catch (submitError) {
      setError(
        submitError instanceof Error
          ? submitError.message
          : "Your details could not be saved. Try again.",
      );
      setIsSubmitting(false);
    }
  }

  return (
    <section className="panel private-trial-panel" aria-labelledby={headingId}>
      <p className="kicker">Before you start</p>
      <h1 id={headingId}>Finish your account</h1>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      {needsDateOfBirth ? (
        <div className="field">
          <label htmlFor="complianceDateOfBirth">Date of birth</label>
          <input
            id="complianceDateOfBirth"
            type="date"
            autoComplete="bday"
            value={dateOfBirth}
            onChange={(event) => setDateOfBirth(event.target.value)}
            required
            aria-invalid={Boolean(dateOfBirth && dateOfBirthError)}
            aria-describedby={dateOfBirth && dateOfBirthError ? "compliance-dob-error" : undefined}
          />
          {dateOfBirth && dateOfBirthError ? (
            <p id="compliance-dob-error" className="error-banner" role="alert">{dateOfBirthError}</p>
          ) : null}
        </div>
      ) : null}

      {needsTerms ? (
        <div className="field auth-consent-field">
          <label className="auth-consent-row" htmlFor="complianceTerms">
            <input
              id="complianceTerms"
              type="checkbox"
              checked={acceptedTerms}
              onChange={(event) => setAcceptedTerms(event.target.checked)}
            />
            <span>
              I accept the{" "}
              <Link href="/terms" className="auth-text-link" target="_blank">
                Terms of Use
              </Link>
              .
            </span>
          </label>
          <div className="auth-consent-help">
            <Link href="/privacy" className="auth-text-link" target="_blank">
              Privacy Notice
            </Link>
          </div>
        </div>
      ) : null}

      {needsHealthConsent && choice !== "coach" ? (
        <div className="field auth-consent-field">
          <label className="auth-consent-row" htmlFor="complianceHealthConsent">
            <input
              id="complianceHealthConsent"
              type="checkbox"
              checked={healthDataConsent}
              onChange={(event) => setHealthDataConsent(event.target.checked)}
            />
            <span>{consentCopy.signupHealthConsentLabel}</span>
          </label>
          <p className="muted auth-consent-help">{consentCopy.signupHealthConsentHelp}</p>
          <p className="muted auth-consent-help">{consentCopy.declineNote}</p>
          <Link href="/privacy" className="auth-text-link" target="_blank">Privacy Notice</Link>
        </div>
      ) : null}

      <div className="private-trial-actions">
        <button type="button" className="cta" onClick={() => void submit()} disabled={!canContinue}>
          {isSubmitting ? "Saving…" : "CONTINUE"}
        </button>
        <button type="button" className="auth-text-link auth-magic-link-action" disabled={isSubmitting} onClick={async () => { await signOut(); router.replace("/login"); }}>
          Sign out
        </button>
      </div>
    </section>
  );
}

export function ComplianceGateScreen() {
  return (
    <RequireAuth>
      <ComplianceAcceptance />
    </RequireAuth>
  );
}
