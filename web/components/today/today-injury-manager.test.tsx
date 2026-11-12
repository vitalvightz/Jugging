import test from "node:test";
import assert from "node:assert/strict";

import { window as domWindow } from "../test-dom";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ToastProvider } from "../toast-provider";

import { TodayInjuryManager } from "./today-injury-manager";
import type { InjuryFlagRecord, TodayCommandView } from "@/lib/types";

// jsdom has no matchMedia; the card's body-map select reads it on mount.
if (typeof domWindow.matchMedia !== "function") {
  Object.defineProperty(domWindow, "matchMedia", {
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

const BLISTER: InjuryFlagRecord = {
  id: "flag-blister",
  athlete_id: "athlete-1",
  source: "checkin",
  body_area: "left foot",
  description: "blister on left foot",
  label: "Left foot blister",
  severity: "moderate",
  status: "open",
  latest_reported_status: "ongoing",
  surface_class: "stable_surface",
  created_at: "2026-07-30T08:00:00Z",
  updated_at: "2026-07-30T08:00:00Z",
};

const OPEN_BLISTER: InjuryFlagRecord = {
  ...BLISTER,
  latest_reported_status: "worse",
  skin_integrity: "open",
  bleeding_status: "controlled",
  drainage: "none",
  infection_signs: [],
  coverable: "yes",
  friction_or_contact_problem: "yes",
  surface_class: "surface_no_contact",
};

const SHOULDER: InjuryFlagRecord = {
  ...BLISTER,
  id: "flag-shoulder",
  body_area: "left shoulder",
  description: "shoulder strain",
  label: "Left shoulder strain",
  surface_class: "non_surface",
};

function mount(): { container: HTMLElement; root: Root; cleanup: () => void } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  // Unmounting tears down React's tree but leaves the host node parented to
  // document.body, so every test used to leave an orphan div behind for the
  // next one to query across. Cleanup detaches the node as well.
  return {
    container,
    root,
    cleanup: () => {
      act(() => root.unmount());
      container.remove();
    },
  };
}

test("single injury removes duplicate clearance while retaining overall safety restrictions", async () => {
  const reported: InjuryFlagRecord = { ...SHOULDER, episode_id: "episode-1", status: "monitoring",
    description: "strain [training_impact:not_limiting]",
    clinician_clearance: { episode_id: "episode-1", scopes: ["rehab", "training", "contact"],
      source: "athlete_reported", externally_verified: false } };
  const effective: NonNullable<TodayCommandView["effective_clinician_clearance"]> = {
    level: "train_contact", scopes: ["rehab", "training", "contact"], requires_update: false,
    limited_by: [{ injury_id: reported.id, injury_episode_id: "episode-1", label: "Left shoulder strain" }],
  };
  const { container, root, cleanup } = mount();
  try {
    for (const [injuries, clearance, summaryVisible] of [
      [[reported], effective, false],
      [[reported, { ...SHOULDER, id: "another" }], effective, true],
      [[reported], { ...effective, level: "rehab_only", scopes: ["rehab"] }, true],
      [[reported], { ...effective, requires_update: true }, true],
    ] as Array<[InjuryFlagRecord[], typeof effective, boolean]>) {
      await act(async () => root.render(<TodayInjuryManager openInjuries={injuries} effectiveClearance={clearance}
        token="t" onRefresh={async () => {}} />));
      assert.equal(Boolean(container.querySelector('[aria-label="Effective clinician clearance"]')), summaryVisible);
      assert.match(container.querySelector(".today-injury-summary")?.textContent ?? "", /Monitoring · Low impact today/);
      assert.equal(container.querySelector(".today-injury-name small:not(.today-injury-summary)"), null);
    }
  } finally { cleanup(); }
});

/** Text a person can still read or reach: a closing section fades out inert. */
function liveText(container: HTMLElement): string {
  const copy = container.cloneNode(true) as HTMLElement;
  copy.querySelectorAll("[inert]").forEach((node) => node.remove());
  return copy.textContent ?? "";
}

async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function buttonNamed(container: HTMLElement, label: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll("button")).find(
    (button) => button.textContent?.trim() === label,
  );
  assert.ok(match, `expected a "${label}" button`);
  return match as HTMLButtonElement;
}

function statusButton(container: HTMLElement, label: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll(label === "Mark resolved" ? ".today-tool-link" : ".today-segment-row button")).find(
    (button) => button.textContent?.trim() === label,
  );
  assert.ok(match, `expected a "${label}" status button`);
  return match as HTMLButtonElement;
}

/** Selected styling means SAVED. A pending confirmation must never carry it. */
function assertNotSelected(button: HTMLButtonElement) {
  assert.equal(button.getAttribute("aria-pressed"), "false");
  assert.ok(
    !button.classList.contains("today-segment-active"),
    "expected no selected styling before the write is confirmed",
  );
}

function assertSelected(button: HTMLButtonElement) {
  assert.equal(button.getAttribute("aria-pressed"), "true");
  assert.ok(button.classList.contains("today-segment-active"));
  assert.ok(!button.classList.contains("today-segment-pending"));
}

async function click(element: HTMLElement) {
  await act(async () => {
    element.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  });
  await settle();
}

/** Stub /api/today/injury-checkin, capturing what was posted. */
function stubCheckin(options: { fail?: boolean; openInjuries?: InjuryFlagRecord[] } = {}) {
  const calls: Array<Record<string, unknown>> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (_input: unknown, init?: RequestInit) => {
    calls.push(JSON.parse(String(init?.body ?? "{}")));
    if (options.fail) {
      return new Response(JSON.stringify({ detail: "Injury update failed." }), {
        status: 500,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ open_injuries: options.openInjuries ?? [] }), {
      status: 201,
      headers: { "content-type": "application/json" },
    });
  }) as typeof globalThis.fetch;
  return { calls, restore: () => void (globalThis.fetch = originalFetch) };
}

test("an unknown injury type leaves no placeholder or secondary line", async () => {
  const { container, root, cleanup } = mount();

  await act(async () => {
    root.render(
      <TodayInjuryManager
        openInjuries={[
          {
            ...SHOULDER,
            description: "injury",
            label: "Right shoulder injury",
          },
        ]}
        token="t"
        onRefresh={async () => {}}
      />,
    );
  });

  assert.equal(container.querySelector(".today-injury-name strong")?.textContent, "Right shoulder injury");
  assert.equal(container.querySelector(".today-injury-name small:not(.today-injury-summary)"), null);
  assert.doesNotMatch(container.textContent ?? "", /Type not specified/);
  cleanup();
});

test("a cut card shows the normalised wound type beneath the injury name", async () => {
  const { container, root, cleanup } = mount();

  await act(async () => {
    root.render(
      <TodayInjuryManager
        openInjuries={[
          {
            ...BLISTER,
            body_area: "right eye",
            description: "Right eye cut",
            label: "Right eye cut",
            severity: "severe",
          },
        ]}
        token="t"
        onRefresh={async () => {}}
      />,
    );
  });

  assert.equal(container.querySelector(".today-injury-name strong")?.textContent, "Right eye cut");
  assert.equal(container.querySelector(".today-injury-name small")?.textContent, "Cut / laceration");
  assert.match(container.querySelector(".today-injury-meta")?.textContent ?? "", /severe/i);
  cleanup();
});

