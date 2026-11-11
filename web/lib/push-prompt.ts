/** A saved workout or check-in is the value moment for the one-time device
 * opt-ins. Notifications go first wherever the device can be asked now;
 * install guidance takes the moment when it cannot (an iPhone browser tab has
 * no push until UNLXCK is installed). A newly generated plan no longer offers
 * install, so nothing interrupts the path from a new plan into its first
 * session. */
export const PUSH_PROMPT_EVENT = "unlxck:session-saved";
/** The install prompt cancels it when it takes the moment. */
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

/** Device memory of the one automatic notification offer. */
export const pushPromptSeenKey = (athleteId: string) => `unlxck:push-prompt:v1:${athleteId}`;

/** Call after any saved action. The notification prompt cancels the event
 * when it takes the moment; otherwise install guidance may. */
export function offerPushNotifications(token: string): void {
  if (installClaimed) return;
  if (!window.dispatchEvent(new CustomEvent(PUSH_PROMPT_EVENT, { detail: { token }, cancelable: true }))) return;
  offerInstallPrompt();
}
