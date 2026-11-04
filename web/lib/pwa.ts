export const PWA_DISPLAY_MODE_QUERY = "(display-mode: standalone)";
export const PWA_INSTALL_GUIDE_DISMISSED_KEY = "unlxck.pwaInstallGuideDismissedAt";
// Separate from the Settings dismissal so existing users still get one offer.
export const PWA_INSTALL_PROMPT_SEEN_KEY = "unlxck.pwaInstallPromptSeenAt";
export const PWA_INSTAGRAM_EXIT_SEEN_KEY = "unlxck.pwaInstagramExitSeen";

export type PwaInstallAvailability =
  | "checking"
  | "installed"
  | "ios-manual"
  | "native"
  | "unsupported";

export function isStandaloneDisplay(
  matchesStandalone: boolean,
  navigatorStandalone: boolean | undefined,
): boolean {
  return matchesStandalone || navigatorStandalone === true;
}

export function isIosDevice(userAgent: string, maxTouchPoints = 0): boolean {
  return /iPad|iPhone|iPod/i.test(userAgent) || (/Macintosh/i.test(userAgent) && maxTouchPoints > 1);
}

export function resolvePwaInstallAvailability({
  hasNativePrompt,
  installed,
  ios,
}: {
  hasNativePrompt: boolean;
  installed: boolean | null;
  ios: boolean;
}): PwaInstallAvailability {
  if (installed === null) return "checking";
  if (installed) return "installed";
  if (hasNativePrompt) return "native";
  if (ios) return "ios-manual";
  return "unsupported";
}

export type IosInstallBrowser = "safari" | "safari-legacy" | "chrome" | "other" | "unsupported";

export const isInstagramBrowser = (userAgent: string): boolean => /Instagram/i.test(userAgent);

/** Which Add to Home Screen route this iOS browser offers. Safari 26 moved
 * Share under the toolbar's ••• menu; Chrome and other browsers reach the
 * same system sheet through their own Share action from iOS 16.4. In-app web
 * views (no Safari token, or a known app marker) cannot add to Home Screen. */
export function resolveIosInstallBrowser(userAgent: string): IosInstallBrowser {
  if (!/Safari\//.test(userAgent) || /FBAN|FBAV|LinkedInApp|Snapchat|TikTok|musical_ly|Line\/|GSA\//.test(userAgent) || isInstagramBrowser(userAgent)) {
    return "unsupported";
  }
  if (/CriOS\/|FxiOS\/|EdgiOS\/|OPiOS\/|OPT\/|DuckDuckGo\/|YaBrowser\//.test(userAgent)) {
    const [, major = "0", minor = "0"] = /OS (\d+)_(\d+)/.exec(userAgent) ?? [];
    if (Number(major) < 16 || (Number(major) === 16 && Number(minor) < 4)) return "unsupported";
    return /CriOS\//.test(userAgent) ? "chrome" : "other";
  }
  // iOS 26 freezes the OS version in the user agent, so Safari's own version decides.
  const safariVersion = Number(/Version\/(\d+)/.exec(userAgent)?.[1] ?? 0);
  if (!safariVersion) return "unsupported";
  return safariVersion >= 26 ? "safari" : "safari-legacy";
}

export function shouldRegisterServiceWorker(
  environment: string | undefined,
  serviceWorkerSupported: boolean,
): boolean {
  return environment === "production" && serviceWorkerSupported;
}

export function createPwaWorkerUrl(buildVersion: string | undefined): string {
  const versionInput = buildVersion?.trim() || "unlxck-local";
  let hash = 2166136261;

  for (let index = 0; index < versionInput.length; index += 1) {
    hash ^= versionInput.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }

  return `/sw.js?build=${(hash >>> 0).toString(36)}`;
}

export function rememberInstallGuideDismissal(
  storage: Pick<Storage, "setItem">,
  dismissedAt = Date.now(),
): void {
  try {
    storage.setItem(PWA_INSTALL_GUIDE_DISMISSED_KEY, String(dismissedAt));
  } catch {
    // Installation remains available even if private browsing blocks storage.
  }
}

export function hasSeenInstallPrompt(storage: Pick<Storage, "getItem">): boolean {
  try {
    return storage.getItem(PWA_INSTALL_PROMPT_SEEN_KEY) !== null;
  } catch {
    // Without storage the choice cannot be remembered, so do not ask again.
    return true;
  }
}

export function rememberInstallPromptSeen(storage: Pick<Storage, "setItem">, seenAt = Date.now()): void {
  try {
    storage.setItem(PWA_INSTALL_PROMPT_SEEN_KEY, String(seenAt));
  } catch {
    // The in-visit guard still prevents a repeat.
  }
}