test("the injury card never renders the planner's internal taxonomy tokens", async () => {
  // A flag bootstrapped from guided intake stores the structured read of the
  // injury in its description. The athlete gets the condition word; the routing
  // keys ("surface injury", "surface_injury:blister") stay internal.
  const { container, root, cleanup } = mount();

  await act(async () => {
    root.render(
      <TodayInjuryManager
        openInjuries={[
          {
            ...BLISTER,
            body_area: "Right shoulder",
            // Both stored forms: the raw enum pair, and the humanized one the
            // backend writes once the underscores are stripped.
            description:
              "Right shoulder: blister. surface_injury:blister. surface injury. surface injury:blister",
            label: "Right shoulder blister",
          },
        ]}
        token="t"
        onRefresh={async () => {}}
      />,
    );
  });

  const text = container.textContent ?? "";
  assert.match(text, /Right shoulder blister/);
  assert.doesNotMatch(text, /surface injury/i);
  assert.doesNotMatch(text, /surface_injury/i);
  assert.equal(
    container.querySelector(".today-injury-name small:not(.today-injury-summary)"),
    null,
  );

  cleanup();
});

test("actionable skin guidance persists on its injury card", async () => {
  const cases: Array<{
    surfaceClass: InjuryFlagRecord["surface_class"];
    label: string;
    message: string;
    tone: string;
  }> = [
    {
      surfaceClass: "surface_medical_review",
      label: "Check before training",
      message: "This skin injury needs checking before training.",
      tone: "danger",
    },
    {
      surfaceClass: "surface_no_contact",
      label: "Contact restriction",
      message: "Keep contact off it until the skin is closed and coverable.",
      tone: "caution",
    },
    {
      surfaceClass: "surface_local_restriction",
      label: "Protect the area",
      message: "Protect it from rubbing or contact.",
      tone: "caution",
    },
  ];
  const { container, root, cleanup } = mount();

  await act(async () => {
    root.render(
      <TodayInjuryManager
        openInjuries={cases.map((item, index) => ({
          ...BLISTER,
          id: `surface-${index}`,
          surface_class: item.surfaceClass,
        }))}
        token="t"
        onRefresh={async () => {}}
      />,
    );
  });

  const guidance = Array.from(container.querySelectorAll<HTMLElement>(".today-injury-guidance"));
  assert.equal(guidance.length, cases.length);
  cases.forEach((item, index) => {
    assert.equal(guidance[index]?.dataset.tone, item.tone);
    assert.ok(guidance[index]?.textContent?.includes(item.label));
    assert.ok(guidance[index]?.textContent?.includes(item.message));
  });

  cleanup();
});

test("stable and non-surface injuries do not show persistent restriction guidance", async () => {
  const { container, root, cleanup } = mount();

  await act(async () => {
    root.render(
      <TodayInjuryManager openInjuries={[BLISTER, SHOULDER]} token="t" onRefresh={async () => {}} />,
    );
  });

  assert.equal(container.querySelector(".today-injury-guidance"), null);
  cleanup();
});

test("persistent guidance follows refreshed injury data and clears with the injury", async () => {
  const { container, root, cleanup } = mount();
  const render = async (openInjuries: InjuryFlagRecord[]) => {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={openInjuries} token="t" onRefresh={async () => {}} />,
      );
    });
  };

  await render([{ ...BLISTER, surface_class: "surface_no_contact" }]);
  assert.match(container.textContent ?? "", /Keep contact off it/);

  await render([{ ...BLISTER, surface_class: "stable_surface" }]);
  assert.equal(container.querySelector(".today-injury-guidance"), null);

  await render([]);
  assert.equal(container.querySelector(".today-injury-guidance"), null);
  assert.match(container.textContent ?? "", /No injuries tracked\./);

  cleanup();
});

test("marking a skin injury worse asks the surface follow-up before saving anything", async () => {
  const { calls, restore } = stubCheckin();
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={[BLISTER]} token="t" onRefresh={async () => {}} />,
      );
    });

    await click(statusButton(container, "Worse"));

    // Nothing is sent, and nothing reads as saved, until the follow-up is done.
    assert.equal(calls.length, 0);
    assertNotSelected(statusButton(container, "Worse"));
    assert.ok(statusButton(container, "Worse").classList.contains("today-segment-pending"));
    assert.match(container.textContent ?? "", /Is it open or burst\?/);
    assert.match(container.textContent ?? "", /Bleeding or weeping\?/);
    assert.match(container.textContent ?? "", /Any infection signs\?/);
    assert.match(container.textContent ?? "", /Can it stay covered\?/);
    assert.match(container.textContent ?? "", /Is rubbing or contact the problem\?/);

    await click(buttonNamed(container, "Open or burst"));
    await click(buttonNamed(container, "Pus"));
    await click(buttonNamed(container, "Save update"));

    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0], {
      injuries: [
        {
          flag_id: "flag-blister",
          status: "worse",
          skin_integrity: "open",
          infection_signs: ["pus"],
          coverable: "unknown",
          friction_or_contact_problem: "unknown",
          bleeding_status: "none",
          drainage: "none",
        },
      ],
    });
    // Only now is it marked as saved, and the follow-up closes.
    assertSelected(statusButton(container, "Worse"));
    assert.doesNotMatch(liveText(container), /Is it open or burst\?/);

    cleanup();
  } finally {
    restore();
  }
});

test("a medically concerning worse answer reports the raised severity immediately", async () => {
  const severeWound: InjuryFlagRecord = {
    ...BLISTER,
    severity: "severe",
    latest_reported_status: "worse",
    skin_integrity: "open",
    drainage: "present",
    infection_signs: ["pus"],
    coverable: "no",
    surface_class: "surface_medical_review",
  };
  const { restore } = stubCheckin({ openInjuries: [severeWound] });
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <ToastProvider>
          <TodayInjuryManager openInjuries={[{ ...BLISTER, severity: "mild" }]} token="t" onRefresh={async () => {}} />
        </ToastProvider>,
      );
    });

    await click(statusButton(container, "Worse"));
    await click(buttonNamed(container, "Open or burst"));
    await click(buttonNamed(container, "Pus"));
    await click(buttonNamed(container, "Save update"));

    assert.match(container.textContent ?? "", /Severity raised to severe/);
    assert.match(container.textContent ?? "", /skin injury needs checking/i);

    cleanup();
  } finally {
    restore();
  }
});

test("a failed worse update leaves the injury unselected and the follow-up open", async () => {
  const { calls, restore } = stubCheckin({ fail: true });
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={[BLISTER]} token="t" onRefresh={async () => {}} />,
      );
    });

    await click(statusButton(container, "Worse"));
    await click(buttonNamed(container, "Save update"));

    assert.equal(calls.length, 1);
    assertNotSelected(statusButton(container, "Worse"));
    // The answers are still on screen, so a retry does not start from scratch.
    assert.match(container.textContent ?? "", /Is it open or burst\?/);

    cleanup();
  } finally {
    restore();
  }
});

test("a non-surface injury marked worse saves directly, with no skin questions", async () => {
  const { calls, restore } = stubCheckin();
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={[SHOULDER]} token="t" onRefresh={async () => {}} />,
      );
    });

    await click(statusButton(container, "Worse"));

    assert.deepEqual(calls, [{ injuries: [{ flag_id: "flag-shoulder", status: "worse" }] }]);
    assert.doesNotMatch(liveText(container), /Is it open or burst\?/);
    assertSelected(statusButton(container, "Worse"));

    cleanup();
  } finally {
    restore();
  }
});

