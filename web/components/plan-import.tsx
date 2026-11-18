"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, importPlanText, type PlanImportResult } from "@/lib/api";
import type { CustomPlanInput, ImportFlag } from "@/lib/api-schema.generated";
import { nextCalendarMonday } from "@/lib/custom-plan-calendar";
import { describeLoadFailure } from "@/lib/connectivity";
import { MAX_SCREENSHOTS, PLAN_TEXT_MAX_CHARS, PlanFileError, extractPlanText, planFileKind, screenshotData } from "@/lib/plan-text";
import styles from "./plan-import.module.css";

type Stage = { step: "pick" } | { step: "reading" | "building"; name: string; kind: "file" | "images" | "paste" }
  | { step: "done"; name: string; result: PlanImportResult };

/**
 * Import a plan someone already follows: a PDF or Word file (its text is read
 * on this device), screenshots (redrawn as plain JPEGs, read once, never kept)
 * or pasted text. It's turned into weeks and sessions and opened in the
 * builder to check and edit. Nothing is saved until they save it there.
 */
export function PlanImport({ accessToken, timezone, onReview, onCancel, forFighter }: {
  accessToken: string;
  timezone?: string | null;
  onReview: (imported: ImportedPlan) => void;
  onCancel: () => void;
  /** A coach importing for a fighter: the wording names them. */
  forFighter?: string | null;
}) {
  const [stage, setStage] = useState<Stage>({ step: "pick" });
  const [problem, setProblem] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [pasted, setPasted] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  /** One PDF or Word file, or up to four screenshots; or pasted text. */
  async function run(source: { files: File[] } | { text: string }) {
    setProblem(null);
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const files = "files" in source ? source.files : [];
    const images = files.filter(file => planFileKind(file) === "image");
    const name = "text" in source ? "pasted text"
      : images.length ? images.length === 1 ? images[0].name : `${images.length} screenshots` : files[0].name;
    try {
      if (files.length > 1 && images.length !== files.length) throw new PlanFileError("Choose one PDF or Word file, or up to 4 screenshots.");
      if (images.length > MAX_SCREENSHOTS) throw new PlanFileError(`Choose up to ${MAX_SCREENSHOTS} screenshots.`);
      setStage({ step: "reading", name, kind: images.length ? "images" : "text" in source ? "paste" : "file" });
      const text = "text" in source ? source.text.slice(0, PLAN_TEXT_MAX_CHARS) : images.length ? "" : await extractPlanText(files[0]);
      if ("text" in source && text.replace(/\s/g, "").length < 20) throw new PlanFileError("Paste the whole programme: sessions, days and exercises.");
      const shots = await Promise.all(images.map(screenshotData));
      if (controller.signal.aborted) return;
      setStage({ step: "building", name, kind: images.length ? "images" : "text" in source ? "paste" : "file" });
      const result = await importPlanText(accessToken, {
        text, images: shots, file_name: name.slice(0, 200), start_date: nextCalendarMonday(timezone ?? "UTC"),
      }, controller.signal);
      if (!controller.signal.aborted) { setStage({ step: "done", name, result }); setPasting(false); setPasted(""); }
    } catch (error) {
      if (controller.signal.aborted) return;
      setStage({ step: "pick" });
      setProblem(error instanceof PlanFileError ? error.message
        : error instanceof ApiError && error.status < 500 ? error.message.replace(/\s*\(request id: [^)]*\)$/, "")
        : describeLoadFailure(error, "That plan couldn't be read just now. Try again in a minute."));
    } finally {
      if (input.current) input.current.value = "";
    }
  }

  function take(list: FileList | null | undefined) {
    const files = Array.from(list ?? []);
    if (files.length) void run({ files });
  }

  const busy = stage.step === "reading" || stage.step === "building";
  const first = busy && stage.kind === "images" ? "Prepare the screenshots on this device"
    : busy && stage.kind === "paste" ? "Take the pasted text" : "Read the file on this device";
  return <section className={styles.root} aria-labelledby="plan-import-heading">
    <header className={styles.head}>
      <p className="kicker">Import</p>
      <h1 id="plan-import-heading">Bring in {forFighter ? `${forFighter}'s` : "your"} plan</h1>
      <p className={styles.lede}>Upload a PDF, a Word file or screenshots, or paste the programme. We read the sessions and exercises, then you check and edit everything before it&apos;s saved.</p>
    </header>

    {stage.step === "done" ? <Result stage={stage} onReview={() => onReview({ plan: stage.result.plan, name: stage.name, flags: stage.result.flags })}
      onAnother={() => setStage({ step: "pick" })} />
      : <>
        {pasting && !busy ? <form className={styles.paste} onSubmit={event => { event.preventDefault(); void run({ text: pasted }); }}>
          <label htmlFor="plan-import-paste">Programme text</label>
          <textarea id="plan-import-paste" value={pasted} onChange={event => setPasted(event.target.value)} rows={10}
            maxLength={PLAN_TEXT_MAX_CHARS} placeholder={"Mon: Strength\nTrap bar deadlift 3x5 @ 140kg\nWed: Pads 6 x 3 min rounds"} autoFocus />
          <div className={styles.actions}>
            <button type="submit" className="cta" disabled={!pasted.trim()}>Import text</button>
            <button type="button" className={styles.cancel} onClick={() => setPasting(false)}>Upload a file instead</button>
          </div>
        </form>
        : <div className={styles.drop} data-over={over || undefined} data-busy={busy || undefined}
          onDragOver={event => { event.preventDefault(); if (!busy) setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={event => { event.preventDefault(); setOver(false); if (!busy) take(event.dataTransfer.files); }}>
          {busy ? <div className={styles.progress} role="status" aria-live="polite">
            <span className={styles.spinner} aria-hidden="true" />
            <strong className={styles.fileName}>{stage.name}</strong>
            <span>{stage.step === "reading" ? "Getting it ready…" : "Building weeks and sessions… this can take up to a minute."}</span>
            <ol className={styles.steps} aria-label="Import steps">
              <li data-state={stage.step === "reading" ? "now" : "done"}>{first}</li>
              <li data-state={stage.step === "building" ? "now" : "next"}>Turn it into sessions</li>
              <li data-state="next">You check and save</li>
            </ol>
          </div> : <>
            <svg className={styles.fileIcon} viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M9 13h6M9 17h4" /></svg>
            <button type="button" className="cta" onClick={() => input.current?.click()}>Choose a file</button>
            <span className={styles.hint}>PDF, Word (.docx) or up to {MAX_SCREENSHOTS} screenshots</span>
            <button type="button" className={styles.link} onClick={() => { setProblem(null); setPasting(true); }}>Paste text instead</button>
          </>}
        </div>}
        <input ref={input} type="file" hidden multiple aria-label="Plan file (PDF, Word or screenshots)"
          accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,image/jpeg,image/png,image/webp,image/heic,image/heif"
          onChange={event => take(event.target.files)} />
        {problem ? <p className={styles.problem} role="alert">{problem}</p> : null}
        <ul className={styles.notes}>
          <li>PDF and Word files stay on this device: only their text is sent. Screenshots are sent once to read them and aren&apos;t kept.</li>
          <li>For a scanned or paper plan, take screenshots or photos of each page.</li>
        </ul>
        <button type="button" className={styles.cancel} onClick={() => { request.current?.abort(); onCancel(); }}>
          {busy ? "Cancel import" : "Back"}</button>
      </>}
  </section>;
}

function Result({ stage, onReview, onAnother }: {
  stage: Extract<Stage, { step: "done" }>; onReview: () => void; onAnother: () => void;
}) {
  const { result } = stage;
  const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
  const weekly = result.plan.schedule_mode === "weekly";
  return <div className={styles.result}>
    <div className={styles.summary} role="status">
      <p className={styles.found}>Found in <strong className={styles.fileName}>{stage.name}</strong></p>
      <dl className={styles.counts}>
        <div><dt>{weekly ? "Repeats" : "Weeks"}</dt><dd>{weekly ? "Weekly" : result.weeks}</dd></div>
        <div><dt>Sessions</dt><dd>{result.sessions}</dd></div>
        <div><dt>Exercises</dt><dd>{result.exercises}</dd></div>
      </dl>
      <p className={styles.planName}>{result.plan.name}</p>
    </div>
    {result.flags.length ? <ImportFlags flags={result.flags} open /> : null}
    <p className={styles.check}>{result.flags.length ? "Everything else came in as written." : "Everything came in as written."} Check each session against your file before saving.</p>
    <div className={styles.actions}>
      <button type="button" className="cta" onClick={onReview}>Review and edit</button>
      <button type="button" className={styles.cancel} onClick={onAnother}>Choose another file</button>
    </div>
  </div>;
}

/** What couldn't be carried over exactly, grouped by where it belongs. */
export function ImportFlags({ flags, open }: { flags: ImportFlag[]; open?: boolean }) {
  const groups = new Map<string, string[]>();
  for (const flag of flags) groups.set(flag.where, [...(groups.get(flag.where) ?? []), flag.message]);
  return <details className={styles.flags} open={open}>
    <summary>{flags.length} {flags.length === 1 ? "thing" : "things"} to check</summary>
    <dl>{[...groups].map(([where, messages]) => <div key={where}><dt>{where}</dt>
      <dd>{messages.map((message, index) => <span key={index}>{message}</span>)}</dd></div>)}</dl>
  </details>;
}

export type ImportedPlan = { plan: CustomPlanInput; name: string; flags: ImportFlag[] };

// The imported draft travels to the builder page: in memory for an in-app
// move, and in this tab's session storage so a reload doesn't lose it.
const KEY = "unlxck:imported-plan";
let pending: ImportedPlan | null = null;

export function stashImportedPlan(imported: ImportedPlan) {
  pending = imported;
  try { sessionStorage.setItem(KEY, JSON.stringify(pending)); } catch { /* memory copy is enough */ }
}

export function takeImportedPlan(): ImportedPlan | null {
  let value = pending;
  if (!value) {
    try { value = JSON.parse(sessionStorage.getItem(KEY) ?? "null"); } catch { value = null; }
  }
  return value && Array.isArray(value.plan?.sessions) ? { ...value, flags: Array.isArray(value.flags) ? value.flags : [] } : null;
}

export function clearImportedPlan() {
  pending = null;
  try { sessionStorage.removeItem(KEY); } catch { /* nothing stored */ }
}
