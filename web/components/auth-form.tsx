"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useTransition,
  type FormEvent,
  type ReactNode,
} from "react";

import { useAppSession } from "@/components/auth-provider";
import { PasswordStrengthMeter } from "@/components/password-strength-meter";
import { isTurnstileConfigured, TurnstileChallenge } from "@/components/turnstile-challenge";
import { getMe } from "@/lib/api";
import {
  AUTH_FEEDBACK,
  getLoginErrorMessage,
  getMagicLinkErrorMessage,
  getSignupCodeErrorMessage,
  getSignupCodeResendErrorMessage,
} from "@/lib/auth-feedback";
import { clearAuthLinkParams, readAuthLinkStatus } from "@/lib/auth-link";
import { getAuthenticatedLandingHref, getSafePlanReturnHref } from "@/lib/auth-routing";
import {
  TERMS_CONSENT_LEAD,
  TERMS_LINK_LABEL,
  consentCopyForBand,
  provisionalAgeBand,
  signupConsentBlockReason,
} from "@/lib/compliance";
import { PRIVACY_HREF, TERMS_HREF } from "@/lib/legal-documents";
import { recordCompliance } from "@/lib/api";
import { evaluatePasswordStrength } from "@/lib/password-strength";
import { buildAuthRedirectUrl } from "@/lib/site-url";
import { getSupabaseBrowserClient } from "@/lib/supabase";
import { hasSignupAdmission } from "@/lib/signup-invite-guard";
import type { Session } from "@supabase/supabase-js";

import type { UserRole } from "@/lib/types";
import styles from "./auth-form.module.css";
import { useFadeNavigate } from "@/components/fade-through";

/** How long Turnstile gets to pass on its own before the hint explains the disabled button. */
const CAPTCHA_HINT_DELAY_MS = 2500;

const CAPTCHA_REQUIRED_MESSAGE = "Complete the security check, then try again.";
const CAPTCHA_UNAVAILABLE_MESSAGE = "Security check failed to load. Refresh and try again.";

/** Supabase allows one confirmation email per address per minute. */
const SIGNUP_CODE_RESEND_COOLDOWN_S = 60;
/** Supabase's email OTP length is configurable from 6 to 10 digits. */
const SIGNUP_CODE_MIN_LENGTH = 6;
const SIGNUP_CODE_MAX_LENGTH = 10;