test("daily response uses Better, Same and Worse", async () => {
  // "Same" is the implicit default: an untouched injury stays ongoing in the
  // backend, so the row only ever asks about a CHANGE. A bright 'Same' button
  // read as a required daily confirmation — the confusion this removes.
  const { container, root, cleanup } = mount();

  await act(async () => {
    root.render(
      <TodayInjuryManager openInjuries={[BLISTER]} token="t" onRefresh={async () => {}} />,
    );
  });

  const labels = Array.from(
    container.querySelectorAll<HTMLButtonElement>(".today-injury-status-row button"),
  ).map((button) => button.textContent?.trim());
  assert.deepEqual(labels, ["Better", "Same", "Worse"]);
  assert.match(container.textContent ?? "", /Same/);

  cleanup();
});

test("resolving an injury is not marked selected until the confirmed write succeeds", async () => {
  const failing = stubCheckin({ fail: true });
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={[BLISTER]} token="t" onRefresh={async () => {}} />,
      );
    });

    await click(statusButton(container, "Mark resolved"));
    // The confirmation is open, but nothing has been sent or marked saved.
    assert.equal(failing.calls.length, 0);
    assertNotSelected(statusButton(container, "Mark resolved"));
    assert.ok(statusButton(container, "Mark resolved").classList.contains("today-segment-pending"));

    await click(buttonNamed(container, "Yes, resolve"));

    // The write failed: no selected state, and the confirmation stays open.
    assert.equal(failing.calls.length, 1);
    assertNotSelected(statusButton(container, "Mark resolved"));
    assert.match(container.textContent ?? "", /Resolve this injury\?/);
  } finally {
    failing.restore();
  }

  const succeeding = stubCheckin();
  try {
    await click(buttonNamed(container, "Yes, resolve"));

    assert.equal(succeeding.calls.length, 1);
    assert.deepEqual(succeeding.calls[0].injuries, [{ flag_id: BLISTER.id, status: "resolved" }]);
    assertSelected(statusButton(container, "Mark resolved"));
    assert.doesNotMatch(liveText(container), /Resolve this injury\?/);

    cleanup();
  } finally {
    succeeding.restore();
  }
});

test("a restricted wound reported easing rechecks the skin before the restriction lifts", async () => {
  const { calls, restore } = stubCheckin();
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={[OPEN_BLISTER]} token="t" onRefresh={async () => {}} />,
      );
    });

    await click(statusButton(container, "Better"));

    // Nothing is written on the tap alone: an open wound cannot stop blocking
    // contact without the athlete saying the skin has closed.
    assert.equal(calls.length, 0);
    assertNotSelected(statusButton(container, "Better"));
    assert.match(container.textContent ?? "", /Is the skin closed now\?/);
    // Friction is asked on the way back down too: it is what holds a closed
    // wound at a local restriction, so a recheck that could not answer it would
    // leave that restriction with no way to lift.
    assert.match(container.textContent ?? "", /Is rubbing or contact still the problem\?/);

    await click(buttonNamed(container, "Still closed"));
    await click(buttonNamed(container, "No"));
    await click(buttonNamed(container, "Save update"));

    assert.deepEqual(calls, [
      {
        injuries: [
          {
            flag_id: "flag-blister",
            status: "improving",
            skin_integrity: "intact",
            infection_signs: [],
            // Untouched answers come back as recorded rather than blanked.
            coverable: "yes",
            friction_or_contact_problem: "yes",
            bleeding_status: "none",
            drainage: "none",
          },
        ],
      },
    ]);
    assertSelected(statusButton(container, "Better"));

    cleanup();
  } finally {
    restore();
  }
});

test("the recheck opens on what is stored, so saving it cannot silently clear an answer", async () => {
  const { calls, restore } = stubCheckin();
  const infected: InjuryFlagRecord = {
    ...OPEN_BLISTER,
    infection_signs: ["pus"],
    surface_class: "surface_medical_review",
  };
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={[infected]} token="t" onRefresh={async () => {}} />,
      );
    });

    await click(statusButton(container, "Better"));
    // Pre-filled from the record: the stored infection sign is still selected.
    assert.equal(buttonNamed(container, "Pus").getAttribute("aria-pressed"), "true");
    assert.equal(buttonNamed(container, "Open or burst").getAttribute("aria-pressed"), "true");

    await click(buttonNamed(container, "Save update"));

    const sent = (calls[0].injuries as Array<Record<string, unknown>>)[0];
    assert.equal(sent.status, "improving");
    // Untouched answers survive the recheck rather than being blanked by it.
    assert.deepEqual(sent.infection_signs, ["pus"]);
    assert.equal(sent.skin_integrity, "open");
    assert.equal(sent.bleeding_status, "controlled");

    cleanup();
  } finally {
    restore();
  }
});

test("a stable skin injury reported easing saves directly, with no recheck", async () => {
  const { calls, restore } = stubCheckin();
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={[BLISTER]} token="t" onRefresh={async () => {}} />,
      );
    });

    await click(statusButton(container, "Better"));

    assert.deepEqual(calls, [{ injuries: [{ flag_id: "flag-blister", status: "improving" }] }]);
    assert.doesNotMatch(container.textContent ?? "", /Is the skin closed now\?/);

    cleanup();
  } finally {
    restore();
  }
});

// A wound that is bleeding uncontrollably AND draining. The follow-up asks one
// bleeding question, and "Won't stop" is the answer it prefills — which says
// nothing about drainage.
const LEAKING_UNCONTROLLED_BLISTER: InjuryFlagRecord = {
  ...OPEN_BLISTER,
  bleeding_status: "uncontrolled",
  drainage: "present",
  surface_class: "surface_medical_review",
};

test("an untouched recheck preserves a stored drainage the bleeding answer does not cover", async () => {
  // Saving the canonical mapping for "Won't stop" wrote drainage: "unknown"
  // over a recorded "present" — losing a safety signal the athlete never
  // touched, on a recheck of a wound already flagged for medical review.
  const { calls, restore } = stubCheckin();
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager
          openInjuries={[LEAKING_UNCONTROLLED_BLISTER]}
          token="t"
          onRefresh={async () => {}}
        />,
      );
    });

    await click(statusButton(container, "Better"));
    await click(buttonNamed(container, "Save update"));

    const sent = (calls[0]?.injuries as Array<Record<string, unknown>>)[0];
    assert.equal(sent.bleeding_status, "uncontrolled");
    assert.equal(sent.drainage, "present");
    assert.notEqual(sent.drainage, "unknown");

    cleanup();
  } finally {
    restore();
  }
});

test("changing the bleeding answer does replace the stored drainage", async () => {
  // The preservation above must not freeze the field: an answered question
  // still overwrites what was on record.
  const { calls, restore } = stubCheckin();
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager
          openInjuries={[LEAKING_UNCONTROLLED_BLISTER]}
          token="t"
          onRefresh={async () => {}}
        />,
      );
    });

    await click(statusButton(container, "Better"));
    await click(buttonNamed(container, "No"));
    await click(buttonNamed(container, "Save update"));

    const sent = (calls[0]?.injuries as Array<Record<string, unknown>>)[0];
    assert.equal(sent.bleeding_status, "none");
    assert.equal(sent.drainage, "none");

    cleanup();
  } finally {
    restore();
  }
});

