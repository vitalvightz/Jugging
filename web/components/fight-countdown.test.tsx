import "./test-dom";

import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { FightCountdown, FightCountdownSkeleton } from "./fight-countdown";

test("server render shows the final day count, not the animation's start", () => {
  const html = renderToStaticMarkup(
    <FightCountdown
      fightDate="2026-06-27"
      trainingDay="2026-06-10"
      phase="SPP"
      plan={{ weeks: [{ phase_label: "SPP", start_date: "2026-06-01", end_date: "2026-06-27" }] }}
    />,
  );
  assert.match(html, /fight-countdown-number"[^>]*>17</);
  assert.match(html, /aria-label="Fight countdown: 17 days to fight night"/);
  assert.match(html, /fight-countdown-fill/);
});

test("renders nothing for an open plan", () => {
  assert.equal(renderToStaticMarkup(<FightCountdown fightDate={null} trainingDay="2026-06-10" />), "");
});

test("skeleton reuses the card's markup so it holds the card's exact size", () => {
  const html = renderToStaticMarkup(<FightCountdownSkeleton />);
  assert.match(html, /class="fight-countdown fight-countdown-skeleton"/);
  assert.match(html, /aria-busy="true"/);
  for (const part of ["fight-countdown-kicker", "fight-countdown-number", "fight-countdown-date", "fight-countdown-meta", "fight-countdown-track", "fight-countdown-next"]) {
    assert.match(html, new RegExp(part));
  }
});

const campPlan = { weeks: [{ phase_label: "SPP", start_date: "2026-06-01", end_date: "2026-06-27" }] };

test("shows the upcoming fight's event name and target fight weight", () => {
  const html = renderToStaticMarkup(
    <FightCountdown fightDate="2026-06-27" trainingDay="2026-06-10" phase="SPP" plan={campPlan}
      eventName="  London Regionals " targetWeightKg={90.7} />,
  );
  assert.match(html, /fight-countdown-kicker">Upcoming fight</);
  assert.match(html, /fight-countdown-event">London Regionals</);
  assert.match(html, /fight-countdown-weight-label">Fight weight</);
  assert.match(html, /fight-countdown-weight-value">90\.7 kg</);
  // Countdown, phase and progress are untouched.
  assert.match(html, /fight-countdown-number"[^>]*>17</);
  assert.match(html, /days to fight</);
  assert.match(html, /fight-countdown-date">27 Jun 2026</);
  assert.match(html, /fight-countdown-meta/);
  assert.match(html, /role="progressbar"/);
});

test("falls back to Fight Camp and drops the weight section when neither is set", () => {
  for (const props of [{}, { eventName: "   ", targetWeightKg: null }, { eventName: null, targetWeightKg: 0 }]) {
    const html = renderToStaticMarkup(
      <FightCountdown fightDate="2026-06-27" trainingDay="2026-06-10" phase="SPP" plan={campPlan} {...props} />,
    );
    assert.match(html, /fight-countdown-event fight-countdown-event-side">Fight Camp</);
    assert.doesNotMatch(html, /fight-countdown-weight/);
    assert.match(html, /fight-countdown-number"[^>]*>17</);
  }
});

test("a long event name renders whole (the CSS clamps it, nothing is cut in markup)", () => {
  const name = "IBJJF European Open Jiu-Jitsu Championship Masters Division Lisbon 2026";
  const html = renderToStaticMarkup(
    <FightCountdown fightDate="2026-06-27" trainingDay="2026-06-10" eventName={name} />,
  );
  assert.match(html, new RegExp(`fight-countdown-event[^"]*">${name}<`));
});

test("while the calendar loads, the week line, bar and next line hold their space as shimmer", () => {
  const html = renderToStaticMarkup(
    <FightCountdown fightDate="2026-06-27" trainingDay="2026-06-10" phase="SPP" calendarPending />,
  );
  assert.match(html, /aria-busy="true"/);
  assert.match(html, /fight-countdown-meta" aria-hidden="true"><span class="skeleton/);
  assert.match(html, /fight-countdown-track" aria-hidden="true"/);
  assert.match(html, /fight-countdown-next" aria-hidden="true"><span class="skeleton/);
  assert.doesNotMatch(html, /role="progressbar"/);
  // The countdown itself is real from the start.
  assert.match(html, /fight-countdown-number"[^>]*>17</);
});

test("a calendar that was already loaded renders without the settle-in fade", () => {
  const html = renderToStaticMarkup(
    <FightCountdown fightDate="2026-06-27" trainingDay="2026-06-10" phase="SPP" plan={campPlan} />,
  );
  assert.doesNotMatch(html, /data-arrived|aria-busy/);
  assert.match(html, /role="progressbar"/);
});
