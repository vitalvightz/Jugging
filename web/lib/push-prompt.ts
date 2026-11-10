/** A saved workout is the value moment for the one-time device opt-ins:
 * install guidance first, then notifications on a later visit. A newly
 * generated plan no longer offers install, so nothing interrupts the path
 * from a new plan into its first session. */
export const PUSH_PROMPT_EVENT = "unlxck:session-saved";
/** Dispatched first; the install prompt cancels it when it takes the moment. */
export const INSTALL_PROMPT_EVENT = "unlxck:install-offer";

// Lives for this page visit: once install guidance has taken a moment, the
// notification prompt waits for the next visit instead of following it.
let installClaimed = false;

export function installPromptClaimed(): boolean {
  return installClaimed;
}

/** Offers install guidance on its own, e.g. when a new plan is ready.
 * Returns true when the install prompt took the moment. */
export function offerInstallPrompt(): boolean {
  if (installClaimed) return false;
  if (window.dispatchEvent(new CustomEvent(INSTALL_PROMPT_EVENT, { cancelable: true }))) return false;
  installClaimed = true;
  return true;
}

export function offerPushNotifications(token: string): void {
  if (installClaimed || offerInstallPrompt()) return;
  window.dispatchEvent(new CustomEvent(PUSH_PROMPT_EVENT, { detail: { token } }));
}
