"use client";

import { deletePushSubscription, getPushSettings, savePushSubscription, trackProductEvent } from "@/lib/api";
import { isInstagramBrowser, isIosDevice, isStandaloneDisplay, PWA_DISPLAY_MODE_QUERY } from "@/lib/pwa";

/**
 * Web push opt-in flow for the PWA.
 *
 * The service worker (public/sw.js) only registers in production builds, so in
 * dev the state reads as "unsupported" — expected, not a bug. On iOS, push is
 * only available once the app is installed to the home screen (iOS 16.4+),
 * which `isPushSupported` surfaces naturally via the missing PushManager.
 */

export type PushOptInState =
  | "unsupported"
  | "server-disabled"
  | "denied"
  | "unsubscribed"
  | "subscribed";

export function isPushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** Running as the installed app (Home Screen or desktop install). */
export function isInstalledApp(): boolean {
  if (typeof window === "undefined") return false;
  return isStandaloneDisplay(
    window.matchMedia?.(PWA_DISPLAY_MODE_QUERY).matches ?? false,
    (navigator as Navigator & { standalone?: boolean }).standalone,
  );
}

/** iPhone and iPad deliver web push only to the installed app, so a browser
 * tab there is never asked: install guidance comes first. */
export function pushNeedsInstall(): boolean {
  if (typeof window === "undefined" || !isIosDevice(navigator.userAgent ?? "", navigator.maxTouchPoints)) return false;
  return !isInstalledApp();
}

// Set when the athlete turns this device's notifications off in Settings, so
// a granted-but-unsubscribed device is not offered them straight back.
const DEVICE_OPTED_OUT_KEY = "unlxck:push-device-opted-out";

function deviceOptedOut(): boolean {
  try { return window.localStorage.getItem(DEVICE_OPTED_OUT_KEY) === "1"; } catch { return false; }
}

function rememberDeviceOptOut(optedOut: boolean): void {
  try {
    if (optedOut) window.localStorage.setItem(DEVICE_OPTED_OUT_KEY, "1");
    else window.localStorage.removeItem(DEVICE_OPTED_OUT_KEY);
  } catch { /* Device storage is optional. */ }
}

/** Synchronous: whether this device could be offered notifications right now.
 * Permission may be undecided, or already granted with the subscription since
 * lost (cleared site data, an expired or pruned endpoint): that device is
 * recovered without asking for permission again. */
export function canAskForPush(): boolean {
  return isPushSupported() && !pushNeedsInstall() && !isInstagramBrowser(navigator.userAgent ?? "")
    && Notification.permission !== "denied" && !deviceOptedOut();
}

export type PreparedPush = { public_key: string; registration: ServiceWorkerRegistration };

/** Everything an "Enable reminders" button needs, resolved before the tap so
 * the native permission request keeps its user activation. Null when there is
 * nothing to offer: unsupported, blocked, paused, or already subscribed here
 * and known to the server. A browser subscription the server no longer has
 * (e.g. pruned as a dead endpoint) is offered again, since nothing reaches it. */
export async function preparePushOffer(token: string): Promise<PreparedPush | null> {
  if (!canAskForPush()) return null;
  const [settings, registration] = await Promise.all([
    getPushSettings(token), navigator.serviceWorker.getRegistration(),
  ]);
  if (!settings.enabled || !settings.public_key || !registration?.active) return null;
  if ((settings as typeof settings & { preferences?: { push_enabled?: boolean } }).preferences?.push_enabled === false) return null;
  const existing = await registration.pushManager.getSubscription();
  const endpoints = settings.subscription_endpoints;
  // Unknown server state (lookup failed) trusts the browser rather than nagging.
  if (existing && (!Array.isArray(endpoints) || endpoints.includes(existing.endpoint))) return null;
  return { public_key: settings.public_key, registration };
}

async function getReadyRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (!isPushSupported()) {
    return null;
  }
  try {
    // `ready` resolves only once a SW controls the page; in dev no SW ever
    // registers, so guard with the current registration instead of hanging.
    const registration = await navigator.serviceWorker.getRegistration();
    return registration ?? null;
  } catch {
    return null;
  }
}

export async function getExistingPushSubscription(): Promise<PushSubscription | null> {
  const registration = await getReadyRegistration();
  if (!registration) {
    return null;
  }
  try {
    return await registration.pushManager.getSubscription();
  } catch {
    return null;
  }
}

export async function getPushOptInState(token: string | null): Promise<PushOptInState> {
  if (!isPushSupported()) {
    return "unsupported";
  }
  if (Notification.permission === "denied") {
    return "denied";
  }

  const subscription = await getExistingPushSubscription();
  if (token) {
    try {
      const settings = await getPushSettings(token);
      if (!settings.enabled) {
        return "server-disabled";
      }
      const endpoints = settings.subscription_endpoints;
      if (
        subscription &&
        Array.isArray(endpoints) &&
        !endpoints.includes(subscription.endpoint)
      ) {
        return "unsubscribed";
      }
    } catch {
      // Preserve the browser's current state during a transient settings failure.
    }
  }

  return subscription ? "subscribed" : "unsubscribed";
}

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i += 1) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

function deviceTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/**
 * Request permission, subscribe the browser, and save the subscription
 * server-side. Throws with a human-readable message on any failure so callers
 * can surface it inline.
 */
export async function subscribeToPushNotifications(
  token: string,
  prepared?: PreparedPush,
): Promise<void> {
  // Activation funnel: every enable tap, then every saved subscription.
  void trackProductEvent(token, "push_prompt_accepted", "onboarding");
  if (!isPushSupported()) {
    throw new Error(
      "Notifications aren't available in this browser. On iPhone, install UNLXCK to your home screen first.",
    );
  }
  const settings = prepared ? { enabled: true, public_key: prepared.public_key } : await getPushSettings(token);
  if (!settings.enabled || !settings.public_key) {
    throw new Error("Notifications aren't enabled on the server yet.");
  }
  const registration = prepared?.registration ?? await getReadyRegistration();
  if (!registration) {
    throw new Error("Install the app first to enable notifications.");
  }
  // Already granted (a lost subscription being recovered): never ask again.
  const permission = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notifications blocked. Enable them in browser settings.");
  }

  const existing = await registration.pushManager.getSubscription();
  if (existing && !(await existing.unsubscribe())) {
    throw new Error("Couldn't reset the old notification subscription. Reload and try again.");
  }

  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(settings.public_key) as BufferSource,
  });
  const json = subscription.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
    throw new Error("The browser returned an incomplete push subscription.");
  }
  await savePushSubscription(token, {
    endpoint: json.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
    timezone: deviceTimezone(),
  });
  rememberDeviceOptOut(false);
  void trackProductEvent(token, "push_subscribed", "onboarding");
}

export async function unsubscribeFromPushNotifications(token: string): Promise<void> {
  rememberDeviceOptOut(true);
  const subscription = await getExistingPushSubscription();
  if (!subscription) {
    return;
  }
  const endpoint = subscription.endpoint;
  try {
    await subscription.unsubscribe();
  } catch {
    // Even if the browser-side unsubscribe fails, removing the server row
    // stops sends; the dead endpoint would be pruned on the next push anyway.
  }
  await deletePushSubscription(token, endpoint);
}
