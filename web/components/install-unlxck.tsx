"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import Image from "next/image";
import { usePathname } from "next/navigation";

import { usePwaRuntime } from "@/components/pwa-register";
import {
  hasSeenInstallPrompt,
  isInstagramBrowser,
  PWA_INSTAGRAM_EXIT_SEEN_KEY,
  rememberInstallGuideDismissal,
  rememberInstallPromptSeen,
  resolveIosInstallBrowser,
  type IosInstallBrowser,
} from "@/lib/pwa";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";
import { INSTALL_PROMPT_EVENT } from "@/lib/push-prompt";
import promptStyles from "./push-notification-prompt.module.css";
import styles from "./install-unlxck.module.css";
import { useSheetExit } from "@/lib/use-sheet-exit";

function ShareIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 15V3m0 0L8 7m4-4 4 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M7 10H5.8A1.8 1.8 0 0 0 4 11.8v7.4A1.8 1.8 0 0 0 5.8 21h12.4a1.8 1.8 0 0 0 1.8-1.8v-7.4a1.8 1.8 0 0 0-1.8-1.8H17" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function AddToHomeIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3.5" y="3.5" width="17" height="17" rx="3" stroke="currentColor" strokeWidth="1.7" />
      <path d="M12 8v8M8 12h8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function MoreIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="5.5" cy="12" r="1.7" /><circle cx="12" cy="12" r="1.7" /><circle cx="18.5" cy="12" r="1.7" />
    </svg>
  );
}

function AddIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="m5 12.5 4.5 4.5L19 7.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="8" y="8" width="12" height="12" rx="2" stroke="currentColor" strokeWidth="1.7" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

// Never include auth tokens, codes or other query/fragment data in copied links.
async function copyCurrentPageLink(): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(window.location.origin + window.location.pathname);
    return true;
  } catch {
    return false;
  }
}

type IosGuide = Exclude<IosInstallBrowser, "unsupported">;
type InstallStep = { icon: ReactNode; title: string; hint: string };

const FIND_ADD_TO_HOME: InstallStep = {
  icon: <AddToHomeIcon />, title: "Tap Add to Home Screen", hint: "Can’t find it? Scroll down or tap View More.",
};

const IOS_STEPS: Record<IosGuide, InstallStep[]> = {
  safari: [
    { icon: <MoreIcon />, title: "Tap ••• in Safari’s toolbar", hint: "Share icon showing? Tap it and skip to step 3." },
    { icon: <ShareIcon />, title: "Select Share", hint: "It opens the share sheet." },
    { icon: <AddToHomeIcon />, title: "Tap Add to Home Screen", hint: "Can’t find it? Tap View More." },
    { icon: <AddIcon />, title: "Tap Add", hint: "Keep Open as Web App switched on." },
  ],
  "safari-legacy": [
    { icon: <ShareIcon />, title: "Tap the Share icon", hint: "It sits in Safari’s toolbar." },
    { icon: <AddToHomeIcon />, title: "Tap Add to Home Screen", hint: "Scroll down the list if you can’t see it." },
    { icon: <AddIcon />, title: "Tap Add", hint: "Top right of the screen." },
  ],
  chrome: [
    { icon: <ShareIcon />, title: "Tap the Share icon", hint: "In Chrome’s address bar, or under ••• then Share." },
    FIND_ADD_TO_HOME,
    { icon: <AddIcon />, title: "Tap Add", hint: "If Open as Web App appears, keep it on." },
  ],
  other: [
    { icon: <MoreIcon />, title: "Open your browser menu", hint: "Then tap Share." },
    FIND_ADD_TO_HOME,
    { icon: <AddIcon />, title: "Tap Add", hint: "If Open as Web App appears, keep it on." },
  ],
};

const INSTAGRAM_STEPS: InstallStep[] = [
  { icon: <MoreIcon />, title: "Tap ••• in Instagram", hint: "Find the menu in Instagram’s browser toolbar." },
  { icon: <ShareIcon />, title: "Choose Open in external browser", hint: "The name may vary. Your default browser will open." },
];

/** Instagram entry guidance or a one-time install offer after a saved session. Takes priority over the
 * notification prompt for that moment and reuses the PWA runtime's install
 * detection; Settings keeps the manual route. */