test("a write in flight locks every row, so another injury's follow-up cannot be discarded", async () => {
  // The other row stayed clickable, the handler closed the open follow-up, and
  // updateInjury then refused the request because a write was pending — so the
  // answers being filled in were lost on the way to a no-op.
  let release: (() => void) | null = null;
  const originalFetch = globalThis.fetch;
  const calls: Array<Record<string, unknown>> = [];
  globalThis.fetch = (async (_input: unknown, init?: RequestInit) => {
    calls.push(JSON.parse(String(init?.body ?? "{}")));
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    return new Response(JSON.stringify({ open_injuries: [] }), {
      status: 201,
      headers: { "content-type": "application/json" },
    });
  }) as typeof globalThis.fetch;

  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager
          openInjuries={[SHOULDER, OPEN_BLISTER]}
          token="t"
          onRefresh={async () => {}}
        />,
      );
    });

    // Start a write on the shoulder and leave it hanging.
    await click(statusButton(container, "Better"));
    assert.equal(calls.length, 1);

    // Every status button is now disabled, including the other injury's.
    // Scoped to the tracked-injury rows: the "add an injury" form below has its
    // own segment row, and that flow is not what the pending write blocks.
    const allStatusButtons = Array.from(
      container.querySelectorAll<HTMLButtonElement>(".today-injury-item .today-segment-row button"),
    );
    assert.ok(
      allStatusButtons.every((button) => button.disabled),
      "expected every row's status actions to be locked while a write is in flight",
    );

    // And a click that slips through (a stale pointer event) changes nothing.
    await click(allStatusButtons[allStatusButtons.length - 1]);
    assert.equal(calls.length, 1);

    await act(async () => {
      release?.();
      await Promise.resolve();
    });
    await settle();

    cleanup();
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a pending answer is announced, not just outlined", async () => {
  // aria-pressed stays false until the write is confirmed, so the "captured but
  // not saved" state needs its own accessible description.
  const { restore } = stubCheckin();
  const { container, root, cleanup } = mount();

  try {
    await act(async () => {
      root.render(
        <TodayInjuryManager openInjuries={[BLISTER]} token="t" onRefresh={async () => {}} />,
      );
    });

    await click(statusButton(container, "Mark resolved"));

    const cleared = statusButton(container, "Mark resolved");
    assertNotSelected(cleared);
    const describedBy = cleared.getAttribute("aria-describedby");
    assert.ok(describedBy, "expected the pending button to describe its state");
    const hint = container.querySelector(`[id="${describedBy}"]`);
    assert.ok(hint, "expected the described element to exist");
    assert.match(hint?.textContent ?? "", /Not saved yet/);

    cleanup();
  } finally {
    restore();
  }
});

function mainInjury(overrides: Partial<InjuryFlagRecord> = {}): InjuryFlagRecord {
  return {
    id: "injury-1",
    athlete_id: "athlete-1",
    plan_id: "plan-1",
    source: "manual",
    body_area: "Left shoulder",
    description: "Bruise",
    label: "Left shoulder",
    severity: "moderate",
    status: "open",
    created_at: "2026-07-30T08:00:00Z",
    updated_at: "2026-07-30T08:00:00Z",
    ...overrides,
  };
}

function mountMain(
  openInjuries: InjuryFlagRecord[] = [],
  onRefresh: () => Promise<void> = async () => {},
): { container: HTMLElement; root: Root } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <ToastProvider>
        <TodayInjuryManager openInjuries={openInjuries} token="token" onRefresh={onRefresh} />
      </ToastProvider>,
    );
  });
  return { container, root };
}

function unmountMain(container: HTMLElement, root: Root) {
  act(() => root.unmount());
  container.remove();
}

// The add / edit form opens in a sheet portaled to the body, so look there.
function button(container: HTMLElement, label: string): HTMLButtonElement {
  const found = Array.from((container.ownerDocument?.body ?? container).querySelectorAll("button")).find(
    (item) => item.textContent?.trim() === label,
  );
  assert.ok(found, `Expected button labelled ${label}`);
  return found;
}

async function setInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
  assert.ok(setter);
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  if (input.id === "today-injury-area" && value.trim()) await act(async () => input.dispatchEvent(new window.FocusEvent("focusout", { bubbles: true })));
}

async function setImpact(label: string) {
  const slider = document.querySelector<HTMLInputElement>('.injury-impact-input input[type="range"]');
  assert.ok(slider);
  await setInput(slider, String(["Not limiting me", "Limiting me", "Can’t train normally"].indexOf(label)));
  // Native range inputs do not emit a change for the unchanged middle stop.
  if (slider.getAttribute("aria-valuetext") === "Not set") {
    await act(async () => slider.dispatchEvent(new window.KeyboardEvent("keydown", { key: " ", bubbles: true })));
  }
}

test("add injury form stays off the page and opens in its own sheet from the empty-state trigger", async () => {
  const { container, root } = mountMain();
  try {
    const trigger = button(container, "+ Add injury");
    assert.equal(document.querySelector("form.today-injury-add"), null);
    assert.equal(trigger.getAttribute("aria-haspopup"), "dialog");
    assert.match(container.textContent ?? "", /No injuries tracked\./);

    await click(trigger);

    const sheet = document.querySelector('[role="dialog"][aria-modal="true"]');
    assert.ok(sheet, "the form opens in a modal sheet");
    assert.ok(!container.contains(sheet), "the sheet is not part of the Today page flow");
    assert.match(sheet.querySelector("h2")?.textContent ?? "", /^Add injury$/);
    assert.ok(sheet.querySelector("form.today-injury-add"));
  } finally {
    unmountMain(container, root);
  }
});

test("active injuries stay first with name, type, severity, actions, and the add-another label", async () => {
  const { container, root } = mountMain([mainInjury()]);
  try {
    const list = document.querySelector(".today-injury-list");
    const trigger = button(container, "+ Add another injury");
    assert.ok(list);
    assert.ok(
      Boolean(list.compareDocumentPosition(trigger) & window.Node.DOCUMENT_POSITION_FOLLOWING),
      "active injury list should appear before the add trigger",
    );
    assert.match(list.textContent ?? "", /Left shoulder/);
    assert.match(list.textContent ?? "", /Bruise/);
    assert.match(list.textContent ?? "", /moderate/);
    assert.ok(button(container, "Better"));
    assert.ok(button(container, "Mark resolved"));
  } finally {
    unmountMain(container, root);
  }
});

test("successful injury submission refreshes, resets, and collapses the form", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; body: unknown }> = [];
  let refreshes = 0;
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ open_injuries: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const { container, root } = mountMain([], async () => {
    refreshes += 1;
  });
  try {
    await click(button(container, "+ Add injury"));
    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "Left ankle");
    await setImpact("Limiting me");
    await click(button(container, "Soreness"));

    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();

    assert.equal(refreshes, 1);
    assert.equal(requests.length, 1);
    assert.equal(requests[0]?.url, "/api/today/injury-checkin");
    assert.deepEqual(requests[0]?.body, {
      injuries: [
        {
          body_area: "Left ankle",
          description: "soreness [training_impact:limiting]",
          severity: "moderate",
          status: "ongoing",
        },
      ],
    });
    assert.equal(document.querySelector("form.today-injury-add"), null);

    await click(button(container, "+ Add injury"));
    assert.equal(document.querySelector<HTMLInputElement>("#today-injury-area"), null);
    assert.equal(document.querySelector('.injury-impact-input'), null);
    assert.equal(document.querySelector('[aria-label="Type"]'), null);
  } finally {
    globalThis.fetch = originalFetch;
    unmountMain(container, root);
  }
});

