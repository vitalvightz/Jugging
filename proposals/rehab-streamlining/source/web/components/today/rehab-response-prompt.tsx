"use client";

import { useState } from "react";
import { submitInjuryEpisodeObservation, submitRehabResponses } from "@/lib/api";
import type { RehabDuringResponse, RehabResponsePrompt as Prompt, TodayCommandView } from "@/lib/types";
import { RehabFeedbackQuestion } from "./rehab-feedback-question";

/** A tap records one observation; completion never supplies an answer for it. */
export function RehabResponsePrompt({ token, planId, sessionId, trainingDay, prompts, onDismiss, onRefresh }: {
  token: string | null; planId: string; sessionId: string; trainingDay: string;
  prompts: Prompt[]; onDismiss: () => void; onRefresh?: () => Promise<void>;
}) {
  const [saved, setSaved] = useState<string[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const unique = prompts.filter((prompt, index) => prompts.findIndex(other =>
    other.injury_id === prompt.injury_id && other.injury_episode_id === prompt.injury_episode_id) === index);
  async function answer(prompt: Prompt, response: RehabDuringResponse) {
    if (!token || pending) return;
    const key = `${prompt.injury_id}:${prompt.injury_episode_id}`;
    setPending(key);
    setError(null);
    try {
      await submitRehabResponses(token, { plan_id: planId, session_id: sessionId, training_day: trainingDay,
        answers: [{ injury_id: prompt.injury_id, injury_episode_id: prompt.injury_episode_id, during_response: response }] });
      const next = [...saved, key];
      setSaved(next);
      if (unique.every(item => next.includes(`${item.injury_id}:${item.injury_episode_id}`))) onDismiss();
      await onRefresh?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save your response. Try again.");
    } finally { setPending(null); }
  }
  return <div className="today-injury-guidance" aria-label="Rehab feedback">
    {unique.filter(prompt => !saved.includes(`${prompt.injury_id}:${prompt.injury_episode_id}`)).map(prompt =>
      <div key={`${prompt.injury_id}:${prompt.injury_episode_id}`}>
        <p><strong>{prompt.injury_label}</strong> · {trainingDay}</p>
        <RehabFeedbackQuestion injury={prompt.injury_label} question={prompt.during_question} options={prompt.during_options}
          disabled={!token || pending !== null} onAnswer={response => void answer(prompt, response)} />
      </div>)}
    {error ? <p role="alert">{error}</p> : null}
    <button type="button" className="ghost-button" disabled={pending !== null} onClick={onDismiss}>Later</button>
  </div>;
}

type DelayedPrompt = NonNullable<TodayCommandView["delayed_rehab_prompts"]>[number];

export function DelayedRehabResponse({ prompt, token, onRefresh }: {
  prompt: DelayedPrompt; token: string | null; onRefresh: () => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function answer(response: RehabDuringResponse) {
    if (!token || pending || saved) return;
    setPending(true);
    setError(null);
    try {
      await submitInjuryEpisodeObservation(token, { injury_id: prompt.injury_id, injury_episode_id: prompt.injury_episode_id,
        event_type: "delayed_rehab_response", exposure_id: prompt.exposure_id, response });
      setSaved(true);
      await onRefresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save your response. Try again.");
    } finally { setPending(false); }
  }
  if (saved) return null;
  return <div className="today-injury-guidance" aria-label="Next-day rehab feedback">
    <p><strong>{prompt.region.replaceAll("_", " ")}</strong> · Next-day check</p>
    <RehabFeedbackQuestion injury={prompt.region} question={prompt.question} options={prompt.options}
      disabled={!token || pending} onAnswer={response => void answer(response)} />
    {error ? <p role="alert">{error}</p> : null}
  </div>;
}
