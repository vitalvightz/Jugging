import assert from "node:assert/strict";
import test from "node:test";

import "../components/test-dom";
import { INSTALL_PROMPT_EVENT, offerInstallPrompt, offerPushNotifications, PUSH_PROMPT_EVENT } from "./push-prompt";

test("notifications take a saved moment when the device can be asked, and install guidance cannot follow", () => {
  let installOffers = 0;
  const onPush = (event: Event) => event.preventDefault();
  const onInstall = (event: Event) => { installOffers++; event.preventDefault(); };
  window.addEventListener(PUSH_PROMPT_EVENT, onPush);
  window.addEventListener(INSTALL_PROMPT_EVENT, onInstall);
  try {
    offerPushNotifications("token");
    assert.equal(installOffers, 0, "install guidance must not pre-empt the notification offer");
  } finally {
    window.removeEventListener(PUSH_PROMPT_EVENT, onPush);
    window.removeEventListener(INSTALL_PROMPT_EVENT, onInstall);
  }
});

test("install guidance takes an unclaimed moment and holds notifications for a later visit", () => {
  let pushOffers = 0;
  let claim = false;
  const onPush = () => { pushOffers++; };
  const onInstall = (event: Event) => { if (claim) event.preventDefault(); };
  window.addEventListener(PUSH_PROMPT_EVENT, onPush);
  window.addEventListener(INSTALL_PROMPT_EVENT, onInstall);
  try {
    // An ineligible device leaves the moment untouched; saves still offer notifications.
    assert.equal(offerInstallPrompt(), false);
    offerPushNotifications("token");
    assert.equal(pushOffers, 1);

    claim = true;
    assert.equal(offerInstallPrompt(), true);
    assert.equal(offerInstallPrompt(), false, "only one install offer per visit");
    offerPushNotifications("token");
    assert.equal(pushOffers, 1);
  } finally {
    window.removeEventListener(PUSH_PROMPT_EVENT, onPush);
    window.removeEventListener(INSTALL_PROMPT_EVENT, onInstall);
  }
});
