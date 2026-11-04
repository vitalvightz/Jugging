import assert from "node:assert/strict";
import test from "node:test";

import {
  createPwaWorkerUrl,
  hasSeenInstallPrompt,
  isInstagramBrowser,
  isIosDevice,
  isStandaloneDisplay,
  PWA_INSTALL_GUIDE_DISMISSED_KEY,
  PWA_INSTALL_PROMPT_SEEN_KEY,
  rememberInstallGuideDismissal,
  rememberInstallPromptSeen,
  resolveIosInstallBrowser,
  resolvePwaInstallAvailability,
  shouldRegisterServiceWorker,
} from "./pwa";

test("Instagram detection covers iPhone and Android without matching standard browsers", () => {
  assert.equal(isInstagramBrowser("Mozilla/5.0 (iPhone) Mobile/15E148 Instagram 400.0.0"), true);
  assert.equal(isInstagramBrowser("Mozilla/5.0 (Linux; Android 15) Chrome/141 Mobile Safari/537.36 Instagram 400.0.0"), true);
  assert.equal(isInstagramBrowser("Mozilla/5.0 (iPhone) Version/26.0 Mobile Safari/604.1"), false);
  assert.equal(isInstagramBrowser("Mozilla/5.0 (Linux; Android 15) Chrome/141 Mobile Safari/537.36"), false);
});

test("service-worker URL changes with the production deployment fingerprint", () => {
  const first = createPwaWorkerUrl("commit-a1");
  const sameBuild = createPwaWorkerUrl("commit-a1");
  const nextBuild = createPwaWorkerUrl("commit-b2");

  assert.match(first, /^\/sw\.js\?build=[a-z0-9]+$/);
  assert.equal(first, sameBuild);
  assert.notEqual(first, nextBuild);
});

test("standalone detection supports display-mode and iOS navigator.standalone", () => {
  assert.equal(isStandaloneDisplay(true, false), true);
  assert.equal(isStandaloneDisplay(false, true), true);
  assert.equal(isStandaloneDisplay(false, false), false);
  assert.equal(isStandaloneDisplay(false, undefined), false);
});

test("iOS detection covers iPhone, iPad, and touch-enabled iPadOS desktop user agents", () => {
  assert.equal(isIosDevice("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"), true);
  assert.equal(isIosDevice("Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)"), true);
  assert.equal(isIosDevice("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)", 5), true);
  assert.equal(isIosDevice("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)", 0), false);
  assert.equal(isIosDevice("Mozilla/5.0 (Linux; Android 15)"), false);
});

test("install availability stays hidden until detection and only exposes real install routes", () => {
  assert.equal(
    resolvePwaInstallAvailability({ hasNativePrompt: false, installed: null, ios: false }),
    "checking",
  );
  assert.equal(
    resolvePwaInstallAvailability({ hasNativePrompt: true, installed: true, ios: false }),
    "installed",
  );
  assert.equal(
    resolvePwaInstallAvailability({ hasNativePrompt: true, installed: false, ios: false }),
    "native",
  );
  assert.equal(
    resolvePwaInstallAvailability({ hasNativePrompt: false, installed: false, ios: true }),
    "ios-manual",
  );
  assert.equal(
    resolvePwaInstallAvailability({ hasNativePrompt: false, installed: false, ios: false }),
    "unsupported",
  );
});

test("service-worker registration is production-only and capability-gated", () => {
  assert.equal(shouldRegisterServiceWorker("production", true), true);
  assert.equal(shouldRegisterServiceWorker("development", true), false);
  assert.equal(shouldRegisterServiceWorker("test", true), false);
  assert.equal(shouldRegisterServiceWorker("production", false), false);
});

test("install-guide dismissal is stored without making storage mandatory", () => {
  const writes = new Map<string, string>();
  rememberInstallGuideDismissal({ setItem: (key, value) => writes.set(key, value) }, 1234);
  assert.equal(writes.get(PWA_INSTALL_GUIDE_DISMISSED_KEY), "1234");

  assert.doesNotThrow(() =>
    rememberInstallGuideDismissal({
      setItem: () => {
        throw new Error("storage blocked");
      },
    }),
  );
});

test("iOS install guidance follows the browser's real Add to Home Screen route", () => {
  const ios = (os: string, browser: string) =>
    `Mozilla/5.0 (iPhone; CPU iPhone OS ${os} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) ${browser}`;
  // iOS 26 reports a frozen 18_6 OS version; Safari's own version moves on.
  assert.equal(resolveIosInstallBrowser(ios("18_6", "Version/26.0 Mobile/15E148 Safari/604.1")), "safari");
  assert.equal(resolveIosInstallBrowser(ios("18_0", "Version/18.0 Mobile/15E148 Safari/604.1")), "safari-legacy");
  assert.equal(resolveIosInstallBrowser(ios("18_6", "CriOS/141.0.0.0 Mobile/15E148 Safari/604.1")), "chrome");
  assert.equal(resolveIosInstallBrowser(ios("17_4", "FxiOS/140.0 Mobile/15E148 Safari/605.1.15")), "other");
  assert.equal(resolveIosInstallBrowser(ios("16_3", "CriOS/120.0.0.0 Mobile/15E148 Safari/604.1")), "unsupported");
  assert.equal(resolveIosInstallBrowser(ios("18_6", "Mobile/15E148 Instagram 400.0.0")), "unsupported");
  assert.equal(resolveIosInstallBrowser(ios("18_6", "Mobile/15E148 Safari/604.1 GSA/390.0")), "unsupported");
});

test("the automatic install offer is remembered apart from the Settings dismissal", () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => void values.set(key, value) };
  // Existing users who closed the Settings sheet still get one offer.
  rememberInstallGuideDismissal(storage, 1);
  assert.equal(hasSeenInstallPrompt(storage), false);
  rememberInstallPromptSeen(storage, 2);
  assert.equal(values.get(PWA_INSTALL_PROMPT_SEEN_KEY), "2");
  assert.equal(hasSeenInstallPrompt(storage), true);
  assert.equal(hasSeenInstallPrompt({ getItem: () => { throw new Error("blocked"); } }), true);
});