export function InstallPrompt() {
  const pathname = usePathname();
  const { installAvailability, isInstalled, promptInstall } = usePwaRuntime();
  const dialog = useRef<HTMLDialogElement>(null);
  const availability = useRef(installAvailability);
  const [guide, setGuide] = useState<"native" | IosGuide | "instagram" | null>(null);
  const instagramOffered = useRef(false);
  const [busy, setBusy] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"copied" | "failed" | null>(null);
  // A guide only stays up while the browser still offers that route.
  const shown = guide === "instagram"
    ? isInstalled === false
    : guide === "native" ? installAvailability === "native" : guide !== null && installAvailability === "ios-manual";

  useEffect(() => {
    availability.current = installAvailability;
  }, [installAvailability]);

  useEffect(() => {
    // Defer during active training, plan creation and editing. React to route changes.
    if (isInstalled !== false || guide !== null || instagramOffered.current ||
        !isInstagramBrowser(navigator.userAgent) ||
        /^\/(today|create|quick-build|onboarding|generate|round-timer|session-timer|sparring|log-training)(\/|$)/.test(pathname)) return;
    try {
      if (window.sessionStorage.getItem(PWA_INSTAGRAM_EXIT_SEEN_KEY) !== null) return;
    } catch { /* In-memory guard still prevents repeats during this visit. */ }
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible" || document.querySelector('dialog[open], [aria-modal="true"]')) return;
      window.clearInterval(timer);
      setGuide("instagram");
    }, 500);
    return () => window.clearInterval(timer);
  }, [isInstalled, guide, pathname]);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined;
    const resolveGuide = () => {
      if (isInstagramBrowser(navigator.userAgent)) return null;
      if (availability.current === "native") return "native";
      if (availability.current !== "ios-manual") return null;
      const browser = resolveIosInstallBrowser(navigator.userAgent);
      return browser === "unsupported" ? null : browser;
    };
    function onOffer(event: Event) {
      if (timer || !resolveGuide() || hasSeenInstallPrompt(window.localStorage)) return;
      event.preventDefault();
      // Let completion review, sparring safety reports and other modals finish.
      timer = setInterval(() => {
        if (document.visibilityState !== "visible" || document.querySelector('dialog[open], [aria-modal="true"]')) return;
        clearInterval(timer);
        setGuide(resolveGuide());
      }, 500);
    }
    window.addEventListener(INSTALL_PROMPT_EVENT, onOffer);
    return () => {
      clearInterval(timer);
      window.removeEventListener(INSTALL_PROMPT_EVENT, onOffer);
    };
  }, []);

  useEffect(() => {
    if (!shown || !dialog.current) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const node = dialog.current;
    const release = lockOverlayScroll();
    try {
      node.showModal();
    } catch {
      release();
      return;
    }
    if (node.open) {
      if (guide === "instagram") {
        instagramOffered.current = true;
        try { window.sessionStorage.setItem(PWA_INSTAGRAM_EXIT_SEEN_KEY, "1"); }
        catch { /* Only the current visit can be remembered. */ }
      } else rememberInstallPromptSeen(window.localStorage);
    }
    return () => {
      node.close();
      release();
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    };
  }, [shown]);

  async function install() {
    setBusy(true);
    try {
      // The browser reports the result; appinstalled is the only proof of install.
      await promptInstall();
    } finally {
      setBusy(false);
      setGuide(null);
    }
  }

  // Slides away before it is removed.
  const { closing, close: dismiss } = useSheetExit(() => setGuide(null));
  if (!shown) return null;
  return (
    <dialog ref={dialog} className={`${promptStyles.dialog} ${styles.dialog}`} aria-labelledby="install-prompt-title" aria-describedby="install-prompt-description" aria-busy={busy} data-closing={closing || undefined}
      onCancel={(event) => { event.preventDefault(); if (!busy) dismiss(); }}>
      <div className={`${promptStyles.header} ${styles.header}`}>
        <Image src="/brand/unlxck-one-angle-48.png" alt="" width={28} height={28} className={promptStyles.mark} />
        <p className={promptStyles.eyebrow}>GET UNLXCK ON YOUR PHONE</p>
      </div>
      <h2 id="install-prompt-title"><span className={styles.headline}>{guide === "instagram" ? "Open UNLXCK outside Instagram." : "Your training. One tap away."}</span></h2>
      <p id="install-prompt-description" className={`${promptStyles.description} ${styles.description}`}>
        {guide === "instagram"
          ? "Open UNLXCK in your phone’s browser to add it to your Home Screen."
          : "Add UNLXCK to your Home Screen, like an app."}
      </p>
      {guide === "native" ? (
        <button type="button" className={promptStyles.enable} onClick={() => void install()} disabled={busy}>
          {busy ? "Waiting for your browser…" : "Install UNLXCK"}<AddToHomeIcon />
        </button>
      ) : (
        <>
          <ol className={styles.steps} aria-label={guide === "instagram" ? "Open UNLXCK outside Instagram" : "Add UNLXCK to your Home Screen"}>
            {(guide === "instagram" ? INSTAGRAM_STEPS : IOS_STEPS[guide!]).map((step) => (
              <li key={step.title}>
                <span className={styles.stepIcon}>{step.icon}</span>
                <span><strong>{step.title}</strong><small>{step.hint}</small></span>
              </li>
            ))}
          </ol>
          <p className={styles.done}>{guide === "instagram"
            ? "Can’t see that option? Copy the link instead."
            : "That’s it. UNLXCK will be on your Home Screen."}</p>
          <button type="button" className={promptStyles.enable} onClick={guide === "instagram"
            ? () => void copyCurrentPageLink().then((copied) => setCopyStatus(copied ? "copied" : "failed"))
            : () => dismiss()}>
            {guide === "instagram" ? (copyStatus === "copied" ? "Link copied" : "Copy link") : "Got it"}
            {guide === "instagram" && copyStatus !== "copied" ? <CopyIcon /> : <AddIcon />}
          </button>
          {guide === "instagram" ? <p className={styles.copyFeedback} role="status">
            {copyStatus === "copied" ? "Paste it into your phone’s browser."
              : copyStatus === "failed" ? "Couldn’t copy automatically. Use Instagram’s menu to copy the link."
                : "Paste the link into your phone’s browser."}
          </p> : null}
        </>
      )}
      <button type="button" className={promptStyles.later} disabled={busy} onClick={() => dismiss()}>Maybe later</button>
    </dialog>
  );
}