export function AuthForm({
  mode,
  footerSlot,
}: {
  mode: "signup" | "login";
  footerSlot?: ReactNode;
}) {
  const t = useTranslations("Auth");
  const router = useRouter();
  const fadeNavigate = useFadeNavigate();
  const { isReady, session, me, replaceMe } = useAppSession();
  const nextPlan = () => getSafePlanReturnHref(new URLSearchParams(window.location.search).get("next"));
  const destination = (account: Parameters<typeof getAuthenticatedLandingHref>[0]) =>
    getAuthenticatedLandingHref(account, nextPlan());
  const authRedirectPath = () => nextPlan() ? `/login?next=${encodeURIComponent(nextPlan()!)}` : "/login";
  const emailInputRef = useRef<HTMLInputElement | null>(null);
  const [showEmail, setShowEmail] = useState(false);
  const [isGooglePending, setIsGooglePending] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [dateOfBirth, setDateOfBirth] = useState("");
  const [acceptedTerms, setAcceptedTerms] = useState(false);
  const [healthDataConsent, setHealthDataConsent] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  // Set once signup succeeds without a session (email confirmation is on): the
  // form swaps to a one-time-code step so the athlete never has to leave the
  // page, follow a link and log in again.
  const [verificationEmail, setVerificationEmail] = useState<string | null>(null);
  const [verificationCode, setVerificationCode] = useState("");
  const [resendCooldown, setResendCooldown] = useState(0);
  const [isVerifyPending, startVerifyTransition] = useTransition();
  const [isResendPending, startResendTransition] = useTransition();
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  const [captchaResetKey, setCaptchaResetKey] = useState(0);
  const [isPending, startTransition] = useTransition();
  const [isMagicLinkPending, startMagicLinkTransition] = useTransition();
  const passwordStrength = evaluatePasswordStrength(password, { email });
  const requiresCaptcha = isTurnstileConfigured();
  const isSignupPasswordBlocked = mode === "signup" && !passwordStrength.isAcceptable;
  const isCaptchaBlocked = requiresCaptcha && !captchaToken;
  // Turnstile usually passes on its own within a few seconds; only explain the
  // disabled button once it has clearly stalled, so an auto-pass never flashes
  // the hint. Once due it stays due, so a later expiry explains itself at once.
  const [captchaHintDue, setCaptchaHintDue] = useState(false);
  useEffect(() => {
    if (!isCaptchaBlocked || captchaHintDue) return;
    const timer = window.setTimeout(() => setCaptchaHintDue(true), CAPTCHA_HINT_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [isCaptchaBlocked, captchaHintDue]);
  // Client-side courtesy checks only. The backend and a Postgres trigger both
  // reject an under-13 signup, and the server stamps every consent record, so
  // disabling the button here is about giving a clear message — not about being
  // the control.
  const signupBlockReason =
    mode === "signup" ? signupConsentBlockReason({ dateOfBirth, acceptedTerms }) : null;
  // Health-data consent is deliberately NOT part of this condition. Making it a
  // precondition of creating an account would make it conditional, and consent
  // that is a condition of the service is not freely given (UK GDPR Art. 7(4)) —
  // which would invalidate the very Article 9 basis it exists to establish. An
  // athlete can decline it, keep their account, and give it later from Settings.
  const isSignupConsentBlocked = signupBlockReason !== null;
  // Wording only. The band is derived locally from the date being typed so the
  // explanation matches the reader before the profile exists; the server
  // re-derives it from the stored date and owns every actual consequence.
  const consentCopy = consentCopyForBand(provisionalAgeBand(dateOfBirth));

  useEffect(() => {
    if (resendCooldown <= 0) return;
    const timer = window.setTimeout(() => setResendCooldown((seconds) => seconds - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [resendCooldown]);

  const handleCaptchaUnavailable = useCallback(() => {
    setError(CAPTCHA_UNAVAILABLE_MESSAGE);
  }, []);

  function resetCaptcha() {
    if (!requiresCaptcha) {
      return;
    }
    setCaptchaToken(null);
    setCaptchaResetKey((current) => current + 1);
  }

  useEffect(() => {
    if (!isReady) {
      return;
    }
    if (session && me) {
      router.replace(destination(me));
    }
  }, [isReady, me, router, session]);

  useEffect(() => {
    if (showEmail) {
      emailInputRef.current?.focus();
    }
  }, [showEmail]);

  useEffect(() => {
    const linkStatus = readAuthLinkStatus({
      hash: window.location.hash,
      search: window.location.search,
    });
    if (linkStatus.kind === "error") {
      setError(linkStatus.message);
      clearAuthLinkParams();
    }
  }, []);

  /**
   * Records the signup acceptances against the new session so the evidence is
   * server-stamped and versioned, then opens the starting choice. Shared by the
   * immediate-session signup and the one-time-code step. If recording fails the
   * account still exists but is not consented, and the compliance gate catches
   * it on the next screen — so a failure here is surfaced, not swallowed.
   */
  async function finishSignup(newSession: Session) {
    try {
      const nextMe = await recordCompliance(newSession.access_token, {
        date_of_birth: dateOfBirth,
        accept_terms: true,
        // Only sent when ticked. Sending `false` would record a
        // withdrawal, and someone who never consented has not withdrawn —
        // those are different facts and the audit trail keeps them apart.
        ...(healthDataConsent ? { health_data_consent: true } : {}),
      });
      replaceMe(nextMe);
      fadeNavigate(() => router.replace(destination(nextMe)));
    } catch (complianceError) {
      setError(
        complianceError instanceof Error
          ? complianceError.message
          : "Your account was created but your consent could not be saved. Sign in to finish setup.",
      );
      return;
    }
  }

  async function handleGoogle() {
    if (isGooglePending) return;
    setError(null);
    setMessage(null);
    setIsGooglePending(true);
    try {
      const { error: oauthError } = await getSupabaseBrowserClient().auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: buildAuthRedirectUrl(authRedirectPath()) },
      });
      if (oauthError) throw oauthError;
      // Keep the button busy until navigation. The existing AuthProvider consumes
      // the callback session, resolves /me and uses the same landing-route gate.
    } catch {
      setError(t("googleError"));
      setIsGooglePending(false);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    setError(null);

    if (mode === "signup" && !passwordStrength.isAcceptable) {
      setError(passwordStrength.feedback);
      return;
    }

    if (signupBlockReason) {
      setError(signupBlockReason);
      return;
    }

    const captchaTokenForRequest = requiresCaptcha ? captchaToken ?? undefined : undefined;
    if (requiresCaptcha && !captchaTokenForRequest) {
      setError(CAPTCHA_REQUIRED_MESSAGE);
      return;
    }

    startTransition(async () => {
      let client;
      try {
        client = getSupabaseBrowserClient();
      } catch {
        setError(AUTH_FEEDBACK.connectionFailure);
        return;
      }

      if (mode === "signup") {
        // Athlete is the only live role, so signup does not ask. Coach and gym
        // owner accounts are assigned by an admin when those workspaces ship.
        const selectedRole: UserRole = "athlete";
        let signUpResult;
        try {
          signUpResult = await client.auth.signUp({
            email,
            password,
            options: {
              captchaToken: captchaTokenForRequest,
              emailRedirectTo: buildAuthRedirectUrl(authRedirectPath()),
              data: {
                // Seeds the profile's date of birth on first sign-in and lets a
                // Postgres trigger reject an under-13 signup even when the
                // caller bypasses this form. The server re-reads and re-derives
                // the age band; nothing trusts this value as an age claim.
                date_of_birth: dateOfBirth,
                role: selectedRole,
              },
            },
          });
        } catch {
          resetCaptcha();
          setError(AUTH_FEEDBACK.connectionFailure);
          return;
        }

        const { data, error: signUpError } = signUpResult;
        if (signUpError) {
          resetCaptcha();
          setError(signUpError.message);
          return;
        }
        // A Turnstile token is single-use, so the widget must be reset whenever
        // the athlete stays on this form and may submit again. It is NOT reset
        // when a session came back: the form is about to navigate away, and
        // resetting there re-runs the human check on screen while the redirect
        // is still in flight.
        if (data.session) {
          await finishSignup(data.session);
          return;
        }
        resetCaptcha();
        setVerificationCode("");
        setVerificationEmail(email.trim());
        setResendCooldown(SIGNUP_CODE_RESEND_COOLDOWN_S);
        return;
      }

      let loginResult;
      try {
        loginResult = await client.auth.signInWithPassword({
          email,
          password,
          options: { captchaToken: captchaTokenForRequest },
        });
      } catch {
        resetCaptcha();
        setError(AUTH_FEEDBACK.connectionFailure);
        return;
      }

      const { data, error: loginError } = loginResult;
      if (loginError) {
        resetCaptcha();
        setError(getLoginErrorMessage(loginError));
        return;
      }
      // Signed in: leave the widget alone. Resetting here made it verify a
      // second time, with the "complete the human check" hint back on screen,
      // for as long as the profile lookup and redirect below took.

      const accessToken = data.session?.access_token ?? null;
      if (!accessToken) {
        fadeNavigate(() => router.replace("/plans"));
        return;
      }

      try {
        if (!(await hasSignupAdmission(data.session?.user.id ?? data.user?.id))) {
          await client.auth.signOut();
          router.replace("/signup?invite=required");
          return;
        }
      } catch {
        resetCaptcha();
        setError("We couldn't verify your account access. Please try again.");
        return;
      }
      const nextMe = await getMe(accessToken).catch(() => null);
      fadeNavigate(() => router.replace(nextMe ? destination(nextMe) : "/plans"));
    });
  }

  function handleVerifyCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!verificationEmail) return;
    setMessage(null);
    setError(null);
    const token = verificationCode.trim();

    startVerifyTransition(async () => {
      let result;
      try {
        result = await getSupabaseBrowserClient().auth.verifyOtp({
          email: verificationEmail,
          token,
          type: "email",
        });
      } catch {
        setError(AUTH_FEEDBACK.connectionFailure);
        return;
      }
      const { data, error: verifyError } = result;
      if (verifyError || !data.session) {
        setError(verifyError ? getSignupCodeErrorMessage(verifyError, t("invalidCode")) : t("invalidCode"));
        return;
      }
      await finishSignup(data.session);
    });
  }

  function handleResendCode() {
    if (!verificationEmail) return;
    setMessage(null);
    setError(null);
    const captchaTokenForRequest = requiresCaptcha ? captchaToken ?? undefined : undefined;
    if (requiresCaptcha && !captchaTokenForRequest) {
      setError(CAPTCHA_REQUIRED_MESSAGE);
      return;
    }

    startResendTransition(async () => {
      let result;
      try {
        result = await getSupabaseBrowserClient().auth.resend({
          type: "signup",
          email: verificationEmail,
          options: {
            captchaToken: captchaTokenForRequest,
            emailRedirectTo: buildAuthRedirectUrl(authRedirectPath()),
          },
        });
      } catch {
        resetCaptcha();
        setError(AUTH_FEEDBACK.connectionFailure);
        return;
      }
      resetCaptcha();
      if (result.error) {
        setError(getSignupCodeResendErrorMessage(result.error));
        return;
      }
      setResendCooldown(SIGNUP_CODE_RESEND_COOLDOWN_S);
      setMessage(t("codeResent"));
    });
  }

  function handleUseDifferentEmail() {
    setVerificationEmail(null);
    setVerificationCode("");
    setResendCooldown(0);
    setMessage(null);
    setError(null);
  }

  function handleMagicLink() {
    setMessage(null);
    setError(null);
    const trimmedEmail = email.trim();
    if (!trimmedEmail) {
      setError("Enter your email above, then request a sign-in link.");
      return;
    }

    const captchaTokenForRequest = requiresCaptcha ? captchaToken ?? undefined : undefined;
    if (requiresCaptcha && !captchaTokenForRequest) {
      setError(CAPTCHA_REQUIRED_MESSAGE);
      return;
    }

    startMagicLinkTransition(async () => {
      let client;
      try {
        client = getSupabaseBrowserClient();
      } catch {
        setError(AUTH_FEEDBACK.connectionFailure);
        return;
      }
      const redirectTo = buildAuthRedirectUrl(authRedirectPath());
      let magicLinkResult;
      try {
        magicLinkResult = await client.auth.signInWithOtp({
          email: trimmedEmail,
          options: {
            captchaToken: captchaTokenForRequest,
            shouldCreateUser: mode === "signup",
            emailRedirectTo: redirectTo,
          },
        });
      } catch {
        resetCaptcha();
        setError(AUTH_FEEDBACK.connectionFailure);
        return;
      }
      resetCaptcha();

      const { error: otpError } = magicLinkResult;
      if (otpError) {
        setError(getMagicLinkErrorMessage(otpError));
        return;
      }
      setMessage(AUTH_FEEDBACK.magicLinkSent);
    });
  }

  return (
    <section className={styles.layout} aria-labelledby="auth-title">
      <div className={`auth-card ${styles.content}`}>
        <div className="auth-header">
          <div>
            <p className={`kicker ${styles.brand}`}>UNLXCK</p>
            <h1 id="auth-title" className={styles.title}>{verificationEmail ? t("verifyTitle") : mode === "signup" ? t("signupTitle") : t("welcomeBack")}</h1>
            {mode === "signup" && !verificationEmail ? (
              <div className={styles.signupIntro}>
                <p className="muted">{t("signupSummary")}</p>
                <p>{t("signupAccess")}</p>
              </div>
            ) : null}
          </div>
        </div>

        {message ? (
          <div className="success-banner" role="status" aria-live="polite" aria-atomic="true">
            {message}
          </div>
        ) : null}
        {error ? (
          <div className="error-banner" role="alert" aria-live="assertive" aria-atomic="true">
            {error}
          </div>
        ) : null}

        {verificationEmail ? (
          <form onSubmit={handleVerifyCode} className="auth-form-grid" aria-describedby="verificationHelp">
            <p id="verificationHelp" className="muted auth-verify-help">
              {t("verifyBody", { email: verificationEmail })}
            </p>
            <div className="field">
              <label htmlFor="verificationCode">{t("codeLabel")}</label>
              <input
                id="verificationCode"
                name="code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern={`[0-9]{${SIGNUP_CODE_MIN_LENGTH},${SIGNUP_CODE_MAX_LENGTH}}`}
                maxLength={SIGNUP_CODE_MAX_LENGTH}
                className="auth-verify-code"
                value={verificationCode}
                onChange={(event) => setVerificationCode(event.target.value.replace(/\D/g, ""))}
                autoFocus
                required
              />
              <p className="muted auth-verify-hint">{t("linkFallback")}</p>
            </div>

            <TurnstileChallenge
              action="signup"
              onTokenChange={setCaptchaToken}
              onUnavailable={handleCaptchaUnavailable}
              resetKey={captchaResetKey}
            />

            <div className="form-actions auth-form-actions">
              <button
                type="submit"
                className="cta"
                disabled={isVerifyPending || verificationCode.length < SIGNUP_CODE_MIN_LENGTH}
              >
                {isVerifyPending ? t("verifying") : t("verifyAction")}
              </button>
              <div className="auth-secondary-links">
                <button
                  type="button"
                  className="auth-text-link auth-magic-link-action"
                  onClick={handleResendCode}
                  disabled={isResendPending || resendCooldown > 0 || isCaptchaBlocked}
                >
                  {resendCooldown > 0 ? t("resendIn", { seconds: resendCooldown }) : t("resendCode")}
                </button>
                <button
                  type="button"
                  className="auth-text-link auth-magic-link-action"
                  onClick={handleUseDifferentEmail}
                >
                  {t("differentEmail")}
                </button>
              </div>
            </div>
          </form>
        ) : !showEmail ? (
          <div className={styles.choices} aria-busy={isGooglePending}>
            <button type="button" className={styles.google} onClick={() => void handleGoogle()} disabled={!isReady || isGooglePending}>
              <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24">
                <path fill="#4285F4" d="M21.6 12.23c0-.71-.06-1.39-.18-2.05H12v3.88h5.38a4.6 4.6 0 0 1-2 3.02v2.51h3.24c1.9-1.75 2.98-4.33 2.98-7.36Z" />
                <path fill="#34A853" d="M12 22c2.7 0 4.96-.9 6.62-2.41l-3.24-2.51c-.9.6-2.05.97-3.38.97-2.6 0-4.8-1.76-5.59-4.12H3.07v2.59A10 10 0 0 0 12 22Z" />
                <path fill="#FBBC05" d="M6.41 13.93A6 6 0 0 1 6.1 12c0-.67.11-1.32.31-1.93V7.48H3.07A10 10 0 0 0 2 12c0 1.61.38 3.14 1.07 4.52l3.34-2.59Z" />
                <path fill="#EA4335" d="M12 5.95c1.47 0 2.79.51 3.83 1.51l2.87-2.87A9.6 9.6 0 0 0 12 2a10 10 0 0 0-8.93 5.48l3.34 2.59C7.2 7.71 9.4 5.95 12 5.95Z" />
              </svg>
              {isGooglePending ? t("connectingGoogle") : t("continueGoogle")}
            </button>
            <button type="button" className={`secondary-button ${styles.email}`} disabled={!isReady || isGooglePending} onClick={() => { setShowEmail(true); setError(null); setMessage(null); }}>
              {t("continueEmail")}
            </button>
          </div>
        ) : (
        <form onSubmit={handleSubmit} className="auth-form-grid" id="email-auth-form">
          <button type="button" className={`auth-text-link ${styles.back}`} disabled={isPending || isMagicLinkPending} onClick={() => { setShowEmail(false); setError(null); setMessage(null); resetCaptcha(); }}>
            <span aria-hidden="true">← </span>{t("allOptions")}
          </button>
          <div className="field">
            <label htmlFor="email">{t("email")}</label>
            <input
              id="email"
              name="email"
              type="email"
              inputMode="email"
              autoComplete="email"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              ref={emailInputRef}
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="password">{t("password")}</label>
            <div className="password-field">
              <input
                id="password"
                name="password"
                type={showPassword ? "text" : "password"}
                autoComplete={mode === "signup" ? "new-password" : "current-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                minLength={8}
              />
              <button
                type="button"
                className="password-toggle"
                onClick={() => setShowPassword((prev) => !prev)}
                aria-label={showPassword ? t("hidePassword") : t("showPassword")}
                aria-pressed={showPassword}
              >
                {showPassword ? t("hide") : t("show")}
              </button>
            </div>
            {/* Shown once typing starts, so an empty form doesn't open on a
                "Too weak" verdict. */}
            {mode === "signup" && password ? <PasswordStrengthMeter strength={passwordStrength} /> : null}
          </div>

          {mode === "signup" ? (
            <>
              <div className="field">
                <label htmlFor="dateOfBirth">{t("dateOfBirth")}</label>
                <input
                  id="dateOfBirth"
                  name="bday"
                  type="date"
                  autoComplete="bday"
                  value={dateOfBirth}
                  onChange={(event) => setDateOfBirth(event.target.value)}
                  required
                />
              </div>

              <div className="field auth-consent-field">
                <label className="auth-consent-row" htmlFor="acceptTerms">
                  <input
                    id="acceptTerms"
                    name="acceptTerms"
                    type="checkbox"
                    checked={acceptedTerms}
                    onChange={(event) => setAcceptedTerms(event.target.checked)}
                    required
                  />
                  <span>
                    {TERMS_CONSENT_LEAD}{" "}
                    <Link href={TERMS_HREF} className="auth-text-link" target="_blank">
                      {TERMS_LINK_LABEL}
                    </Link>
                    .
                  </span>
                </label>
              </div>

              {/* Kept as its own affirmative tick, never folded into the Terms:
                  UK GDPR Art. 9(2)(a) consent has to be specific and separate,
                  and a bundled tick would not be valid consent at all. It is
                  also optional — see signupConsentBlockReason. */}
              <div className="field auth-consent-field">
                <label className="auth-consent-row" htmlFor="healthDataConsent">
                  <input
                    id="healthDataConsent"
                    name="healthDataConsent"
                    type="checkbox"
                    checked={healthDataConsent}
                    onChange={(event) => setHealthDataConsent(event.target.checked)}
                    aria-describedby="healthDataConsentHelp"
                  />
                  <span>{consentCopy.signupHealthConsentLabel}</span>
                </label>
                {/* The full explanation — what each category is used for, that
                    it is never shown to other users, what declining costs —
                    lives in the Privacy Notice linked here and in Settings →
                    Privacy. Repeating it inline buried the form on a phone. */}
                <p id="healthDataConsentHelp" className="muted auth-consent-help auth-consent-meta">
                  {consentCopy.signupHealthConsentHelp} ·{" "}
                  <Link href={PRIVACY_HREF} className="auth-text-link" target="_blank">
                    {t("privacyNotice")}
                  </Link>
                </p>
              </div>
            </>
          ) : null}

          <TurnstileChallenge
            action={mode}
            onTokenChange={setCaptchaToken}
            onUnavailable={handleCaptchaUnavailable}
            resetKey={captchaResetKey}
          />
          {isCaptchaBlocked && captchaHintDue ? (
            <p id="captchaHint" className="muted auth-captcha-hint" role="status">
              {t("captchaHint")}
            </p>
          ) : null}

          <div className="form-actions auth-form-actions">
            <button
              type="submit"
              className="cta"
              aria-describedby={isCaptchaBlocked && captchaHintDue ? "captchaHint" : undefined}
              disabled={
                isPending || isSignupPasswordBlocked || isSignupConsentBlocked || isCaptchaBlocked
              }
            >
              {isPending
                ? mode === "signup"
                  ? t("creatingAccount")
                  : t("signingIn")
                : mode === "signup"
                  ? t("createAccount")
                  : t("login")}
            </button>
            <button
              type="button"
              className="auth-text-link auth-magic-link-action"
              onClick={handleMagicLink}
              disabled={isPending || isMagicLinkPending || isCaptchaBlocked}
            >
              {isMagicLinkPending ? t("sendingLink") : t("emailLink")}
            </button>
            <div className="auth-secondary-links" aria-label={t("accountHelp")}>
              {mode === "login" ? (
                <Link href="/forgot-password" className="auth-text-link">
                  {t("forgotPassword")}
                </Link>
              ) : null}
            </div>
          </div>
        </form>
        )}

        {!verificationEmail ? (
          <Link href={mode === "signup" ? "/login" : "/signup"} className={`auth-text-link ${styles.accountLink}`}>
            {mode === "signup" ? t("alreadyAccount") : t("joinBeta")}
          </Link>
        ) : null}

        <div className={styles.legal}>
          <Link href={TERMS_HREF}>{TERMS_LINK_LABEL}</Link>
          <span aria-hidden="true">·</span>
          <Link href={PRIVACY_HREF}>{t("privacyNotice")}</Link>
        </div>

        {footerSlot}
      </div>

    </section>
  );
}
