import React from "react";
import { distinctGuidanceLines } from "../../lib/guidance-copy";
import type { TodayCommandView } from "../../lib/types";

export function TodaySessionGuidance({ prescription }: { prescription: TodayCommandView["live_prescription"] }) {
  if (!prescription?.safety_hold && !prescription?.rehab_only) return null;
  const lines = prescription.safety_hold
    ? [prescription.safety_hold_reason || "This session is on hold. Follow the current injury guidance before training."]
    : [prescription.training_hold_reason || "Your planned training is held by the current injury restrictions.", "Follow only the rehab shown today."];
  return <div className="today-injury-guidance" role={prescription.safety_hold ? "alert" : "note"}>
    {distinctGuidanceLines(...lines).map(line => <p key={line}>{line}</p>)}
    {prescription.safety_hold ? <a href="#today-injury">Review injury guidance</a> : null}
  </div>;
}