export function InstallUnlxck({ variant = "panel" }: { variant?: "panel" | "inline" }) {
  const { installAvailability, isInstalled, promptInstall } = usePwaRuntime();
  const canPromptInstall = installAvailability === "native";
  const isIos = installAvailability === "ios-manual";
  const inInstagram = typeof navigator !== "undefined" && isInstagramBrowser(navigator.userAgent);
  const [showGuide, setShowGuide] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"copied" | "failed" | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!showGuide) {
      return;
    }

    const releaseScroll = lockOverlayScroll();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowGuide(false);
        return;
      }
      if (event.key !== "Tab") {
        return;
      }

      const dialog = dialogRef.current;
      const focusable = Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((element) => !element.hasAttribute("hidden"));
      const first = focusable[0];
      const last = focusable.at(-1);

      if (!first || !last) {
        event.preventDefault();
        return;
      }
      if (event.shiftKey && (document.activeElement === first || !dialog?.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last || !dialog?.contains(document.activeElement))
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    const trigger = triggerRef.current;

    document.documentElement.dataset.pwaInstallSheet = "open";
    document.body.dataset.pwaInstallSheet = "open";
    closeRef.current?.focus({ preventScroll: true });
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      delete document.documentElement.dataset.pwaInstallSheet;
      delete document.body.dataset.pwaInstallSheet;
      window.removeEventListener("keydown", handleKeyDown);
      releaseScroll();
      trigger?.focus({ preventScroll: true });
    };
  }, [showGuide]);

  function dismissGuide() {
    rememberInstallGuideDismissal(window.localStorage);
    setShowGuide(false);
  }
  // The steps sheet slides away before it is removed.
  const { closing: guideClosing, close: closeGuide } = useSheetExit(dismissGuide);

  async function handleInstall() {
    if (canPromptInstall) {
      const outcome = await promptInstall();
      if (outcome === "dismissed") {
        rememberInstallGuideDismissal(window.localStorage);
      }
      return;
    }
    if (isIos || inInstagram) {
      setCopyStatus(null);
      setShowGuide(true);
    }
  }

  if (
    isInstalled !== false ||
    (!canPromptInstall && !isIos && !inInstagram)
  ) {
    return null;
  }

  // The sheet is fixed-position, so it must escape ancestors whose entrance
  // animations retain a transform (they become the containing block and push
  // the sheet off-screen). Portaling to <body> keeps it viewport-anchored.
  const sheetBrowser = showGuide ? resolveIosInstallBrowser(navigator.userAgent) : "unsupported";
  const installSheet = showGuide
    ? createPortal(
        <div
          className="pwa-install-sheet-backdrop"
          role="presentation"
          data-closing={guideClosing || undefined}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              closeGuide();
            }
          }}
        >
          <section
            ref={dialogRef}
            className="pwa-install-sheet"
            role="dialog"
            aria-modal="true"
            aria-labelledby="pwa-install-sheet-title"
            aria-describedby="pwa-install-sheet-description"
          >
            <div className="pwa-install-sheet-header">
              <div>
                <p className="pwa-install-label">Add to your device</p>
                <h2 id="pwa-install-sheet-title">{inInstagram ? "Open in your browser" : "Install UNLXCK"}</h2>
              </div>
              <button ref={closeRef} type="button" className="pwa-install-sheet-close" onClick={() => closeGuide()} aria-label="Close install instructions">
                <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
                  <path d="M5 5l10 10M15 5 5 15" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <p id="pwa-install-sheet-description" className="muted">
              {inInstagram
                ? "Open UNLXCK in your phone’s browser first. Use Instagram’s menu:"
                : sheetBrowser === "unsupported"
                  ? "This browser can’t add to Home Screen. Open UNLXCK in Safari, then:"
                  : "Your browser’s Share menu adds UNLXCK to your Home Screen."}
            </p>
            <ol className="pwa-install-steps">
              {(inInstagram ? INSTAGRAM_STEPS : IOS_STEPS[sheetBrowser === "unsupported" ? "safari" : sheetBrowser]).map((step) => (
                <li key={step.title}>
                  <span className="pwa-install-step-icon">{step.icon}</span>
                  <span><strong>{step.title}</strong><small>{step.hint}</small></span>
                </li>
              ))}
            </ol>
            {inInstagram ? (
              <>
                <button type="button" className="cta pwa-install-sheet-done" onClick={() =>
                  void copyCurrentPageLink().then((copied) => setCopyStatus(copied ? "copied" : "failed"))}>
                  {copyStatus === "copied" ? "Link copied" : "Copy link"}
                </button>
                <p className={styles.copyFeedback} role="status">
                  {copyStatus === "copied" ? "Paste it into your phone’s browser."
                    : copyStatus === "failed" ? "Couldn’t copy automatically. Use Instagram’s menu to copy the link."
                      : "No open-in-browser option? Copy this link instead."}
                </p>
              </>
            ) : (
              <button type="button" className="ghost-button pwa-install-sheet-done" onClick={() => closeGuide()}>
                Done
              </button>
            )}
          </section>
        </div>,
        document.body,
      )
    : null;

  if (variant === "inline") {
    return (
      <>
        <div className="pwa-install-inline" data-testid="install-unlxck-inline">
          <div className="pwa-install-inline-copy">
            <p className="pwa-install-label">Mobile access</p>
            <p className="muted pwa-install-inline-text">
              Add UNLXCK to your home screen, like an app.
            </p>
          </div>
          <button
            ref={triggerRef}
            type="button"
            className="secondary-button pwa-install-inline-button"
            onClick={() => void handleInstall()}
          >
            {canPromptInstall ? "Install UNLXCK" : inInstagram ? "View browser steps" : "View iPhone steps"}
          </button>
        </div>
        {installSheet}
      </>
    );
  }

  return (
    <>
      <div className="settings-subsection pwa-install-panel" data-testid="install-unlxck">
        <div className="pwa-install-mark" aria-hidden="true">
          <Image src="/icons/icon-192x192.png" alt="" width={64} height={64} />
        </div>
        <div className="pwa-install-copy">
          <div className="settings-subsection-header">
            <div>
              <p className="pwa-install-label">Mobile access</p>
              <h3 className="settings-subsection-title">Install UNLXCK</h3>
            </div>
          </div>
          <p className="muted pwa-install-description">
            Open UNLXCK from your home screen in its own window.
          </p>
        </div>
        <div className="pwa-install-actions">
          <button ref={triggerRef} type="button" className="cta" onClick={() => void handleInstall()}>
            {canPromptInstall ? "Install UNLXCK" : inInstagram ? "View browser steps" : "View iPhone steps"}
          </button>
        </div>
      </div>

      {installSheet}
    </>
  );
}
