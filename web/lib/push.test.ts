import test from "node:test";
import assert from "node:assert/strict";

import { getPushOptInState, subscribeToPushNotifications } from "./push";
import { sendQueuedProductEvents } from "./api";

const GLOBAL_KEYS = ["window", "document", "navigator", "Notification", "fetch"] as const;

type GlobalKey = (typeof GLOBAL_KEYS)[number];

type PushEnvironment = {
  existing: PushSubscription | null;
  subscribe: () => Promise<PushSubscription>;
  settingsEndpoints: string[] | null;
  onSave?: (body: unknown) => void;
};

function installPushEnvironment(environment: PushEnvironment): () => void {
  const descriptors = new Map<GlobalKey, PropertyDescriptor | undefined>();
  for (const key of GLOBAL_KEYS) {
    descriptors.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  }

  const pushManager = {
    getSubscription: async () => environment.existing,
    subscribe: environment.subscribe,
  };
  const serviceWorker = {
    getRegistration: async () => ({ pushManager }),
  };
  const notification = {
    permission: "granted",
    requestPermission: async () => "granted",
  };
  const browserWindow = Object.assign(new EventTarget(), {
    PushManager: function PushManager() {},
    Notification: notification,
    atob: (value: string) => Buffer.from(value, "base64").toString("binary"),
  });

  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: browserWindow,
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: new EventTarget(),
  });
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { serviceWorker },
  });
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    value: notification,
  });
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/push/settings") {
        return new Response(
          JSON.stringify({
            enabled: true,
            public_key: "AQAB",
            subscription_endpoints: environment.settingsEndpoints,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/push/subscriptions" && init?.method === "POST") {
        environment.onSave?.(JSON.parse(String(init.body ?? "{}")));
        return new Response(null, { status: 204 });
      }
      if (url === "/api/analytics/events/batch") {
        const { events } = JSON.parse(String(init?.body)) as { events: unknown[] };
        return new Response(JSON.stringify({ accepted: events.map(() => true) }), { status: 202 });
      }
      if (url === "/api/analytics/events") {
        return new Response(JSON.stringify({ accepted: true }), { status: 202 });
      }
      throw new Error(`unexpected fetch: ${url}`);
    },
  });

  return () => {
    sendQueuedProductEvents();
    for (const key of GLOBAL_KEYS) {
      const descriptor = descriptors.get(key);
      if (descriptor) {
        Object.defineProperty(globalThis, key, descriptor);
      } else {
        delete (globalThis as Record<string, unknown>)[key];
      }
    }
  };
}

function subscription(
  endpoint: string,
  unsubscribe: () => Promise<boolean> = async () => true,
): PushSubscription {
  return {
    endpoint,
    unsubscribe,
    toJSON: () => ({
      endpoint,
      keys: { p256dh: "p256dh-key", auth: "auth-key" },
    }),
  } as unknown as PushSubscription;
}

test("stale browser subscription is reported as unsubscribed when the server pruned it", async () => {
  const stale = subscription("https://push.example/stale");
  const cleanup = installPushEnvironment({
    existing: stale,
    settingsEndpoints: ["https://push.example/another-device"],
    subscribe: async () => subscription("https://push.example/new"),
  });

  try {
    assert.equal(await getPushOptInState("token"), "unsubscribed");
  } finally {
    cleanup();
  }
});

test("unknown server subscription state preserves the browser subscription", async () => {
  const stale = subscription("https://push.example/current");
  const cleanup = installPushEnvironment({
    existing: stale,
    settingsEndpoints: null,
    subscribe: async () => subscription("https://push.example/new"),
  });

  try {
    assert.equal(await getPushOptInState("token"), "subscribed");
  } finally {
    cleanup();
  }
});

test("resubscribe removes the stale browser endpoint before saving its replacement", async () => {
  const calls: string[] = [];
  let savedBody: unknown = null;
  const stale = subscription("https://push.example/stale", async () => {
    calls.push("unsubscribe");
    return true;
  });
  const replacement = subscription("https://push.example/replacement");
  const cleanup = installPushEnvironment({
    existing: stale,
    settingsEndpoints: [],
    subscribe: async () => {
      calls.push("subscribe");
      return replacement;
    },
    onSave: (body) => {
      calls.push("save");
      savedBody = body;
    },
  });

  try {
    await subscribeToPushNotifications("token");
    assert.deepEqual(calls, ["unsubscribe", "subscribe", "save"]);
    assert.deepEqual(savedBody, {
      endpoint: "https://push.example/replacement",
      keys: { p256dh: "p256dh-key", auth: "auth-key" },
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    });
  } finally {
    cleanup();
  }
});