test("adding a skin injury asks the five surface questions immediately", async () => {
  // The reported problem: a freshly added blister sat silent until the athlete
  // later marked it easing or worse. The five skin questions now open on the add
  // itself, so the wound is routed by what the skin is doing from the start.
  const created: InjuryFlagRecord = {
    ...BLISTER,
    id: "flag-new-blister",
    body_area: "left hand",
    description: "blister",
    label: "Left hand blister",
    surface_class: "stable_surface",
    latest_reported_status: "ongoing",
  };
  const { calls, restore } = stubCheckin({ openInjuries: [created] });
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  // The follow-up renders on the refreshed row, so onRefresh has to surface the
  // just-created flag — exactly as the live Today screen re-fetches after a write.
  let openInjuries: InjuryFlagRecord[] = [];
  const renderTree = () => {
    root.render(
      <ToastProvider>
        <TodayInjuryManager
          openInjuries={openInjuries}
          token="t"
          onRefresh={async () => {
            openInjuries = [created];
            renderTree();
          }}
        />
      </ToastProvider>,
    );
  };

  try {
    await act(async () => {
      renderTree();
    });

    await click(button(container, "+ Add injury"));
    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "left hand");
    await setImpact("Limiting me");
    await click(button(container, "+ Add note (optional)"));
    const detail = document.querySelector<HTMLInputElement>("#today-injury-detail");
    assert.ok(detail);
    await setInput(detail, "blister");
    await click(button(container, "Other"));

    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();

    // One post so far (the create), and the skin questions are already on screen
    // without the athlete having marked it easing or worse.
    assert.equal(calls.length, 1);
    const followup = document.querySelector(".today-injury-surface-followup");
    assert.ok(followup, "expected the surface follow-up to open on add");
    assert.equal(followup?.getAttribute("data-mode"), "initial");
    assert.match(container.textContent ?? "", /Is it open or burst\?/);
    assert.match(container.textContent ?? "", /Bleeding or weeping\?/);
    assert.match(container.textContent ?? "", /Any infection signs\?/);
    assert.match(container.textContent ?? "", /Can it stay covered\?/);
    assert.match(container.textContent ?? "", /Is rubbing or contact the problem\?/);

    // Answering and saving reports the baseline as "ongoing" — nothing has
    // changed, it was only just added — carrying the skin answers.
    await click(buttonNamed(container, "Open or burst"));
    await click(buttonNamed(container, "Save update"));

    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1], {
      injuries: [
        {
          flag_id: "flag-new-blister",
          status: "ongoing",
          skin_integrity: "open",
          infection_signs: [],
          coverable: "unknown",
          friction_or_contact_problem: "unknown",
          bleeding_status: "none",
          drainage: "none",
        },
      ],
    });
    assert.doesNotMatch(liveText(container), /Is it open or burst\?/);

    act(() => root.unmount());
    container.remove();
  } finally {
    restore();
  }
});

test("adding a non-surface injury does not open the skin follow-up", async () => {
  const created: InjuryFlagRecord = {
    ...SHOULDER,
    id: "flag-new-shoulder",
    latest_reported_status: "ongoing",
  };
  const { calls, restore } = stubCheckin({ openInjuries: [created] });
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  let openInjuries: InjuryFlagRecord[] = [];
  const renderTree = () => {
    root.render(
      <ToastProvider>
        <TodayInjuryManager
          openInjuries={openInjuries}
          token="t"
          onRefresh={async () => {
            openInjuries = [created];
            renderTree();
          }}
        />
      </ToastProvider>,
    );
  };

  try {
    await act(async () => {
      renderTree();
    });

    await click(button(container, "+ Add injury"));
    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "left shoulder");
    await setImpact("Limiting me");
    await click(button(container, "Soreness"));

    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();

    assert.equal(calls.length, 1);
    assert.equal(document.querySelector(".today-injury-surface-followup"), null);
    assert.doesNotMatch(liveText(container), /Is it open or burst\?/);

    act(() => root.unmount());
    container.remove();
  } finally {
    restore();
  }
});

test("adding a medical-review skin injury skips the questions for its banner", async () => {
  // A wound that already lands in medical review does not need the five
  // questions on add — it goes straight to its "get it checked" guidance.
  const created: InjuryFlagRecord = {
    ...BLISTER,
    id: "flag-new-severe",
    body_area: "right eye",
    description: "deep cut",
    label: "Right eye cut",
    severity: "severe",
    surface_class: "surface_medical_review",
    latest_reported_status: "ongoing",
  };
  const { calls, restore } = stubCheckin({ openInjuries: [created] });
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  let openInjuries: InjuryFlagRecord[] = [];
  const renderTree = () => {
    root.render(
      <ToastProvider>
        <TodayInjuryManager
          openInjuries={openInjuries}
          token="t"
          onRefresh={async () => {
            openInjuries = [created];
            renderTree();
          }}
        />
      </ToastProvider>,
    );
  };

  try {
    await act(async () => {
      renderTree();
    });

    await click(button(container, "+ Add injury"));
    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "right eye");
    await setImpact("Limiting me");
    await click(button(container, "Other"));

    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();

    assert.equal(calls.length, 1);
    assert.equal(document.querySelector(".today-injury-surface-followup"), null);
    assert.doesNotMatch(liveText(container), /Is it open or burst\?/);
    // Its persistent guidance stands in for the questions.
    assert.match(container.textContent ?? "", /needs checking before training/i);

    act(() => root.unmount());
    container.remove();
  } finally {
    restore();
  }
});

test("failed injury submission leaves the populated form open", async () => {
  const originalFetch = globalThis.fetch;
  let refreshes = 0;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "Could not save injury" }), {
      status: 500,
      headers: { "content-type": "application/json" },
    });
  const { container, root } = mountMain([], async () => {
    refreshes += 1;
  });
  try {
    await click(button(container, "+ Add injury"));
    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "Right knee");
    await setImpact("Limiting me");
    await click(button(container, "Tightness"));
    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();

    assert.equal(refreshes, 0);
    assert.ok(document.querySelector("form.today-injury-add"), "the sheet stays open after a failed save");
    assert.match(document.querySelector(".today-injury-selection")?.textContent ?? "", /Right knee/);
    assert.equal(document.querySelector('.injury-impact-input input')?.getAttribute("aria-valuetext"), "Limiting me");
    assert.match(document.body.textContent ?? "", /Could not save injury/);
  } finally {
    globalThis.fetch = originalFetch;
    unmountMain(container, root);
  }
});

