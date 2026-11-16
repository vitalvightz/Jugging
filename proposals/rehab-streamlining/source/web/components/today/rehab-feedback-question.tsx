import React from "react";
import type { RehabDuringResponse } from "../../lib/types";

const ANSWERS: Array<{ value: RehabDuringResponse; label: string }> = [
  { value: "better", label: "Better" }, { value: "same", label: "Same" },
  { value: "worse", label: "Worse" }, { value: "not_sure", label: "Not sure" },
];

export function RehabFeedbackQuestion({ injury, question, options, disabled, onAnswer }: {
  injury: string; question: string; options: RehabDuringResponse[];
  disabled: boolean; onAnswer: (response: RehabDuringResponse) => void;
}) {
  return <>
    <p>{question}</p>
    <div className="today-action-row" role="group" aria-label={`${injury}: ${question}`}>
      {ANSWERS.filter(option => options.includes(option.value)).map(option =>
        <button key={option.value} type="button" className="secondary-button" disabled={disabled}
          onClick={() => onAnswer(option.value)}>{option.label}</button>)}
    </div>
  </>;
}