test("existing injury status actions still submit through the current refresh flow", async () => {
  const originalFetch = globalThis.fetch;
  let payload: unknown;
  let refreshes = 0;
  globalThis.fetch = async (_input, init) => {
    payload = JSON.parse(String(init?.body));
    return new Response(JSON.stringify({ open_injuries: [mainInjury()] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const { container, root } = mountMain([mainInjury()], async () => {
    refreshes += 1;
  });
  try {
    await click(button(container, "Better"));
    await settle();

    assert.deepEqual(payload, { injuries: [{ flag_id: "injury-1", status: "improving" }] });
    assert.equal(refreshes, 1);
    assert.equal(button(container, "Better").getAttribute("aria-pressed"), "true");

    await click(button(container, "Mark resolved"));
    assert.match(container.textContent ?? "", /Resolve this injury/);
    await click(button(container, "Cancel"));
    assert.doesNotMatch(liveText(container), /Resolve this injury/);
  } finally {
    globalThis.fetch = originalFetch;
    unmountMain(container, root);
  }
});

test("submitting without a type says so instead of refusing silently", async () => {
  // The type has no default and is required. When the submit button simply went
  // dead there was nothing on screen naming what was missing — the reported
  // symptom was athletes concluding the app was broken.
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return new Response(JSON.stringify({ open_injuries: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const { container, root } = mountMain();
  try {
    await click(button(container, "+ Add injury"));
    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "Left ankle");
    await setImpact("Limiting me");

    const submit = button(container, "Save injury");
    assert.equal(submit.disabled, false, "an incomplete form must still accept the tap");

    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();

    assert.equal(requests, 0, "nothing may be sent without a type");
    const alert = document.querySelector('[role="alert"]');
    assert.ok(alert, "expected the missing answer to be named");
    assert.match(alert?.textContent ?? "", /Pick a type/);
    // And it points at the control that stopped the submit.
    assert.ok(document.querySelector(".today-segment-row[data-invalid]"));

    // Answering it clears the error without a second submit attempt.
    await click(button(container, "Other"));
    assert.equal(document.querySelector('[role="alert"]'), null);
    assert.equal(document.querySelector(".today-segment-row[data-invalid]"), null);
  } finally {
    globalThis.fetch = originalFetch;
    unmountMain(container, root);
  }
});

test("submitting without an area names the area, not the type", async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return new Response(JSON.stringify({ open_injuries: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const { container, root } = mountMain();
  try {
    await click(button(container, "+ Add injury"));
    assert.equal(document.querySelector('[aria-label="Type"]'), null);

    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();

    assert.equal(requests, 0);
    const alert = document.querySelector('[role="alert"]');
    assert.match(alert?.textContent ?? "", /Say where it is/);

    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "Left ankle");
    await setImpact("Limiting me");
    assert.equal(document.querySelector('[role="alert"]'), null);
  } finally {
    globalThis.fetch = originalFetch;
    unmountMain(container, root);
  }
});

test("tap-first selection never opens text fields or changes impact on repeated taps", async () => {
  const { container, root } = mountMain();
  const { calls, restore } = stubCheckin();
  try {
    await click(button(container, "+ Add injury"));
    assert.equal(document.querySelector("#today-injury-area"), null);
    assert.equal(document.querySelector("#today-injury-detail"), null);
    const selectShoulder = async () => {
      const shoulder = document.querySelector('[aria-label="Left shoulder"]');
      assert.ok(shoulder);
      await act(async () => shoulder.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
    };
    await selectShoulder();
    await click(button(container, "Tightness"));
    const form = document.querySelector("form")!;
    await act(async () => form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
    assert.equal(calls.length, 0, "impact must be an explicit choice");
    assert.match(document.querySelector('[role="alert"]')?.textContent ?? "", /affecting your training/);
    await setImpact("Not limiting me");
    await click(button(container, "Change"));
    await selectShoulder();
    assert.equal(document.querySelector('.injury-impact-input input')?.getAttribute("aria-valuetext"), "Not limiting me");
    await act(async () => form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
    assert.deepEqual(calls[0], { injuries: [{ body_area: "Left shoulder", description: "tightness [training_impact:not_limiting]", severity: "mild", status: "ongoing" }] });
    assert.equal(document.querySelector("form.today-injury-add"), null);
  } finally { restore(); unmountMain(container, root); }
});

test("editing a saved injury updates its flag and retains the functional answer and note", async () => {
  const injury = { ...SHOULDER, description: "tightness. worse when punching [training_impact:limiting]" };
  const { container, root } = mountMain([injury]);
  const { calls, restore } = stubCheckin({ openInjuries: [injury] });
  try {
    assert.doesNotMatch(document.body.textContent ?? "", /training_impact/);
    // Edit (a pencil) shows once the injury is open.
    const summaryRow = document.querySelector<HTMLButtonElement>(".today-injury-summary-row");
    if (summaryRow?.getAttribute("aria-expanded") === "false") await click(summaryRow);
    await click(button(container, "Edit"));
    assert.equal(document.querySelector('.injury-impact-input input')?.getAttribute("aria-valuetext"), "Limiting me");
    assert.equal(button(container, "Tightness").getAttribute("aria-pressed"), "true");
    assert.equal(document.querySelector("#today-injury-detail"), null);
    await setImpact("Can’t train normally");
    const form = document.querySelector("form")!;
    await act(async () => form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
    assert.deepEqual(calls[0], { injuries: [{ flag_id: injury.id, body_area: "left shoulder", description: "tightness. worse when punching [training_impact:cant_train]", severity: "severe", status: "ongoing" }] });
  } finally { restore(); unmountMain(container, root); }
});

test("elbow tendon location is one simple answer in existing injury edit, retained without a worksheet", async () => {
  const injury = { ...SHOULDER, body_area: "left elbow", description: "tendonitis [training_impact:limiting] [elbow_site:unknown]" };
  const { container, root } = mountMain([injury]);
  const { calls, restore } = stubCheckin({ openInjuries: [injury] });
  try {
    assert.doesNotMatch(document.body.textContent ?? "", /elbow_site/);
    const summaryRow = document.querySelector<HTMLButtonElement>(".today-injury-summary-row");
    if (summaryRow?.getAttribute("aria-expanded") === "false") await click(summaryRow);
    await click(button(container, "Edit"));
    assert.match(document.body.textContent ?? "", /Where is the elbow problem\?/);
    assert.equal(document.querySelector('input[type="datetime-local"]'), null);
    await click(button(container, "Outside of elbow"));
    const form = document.querySelector("form")!;
    await act(async () => form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
    const saved = (calls[0].injuries as Array<{ description: string }>)[0].description;
    assert.equal(saved, "tendonitis [training_impact:limiting] [elbow_site:lateral]");
    assert.equal(calls.length, 1);
  } finally { restore(); unmountMain(container, root); }
});

test("Other opens area-specific types that tap in the condition word", async () => {
  const { calls, restore } = stubCheckin();
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(
        <ToastProvider>
          <TodayInjuryManager openInjuries={[]} token="t" onRefresh={async () => {}} />
        </ToastProvider>,
      );
    });
    await click(button(container, "+ Add injury"));
    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "left knee");
    assert.equal(document.querySelector(".today-injury-other"), null);
    await click(button(container, "Other"));
    const picker = document.querySelector(".today-injury-other");
    assert.ok(picker);
    assert.match(picker.textContent ?? "", /Common for left knee/);
    for (const label of ["Sprain / rolled", "Giving way", "Hyperextended", "Cut", "Graze / mat burn"]) button(container, label);
    assert.ok(!Array.from(picker.querySelectorAll("button")).some((b) => b.textContent === "Head knock / concussion"));
    assert.ok(!Array.from(picker.querySelectorAll("button")).some((b) => b.textContent === "Blister"));

    await click(button(container, "Possible break"));
    assert.match(document.body.textContent ?? "", /Get this checked by a clinician before training\./);
    await click(button(container, "Sprain / rolled"));
    assert.doesNotMatch(document.body.textContent ?? "", /Get this checked by a clinician/);
    assert.equal(button(container, "Sprain / rolled").getAttribute("aria-pressed"), "true");
    await setImpact("Limiting me");

    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();
    const injuries = calls[0].injuries as Array<{ body_area: string; description: string }>;
    assert.equal(injuries[0].body_area, "left knee");
    assert.match(injuries[0].description, /^sprain\b/);
  } finally {
    restore();
    unmountMain(container, root);
  }
});

test("switching away from Other drops its specific type", async () => {
  const { calls, restore } = stubCheckin();
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(
        <ToastProvider>
          <TodayInjuryManager openInjuries={[]} token="t" onRefresh={async () => {}} />
        </ToastProvider>,
      );
    });
    await click(button(container, "+ Add injury"));
    await click(button(container, "Can’t find the area? Enter it manually"));
    const area = document.querySelector<HTMLInputElement>("#today-injury-area");
    assert.ok(area);
    await setInput(area, "right hamstring");
    await click(button(container, "Other"));
    await click(button(container, "Strain / pulled"));
    await click(button(container, "Soreness"));
    assert.equal(document.querySelector(".today-injury-other"), null);
    await setImpact("Limiting me");
    const form = document.querySelector<HTMLFormElement>("form.today-injury-add");
    assert.ok(form);
    await act(async () => {
      form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    });
    await settle();
    const injuries = calls[0].injuries as Array<{ description: string }>;
    assert.match(injuries[0].description, /^soreness\b/);
    assert.doesNotMatch(injuries[0].description, /strain/);
  } finally {
    restore();
    unmountMain(container, root);
  }
});


test("ankle editor adds one occasional subtype answer, defaults unknown, and hides internal markers", async () => {
  const { calls, restore } = stubCheckin();
  const { container, root } = mountMain([mainInjury({ body_area: "Left ankle", description: "sprain [training_impact:limiting] [ankle_scope:unknown]" })]);
  try {
    assert.doesNotMatch(document.body.textContent ?? "", /ankle_scope/);
    const summaryRow = document.querySelector<HTMLButtonElement>(".today-injury-summary-row");
    if (summaryRow?.getAttribute("aria-expanded") === "false") await click(summaryRow);
    await click(button(container, "Edit"));
    assert.match(document.body.textContent ?? "", /What type of sprain did your clinician describe\?/);
    assert.equal(document.querySelector('input[type="datetime-local"]'), null);
    assert.equal(document.querySelector('select'), null);
    assert.doesNotMatch(document.body.textContent ?? "", /Balance score|ROM measurement|Assessment date/);
    await click(button(container, "Uncomplicated outer ankle sprain"));
    const form = document.querySelector("form")!;
    await act(async () => form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
    const saved = (calls[0].injuries as Array<{ description: string }>)[0].description;
    assert.equal(saved, "sprain [training_impact:limiting] [ankle_scope:uncomplicated_lateral]");
    assert.equal(calls.length, 1);
  } finally { restore(); unmountMain(container, root); }
});
test("shoulder editor adds one occasional subtype answer, defaults unknown, and hides internal markers", async () => {
  const { calls, restore } = stubCheckin();
  const { container, root } = mountMain([mainInjury({ body_area: "Left shoulder", description: "tendonitis [training_impact:limiting] [shoulder_scope:unknown]" })]);
  try {
    assert.doesNotMatch(document.body.textContent ?? "", /shoulder_scope/);
    const summaryRow = document.querySelector<HTMLButtonElement>(".today-injury-summary-row");
    if (summaryRow?.getAttribute("aria-expanded") === "false") await click(summaryRow);
    await click(button(container, "Edit"));
    assert.match(document.body.textContent ?? "", /What shoulder problem did your clinician describe\?/);
    assert.equal(document.querySelector('input[type="datetime-local"]'), null);
    assert.equal(document.querySelector('select'), null);
    assert.doesNotMatch(document.body.textContent ?? "", /Balance score|ROM measurement|Assessment date/);
    await click(button(container, "Rotator cuff tendinopathy, no injury accident or tear"));
    const form = document.querySelector("form")!;
    await act(async () => form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
    const saved = (calls[0].injuries as Array<{ description: string }>)[0].description;
    assert.equal(saved, "tendonitis [training_impact:limiting] [shoulder_scope:nontraumatic_cuff]");
    assert.equal(calls.length, 1);
  } finally { restore(); unmountMain(container, root); }
});

for (const [area, policyId] of [["Achilles", "achilles_tendonitis"], ["elbow", "elbow_tendonitis"], ["ankle", "ankle_sprain"], ["shoulder", "shoulder_tendonitis"]] as const) {
for (const [label, status, response] of [["Better", "improving", "better"], ["Same", "ongoing", "same"], ["Worse", "worse", "worse"]] as const) {
test(`${area} Today has no worksheet and one ${label} tap records the injury and episode-bound next-day response`, async () => {
  const { container, root, cleanup } = mount();
  const original = globalThis.fetch;
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  const refreshAfterWriteCounts: number[] = [];
  const injury: InjuryFlagRecord = { ...SHOULDER, id: "injury", episode_id: "episode", body_area: `Left ${area}`,
    description: `Left ${area} tendonitis`, rehab_decision: { policy_id: policyId, outcome: "prescribed_rehab",
      summary: "Current rehab", reason_codes: [], stage: "restore" } };
  globalThis.fetch = (async (input, init) => {
    calls.push({ url: String(input), body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ open_injuries: [injury] }), { status: 200 });
  }) as typeof fetch;
  try {
    await act(async () => root.render(<TodayInjuryManager openInjuries={[injury]} token="token" onRefresh={async () => { refreshAfterWriteCounts.push(calls.length); }}
      delayedPrompts={[{ injury_id: "injury", injury_episode_id: "episode", exposure_id: "exposure", region: area.toLowerCase(), question: "After rehab?", options: ["better", "same", "worse", "not_sure"] }]} />));
    assert.doesNotMatch(document.body.textContent ?? "", /Heel-rise|assessor|Achilles assessment|Range of motion|Loading task/);
    assert.equal(document.querySelector('input[type="datetime-local"]'), null);
    await click(statusButton(container, label));
    assert.equal(calls.length, 2); // Two owned records, one athlete tap.
    assert.deepEqual(refreshAfterWriteCounts, [2], "refresh once after both writes");
    assert.deepEqual(calls[0].body.injuries, [{ flag_id: "injury", status }]);
    assert.equal(calls[1].body.event_type, "delayed_rehab_response");
    assert.equal(calls[1].body.response, response);
    assert.equal(calls[1].body.exposure_id, "exposure");
  } finally { globalThis.fetch = original; cleanup(); }
});
}
}

test("a failed delayed response cannot block the current worsening report", async () => {
  const { container, root, cleanup } = mount();
  const original = globalThis.fetch;
  const calls: Array<Record<string, unknown>> = [];
  const refreshAfterWriteCounts: number[] = [];
  const injury = { ...SHOULDER, episode_id: "episode" };
  globalThis.fetch = (async (_input, init) => {
    calls.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify(calls.length === 1 ? { open_injuries: [injury] } : { detail: "Unavailable" }), { status: calls.length === 1 ? 200 : 500 });
  }) as typeof fetch;
  try {
    await act(async () => root.render(<TodayInjuryManager openInjuries={[injury]} token="token" onRefresh={async () => { refreshAfterWriteCounts.push(calls.length); }}
      delayedPrompts={[{ injury_id: injury.id, injury_episode_id: "episode", exposure_id: "exposure", region: "shoulder", question: "After rehab?", options: ["better", "same", "worse", "not_sure"] }]} />));
    await click(statusButton(container, "Worse"));
    assert.deepEqual(calls[0].injuries, [{ flag_id: injury.id, status: "worse" }]);
    assert.equal(calls[1].response, "worse");
    assert.deepEqual(refreshAfterWriteCounts, [2], "refresh saved injury even when its delayed response fails");
    assert.equal(statusButton(container, "Worse").getAttribute("aria-pressed"), "true");
  } finally { globalThis.fetch = original; cleanup(); }
});

test("a skin injury card shows only skin care, and not twice when Today already leads with it", async () => {
  const { container, root, cleanup } = mount();
  const skin = { ...OPEN_BLISTER, rehab_decision: { outcome: "wound_care", summary: "Follow the skin-care guidance for this injury.",
    reason_codes: ["surface_wound_care"], stage: "calm" as const } };
  const render = async (todaySafetyNoticeInjuryId: string | null) => {
    await act(async () => {
      root.render(<TodayInjuryManager openInjuries={[skin]} todaySafetyNoticeInjuryId={todaySafetyNoticeInjuryId}
        token="t" onRefresh={async () => {}} />);
    });
  };
  // A higher warning leads Today, so the card carries this injury's skin care.
  await render("another-injury");
  const text = container.textContent ?? "";
  assert.match(text, /Keep contact off it/);
  assert.equal(container.querySelector(".today-injury-summary-row")?.getAttribute("aria-expanded"), "false");
  assert.equal(container.querySelector(".today-injury-guidance")?.closest("[hidden]"), null,
    "contact restriction must remain visible when the injury controls are collapsed");
  assert.doesNotMatch(text, /Stage:|Stage 1 of 5|Recovery monitoring|skin-care guidance for this injury/);
  // Today's main advice already shows it: the card does not repeat it.
  await render(skin.id);
  assert.equal(document.querySelector(".today-injury-guidance"), null);
  cleanup();
});

test("an injury without published exercises keeps its actual stage and content message", async () => {
  const { container, root, cleanup } = mount();
  const stiffness = { ...SHOULDER, description: "stiffness", label: "Left shoulder stiffness", rehab_decision: {
    outcome: "unsupported_prescription", summary: "No suitable rehab is available for this injury yet.",
    reason_codes: ["unsupported_injury_policy"], stage: "calm" as const } };
  await act(async () => {
    root.render(<TodayInjuryManager openInjuries={[stiffness]} token="t" onRefresh={async () => {}} />);
  });
  const text = container.textContent ?? "";
  assert.match(text, /Stage: Calm/);
  assert.match(text, /CALM → RESTORE → LOAD → DYNAMIC → RETURN/);
  await act(async () => (document.querySelector(".today-injury-summary-row") as HTMLButtonElement).click());
  assert.match(container.textContent ?? "", /No rehab exercises available for this stage yet\./);
  cleanup();
});


test("location steps keep the form short, focus description, and never preselect impact", async () => {
  const { container, root } = mountMain();
  try {
    await click(button(container, "+ Add injury"));
    assert.equal(document.querySelector('[aria-label="Type"]'), null);
    assert.equal(document.querySelector('input[type="range"]'), null);
    assert.equal(document.querySelector('button[type="submit"]'), null);
    const shoulder = document.querySelector('[aria-label="Left shoulder"]')!;
    await act(async () => shoulder.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
    assert.ok(document.querySelector('.body-map-panel')?.closest('[hidden]'));
    assert.equal(document.activeElement?.getAttribute("aria-label"), "Describe the injury");
    const slider = document.querySelector<HTMLInputElement>('.injury-impact-input input')!;
    assert.equal(slider.getAttribute("aria-valuetext"), "Not set");
    assert.equal(document.querySelector('.level-slider-indicator'), null);
    // Space deliberately chooses the middle position without a native change event.
    await act(async () => slider.dispatchEvent(new window.KeyboardEvent("keydown", { key: " ", bubbles: true })));
    assert.equal(slider.getAttribute("aria-valuetext"), "Limiting me");
    await click(button(container, "Soreness"));
    await click(button(container, "+ Add note (optional)"));
    await setInput(document.querySelector<HTMLInputElement>('#today-injury-detail')!, "Only after running");
    await click(button(container, "Change"));
    assert.ok(document.querySelector('.body-map-panel'));
    assert.equal(document.querySelector('input[type="range"]'), null);
    await click(button(container, "Keep Left shoulder"));
    assert.equal(document.querySelector('.injury-impact-input input')?.getAttribute("aria-valuetext"), "Limiting me");
    assert.equal(document.querySelector<HTMLInputElement>('#today-injury-detail')?.value, "Only after running");
    assert.equal(button(container, "Soreness").getAttribute("aria-pressed"), "true");
  } finally { unmountMain(container, root); }
});

test("cancelling a draft resets location and answers before another injury", async () => {
  const { container, root } = mountMain();
  try {
    const trigger = button(container, "+ Add injury");
    trigger.focus();
    await click(trigger);
    await click(button(container, "Can’t find the area? Enter it manually"));
    await setInput(document.querySelector<HTMLInputElement>('#today-injury-area')!, "Left shin");
    await setImpact("Can’t train normally");
    await click(button(container, "Bruise"));
    await act(async () => {
      document.querySelector<HTMLButtonElement>('.injury-sheet-close')!.click();
      await new Promise(resolve => setTimeout(resolve, 230));
    });
    assert.equal(document.querySelector('[role="dialog"]'), null);
    assert.equal(document.activeElement, trigger);
    await click(trigger);
    assert.ok(document.querySelector('.body-map-panel'));
    assert.equal(document.querySelector('.today-injury-selection'), null);
    assert.equal(document.querySelector('input[type="range"]'), null);
  } finally { unmountMain(container, root); }
});

test("clearance is visible on a closed injury and opening it retires the discovery hint", async () => {
  const key = "discovery-injury-clearance-seen";
  window.localStorage.removeItem(key);
  const injury = { ...SHOULDER, episode_id: "episode-1" };
  const ui = mount();
  try {
    await act(async () => ui.root.render(<ToastProvider><TodayInjuryManager openInjuries={[injury]} token="token" onRefresh={async () => {}} /></ToastProvider>));
    assert.equal(ui.container.querySelector(".today-injury-controls")?.hasAttribute("hidden"), true);
    const clearance = ui.container.querySelector<HTMLButtonElement>('[aria-label="Report clearance"]')!;
    assert.ok(clearance);
    assert.equal(clearance.closest("[hidden]"), null);
    assert.match(ui.container.querySelector(".injury-discovery-hint")?.textContent ?? "", /what your clinician says you can do/);
    await click(clearance);
    assert.equal(window.localStorage.getItem(key), "1");
    assert.equal(ui.container.querySelector(".injury-discovery-hint"), null);
    const sheet = document.querySelector('[role="dialog"]')!;
    assert.match(sheet.textContent ?? "", /The rehab exercises your clinician allows/);
    assert.match(sheet.textContent ?? "", /does not issue or verify medical clearance/);
  } finally { ui.cleanup(); window.localStorage.removeItem(key); }
});

test("a medical hold stays visible when collapsed even beside permissive reported clearance", async () => {
  const injury: InjuryFlagRecord = { ...SHOULDER, episode_id: "episode-1", clinician_clearance: {
    episode_id: "episode-1", scopes: ["rehab", "training", "contact"], source: "athlete_reported", externally_verified: false,
  }, rehab_decision: { outcome: "medical_review", summary: "Get this injury checked before training.", reason_codes: ["red_flag_medical_gate"] } };
  const ui = mount();
  try {
    await act(async () => ui.root.render(<ToastProvider><TodayInjuryManager openInjuries={[injury]} token="token" onRefresh={async () => {}} /></ToastProvider>));
    const guidance = ui.container.querySelector('[aria-label="Guidance"]')!;
    assert.ok(guidance);
    assert.equal(guidance.closest("[hidden]"), null);
    assert.match(guidance.textContent ?? "", /reported warning sign needs medical review/);
    assert.match(guidance.textContent ?? "", /checked before training/);
    assert.equal(ui.container.querySelectorAll('[aria-label="Guidance"]').length, 1);
    assert.match(ui.container.querySelector(".injury-clearance")?.textContent ?? "", /Contact training/);
  } finally { ui.cleanup(); }
});
